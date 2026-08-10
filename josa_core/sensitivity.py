"""
Modulo sensitivity analysis per dimensionamento infrastruttura di ricarica.

Calcola, per una gamma di configurazioni hardware, le metriche operative
rilevanti per la decisione commerciale:
- Cambi fisici per colonnina al giorno (onere operativo)
- Attesa media per veicolo (ore dal rientro all'inizio della carica)
- Copertura percentuale
- Picco di potenza reale (kW)
- CAPEX e payback stimato

Integra anche la distribuzione settimanale dei km: non solo il giorno
medio, ma la variabilità lun-ven che determina i giorni critici.
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SensitivityRow:
    """Una riga della matrice: una configurazione hardware specifica."""
    config: dict
    label: str
    capex_eur: float
    copertura_pct: float
    picco_kw: float
    sessioni_totali: int
    cambi_per_colonnina: float
    attesa_media_h: float
    attesa_max_h: float
    giorni_critici_su_5: int
    tasso_utilizzo_pct: float
    # Scomposizione energetica: quanta energia viene DAVVERO dalle colonnine
    pct_energia_colonnine: float    # % del fabbisogno coperta dalle colonnine (non dalla casa)
    energia_colonnine_kwh: float    # kWh/giorno reali dalle colonnine
    energia_casa_kwh: float         # kWh/giorno assunti dalla casa (non verificati)
    energia_pubblica_kwh: float      # kWh/giorno da pubblica (non verificati)
    fabbisogno_totale_kwh: float     # kWh/giorno totali della flotta
    zona: str
    raccomandazione: str


def _zona(cambi: float, copertura: float, soglia_cambi: float) -> str:
    if copertura < 95.0:
        return "rossa"
    if cambi > soglia_cambi:
        return "arancione"
    return "verde"


def _label(config: dict) -> str:
    parti = []
    for nome, q in sorted(config.items()):
        if q > 0:
            p = nome.replace("kW", "kW").strip()
            parti.append(f"{q}×{p}")
    return " + ".join(parti)


def calcola_sensitivity(
    sim_fn,                          # callable(config: dict) -> SimResult
    catalogo: list[dict],            # [{nome, potenza_kw, costo_acq, costo_ins, ...}]
    n_veicoli: int,
    km_giornalieri: float,
    consumo_kwh_km: float,
    p_max_ac_kw: float,
    finestra_h: float,               # ore disponibili (es. 18-9 = 9h)
    p_shave_kw: float,
    soglia_cambi: int = 4,           # max cambi/col/giorno accettabili
    budget_max: float = 999999.0,
    # Distribuzione settimanale km (fattori rispetto al giorno medio)
    fattori_settimanali: Optional[list[float]] = None,
) -> list[SensitivityRow]:
    """
    Calcola la matrice di sensitivity per tutte le configurazioni
    ragionevoli tra 1 colonnina e n_veicoli colonnine.

    fattori_settimanali: [lun, mar, mer, gio, ven] — default [1.0,1.0,1.0,1.0,0.8]
    """
    if fattori_settimanali is None:
        fattori_settimanali = [1.0, 1.0, 1.0, 1.0, 0.8]

    e_per_veh_medio = km_giornalieri * consumo_kwh_km
    hw_map = {h["nome"]: h for h in catalogo}

    # Genera configurazioni candidate
    hw_ac = [h for h in catalogo if "AC" in h["nome"] and "DC" not in h["nome"]]
    hw_dc = [h for h in catalogo if "DC" in h["nome"]]

    if not hw_ac:
        return []

    ac = hw_ac[0]   # usa il primo AC disponibile
    dc = hw_dc[0] if hw_dc else None

    configs_da_testare = []

    # Colonnine AC pure: da minimo fisico a n_veicoli
    t_sess_ac = e_per_veh_medio / min(ac["potenza_kw"], p_shave_kw)  # ore/sessione
    n_min_ac = max(1, math.ceil(n_veicoli / max(1, finestra_h / t_sess_ac)))
    for n in range(max(1, n_min_ac - 1), min(n_veicoli + 1, n_min_ac * 4 + 2)):
        cfg = {ac["nome"]: n}
        capex = n * (ac["costo_acq"] + ac["costo_ins"])
        if capex <= budget_max:
            configs_da_testare.append(cfg)

    # AC + 1 DC, AC + 2 DC
    if dc:
        for n_dc in [1, 2]:
            t_sess_dc = e_per_veh_medio / min(dc["potenza_kw"], p_shave_kw)
            # Con il DC che serve i veicoli urgenti, serve meno AC
            for n_ac in range(max(1, n_min_ac - 1), min(n_veicoli, n_min_ac * 3)):
                cfg = {ac["nome"]: n_ac, dc["nome"]: n_dc}
                capex = (n_ac * (ac["costo_acq"] + ac["costo_ins"]) +
                         n_dc * (dc["costo_acq"] + dc["costo_ins"]))
                if capex <= budget_max:
                    configs_da_testare.append(cfg)

    # Deduplication
    seen = set()
    configs_unici = []
    for cfg in configs_da_testare:
        key = tuple(sorted(cfg.items()))
        if key not in seen:
            seen.add(key)
            configs_unici.append(cfg)

    rows = []
    for cfg in configs_unici:
        try:
            res = sim_fn(cfg)
        except Exception:
            continue

        kpi = res.get("kpi", {})
        gantt = res.get("gantt_veicoli", [])
        timeline_p = list(res.get("timeline_p_kw") or [])

        copertura = float(kpi.get("copertura_reale_pct", kpi.get("perc", 0.0)))
        capex = float(kpi.get("c_cap", 0.0))
        picco = float(max(timeline_p)) if timeline_p else 0.0

        # Conta sessioni reali dal Gantt (source of truth)
        az_segs_all = [s for v in gantt for s in v.get("segmenti", [])
                       if s.get("stato") == "carica_azienda"]
        sessioni = len(az_segs_all)

        # Raggruppa per colonnina per calcolare cambi per colonnina
        col_sessioni: dict = {}
        for s in az_segs_all:
            col = s.get("colonnina") or "?"
            col_sessioni[col] = col_sessioni.get(col, 0) + 1
        n_col_reali = max(1, len(col_sessioni)) if col_sessioni else max(1, sum(int(q) for q in cfg.values()))
        cambi = round(sessioni / n_col_reali, 1)

        # Attesa media: tempo dal t_avail_start del veicolo all'inizio della sessione
        attese = []
        for v in gantt:
            # Trova il rientro (fine dell'ultimo evento drive)
            drive_segs = [s for s in v.get("segmenti", []) if s.get("stato") == "lavoro"]
            az_segs = [s for s in v.get("segmenti", []) if s.get("stato") == "carica_azienda"]
            if drive_segs and az_segs:
                t_rientro = max(s["fine"] for s in drive_segs)
                t_inizio_carica = min(s["inizio"] for s in az_segs)
                attesa = max(0.0, t_inizio_carica - t_rientro)
                attese.append(attesa)
            elif drive_segs:
                # veicolo non caricato — attesa = tutta la finestra
                t_rientro = max(s["fine"] for s in drive_segs)
                attese.append(max(0.0, finestra_h - (t_rientro - 9.0)))  # appross.

        attesa_media = round(sum(attese) / len(attese), 2) if attese else 0.0
        attesa_max = round(max(attese), 2) if attese else 0.0

        # Tasso utilizzo colonnine (% tempo occupate)
        ore_sessioni = sum(
            (s["fine"] - s["inizio"])
            for v in gantt
            for s in v.get("segmenti", [])
            if s.get("stato") == "carica_azienda"
        )
        tasso_util = round((ore_sessioni / (n_col_reali * finestra_h)) * 100, 1) if finestra_h > 0 else 0.0

        # Giorni critici su 5: stima da varianza settimanale
        giorni_critici = 0
        for fattore in fattori_settimanali:
            if fattore > 1.0:
                # giorno più intenso: probabile che qualche veicolo resti scoperto
                # se la copertura normale è già < 100%
                if copertura < 100.0 or fattore > 1.2:
                    giorni_critici += 1

        # Zona operativa
        zona = _zona(cambi, copertura, soglia_cambi)

        # Raccomandazione
        if zona == "verde":
            rec = f"✓ Soluzione operativa — {cambi:.1f} cambi/col/giorno, attesa media {attesa_media*60:.0f} min"
        elif zona == "arancione":
            rec = f"⚠ Operativamente pesante — {cambi:.1f} cambi/col/giorno (soglia: {soglia_cambi}). Valuta +1 colonnina"
        else:
            rec = f"✗ Copertura insufficiente ({copertura:.0f}%) — aumenta colonnine o allarga finestra"

        # Scomposizione energetica dal KPI
        # Autosufficienza = solo colonnine aziendali, senza casa né pubblica
        e_int = float(kpi.get("e_int", 0.0))
        e_home = float(kpi.get("e_home_private", 0.0))
        e_ext = float(kpi.get("e_ext", 0.0))
        e_need = float(kpi.get("e_need", 0.0))
        e_tot = e_int + e_home + e_ext
        if e_tot == 0:
            e_tot = max(e_need, 0.01)
        pct_col = round(e_int / e_tot * 100, 1) if e_tot > 0 else 100.0

        rows.append(SensitivityRow(
            config=cfg,
            label=_label(cfg),
            capex_eur=capex,
            copertura_pct=copertura,
            picco_kw=picco,
            sessioni_totali=sessioni,
            cambi_per_colonnina=cambi,
            attesa_media_h=attesa_media,
            attesa_max_h=attesa_max,
            giorni_critici_su_5=giorni_critici,
            tasso_utilizzo_pct=tasso_util,
            pct_energia_colonnine=pct_col,
            energia_colonnine_kwh=round(e_int, 1),
            energia_casa_kwh=round(e_home, 1),
            energia_pubblica_kwh=round(e_ext, 1),
            fabbisogno_totale_kwh=round(e_need, 1),
            zona=zona,
            raccomandazione=rec,
        ))

    # Ordina: prima le verdi per CAPEX crescente, poi arancioni, poi rosse
    ordine = {"verde": 0, "arancione": 1, "rossa": 2}
    rows.sort(key=lambda r: (ordine[r.zona], r.capex_eur))
    return rows
