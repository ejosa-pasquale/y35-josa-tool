"""
Collegamento tra schemi API (app/schemas.py) e josa_core.

Nessuna logica di business qui: solo conversione — dict/Pydantic -> DataFrame/dataclass
attesi da josa_core, e viceversa per la risposta. Se il motore cambia forma
internamente, cambia solo questo file, non gli endpoint ne' gli schemi esposti.
"""

import math
import itertools

import pandas as pd

from josa_core import EngineConfig, FuelCosts, SocPolicy, optimizer
from josa_core import genera_timeline_soc_da_gruppi, simulazione_soc
from josa_core import compliance_dm2025
from josa_core import tco as tco_module
from josa_core.ems import VehicleAsset, ChargerAsset, DispatchHorizon, SiteForecast, solve_dispatch
from josa_core.ems.rolling_mpc import VehicleSchedule, run_rolling_mpc
from josa_core.smart_bridge import run_smart_allocation_for_config

from . import schemas
from datetime import date as _date


def _groups_to_df(gruppi: list[schemas.FleetGroup]) -> pd.DataFrame:
    rows = []
    for g in gruppi:
        # None -> NaN: il motore (fleet_events._yes_no) interpreta NaN come "usa la
        # policy globale", coerente con la semantica gia' presente in josa_core.
        ricarica_domestica_val = g.ricarica_domestica
        if ricarica_domestica_val is None:
            ricarica_domestica_val = float("nan")
        ricarica_notturna_azienda_val = g.ricarica_notturna_azienda
        if ricarica_notturna_azienda_val is None:
            ricarica_notturna_azienda_val = float("nan")
        rows.append({
            "Gruppo": g.gruppo,
            "Profilo": g.profilo,
            "N_veicoli": g.n_veicoli,
            "Km_per_giro": g.km_per_giro,
            "Giri_per_veicolo_giorno": g.giri_per_veicolo_giorno,
            "Giri_per_autonomia": g.giri_per_autonomia,
            "Consumo_kWh_km": g.consumo_kwh_km,
            "Batteria_kWh": g.batteria_kwh,
            "Tempo_disponibile_min": g.tempo_disponibile_min,
            "Finestra_inizio": g.finestra_inizio,
            "Finestra_fine": g.finestra_fine,
            "Contemporanei_max": g.contemporanei_max,
            "Quota_ricarica_deposito": g.quota_ricarica_deposito,
            "K_factor": g.k_factor,
            "Ricarica_domestica": ricarica_domestica_val,
            "Ricarica_notturna_azienda": ricarica_notturna_azienda_val,
            "Potenza_max_ricarica_ac_kW": g.potenza_max_ricarica_ac_kw,
            # quota_ricarica_domestica_pct e' "quanto copro a casa" (input naturale
            # per l'utente) — Buffer_azienda_pct nel motore e' il suo complemento
            # ("quanto DEVE arrivare dall'azienda"), un dettaglio interno che
            # l'utente non deve conoscere.
            "Buffer_azienda_pct": (100.0 - g.quota_ricarica_domestica_pct) if g.quota_ricarica_domestica_pct is not None else float("nan"),
            "Probabilita_utilizzo_pct": g.probabilita_utilizzo_pct if g.probabilita_utilizzo_pct is not None else float("nan"),
        })
    return pd.DataFrame(rows)


def _hw_catalog_to_db(catalogo: list[schemas.HardwareSpec]) -> dict:
    return {
        hw.nome: {
            "p": hw.potenza_kw,
            "acq": hw.costo_acquisto_eur,
            "ins": hw.costo_installazione_eur,
            "mnt": hw.costo_manutenzione_eur_anno,
        }
        for hw in catalogo
    }


def _build_engine_cfg(policy: schemas.EnginePolicy) -> EngineConfig:
    return EngineConfig(
        p_rete=policy.p_rete_kw,
        allow_oversizing=policy.allow_oversizing,
        dc_fixed_power=policy.dc_fixed_power,
        dc_redundancy=policy.dc_redundancy,
    )


def _build_fuel(fuel: schemas.FuelCostsIn) -> FuelCosts:
    return FuelCosts(km_l=fuel.diesel_km_l, e_l=fuel.diesel_eur_l, h_rate=fuel.staff_eur_h)


def _build_soc_policy(policy: schemas.EnginePolicy) -> SocPolicy:
    return SocPolicy(
        soc_start_pct=policy.soc_start_pct,
        soc_min_pct=policy.soc_min_pct,
        soc_max_pct=policy.soc_max_pct,
        soc_buffer_pct=policy.soc_buffer_pct,
    )


def _prepare_events(gruppi, policy: schemas.EnginePolicy):
    df = _groups_to_df(gruppi)
    soc_params = _build_soc_policy(policy).as_fractions()
    soc_params["hybrid_private_home_charging"] = policy.hybrid_private_home_charging
    soc_params["company_buffer_pct"] = policy.company_buffer_pct / 100.0
    events, vehicles_map, fleet_nv, fleet_km_day_total, cons_avg = genera_timeline_soc_da_gruppi(
        df, soc_params,
    )
    return events, vehicles_map, fleet_nv, fleet_km_day_total


