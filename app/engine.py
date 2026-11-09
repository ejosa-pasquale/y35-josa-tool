"""
Collegamento tra schemi API (app/schemas.py) e josa_core.

Nessuna logica di business qui: solo conversione — dict/Pydantic -> DataFrame/dataclass
attesi da josa_core, e viceversa per la risposta. Se il motore cambia forma
internamente, cambia solo questo file, non gli endpoint ne' gli schemi esposti.
"""

import math
import itertools
from typing import Optional

import pandas as pd

from josa_core import EngineConfig, FuelCosts, SocPolicy, optimizer
from josa_core import genera_timeline_soc_da_gruppi, simulazione_soc
from josa_core import compliance_dm2025
from josa_core import sustainability
from josa_core import tco as tco_module
from josa_core.ems import VehicleAsset, ChargerAsset, DispatchHorizon, SiteForecast, solve_dispatch
from josa_core.ems.rolling_mpc import VehicleSchedule, run_rolling_mpc
from josa_core.smart_bridge import run_smart_allocation_for_config
from josa_core import carbon

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
            # Pct_veicoli_con_casa: percentuale di VEICOLI del gruppo (non di fabbisogno)
            # che hanno accesso a wallbox domestico. Il motore assegna i primi N veicoli
            # (per ID) come "con casa", il resto senza. Completamente diverso dal vecchio
            # "Buffer_azienda_pct" che era % di fabbisogno coperto a casa.
            "Pct_veicoli_con_casa": g.pct_veicoli_con_casa if g.pct_veicoli_con_casa is not None else float("nan"),
            "Veicoli_con_casa": ",".join(str(v) for v in g.veicoli_con_casa) if g.veicoli_con_casa else "",
            "Probabilita_utilizzo_pct": g.probabilita_utilizzo_pct if g.probabilita_utilizzo_pct is not None else float("nan"),
            "Autonomia_elettrica_km": g.autonomia_elettrica_km if g.autonomia_elettrica_km is not None else float("nan"),
            "Consumo_benzina_l100km": g.consumo_benzina_l100km if g.consumo_benzina_l100km is not None else float("nan"),
            "Accetta_ricarica_dc": bool(g.accetta_ricarica_dc),
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


def _sede_giorno(policy: schemas.EnginePolicy, giorno: Optional[int] = None):
    """Curva della sede da passare al motore: intera, oppure il giorno `giorno`
    (0 = lunedi') quando la curva e' settimanale e si simula un giorno alla volta."""
    sede = getattr(policy, "carico_sede_kw", None)
    if not sede:
        return None
    if giorno is not None and len(sede) == 672:
        g = int(giorno) % 7
        return tuple(float(x) for x in sede[g * 96:(g + 1) * 96])
    return tuple(float(x) for x in sede)


