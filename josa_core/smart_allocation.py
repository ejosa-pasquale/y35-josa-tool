"""
josa_core.smart_allocation — allocazione intelligente della potenza di ricarica
per il dimensionamento infrastruttura.

Sostituisce, come motore ALTERNATIVO opzionale (non il default, per non rompere
nulla di gia' validato), la logica di assegnazione greedy a colonnine esclusive
di `simulazione_soc` con un'allocazione congiunta: le colonnine di ogni tipo
sono un POOL condiviso (n_T punti da p_T kW ciascuno), e la potenza erogata a
ciascun veicolo in ogni istante e' decisa risolvendo un problema di
ottimizzazione (MILP) che minimizza il picco aggregato, rispettando:
  - la disponibilita' reale di ciascun veicolo (quando e' in deposito, non in
    marcia — derivato dagli stessi eventi generati da josa_core.fleet_events);
  - il fabbisogno energetico giornaliero di ciascun veicolo;
  - il numero fisico di punti disponibili per tipo (non si puo' collegare piu'
    veicoli di quanti punti esistano di quel tipo, nello stesso istante);
  - il limite di rete/peak-shaving del sito.

IMPORTANTE — cosa NON fa di proposito: nessuna scarica/V2G. Il V2G e' e resta
uno strato di valutazione separato (vedi josa_core.ems), applicato DOPO aver
dimensionato — includerlo qui spingerebbe la logica verso "un punto per
veicolo" per garantire a tutti la capacita' di scarica, il che e' l'opposto
dell'obiettivo di condividere l'infrastruttura tra piu' veicoli.
"""

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds


@dataclass
class SmartVehicle:
    """Un veicolo per l'allocazione intelligente, derivato dagli eventi flotta."""
    id: str
    capacita_kwh: float
    soc_iniziale_pct: float
    soc_min_pct: float
    energia_richiesta_kwh: float  # fabbisogno energetico giornaliero da coprire
    disponibile: list  # list[bool], lunghezza = n_timestep (True = in deposito, puo' caricare)
    tipi_ammessi: list  # nomi tipo hardware che questo veicolo puo' usare (es. tutti, o solo AC)
    potenza_max_ac_kw: float = 11.0  # limite del caricatore di bordo, indipendente dalla potenza
    # nominale della colonnina — stessa logica/default di simulazione_soc, per coerenza tra i due motori


@dataclass
class SmartAllocationResult:
    successo: bool
    messaggio: str
    picco_kw: float = 0.0
    energia_totale_kwh: float = 0.0
    copertura_pct: float = 100.0
    veicoli_non_serviti: list = field(default_factory=list)
    potenza_timeline_kw: list = field(default_factory=list)
    utilizzo_per_tipo: dict = field(default_factory=dict)  # tipo -> lista occupazione nel tempo (0..n_T)


