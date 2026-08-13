"""
josa_core.ems.dispatch — motore di dispacciamento energetico (V2G come DER).

Risolve, per un dato orizzonte temporale discretizzato, quanto ogni veicolo
deve caricare/scaricare in ogni intervallo per minimizzare il costo energetico
complessivo del sito, rispettando:
  - il vincolo hard di mobilita' (SoC minimo all'orario di partenza previsto);
  - i limiti di potenza di rete (peak shaving);
  - la disponibilita' fisica di ciascun veicolo (in deposito o meno) e la
    capacita' bidirezionale del suo punto di ricarica;
  - il costo di degrado batteria per l'energia scaricata in V2G.

Formulato come programma lineare; le variabili di carica/scarica sono sempre
continue. Se punti_disponibili_per_tipo NON e' fornito (caso semplice, un
punto dedicato per veicolo): nessuna variabile binaria, un ottimo lineare non
ha mai convenienza a caricare e scaricare lo stesso veicolo nello stesso
istante (costo puro senza beneficio), quindi il comportamento "o carica o
scarica" emerge naturalmente. Se invece e' fornito (pool condiviso di punti
fisici tra piu' veicoli): si aggiungono variabili binarie "connect" (connesso
o no a un punto fisico in quell'istante) per rappresentare correttamente che
un connettore e' o libero o occupato — non frazionabile tra piu' veicoli.

Pensato per essere richiamato in modalita' MPC: si risolve per l'intero
orizzonte, si applicano solo le decisioni del primo intervallo, poi si
ri-ottimizza al passo successivo con dati aggiornati (nuove previsioni, nuovo
SoC reale misurato). Questo modulo risolve un singolo passo; il ciclo di
ri-ottimizzazione e' responsabilita' del chiamante (vedi modalita' "planning"
vs "operativa" nel report di design).

Solver: scipy.optimize.linprog (backend HiGHS, incluso in scipy, nessuna
dipendenza aggiuntiva a pagamento).
"""

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy.optimize import linprog

try:
    from .assets import VehicleAsset, ChargerAsset, DispatchHorizon, SiteForecast
except ImportError:
    VehicleAsset = ChargerAsset = DispatchHorizon = SiteForecast = None


@dataclass
class VehiclePlan:
    vehicle_id: str
    carica_kw: list       # lunghezza n_timestep
    scarica_kw: list
    soc_pct: list         # SoC a fine di ogni timestep


@dataclass
class DispatchResult:
    successo: bool
    messaggio: str
    piani_veicolo: list = field(default_factory=list)  # list[VehiclePlan]
    prelievo_rete_kw: list = field(default_factory=list)
    immissione_rete_kw: list = field(default_factory=list)
    picco_kw: float = 0.0
    costo_totale_eur: float = 0.0
    costo_energia_eur: float = 0.0
    costo_potenza_impegnata_eur: float = 0.0
    costo_degrado_eur: float = 0.0
    ricavo_vendita_eur: float = 0.0