def _build_gantt_veicoli(events: list, vehicles_map: dict, res: dict, orizzonte_h: float = 24.0) -> list:
    """Costruisce, per OGNI veicolo della flotta (nessun campionamento), i segmenti
    sull'orizzonte dato (24h per un giorno tipo, 168h per una settimana): lavoro
    (guida, orari esatti dagli eventi), carica_azienda (sessione precisa al punto,
    orari esatti dalle sessioni), finestra_domestica (disponibilita' di ricarica a
    casa — ripetuta OGNI notte dell'orizzonte se il veicolo vi ha accesso, non solo
    nei giorni lavorativi: si torna a casa comunque anche nei weekend), sosta
    (fermo, non fa nulla). Usato per il Gantt visuale nel frontend.
    """
    drive_by_vid: dict = {}
    for e in events:
        if e.get("type") == "drive":
            vid = e.get("vid")
            drive_by_vid.setdefault(vid, []).append((float(e["s"]), float(e["f"])))

    sessions_by_vid: dict = {}
    for s in res.get("sessions", []) or []:
        vid = s.get("vid")
        lp = s.get("log_p") or {}
        i, ec = lp.get("i"), lp.get("ec")
        if vid and i is not None and ec is not None:
            sessions_by_vid.setdefault(vid, []).append((float(i), float(ec)))

    n_giorni = max(1, int(round(orizzonte_h / 24.0)))
    gantt = []
    for vid, v in vehicles_map.items():
        can_home = bool(v.get("can_home_night"))
        eventi = []
        for (s, f) in drive_by_vid.get(vid, []):
            eventi.append((s, f, "lavoro"))
        for (s, f) in sessions_by_vid.get(vid, []):
            eventi.append((s, f, "carica_azienda"))
        if can_home:
            for day in range(n_giorni):
                base = day * 24.0
                eventi.append((base + 0.0, base + 8.0, "finestra_domestica"))
                eventi.append((base + 19.0, base + 24.0, "finestra_domestica"))
        eventi.sort(key=lambda x: x[0])

        segmenti = []
        cursore = 0.0
        for (s, f, stato) in eventi:
            s = max(0.0, min(orizzonte_h, s))
            f = max(0.0, min(orizzonte_h, f))
            if f <= cursore:
                continue
            s_eff = max(s, cursore)
            if s_eff > cursore:
                segmenti.append({"inizio": cursore, "fine": s_eff, "stato": "sosta"})
            segmenti.append({"inizio": s_eff, "fine": f, "stato": stato})
            cursore = max(cursore, f)
        if cursore < orizzonte_h:
            segmenti.append({"inizio": cursore, "fine": orizzonte_h, "stato": "sosta"})

        gantt.append({
            "vehicle_id": vid,
            "gruppo": v.get("group"),
            "ricarica_domestica": can_home,
            "segmenti": segmenti,
        })
    return gantt


def _build_gantt_settimanale(req: "schemas.SimulateRequest", config_vincente: dict) -> list:
    """Costruisce il Gantt su un'intera settimana (Lun-Ven lavorativi, weekend fermo)
    SENZA toccare il dimensionamento: rilancia la simulazione giorno per giorno con
    la STESSA configurazione hardware gia' decisa (config_vincente), un seed diverso
    per ciascun giorno lavorativo (variazione realistica orario per orario, non lo
    stesso giorno fotocopiato 5 volte), poi unisce eventi e sessioni con l'offset del
    giorno giusto. Riusa per intero la logica di simulazione a giorno singolo, gia'
    testata — nessuna modifica al motore di dimensionamento (CAPEX) stesso.
    """
    df = _groups_to_df(req.gruppi)
    soc_params = _build_soc_policy(req.policy).as_fractions()
    soc_params["hybrid_private_home_charging"] = req.policy.hybrid_private_home_charging
    soc_params["company_buffer_pct"] = req.policy.company_buffer_pct / 100.0

    hw_db = _hw_catalog_to_db(req.catalogo_hardware)
    engine_cfg = _build_engine_cfg(req.policy)
    fuel = _build_fuel(req.fuel)
    soc_policy = _build_soc_policy(req.policy)
    costi = {"pri": req.energia.prezzo_privato_eur_kwh, "pub": req.energia.prezzo_pubblico_eur_kwh}

    tutti_eventi = []
    tutte_sessioni = []
    vehicles_map_canonico = None
    semi_lun_ven = [101, 102, 103, 104, 105]  # un seed diverso per ciascun giorno lavorativo

    for day_idx, seed in enumerate(semi_lun_ven):
        events_day, vehicles_map_day, _nv, km_day_tot, _cons = genera_timeline_soc_da_gruppi(
            df, soc_params, seed=seed,
            hybrid_private_home_charging=req.policy.hybrid_private_home_charging,
            company_buffer_pct=req.policy.company_buffer_pct / 100.0,
        )
        if vehicles_map_canonico is None:
            vehicles_map_canonico = vehicles_map_day

        res_day = simulazione_soc(
            config_vincente, events_day, vehicles_map_day, costi, hw_db,
            p_shave_limit=req.policy.p_shaving_kw,
            max_ac_v=req.policy.max_ac_veicoli_per_punto,
            max_dc_v=req.policy.max_dc_veicoli_per_punto,
            limit_h=req.policy.limite_ora_turno,
            engine_cfg=engine_cfg, fuel=fuel, soc_policy=soc_policy,
            fleet_km_day_total=km_day_tot, sim_days=1,
            hybrid_private_home_charging=req.policy.hybrid_private_home_charging,
            company_buffer_pct=req.policy.company_buffer_pct / 100.0,
        )
        if res_day is None:
            continue  # giorno non simulabile con questa config: lo salta, non blocca l'intera settimana

        offset = day_idx * 24.0
        for e in events_day:
            if e.get("type") == "drive":
                tutti_eventi.append({**e, "s": float(e["s"]) + offset, "f": float(e["f"]) + offset})
        for s in res_day.get("sessions", []) or []:
            lp = s.get("log_p") or {}
            if lp.get("i") is not None and lp.get("ec") is not None:
                tutte_sessioni.append({**s, "log_p": {"i": float(lp["i"]) + offset, "ec": float(lp["ec"]) + offset}})

    if vehicles_map_canonico is None:
        return []
    return _build_gantt_veicoli(tutti_eventi, vehicles_map_canonico, {"sessions": tutte_sessioni}, orizzonte_h=168.0)


def run_simulate(req: "schemas.SimulateRequest") -> dict:
    hw_db = _hw_catalog_to_db(req.catalogo_hardware)
    events, vehicles_map, fleet_nv, fleet_km_day_total = _prepare_events(req.gruppi, req.policy)

    engine_cfg = _build_engine_cfg(req.policy)
    fuel = _build_fuel(req.fuel)
    soc_policy = _build_soc_policy(req.policy)
    costi = {"pri": req.energia.prezzo_privato_eur_kwh, "pub": req.energia.prezzo_pubblico_eur_kwh}

    res = simulazione_soc(
        req.configurazione.quantita,
        events, vehicles_map, costi, hw_db,
        p_shave_limit=req.policy.p_shaving_kw,
        max_ac_v=req.policy.max_ac_veicoli_per_punto,
        max_dc_v=req.policy.max_dc_veicoli_per_punto,
        limit_h=req.policy.limite_ora_turno,
        engine_cfg=engine_cfg, fuel=fuel, soc_policy=soc_policy,
        fleet_km_day_total=fleet_km_day_total,
        is_stress=req.stress_test,
        extra_c=req.stress_extra_consumo_pct,
        delay_m=req.stress_ritardo_min,
        sim_days=req.policy.sim_days,
        hybrid_private_home_charging=req.policy.hybrid_private_home_charging,
        company_buffer_pct=req.policy.company_buffer_pct / 100.0,
    )
    if res is None:
        raise ValueError(
            "Configurazione non simulabile: verifica potenza installata vs p_rete/p_shaving "
            "(vedi policy.allow_oversizing) e vincoli DC."
        )
    k = res["kpi"]
    if getattr(req, "gantt_settimanale", False):
        gantt_veicoli = _build_gantt_settimanale(req, req.configurazione.quantita)
        gantt_orizzonte_h = 168.0
    else:
        gantt_veicoli = _build_gantt_veicoli(events, vehicles_map, res, orizzonte_h=24.0)
        gantt_orizzonte_h = 24.0
    return {
        "config": res["config"],
        "kpi": k,
        "veicoli_totali": int(k.get("veh_total", 0)),
        "veicoli_serviti": int(k.get("veh_served", 0)),
        "copertura_pct": float(k.get("copertura_reale_pct", k.get("perc", 0.0))),
        "capex_eur": float(k.get("c_cap", 0.0)),
        "timeline_p_kw": [float(x) for x in (res.get("timeline_p") if res.get("timeline_p") is not None else [])],
        "timeline_q": [float(x) for x in (res.get("timeline_q") if res.get("timeline_q") is not None else [])],
        "gantt_veicoli": gantt_veicoli,
        "gantt_orizzonte_h": gantt_orizzonte_h,
    }