def _build_engine_cfg(policy: schemas.EnginePolicy, giorno: Optional[int] = None) -> EngineConfig:
    return EngineConfig(
        p_rete=policy.p_rete_kw,
        allow_oversizing=policy.allow_oversizing,
        dc_fixed_power=policy.dc_fixed_power,
        dc_redundancy=policy.dc_redundancy,
        carico_sede_kw=_sede_giorno(policy, giorno),
        potenza_contrattuale_kw=getattr(policy, "potenza_contrattuale_kw", None),
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


def _prepare_events(gruppi, policy: schemas.EnginePolicy, seed: int = 42):
    df = _groups_to_df(gruppi)
    soc_params = _build_soc_policy(policy).as_fractions()
    soc_params["hybrid_private_home_charging"] = policy.hybrid_private_home_charging
    soc_params["company_buffer_pct"] = policy.company_buffer_pct / 100.0
    events, vehicles_map, fleet_nv, fleet_km_day_total, cons_avg = genera_timeline_soc_da_gruppi(
        df, soc_params, seed=seed,
    )
    return events, vehicles_map, fleet_nv, fleet_km_day_total


def _build_gantt_veicoli(events: list, vehicles_map: dict, res: dict, orizzonte_h: float = 24.0) -> list:
    """Gantt per veicolo con orari esatti e dettaglio completo:
    - lavoro: ore di guida (HH:MM - HH:MM)
    - carica_azienda: colonnina specifica + energia erogata + orari precisi
    - carica_esterna: ricarica pubblica con orari
    - carica_casa: finestra fissa 23:00-05:00 (convenzione: ricarica lenta
      notturna fuori orario di punta, ogni notte in cui il veicolo ha accesso)
    - sosta: fermo senza attivita
    """
    def _hm(h):
        h = h % 24.0
        hh, mm = int(h), int(round((h - int(h)) * 60))
        if mm == 60: hh, mm = hh + 1, 0
        return f"{hh:02d}:{mm:02d}"

    drive_by_vid = {}
    for e in events:
        if e.get("type") == "drive":
            drive_by_vid.setdefault(e["vid"], []).append((float(e["s"]), float(e["f"])))

    # Energia caricata a casa per veicolo: letta dal campo "Privata casa (kWh)"
    # nel veh_rows per ogni veicolo. Questo valore riflette la quota che il
    # motore ha effettivamente assegnato a casa — può essere 0 per veicoli
    # che avevano già SOC sufficiente dopo la ricarica aziendale.
    # Per il Gantt usiamo il valore per-veicolo: se è 0 non mostriamo il
    # segmento casa (il veicolo non ha avuto bisogno di caricare a casa).
    home_kwh_by_vid = {}
    for vrow in (res.get("veicoli") or []):
        vid = vrow.get("nome", "")
        if not vid:
            continue
        home_kwh_by_vid[vid] = float(vrow.get("Privata casa (kWh)", 0.0))

    sess_az, sess_ext = {}, {}
    # Prima prova a leggere da sessions (formato vecchio)
    for s in res.get("sessions", []) or []:
        vid = s.get("vid")
        lp = s.get("log_p") or {}
        i_h, ec_h = lp.get("i"), lp.get("ec")
        if not vid or i_h is None or ec_h is None:
            continue
        col = lp.get("st", "")
        en = float(s.get("caricato", 0.0))
        is_ext = "ext" in str(s.get("kind", "")).lower() or "EXT" in str(col).upper()
        bucket = sess_ext if is_ext else sess_az
        bucket.setdefault(vid, []).append((float(i_h), float(ec_h), col, en))

    # Se sessions è vuoto, leggi dal gantt_veicoli (formato DLM)
    if not any(sess_az.values()) and not any(sess_ext.values()):
        for vg in res.get("gantt_veicoli", []):
            vid = vg.get("vehicle_id", "")
            for seg in vg.get("segmenti", []):
                stato = seg.get("stato", "")
                col = seg.get("colonnina") or ""
                en = float(seg.get("energia_kwh") or 0.0)
                i_h = seg.get("inizio")
                ec_h = seg.get("fine")
                if i_h is None or ec_h is None or en <= 0:
                    continue
                if stato == "carica_azienda":
                    sess_az.setdefault(vid, []).append((float(i_h), float(ec_h), col, en))
                elif stato == "carica_esterna":
                    sess_ext.setdefault(vid, []).append((float(i_h), float(ec_h), col, en))

    n_giorni = max(1, int(round(orizzonte_h / 24.0)))
    gantt = []
    for vid, v in vehicles_map.items():
        can_home = bool(v.get("can_home_night"))
        raw = []

        for s, f in drive_by_vid.get(vid, []):
            raw.append({"s": s, "f": f, "stato": "lavoro", "colonnina": None,
                        "energia_kwh": None, "note": f"{_hm(s)}–{_hm(f)}"})

        for s, f, col, en in sess_az.get(vid, []):
            raw.append({"s": s, "f": f, "stato": "carica_azienda", "colonnina": col,
                        "energia_kwh": en,
                        "note": f"{_hm(s)}–{_hm(f)} • {col} • {en:.1f} kWh"})

        for s, f, col, en in sess_ext.get(vid, []):
            raw.append({"s": s, "f": f, "stato": "carica_esterna", "colonnina": col,
                        "energia_kwh": en,
                        "note": f"{_hm(s)}–{_hm(f)} • pubblica • {en:.1f} kWh"})

        # Ricarica casa: mostrata SOLO se il motore ha effettivamente caricato
        # energia a casa per questo veicolo (home_private_kwh > 0 nel risultato).
        # La % è definita dall'algoritmo in base alla quota_ricarica_domestica_pct
        # del gruppo — non assume mai la finestra piena se la quota è bassa.
        home_kwh_v = home_kwh_by_vid.get(vid, 0.0)
        if can_home and home_kwh_v > 0:
            kwh_per_notte = round(home_kwh_v / max(1, n_giorni), 2)
            for day in range(n_giorni):
                base = day * 24.0
                s_c, f_c = base + 23.0, base + 29.0   # 23:00 -> 05:00 giorno dopo
                f_c = min(f_c, orizzonte_h)
                if s_c < orizzonte_h:
                    raw.append({"s": s_c, "f": f_c, "stato": "carica_casa",
                                "colonnina": "wallbox casa",
                                "energia_kwh": kwh_per_notte,
                                "note": f"23:00–05:00 • casa • {kwh_per_notte:.1f} kWh"})

        raw.sort(key=lambda x: x["s"])
        segmenti, cursore = [], 0.0
        for ev in raw:
            s = max(0.0, min(orizzonte_h, ev["s"]))
            f = max(0.0, min(orizzonte_h, ev["f"]))
            if f <= cursore:
                continue
            s_eff = max(s, cursore)
            if s_eff > cursore:
                segmenti.append({"inizio": cursore, "fine": s_eff, "stato": "sosta",
                                 "note": f"Sosta {_hm(cursore)}–{_hm(s_eff)}"})
            segmenti.append({"inizio": s_eff, "fine": f, "stato": ev["stato"],
                             "colonnina": ev["colonnina"], "energia_kwh": ev["energia_kwh"],
                             "note": ev["note"]})
            cursore = max(cursore, f)
        if cursore < orizzonte_h:
            segmenti.append({"inizio": cursore, "fine": orizzonte_h, "stato": "sosta",
                             "note": f"Sosta {_hm(cursore)}–{_hm(orizzonte_h)}"})

        gantt.append({"vehicle_id": vid, "gruppo": v.get("group"),
                      "ricarica_domestica": can_home, "segmenti": segmenti})
    return gantt


def _build_gantt_colonnine(res: dict, orizzonte_h: float = 24.0) -> list:
    """Costruisce, per OGNI COLONNINA FISICA (non per veicolo), i segmenti di
    occupazione: quando è occupata, da quale veicolo, e il tasso di utilizzo
    complessivo. Risponde direttamente alla domanda 'questa colonnina, quante
    e quando carica le auto' — vista complementare a _build_gantt_veicoli.
    """
    sessioni_per_stazione: dict = {}
    # Leggi da sessions se disponibile
    for s in res.get("sessions", []) or []:
        lp = s.get("log_p") or {}
        stazione = lp.get("st")
        i, ec = lp.get("i"), lp.get("ec")
        if not stazione or i is None or ec is None:
            continue
        sessioni_per_stazione.setdefault(stazione, []).append({
            "vehicle_id": s.get("vid"), "inizio": float(i), "fine": float(ec),
            "energia_kwh": float(s.get("caricato", 0.0)),
        })
    # Fallback: leggi dal gantt_veicoli (DLM sessions)
    if not sessioni_per_stazione:
        for vg in res.get("gantt_veicoli", []):
            vid = vg.get("vehicle_id", "")
            for seg in vg.get("segmenti", []):
                if seg.get("stato") != "carica_azienda":
                    continue
                stazione = seg.get("colonnina") or ""
                if not stazione:
                    continue
                i, ec = seg.get("inizio"), seg.get("fine")
                if i is None or ec is None:
                    continue
                sessioni_per_stazione.setdefault(stazione, []).append({
                    "vehicle_id": vid, "inizio": float(i), "fine": float(ec),
                    "energia_kwh": float(seg.get("energia_kwh") or 0.0),
                })

    colonnine = []
    for stazione, sessioni in sorted(sessioni_per_stazione.items()):
        sessioni.sort(key=lambda x: x["inizio"])
        segmenti = []
        cursore = 0.0
        tempo_occupato = 0.0
        for sess in sessioni:
            ini = max(0.0, min(orizzonte_h, sess["inizio"]))
            fin = max(0.0, min(orizzonte_h, sess["fine"]))
            if fin <= cursore:
                continue
            ini_eff = max(ini, cursore)
            if ini_eff > cursore:
                segmenti.append({"inizio": cursore, "fine": ini_eff, "stato": "libera"})
            segmenti.append({"inizio": ini_eff, "fine": fin, "stato": "occupata", "vehicle_id": sess["vehicle_id"], "energia_kwh": sess["energia_kwh"]})
            tempo_occupato += (fin - ini_eff)
            cursore = max(cursore, fin)
        if cursore < orizzonte_h:
            segmenti.append({"inizio": cursore, "fine": orizzonte_h, "stato": "libera"})

        colonnine.append({
            "nome_colonnina": stazione,
            "n_veicoli_serviti": len(sessioni),
            "tasso_utilizzo_pct": round((tempo_occupato / orizzonte_h) * 100, 1) if orizzonte_h > 0 else 0.0,
            "segmenti": segmenti,
        })
    return colonnine


def _build_giornata(events: list, vehicles_map: dict, res: dict, req, engine_cfg, hw_db: dict) -> dict:
    """Il giorno tipo (0-24h) a passi da 15 minuti, letto dai risultati della
    simulazione per l'animazione "Rivedi la giornata". Non ricalcola nulla:
    kW per sessione e SOC all'aggancio sono quelli registrati dal motore.
    """
    from josa_core import fasce_orarie
    N = 96
    SL = 0.25
    tl = [float(x) for x in (res.get("timeline_p") if res.get("timeline_p") is not None else [])]
    flotta = [round(tl[i], 2) if i < len(tl) else 0.0 for i in range(N)]

    sede = None
    if engine_cfg.carico_sede_kw:
        c = engine_cfg.carico_sede_kw
        sede = [round(float(c[i % len(c)]), 2) for i in range(N)]
    contratto = engine_cfg.potenza_contrattuale_kw if sede is not None else None

    # Colonnine: nome, tipo, potenza di targa e kW erogati slot per slot
    colonnine = {}
    for t, q in (res.get("config") or {}).items():
        for i in range(int(q)):
            nome = f"{t}_{i+1}"
            colonnine[nome] = {"nome": nome, "tipo": t, "p_kw": float(hw_db[t]["p"]), "kw": [0.0] * N}

    sess_by_vid = {}
    for s in res.get("sessions", []) or []:
        lp = s.get("log_p") or {}
        st, i0 = lp.get("st"), lp.get("i")
        kw = s.get("kw_slot") or []
        if not st or i0 is None or float(s.get("caricato", 0.0)) <= 1e-4:
            continue
        slot0 = int(s["slot0"]) if s.get("slot0") is not None else int(float(i0) / SL)
        if slot0 >= N:
            continue
        if st in colonnine:
            for j, p in enumerate(kw):
                if 0 <= slot0 + j < N:
                    colonnine[st]["kw"][slot0 + j] = round(colonnine[st]["kw"][slot0 + j] + float(p), 3)
        sess_by_vid.setdefault(s.get("vid"), []).append({
            "colonnina": st,
            "slot": slot0,
            "kw": [round(float(p), 2) for p in kw],
            "soc_inizio_kwh": None if s.get("soc_before") is None else round(float(s["soc_before"]), 2),
            "energia_kwh": round(float(s.get("caricato", 0.0)), 2),
        })

    righe = {r.get("nome"): r for r in (res.get("veicoli") or [])}
    hybrid = bool(getattr(req.policy, "hybrid_private_home_charging", True))
    veicoli = []
    for vid, v in vehicles_map.items():
        presenze, guide = [], []
        for e in events:
            if e.get("vid") != vid:
                continue
            s0, f0 = float(e["s"]), float(e["f"])
            if s0 >= 24.0 or f0 <= 0.0:
                continue
            if e.get("type") == "charge":
                presenze.append([round(max(0.0, s0), 3), round(min(24.0, f0), 3)])
            elif e.get("type") == "drive":
                guide.append([round(s0, 3), round(f0, 3), round(float(e.get("e_drive", 0.0)), 3)])
        r = righe.get(vid, {})
        esplicito = float(r.get("Depot (kWh)", 0.0)) + float(r.get("Esterno (kWh)", 0.0)) + float(r.get("Privata casa (kWh)", 0.0))
        servito = esplicito >= float(r.get("Drive (kWh)", 0.0)) - 1e-6 or (hybrid and bool(r.get("can_home_night", True)))
        veicoli.append({
            "id": vid,
            "gruppo": v.get("group"),
            "batteria_kwh": round(float(v.get("batt", 0.0)), 2),
            "soc_start_kwh": round(float(v.get("soc_start", 0.0)), 2),
            "presenze": presenze,
            "guide": guide,
            "sessioni": sorted(sess_by_vid.get(vid, []), key=lambda x: x["slot"]),
            "servito": bool(servito),
        })

    return {
        "slot_h": SL,
        "n_slot": N,
        "flotta_kw": flotta,
        "sede_kw": sede,
        "contratto_kw": contratto,
        "limite_ricarica_kw": float(req.policy.p_shaving_kw),
        "p_rete_kw": float(req.policy.p_rete_kw),
        "fasce": [fasce_orarie.fascia_ora(0, i * SL) for i in range(N)],
        "colonnine": list(colonnine.values()),
        "veicoli": veicoli,
    }


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
    costi = req.energia.costi_motore()

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
            engine_cfg=_build_engine_cfg(req.policy, day_idx), fuel=fuel, soc_policy=soc_policy,
            fleet_km_day_total=km_day_tot, sim_days=1,
            hybrid_private_home_charging=req.policy.hybrid_private_home_charging,
            company_buffer_pct=req.policy.company_buffer_pct / 100.0,
            tolleranza_min=req.policy.tolleranza_tra_veicoli_min,
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


def run_simulate(req: "schemas.SimulateRequest", seed: int = 42, giorno: Optional[int] = None) -> dict:
    hw_db = _hw_catalog_to_db(req.catalogo_hardware)
    events, vehicles_map, fleet_nv, fleet_km_day_total = _prepare_events(req.gruppi, req.policy, seed=seed)

    engine_cfg = _build_engine_cfg(req.policy, giorno)
    fuel = _build_fuel(req.fuel)
    soc_policy = _build_soc_policy(req.policy)
    costi = req.energia.costi_motore()

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
        tolleranza_min=req.policy.tolleranza_tra_veicoli_min,
    )
    if res is None:
        raise ValueError(
            "Configurazione non simulabile: il limite di Peak Shaving supera la Potenza di Rete disponibile "
            "(p_shaving_kw > p_rete_kw). Verifica i valori nella Policy operativa. "
            "La potenza installata delle colonnine non è un vincolo: il DLM la limita dinamicamente."
        )
    k = res["kpi"]
    _attach_carbon(k, req.gruppi, req.fuel, req.carbon, res.get("timeline_p"), req.policy.p_rete_kw)
    if getattr(req, "gantt_settimanale", False):
        gantt_veicoli = _build_gantt_settimanale(req, req.configurazione.quantita)
        gantt_orizzonte_h = 168.0
        gantt_colonnine = []  # non ancora costruito per la vista settimanale
    else:
        gantt_veicoli = _build_gantt_veicoli(events, vehicles_map, res, orizzonte_h=24.0)
        gantt_orizzonte_h = 24.0
        gantt_colonnine = _build_gantt_colonnine(res, orizzonte_h=24.0)
    giornata = None
    if getattr(req, "giornata", False):
        giornata = _build_giornata(events, vehicles_map, res, req, engine_cfg, hw_db)
    return {
        "giornata": giornata,
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
        "gantt_colonnine": gantt_colonnine,
    }


