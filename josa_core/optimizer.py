"""
Motore decisionale: vincoli hardware, scoring multi-KPI, ricerca configurazioni.

Nel main.py originale queste funzioni (_capex, _score, _final_rank, la beam
search stessa, ...) erano closure definite DENTRO al blocco
`if run_optimization:`, quindi catturavano una dozzina di variabili
Streamlit (hw_db, budget_max, p_rete, fleet_nv, h_limit, policy_mode, v_data,
...) senza dichiararle. Qui diventano parametri espliciti di un
OptimizerContext, e le funzioni sono top-level: nessun cambio di
comportamento, solo di collocazione.

Nota architetturale: la ricerca (beam search) NON richiama direttamente
simulazione_soc/simulazione. Riceve una funzione `run_sim(cfg, is_stress)`
iniettata dal chiamante (main.py o un futuro layer API), cosi' l'optimizer
non deve conoscere quale motore di simulazione o quali parametri operativi
(finestre, SOC, stress test...) sta usando il chiamante. Nel monolite
originale questo accoppiamento era implicito (closure su _run_sim); qui e'
esplicito e iniettato.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd


@dataclass
class OptimizerContext:
    """Tutto cio' che serve al motore decisionale, dichiarato esplicitamente.

    hw_db: dizionario hardware {nome_tipo: {"p":..., "acq":..., "ins":..., "mnt":...}}.
    budget_max: budget CAPEX massimo (EUR).
    p_rete / p_shaving: potenza di rete disponibile e limite di peak shaving (kW).
    allow_oversizing: se True, la potenza installata puo' eccedere p_rete.
    dc_fixed_power / dc_redundancy: comportamento DC "a spunto" e ridondanza ammessa.
    fleet_nv: numero veicoli della flotta (limite fisico: 1 veicolo = 1 presa alla volta).
    h_limit / h_plug: fine turno e orario di plug-in serale (ore, come nell'UI originale).
    policy_mode: "asap" oppure "plug" — politica di connessione notturna.
    v_data: dati flotta (DataFrame o lista di dict) usati per stimare la domanda energetica
        quando il KPI 'e_need' non e' ancora disponibile in un risultato di simulazione.
    hybrid_private_home_charging: se True, il buffer aziendale (non il pieno SOC) e' il
        target della ricarica in sede.
    """

    hw_db: dict
    budget_max: float
    p_rete: float
    p_shaving: float
    fleet_nv: float
    h_limit: float
    h_plug: float
    policy_mode: str = "asap"
    allow_oversizing: bool = False
    dc_fixed_power: bool = True
    dc_redundancy: int = 2
    v_data: object = None  # DataFrame-like con colonne N_veicoli/Km_per_giro/Giri_per_veicolo_giorno/Consumo_kWh_km
    hybrid_private_home_charging: bool = True


def capex(ctx: OptimizerContext, cfg: dict) -> float:
    return float(sum((ctx.hw_db[t]["acq"] + ctx.hw_db[t]["ins"]) * int(q) for t, q in (cfg or {}).items() if int(q) > 0))


def num_chargers(cfg: dict) -> int:
    """Numero fisico di punti/prese installate.

    Vincolo operativo: ogni veicolo puo' essere collegato al massimo a un punto
    di ricarica alla volta, quindi l'ottimizzatore non deve mai proporre piu'
    punti di ricarica dei veicoli effettivamente dimensionati.
    """
    return int(sum(int(q) for q in (cfg or {}).values() if int(q) > 0))


def installed_power_kw(ctx: OptimizerContext, cfg: dict) -> float:
    return float(sum(float(ctx.hw_db.get(t, {}).get("p", 0.0)) * int(q) for t, q in (cfg or {}).items() if int(q) > 0))


def vehicle_port_limit_ok(ctx: OptimizerContext, cfg: dict) -> bool:
    try:
        n_veh = int(max(0, round(float(ctx.fleet_nv))))
    except Exception:
        n_veh = 0
    if n_veh <= 0:
        return True
    return num_chargers(cfg) <= n_veh


def installed_power_limit_ok(ctx: OptimizerContext, cfg: dict) -> bool:
    if bool(ctx.allow_oversizing):
        return True
    return installed_power_kw(ctx, cfg) <= min(float(ctx.p_rete), float(ctx.p_shaving)) + 1e-9


def dc_limits_ok(ctx: OptimizerContext, cfg: dict) -> bool:
    """Limite pratico quando DC e' a spunto (non modulabile).

    Se una DC eroga sempre alla potenza nominale quando e' attiva, con Peak
    Shaving cap il numero di DC identiche che ha senso installare e' limitato
    (oltre aumenta CAPEX senza aumentare energia interna).
    """
    if not ctx.dc_fixed_power:
        return True
    for t, q in (cfg or {}).items():
        if int(q) <= 0:
            continue
        if "DC" not in str(t):
            continue
        p = float(ctx.hw_db.get(t, {}).get("p", 0.0))
        if p <= 0:
            continue
        if float(ctx.p_shaving) + 1e-9 < p:
            return False
        max_parallel = int(float(ctx.p_shaving) // p)
        max_install = int(max_parallel) * int(ctx.dc_redundancy)
        if int(q) > int(max_install):
            return False
    return True


def hard_constraints_ok(ctx: OptimizerContext, cfg: dict) -> bool:
    return bool(
        vehicle_port_limit_ok(ctx, cfg)
        and installed_power_limit_ok(ctx, cfg)
        and dc_limits_ok(ctx, cfg)
    )


def fleet_time_pressure(ctx: OptimizerContext, cfg: dict, res: Optional[dict] = None) -> dict:
    """Misura data-driven del rischio tempo per configurazioni AC-heavy.

    Tie-break operativo: evita che soluzioni solo-AC vengano preferite quando
    caricano la stessa energia ma con tempi di recupero troppo tirati.
    """
    cfg = cfg or {}
    ac_points = int(sum(int(q) for t, q in cfg.items() if "AC" in str(t)))
    dc_points = int(sum(int(q) for t, q in cfg.items() if "DC" in str(t)))
    dc_power = float(sum(float(ctx.hw_db.get(t, {}).get("p", 0.0)) * int(q) for t, q in cfg.items() if "DC" in str(t)))
    ac_kw_eff = 11.0

    try:
        if str(ctx.policy_mode) == "plug":
            window_h = max(0.5, 24.0 - float(ctx.h_plug))
        else:
            window_h = max(0.5, 24.0 - float(ctx.h_limit))
    except Exception:
        window_h = 8.0

    kpi = ((res or {}).get("kpi", {}) if isinstance(res, dict) else {})
    e_need = float(kpi.get("e_need", 0.0) or 0.0)
    v_data = ctx.v_data
    if e_need <= 0.0 and v_data is not None:
        try:
            e_need = float((pd.to_numeric(v_data.get("N_veicoli"), errors="coerce").fillna(0.0) *
                            pd.to_numeric(v_data.get("Km_per_giro"), errors="coerce").fillna(0.0) *
                            pd.to_numeric(v_data.get("Giri_per_veicolo_giorno"), errors="coerce").fillna(0.0) *
                            pd.to_numeric(v_data.get("Consumo_kWh_km"), errors="coerce").fillna(0.22)).sum())
        except Exception:
            e_need = 0.0

    max_vehicle_kwh = 0.0
    if v_data is not None:
        try:
            max_vehicle_kwh = float((pd.to_numeric(v_data.get("Km_per_giro"), errors="coerce").fillna(0.0) *
                                     pd.to_numeric(v_data.get("Giri_per_veicolo_giorno"), errors="coerce").fillna(0.0) *
                                     pd.to_numeric(v_data.get("Consumo_kWh_km"), errors="coerce").fillna(0.22)).max())
        except Exception:
            max_vehicle_kwh = 0.0

    ac_required_h_per_point = (e_need / max(1, ac_points) / ac_kw_eff) if ac_points > 0 else 999.0
    ac_pressure = ac_required_h_per_point / max(0.5, window_h)
    critical_vehicle_ac_h = max_vehicle_kwh / ac_kw_eff if ac_kw_eff > 0 else 0.0
    ratio_auto_ac = float(ctx.fleet_nv) / max(1, ac_points) if ac_points > 0 else 999.0

    long_mission = bool(max_vehicle_kwh >= 45.0 or critical_vehicle_ac_h >= 4.0 or ac_required_h_per_point >= 8.0)
    ac_shared = bool(ratio_auto_ac > 1.15)
    no_fast_buffer = bool(dc_power <= 0.0)
    time_pressure_high = bool(ac_pressure >= 0.65 or ac_required_h_per_point >= 8.0)
    ac_time_risk_flag = bool(no_fast_buffer and ac_shared and (long_mission or time_pressure_high))
    ac_only_time_gate = bool(ac_time_risk_flag)

    return {
        "ac_points": ac_points,
        "dc_points": dc_points,
        "dc_power_kw": dc_power,
        "window_h": float(window_h),
        "e_need_kwh_g": float(e_need),
        "max_vehicle_kwh_g": float(max_vehicle_kwh),
        "critical_vehicle_ac_h": float(critical_vehicle_ac_h),
        "ratio_auto_ac": float(ratio_auto_ac),
        "ac_required_h_per_point": float(ac_required_h_per_point),
        "ac_pressure": float(ac_pressure),
        "ac_time_risk_flag": ac_time_risk_flag,
        "ac_only_time_gate": ac_only_time_gate,
        "dc_buffer_kw": float(dc_power),
    }


def has_dc(cfg: dict) -> bool:
    return any(("DC" in str(t)) and int(q) > 0 for t, q in (cfg or {}).items())


def score(ctx: OptimizerContext, res: dict) -> tuple:
    """Score lessicografico: priorita' a servizio operativo reale (tutti caricano
    entro l'uscita), poi COSTO, poi attesa/coda solo come spareggio finale.

    Cambiato apposta (richiesta esplicita): l'attesa a una colonnina NON e' un
    costo reale se il veicolo resta comunque in sede fino a fine giornata (es.
    profilo Office) — quello che conta e' che carichi PRIMA di uscire (gia'
    garantito da veh_unserved/buffer_gap/coverage), non quanto aspetta nel
    mentre. Prima l'attesa pesava piu' del costo, spingendo verso configurazioni
    piu' care anche quando una piu' economica raggiungeva comunque il 100% entro
    l'orario di uscita. Ora, tra configurazioni che coprono tutte il 100%, vince
    la piu' economica — l'attesa decide solo a parita' di costo."""
    k = (res or {}).get("kpi", {})
    ks = ((res or {}).get("stress") or {}).get("kpi", {})

    e_ext = float(k.get("e_ext", 0.0))
    e_ext_s = float(ks.get("e_ext", 0.0))
    buffer_gap = float(k.get("company_buffer_gap_kwh", 0.0))
    buffer_gap_s = float(ks.get("company_buffer_gap_kwh", 0.0))

    veh_unserved = int(k.get("veh_unserved", 0))
    veh_unserved_s = int(ks.get("veh_unserved", 0))
    capex_v = float(k.get("c_cap", 0.0))

    mnf_b = int(k.get("morning_not_full_days", 0))
    mshort_b = float(k.get("morning_shortfall_kwh", 0.0))
    mnf_s = int(ks.get("morning_not_full_days", 0))
    mshort_s = float(ks.get("morning_shortfall_kwh", 0.0))
    wait_b = float(k.get("wait_p95_min", k.get("wait_avg_min", 0.0)))
    wait_s = float(ks.get("wait_p95_min", ks.get("wait_avg_min", 0.0)))
    qmax_b = float(k.get("queue_max", 0.0))
    qmax_s = float(ks.get("queue_max", 0.0))
    perc = float(k.get("perc", 0.0))
    perc_s = float(ks.get("perc", perc))

    tp = fleet_time_pressure(ctx, (res or {}).get("config", {}), res)
    ac_pressure = float(tp.get("ac_pressure", 0.0))
    ac_time_risk = 1 if bool(tp.get("ac_time_risk_flag", False)) else 0
    ac_only_time_gate = 1 if bool(tp.get("ac_only_time_gate", False)) else 0

    # Preferenza operativa esplicita: si parte sempre da AC, il DC si aggiunge
    # SOLO se serve davvero per raggiungere una copertura che l'AC da solo non
    # riesce a dare — non e' un capriccio del motore, e' la prassi di cantiere
    # (il DC richiede un allaccio e un'installazione piu' complessi). Contiamo
    # le unita' DC nella config e le mettiamo PRIMA del costo: a parita' di
    # copertura raggiunta, vince sempre la configurazione con meno DC, anche se
    # per puro prezzo unitario il DC fosse marginalmente piu' economico in un
    # caso limite.
    cfg = (res or {}).get("config", {}) or {}
    unita_dc = sum(int(q) for t, q in cfg.items() if "DC" in str(t) and "AC" not in str(t))

    # NOTA: ac_only_time_gate (come ac_pressure/ac_time_risk) e' calcolato da
    # fleet_time_pressure() con una finestra stimata "24h - orario di chiusura"
    # — un'approssimazione pensata per ricarica overnight, sbagliata per
    # profili come Office dove il veicolo resta parcheggiato quasi tutto il
    # giorno (finestra reale molto piu' ampia di quella stimata). Prima questo
    # flag stava PRIMA di tutto, bloccando configurazioni solo-AC genuinamente
    # migliori (piu' economiche, stessa copertura reale misurata) solo per una
    # stima teorica sbagliata. Spostato dopo il costo, insieme agli altri
    # proxy previsionali — le metriche VERE misurate (veh_unserved, buffer_gap,
    # perc) restano prioritarie e catturano il rischio reale correttamente.

    # Picco di potenza effettivo dalla timeline simulata
    _tl = (res or {}).get("timeline_p")
    timeline_p = list(_tl) if _tl is not None else []
    picco_kw_reale = float(max(timeline_p)) if timeline_p else 999.0

    # Utilizzo colonnine: sessioni reali / numero colonnine (vogliamo MAX)
    capacity = (res or {}).get("capacity") or {}
    sessioni_reali = int(capacity.get("sessions_total", 0))
    n_colonnine = max(1, sum(int(q) for q in cfg.values()))
    utilizzo_inv = -(sessioni_reali / n_colonnine)  # negativo = massimizza

    return (
        veh_unserved,
        veh_unserved_s,
        mnf_b,
        mshort_b,
        buffer_gap,
        buffer_gap_s,
        e_ext,
        e_ext_s,
        mnf_s,
        mshort_s,
        -perc,
        -perc_s,
        unita_dc,
        round(picco_kw_reale, 1),  # picco reale minimo — competitivo
        utilizzo_inv,              # utilizzo colonnine massimo
        capex_v,                   # costo dopo i criteri operativi
        ac_only_time_gate,
        ac_time_risk,
        round(max(0.0, ac_pressure), 3),
        wait_b,
        wait_s,
        qmax_b,
        qmax_s,
    )