def run_optimize(req: "schemas.OptimizeRequest") -> dict:
    hw_db = _hw_catalog_to_db(req.catalogo_hardware)
    events, vehicles_map, fleet_nv, fleet_km_day_total = _prepare_events(req.gruppi, req.policy)

    engine_cfg = _build_engine_cfg(req.policy)
    fuel = _build_fuel(req.fuel)
    soc_policy = _build_soc_policy(req.policy)
    costi = {"pri": req.energia.prezzo_privato_eur_kwh, "pub": req.energia.prezzo_pubblico_eur_kwh}

    def run_sim(cfg, is_stress):
        return simulazione_soc(
            cfg, events, vehicles_map, costi, hw_db,
            p_shave_limit=req.policy.p_shaving_kw,
            max_ac_v=req.policy.max_ac_veicoli_per_punto,
            max_dc_v=req.policy.max_dc_veicoli_per_punto,
            limit_h=req.policy.limite_ora_turno,
            engine_cfg=engine_cfg, fuel=fuel, soc_policy=soc_policy,
            fleet_km_day_total=fleet_km_day_total,
            is_stress=is_stress,
            sim_days=req.policy.sim_days,
            hybrid_private_home_charging=req.policy.hybrid_private_home_charging,
            company_buffer_pct=req.policy.company_buffer_pct / 100.0,
        )

    ctx = optimizer.OptimizerContext(
        hw_db=hw_db,
        budget_max=req.budget_max_eur,
        p_rete=req.policy.p_rete_kw,
        p_shaving=req.policy.p_shaving_kw,
        fleet_nv=fleet_nv,
        h_limit=req.policy.limite_ora_turno,
        h_plug=req.policy.limite_ora_turno,
        policy_mode="asap",
        allow_oversizing=req.policy.allow_oversizing,
        dc_fixed_power=req.policy.dc_fixed_power,
        dc_redundancy=req.policy.dc_redundancy,
        v_data=_groups_to_df(req.gruppi),
        hybrid_private_home_charging=req.policy.hybrid_private_home_charging,
    )
    params = optimizer.BeamSearchParams(
        hw_selection=req.tipi_hardware_da_esplorare,
        beam_size=req.beam_size,
        patience=req.patience,
        max_steps=req.max_steps,
    )
    out = optimizer.run_beam_search(ctx, run_sim, params)

    ranked = sorted(out.results, key=lambda r: optimizer.final_rank(ctx, r))
    soluzioni = [
        {
            "config": r.get("config", {}),
            "kpi": r.get("kpi", {}),
            "capex_eur": float(r.get("kpi", {}).get("c_cap", 0.0)),
            "copertura_pct": float(r.get("kpi", {}).get("copertura_reale_pct", r.get("kpi", {}).get("perc", 0.0))),
            "ammissibile": True,
            "gap_kwh_da_coprire": None,
        }
        for r in ranked
    ]

    # BUG CORRETTO (stesso identificato in run_scenario_compare, non ancora
    # propagato qui): se nessuna configurazione raggiunge il 100% del fabbisogno
    # aziendale entro budget, prima si restituiva una lista vuota — l'utente
    # vedeva solo "alza il budget", senza nessuna indicazione concreta. Ora, se
    # 'soluzioni' e' vuota ma il motore ha comunque esplorato configurazioni
    # entro budget (out.search_results), restituiamo le migliori 3 con
    # ammissibile=False e il gap kWh/giorno ancora scoperto — un'indicazione
    # azionabile, non solo "prova ad alzare il budget".
    if not soluzioni and out.search_results:
        migliori_parziali = sorted(out.search_results, key=lambda r: optimizer.score(ctx, r))[:3]
        soluzioni = [
            {
                "config": r.get("config", {}),
                "kpi": r.get("kpi", {}),
                "capex_eur": float(r.get("kpi", {}).get("c_cap", 0.0)),
                "copertura_pct": float(r.get("kpi", {}).get("copertura_reale_pct", r.get("kpi", {}).get("perc", 0.0))),
                "ammissibile": False,
                "gap_kwh_da_coprire": float(r.get("kpi", {}).get("company_buffer_gap_kwh", 0.0)),
            }
            for r in migliori_parziali
        ]

    return {
        "soluzioni": soluzioni,
        "nodi_esplorati": len(out.search_results),
        "ammissibili_trovate": len(out.results),
    }


def run_site_scoring(req: "schemas.SiteScoringRequest") -> dict:
    from josa_core.site_scoring import compute_site_score, CriterioInput, PESI_DEFAULT

    input_dati = CriterioInput(
        traffico_veicoli_giorno=req.traffico_veicoli_giorno,
        accesso_facile=req.accesso_facile,
        distanza_arteria_km=req.distanza_arteria_km,
        densita_abitanti_km2=req.densita_abitanti_km2,
        densita_aziende_km2=req.densita_aziende_km2,
        n_servizi_300m=req.n_servizi_300m,
        potenza_disponibile_kw=req.potenza_disponibile_kw,
        posti_parcheggio_disponibili=req.posti_parcheggio_disponibili,
        distanza_trasporto_pubblico_km=req.distanza_trasporto_pubblico_km,
        distanza_competitor_km=req.distanza_competitor_km,
        visibilita=req.visibilita,
    )
    pesi = req.pesi_personalizzati or PESI_DEFAULT
    result = compute_site_score(input_dati, pesi=pesi, e_deposito_aziendale=req.e_deposito_aziendale)

    return {
        "punteggio_totale_0_100": result.punteggio_totale_0_100,
        "grado": result.grado,
        "criteri": [
            {"nome": c.nome, "punteggio_0_100": c.punteggio_0_100, "peso": c.peso,
             "contributo_ponderato": round(c.contributo_ponderato, 1), "spiegazione": c.spiegazione}
            for c in result.criteri
        ],
        "e_deposito_aziendale": result.e_deposito_aziendale,
        "nota_contesto": result.nota_contesto,
    }