def run_optimize(req: "schemas.OptimizeRequest") -> dict:
    hw_db = _hw_catalog_to_db(req.catalogo_hardware)
    events, vehicles_map, fleet_nv, fleet_km_day_total = _prepare_events(req.gruppi, req.policy)

    engine_cfg = _build_engine_cfg(req.policy)
    fuel = _build_fuel(req.fuel)
    soc_policy = _build_soc_policy(req.policy)
    costi = req.energia.costi_motore()

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
            tolleranza_min=req.policy.tolleranza_tra_veicoli_min,
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
    def _sol_dict(r, ammissibile=True):
        _tl = r.get("timeline_p")
        _tl_list = list(_tl) if _tl is not None else []
        _picco = round(float(max(_tl_list)), 1) if _tl_list else 0.0
        _cap = r.get("capacity") or {}
        _sess = int(_cap.get("sessions_total", 0))
        _n_col = max(1, sum(int(q) for q in r.get("config", {}).values()))
        _k = r.get("kpi", {})
        _carbon = _attach_carbon(_k, req.gruppi, req.fuel, req.carbon, _tl_list, req.policy.p_rete_kw)
        return {
            "config": r.get("config", {}),
            "kpi": _k,
            "carbon": _carbon,
            "capex_eur": float(r.get("kpi", {}).get("c_cap", 0.0)),
            "copertura_pct": float(r.get("kpi", {}).get("copertura_reale_pct", r.get("kpi", {}).get("perc", 0.0))),
            "ammissibile": ammissibile,
            "gap_kwh_da_coprire": None if ammissibile else float(r.get("kpi", {}).get("company_buffer_gap_kwh", 0.0)),
            "timeline_p_kw": [float(x) for x in _tl_list],
            "capacity": _cap,
            "picco_kw": _picco,
            "utilizzo_colonnine": round(_sess / _n_col, 2),
        }
    soluzioni = [_sol_dict(r) for r in ranked]

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
        soluzioni = [_sol_dict(r, ammissibile=False) for r in migliori_parziali]

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