def final_rank(ctx: OptimizerContext, res: dict) -> tuple:
    """Ranking finale — delega interamente a score(), che gia' include (in
    posizione corretta, dopo le metriche vere misurate) sia la preferenza
    AC-first sia il proxy previsionale ac_only_time_gate. Prima questa
    funzione applicava un SECONDO controllo duplicato qui, a un livello
    ancora piu' esterno di score() — bypassando qualunque riordino fatto li'
    dentro e bloccando configurazioni solo-AC genuinamente migliori (piu'
    economiche, stessa copertura reale) solo per una stima teorica della
    finestra di ricarica sbagliata per profili come Office."""
    return score(ctx, res)


def search_viable(ctx: OptimizerContext, res: dict) -> bool:
    """Nodo di ricerca valido (non richiede ancora il buffer aziendale completo,
    altrimenti la beam search si fermerebbe troppo presto)."""
    if not res:
        return False
    cfg = (res or {}).get("config", {})
    if not hard_constraints_ok(ctx, cfg):
        return False
    k = res.get("kpi", {})
    cap = float(k.get("c_cap", 0.0))
    return cap > 0 and cap <= ctx.budget_max


def feasible(ctx: OptimizerContext, res: dict) -> bool:
    """Configurazione finale ammissibile per output e ranking."""
    if not search_viable(ctx, res):
        return False
    k = res.get("kpi", {})
    if bool(ctx.hybrid_private_home_charging) and float(k.get("company_buffer_gap_kwh", 0.0)) > 1e-6:
        return False
    return True