def run_compliance_check(req: "schemas.ComplianceDM2025Request") -> dict:
    data_rif = _date.fromisoformat(req.data_riferimento) if req.data_riferimento else None

    profile = compliance_dm2025.BuildingProfile(
        residenziale=req.residenziale,
        accesso_pubblico=req.accesso_pubblico,
        posti_auto=req.posti_auto,
        tipo_intervento=req.tipo_intervento,
        pmi_proprietaria_e_occupante=req.pmi_proprietaria_e_occupante,
        permesso_costruire_ante_2021_03_10=req.permesso_costruire_ante_2021_03_10,
        costo_ricarica_pct_su_ristrutturazione=req.costo_ricarica_pct_su_ristrutturazione,
        microsistema_isolato_critico=req.microsistema_isolato_critico,
        edificio_pubblico_gia_conforme_dlgs257=req.edificio_pubblico_gia_conforme_dlgs257,
    )
    result = compliance_dm2025.compute_dm2025(profile, data_riferimento=data_rif)

    confronto = None
    if req.configurazione_da_verificare and req.catalogo_hardware:
        hw_db = _hw_catalog_to_db(req.catalogo_hardware)
        confronto = compliance_dm2025.compare_with_hardware_config(
            result, req.configurazione_da_verificare.quantita, hw_db,
        )

    return {
        "esente": result.esente,
        "motivo_esenzione": result.motivo_esenzione,
        "canalizzazione_richiesta": result.canalizzazione_richiesta,
        "canalizzazione_quota_posti": result.canalizzazione_quota_posti,
        "punti_tipologia_a_minimi_a_regime": result.punti_tipologia_a_minimi_a_regime,
        "punti_tipologia_b_minimi_a_regime": result.punti_tipologia_b_minimi_a_regime,
        "punti_tipologia_a_applicabili_oggi": result.punti_tipologia_a_applicabili_oggi,
        "punti_tipologia_b_applicabili_oggi": result.punti_tipologia_b_applicabili_oggi,
        "fase_transitoria": result.fase_transitoria,
        "smart_charging_v1g_richiesto": result.smart_charging_v1g_richiesto,
        "registrazione_pun_richiesta": result.registrazione_pun_richiesta,
        "note_tecniche": result.note_tecniche,
        "fonti_da_verificare": result.fonti_da_verificare,
        "disclaimer": result.disclaimer,
        "confronto_con_configurazione": confronto,
    }


def run_v2g_dispatch(req: "schemas.V2GDispatchRequest") -> dict:
    n_timestep = len(req.carico_edificio_kw)
    horizon = DispatchHorizon(n_timestep=n_timestep, durata_timestep_h=req.durata_timestep_h)

    vehicles = []
    chargers = {}
    for v in req.veicoli:
        vehicles.append(VehicleAsset(
            id=v.id,
            capacita_kwh=v.capacita_kwh,
            soc_iniziale_pct=v.soc_iniziale_pct,
            soc_min_pct=v.soc_min_pct,
            soc_max_pct=v.soc_max_pct,
            rendimento_carica=v.rendimento_carica,
            rendimento_scarica=v.rendimento_scarica,
            timestep_partenza=v.timestep_partenza,
            soc_minimo_alla_partenza_pct=v.soc_minimo_alla_partenza_pct,
            disponibile=v.disponibile,
            priorita=v.priorita,
            probabilita_utilizzo=v.probabilita_utilizzo,
            costo_degrado_eur_kwh=v.costo_degrado_eur_kwh,
        ))
        chargers[v.id] = ChargerAsset(
            vehicle_id=v.id, potenza_kw=v.potenza_caricatore_kw, v2g_capace=v.v2g_capace,
            tipo=v.tipo_colonnina,
        )

    forecast = SiteForecast(
        carico_edificio_kw=req.carico_edificio_kw,
        produzione_fv_kw=req.produzione_fv_kw,
        prezzo_acquisto_eur_kwh=req.prezzo_acquisto_eur_kwh,
        prezzo_vendita_eur_kwh=req.prezzo_vendita_eur_kwh,
        p_rete_max_kw=req.p_rete_max_kw,
        costo_potenza_impegnata_eur_kw=req.costo_potenza_impegnata_eur_kw,
    )

    result = solve_dispatch(horizon, vehicles, chargers, forecast, punti_disponibili_per_tipo=req.punti_disponibili_per_tipo)

    if not result.successo:
        return {
            "successo": False,
            "messaggio": result.messaggio,
            "piani_veicolo": [],
            "prelievo_rete_kw": [],
            "immissione_rete_kw": [],
            "picco_kw": 0.0,
            "costo_totale_eur": 0.0,
            "costo_energia_eur": 0.0,
            "costo_potenza_impegnata_eur": 0.0,
            "costo_degrado_eur": 0.0,
            "ricavo_vendita_eur": 0.0,
        }

    return {
        "successo": True,
        "messaggio": result.messaggio,
        "piani_veicolo": [
            {"vehicle_id": p.vehicle_id, "carica_kw": p.carica_kw, "scarica_kw": p.scarica_kw, "soc_pct": p.soc_pct}
            for p in result.piani_veicolo
        ],
        "prelievo_rete_kw": result.prelievo_rete_kw,
        "immissione_rete_kw": result.immissione_rete_kw,
        "picco_kw": result.picco_kw,
        "costo_totale_eur": result.costo_totale_eur,
        "costo_energia_eur": result.costo_energia_eur,
        "costo_potenza_impegnata_eur": result.costo_potenza_impegnata_eur,
        "costo_degrado_eur": result.costo_degrado_eur,
        "ricavo_vendita_eur": result.ricavo_vendita_eur,
    }