def _prezzo_sede(k: dict, req_energia) -> float:
    """Prezzo dell'energia caricata in sede: con prezzi per fascia oraria e' il medio
    pesato sulla curva simulata, altrimenti il prezzo privato (comportamento storico)."""
    if getattr(req_energia, "prezzi_fasce", None) is not None and k.get("prezzo_sede_eur_kwh") is not None:
        return float(k["prezzo_sede_eur_kwh"])
    return float(req_energia.prezzo_privato_eur_kwh)


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
        prezzo_energia_privato_eur_kwh=_prezzo_sede(k, req_energia),
        prezzo_energia_pubblico_eur_kwh=req_energia.prezzo_pubblico_eur_kwh,
        infra_capex_eur=float(k.get("c_cap", 0.0)),
        infra_om_annuo_eur=float(k.get("mnt", 0.0)),
        staff_ext_annuo_eur=float(k.get("staff_ext", 0.0)),
        vehicle_costs=vc, financial=fin,
        e_home_kwh_g=float(k.get("e_home_private", 0.0)),
        # senza tariffa domestica la casa paga il prezzo privato unico, non quello per fasce della sede
        prezzo_energia_domestica_eur_kwh=(req_energia.prezzo_domestico_eur_kwh
                                          if req_energia.prezzo_domestico_eur_kwh is not None
                                          else req_energia.prezzo_privato_eur_kwh),
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
        prezzo_energia_privato_eur_kwh=_prezzo_sede(k, req_energia),
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
    costi = req.energia.costi_motore()

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
            tolleranza_min=req.policy.tolleranza_tra_veicoli_min,
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
        _carbon = _attach_carbon(k, req.gruppi, req.fuel, req.carbon, res.get("timeline_p"), req.policy.p_rete_kw)
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
            "carbon": _carbon,
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


# ============== CARBON ACCOUNTING ==============

def _carbon_assumptions(carbon_in) -> "carbon.CarbonAssumptions":
    data = carbon_in.model_dump() if carbon_in is not None else {}
    return carbon.CarbonAssumptions(**data)


def _carbon_from_kpi(kpi: dict, gruppi, fuel_in, carbon_in, timeline_p_kw=None) -> dict:
    """Quantificazione ex-ante completa a partire dai KPI di una simulazione."""
    groups = carbon.activities_from_groups(gruppi, fuel_in.diesel_km_l)
    energy = carbon.energy_from_kpi(kpi, timeline_p_kw)
    return carbon.quantify(groups, energy, _carbon_assumptions(carbon_in), tipo="ex_ante")


def _carbon_summary(res: dict) -> dict:
    return {
        "baseline_tco2e_anno": res["baseline_tco2e"],
        "progetto_tco2e_anno": res["progetto_tco2e"],
        "riduzione_netta_tco2e_anno": res["riduzione_netta_tco2e_anno"],
        "riduzione_pct": res["riduzione_pct"],
        "intensita_baseline_g_km": res["intensita_baseline_g_km"],
        "intensita_progetto_g_km": res["intensita_progetto_g_km"],
        "valore_economico_eur_anno": res["valore_economico"]["eur_anno"],
        "valore_economico_modalita": res["valore_economico"]["modalita"],
        "dicitura": res["dicitura"],
    }


def _sustainability_from(kpi: dict, res: dict, gruppi, fuel_in, timeline_p_kw, p_rete_kw) -> dict:
    """Sustainability Score SS-1.0: solo grandezze gia' calcolate (nessun effetto sugli altri KPI)."""
    groups = carbon.activities_from_groups(gruppi, fuel_in.diesel_km_l)
    km_tot = sum(g.km for g in groups)
    km_el = sum(max(0.0, g.km - g.km_termici_phev) for g in groups)
    en = res.get("energia_kwh", {})
    kwh_tot = sum(float(en.get(x, 0.0)) for x in ("deposito_rete", "deposito_go", "deposito_fv", "domestica", "pubblica"))
    kwh_rinn = float(en.get("deposito_go", 0.0)) + float(en.get("deposito_fv", 0.0))
    tl = list(timeline_p_kw) if timeline_p_kw is not None else []
    picco = float(max(tl)) if tl else None
    return sustainability.compute(
        riduzione_pct=res.get("riduzione_pct"),
        intensita_g_km=res.get("intensita_progetto_g_km") if km_tot > 0 else None,
        km_totali=km_tot, km_elettrici=km_el,
        kwh_ricarica_totali=kwh_tot, kwh_rinnovabili=kwh_rinn,
        kwh_sede=float(kpi.get("e_int", 0.0)), kwh_pubblica=float(kpi.get("e_ext", 0.0)),
        picco_kw=picco, potenza_rete_kw=p_rete_kw,
        affidabilita="stimato", versione_fattori=res.get("versione_fattori"),
    )