def cfg_key(cfg: dict) -> tuple:
    cfg = {t: int(q) for t, q in (cfg or {}).items() if int(q) > 0}
    return tuple(sorted(cfg.items(), key=lambda x: x[0]))


@dataclass
class BeamSearchParams:
    hw_selection: list
    beam_size: int = 4
    patience: int = 8
    max_steps: int = 200


@dataclass
class BeamSearchResult:
    results: list = field(default_factory=list)
    search_results: list = field(default_factory=list)


def run_beam_search(
    ctx: OptimizerContext,
    run_sim: Callable[[dict, bool], Optional[dict]],
    params: BeamSearchParams,
    progress_cb: Optional[Callable[[float], None]] = None,
) -> BeamSearchResult:
    """Beam search incrementale sulla combinazione hardware.

    Algoritmo identico all'originale (blocco "Ottimizzazione budget-aware"),
    estratto dal main.py come funzione pura. `run_sim(cfg, is_stress)` e'
    iniettata dal chiamante: incapsula quale motore di simulazione (SOC o
    legacy) e quali parametri operativi (finestre, SOC target, stress test...)
    usare — l'optimizer non ha bisogno di conoscerli.

    progress_cb(x) opzionale: x in [0, 1], per collegare una progress bar UI
    senza che questo modulo dipenda da Streamlit.
    """
    out = BeamSearchResult()
    if not params.hw_selection:
        return out

    beam = [({}, None)]
    best = None
    best_score = None
    no_improve = 0
    seen_cfg = set()

    max_steps = max(1, int(params.max_steps))

    for step in range(1, max_steps + 1):
        if progress_cb:
            progress_cb(step / max_steps)

        expansions = []
        for cfg0, _ in beam:
            for t in params.hw_selection:
                cand = dict(cfg0)
                cand[t] = int(cand.get(t, 0)) + 1
                if capex(ctx, cand) > ctx.budget_max:
                    continue
                if not hard_constraints_ok(ctx, cand):
                    continue
                key = cfg_key(cand)
                if key in seen_cfg:
                    continue
                seen_cfg.add(key)

                res = run_sim(cand, False)
                if not search_viable(ctx, res):
                    continue
                res["stress"] = run_sim(cand, True)
                expansions.append(res)
                out.search_results.append(res)

        if not expansions:
            break

        expansions.sort(key=lambda r: score(ctx, r))
        step_best = expansions[0]
        step_score = score(ctx, step_best)
        if best is None or step_score < best_score:
            best = step_best
            best_score = step_score
            no_improve = 0
        else:
            no_improve += 1

        beam = [(dict(r.get("config", {})), r) for r in expansions[: params.beam_size]]

        for r in expansions[: params.beam_size]:
            if feasible(ctx, r):
                out.results.append(r)

        if no_improve >= params.patience and step > 10:
            break

    return out