def run_v2g_weekly_dispatch(req: "schemas.V2GWeeklyDispatchRequest") -> dict:
    """Dispacciamento V2G su un orizzonte multi-giorno (tipicamente una settimana
    lavorativa, 168 ore), con MPC a orizzonte scorrevole (run_rolling_mpc).

    Previsione = valore reale in questa esposizione (nessun rumore di previsione):
    l'obiettivo qui e' mostrare COME il motore si comporta durante la settimana in
    funzione dei turni, non dimostrare la robustezza a previsioni imperfette (quella
    e' gia' validata in test_rolling_mpc_and_business_model.py).
    """
    n_timestep = len(req.carico_edificio_kw)

    schedules = []
    chargers = {}
    for v in req.veicoli:
        schedules.append(VehicleSchedule(
            id=v.id,
            capacita_kwh=v.capacita_kwh,
            soc_iniziale_pct=v.soc_iniziale_pct,
            soc_min_pct=v.soc_min_pct,
            soc_max_pct=v.soc_max_pct,
            rendimento_carica=v.rendimento_carica,
            rendimento_scarica=v.rendimento_scarica,
            priorita=v.priorita,
            probabilita_utilizzo=v.probabilita_utilizzo,
            costo_degrado_eur_kwh=v.costo_degrado_eur_kwh,
            disponibile_assoluto=v.disponibile,
            partenze=[(p.timestep, p.soc_minimo_pct) for p in v.partenze],
        ))
        chargers[v.id] = ChargerAsset(
            vehicle_id=v.id, potenza_kw=v.potenza_caricatore_kw, v2g_capace=v.v2g_capace,
            tipo=v.tipo_colonnina,
        )

    def forecast_provider(t0: int, win: int) -> SiteForecast:
        return SiteForecast(
            carico_edificio_kw=req.carico_edificio_kw[t0:t0 + win],
            produzione_fv_kw=req.produzione_fv_kw[t0:t0 + win],
            prezzo_acquisto_eur_kwh=req.prezzo_acquisto_eur_kwh[t0:t0 + win],
            prezzo_vendita_eur_kwh=req.prezzo_vendita_eur_kwh[t0:t0 + win],
            p_rete_max_kw=req.p_rete_max_kw,
            costo_potenza_impegnata_eur_kw=req.costo_potenza_impegnata_eur_kw,
        )

    def actual_provider(t: int) -> dict:
        return {
            "carico_kw": req.carico_edificio_kw[t],
            "produzione_fv_kw": req.produzione_fv_kw[t],
            "prezzo_acquisto": req.prezzo_acquisto_eur_kwh[t],
            "prezzo_vendita": req.prezzo_vendita_eur_kwh[t],
        }

    result = run_rolling_mpc(
        schedules, chargers, forecast_provider, actual_provider,
        n_timestep_totale=n_timestep,
        durata_timestep_h=req.durata_timestep_h,
        horizon_len=req.orizzonte_lookahead_timestep,
        p_rete_max_kw=req.p_rete_max_kw,
        costo_potenza_impegnata_eur_kw=req.costo_potenza_impegnata_eur_kw,
        punti_disponibili_per_tipo=req.punti_disponibili_per_tipo,
    )

    # run_rolling_mpc non espone carica/scarica applicate per singolo intervallo (solo
    # la traiettoria SoC): le derivo qui dal delta di SoC tra intervalli consecutivi.
    # E' una stima per la sola visualizzazione (carica netta), non il valore esatto
    # applicato dal solver (che tiene conto separatamente di rendimento carica/scarica).
    piani = []
    for s in schedules:
        soc = result.soc_traiettoria.get(s.id, [])
        carica_stimata = [0.0] * len(soc)
        scarica_stimata = [0.0] * len(soc)
        prev = s.soc_iniziale_pct
        for i, val in enumerate(soc):
            delta_pct = val - prev
            delta_kwh = delta_pct / 100.0 * s.capacita_kwh
            if delta_kwh >= 0:
                carica_stimata[i] = delta_kwh / req.durata_timestep_h
            else:
                scarica_stimata[i] = -delta_kwh / req.durata_timestep_h
            prev = val
        piani.append({
            "vehicle_id": s.id,
            "soc_pct": soc,
            "carica_kw_stimata": carica_stimata,
            "scarica_kw_stimata": scarica_stimata,
        })

    return {
        "successo": True,
        "messaggio": "ok",
        "n_timestep": result.n_timestep,
        "piani_veicolo": piani,
        "prelievo_rete_kw": result.prelievo_rete_kw,
        "immissione_rete_kw": result.immissione_rete_kw,
        "picco_kw": result.picco_reale_kw,
        "costo_totale_eur": result.costo_totale_reale_eur,
        "costo_energia_eur": result.costo_energia_reale_eur,
        "costo_potenza_impegnata_eur": result.costo_potenza_impegnata_reale_eur,
        "costo_degrado_eur": result.costo_degrado_reale_eur,
        "ricavo_vendita_eur": result.ricavo_vendita_reale_eur,
        "vincoli_partenza_rispettati": result.vincoli_partenza_rispettati,
        "dettaglio_violazioni": result.dettaglio_violazioni,
    }


def _generate_benchmark_configs(hw_db: dict, fleet_nv: int) -> dict:
    """Genera le configurazioni benchmark standard 1:1, 1:2, 1:4 auto/punto,
    usando il primo tipo hardware AC nel catalogo come riferimento (stessa
    convenzione dell'analisi originale: i benchmark sono sempre in AC).
    Ritorna {label: cfg_dict}, vuoto se non c'e' nessun tipo AC nel catalogo.
    """
    ac_type = next((t for t in hw_db if "AC" in str(t).upper()), None)
    if ac_type is None or fleet_nv <= 0:
        return {}
    out = {}
    for ratio, label in [(1, "Benchmark 1:1"), (2, "Benchmark 1:2"), (4, "Benchmark 1:4")]:
        n_punti = max(1, math.ceil(fleet_nv / ratio))
        out[label] = {ac_type: n_punti}
    return out