def solve_dispatch(
    horizon: DispatchHorizon,
    vehicles: list,      # list[VehicleAsset]
    chargers: dict,       # vehicle_id -> ChargerAsset
    forecast: SiteForecast,
    punti_disponibili_per_tipo: Optional[dict] = None,  # {"AC 22kW": 3, "DC 30kW": 1, ...}
) -> DispatchResult:
    """Risolve un singolo passo di ottimizzazione MPC sull'orizzonte dato.

    Il chiamante e' responsabile di aggiornare vehicles/forecast e richiamare
    di nuovo questa funzione ad ogni passo di ri-ottimizzazione (rolling horizon).

    punti_disponibili_per_tipo: se fornito, impone che i veicoli con lo stesso
    ChargerAsset.tipo condividano un numero LIMITATO di punti fisici, invece di
    assumere (come faceva prima) che ogni veicolo abbia sempre un proprio punto
    dedicato disponibile. Es. 5 veicoli di tipo "AC 22kW" con
    punti_disponibili_per_tipo={"AC 22kW": 3} non possono mai avere piu' di 3
    di loro attivi (in carica o scarica) nello stesso istante — coerente con
    quanti punti fisici esistono davvero in sede. Se None (default), nessun
    vincolo di pool: comportamento identico a prima (retrocompatibile).
    """
    T = horizon.n_timestep
    dt = horizon.durata_timestep_h
    nV = len(vehicles)

    if nV == 0:
        return DispatchResult(successo=True, messaggio="Nessun veicolo da dispacciare.")

    for name, lst in [("carico_edificio_kw", forecast.carico_edificio_kw),
                       ("produzione_fv_kw", forecast.produzione_fv_kw),
                       ("prezzo_acquisto_eur_kwh", forecast.prezzo_acquisto_eur_kwh),
                       ("prezzo_vendita_eur_kwh", forecast.prezzo_vendita_eur_kwh)]:
        if len(lst) != T:
            raise ValueError(f"forecast.{name} deve avere lunghezza {T} (n_timestep), trovata {len(lst)}")
    for v in vehicles:
        if len(v.disponibile) != T:
            raise ValueError(f"veicolo '{v.id}': disponibile deve avere lunghezza {T}")

    # --- Layout variabili ---
    # Caso semplice (nessun vincolo di pool): [charge(nV*T), discharge(nV*T), grid_import(T), grid_export(T), peak(1)]
    # Caso con pool: si aggiungono variabili "connect" (nV*T) continue in [0,1] — quota
    # di timestep in cui il veicolo occupa fisicamente un connettore. Necessarie per
    # imporre un vincolo sul NUMERO di connessioni simultanee (non sulla potenza
    # aggregata: un tentativo precedente vincolava solo la potenza totale, ma questo
    # permetteva erroneamente a piu' veicoli di "condividere" un connettore a bassa
    # potenza ciascuno — fisicamente impossibile, un connettore e' o libero o occupato).
    usa_pool = bool(punti_disponibili_per_tipo)
    n_charge = nV * T
    n_discharge = nV * T
    n_connect = nV * T if usa_pool else 0
    n_grid_import = T
    n_grid_export = T
    n_peak = 1
    n_vars = n_charge + n_discharge + n_connect + n_grid_import + n_grid_export + n_peak

    def idx_charge(i, t):
        return i * T + t

    def idx_discharge(i, t):
        return n_charge + i * T + t

    def idx_connect(i, t):
        return n_charge + n_discharge + i * T + t

    def idx_grid_import(t):
        return n_charge + n_discharge + n_connect + t

    def idx_grid_export(t):
        return n_charge + n_discharge + n_connect + n_grid_import + t

    idx_peak = n_vars - 1

    # --- Funzione obiettivo ---
    c = np.zeros(n_vars)
    for t in range(T):
        c[idx_grid_import(t)] += forecast.prezzo_acquisto_eur_kwh[t] * dt
        c[idx_grid_export(t)] += -forecast.prezzo_vendita_eur_kwh[t] * dt
    for i, v in enumerate(vehicles):
        for t in range(T):
            # Il costo di degrado e' pesato dalla priorita' del veicolo: a parita' di
            # beneficio economico, il motore preferisce scaricare veicoli a priorita'
            # piu' bassa (una priorita' piu' alta rende la scarica "piu' costosa" agli
            # occhi dell'ottimizzatore, senza vietarla se davvero conveniente).
            c[idx_discharge(i, t)] += v.costo_degrado_eur_kwh * v.priorita * dt
    c[idx_peak] += forecast.costo_potenza_impegnata_eur_kw

    # --- Bounds ---
    bounds = [(0, None)] * n_vars

    for i, v in enumerate(vehicles):
        charger = chargers.get(v.id)
        p_max = float(charger.potenza_kw) if charger else 0.0
        v2g_ok = bool(charger.v2g_capace) if charger else False
        for t in range(T):
            disp = bool(v.disponibile[t])
            bounds[idx_charge(i, t)] = (0, p_max if disp else 0.0)
            bounds[idx_discharge(i, t)] = (0, (p_max if (disp and v2g_ok) else 0.0))
            if usa_pool:
                bounds[idx_connect(i, t)] = (0, 1.0 if disp else 0.0)

    for t in range(T):
        bounds[idx_grid_import(t)] = (0, forecast.p_rete_max_kw)
        bounds[idx_grid_export(t)] = (0, None)

    bounds[idx_peak] = (0, forecast.p_rete_max_kw)

    # --- Vincoli di uguaglianza: bilancio energetico per ogni timestep ---
    A_eq = []
    b_eq = []
    for t in range(T):
        row = np.zeros(n_vars)
        row[idx_grid_import(t)] = 1.0
        row[idx_grid_export(t)] = -1.0
        for i in range(nV):
            row[idx_discharge(i, t)] += 1.0
            row[idx_charge(i, t)] += -1.0
        A_eq.append(row)
        b_eq.append(forecast.carico_edificio_kw[t] - forecast.produzione_fv_kw[t])

    # --- Vincoli di disuguaglianza ---
    A_ub = []
    b_ub = []

    # Epigrafe del picco: grid_import[t] - peak <= 0
    for t in range(T):
        row = np.zeros(n_vars)
        row[idx_grid_import(t)] = 1.0
        row[idx_peak] = -1.0
        A_ub.append(row)
        b_ub.append(0.0)

    # Nota: il vincolo di pool condiviso (quanti veicoli possono essere fisicamente
    # connessi insieme per tipo di colonnina) e' imposto piu' sotto, dopo il vincolo
    # di SoC, con variabili di connessione binarie — vedi commento li' per il perche'.

    # SoC cumulato entro i limiti (min/max) e vincolo di partenza, per ogni veicolo/timestep.
    # soc(t) = soc0 + sum_{k<=t} (eta_c*charge[k] - discharge[k]/eta_d) * dt / capacita * 100
    for i, v in enumerate(vehicles):
        cap = v.capacita_kwh
        # La probabilita' di utilizzo alza la riserva minima effettiva: un veicolo che
        # ha alta probabilita' di essere usato a breve viene scaricato in modo piu'
        # prudente (fino a +20 punti percentuali di riserva quando probabilita'=1),
        # anche fuori dal solo vincolo puntuale alla partenza.
        margine_prudenziale_pct = 20.0 * max(0.0, min(1.0, v.probabilita_utilizzo))
        soc_min_effettivo = min(v.soc_max_pct, v.soc_min_pct + margine_prudenziale_pct)
        # Il margine prudenziale serve a impedire che la SCARICA V2G porti il veicolo
        # troppo in basso — non deve mai richiedere che un veicolo APPENA ARRIVATO,
        # ancora in attesa del proprio turno su un punto condiviso, "salga" artificialmente
        # fino al margine prima ancora di aver iniziato a caricare (bug reale: senza
        # questo min(), un veicolo con SoC iniziale sotto il margine risultava
        # "infeasible" al solo fatto di aspettare, anche con capacita' di potenza
        # ampiamente sufficiente — scoperto introducendo il vincolo di pool condiviso).
        floor_pre_partenza = min(soc_min_effettivo, v.soc_iniziale_pct)
        lb_series = [floor_pre_partenza] * T
        if v.timestep_partenza is not None and 0 <= v.timestep_partenza < T:
            lb_series[v.timestep_partenza] = max(v.soc_min_pct, v.soc_minimo_alla_partenza_pct)

        for t in range(T):
            row_hi = np.zeros(n_vars)
            for k in range(t + 1):
                row_hi[idx_charge(i, k)] += (v.rendimento_carica * dt / cap * 100.0)
                row_hi[idx_discharge(i, k)] += -(dt / (cap * v.rendimento_scarica) * 100.0)
            A_ub.append(row_hi)
            b_ub.append(v.soc_max_pct - v.soc_iniziale_pct)

            row_lo = -row_hi
            A_ub.append(row_lo)
            b_ub.append(v.soc_iniziale_pct - lb_series[t])

    # --- Vincolo di pool condiviso (opzionale) ---
    # Corretto rispetto a un primo tentativo: vincolare solo la potenza aggregata
    # permetteva erroneamente a piu' veicoli di "condividere" un connettore a bassa
    # potenza ciascuno — fisicamente impossibile. Il vincolo giusto e' sul NUMERO di
    # connessioni simultanee: charge/discharge di un veicolo sono ammessi solo se
    # quel veicolo e' "connect"-ato (variabile continua 0-1, rilassamento LP dello
    # stato binario connesso/non connesso), e per ogni tipo la somma delle
    # connessioni in ogni istante non supera il numero di punti fisici disponibili.
    if usa_pool:
        tipi_presenti = {}
        for i, v in enumerate(vehicles):
            charger = chargers.get(v.id)
            tipo = getattr(charger, "tipo", "generico") if charger else "generico"
            tipi_presenti.setdefault(tipo, []).append(i)

        for i, v in enumerate(vehicles):
            charger = chargers.get(v.id)
            p_max = float(charger.potenza_kw) if charger else 0.0
            v2g_ok = bool(charger.v2g_capace) if charger else False
            for t in range(T):
                # charge[i,t] - p_max*connect[i,t] <= 0
                row = np.zeros(n_vars)
                row[idx_charge(i, t)] = 1.0
                row[idx_connect(i, t)] = -p_max
                A_ub.append(row)
                b_ub.append(0.0)
                if v2g_ok:
                    row2 = np.zeros(n_vars)
                    row2[idx_discharge(i, t)] = 1.0
                    row2[idx_connect(i, t)] = -p_max
                    A_ub.append(row2)
                    b_ub.append(0.0)

        for tipo, indici in tipi_presenti.items():
            n_punti = punti_disponibili_per_tipo.get(tipo)
            if n_punti is None:
                continue  # tipo non vincolato esplicitamente: nessun limite di pool
            for t in range(T):
                row = np.zeros(n_vars)
                for i in indici:
                    row[idx_connect(i, t)] += 1.0
                A_ub.append(row)
                b_ub.append(float(n_punti))

    A_eq = np.array(A_eq)
    b_eq = np.array(b_eq)
    A_ub = np.array(A_ub)
    b_ub = np.array(b_ub)

    integrality = np.zeros(n_vars)
    if usa_pool:
        for i in range(nV):
            for t in range(T):
                integrality[idx_connect(i, t)] = 1  # binaria: connesso o no, non frazionabile

    res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds,
                  integrality=integrality, method="highs")

    if not res.success:
        return DispatchResult(successo=False, messaggio=f"Ottimizzazione non risolta: {res.message}")

    x = res.x
    piani = []
    for i, v in enumerate(vehicles):
        carica = [float(x[idx_charge(i, t)]) for t in range(T)]
        scarica = [float(x[idx_discharge(i, t)]) for t in range(T)]
        soc = []
        cum = v.soc_iniziale_pct
        for t in range(T):
            cum += (v.rendimento_carica * carica[t] - scarica[t] / v.rendimento_scarica) * dt / v.capacita_kwh * 100.0
            soc.append(cum)
        piani.append(VehiclePlan(vehicle_id=v.id, carica_kw=carica, scarica_kw=scarica, soc_pct=soc))

    prelievo = [float(x[idx_grid_import(t)]) for t in range(T)]
    immissione = [float(x[idx_grid_export(t)]) for t in range(T)]
    picco = float(x[idx_peak])

    costo_energia = sum(forecast.prezzo_acquisto_eur_kwh[t] * prelievo[t] * dt for t in range(T))
    ricavo_vendita = sum(forecast.prezzo_vendita_eur_kwh[t] * immissione[t] * dt for t in range(T))
    costo_potenza = forecast.costo_potenza_impegnata_eur_kw * picco
    costo_degrado = sum(
        v.costo_degrado_eur_kwh * piani[i].scarica_kw[t] * dt
        for i, v in enumerate(vehicles) for t in range(T)
    )

    return DispatchResult(
        successo=True,
        messaggio="ok",
        piani_veicolo=piani,
        prelievo_rete_kw=prelievo,
        immissione_rete_kw=immissione,
        picco_kw=picco,
        costo_totale_eur=float(res.fun),
        costo_energia_eur=costo_energia,
        costo_potenza_impegnata_eur=costo_potenza,
        costo_degrado_eur=costo_degrado,
        ricavo_vendita_eur=ricavo_vendita,
    )
