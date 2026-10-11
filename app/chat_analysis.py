"""
Analisi lanciata dal chatbot: stesso calcolo calibrato del percorso completo.

Per ogni sede del profilo conversazionale:
  1. ottimizzatore (CAPEX minimo, AC-first) sugli stessi gruppi PHEV/BEV del percorso completo;
  2. verifica settimanale con tolleranza tra veicoli: si aggiungono o tolgono punti AC
     finche' la configurazione supera i criteri C1/C5 con il minimo numero di punti;
  3. business case dell'infrastruttura (rampa di adozione, VAN al tasso Y35) e, se ci
     sono i canoni, TCO mensile per modello.
Poi il totale di gruppo su tutte le sedi.

Non modifica il motore: usa solo run_optimize, run_simulate, run_weekly_check e
josa_core.tco_powertrain.
"""
from __future__ import annotations

import dataclasses
from typing import Optional

from app import schemas, engine
from josa_core.chat_engine import profile_to_analysis_payload
from josa_core.tco_powertrain import (ModelloVeicolo, business_case_infrastruttura,
                                      prezzo_energia_da_readiness, tco_mensile_modello)

MAX_PUNTI_AC_AGGIUNTI = 20


def _energia(economia: dict) -> schemas.EnergyCosts:
    return schemas.EnergyCosts(
        prezzo_privato_eur_kwh=economia["prezzo_energia_aziendale_eur_kwh"],
        prezzo_pubblico_eur_kwh=economia["prezzo_energia_pubblica_eur_kwh"],
        prezzo_domestico_eur_kwh=economia["prezzo_energia_domestica_eur_kwh"],
    )


def _nome_ac(catalogo: list) -> str:
    return next(h["nome"] for h in catalogo if "DC" not in h["nome"].upper())


def _verifica(gruppi, catalogo, policy, energia, config: dict, tolleranza: float) -> dict:
    return engine.run_weekly_check(schemas.WeeklyCheckRequest(
        gruppi=gruppi, catalogo_hardware=catalogo, policy=policy, energia=energia,
        configurazione=schemas.HardwareConfig(quantita=config), tolleranza_min=tolleranza, settimane=8))


def _peggiore(ver: dict) -> float:
    """Copertura nel 5% di giorni peggiori, sullo scenario piu' sfavorevole."""
    return min(sc["copertura_giorni_peggiori_pct"] for sc in ver["scenari"])


def _config_minima_verificata(gruppi, catalogo, policy, energia, config, tolleranza, budget):
    """Minimo numero di punti AC (a parita' di DC) che supera la verifica settimanale.

    Se nessun numero di punti la supera (tipicamente un limite di potenza o di finestra,
    che altre colonnine non risolvono), si tiene la configurazione piu' piccola che
    arriva entro 0,5 punti dalla migliore copertura raggiungibile."""
    ac = _nome_ac([h.model_dump() for h in catalogo])
    cfg = {k: int(v) for k, v in config.items() if int(v) > 0}
    cfg.setdefault(ac, 0)
    ver = _verifica(gruppi, catalogo, policy, energia, cfg, tolleranza)
    if ver["superata"]:
        # togli punti AC finche' la verifica resta superata
        while cfg[ac] > 1:
            prova = dict(cfg, **{ac: cfg[ac] - 1})
            v = _verifica(gruppi, catalogo, policy, energia, prova, tolleranza)
            if not v["superata"]:
                break
            cfg, ver = prova, v
    else:
        provate = [(cfg, ver)]
        senza_miglioramento = 0
        for _ in range(MAX_PUNTI_AC_AGGIUNTI):
            prova = dict(cfg, **{ac: cfg[ac] + 1})
            if _capex_hw(prova, catalogo) > budget:
                break
            v = _verifica(gruppi, catalogo, policy, energia, prova, tolleranza)
            senza_miglioramento = senza_miglioramento + 1 if _peggiore(v) < _peggiore(ver) + 0.3 else 0
            cfg, ver = prova, v
            provate.append((cfg, ver))
            if ver["superata"] or senza_miglioramento >= 3:
                break
        if not ver["superata"]:
            migliore = max(_peggiore(v) for _, v in provate)
            cfg, ver = next((c, v) for c, v in provate if _peggiore(v) >= migliore - 0.5)
    ver = dict(ver, scenari_non_superati=[
        sc["scenario"] for sc in ver["scenari"]
        if sc["c1_servizio_ok"] is False or not sc["c5_robustezza_ok"]])
    return {k: v for k, v in cfg.items() if v > 0}, ver


def _capex_hw(config: dict, catalogo) -> float:
    prezzi = {h.nome: h.costo_acquisto_eur + h.costo_installazione_eur for h in catalogo}
    return float(sum(prezzi.get(k, 0.0) * int(v) for k, v in config.items()))