def _tco_from_kpi(k: dict, fleet_km_day_total: float, req_fuel, req_energia, vehicle_costs_in, financial_in) -> dict:
    vc = tco_module.VehicleCostAssumptions(
        canone_diesel_mese_eur=vehicle_costs_in.canone_diesel_mese_eur,
        canone_ev_mese_eur=vehicle_costs_in.canone_ev_mese_eur,
        manutenzione_diesel_anno_eur=vehicle_costs_in.manutenzione_diesel_anno_eur,
        manutenzione_ev_anno_eur=vehicle_costs_in.manutenzione_ev_anno_eur,
        prezzo_acquisto_diesel_eur=vehicle_costs_in.prezzo_acquisto_diesel_eur,
        prezzo_acquisto_ev_eur=vehicle_costs_in.prezzo_acquisto_ev_eur,
        incentivo_ev_eur=vehicle_costs_in.incentivo_ev_eur,
        valore_residuo_diesel_pct_5y=vehicle_costs_in.valore_residuo_diesel_pct_5y,
        valore_residuo_ev_pct_5y=vehicle_costs_in.valore_residuo_ev_pct_5y,
        costo_gestione_diesel_annuo_eur=vehicle_costs_in.costo_gestione_diesel_annuo_eur,
        costo_restrizioni_diesel_annuo_eur=vehicle_costs_in.costo_restrizioni_diesel_annuo_eur,
    )
    fin = tco_module.FinancialAssumptions(
        orizzonte_anni=financial_in.orizzonte_anni,
        tasso_sconto=financial_in.tasso_sconto_pct / 100.0,
        modalita=financial_in.modalita,
    )
    result = tco_module.compute_tco_analysis(
        n_veicoli=int(k.get("veh_total", 0)),
        fleet_km_day_total=fleet_km_day_total,
        diesel_km_l=req_fuel.diesel_km_l,
        diesel_eur_l=req_fuel.diesel_eur_l,
        e_int_kwh_g=float(k.get("e_int", 0.0)),
        e_ext_kwh_g=float(k.get("e_ext", 0.0)),
        prezzo_energia_privato_eur_kwh=req_energia.prezzo_privato_eur_kwh,
        prezzo_energia_pubblico_eur_kwh=req_energia.prezzo_pubblico_eur_kwh,
        infra_capex_eur=float(k.get("c_cap", 0.0)),
        infra_om_annuo_eur=float(k.get("mnt", 0.0)),
        staff_ext_annuo_eur=float(k.get("staff_ext", 0.0)),
        vehicle_costs=vc, financial=fin,
        e_home_kwh_g=float(k.get("e_home_private", 0.0)),
        prezzo_energia_domestica_eur_kwh=req_energia.prezzo_domestico_eur_kwh,
    )
    return {
        "risparmio_operativo_annuo_eur": result.risparmio_operativo_annuo_eur,
        "capex_differenziale_t0_eur": result.capex_differenziale_t0_eur,
        "npv_eur": result.npv_eur,
        "payback_anni": result.payback_anni,
        "delta_tco_eur": result.delta_tco_eur,
        "roi_netto_pct": result.roi_netto_pct,
        "benefit_cost_ratio": result.benefit_cost_ratio,
        "diesel_costo_annuo_eur": result.diesel_costo_annuo_eur,
        "ev_costo_annuo_eur": result.ev_costo_annuo_eur,
        "reconciliation": [
            {"voce": r.voce, "diesel_eur": r.diesel_eur, "ev_eur": r.ev_eur, "delta_eur": r.delta_eur}
            for r in result.reconciliation
        ],
        "cashflow_cumulativo_scontato_eur": result.cashflow_cumulativo_scontato_eur,
        "_raw_result": result,  # uso interno per _breakeven_from_tco, rimosso prima della risposta API
    }


def _marginal_charger_analysis(cfg: dict, k_base: dict, run_sim, hw_db: dict, financial_in) -> list:
    """Per ciascun tipo hardware nel catalogo, valuta se aggiungere UNA unita' in
    piu' conviene rispetto a continuare a pagare il sovrapprezzo di ricarica
    pubblica per i veicoli non serviti internamente (pena_en + staff_ext).

    Risponde esplicitamente alla domanda che l'originale mostrava solo come
    singolo numero ("Costo Inefficienza") senza confrontarlo con l'alternativa:
    qui il confronto con il costo di UN punto in piu' e' calcolato ed esplicito.
    """
    costo_ineff_base = float(k_base.get("pena_en", 0.0)) + float(k_base.get("staff_ext", 0.0))
    tasso = financial_in.tasso_sconto_pct / 100.0
    orizzonte = financial_in.orizzonte_anni
    risultati = []

    for tipo in hw_db:
        cfg_plus = dict(cfg)
        cfg_plus[tipo] = int(cfg_plus.get(tipo, 0)) + 1
        try:
            res_plus = run_sim(cfg_plus, False)
        except Exception:
            res_plus = None
        if res_plus is None:
            continue
        k_plus = res_plus["kpi"]
        costo_ineff_dopo = float(k_plus.get("pena_en", 0.0)) + float(k_plus.get("staff_ext", 0.0))
        risparmio_annuo = costo_ineff_base - costo_ineff_dopo
        capex_incr = float(k_plus.get("c_cap", 0.0)) - float(k_base.get("c_cap", 0.0))

        npv_incr = -capex_incr + sum(risparmio_annuo / ((1 + tasso) ** y) for y in range(1, orizzonte + 1))
        payback = None
        if risparmio_annuo > 0:
            payback = math.ceil(capex_incr / risparmio_annuo) if capex_incr > 0 else 0

        risultati.append({
            "tipo": tipo,
            "capex_incrementale_eur": capex_incr,
            "costo_inefficienza_attuale_eur_anno": costo_ineff_base,
            "costo_inefficienza_dopo_eur_anno": costo_ineff_dopo,
            "risparmio_annuo_eur": risparmio_annuo,
            "npv_incrementale_eur": npv_incr,
            "payback_anni": payback,
            "conviene": bool(npv_incr > 0),
            "veicoli_serviti_prima": int(k_base.get("veh_served", 0)),
            "veicoli_serviti_dopo": int(k_plus.get("veh_served", 0)),
        })
    return risultati


