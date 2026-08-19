"""
Business Report — replica esatta dei KPI del Business Advisor di Streamlit.
Calcola tutti i KPI operativi, finanziari e ESG per la configurazione selezionata
e le alternative, restituendo i dati pronti per il frontend.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import math


def compute_business_report(
    # KPI dalla simulazione base
    kpi: dict,
    # KPI dalla simulazione stress
    kpi_stress: dict,
    # Configurazione hardware selezionata
    config: dict,
    # Timeline potenza (lista kW per slot 15min)
    timeline_p: list,
    # Sessioni di carica (per Gantt)
    sessions: list,
    # Soluzioni alternative (lista di dict con config+kpi)
    soluzioni: list,
    # Parametri flotta
    fleet_nv: int,
    fleet_km_day_total: float,
    fleet_cons_avg: float,
    # Parametri economici
    budget_max: float,
    p_shaving: float,
    c_pri_medio: float = 0.25,   # €/kWh energia interna (media rete+FV)
    c_pub: float = 0.65,          # €/kWh energia pubblica
    km_l: float = 11.0,           # km/L diesel
    e_l: float = 1.85,            # €/L diesel
    c_mnt_die: float = 0.08,      # €/km manutenzione diesel
    c_mnt_ev: float = 0.03,       # €/km manutenzione EV
    c_acq_die: float = 25000.0,   # €/veicolo acquisto diesel
    c_acq_ev: float = 35000.0,    # €/veicolo acquisto EV
    tco_period: int = 36,         # mesi orizzonte TCO
    fin_horizon_years: int = 10,  # anni orizzonte finanziario
    fin_discount_rate: float = 0.05,
) -> dict:
    """
    Calcola tutti i KPI del Business Report.
    Replica esatta della logica Streamlit (main_it.py righe 3424-3660).
    """
    k, s = kpi, kpi_stress

    # --- KPI operativi base ---
    rb_p_max = max(timeline_p) if timeline_p else 0.0
    rb_peak_ratio = (rb_p_max / p_shaving * 100.0) if p_shaving else 0.0
    rb_budget_ratio = (float(k.get("c_cap", 0.0)) / budget_max * 100.0) if budget_max else 0.0
    rb_public_share = max(0.0, 100.0 - float(k.get("perc", 0.0)))
    rb_wait_p95 = float(k.get("wait_p95_min", 0.0))
    rb_queue_max = float(k.get("queue_max", 0.0))
    rb_ext_events = int(k.get("ext_events", 0))
    rb_served_pct = float(k.get("veh_served_pct", float(k.get("veh_served", 0)) / max(1, float(k.get("veh_total", 1))) * 100))
    rb_served_n = int(k.get("veh_served", 0))
    rb_total_n = int(k.get("veh_total", 0))
    rb_coverage_pct = float(k.get("perc", 0.0))
    rb_morning_short = float(s.get("morning_shortfall_kwh", k.get("morning_shortfall_kwh", 0.0)))
    rb_not_full = int(s.get("morning_not_full_days", k.get("morning_not_full_days", 0)))
    rb_e_int = float(k.get("e_int", 0.0))
    rb_e_ext = float(k.get("e_ext", 0.0))
    rb_e_home = float(k.get("e_home_private", 0.0))
    rb_e_need = float(k.get("e_need", rb_e_int + rb_e_ext))

    # Stress
    rb_coverage_stress = float(s.get("perc", rb_coverage_pct))
    rb_e_ext_stress = float(s.get("e_ext", rb_e_ext))

    # --- KPI finanziari (replica esatta Streamlit) ---
    rb_tot_km_annui = float(fleet_km_day_total) * 365.0
    rb_tot_kwh_annui = rb_tot_km_annui * float(fleet_cons_avg)

    # Energia
    rb_energy_mix_y = (rb_e_int * c_pri_medio + rb_e_ext * c_pub) * 365.0
    rb_diesel_fuel_y = (rb_tot_km_annui / km_l) * e_l
    rb_veh_mnt_diesel_y = c_mnt_die * fleet_nv * rb_tot_km_annui / fleet_nv if fleet_nv > 0 else 0
    rb_veh_mnt_diesel_y = (rb_tot_km_annui / fleet_nv * c_mnt_die * fleet_nv) if fleet_nv > 0 else 0
    rb_veh_mnt_ev_y = (rb_tot_km_annui / fleet_nv * c_mnt_ev * fleet_nv) if fleet_nv > 0 else 0
    rb_infra_om_y = float(k.get("mnt", 0.0))
    
    # Costo wallbox domestiche: se pct_casa > 0, aggiunge €1.300/wallbox al CAPEX
    # La wallbox 7.4kW è hardware del dipendente ma Y35 la installa e fattura
    pct_casa_num = float(k.get("e_home_private", 0)) / max(float(k.get("e_need", 1)), 1)
    n_wallbox_casa = round(fleet_nv * pct_casa_num)
    costo_wallbox_casa = n_wallbox_casa * 1300.0  # €1.300/wallbox 7.4kW
    rb_staff_ext_y = float(k.get("staff_ext", 0.0))

    rb_ops_diesel_y = rb_diesel_fuel_y + rb_veh_mnt_diesel_y
    rb_ops_ev_y = rb_energy_mix_y + rb_staff_ext_y + rb_infra_om_y + rb_veh_mnt_ev_y
    rb_ops_saving_y = rb_ops_diesel_y - rb_ops_ev_y
    rb_simple_payback_y = (float(k.get("c_cap", 0.0)) / rb_ops_saving_y) if rb_ops_saving_y > 0 else None

    # Costi energia
    rb_costo_elettrico_privato = rb_tot_kwh_annui * c_pri_medio
    rb_costo_elettrico_pubblico = rb_tot_kwh_annui * c_pub
    rb_costo_kwh_mix = (rb_coverage_pct / 100.0 * c_pri_medio) + ((1.0 - rb_coverage_pct / 100.0) * c_pub)

    # TCO
    rb_tco_diesel_tot = (c_acq_die * fleet_nv * tco_period) + (rb_ops_diesel_y * tco_period / 12.0)
    rb_tco_ev_tot = float(k.get("c_cap", 0.0)) + (c_acq_ev * fleet_nv * tco_period) + (rb_ops_ev_y * tco_period / 12.0)
    rb_risparmio_tco = rb_tco_diesel_tot - rb_tco_ev_tot

    # Cashflow e NPV
    capex_delta0 = float(k.get("c_cap", 0.0)) + (c_acq_ev - c_acq_die) * fleet_nv
    cfs = [-capex_delta0] + [rb_ops_saving_y] * fin_horizon_years
    npv_val = sum(cfs[i] / ((1 + fin_discount_rate) ** i) for i in range(len(cfs)))
    payback_y = next((i for i, v in enumerate(
        [sum(cfs[:i+1]) for i in range(len(cfs))]) if v >= 0), None)
    roi_net = ((sum(cfs[1:]) - abs(cfs[0])) / abs(cfs[0]) * 100.0) if cfs[0] != 0 else 0.0
    bcr = (sum(max(0, x) for x in cfs[1:]) / abs(cfs[0])) if cfs[0] != 0 else 0.0
    cashflow_cumulativo = [sum(cfs[:i+1]) for i in range(len(cfs))]

    # Grafico TCO per km — mostra solo costi operativi (carburante/energia + manutenzione)
    # NON include costo acquisto veicoli che distorce il grafico
    # L'asse Y è costo totale operativo su tco_period mesi
    km_range = list(range(2500, 60001, 2500))
    capex_infra = float(k.get("c_cap", 0.0))
    tco_diesel_plot = [
        (c_mnt_die * km * tco_period / 12) + ((km / km_l * e_l) * tco_period / 12)
        for km in km_range
    ]
    tco_ev_plot = [
        capex_infra + (c_mnt_ev * km * tco_period / 12) + ((km * fleet_cons_avg * rb_costo_kwh_mix) * tco_period / 12)
        for km in km_range
    ]
    # Punto di incrocio
    km_breakeven_tco = None
    for i in range(len(km_range)-1):
        if (tco_diesel_plot[i] - tco_ev_plot[i]) * (tco_diesel_plot[i+1] - tco_ev_plot[i+1]) <= 0:
            km_breakeven_tco = km_range[i]
            break

    # ESG
    co2_saved = float(k.get("co2", rb_tot_km_annui * (2.64 / km_l / 1000.0)))  # t/anno
    trees = int(co2_saved / 0.02)
    diesel_evitato_l = rb_tot_km_annui / km_l
    esg_rating = "AAA" if rb_coverage_pct > 90 else ("A" if rb_coverage_pct > 70 else "B")

    # --- Decisione GO/REVIEW (replica esatta Streamlit) ---
    rb_decision = "GO"
    rb_reasoning = []
    rb_actions = []

    if rb_coverage_pct < 85 or rb_public_share > 15:
        rb_decision = "REVIEW"
        rb_reasoning.append("copertura della domanda di ricarica ancora esposta a ricarica pubblica")
    if rb_peak_ratio >= 95:
        if rb_decision == "GO":
            rb_decision = "GO CON ATTENZIONE"
        rb_reasoning.append("peak shaving quasi saturo")
    if rb_wait_p95 > 20 or rb_queue_max >= 2:
        if rb_decision == "GO":
            rb_decision = "GO CON ATTENZIONE"
        rb_reasoning.append("attese operative da monitorare")
    if rb_ops_saving_y <= 0:
        rb_decision = "REVIEW"
        rb_reasoning.append("saving operativo annuo non positivo")
    if not rb_reasoning:
        rb_reasoning.append("scenario equilibrato su servizio, energia e ritorno economico")

    if rb_public_share > 10:
        rb_actions.append("ridurre quota pubblica aumentando ore utili o potenza disponibile")
    if rb_peak_ratio >= 95:
        rb_actions.append("rivedere peak shaving o distribuire meglio i rientri")
    if rb_wait_p95 > 20 or rb_queue_max >= 2:
        rb_actions.append("agire su simultaneità o numero prese nelle finestre critiche")
    if rb_not_full > 0:
        rb_actions.append("proteggere le partenze del mattino con più margine notturno")
    if not rb_actions:
        rb_actions.append("mantenere questa configurazione come baseline di budget e rollout")

    # Status per semaforo
    def status(val, good, warn, reverse=False):
        if reverse:
            if val <= good: return "OK"
            if val <= warn: return "ATTENZIONE"
            return "REVIEW"
        if val >= good: return "OK"
        if val >= warn: return "ATTENZIONE"
        return "REVIEW"

    coverage_status = status(rb_coverage_pct, 95, 85)
    operations_status = "OK" if (rb_wait_p95 <= 10 and rb_queue_max < 1) else ("ATTENZIONE" if rb_wait_p95 <= 25 else "REVIEW")
    finance_status = "OK" if rb_ops_saving_y > 0 and rb_simple_payback_y and rb_simple_payback_y <= 6 else ("ATTENZIONE" if rb_ops_saving_y > 0 else "REVIEW")
    resilience_status = "OK" if rb_coverage_stress >= 90 and rb_not_full == 0 else ("ATTENZIONE" if rb_coverage_stress >= 75 else "REVIEW")
    esg_status = status(co2_saved, 5.0, 1.0)

    # Confronto soluzioni alternative
    alt_rows = []
    for i, sol in enumerate(soluzioni or [], start=1):
        sk = sol.get("kpi", {})
        alt_rows.append({
            "soluzione": f"S{i}",
            "config": str(sol.get("config", {})),
            "copertura_pct": float(sk.get("perc", sk.get("copertura_reale_pct", 0.0))),
            "e_ext_kwh_g": float(sk.get("e_ext", 0.0)),
            "e_unserved_kwh_g": float(sk.get("e_unserved", 0.0)),
            "capex_eur": float(sk.get("c_cap", 0.0)),
            "risparmio_annuo_eur": float(sk.get("risp", 0.0)),
        })

    # Sessioni per Gantt (formato identico a Streamlit)
    gantt_sessions = []
    for sess in (sessions or []):
        lp = sess.get("log_p") or {}
        if not lp.get("st"):
            continue
        gantt_sessions.append({
            "stazione": lp.get("st"),
            "inizio_h": float(lp.get("i", 0.0)),
            "fine_h": float(lp.get("ec", 0.0)),
            "veicolo": sess.get("vid", "EV"),
            "energia_kwh": float(sess.get("caricato", 0.0)),
        })

    # Reconciliation OPEX
    reconciliation = [
        {"voce": "Carburante Diesel (anno)", "diesel": round(rb_diesel_fuel_y, 0), "ev": 0, "delta": round(rb_diesel_fuel_y, 0)},
        {"voce": "Energia EV mix interno+pubblico (anno)", "diesel": 0, "ev": round(rb_energy_mix_y, 0), "delta": round(-rb_energy_mix_y, 0)},
        {"voce": "Manutenzione veicoli (anno)", "diesel": round(rb_veh_mnt_diesel_y, 0), "ev": round(rb_veh_mnt_ev_y, 0), "delta": round(rb_veh_mnt_diesel_y - rb_veh_mnt_ev_y, 0)},
        {"voce": "O&M infrastruttura (anno)", "diesel": 0, "ev": round(rb_infra_om_y, 0), "delta": round(-rb_infra_om_y, 0)},
        {"voce": "Staff esterno ricarica (anno)", "diesel": 0, "ev": round(rb_staff_ext_y, 0), "delta": round(-rb_staff_ext_y, 0)},
        {"voce": "TOTALE OPEX (anno)", "diesel": round(rb_ops_diesel_y, 0), "ev": round(rb_ops_ev_y, 0), "delta": round(rb_ops_saving_y, 0)},
    ]

    return {
        # Decisione
        "decisione": rb_decision,
        "reasoning": rb_reasoning,
        "azioni": rb_actions,
        "status": {
            "copertura": coverage_status,
            "operations": operations_status,
            "finanza": finance_status,
            "resilienza": resilience_status,
            "esg": esg_status,
        },

        # KPI operativi
        "copertura_pct": round(rb_coverage_pct, 1),
        "copertura_stress_pct": round(rb_coverage_stress, 1),
        "picco_kw": round(rb_p_max, 1),
        "peak_ratio_pct": round(rb_peak_ratio, 1),
        "budget_ratio_pct": round(rb_budget_ratio, 1),
        "quota_pubblica_pct": round(rb_public_share, 1),
        "attesa_p95_min": round(rb_wait_p95, 1),
        "coda_max": round(rb_queue_max, 1),
        "eventi_esterni": rb_ext_events,
        "veicoli_serviti_pct": round(rb_served_pct, 1),
        "veicoli_serviti_n": rb_served_n,
        "veicoli_totali_n": rb_total_n,
        "partenze_non_al_soc": rb_not_full,
        "shortfall_mattino_kwh": round(rb_morning_short, 1),

        # Energia
        "e_int_kwh_g": round(rb_e_int, 1),
        "e_ext_kwh_g": round(rb_e_ext, 1),
        "e_home_kwh_g": round(rb_e_home, 1),
        "e_need_kwh_g": round(rb_e_need, 1),
        "e_int_stress_kwh_g": round(float(s.get("e_int", rb_e_int)), 1),
        "e_ext_stress_kwh_g": round(rb_e_ext_stress, 1),

        # Finanziari
        "capex_eur": float(k.get("c_cap", 0.0)),
        "capex_wallbox_casa_eur": costo_wallbox_casa,
        "n_wallbox_casa": n_wallbox_casa,
        "capex_totale_eur": float(k.get("c_cap", 0.0)) + costo_wallbox_casa,
        "ops_saving_annuo_eur": round(rb_ops_saving_y, 0),
        "ops_diesel_annuo_eur": round(rb_ops_diesel_y, 0),
        "ops_ev_annuo_eur": round(rb_ops_ev_y, 0),
        "simple_payback_anni": round(rb_simple_payback_y, 2) if rb_simple_payback_y else None,
        "npv_eur": round(npv_val, 0),
        "payback_anni": payback_y,
        "roi_netto_pct": round(roi_net, 1),
        "bcr": round(bcr, 2),
        "delta_tco_eur": round(rb_risparmio_tco, 0),
        "tco_diesel_eur": round(rb_tco_diesel_tot, 0),
        "tco_ev_eur": round(rb_tco_ev_tot, 0),
        "costo_kwh_mix": round(rb_costo_kwh_mix, 3),
        "costo_elettrico_privato_annuo": round(rb_costo_elettrico_privato, 0),
        "costo_elettrico_pubblico_annuo": round(rb_costo_elettrico_pubblico, 0),
        "cashflow": [round(x, 0) for x in cfs],
        "cashflow_cumulativo": [round(x, 0) for x in cashflow_cumulativo],
        "tco_periodo_mesi": tco_period,
        "fin_horizon_anni": fin_horizon_years,
        "reconciliation": reconciliation,

        # Grafico TCO km
        "tco_km_range": km_range,
        "tco_diesel_plot": [round(x, 0) for x in tco_diesel_plot],
        "tco_ev_plot": [round(x, 0) for x in tco_ev_plot],
        "km_breakeven_tco": km_breakeven_tco,
        "fleet_km_day_total": round(fleet_km_day_total, 0),

        # ESG
        "co2_saved_t_anno": round(co2_saved, 1),
        "trees_equivalent": trees,
        "diesel_evitato_l": round(diesel_evitato_l, 0),
        "esg_rating": esg_rating,

        # Confronto soluzioni
        "soluzioni_alternative": alt_rows,

        # Gantt sessioni
        "gantt_sessions": gantt_sessions,

        # Snapshot configurazione
        "snapshot": {
            "config": config,
            "fleet_nv": fleet_nv,
            "fleet_km_day_total": round(fleet_km_day_total, 0),
            "e_int_kwh_g": round(rb_e_int, 1),
            "e_ext_kwh_g": round(rb_e_ext, 1),
            "picco_kw": round(rb_p_max, 1),
            "picco_limite_kw": round(p_shaving, 1),
            "attesa_p95_min": round(rb_wait_p95, 1),
        },
    }
