"""
Motore di simulazione SOC per veicolo/flotta.

Estratto identico da main.py: simulazione() [legacy] e simulazione_soc()
[motore principale multi-giorno]. La logica NON e' cambiata: le uniche
modifiche sono le firme delle funzioni, che ora ricevono esplicitamente
engine_cfg / fuel / soc_policy / fleet_km_day_total invece di leggerli come
variabili globali dello script Streamlit (p_rete, allow_oversizing,
dc_fixed_power, km_l, e_l, h_rate, soc_start_pct, soc_min_pct, soc_max_pct,
soc_buffer_pct).
"""

import zlib

import numpy as np
import pandas as pd

from .models import EngineConfig, FuelCosts, SocPolicy


def simulazione(
    config, veicoli, costi, hw_params,
    p_shave_limit,
    max_ac_v,
    max_dc_v,
    limit_h,
    engine_cfg,
    fuel,
    fleet_km_day_total,
    is_stress=False, extra_c=0, delay_m=0,
):
    stations = []
    p_tot_inst = sum(hw_params[t]["p"] * q for t, q in config.items())
    # Vincolo di potenza: il picco prelevabile (Peak Shaving) deve rispettare la Potenza Rete.
    if p_shave_limit > engine_cfg.p_rete: return None
    # La potenza installata può eccedere la potenza di rete se c'è load management (oversizing prese).
    if (not engine_cfg.allow_oversizing) and (p_tot_inst > engine_cfg.p_rete):
        return None
    
    for t, q in config.items():
        for i in range(q): 
            stations.append({"nome": f"{t}_{i+1}", "p": hw_params[t]["p"], "type": t, "busy": 0.0, "v_count": 0})
            
    power_timeline = np.zeros(144)
    queue_timeline = np.zeros(144)

    v_sim = []
    for v in veicoli:
        v_c = v.copy()
        if is_stress:
            v_c["e_req"] *= (1 + extra_c/100)
            v_c["s"] += (delay_m/60)
        v_sim.append({**v_c, "caricato": 0.0, "log_p": None, "wait_h": None})

    for v in sorted(v_sim, key=lambda x: x["s"]):
        best_s = None
        t_start_best = 999
        
        for s in stations:
            if "AC" in s["type"] and s["v_count"] >= max_ac_v: continue
            if "DC" in s["type"] and s["v_count"] >= max_dc_v: continue
            avail = s["busy"]
            if avail > limit_h and avail < 24: continue 
            act = max(avail, v["s"])
            if act < t_start_best and act <= limit_h:
                t_start_best, best_s = act, s
        
        if best_s:
            p_nominale = min(best_s["p"], v.get("potenza_max_ac_kw", 11.0)) if "AC" in best_s["type"] else best_s["p"]
            max_can_charge = min(v["e_req"], v["batt"])
            # Se la DC è a "spunto" (non modulabile), posticipa l'inizio al primo slot in cui c'è
            # abbastanza potenza disponibile per erogare la potenza nominale.
            if engine_cfg.dc_fixed_power and ("DC" in best_s["type"]):
                t_scan = t_start_best
                t_end_scan = float(v.get("f", limit_h))
                t_end_scan = min(t_end_scan, 36.0)
                while t_scan < t_end_scan:
                    sidx = int(t_scan * 4)
                    if sidx >= 144:
                        break
                    if (p_shave_limit - power_timeline[sidx]) >= (p_nominale - 1e-9):
                        break
                    t_scan += 0.25
                t_start_best = t_scan

            # aggiorna attesa/coda usando l'orario di inizio effettivo
            v["wait_h"] = max(0.0, t_start_best - v["s"])
            a0 = int(max(0, v["s"] * 4))
            a1 = int(max(0, t_start_best * 4))
            a0 = min(a0, 143); a1 = min(a1, 144)
            if a1 > a0:
                queue_timeline[a0:a1] += 1

            current_t = t_start_best
            carica_accumulata = 0.0
            
            # Una volta "attaccata" la presa (prima di fine turno), la ricarica può proseguire anche oltre fine turno
            # fino alla partenza del veicolo (finestra disponibile).
            t_end = float(v.get("f", limit_h))
            t_end = min(t_end, 36.0)  # orizzonte massimo della timeline (144 slot da 15min)

            while current_t < t_end and carica_accumulata < max_can_charge:
                slot_idx = int(current_t * 4)
                if slot_idx >= 144: break
                p_available_shaving = max(0, p_shave_limit - power_timeline[slot_idx])
                if engine_cfg.dc_fixed_power and ("DC" in best_s["type"]):
                    # DC non modulabile: o eroga p_nominale o 0
                    p_effettiva = p_nominale if p_available_shaving >= (p_nominale - 1e-9) else 0.0
                else:
                    p_effettiva = min(p_nominale, p_available_shaving)
                if p_effettiva > 0:
                    energia_slot = p_effettiva * 0.25
                    if carica_accumulata + energia_slot > max_can_charge:
                        energia_slot = max_can_charge - carica_accumulata
                        p_effettiva = energia_slot / 0.25
                    power_timeline[slot_idx] += p_effettiva
                    carica_accumulata += energia_slot
                current_t += 0.25
            
            v["caricato"] = carica_accumulata
            v["log_p"] = {"st": best_s["nome"], "i": t_start_best, "ec": current_t}
            best_s["busy"] = current_t + 0.1
            best_s["v_count"] += 1
    
    e_tot = sum(v["e_req"] for v in v_sim)
    e_int = sum(v["caricato"] for v in v_sim)
    e_ext = max(0, e_tot - e_int)

    # KPI veicoli serviti (utile per capire se alcuni mezzi non vengono proprio agganciati)
    veh_total = len(v_sim)
    veh_with_charge = sum(1 for v in v_sim if float(v.get("caricato", 0.0)) > 0.0)
    veh_unserved = sum(1 for v in v_sim if float(v.get("caricato", 0.0)) <= 0.0 and float(v.get("e_req", 0.0)) > 0.0)
    veh_served_pct = round((veh_with_charge / veh_total) * 100, 1) if veh_total else 0.0

    sessions_total = int(sum(1 for v in v_sim if float(v.get("caricato", 0.0)) > 0.0))
    sessions_ac = int(sum(1 for v in v_sim if float(v.get("caricato", 0.0)) > 0.0 and "AC" in str((v.get("log_p") or {}).get("st", ""))))
    sessions_dc = int(sum(1 for v in v_sim if float(v.get("caricato", 0.0)) > 0.0 and "DC" in str((v.get("log_p") or {}).get("st", ""))))

    c_cap = sum((hw_params[t]["acq"] + hw_params[t]["ins"]) * q for t, q in config.items())
    c_mnt = sum(hw_params[t]["mnt"] * q for t, q in config.items())
    risp_diesel = (fleet_km_day_total / fuel.km_l * fuel.e_l) * 365
    staff_cost = (e_ext / 30 * fuel.h_rate) * 365
    pena_en = (e_ext * (costi['pub'] - costi['pri'])) * 365
    costo_ev_teorico = (e_int * costi['pri'] + e_ext * costi['pub']) * 365 + staff_cost + c_mnt
    demand_ref = max(float(e_tot), float(e_int + e_ext))
    e_ext_effective = max(float(e_ext), float(demand_ref - e_int))
    unserved_energy = max(0.0, float(demand_ref - (e_int + e_ext)))
    staff_cost_effective = (e_ext_effective / 30 * fuel.h_rate) * 365
    costo_ev = (e_int * costi['pri'] + e_ext_effective * costi['pub']) * 365 + staff_cost_effective + c_mnt
    co2 = (fleet_km_day_total / fuel.km_l * 2.65 * 365) / 1000
    
    energy_need_recorded = e_int + e_ext
    coverage_recorded = round((e_int / energy_need_recorded) * 100, 1) if energy_need_recorded > 0 else 100.0
    coverage_validated = round((e_int / demand_ref) * 100, 1) if demand_ref > 0 else 100.0

    return {
        "config": config, "veicoli": v_sim, "timeline_p": power_timeline, "timeline_q": queue_timeline,
        "kpi": {
            "perc": coverage_validated,
            "perc_recorded": coverage_recorded,
            "wait_avg_min": float(np.nanmean([(v.get('wait_h') if v.get('wait_h') is not None else np.nan) for v in v_sim]) * 60),
            "wait_p95_min": float(np.nanpercentile([v.get('wait_h', 0.0) for v in v_sim if v.get('wait_h') is not None], 95) * 60) if any(v.get('wait_h') is not None for v in v_sim) else 0.0,
            "queue_max": float(np.max(queue_timeline)),
            "c_cap": c_cap, "risp": risp_diesel - costo_ev, "risp_teorico": risp_diesel - costo_ev_teorico, "mnt": c_mnt,
            "pena_en": pena_en, "staff_ext": staff_cost_effective, "staff_ext_recorded": staff_cost, "co2": co2, "trees": int(co2 * 50),
            "e_int": e_int, "e_ext": e_ext_effective, "e_ext_recorded": e_ext, "e_unserved": unserved_energy,
            "e_tot": e_tot, "e_need": demand_ref,
            "coverage_formula": "e_int / max(e_tot, e_int + e_ext)",
            "veh_total": int(veh_total),
            "veh_served": int(veh_with_charge),
            "veh_unserved": int(veh_unserved),
            "veh_served_pct": float(veh_served_pct),
            "sessions_total": int(sessions_total),
            "sessions_ac": int(sessions_ac),
            "sessions_dc": int(sessions_dc),
        }
    }