def _breakeven_from_tco(raw_tco_result, k: dict, req_energia, financial_in) -> dict:
    e_int = float(k.get("e_int", 0.0))
    e_home = float(k.get("e_home_private", 0.0))
    e_tot = e_int + float(k.get("e_ext", 0.0)) + e_home
    if e_tot <= 0:
        e_tot = max(e_int, 1e-6)
    be = tco_module.compute_breakeven_analysis(
        raw_tco_result,
        e_int_kwh_g=e_int, e_tot_kwh_g=e_tot,
        prezzo_energia_privato_eur_kwh=req_energia.prezzo_privato_eur_kwh,
        prezzo_energia_pubblico_eur_kwh=req_energia.prezzo_pubblico_eur_kwh,
        orizzonte_anni=financial_in.orizzonte_anni,
    )
    return {
        "stato": be.stato,
        "quota_interna_attuale_pct": be.quota_interna_attuale_pct,
        "quota_interna_breakeven_pct": be.quota_interna_breakeven_pct,
        "quota_interna_breakeven_raggiungibile": be.quota_interna_breakeven_raggiungibile,
        "gap_quota_interna_pt": be.gap_quota_interna_pt,
        "energia_interna_richiesta_kwh_g": be.energia_interna_richiesta_kwh_g,
        "gap_energia_interna_kwh_g": be.gap_energia_interna_kwh_g,
        "prezzo_medio_attuale_eur_kwh": be.prezzo_medio_attuale_eur_kwh,
        "prezzo_medio_breakeven_eur_kwh": be.prezzo_medio_breakeven_eur_kwh,
        "prezzo_pubblico_breakeven_eur_kwh": be.prezzo_pubblico_breakeven_eur_kwh,
        "capex_massimo_sostenibile_eur": be.capex_massimo_sostenibile_eur,
        "capex_margine_o_gap_eur": be.capex_margine_o_gap_eur,
        "sensitivita_quota_interna_pct": be.sensitivita_quota_interna_pct,
        "sensitivita_quota_interna_delta_tco": be.sensitivita_quota_interna_delta_tco,
        "sensitivita_prezzo_pubblico_eur_kwh": be.sensitivita_prezzo_pubblico_eur_kwh,
        "sensitivita_prezzo_pubblico_delta_tco": be.sensitivita_prezzo_pubblico_delta_tco,
        "azioni_consigliate": be.azioni_consigliate,
    }