def _soluzione(sim: dict, config: dict) -> dict:
    """Simulazione nel formato di una soluzione dell'ottimizzatore (per il frontend)."""
    tl = sim.get("timeline_p_kw") or []
    k = sim.get("kpi", {})
    n_col = max(1, sum(int(q) for q in config.values()))
    sess = int(k.get("sessions_ac", 0) or 0) + int(k.get("sessions_dc", 0) or 0)
    return {
        "config": config, "kpi": k, "carbon": k.get("carbon"),
        "capex_eur": float(sim.get("capex_eur", 0.0)),
        "copertura_pct": float(sim.get("copertura_pct", 0.0)),
        "ammissibile": float(sim.get("copertura_pct", 0.0)) >= 99.0,
        "gap_kwh_da_coprire": None, "timeline_p_kw": tl,
        "capacity": {"sessions_total": sess, "sessions_ac": int(k.get("sessions_ac", 0) or 0),
                     "sessions_dc": int(k.get("sessions_dc", 0) or 0)},
        "picco_kw": round(max(tl), 1) if tl else 0.0,
        "utilizzo_colonnine": round(sess / n_col, 2),
        "verificata_settimanale": True,
    }


def _risparmio_annuo(p: dict, economia: dict) -> dict:
    """Risparmio a regime della sede: carburante evitato - energia elettrica (+ manutenzione, se indicata).

    PHEV: quota elettrica dei km (70%) a 0,17 kWh/km invece che a carburante.
    BEV: tutti i km in elettrico invece che con il consumo carburante attuale.
    Prezzo dell'energia: medio da readiness domestica (casa + sede)."""
    km = p["km_anno_veicolo"]
    quota = economia["quota_elettrica_phev_pct"] / 100.0
    p_carb = economia["prezzo_carburante_eur_l"]
    p_en = prezzo_energia_da_readiness(p["pct_casa"], economia["prezzo_energia_domestica_eur_kwh"],
                                       economia["prezzo_energia_aziendale_eur_kwh"],
                                       economia["prezzo_energia_pubblica_eur_kwh"], 100.0)
    bev = next((g for g in p["gruppi"] if g["gruppo"] == "BEV"), None)
    km_phev_el = p["n_phev"] * km * quota
    km_bev = p["n_bev"] * km
    kwh = km_phev_el * 0.17 + km_bev * (bev["consumo_kwh_km"] if bev else 0.18)
    litri_evitati = km_phev_el * 6.0 / 100.0 + km_bev * economia["consumo_carburante_l100km"] / 100.0
    risparmio = litri_evitati * p_carb - kwh * p_en
    return {"kwh_anno": round(kwh), "litri_evitati_anno": round(litri_evitati),
            "prezzo_energia_medio_eur_kwh": round(p_en, 4), "risparmio_energia_eur_anno": round(risparmio)}