def _attach_carbon(kpi: dict, gruppi, fuel_in, carbon_in, timeline_p_kw=None, p_rete_kw=None) -> dict:
    """Sostituisce il KPI 'co2' (prima: solo gasolio evitato allo scarico) con la
    riduzione netta del modulo Carbon Accounting. Il vecchio valore resta in
    'co2_lorda_scarico' per confronto. Aggiunge 'sustainability' (score 0-100,
    blocco separato). Restituisce la sintesi carbon."""
    res = _carbon_from_kpi(kpi, gruppi, fuel_in, carbon_in, timeline_p_kw)
    try:
        kpi["sustainability"] = _sustainability_from(kpi, res, gruppi, fuel_in, timeline_p_kw, p_rete_kw)
    except Exception as exc:  # lo score non deve mai bloccare la simulazione
        kpi["sustainability"] = {"score": None, "errore": str(exc), "versione_metodologia": sustainability.VERSIONE}
    summary = _carbon_summary(res)
    if "co2" in kpi and "co2_lorda_scarico" not in kpi:
        kpi["co2_lorda_scarico"] = kpi["co2"]
    kpi["co2"] = summary["riduzione_netta_tco2e_anno"]
    kpi["trees"] = int(max(0.0, kpi["co2"]) * 50)
    kpi["co2_baseline_t"] = summary["baseline_tco2e_anno"]
    kpi["co2_progetto_t"] = summary["progetto_tco2e_anno"]
    kpi["co2_riduzione_pct"] = summary["riduzione_pct"]
    kpi["co2_g_km_baseline"] = summary["intensita_baseline_g_km"]
    kpi["co2_g_km_progetto"] = summary["intensita_progetto_g_km"]
    return summary


def run_carbon_quantify(req: "schemas.CarbonQuantifyRequest") -> dict:
    sim = run_simulate(req)
    res = _carbon_from_kpi(sim["kpi"], req.gruppi, req.fuel, req.carbon, sim.get("timeline_p_kw"))
    res["simulazione"] = {
        "config": sim["config"],
        "copertura_pct": sim["copertura_pct"],
        "veicoli_totali": sim["veicoli_totali"],
        "veicoli_serviti": sim["veicoli_serviti"],
        "energia_kwh_giorno": {
            "deposito": float(sim["kpi"].get("e_int", 0.0)),
            "domestica": float(sim["kpi"].get("e_home_private", 0.0)),
            "pubblica": float(sim["kpi"].get("e_ext", 0.0)),
            "non_servita": float(sim["kpi"].get("e_unserved", 0.0)),
        },
    }
    return res


def run_carbon_quick(req: "schemas.CarbonQuickRequest") -> dict:
    """Stima rapida (home): quota elettrificata + quota di ricarica aziendale.

    Il confine di progetto sono i soli veicoli elettrificati: la loro baseline a
    combustione contro la loro elettricita' di ricarica. I veicoli che restano
    termici emettono uguale nei due scenari, quindi non cambiano la riduzione;
    servono solo per riportarla sulla flotta intera (riduzione_pct_flotta).
    """
    a = _carbon_assumptions(req.carbon)
    giorni = float(a.giorni_operativi_anno)
    km_giorno_veicolo = req.km_anno_veicolo / giorni
    n_ev = req.n_veicoli * req.elettrificazione_pct / 100.0
    km_giorno_ev = n_ev * km_giorno_veicolo
    kml = req.fuel.diesel_km_l

    group = carbon.GroupActivity(
        gruppo="Veicoli elettrificati", n_veicoli=int(round(n_ev)),
        km=km_giorno_ev, baseline_km_l=kml,
    )
    kwh = km_giorno_ev * req.consumo_kwh_km
    q_az = req.ricarica_aziendale_pct / 100.0
    q_dom = req.ricarica_domestica_pct / 100.0
    energy = carbon.EnergyActivity(
        kwh_deposito=kwh * q_az,
        kwh_domestica=kwh * q_dom,
        kwh_pubblica=kwh * max(0.0, 1.0 - q_az - q_dom),
    )
    res = carbon.quantify([group], energy, a, tipo="ex_ante")

    # Baseline della flotta intera (tutti i veicoli a combustione) per la quota sul totale
    fe_id = "gasolio_combustione" if a.baseline_combustibile == "diesel" else "benzina_combustione"
    fe = next(f["valore"] for f in res["fattori_usati"] if f["id"] == fe_id)
    fossil = 1.0 - a.quota_biocarburante_pct / 100.0
    be_flotta = req.n_veicoli * req.km_anno_veicolo / kml * fe * fossil / 1000.0
    if a.includi_well_to_tank:
        fe_wtt = carbon.load_default_library().with_overrides(a.fattori_override).get(
            "gasolio_wtt" if a.baseline_combustibile == "diesel" else "benzina_wtt")["valore"]
        be_flotta += req.n_veicoli * req.km_anno_veicolo / kml * fe_wtt / 1000.0
    res["stima_rapida"] = {
        "n_veicoli": req.n_veicoli,
        "veicoli_elettrificati": round(n_ev, 1),
        "km_anno_veicolo": req.km_anno_veicolo,
        "elettrificazione_pct": req.elettrificazione_pct,
        "ricarica_aziendale_pct": req.ricarica_aziendale_pct,
        "ricarica_domestica_pct": req.ricarica_domestica_pct,
        "ricarica_pubblica_pct": round(max(0.0, 100.0 - req.ricarica_aziendale_pct - req.ricarica_domestica_pct), 1),
        "kwh_anno": round(kwh * giorni, 0),
        "baseline_flotta_tco2e_anno": round(be_flotta, 3),
        "riduzione_pct_flotta": round(res["riduzione_netta_tco2e_anno"] / be_flotta * 100.0, 1) if be_flotta > 0 else 0.0,
    }
    res["avvertenze"] = [w for w in res["avvertenze"] if not w.startswith("Stima ex-ante dalla simulazione")]
    res["avvertenze"].insert(0, "Stima rapida ex-ante da km annui e quote dichiarate, senza simulazione della ricarica: "
                                "non e' un dato misurato. Per il dimensionamento e il pacchetto evidenze usa il percorso completo.")
    return res


def run_carbon_ex_post(req: "schemas.CarbonExPostRequest") -> dict:
    from josa_core.carbon.monitoring import quantify_ex_post
    periodi = [p.model_dump() for p in req.periodi]
    return quantify_ex_post(req.gruppi, req.fuel.diesel_km_l, periodi, _carbon_assumptions(req.carbon))


def run_carbon_sessions_import(req: "schemas.CarbonSessionsImportRequest") -> dict:
    from josa_core.carbon.monitoring import import_sessions_csv
    out = import_sessions_csv(req.csv_text, req.periodo_da, req.periodo_a)
    for k in ("periodo_da", "periodo_a"):
        if out[k] is not None:
            out[k] = out[k].isoformat()
    return out


def run_carbon_evidence(req: "schemas.CarbonEvidenceRequest", tool_version: str) -> dict:
    from josa_core.carbon.evidence import build_evidence_pack
    if req.tipo == "ex_ante":
        if req.ex_ante is None:
            raise ValueError("Per tipo='ex_ante' serve il campo 'ex_ante'")
        payload = req.ex_ante.model_dump(mode="json")
        result = run_carbon_quantify(req.ex_ante)
    else:
        if req.ex_post is None:
            raise ValueError("Per tipo='ex_post' serve il campo 'ex_post'")
        payload = req.ex_post.model_dump(mode="json")
        result = run_carbon_ex_post(req.ex_post)
    return build_evidence_pack(payload, result, tool_version)