def run_scenario_compare(req: "schemas.ScenarioCompareRequest") -> dict:
    """Confronta TUTTI gli scenari ammissibili trovati dalla beam search, piu'
    (opzionalmente) i benchmark standard 1:1/1:2/1:4 — per ciascuno, KPI
    operativi, impatto TCO/ROI e piano di ricarica giornaliero (timeline_p).
    """
    hw_db = _hw_catalog_to_db(req.catalogo_hardware)
    events, vehicles_map, fleet_nv, fleet_km_day_total = _prepare_events(req.gruppi, req.policy)

    engine_cfg = _build_engine_cfg(req.policy)
    fuel = _build_fuel(req.fuel)
    soc_policy = _build_soc_policy(req.policy)
    costi = {"pri": req.energia.prezzo_privato_eur_kwh, "pub": req.energia.prezzo_pubblico_eur_kwh}

    def run_sim(cfg, is_stress):
        return simulazione_soc(
            cfg, events, vehicles_map, costi, hw_db,
            p_shave_limit=req.policy.p_shaving_kw,
            max_ac_v=req.policy.max_ac_veicoli_per_punto,
            max_dc_v=req.policy.max_dc_veicoli_per_punto,
            limit_h=req.policy.limite_ora_turno,
            engine_cfg=engine_cfg, fuel=fuel, soc_policy=soc_policy,
            fleet_km_day_total=fleet_km_day_total,
            is_stress=is_stress,
            sim_days=req.policy.sim_days,
            hybrid_private_home_charging=req.policy.hybrid_private_home_charging,
            company_buffer_pct=req.policy.company_buffer_pct / 100.0,
        )

    ctx = optimizer.OptimizerContext(
        hw_db=hw_db,
        budget_max=req.budget_max_eur,
        p_rete=req.policy.p_rete_kw,
        p_shaving=req.policy.p_shaving_kw,
        fleet_nv=fleet_nv,
        h_limit=req.policy.limite_ora_turno,
        h_plug=req.policy.limite_ora_turno,
        policy_mode="asap",
        allow_oversizing=req.policy.allow_oversizing,
        dc_fixed_power=req.policy.dc_fixed_power,
        dc_redundancy=req.policy.dc_redundancy,
        v_data=_groups_to_df(req.gruppi),
        hybrid_private_home_charging=req.policy.hybrid_private_home_charging,
    )
    params = optimizer.BeamSearchParams(
        hw_selection=req.tipi_hardware_da_esplorare,
        beam_size=req.beam_size, patience=req.patience, max_steps=req.max_steps,
    )
    out = optimizer.run_beam_search(ctx, run_sim, params)
    ranked = sorted(out.results, key=lambda r: optimizer.final_rank(ctx, r))

    scenari = []
    seen_cfg_keys = set()

    scenari_k_by_key = {}

    def _add_scenario(label, cfg, is_benchmark):
        key = tuple(sorted((t, q) for t, q in cfg.items() if q > 0))
        if not key or key in seen_cfg_keys:
            return
        seen_cfg_keys.add(key)
        res = run_sim(cfg, False)
        if res is None:
            return
        k = res["kpi"]
        scenari_k_by_key[key] = k
        capex = float(k.get("c_cap", 0.0))
        ammissibile = capex > 0 and capex <= req.budget_max_eur and optimizer.hard_constraints_ok(ctx, cfg)
        tco_out = None
        breakeven_out = None
        try:
            tco_out = _tco_from_kpi(k, fleet_km_day_total, req.fuel, req.energia, req.vehicle_costs, req.financial)
            raw_result = tco_out.pop("_raw_result", None)
            if raw_result is not None:
                breakeven_out = _breakeven_from_tco(raw_result, k, req.energia, req.financial)
        except Exception:
            tco_out = None
            breakeven_out = None

        # Allocazione intelligente (pool condiviso, LP): confronto affiancato, non sostituisce
        # i KPI del motore esistente (che copre economia/ambiente/tempi che qui non calcoliamo).
        picco_intelligente = None
        copertura_intelligente = None
        try:
            smart_res = run_smart_allocation_for_config(
                events, vehicles_map, hw_db, cfg, p_rete_max_kw=req.policy.p_rete_kw,
                hybrid_private_home_charging=req.policy.hybrid_private_home_charging,
            )
            if smart_res.successo:
                picco_intelligente = round(smart_res.picco_kw, 2)
                copertura_intelligente = round(smart_res.copertura_pct, 1)
            else:
                copertura_intelligente = 0.0
        except Exception:
            pass

        scenari.append({
            "label": label,
            "is_benchmark": is_benchmark,
            "ammissibile": bool(ammissibile),
            "config": {t: int(q) for t, q in cfg.items() if q > 0},
            "capex_eur": capex,
            "copertura_pct": float(k.get("copertura_reale_pct", k.get("perc", 0.0))),
            "veicoli_serviti": int(k.get("veh_served", 0)),
            "veicoli_totali": int(k.get("veh_total", 0)),
            "tco": tco_out,
            "breakeven": breakeven_out,
            "analisi_marginale": None,  # calcolato dopo, sul vero migliore mostrato — vedi in fondo alla funzione
            "timeline_p_kw": [float(x) for x in (res.get("timeline_p") if res.get("timeline_p") is not None else [])],
            "timeline_q": [float(x) for x in (res.get("timeline_q") if res.get("timeline_q") is not None else [])],
            "picco_intelligente_kw": picco_intelligente,
            "copertura_intelligente_pct": copertura_intelligente,
        })

    for i, r in enumerate(ranked):
        label = "Configurazione consigliata" if i == 0 else f"Alternativa ammissibile #{i+1}"
        _add_scenario(label, r.get("config", {}), is_benchmark=False)

    # --- Frontiera compatibile estesa ---
    # La beam search converge su un percorso vincente e poda alternative che restano
    # ammissibili e utili per il confronto (stesso motivo del main.py originale).
    # Qui esploriamo sistematicamente l'intorno della soluzione migliore: quantita'
    # dei tipi "AC-like" variate di ±5 unita' (piu' i rapporti flotta/2, /3, /4), tipi
    # "DC-like" provati a {0, quantita' del seed, 1}. Cap a 24 aggiunte per non
    # esplodere i tempi di calcolo.
    #
    # BUG CORRETTO: prima questo blocco partiva SOLO se la beam search aveva gia'
    # trovato un risultato pienamente ammissibile (`if ranked:`) — se non ne trovava
    # nessuno (caso comune con budget stretti), la frontiera non veniva esplorata
    # affatto e l'utente vedeva al massimo i soli benchmark, non "tutte le soluzioni
    # nel budget". Ora il seed usa, in ordine di preferenza: il miglior risultato
    # ammissibile, il miglior nodo diagnostico esplorato (search_results, anche se
    # non pienamente ammissibile), o un seed proporzionale di default se anche
    # quello manca.
    if ranked:
        seed_cfg = dict(ranked[0].get("config", {}))
    elif out.search_results:
        best_diag = sorted(out.search_results, key=lambda r: optimizer.score(ctx, r))[0]
        seed_cfg = dict(best_diag.get("config", {}))
    else:
        seed_cfg = {}

    if True:
        ac_types = [t for t in req.tipi_hardware_da_esplorare if "AC" in str(t).upper()]
        dc_types = [t for t in req.tipi_hardware_da_esplorare if "DC" in str(t).upper()]

        ac_value_sets = []
        for t in ac_types:
            seed_q = int(seed_cfg.get(t, 0) or 0)
            deltas = {max(1, seed_q + d) for d in range(-5, 6)}
            deltas |= {
                max(1, math.ceil(fleet_nv / 2.0)),
                max(1, math.ceil(fleet_nv / 3.0)),
                max(1, math.ceil(fleet_nv / 4.0)),
            }
            ac_value_sets.append((t, sorted(deltas)))

        dc_value_sets = []
        for t in dc_types:
            seed_q = int(seed_cfg.get(t, 0) or 0)
            dc_value_sets.append((t, sorted({0, seed_q, 1})))

        added = 0
        MAX_FRONTIER = 24
        ac_combos = list(itertools.product(*[vals for _, vals in ac_value_sets])) if ac_value_sets else [()]
        dc_combos = list(itertools.product(*[vals for _, vals in dc_value_sets])) if dc_value_sets else [()]
        for ac_combo in ac_combos:
            for dc_combo in dc_combos:
                if added >= MAX_FRONTIER:
                    break
                cfg_try = {}
                for (t, _), q in zip(ac_value_sets, ac_combo):
                    if q > 0:
                        cfg_try[t] = int(q)
                for (t, _), q in zip(dc_value_sets, dc_combo):
                    if q > 0:
                        cfg_try[t] = int(q)
                if not cfg_try:
                    continue
                before = len(scenari)
                _add_scenario("Frontiera compatibile", cfg_try, is_benchmark=False)
                if len(scenari) > before:
                    added += 1
            if added >= MAX_FRONTIER:
                break

    if req.includi_benchmark:
        for label, cfg in _generate_benchmark_configs(hw_db, fleet_nv).items():
            _add_scenario(label, cfg, is_benchmark=True)

    # Analisi marginale: calcolata DOPO aver raccolto tutti gli scenari (beam +
    # frontiera + benchmark), sul vero "migliore mostrato" — stesso criterio di
    # ordinamento del frontend (ammissibili prima, poi piu' veicoli serviti, poi
    # NPV piu' alto) — non sul solo primo risultato interno della beam search,
    # che poteva restare vuoto (nessun ammissibile) lasciando l'analisi assente.
    if scenari:
        ammissibili = [s for s in scenari if s["ammissibile"]]
        pool = ammissibili if ammissibili else scenari
        best = sorted(pool, key=lambda s: (-s["veicoli_serviti"], -(s["tco"]["npv_eur"] if s["tco"] else 0)))[0]
        best_key = tuple(sorted(best["config"].items()))
        k_best = scenari_k_by_key.get(best_key)
        if k_best is not None:
            try:
                best["analisi_marginale"] = _marginal_charger_analysis(best["config"], k_best, run_sim, hw_db, req.financial)
            except Exception:
                best["analisi_marginale"] = None

    # Segnala esplicitamente, tra le configurazioni ammissibili (100% copertura,
    # entro budget), quella con PIU' punti installati — utile per due motivi
    # concreti che il costo minimo non cattura: (1) meno attesa/coda per il
    # personale che deve spostare l'auto (piu' punti = piu' probabilita' di
    # trovarne uno libero subito), (2) piu' punti fisici disponibili se in
    # futuro si vuole abilitare il V2G, dove ogni veicolo partecipa meglio
    # avendo un punto dedicato invece di condividerne uno con altri.
    ammissibili_per_tag = [s for s in scenari if s["ammissibile"]]
    if ammissibili_per_tag:
        piu_punti = max(ammissibili_per_tag, key=lambda s: sum(s["config"].values()))
        piu_punti["configurazione_abbondante"] = True
        piu_punti["nota_configurazione_abbondante"] = (
            "Più colonnine del minimo necessario per coprire il 100% — riduce l'attesa "
            "per il personale (più probabilità di trovare subito un punto libero) ed è una "
            "base migliore se in futuro vuoi abilitare il V2G (un punto dedicato per veicolo, "
            "invece di condividerne uno)."
        )

    return {"scenari": scenari, "nodi_esplorati": len(out.search_results)}