def run_chat_analysis(profile: dict, catalogo: Optional[list] = None) -> dict:
    payload = profile_to_analysis_payload(profile, catalogo)
    economia, ipotesi = payload["economia"], list(payload["ipotesi"])
    tolleranza = economia["tolleranza_tra_veicoli_min"]
    tasso = economia["tasso_sconto_pct"] / 100.0

    sedi_out = []
    for p in payload["sedi"]:
        gruppi = [schemas.FleetGroup(**g) for g in p["gruppi"]]
        cat = [schemas.HardwareSpec(**h) for h in p["catalogo_hardware"]]
        policy = schemas.EnginePolicy(**p["policy"])
        energia = _energia(economia)
        opt = engine.run_optimize(schemas.OptimizeRequest(
            gruppi=gruppi, catalogo_hardware=cat, policy=policy, energia=energia,
            budget_max_eur=p["budget_max_eur"], tipi_hardware_da_esplorare=p["tipi_hardware_da_esplorare"],
            beam_size=p["beam_size"], patience=p["patience"], max_steps=p["max_steps"]))
        top = (opt.get("soluzioni") or [None])[0]
        cfg0 = top["config"] if top else {_nome_ac(p["catalogo_hardware"]): 1}
        cfg, ver = _config_minima_verificata(gruppi, cat, policy, energia, cfg0,
                                             tolleranza, p["budget_max_eur"])
        sim = engine.run_simulate(schemas.SimulateRequest(
            gruppi=gruppi, catalogo_hardware=cat, policy=policy, energia=energia,
            configurazione=schemas.HardwareConfig(quantita=cfg)))
        raccomandata = _soluzione(sim, cfg)
        altre = [s for s in opt.get("soluzioni", []) if s["config"] != cfg][:4]

        capex_hw = _capex_hw(cfg, cat)
        capex = capex_hw + p["adeguamento_rete_eur"]
        econ = _risparmio_annuo(p, economia)
        manut = economia["risparmio_manutenzione_eur_anno"] * (p["n_phev"] + p["n_bev"]) / max(
            1, sum(s["n_phev"] + s["n_bev"] for s in payload["sedi"]))
        risparmio = econ["risparmio_energia_eur_anno"] + manut
        bc = business_case_infrastruttura([capex], risparmio, tasso_sconto=tasso)
        sedi_out.append({
            "nome": p["nome"], "n_phev": p["n_phev"], "n_bev": p["n_bev"], "pct_casa": p["pct_casa"],
            "km_giornalieri": p["km_giornalieri"], "mix_dichiarato": p["mix_dichiarato"],
            "configurazione_ottimizzatore": cfg0, "configurazione_raccomandata": cfg,
            "energia_sede_kwh_giorno": round(float(sim["kpi"].get("e_int", 0.0)), 1),
            "copertura_pct": round(raccomandata["copertura_pct"], 1),
            "picco_kw": raccomandata["picco_kw"],
            "verifica_settimanale": ver,
            "capex_hardware_eur": capex_hw, "adeguamento_rete_eur": p["adeguamento_rete_eur"],
            "adeguamento_rete_ipotesi": p["adeguamento_rete_ipotesi"], "capex_eur": capex,
            "economia": dict(econ, risparmio_manutenzione_eur_anno=round(manut), risparmio_a_regime_eur=round(risparmio)),
            "business_case": dataclasses.asdict(bc),
            "budget_indicato": p["budget_indicato"],
            "soluzioni": [raccomandata] + altre,
            "payload_usato": {"gruppi": p["gruppi"], "catalogo_hardware": p["catalogo_hardware"],
                              "policy": p["policy"], "budget_max_eur": p["budget_max_eur"]},
        })
        if p["adeguamento_rete_ipotesi"]:
            ipotesi.append(f"{p['nome']}: adeguamento rete {p['adeguamento_rete_eur']:,.0f} € (ipotesi standard Y35)".replace(",", "."))
        if not p["mix_dichiarato"]:
            ipotesi.append(f"{p['nome']}: mix PHEV/BEV non indicato, tutti i veicoli trattati come "
                           f"{'PHEV' if p['n_phev'] else 'BEV'}")

    capex_tot = sum(s["capex_eur"] for s in sedi_out)
    risp_tot = sum(s["economia"]["risparmio_a_regime_eur"] for s in sedi_out)
    gruppo = {
        "n_sedi": len(sedi_out),
        "n_veicoli": sum(s["n_phev"] + s["n_bev"] for s in sedi_out),
        "punti_ac": sum(v for s in sedi_out for k, v in s["configurazione_raccomandata"].items() if "DC" not in k.upper()),
        "punti_dc": sum(v for s in sedi_out for k, v in s["configurazione_raccomandata"].items() if "DC" in k.upper()),
        "energia_sede_kwh_giorno": round(sum(s["energia_sede_kwh_giorno"] for s in sedi_out), 1),
        "capex_hardware_eur": sum(s["capex_hardware_eur"] for s in sedi_out),
        "adeguamento_rete_eur": sum(s["adeguamento_rete_eur"] for s in sedi_out),
        "capex_eur": capex_tot,
        "risparmio_a_regime_eur": risp_tot,
        "business_case": dataclasses.asdict(business_case_infrastruttura([capex_tot], risp_tot, tasso_sconto=tasso)),
        "tutte_verificate": all(s["verifica_settimanale"]["superata"] for s in sedi_out),
    }

    tco = []
    modelli = [m for m in (profile.get("modelli") or []) if isinstance(m, dict) and m.get("nome")]
    if modelli:
        n_tot = max(1, gruppo["n_veicoli"])
        km_medio = sum(s["km_giornalieri"] * (s["n_phev"] + s["n_bev"]) for s in sedi_out) / n_tot \
            * economia["giorni_lavorativi_anno"]
        readiness = sum(s["pct_casa"] * (s["n_phev"] + s["n_bev"]) for s in sedi_out) / n_tot
        prezzo = prezzo_energia_da_readiness(readiness, economia["prezzo_energia_domestica_eur_kwh"],
                                             economia["prezzo_energia_aziendale_eur_kwh"],
                                             economia["prezzo_energia_pubblica_eur_kwh"], 100.0)
        campi = {f.name for f in dataclasses.fields(ModelloVeicolo)}
        for m in modelli:
            mv = ModelloVeicolo(**{k: v for k, v in m.items() if k in campi})
            tco.append(dataclasses.asdict(tco_mensile_modello(
                mv, km_medio, economia["prezzo_carburante_eur_l"], prezzo,
                economia["quota_elettrica_phev_pct"] / 100.0)))

    prima = sedi_out[0]
    return {
        # Formato compatibile con il frontend (prima sede = vista di dettaglio)
        "soluzioni": prima["soluzioni"],
        "payload_usato": prima["payload_usato"],
        "profilo_conversazionale": profile,
        "sedi": sedi_out,
        "gruppo": gruppo,
        "tco_modelli": tco,
        "ipotesi": ipotesi,
        "metodo": {
            "tolleranza_tra_veicoli_min": tolleranza,
            "tasso_sconto_pct": economia["tasso_sconto_pct"],
            "rampa_adozione": [0.30, 0.90, 1.00],
            "presenza_pct": economia["presenza_pct"],
        },
    }