def simulazione_soc(
    config,
    events,
    vehicles_map,
    costi,
    hw_params,
    p_shave_limit,
    max_ac_v,
    max_dc_v,
    limit_h,
    engine_cfg,
    fuel,
    soc_policy,
    fleet_km_day_total,
    allow_night_after_shift=True,
    staff_no_rotation_night=True,
    max_wait_night_min=0,
    max_wait_topup_min=10,
    is_stress=False,
    extra_c=0,
    delay_m=0,
    sim_days=1,
    hybrid_private_home_charging=True,
    company_buffer_pct=0.30,
):
    """Simulazione SOC multi-giro.

    - Gli eventi includono drive (consumo) e charge (finestre in deposito)
    - Si parte da SOC iniziale (soc_start) già carico a inizio giornata
    - In logica ibrida, la notte aziendale e rimossa: il ripristino a SOC target avviene come ricarica domestica privata.
    - In azienda si tenta di coprire un buffer diurno minimo (default 30%) anche con fast charging se la finestra e breve.
    """
    try:
        sim_days_int_pre = int(sim_days)
    except Exception:
        sim_days_int_pre = 1
    sim_days_int_pre = max(1, sim_days_int_pre)

    stations = []
    p_tot_inst = sum(hw_params[t]["p"] * q for t, q in config.items())
    # Vincolo di potenza: il picco prelevabile (Peak Shaving) deve rispettare la Potenza Rete.
    if p_shave_limit > engine_cfg.p_rete:
        return None
    # La potenza installata può eccedere la potenza di rete se c'è load management (oversizing prese).
    if (not engine_cfg.allow_oversizing) and (p_tot_inst > engine_cfg.p_rete):
        return None

    for t, q in config.items():
        for i in range(q):
            # v_count viene tracciato per giorno (evita che il vincolo "auto/colonnina/gg" blocchi scenari multi-giorno)
            stations.append({"nome": f"{t}_{i+1}", "p": hw_params[t]["p"], "type": t, "busy": 0.0, "v_count_day": {}})

    # Timeline dinamica (supporta multi-giorno): 15 min slot fino a fine ultimo evento + buffer
    max_h = 36.0
    if events:
        max_h = max(max_h, float(max(ev.get("f", 0.0) for ev in events)) + 1.0)
    n_slots = int(np.ceil(max_h * 4))
    power_timeline = np.zeros(n_slots)
    queue_timeline = np.zeros(n_slots)

    # Copia veicoli (stato SOC)
    v_state = {}
    for vid, v in vehicles_map.items():
        v_state[vid] = v.copy()
        # reset state for simulation
        v_state[vid]["soc"] = float(v.get("soc_start", v.get("soc", 0.0)))
        v_state[vid]["drive_kwh"] = 0.0
        v_state[vid]["depot_kwh"] = 0.0
        v_state[vid]["ext_kwh"] = 0.0
        v_state[vid]["ext_events"] = 0
        v_state[vid]["overnight_kwh"] = 0.0
        v_state[vid]["home_private_kwh"] = float(v_state[vid].get("home_private_kwh", 0.0) or 0.0)
        v_state[vid]["company_buffer_target_kwh"] = float(v_state[vid].get("company_buffer_target_kwh", 0.0) or 0.0) * float(sim_days_int_pre)
        v_state[vid]["company_buffer_charged_kwh"] = 0.0

    # Log sessioni di ricarica (per timeline/code)
    sessions = []
    wait_list_h = []
    e_drive_total = 0.0
    e_depot_total = 0.0
    e_ext_total = 0.0
    e_home_private_total = 0.0
    e_home_private_total_ref = [0.0]

    # KPI: "parto al SOC target" al primo giro di ogni giorno
    first_departure_checked = {}
    morning_shortfall_total = 0.0
    morning_not_full_days = 0

    # Per visualizzare l'evoluzione multi-giorno: SOC al primo giro per veicolo/giorno
    soc_morning_rows = []

    delay_h = (delay_m / 60.0) if is_stress else 0.0
    extra_mul = (1 + extra_c / 100.0) if is_stress else 1.0

    # Helper: assegna e simula una singola finestra di ricarica
    def _serve_charge_window(vid, kind, s, f, e_next, soc_min, soc_max, soc_buffer, soc_start, charge_reason=""):
        nonlocal e_depot_total
        nonlocal e_ext_total
        v = v_state[vid]
        batt = float(v["batt"])
        soc_kwh = float(v["soc"])

        # target SOC
        if kind == "night":
            # Notte: obiettivo = SOC_start (pieno operativo per il giorno dopo)
            target_kwh = batt * float(soc_start)
        else:
            # Giorno: base = riserva + energia prossimo giro + buffer
            base_target = batt * float(soc_min) + float(e_next) * extra_mul + batt * float(soc_buffer)
            base_target = min(base_target, batt * float(soc_max))

            # Opportunistic charging (per liberare la notte):
            # - Office/Ufficio: se sono in sede al mattino, puntiamo a SOC_start
            # - Stop tra giri (e_next>0): se posso, faccio un top-up verso SOC_start (meglio con DC)
            prof = str(v.get("profilo", "") or "").strip().lower()
            grp = str(v.get("group", "") or "").strip().lower()
            is_office = (prof == "office") or ("ufficio" in grp)

            # Logica ibrida: se la finestra e marcata come company_buffer, la sede NON prova
            # a riportare il mezzo al SOC pieno operativo. Copre solo:
            # 1) energia minima per il giro successivo, se presente;
            # 2) quota buffer aziendale configurata per il gruppo/scenario.
            if bool(hybrid_private_home_charging) and str(charge_reason) == "company_buffer":
                buffer_target = float(v.get("company_buffer_target_kwh", 0.0) or 0.0)
                buffer_done = float(v.get("company_buffer_charged_kwh", 0.0) or 0.0)
                buffer_remaining = max(0.0, buffer_target - buffer_done)
                target_kwh = base_target
                if buffer_remaining > 1e-9:
                    target_kwh = max(target_kwh, min(batt * float(soc_max), soc_kwh + buffer_remaining))
            elif is_office or (float(e_next) > 1e-9):
                target_kwh = min(batt * float(soc_start), batt * float(soc_max))
                # mai peggio del base target
                target_kwh = max(target_kwh, base_target)
            else:
                target_kwh = base_target

        target_kwh = max(target_kwh, soc_kwh)

        e_need = max(0.0, target_kwh - soc_kwh)
        if e_need <= 1e-6:
            return

        s_eff = float(s) + delay_h
        f_eff = float(f)  # stress delay riduce la finestra
        if f_eff <= s_eff + 1e-6:
            return

        # scelta stazione (earliest + preferenze realistiche)
        best_s = None
        best_key = (1e18, 1e18)
        window_h = max(0.0, float(f_eff) - float(s_eff))

        for stn in stations:
            avail = stn["busy"]
            act = max(avail, s_eff)

            day_idx = int(act // 24)
            v_count = int(stn["v_count_day"].get(day_idx, 0))
            if "AC" in stn["type"] and v_count >= max_ac_v:
                continue
            if "DC" in stn["type"] and v_count >= max_dc_v:
                continue

            allow_after = bool(allow_night_after_shift) and (str(kind) == "night")
            # Se non e' consentito iniziare nuove connessioni dopo fine turno, confronta solo l'ora del giorno (mod 24).
            tod = float(act) % 24.0
            if (not allow_after) and (tod > float(limit_h) + 1e-9):
                continue
            # Attesa massima distinta per: top-up tra giri vs rientro serale/notte.
            # - Tra giri (day + e_next>0): il driver può attendere fino a X minuti per una DC.
            # - Notte: tipicamente 0 (nessuna manovra/attesa staff).
            if str(kind) == "day" and (float(e_next) > 1e-9 or str(charge_reason) == "company_buffer"):
                max_wait_h = float(max_wait_topup_min) / 60.0
                # Attesa consentita solo per DC/FAST. Per AC: o è libera subito o si salta.
                if "AC" in stn["type"]:
                    max_wait_h = 0.0
            else:
                max_wait_h = float(max_wait_night_min) / 60.0

            if max_wait_h <= 1e-9:
                # serve presa libera ESATTAMENTE al rientro
                if float(avail) > float(s_eff) + 1e-9:
                    continue
            else:
                if (float(act) - float(s_eff)) > (max_wait_h + 1e-9):
                    continue

            if act >= f_eff:
                continue

            # Preferenze:
            # - STOP breve tra giri (day + e_next>0 + finestra corta): preferisci DC
            # - NOTTE o soste lunghe: preferisci AC
            pref = 1
            if (str(kind) == "day" and float(e_next) > 1e-9 and window_h <= 1.5) or (str(charge_reason) == "company_buffer" and window_h <= 2.0):
                # FAST/DC anche al rientro dell unico giro se il buffer aziendale va recuperato in poco tempo.
                pref = 0 if "DC" in stn["type"] else 1
            else:
                pref = 0 if "AC" in stn["type"] else 1

            key = (float(act), float(pref))
            if key < best_key:
                best_key = key
                best_s = stn
                t_start_best = float(act)


        sess = {
            "nome": f"{vid}_{kind}_{len([x for x in sessions if x.get('vid') == vid]) + 1}",
            "vid": vid,
            "group": v.get("group"),
            "profilo": v.get("profilo"),
            "batt": batt,
            "cons": float(v.get("cons", 0.22)),
            "s": s_eff,
            "f": f_eff,
            "kind": kind,
            "soc_before": soc_kwh,
            "soc_target": target_kwh,
            "e_req": e_need,
            "caricato": 0.0,
            "wait_h": None,
            "log_p": None,
        }

        if best_s is None:
            sessions.append(sess)
            return

        p_nominale = min(best_s["p"], v.get("potenza_max_ac_kw", 11.0)) if "AC" in best_s["type"] else best_s["p"]
        # Se DC a spunto: posticipa l'inizio al primo slot con abbastanza potenza disponibile.
        if engine_cfg.dc_fixed_power and ("DC" in best_s["type"]):
            t_scan = t_start_best
            t_end_scan = min(f_eff, max_h)
            while t_scan < t_end_scan:
                sidx = int(t_scan * 4)
                if sidx >= n_slots:
                    break
                if (p_shave_limit - power_timeline[sidx]) >= (p_nominale - 1e-9):
                    break
                t_scan += 0.25
            t_start_best = t_scan

        # aggiorna attesa/coda usando l'orario di inizio effettivo
        sess["wait_h"] = max(0.0, t_start_best - s_eff)
        wait_list_h.append(sess["wait_h"])

        a0 = int(max(0, s_eff * 4))
        a1 = int(max(0, t_start_best * 4))
        a0 = min(a0, n_slots - 1)
        a1 = min(a1, n_slots)
        if a1 > a0:
            queue_timeline[a0:a1] += 1
        # non superare capacità residua batteria
        e_cap = max(0.0, batt * float(soc_max) - soc_kwh)
        max_can_charge = min(e_need, e_cap)
        current_t = t_start_best
        carica_acc = 0.0

        t_end = min(f_eff, max_h)
        while current_t < t_end and carica_acc < max_can_charge:
            slot_idx = int(current_t * 4)
            if slot_idx >= n_slots:
                break
            p_available_shaving = max(0.0, p_shave_limit - power_timeline[slot_idx])
            if engine_cfg.dc_fixed_power and ("DC" in best_s["type"]):
                p_eff = p_nominale if p_available_shaving >= (p_nominale - 1e-9) else 0.0
            else:
                p_eff = min(p_nominale, p_available_shaving)
            if p_eff > 0:
                energia_slot = p_eff * 0.25
                if carica_acc + energia_slot > max_can_charge:
                    energia_slot = max_can_charge - carica_acc
                    p_eff = energia_slot / 0.25
                power_timeline[slot_idx] += p_eff
                carica_acc += energia_slot
            current_t += 0.25

        sess["caricato"] = float(carica_acc)

        # Se di notte non raggiungo il target (SOC_start), considero il residuo come energia esterna necessaria prima del giorno dopo.
        if str(kind) == "night":
            rem = max(0.0, float(max_can_charge) - float(carica_acc))
            if rem > 1e-6:
                v["overnight_kwh"] += rem
                v["ext_kwh"] += rem
                v["ext_events"] += 1
                e_ext_total += rem
        sess["log_p"] = {"st": best_s["nome"], "i": t_start_best, "ec": current_t}

        # update station
        if staff_no_rotation_night and (str(kind) == "night"):
            # occupa la presa fino al mattino (fine finestra) anche se finisce prima
            best_s["busy"] = max(current_t, f_eff) + 0.1
        else:
            best_s["busy"] = current_t + 0.1
        day_idx = int(t_start_best // 24)
        best_s["v_count_day"][day_idx] = int(best_s["v_count_day"].get(day_idx, 0)) + 1

        # update vehicle SOC
        v["soc"] = soc_kwh + carica_acc
        v["depot_kwh"] += carica_acc
        if str(charge_reason) == "company_buffer":
            v["company_buffer_charged_kwh"] = float(v.get("company_buffer_charged_kwh", 0.0) or 0.0) + float(carica_acc)
        e_depot_total += carica_acc

        sess["soc_after"] = float(v["soc"])
        sessions.append(sess)

    # Esegui timeline
    def _ev_key(ev):
        s = float(ev.get("s", 0.0) or 0.0)
        if ev.get("type") == "drive":
            return (s, 0, 0)
        kind = str(ev.get("kind", "day"))
        e_next = float(ev.get("e_next", 0.0) or 0.0)
        day_idx = int(s // 24)
        # Fairness: nelle finestre di top-up tra giri (day + e_next>0) ruota l'ordine per giorno
        # così in multi-day non vincono sempre gli stessi.
        if kind == "day" and e_next > 1e-9:
            vid = str(ev.get("vid", ""))
            h = (zlib.crc32(vid.encode("utf-8")) + (day_idx * 2654435761)) & 0xFFFFFFFF
            return (s, 1, int(h))
        return (s, 1, 0)

    for ev in sorted(events, key=_ev_key):
        vid = ev.get("vid")
        if vid not in v_state:
            continue
        v = v_state[vid]
        if ev.get("type") == "drive":
            e_drive = float(ev.get("e_drive", 0.0)) * extra_mul
            batt = float(v["batt"])

            day_idx = int(float(ev.get("s", 0.0)) // 24)
            key = (vid, day_idx)
            if not first_departure_checked.get(key, False):
                # In logica ibrida, prima della prima partenza del giorno il veicolo puo essere stato
                # ricaricato da wallbox privata domestica, senza occupare prese aziendali.
                if bool(hybrid_private_home_charging) and bool(v.get("can_home_night", True)):
                    target_home = batt * (soc_policy.soc_start_pct / 100.0)
                    home_add = max(0.0, target_home - float(v["soc"]))
                    if home_add > 1e-6:
                        v["soc"] += home_add
                        v["home_private_kwh"] = float(v.get("home_private_kwh", 0.0) or 0.0) + home_add
                        e_home_private_total_ref[0] += home_add
                # registra SOC alla prima partenza del giorno (prima di consumare il giro)
                soc_morning_rows.append({
                    "vid": vid,
                    "day": int(day_idx) + 1,
                    "soc_kwh": float(v["soc"]),
                    "batt": float(batt),
                    "group": v.get("group"),
                })
                target_kwh = batt * (soc_policy.soc_start_pct / 100.0)
                shortfall = max(0.0, target_kwh - float(v["soc"]))
                if shortfall > 1e-6:
                    morning_shortfall_total += shortfall
                    morning_not_full_days += 1
                first_departure_checked[key] = True
            reserve = batt * (soc_policy.soc_min_pct / 100.0)
            need_before = reserve + e_drive
            if float(v["soc"]) + 1e-9 < need_before:
                ext = need_before - float(v["soc"])
                v["ext_kwh"] += ext
                v["ext_events"] += 1
                v["soc"] += ext
                e_ext_total += ext
            v["soc"] -= e_drive
            v["drive_kwh"] += e_drive
            e_drive_total += e_drive
        else:
            _serve_charge_window(
                vid=vid,
                kind=str(ev.get("kind", "day")),
                s=float(ev.get("s", 0.0)),
                f=float(ev.get("f", 0.0)),
                e_next=float(ev.get("e_next", 0.0)),
                soc_min=float(ev.get("soc_min", soc_policy.soc_min_pct / 100.0)),
                soc_max=float(ev.get("soc_max", soc_policy.soc_max_pct / 100.0)),
                soc_buffer=float(ev.get("soc_buffer", soc_policy.soc_buffer_pct / 100.0)),
                soc_start=float(ev.get("soc_start", soc_policy.soc_start_pct / 100.0)),
                charge_reason=str(ev.get("charge_reason", "")),
            )

    # Riassunto veicoli
    veh_rows = []
    for vid, v in v_state.items():
        veh_rows.append({
            "nome": v.get("nome", vid),
            "group": v.get("group"),
            "profilo": v.get("profilo"),
            "batt": float(v.get("batt", 0.0)),
            "cons": float(v.get("cons", 0.22)),
            "SOC start (kWh)": float(v.get("soc_start", 0.0)),
            "SOC end (kWh)": float(v.get("soc", 0.0)),
            "Drive (kWh)": float(v.get("drive_kwh", 0.0)),
            "Depot (kWh)": float(v.get("depot_kwh", 0.0)),
            "Esterno (kWh)": float(v.get("ext_kwh", 0.0)),
            "Ext notte (kWh)": float(v.get("overnight_kwh", 0.0)),
            "Privata casa (kWh)": float(v.get("home_private_kwh", 0.0)),
            "Policy notte": str(v.get("night_charging_mode", "")),
            "Buffer azienda target (kWh)": float(v.get("company_buffer_target_kwh", 0.0)),
            "Buffer azienda servito (kWh)": float(v.get("company_buffer_charged_kwh", 0.0)),
            "#Ext": int(v.get("ext_events", 0)),
            "Trips": int(v.get("trips", 0)),
        })

    # KPI
    e_tot = e_drive_total
    e_int = e_depot_total
    e_ext = max(0.0, e_ext_total)
    e_home_private = max(0.0, float(e_home_private_total_ref[0]))

    c_cap = sum((hw_params[t]["acq"] + hw_params[t]["ins"]) * q for t, q in config.items())
    c_mnt = sum(hw_params[t]["mnt"] * q for t, q in config.items())

    # --- Annualizzazione corretta (SOC multi-day) ---
    # In SOC multi-day e_tot/e_int/e_ext sono SUM sui giorni simulati.
    # Normalizziamo a kWh/giorno prima di moltiplicare per 365.
    try:
        sim_days_int = int(sim_days)
    except Exception:
        sim_days_int = 1
    sim_days_int = max(1, sim_days_int)

    e_tot_day = e_tot / sim_days_int
    e_int_day = e_int / sim_days_int
    e_ext_day = e_ext / sim_days_int
    e_home_private_day = e_home_private / sim_days_int

    risp_diesel = (fleet_km_day_total / fuel.km_l * fuel.e_l) * 365
    staff_cost = (e_ext_day / 30 * fuel.h_rate) * 365
    pena_en = (e_ext_day * (costi["pub"] - costi["pri"])) * 365
    # Validazione economica: se una combinazione carica meno energia totale, non deve apparire
    # piu' conveniente solo perche' lascia domanda scoperta. La domanda di riferimento e' la
    # missione giornaliera simulata (e_tot_day). In logica ibrida il gap programmato non e
    # ricarica pubblica: e ricarica domestica privata. La pubblica resta solo emergenza/shortfall.
    demand_ref_day = max(float(e_tot_day), float(e_int_day + e_ext_day + e_home_private_day))
    if bool(hybrid_private_home_charging):
        e_home_private_effective_day = max(float(e_home_private_day), float(demand_ref_day - e_int_day - e_ext_day))
        e_ext_effective_day = max(0.0, float(e_ext_day))
        unserved_energy_day = max(0.0, float(demand_ref_day - (e_int_day + e_ext_day + e_home_private_effective_day)))
    else:
        e_home_private_effective_day = float(e_home_private_day)
        e_ext_effective_day = max(float(e_ext_day), float(demand_ref_day - e_int_day))
        unserved_energy_day = max(0.0, float(demand_ref_day - (e_int_day + e_ext_day)))

    staff_cost = (e_ext_day / 30 * fuel.h_rate) * 365
    pena_en = (e_ext_effective_day * (costi["pub"] - costi["pri"])) * 365
    costo_ev_teorico = (e_int_day * costi["pri"] + e_home_private_effective_day * costi["pri"] + e_ext_day * costi["pub"]) * 365 + staff_cost + c_mnt
    staff_cost_effective = (e_ext_effective_day / 30 * fuel.h_rate) * 365
    costo_ev = (e_int_day * costi["pri"] + e_home_private_effective_day * costi["pri"] + e_ext_effective_day * costi["pub"]) * 365 + staff_cost_effective + c_mnt
    co2 = (fleet_km_day_total / fuel.km_l * 2.65 * 365) / 1000

    waits = [w for w in wait_list_h if w is not None]
    wait_avg_min = float(np.mean(waits) * 60) if waits else 0.0
    wait_p95_min = float(np.percentile(np.array(waits) * 60, 95)) if len(waits) >= 1 else 0.0

    # KPI veicoli: distinguiamo tra "ha usato almeno una colonnina" e "riesce a completare tutto in deposito"
    veh_total = int(len(veh_rows))
    veh_served = int(sum(1 for v in veh_rows if float(v.get("Depot (kWh)", 0.0)) > 0.0))
    veh_unserved = int(sum(1 for v in veh_rows if float(v.get("Depot (kWh)", 0.0)) <= 0.0 and float(v.get("Drive (kWh)", 0.0)) > 0.0))
    veh_served_pct = round((veh_served / veh_total) * 100, 1) if veh_total else 0.0
    veh_chargeable = 0
    veh_not_chargeable = 0
    for v in veh_rows:
        drive_kwh = float(v.get("Drive (kWh)", 0.0))
        ext_kwh = float(v.get("Esterno (kWh)", 0.0))
        if drive_kwh <= 0.0:
            v["Riesce a caricare in deposito"] = "N/A"
            v["Esito ricarica"] = "Nessun fabbisogno"
        elif ext_kwh <= 1e-9:
            veh_chargeable += 1
            v["Riesce a caricare in deposito"] = "Si"
            v["Esito ricarica"] = "Caricato in deposito"
        else:
            veh_not_chargeable += 1
            v["Riesce a caricare in deposito"] = "No"
            v["Esito ricarica"] = "Richiede ricarica esterna"
    veh_chargeable_pct = round((veh_chargeable / veh_total) * 100, 1) if veh_total else 0.0

    sessions_ok = [s for s in sessions if float(s.get("caricato", 0.0)) > 0.0]
    sessions_ac = [s for s in sessions_ok if "AC" in str((s.get("log_p") or {}).get("st", ""))]
    sessions_dc = [s for s in sessions_ok if "DC" in str((s.get("log_p") or {}).get("st", ""))]
    served_by_station = {}
    for s in sessions_ok:
        st_name = str((s.get("log_p") or {}).get("st", "N/A"))
        served_by_station[st_name] = served_by_station.get(st_name, 0) + 1
    energy_need_recorded = e_int + e_ext
    perc_recorded = round((e_int / energy_need_recorded) * 100, 1) if energy_need_recorded > 0 else 100.0
    energy_need = demand_ref_day * sim_days_int
    company_buffer_target_day = max(0.0, float(demand_ref_day) * float(company_buffer_pct)) if bool(hybrid_private_home_charging) else float(demand_ref_day)
    company_buffer_gap_day = max(0.0, company_buffer_target_day - float(e_int_day))
    perc = round((e_int_day / demand_ref_day) * 100, 1) if demand_ref_day > 0 else 100.0
    company_buffer_pct_served = round((e_int_day / company_buffer_target_day) * 100, 1) if company_buffer_target_day > 0 else 100.0

    return {
        "config": config,
        "veicoli": veh_rows,
        "sessions": sessions,
        "capacity": {
            "sessions_total": int(len(sessions_ok)),
            "sessions_ac": int(len(sessions_ac)),
            "sessions_dc": int(len(sessions_dc)),
            "served_by_station": served_by_station,
        },
        "timeline_p": power_timeline,
        "timeline_q": queue_timeline,
        "soc_morning": soc_morning_rows,
        "kpi": {
            "perc": perc,
            "wait_avg_min": wait_avg_min,
            "wait_p95_min": wait_p95_min,
            "queue_max": float(np.max(queue_timeline)),
            "c_cap": c_cap,
            "risp": risp_diesel - costo_ev,
            "risp_teorico": risp_diesel - costo_ev_teorico,
            "mnt": c_mnt,
            "pena_en": pena_en,
            "staff_ext": staff_cost_effective,
            "staff_ext_recorded": staff_cost,
            "co2": co2,
            "trees": int(co2 * 50),
            "sim_days": int(sim_days_int),
            "e_int": e_int_day,
            "e_ext": e_ext_effective_day,
            "e_home_private": e_home_private_effective_day,
            "company_buffer_target_kwh": company_buffer_target_day,
            "company_buffer_gap_kwh": company_buffer_gap_day,
            "company_buffer_served_pct": company_buffer_pct_served,
            "hybrid_private_home_charging": bool(hybrid_private_home_charging),
            "e_ext_recorded": e_ext_day,
            "e_unserved": unserved_energy_day,
            "e_tot": e_tot_day,
            "e_need": demand_ref_day,
            "perc_recorded": float(perc_recorded),
            "coverage_formula": "e_int / max(e_tot, e_int + e_ext)",
            "veh_total": int(veh_total),
            "veh_served": int(veh_served),
            "veh_unserved": int(veh_unserved),
            "veh_served_pct": float(veh_served_pct),
            "veh_chargeable": int(veh_chargeable),
            "veh_not_chargeable": int(veh_not_chargeable),
            "veh_chargeable_pct": float(veh_chargeable_pct),
            "sessions_total": int(len(sessions_ok)),
            "sessions_ac": int(len(sessions_ac)),
            "sessions_dc": int(len(sessions_dc)),
            "ext_events": int(sum(v.get("#Ext", 0) for v in veh_rows)),
            "overnight_shortfall_kwh": float(sum(v.get("Ext notte (kWh)", 0.0) for v in veh_rows)),
            "morning_shortfall_kwh": float(morning_shortfall_total),
            "morning_not_full_days": int(morning_not_full_days),
        },
    }