def allocate_smart(
    vehicles: list,          # list[SmartVehicle]
    hw_db: dict,             # {tipo: {"p": kW, ...}}
    config: dict,            # {tipo: quantita}
    n_timestep: int,
    durata_timestep_h: float,
    p_rete_max_kw: float,
    connessione_binaria: bool = False,
) -> SmartAllocationResult:
    """Risolve l'allocazione di potenza che minimizza il picco aggregato, dato un
    pool condiviso di colonnine per tipo (non assegnazione esclusiva 1:1).

    connessione_binaria: se False (default), le variabili di connessione sono
    continue in [0,1] invece che binarie — il problema diventa un programma
    lineare (LP) invece che lineare-intero (MILP), molto piu' veloce da
    risolvere (da ~20s a <1s su 30 veicoli). Giustificazione: a risoluzione
    oraria, una connessione "frazionaria" rappresenta realisticamente una
    condivisione del punto fisico ENTRO l'ora (es. due veicoli usano lo stesso
    punto per 30 minuti ciascuno) — non un'approssimazione arbitraria. Mettere
    connessione_binaria=True forza l'esatta formulazione MILP (piu' lenta),
    utile per una verifica puntuale finale su una configurazione gia' scelta,
    non per esplorare molte configurazioni in un ciclo di ottimizzazione.

    Se il problema risulta infeasible (energia richiesta non copribile nei
    vincoli dati), ritorna successo=False con messaggio esplicativo — non
    forza una soluzione approssimata silenziosa.
    """
    tipi = [t for t, q in config.items() if int(q) > 0]
    if not tipi or not vehicles:
        return SmartAllocationResult(successo=True, messaggio="Nessun veicolo o hardware da allocare.")

    n_v = len(vehicles)
    n_t = n_timestep
    dt = durata_timestep_h

    # --- Layout variabili: [charge(nV*nT*nTipi), connect_bin(nV*nT*nTipi), peak(1)] ---
    n_tipi = len(tipi)
    n_charge = n_v * n_t * n_tipi
    n_connect = n_v * n_t * n_tipi
    n_vars = n_charge + n_connect + 1
    idx_peak = n_vars - 1

    def idx_charge(vi, ti, k):
        return (vi * n_t + ti) * n_tipi + k

    def idx_connect(vi, ti, k):
        return n_charge + (vi * n_t + ti) * n_tipi + k

    p_tipo = [float(hw_db[t]["p"]) for t in tipi]
    n_punti = [int(config[t]) for t in tipi]

    # --- Bounds ---
    lb = np.zeros(n_vars)
    ub = np.zeros(n_vars)
    integrality = np.zeros(n_vars)  # 0 = continua, 1 = intera

    for vi, v in enumerate(vehicles):
        ammessi = set(v.tipi_ammessi) if v.tipi_ammessi else set(tipi)
        for ti in range(n_t):
            disp = bool(v.disponibile[ti]) if ti < len(v.disponibile) else False
            for k, t in enumerate(tipi):
                can_use = disp and (t in ammessi)
                # Stesso limite del motore principale: la potenza di ricarica AC e' il minimo
                # tra la potenza nominale della colonnina e il limite del caricatore di bordo
                # del veicolo — prima assente qui, causa di inconsistenza tra i due motori.
                p_effettiva = min(p_tipo[k], v.potenza_max_ac_kw) if "AC" in str(t).upper() else p_tipo[k]
                ub[idx_charge(vi, ti, k)] = p_effettiva if can_use else 0.0
                ub[idx_connect(vi, ti, k)] = 1.0 if can_use else 0.0
                integrality[idx_connect(vi, ti, k)] = 1 if connessione_binaria else 0

    ub[idx_peak] = max(p_rete_max_kw, 1e-6)
    lb[idx_peak] = 0.0

    bounds = Bounds(lb, ub)

    # --- Obiettivo: minimizza il picco (unico termine, e' l'obiettivo del confronto richiesto) ---
    c = np.zeros(n_vars)
    c[idx_peak] = 1.0

    A_rows = []
    b_lb = []
    b_ub = []

    # 1) charge_kw[v,t,k] <= p_tipo[k] * connect[v,t,k]  ->  charge - p*connect <= 0
    for vi in range(n_v):
        for ti in range(n_t):
            for k in range(n_tipi):
                row = np.zeros(n_vars)
                row[idx_charge(vi, ti, k)] = 1.0
                row[idx_connect(vi, ti, k)] = -p_tipo[k]
                A_rows.append(row); b_lb.append(-np.inf); b_ub.append(0.0)

    # 2) Pool condiviso: sum_v connect[v,t,k] <= n_punti[k], per ogni tipo/istante
    for ti in range(n_t):
        for k in range(n_tipi):
            row = np.zeros(n_vars)
            for vi in range(n_v):
                row[idx_connect(vi, ti, k)] = 1.0
            A_rows.append(row); b_lb.append(-np.inf); b_ub.append(float(n_punti[k]))

    # 3) Epigrafe del picco: sum_{v,k} charge[v,t,k] - peak <= 0, per ogni istante
    for ti in range(n_t):
        row = np.zeros(n_vars)
        for vi in range(n_v):
            for k in range(n_tipi):
                row[idx_charge(vi, ti, k)] = 1.0
        row[idx_peak] = -1.0
        A_rows.append(row); b_lb.append(-np.inf); b_ub.append(0.0)

    # 4) Fabbisogno energetico giornaliero per veicolo: sum_{t,k} charge[v,t,k]*dt >= energia_richiesta
    #    (vincolo hard: se non soddisfacibile, il problema risulta infeasible — non si approssima in silenzio)
    for vi, v in enumerate(vehicles):
        row = np.zeros(n_vars)
        for ti in range(n_t):
            for k in range(n_tipi):
                row[idx_charge(vi, ti, k)] = dt
        A_rows.append(row); b_lb.append(float(v.energia_richiesta_kwh)); b_ub.append(np.inf)

    A = np.array(A_rows)
    constraints = LinearConstraint(A, np.array(b_lb), np.array(b_ub))

    res = milp(c, constraints=constraints, bounds=bounds, integrality=integrality)

    if not res.success:
        return SmartAllocationResult(
            successo=False,
            messaggio=(
                "Allocazione non risolvibile con i vincoli dati: energia richiesta non copribile "
                "nella disponibilita'/potenza esistente, oppure limite di rete troppo basso. "
                f"Dettaglio solver: {res.message}"
            ),
        )

    x = res.x
    timeline = [
        sum(x[idx_charge(vi, ti, k)] for vi in range(n_v) for k in range(n_tipi))
        for ti in range(n_t)
    ]
    utilizzo = {
        t: [sum(x[idx_connect(vi, ti, k)] for vi in range(n_v)) for ti in range(n_t)]
        for k, t in enumerate(tipi)
    }
    energia_totale = sum(timeline) * dt

    return SmartAllocationResult(
        successo=True,
        messaggio="ok",
        picco_kw=float(x[idx_peak]),
        energia_totale_kwh=float(energia_totale),
        copertura_pct=100.0,  # se il solver ha trovato soluzione, il vincolo (4) e' soddisfatto per tutti
        veicoli_non_serviti=[],
        potenza_timeline_kw=[float(v) for v in timeline],
        utilizzo_per_tipo=utilizzo,
    )
