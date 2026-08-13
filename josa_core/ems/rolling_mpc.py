"""
josa_core.ems.rolling_mpc — ciclo di ri-ottimizzazione a orizzonte scorrevole
(Model Predictive Control) su un orizzonte multi-giorno.

`dispatch.solve_dispatch` risolve un singolo passo di ottimizzazione su una
finestra fissa. Questo modulo orchestra la ripetizione: ad ogni passo di
controllo, costruisce la finestra locale (le prossime N ore a partire da
"adesso"), risolve, applica SOLO la decisione del primo intervallo usando i
valori realizzati (non quelli previsti — cosi' l'errore di previsione emerge
nel costo effettivo), avanza lo stato, e ripete.

Include anche una policy di confronto "ricarica ingenua" (senza ottimizzazione,
senza mai scaricare) per quantificare il valore aggiunto del dispacciamento
intelligente — numero che serve poi al modulo business_model.py per giustificare
l'investimento in hardware V2G o per popolare il modello Pay-per-Use.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    from .assets import VehicleAsset, ChargerAsset, DispatchHorizon, SiteForecast
except ImportError:
    VehicleAsset = ChargerAsset = DispatchHorizon = SiteForecast = None
from .dispatch import solve_dispatch, DispatchResult


@dataclass
class VehicleSchedule:
    """Comportamento di un veicolo su un orizzonte multi-giorno, in indici assoluti."""
    id: str
    capacita_kwh: float
    soc_iniziale_pct: float = 50.0
    soc_min_pct: float = 15.0
    soc_max_pct: float = 100.0
    rendimento_carica: float = 0.95
    rendimento_scarica: float = 0.95
    priorita: float = 1.0
    probabilita_utilizzo: float = 1.0
    costo_degrado_eur_kwh: float = 0.05
    disponibile_assoluto: list = field(default_factory=list)  # bool, lunghezza = n_timestep totale
    # Lista di (timestep assoluto di partenza, soc minimo richiesto in quel momento)
    partenze: list = field(default_factory=list)


@dataclass
class MultiDayResult:
    n_timestep: int
    costo_totale_reale_eur: float
    costo_energia_reale_eur: float
    costo_potenza_impegnata_reale_eur: float
    costo_degrado_reale_eur: float
    ricavo_vendita_reale_eur: float
    picco_reale_kw: float
    soc_traiettoria: dict  # vehicle_id -> list[float], lunghezza n_timestep
    prelievo_rete_kw: list
    immissione_rete_kw: list
    vincoli_partenza_rispettati: bool
    dettaglio_violazioni: list


def _local_window(t0: int, horizon_len: int, n_timestep_totale: int) -> int:
    """Lunghezza effettiva della finestra locale (ridotta vicino alla fine dell'orizzonte totale)."""
    return min(horizon_len, n_timestep_totale - t0)


def _build_local_vehicle(schedule: VehicleSchedule, t0: int, win: int, soc_corrente: float) -> VehicleAsset:
    disponibile_locale = schedule.disponibile_assoluto[t0:t0 + win]
    # prossima partenza dentro la finestra locale, se c'e'
    timestep_partenza_locale = None
    soc_min_partenza = schedule.soc_min_pct
    for (t_assoluto, soc_richiesto) in schedule.partenze:
        if t0 <= t_assoluto < t0 + win:
            timestep_partenza_locale = t_assoluto - t0
            soc_min_partenza = soc_richiesto
            break  # solo la prossima partenza entro la finestra: e' quella vincolante
    return VehicleAsset(
        id=schedule.id,
        capacita_kwh=schedule.capacita_kwh,
        soc_iniziale_pct=soc_corrente,
        soc_min_pct=schedule.soc_min_pct,
        soc_max_pct=schedule.soc_max_pct,
        rendimento_carica=schedule.rendimento_carica,
        rendimento_scarica=schedule.rendimento_scarica,
        timestep_partenza=timestep_partenza_locale,
        soc_minimo_alla_partenza_pct=soc_min_partenza,
        disponibile=disponibile_locale,
        priorita=schedule.priorita,
        probabilita_utilizzo=schedule.probabilita_utilizzo,
        costo_degrado_eur_kwh=schedule.costo_degrado_eur_kwh,
    )


def run_rolling_mpc(
    schedules: list,          # list[VehicleSchedule]
    chargers: dict,            # vehicle_id -> ChargerAsset
    forecast_provider: Callable[[int, int], SiteForecast],  # (t0, win) -> previsione per la finestra locale
    actual_provider: Callable[[int], dict],  # (t_assoluto) -> {"carico_kw":..,"produzione_fv_kw":..,"prezzo_acquisto":..,"prezzo_vendita":..}
    n_timestep_totale: int,
    durata_timestep_h: float,
    horizon_len: int = 24,
    p_rete_max_kw: float = 1e9,
    costo_potenza_impegnata_eur_kw: float = 0.0,
    punti_disponibili_per_tipo: Optional[dict] = None,  # vedi solve_dispatch — pass-through, nessuna logica qui
) -> MultiDayResult:
    """Esegue il ciclo di ri-ottimizzazione a orizzonte scorrevole sull'intero orizzonte totale.

    forecast_provider(t0, win) deve restituire la previsione (eventualmente rumorosa/imperfetta)
    per la finestra [t0, t0+win). actual_provider(t) restituisce i valori REALIZZATI al passo t,
    usati per calcolare il costo effettivo — se forecast e actual differiscono, il costo
    effettivo si discosta da quello pianificato, ma il ciclo si auto-corregge al passo successivo.
    """
    soc_correnti = {s.id: s.soc_iniziale_pct for s in schedules}

    soc_traiettoria = {s.id: [] for s in schedules}
    prelievo_reale = []
    immissione_reale = []
    violazioni = []

    costo_energia_tot = 0.0
    costo_potenza_tot = 0.0  # ricalcolato a fine ciclo sul picco reale complessivo
    costo_degrado_tot = 0.0
    ricavo_vendita_tot = 0.0
    picco_reale = 0.0

    for t0 in range(n_timestep_totale):
        win = _local_window(t0, horizon_len, n_timestep_totale)
        if win <= 0:
            break

        horizon = DispatchHorizon(n_timestep=win, durata_timestep_h=durata_timestep_h)
        vehicles_locali = [_build_local_vehicle(s, t0, win, soc_correnti[s.id]) for s in schedules]
        forecast = forecast_provider(t0, win)

        result = solve_dispatch(horizon, vehicles_locali, chargers, forecast, punti_disponibili_per_tipo=punti_disponibili_per_tipo)
        if not result.successo:
            violazioni.append(f"Passo {t0}: ottimizzazione fallita ({result.messaggio})")
            # fallback prudenziale: nessuna azione in questo passo
            for s in schedules:
                soc_traiettoria[s.id].append(soc_correnti[s.id])
            prelievo_reale.append(0.0)
            immissione_reale.append(0.0)
            continue

        # Applica SOLO il primo timestep del piano, con i valori REALIZZATI (non quelli previsti)
        reale = actual_provider(t0)
        carica_applicata = {}
        scarica_applicata = {}
        for piano in result.piani_veicolo:
            carica_applicata[piano.vehicle_id] = piano.carica_kw[0]
            scarica_applicata[piano.vehicle_id] = piano.scarica_kw[0]

        somma_carica = sum(carica_applicata.values())
        somma_scarica = sum(scarica_applicata.values())
        netto = reale["carico_kw"] - reale["produzione_fv_kw"] + somma_carica - somma_scarica
        prelievo_t = max(0.0, netto)
        immissione_t = max(0.0, -netto)

        prelievo_reale.append(prelievo_t)
        immissione_reale.append(immissione_t)
        picco_reale = max(picco_reale, prelievo_t)

        costo_energia_tot += reale["prezzo_acquisto"] * prelievo_t * durata_timestep_h
        ricavo_vendita_tot += reale["prezzo_vendita"] * immissione_t * durata_timestep_h

        # Aggiorna SoC di ciascun veicolo con la carica/scarica APPLICATA (l'azione di controllo
        # e' quella decisa, non dipende dall'errore di previsione su carico/FV — quello impatta
        # solo il bilancio di rete/costo, non la fisica della batteria)
        for s in schedules:
            v_local = next(v for v in vehicles_locali if v.id == s.id)
            c = carica_applicata.get(s.id, 0.0)
            d = scarica_applicata.get(s.id, 0.0)
            soc_correnti[s.id] += (
                (v_local.rendimento_carica * c - d / v_local.rendimento_scarica)
                * durata_timestep_h / v_local.capacita_kwh * 100.0
            )
            soc_correnti[s.id] = max(0.0, min(100.0, soc_correnti[s.id]))
            soc_traiettoria[s.id].append(soc_correnti[s.id])
            costo_degrado_tot += s.costo_degrado_eur_kwh * d * durata_timestep_h

            # verifica vincolo di partenza se questo e' esattamente un timestep di partenza
            for (t_assoluto, soc_richiesto) in s.partenze:
                if t_assoluto == t0:
                    if soc_correnti[s.id] < soc_richiesto - 1e-3:
                        violazioni.append(
                            f"Veicolo '{s.id}': SoC alla partenza (t={t0}) = "
                            f"{soc_correnti[s.id]:.1f}% < richiesto {soc_richiesto:.1f}%"
                        )

    costo_potenza_tot = costo_potenza_impegnata_eur_kw * picco_reale
    costo_totale = costo_energia_tot + costo_potenza_tot + costo_degrado_tot - ricavo_vendita_tot

    return MultiDayResult(
        n_timestep=n_timestep_totale,
        costo_totale_reale_eur=costo_totale,
        costo_energia_reale_eur=costo_energia_tot,
        costo_potenza_impegnata_reale_eur=costo_potenza_tot,
        costo_degrado_reale_eur=costo_degrado_tot,
        ricavo_vendita_reale_eur=ricavo_vendita_tot,
        picco_reale_kw=picco_reale,
        soc_traiettoria=soc_traiettoria,
        prelievo_rete_kw=prelievo_reale,
        immissione_rete_kw=immissione_reale,
        vincoli_partenza_rispettati=(len(violazioni) == 0),
        dettaglio_violazioni=violazioni,
    )


def run_baseline_dumb_charging(
    schedules: list,
    chargers: dict,
    actual_provider: Callable[[int], dict],
    n_timestep_totale: int,
    durata_timestep_h: float,
    costo_potenza_impegnata_eur_kw: float = 0.0,
    punti_disponibili_per_tipo: Optional[dict] = None,
) -> MultiDayResult:
    """Policy di confronto: carica al massimo appena il veicolo e' disponibile e sotto soc_max,
    non scarica MAI. Rappresenta lo status quo "senza motore di dispacciamento intelligente" —
    serve a quantificare il valore aggiunto del rolling MPC per il modulo business_model.

    punti_disponibili_per_tipo: stesso vincolo di pool di solve_dispatch, applicato qui in
    modo greedy (primo arrivato-primo servito nell'ordine di schedules) — coerente con la
    natura intenzionalmente "ingenua" di questo baseline. Senza, il confronto con l'MPC
    (che rispetta il pool) sarebbe sbilanciato: il baseline assumerebbe implicitamente un
    punto dedicato per veicolo sempre disponibile, il che non e' un confronto onesto.
    """
    soc_correnti = {s.id: s.soc_iniziale_pct for s in schedules}
    soc_traiettoria = {s.id: [] for s in schedules}
    prelievo_reale = []
    immissione_reale = []
    violazioni = []
    costo_energia_tot = 0.0
    ricavo_vendita_tot = 0.0
    picco_reale = 0.0

    for t0 in range(n_timestep_totale):
        reale = actual_provider(t0)
        somma_carica = 0.0
        punti_occupati_per_tipo = {}
        for s in schedules:
            disponibile = bool(s.disponibile_assoluto[t0]) if t0 < len(s.disponibile_assoluto) else False
            charger = chargers.get(s.id)
            p_max = float(charger.potenza_kw) if charger else 0.0
            tipo = getattr(charger, "tipo", "generico") if charger else "generico"
            if punti_disponibili_per_tipo is not None:
                n_punti = punti_disponibili_per_tipo.get(tipo)
                if n_punti is not None:
                    occupati = punti_occupati_per_tipo.get(tipo, 0)
                    if occupati >= n_punti:
                        disponibile = False  # pool esaurito per questo tipo in questo istante
                    else:
                        punti_occupati_per_tipo[tipo] = occupati + 1
            headroom_pct = max(0.0, s.soc_max_pct - soc_correnti[s.id])
            headroom_kwh = headroom_pct / 100.0 * s.capacita_kwh
            p_possibile = headroom_kwh / (s.rendimento_carica * durata_timestep_h) if durata_timestep_h > 0 else 0.0
            c = min(p_max, p_possibile) if disponibile else 0.0
            soc_correnti[s.id] += (s.rendimento_carica * c) * durata_timestep_h / s.capacita_kwh * 100.0
            soc_correnti[s.id] = max(0.0, min(100.0, soc_correnti[s.id]))
            soc_traiettoria[s.id].append(soc_correnti[s.id])
            somma_carica += c

            for (t_assoluto, soc_richiesto) in s.partenze:
                if t_assoluto == t0 and soc_correnti[s.id] < soc_richiesto - 1e-3:
                    violazioni.append(
                        f"[baseline] Veicolo '{s.id}': SoC alla partenza (t={t0}) = "
                        f"{soc_correnti[s.id]:.1f}% < richiesto {soc_richiesto:.1f}%"
                    )

        netto = reale["carico_kw"] - reale["produzione_fv_kw"] + somma_carica
        prelievo_t = max(0.0, netto)
        immissione_t = max(0.0, -netto)
        prelievo_reale.append(prelievo_t)
        immissione_reale.append(immissione_t)
        picco_reale = max(picco_reale, prelievo_t)
        costo_energia_tot += reale["prezzo_acquisto"] * prelievo_t * durata_timestep_h
        ricavo_vendita_tot += reale["prezzo_vendita"] * immissione_t * durata_timestep_h

    costo_potenza_tot = costo_potenza_impegnata_eur_kw * picco_reale
    costo_totale = costo_energia_tot + costo_potenza_tot - ricavo_vendita_tot

    return MultiDayResult(
        n_timestep=n_timestep_totale,
        costo_totale_reale_eur=costo_totale,
        costo_energia_reale_eur=costo_energia_tot,
        costo_potenza_impegnata_reale_eur=costo_potenza_tot,
        costo_degrado_reale_eur=0.0,
        ricavo_vendita_reale_eur=ricavo_vendita_tot,
        picco_reale_kw=picco_reale,
        soc_traiettoria=soc_traiettoria,
        prelievo_rete_kw=prelievo_reale,
        immissione_rete_kw=immissione_reale,
        vincoli_partenza_rispettati=(len(violazioni) == 0),
        dettaglio_violazioni=violazioni,
    )