GIORNI_SETTIMANA = ["Lun", "Mar", "Mer", "Gio", "Ven"]


def run_weekly_check(req: "schemas.WeeklyCheckRequest") -> dict:
    """Verifica settimanale della configurazione con tolleranza tra veicoli e sensitivita'.

    Per ogni scenario simula `settimane` x 5 giorni lavorativi: ogni giorno ha la sua
    presenza (profilo_presenza x probabilita' di utilizzo del gruppo, max 100%) e una
    estrazione casuale diversa. La copertura di un giorno e' copertura_reale_pct del
    motore. Esito: media >= soglia_media_pct (C1) e 5o percentile dei giorni >=
    soglia_giorni_peggiori_pct (C5)."""
    import numpy as np

    hw_db = _hw_catalog_to_db(req.catalogo_hardware)
    engine_cfg = _build_engine_cfg(req.policy)
    fuel = _build_fuel(req.fuel)
    soc_policy = _build_soc_policy(req.policy)
    costi = req.energia.costi_motore()
    profilo = list(req.profilo_presenza)[:5] or [1.0] * 5

    risultati = []
    for i_sc, sc in enumerate(req.scenari):
        config = dict(req.configurazione.quantita)
        guasti = int(sc.punti_ac_guasti)
        for tipo in sorted(config):
            if guasti <= 0:
                break
            if "DC" not in tipo and config[tipo] > 0:
                tolti = min(guasti, int(config[tipo]))
                config[tipo] -= tolti
                guasti -= tolti
        p_rete = req.policy.p_rete_kw * sc.fattore_potenza
        p_shave = req.policy.p_shaving_kw * sc.fattore_potenza

        coperture = np.zeros((req.settimane, len(profilo)))
        non_serviti = np.zeros_like(coperture)
        for w in range(req.settimane):
            for d, fattore in enumerate(profilo):
                gruppi = []
                for g in req.gruppi:
                    base = 100.0 if g.probabilita_utilizzo_pct is None else float(g.probabilita_utilizzo_pct)
                    pres = 100.0 if sc.presenza_piena else min(100.0, base * float(fattore))
                    gruppi.append(g.model_copy(update={
                        "km_per_giro": g.km_per_giro * sc.moltiplicatore_km,
                        "probabilita_utilizzo_pct": pres,
                    }))
                events, vehicles_map, _, fleet_km_day_total = _prepare_events(
                    gruppi, req.policy, seed=1000 * w + d + 1)
                res = simulazione_soc(
                    config, events, vehicles_map, costi, hw_db,
                    p_shave_limit=p_shave,
                    max_ac_v=req.policy.max_ac_veicoli_per_punto,
                    max_dc_v=req.policy.max_dc_veicoli_per_punto,
                    limit_h=req.policy.limite_ora_turno,
                    engine_cfg=EngineConfig(p_rete=p_rete, allow_oversizing=True,
                                            dc_fixed_power=req.policy.dc_fixed_power,
                                            dc_redundancy=req.policy.dc_redundancy,
                                            carico_sede_kw=_sede_giorno(req.policy, d),
                                            potenza_contrattuale_kw=req.policy.potenza_contrattuale_kw),
                    fuel=fuel, soc_policy=soc_policy,
                    fleet_km_day_total=fleet_km_day_total,
                    sim_days=1,
                    hybrid_private_home_charging=req.policy.hybrid_private_home_charging,
                    company_buffer_pct=req.policy.company_buffer_pct / 100.0,
                    tolleranza_min=req.tolleranza_min,
                )
                k = (res or {}).get("kpi", {})
                coperture[w, d] = float(k.get("copertura_reale_pct", k.get("perc", 0.0)))
                non_serviti[w, d] = float(k.get("veh_unserved", 0))

        media = float(coperture.mean())
        p5 = float(np.percentile(coperture.ravel(), 5))
        risultati.append({
            "scenario": sc.nome,
            "configurazione": config,
            "copertura_media_pct": round(media, 1),
            "copertura_giorni_peggiori_pct": round(p5, 1),
            "copertura_minima_per_giorno_pct": {
                GIORNI_SETTIMANA[d] if d < 5 else f"G{d+1}": round(float(coperture[:, d].min()), 1)
                for d in range(len(profilo))
            },
            "veicoli_non_serviti_max": int(non_serviti.max()),
            # C1 (servizio medio) si applica allo scenario base, il primo della lista;
            # gli scenari di stress devono rispettare solo C5 (giorni peggiori).
            "c1_servizio_ok": (media >= req.soglia_media_pct) if i_sc == 0 else None,
            "c5_robustezza_ok": p5 >= req.soglia_giorni_peggiori_pct,
        })

    return {
        "tolleranza_min": req.tolleranza_min,
        "giorni_simulati_per_scenario": req.settimane * len(profilo),
        "scenari": risultati,
        "superata": all(r["c1_servizio_ok"] is not False and r["c5_robustezza_ok"] for r in risultati),
    }


# ============== VERIFICA PREVENTIVO NEI DUE SENSI ==============
# Usa la verifica settimanale esistente (run_weekly_check) come criterio di "regge / non regge":
# nessuna logica nuova nel motore, solo ricerche sul numero di auto, di punti e sulla potenza.

def _ripartisci_auto(sedi, totale: int) -> list[int]:
    if all(s.n_auto is not None for s in sedi):
        return [int(s.n_auto) for s in sedi]
    fissi = sum(int(s.n_auto or 0) for s in sedi if s.n_auto is not None)
    libere = [i for i, s in enumerate(sedi) if s.n_auto is None]
    resto = max(0, totale - fissi)
    pesi = [max(1, sedi[i].n_ac + sedi[i].n_dc) for i in libere]
    quote = [resto * p / sum(pesi) for p in pesi]
    out = [int(s.n_auto) if s.n_auto is not None else 0 for s in sedi]
    base = [int(math.floor(q)) for q in quote]
    avanzo = resto - sum(base)
    ordine = sorted(range(len(libere)), key=lambda j: quote[j] - base[j], reverse=True)
    for j in ordine[:avanzo]:
        base[j] += 1
    for j, i in enumerate(libere):
        out[i] = base[j]
    return out


def run_verifica_preventivo(req: "schemas.VerificaPreventivoRequest") -> dict:
    nome_ac = lambda s: f"AC {s.kw_ac:g} kW"
    nome_dc = lambda s: f"DC {s.kw_dc:g} kW"
    finestra_min = (req.partenza.hour * 60 + req.partenza.minute) - (req.arrivo.hour * 60 + req.arrivo.minute)
    if finestra_min <= 0:
        raise ValueError("L'orario di partenza deve essere dopo l'arrivo.")
    limite_h = req.partenza.hour + req.partenza.minute / 60
    scenari = [schemas.ScenarioSensitivita(nome="Base"),
               schemas.ScenarioSensitivita(nome="Presenza 100%", presenza_piena=True),
               schemas.ScenarioSensitivita(nome="1 punto AC guasto", punti_ac_guasti=1)]
    con_casa = req.pct_con_casa > 0

    def gruppi(n):
        return [schemas.FleetGroup(
            gruppo="Auto elettriche", profilo="Office", n_veicoli=n, km_per_giro=req.km_giorno,
            giri_per_veicolo_giorno=1, tempo_disponibile_min=finestra_min,
            finestra_inizio=req.arrivo, finestra_fine=req.partenza,
            consumo_kwh_km=req.consumo_kwh_km, batteria_kwh=req.batteria_kwh,
            contemporanei_max=n, potenza_max_ricarica_ac_kw=req.potenza_ac_auto_kw,
            probabilita_utilizzo_pct=req.presenza_pct,
            ricarica_domestica=con_casa, pct_veicoli_con_casa=req.pct_con_casa if con_casa else None)]

    def catalogo(s):
        return [schemas.HardwareSpec(nome=nome_ac(s), potenza_kw=s.kw_ac, costo_acquisto_eur=0,
                                     costo_installazione_eur=0, costo_manutenzione_eur_anno=0),
                schemas.HardwareSpec(nome=nome_dc(s), potenza_kw=s.kw_dc, costo_acquisto_eur=0,
                                     costo_installazione_eur=0, costo_manutenzione_eur_anno=0)]

    def policy(p_kw, rot):
        return schemas.EnginePolicy(p_rete_kw=p_kw, p_shaving_kw=p_kw, max_ac_veicoli_per_punto=rot,
                                    max_dc_veicoli_per_punto=max(1, min(6, rot + 2)) if rot > 1 else 1,
                                    limite_ora_turno=limite_h, hybrid_private_home_charging=con_casa,
                                    company_buffer_pct=30 if con_casa else 100)

    cache: dict = {}

    def passa(s, n, n_ac, n_dc, rot, p_kw):
        if n <= 0:
            return True
        if n_ac + n_dc <= 0:
            return False
        chiave = (s.nome, n, n_ac, n_dc, rot, round(p_kw, 1))
        if chiave not in cache:
            r = run_weekly_check(schemas.WeeklyCheckRequest(
                gruppi=gruppi(n), catalogo_hardware=catalogo(s), policy=policy(p_kw, rot),
                configurazione=schemas.HardwareConfig(quantita={nome_ac(s): n_ac, nome_dc(s): n_dc}),
                settimane=req.settimane, scenari=scenari))
            cache[chiave] = bool(r["superata"])
        return cache[chiave]

    def massimo(pred, lo, hi):
        """Massimo x in [lo, hi] con pred(x) vero (pred monotona decrescente)."""
        if not pred(lo):
            return lo - 1
        while lo < hi:
            m = (lo + hi + 1) // 2
            if pred(m):
                lo = m
            else:
                hi = m - 1
        return lo

    def minimo(pred, lo, hi):
        """Minimo x in [lo, hi] con pred(x) vero (pred monotona crescente); None se nessuno."""
        if not pred(hi):
            return None
        while lo < hi:
            m = (lo + hi) // 2
            if pred(m):
                hi = m
            else:
                lo = m + 1
        return lo

    def picco(s, n, n_ac, n_dc, rot, p_kw):
        if n <= 0 or n_ac + n_dc <= 0:
            return 0.0
        g = gruppi(n)
        g[0] = g[0].model_copy(update={"probabilita_utilizzo_pct": 100.0})
        r = run_simulate(schemas.SimulateRequest(
            gruppi=g, catalogo_hardware=catalogo(s), policy=policy(p_kw, rot),
            configurazione=schemas.HardwareConfig(quantita={nome_ac(s): n_ac, nome_dc(s): n_dc})))
        return float(max(r["timeline_p_kw"] or [0.0]))

    auto = _ripartisci_auto(req.sedi, req.n_auto_totali)
    rot_rif = max(1, min(8, int(req.rotazione_riferimento)))
    rotazioni = sorted({max(1, min(8, int(x))) for x in req.rotazioni} | {rot_rif})
    sedi_out = []
    for s, n_auto in zip(req.sedi, auto):
        p_disp = float(s.potenza_disponibile_kw or (s.n_ac * s.kw_ac + s.n_dc * s.kw_dc) or 100.0)
        nominale = s.n_ac * s.kw_ac + s.n_dc * s.kw_dc
        per_rot = []
        for rot in rotazioni:
            hi_cap = s.n_ac * rot + s.n_dc * (1 if rot == 1 else min(6, rot + 2))
            cap = max(0, massimo(lambda n: passa(s, n, s.n_ac, s.n_dc, rot, p_disp), 1, max(1, hi_cap)))
            ok_reale = passa(s, n_auto, s.n_ac, s.n_dc, rot, p_disp)
            hi_ac = max(s.n_ac, n_auto) + 2
            ac_con_dc = minimo(lambda a: passa(s, n_auto, a, s.n_dc, rot, p_disp), 0, hi_ac) if n_auto else 0
            ac_senza_dc = minimo(lambda a: passa(s, n_auto, a, 0, rot, p_disp), 0, hi_ac) if n_auto else 0
            per_rot.append({
                "auto_per_punto_ac": rot,
                "capacita_auto": cap,
                "regge_flotta_reale": ok_reale,
                "ac_ideali_con_dc_del_preventivo": ac_con_dc,
                "ac_ideali_senza_dc": ac_senza_dc,
            })

        # Configurazione ideale alla rotazione di riferimento (auto al giorno per punto AC).
        # Potenza ideale: la minima che, con il bilanciamento dinamico, supera ancora la
        # verifica (passi da 5 kW); in piu' il picco libero, senza limite, a presenza piena.
        rif = next(r for r in per_rot if r["auto_per_punto_ac"] == rot_rif)
        ac_id = rif["ac_ideali_con_dc_del_preventivo"]
        if ac_id is None:
            ac_id = s.n_ac
        potenza_ideale = None
        if n_auto and (ac_id + s.n_dc) > 0:
            passi = minimo(lambda k: passa(s, n_auto, ac_id, s.n_dc, rot_rif, 5.0 * k),
                           1, max(1, int(math.ceil(p_disp / 5.0))))
            potenza_ideale = 5.0 * passi if passi is not None else None
            # Limite fisico: l'energia del giorno medio deve entrare nella finestra di sosta.
            # (Con un'auto per punto la simulazione puo' proseguire oltre la partenza e
            # sottostimare la potenza: questo minimo lo impedisce.)
            e_giorno = n_auto * req.km_giorno * req.consumo_kwh_km * (req.presenza_pct / 100.0)
            if con_casa:
                e_giorno *= 1.0 - 0.7 * req.pct_con_casa / 100.0
            minimo_fisico = 5.0 * math.ceil(e_giorno / (finestra_min / 60.0) / 5.0)
            if potenza_ideale is not None:
                potenza_ideale = max(potenza_ideale, minimo_fisico)
        picco_ideale = picco(s, n_auto, ac_id, s.n_dc, rot_rif, p_disp)
        picco_preventivo = picco(s, n_auto, s.n_ac, s.n_dc, rot_rif, p_disp)
        sedi_out.append({
            "nome": s.nome,
            "auto_flotta_reale": n_auto,
            "preventivo": {"n_ac": s.n_ac, "kw_ac": s.kw_ac, "n_dc": s.n_dc, "kw_dc": s.kw_dc,
                           "potenza_disponibile_kw": p_disp, "potenza_nominale_kw": nominale},
            "per_rotazione": per_rot,
            "ideale": {"n_ac": ac_id, "n_dc": s.n_dc, "potenza_kw": potenza_ideale,
                       "picco_libero_kw": round(picco_ideale, 1),
                       "punti_in_piu_nel_preventivo": (s.n_ac - ac_id)},
            "picco_simulato_kw": round(picco_preventivo, 1),
            "energia_giorno_kwh": round(n_auto * req.km_giorno * req.consumo_kwh_km * (req.presenza_pct / 100.0), 1),
        })

    # Sintesi complessiva
    tot = {
        "auto_flotta_reale": sum(x["auto_flotta_reale"] for x in sedi_out),
        "punti_ac": sum(s.n_ac for s in req.sedi),
        "punti_dc": sum(s.n_dc for s in req.sedi),
        "capacita_per_rotazione": {
            str(rot): sum(x["per_rotazione"][i]["capacita_auto"] for x in sedi_out)
            for i, rot in enumerate(rotazioni)},
        "ac_ideali": sum(x["ideale"]["n_ac"] for x in sedi_out),
        "potenza_disponibile_kw": sum(x["preventivo"]["potenza_disponibile_kw"] for x in sedi_out),
        "potenza_ideale_kw": (sum(x["ideale"]["potenza_kw"] for x in sedi_out)
                              if all(x["ideale"]["potenza_kw"] is not None for x in sedi_out) else None),
        "picco_simulato_kw": round(sum(x["picco_simulato_kw"] for x in sedi_out), 1),
    }
    punti = tot["punti_ac"] + tot["punti_dc"]
    i_rif = rotazioni.index(rot_rif)
    cap_rif = tot["capacita_per_rotazione"][str(rot_rif)]
    tot["capacita_auto"] = cap_rif
    tot["rotazione_riferimento"] = rot_rif
    utilizzo = tot["auto_flotta_reale"] / cap_rif if cap_rif else None
    tot["utilizzo_capacita_pct"] = round(100 * utilizzo, 0) if utilizzo is not None else None
    eccesso_ac = tot["punti_ac"] - tot["ac_ideali"]
    costo_punto = (req.totale_imponibile_eur / punti) if (req.totale_imponibile_eur and punti) else None
    tot["costo_medio_per_punto_eur"] = round(costo_punto, 0) if costo_punto else None

    eur = lambda v: f"{v:,.0f}".replace(",", ".")
    if any(not x["per_rotazione"][i_rif]["regge_flotta_reale"] for x in sedi_out):
        esito = "sottodimensionato"
    elif eccesso_ac >= max(2, 0.2 * tot["punti_ac"]):
        esito = "sovradimensionato"
    else:
        esito = "adeguato"

    spiegazione = []
    cap_desc = ", ".join(
        f"{tot['capacita_per_rotazione'][str(r)]} con {r} auto per punto" for r in rotazioni)
    spiegazione.append(
        f"Il preventivo ({tot['punti_ac']} AC + {tot['punti_dc']} DC) regge circa {cap_rif} auto "
        f"se ogni punto AC serve fino a {rot_rif} auto al giorno; le auto reali sono {tot['auto_flotta_reale']}. "
        f"Dipende da quante volte al giorno si sposta l'auto: {cap_desc}.")
    if utilizzo is not None:
        spiegazione.append(
            f"Con {tot['auto_flotta_reale']} auto si usa circa il {tot['utilizzo_capacita_pct']:.0f}% di questa capacita': "
            f"in media {str(round(tot['auto_flotta_reale'] / max(1, punti), 1)).replace('.', ',')} auto per punto.")
    for x in sedi_out:
        i = x["ideale"]
        frase = (f"{x['nome']}: per {x['auto_flotta_reale']} auto bastano {i['n_ac']} AC"
                 + (f" + {x['preventivo']['n_dc']} DC" if x['preventivo']['n_dc'] else "")
                 + f" con fino a {rot_rif} auto per punto al giorno (preventivo: {x['preventivo']['n_ac']} AC)")
        if i["potenza_kw"] is not None:
            frase += (f"; potenza: minimo {i['potenza_kw']:.0f} kW con bilanciamento dinamico, "
                      f"{i['picco_libero_kw']:.0f} kW di picco senza limiti, "
                      f"contro {x['preventivo']['potenza_disponibile_kw']:.0f} kW disponibili")
        spiegazione.append(frase + ".")
    spiegazione.append(
        f"La potenza di targa ({sum(x['preventivo']['potenza_nominale_kw'] for x in sedi_out):.0f} kW) non si raggiunge: "
        f"le auto caricano in AC al massimo a {req.potenza_ac_auto_kw:g} kW e non tutte insieme. "
        f"Picco simulato con le auto reali, tutte presenti: {tot['picco_simulato_kw']:.0f} kW.")
    if eccesso_ac > 0 and costo_punto:
        spiegazione.append(
            f"Ordine di grandezza: {eccesso_ac} punti AC oltre il necessario; a un costo medio di "
            f"{eur(costo_punto)} EUR per punto del preventivo (opere comprese) valgono circa "
            f"{eur(eccesso_ac * costo_punto)} EUR. Non e' tutto risparmiabile: quadri e scavi restano in parte.")
    spiegazione.append(
        "Il sovradimensionamento puo' essere voluto: crescita della flotta, auto dei dipendenti, "
        "nessuno che sposta le auto durante il giorno. In quel caso va scritto nel preventivo.")

    return {
        "esito": esito,
        "sedi": sedi_out,
        "totale": tot,
        "spiegazione": spiegazione,
        "ipotesi": {
            "km_giorno": req.km_giorno, "consumo_kwh_km": req.consumo_kwh_km,
            "potenza_ac_auto_kw": req.potenza_ac_auto_kw, "presenza_pct": req.presenza_pct,
            "pct_con_casa": req.pct_con_casa, "orario": f"{req.arrivo.strftime('%H:%M')}-{req.partenza.strftime('%H:%M')}",
            "criterio": (f"verifica settimanale Y35 su {req.settimane} settimane, 60 min di tolleranza tra auto, "
                         "scenari: base, presenza 100%, un punto AC guasto"),
        },
    }
