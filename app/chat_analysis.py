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
import math
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


# Scenari che dipendono dal numero di punti. "Km +50%" e "Potenza -25%" dipendono
# soprattutto da kW e finestra: se falliscono, altre colonnine di solito non bastano.
SCENARI_DI_SERVIZIO = ("Base", "Presenza 100%", "1 punto AC guasto")


def _ok(sc: dict) -> bool:
    return sc["c1_servizio_ok"] is not False and sc["c5_robustezza_ok"]


def _peggiore(ver: dict, solo_servizio: bool = False) -> float:
    """Copertura nel 5% di giorni peggiori, sullo scenario piu' sfavorevole."""
    return min(sc["copertura_giorni_peggiori_pct"] for sc in ver["scenari"]
               if not solo_servizio or sc["scenario"] in SCENARI_DI_SERVIZIO)


def _servizio_ok(ver: dict) -> bool:
    return all(_ok(sc) for sc in ver["scenari"] if sc["scenario"] in SCENARI_DI_SERVIZIO)


def _config_minima_verificata(gruppi, catalogo, policy, energia, config, tolleranza, budget):
    """Minimo numero di punti AC (a parita' di DC) che supera la verifica settimanale.

    Se nessun numero di punti la supera in tutti gli scenari (tipicamente km di picco o
    potenza ridotta: un limite di kW o di finestra che altre colonnine non risolvono),
    si tiene la configurazione piu' piccola che supera gli scenari di servizio (base,
    presenza piena, un punto guasto); in mancanza, il criterio C5 dei report Y35 (si ferma
    dove altri 4 punti migliorano il servizio di meno di 0,5 punti: oltre il limite di kW
    o di finestra altre colonnine non servono altri veicoli)."""
    ac = _nome_ac([h.model_dump() for h in catalogo])
    cfg = {k: int(v) for k, v in config.items() if int(v) > 0}
    cfg.setdefault(ac, 0)
    ver = _verifica(gruppi, catalogo, policy, energia, cfg, tolleranza)
    provate = [(cfg, ver)]
    if ver["superata"]:
        # togli punti AC finche' la verifica resta superata
        while cfg[ac] > 1:
            prova = dict(cfg, **{ac: cfg[ac] - 1})
            v = _verifica(gruppi, catalogo, policy, energia, prova, tolleranza)
            if not v["superata"]:
                break
            cfg, ver = prova, v
    else:
        # Prima verso il basso: oltre il limite di kW o di finestra meno punti servono
        # lo stesso (es. 50 kW per 30 auto: 8 punti valgono quanto 18).
        giu, v_giu = cfg, ver
        while giu[ac] > 1:
            prova = dict(giu, **{ac: giu[ac] - 1})
            v = _verifica(gruppi, catalogo, policy, energia, prova, tolleranza)
            if _peggiore(v, True) < max(_peggiore(x, True) for _, x in provate) - 2.0 and not _servizio_ok(v):
                break
            giu, v_giu = prova, v
            provate.append((giu, v_giu))
        fermo_tutti = fermo_servizio = 0
        for _ in range(MAX_PUNTI_AC_AGGIUNTI):
            prova = dict(cfg, **{ac: cfg[ac] + 1})
            if _capex_hw(prova, catalogo) > budget:
                break
            v = _verifica(gruppi, catalogo, policy, energia, prova, tolleranza)
            fermo_tutti = fermo_tutti + 1 if _peggiore(v) < _peggiore(ver) + 0.3 else 0
            fermo_servizio = fermo_servizio + 1 if _peggiore(v, True) < _peggiore(ver, True) + 0.3 else 0
            cfg, ver = prova, v
            provate.append((cfg, ver))
            if ver["superata"] or fermo_tutti >= 3 and (_servizio_ok(ver) or fermo_servizio >= 3):
                break
        if not ver["superata"]:
            servizio = [(c, v) for c, v in provate if _servizio_ok(v)]
            if servizio:
                cfg, ver = min(servizio, key=lambda cv: cv[0][ac])
            else:
                cfg, ver = _arresto_c5(gruppi, catalogo, policy, energia, provate, ac, tolleranza, budget)
    ver = dict(ver, scenari_non_superati=[sc["scenario"] for sc in ver["scenari"] if not _ok(sc)])
    return {k: v for k, v in cfg.items() if v > 0}, ver


PASSO_C5_PUNTI = 4        # criterio C5 dei report Y35: quattro punti in piu'...
SOGLIA_C5_PCT = 0.5       # ...che migliorano il servizio di meno di 0,5 punti percentuali


def _servizio_base(ver: dict) -> float:
    return next(sc["copertura_media_pct"] for sc in ver["scenari"] if sc["scenario"] == "Base")


def _arresto_c5(gruppi, catalogo, policy, energia, provate, ac, tolleranza, budget):
    """Configurazione quando nessun numero di punti supera la verifica (limite di potenza o
    di finestra): il minimo numero di punti AC oltre il quale altri PASSO_C5_PUNTI punti
    migliorano il servizio medio di meno di SOGLIA_C5_PCT punti (criterio C5 dei report Y35).
    Oltre la saturazione della potenza altre colonnine non servono altri veicoli."""
    curva = {c[ac]: (c, v) for c, v in provate}
    modello = dict(provate[0][0])

    def punto(n):
        if n not in curva:
            prova = dict(modello, **{ac: n})
            if n < 1 or _capex_hw(prova, catalogo) > budget:
                return None
            curva[n] = (prova, _verifica(gruppi, catalogo, policy, energia, prova, tolleranza))
        return curva[n]

    def ferma(n):
        base = _servizio_base(punto(n)[1])
        oltre = [punto(m) for m in range(n + 1, n + PASSO_C5_PUNTI + 1)]
        oltre = [_servizio_base(x[1]) for x in oltre if x is not None]
        return not oltre or max(oltre) - base < SOGLIA_C5_PCT

    n = min(curva)
    if ferma(n):
        while n > 1 and punto(n - 1) is not None and ferma(n - 1):
            n -= 1
    else:
        while n < min(curva) + MAX_PUNTI_AC_AGGIUNTI and punto(n + 1) is not None:
            n += 1
            if ferma(n):
                break
    return punto(n)


def _verifica_base(gruppi, catalogo, policy, energia, config: dict, tolleranza: float) -> dict:
    """Solo gli scenari di servizio della verifica settimanale (piu' veloce, per lo stress test)."""
    scenari = [sc for sc in schemas.WeeklyCheckRequest.model_fields["scenari"].default_factory()
               if sc.nome in SCENARI_DI_SERVIZIO]
    v = engine.run_weekly_check(schemas.WeeklyCheckRequest(
        gruppi=gruppi, catalogo_hardware=catalogo, policy=policy, energia=energia,
        configurazione=schemas.HardwareConfig(quantita=config), tolleranza_min=tolleranza,
        settimane=8, scenari=scenari))
    base = v["scenari"][0]
    # stessi scenari di servizio del dimensionamento: media nel giorno normale (C1),
    # robustezza (C5) anche con presenza piena e con un punto AC guasto
    return dict(base, c5_robustezza_ok=all(sc["c5_robustezza_ok"] for sc in v["scenari"]))


def _stress_wallbox_non_usata(gruppi, catalogo, policy, energia, cfg, tolleranza, budget,
                              pct_casa: float, quota_non_usata_pct: float) -> Optional[dict]:
    """Stress test del caso peggiore (metodo del report del caso B): una quota di chi ha la
    wallbox ricarica comunque in sede. Non cambia il dimensionamento (giorno normale): dice il
    servizio della configurazione scelta in quel caso e i punti AC che servirebbero (minimo
    numero che supera C1 e C5 negli scenari di servizio (base, presenza piena, un punto
    guasto), o dove altri 4 punti migliorano di meno
    di 0,5 punti se la potenza non basta)."""
    if pct_casa <= 0 or quota_non_usata_pct <= 0:
        return None
    fattore = max(0.0, 1.0 - quota_non_usata_pct / 100.0)
    gruppi_wc = [g.model_copy(update={"pct_veicoli_con_casa": (g.pct_veicoli_con_casa or 0.0) * fattore})
                 for g in gruppi]
    ac = _nome_ac([h.model_dump() for h in catalogo])
    cache = {}

    def prova(n):
        if n not in cache:
            c = dict(cfg, **{ac: n})
            cache[n] = None if n < 1 or _capex_hw(c, catalogo) > budget else \
                _verifica_base(gruppi_wc, catalogo, policy, energia, c, tolleranza)
        return cache[n]

    def ok(n):
        v = prova(n)
        return v is not None and v["c1_servizio_ok"] is not False and v["c5_robustezza_ok"]

    n0 = cfg.get(ac, 0)
    attuale = prova(n0)
    # partenza: i punti scalati con i veicoli che caricano in sede (solo per velocita')
    quota_base = max(0.05, 1.0 - pct_casa / 100.0)
    n = max(n0, int(n0 * (1.0 - pct_casa * fattore / 100.0) / quota_base) - 2)
    if ok(n):
        while n > n0 and ok(n - 1):
            n -= 1
    else:
        while n < n0 + 3 * MAX_PUNTI_AC_AGGIUNTI and prova(n + 1) is not None:
            n += 1
            if ok(n):
                break
            if n >= n0 + 4 and prova(n)["copertura_media_pct"] - prova(n - 4)["copertura_media_pct"] < SOGLIA_C5_PCT:
                n -= 4      # potenza esaurita: altri punti non servono altri veicoli
                break
    return {
        "quota_wallbox_non_usata_pct": quota_non_usata_pct,
        "pct_casa_effettiva": round(pct_casa * fattore, 1),
        "servizio_con_configurazione_pct": attuale["copertura_media_pct"],
        "servizio_giorni_peggiori_pct": attuale["copertura_giorni_peggiori_pct"],
        "configurazione_necessaria": dict(cfg, **{ac: n}),
        "verifica_superata": ok(n),
        "punti_ac_in_piu": max(0, n - n0),
    }


def _punti_nuovi(cfg: dict, p: dict) -> dict:
    """Punti da installare: totale necessario meno i punti gia' presenti in sede (per tipo AC/DC).
    I DC esistenti si tolgono dalla taglia DC piu' piccola della configurazione."""
    out = dict(cfg)
    for tipo_dc, esistenti in ((False, p.get("punti_ac_esistenti", 0)), (True, p.get("punti_dc_esistenti", 0))):
        for k in sorted(out):
            if esistenti <= 0:
                break
            if ("DC" in k.upper()) == tipo_dc:
                tolti = min(esistenti, out[k])
                out[k] -= tolti
                esistenti -= tolti
    return {k: v for k, v in out.items() if v > 0}


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


def _prezzo_energia(pct_casa: float, economia: dict) -> float:
    """Prezzo medio dell'energia: casa + sede secondo la readiness, piu' la quota pubblica."""
    base = prezzo_energia_da_readiness(pct_casa, economia["prezzo_energia_domestica_eur_kwh"],
                                       economia["prezzo_energia_aziendale_eur_kwh"],
                                       economia["prezzo_energia_pubblica_eur_kwh"], 100.0)
    q = economia["quota_ricarica_pubblica_pct"] / 100.0
    return (1.0 - q) * base + q * economia["prezzo_energia_pubblica_eur_kwh"]


def _risparmio_annuo(p: dict, economia: dict) -> dict:
    """Risparmio a regime della sede: carburante evitato - energia elettrica (+ manutenzione, se indicata).

    Solo i veicoli da convertire: i plug-in gia' in flotta (n_*_esistenti) restano nel
    dimensionamento ma non nel risparmio.
    BEV: tutti i km in elettrico. PHEV: la quota indicata dall'utente; se sono indicati i km
    dei PHEV, la quota consentita dalla batteria (12 kWh utili / consumo); altrimenti 70%.
    Carburante evitato: dalla spesa reale (spesa_carburante_eur_anno, ripartita per km)
    se indicata, altrimenti dai km con il consumo attuale.
    Prezzo dell'energia: medio da readiness domestica (casa + sede) con la quota pubblica."""
    giorni = economia["giorni_lavorativi_anno"]
    bev = next((g for g in p["gruppi"] if g["gruppo"] == "BEV"), None)
    phev = next((g for g in p["gruppi"] if g["gruppo"] == "PHEV"), None)
    if p["km_per_gruppo"]:
        km_bev, km_phev = p["km_giornalieri_bev"] * giorni, p["km_giornalieri_phev"] * giorni
    else:
        km_bev = km_phev = p["km_anno_veicolo"]
    n_bev = p["n_bev"] - p["n_bev_esistenti"]
    n_phev = p["n_phev"] - p["n_phev_esistenti"]
    if economia.get("quota_elettrica_phev_indicata") or not p["km_per_gruppo"] or not phev:
        quota = economia["quota_elettrica_phev_pct"] / 100.0
    else:
        quota = min(1.0, float(phev["autonomia_elettrica_km"]) / max(1e-9, p["km_giornalieri_phev"]))
    km_phev_el = n_phev * km_phev * quota
    km_bev_tot = n_bev * km_bev
    kwh = km_phev_el * (phev["consumo_kwh_km"] if phev else 0.17) + km_bev_tot * (bev["consumo_kwh_km"] if bev else 0.18)
    p_en = _prezzo_energia(p["pct_casa"], economia)
    p_carb = economia["prezzo_carburante_eur_l"]
    spesa = p.get("spesa_carburante_eur_anno")
    km_conv = km_bev_tot + n_phev * km_phev
    if spesa and km_conv > 0:
        carburante_evitato = spesa * (km_bev_tot + km_phev_el) / km_conv
        litri_evitati = carburante_evitato / p_carb
    else:
        litri_evitati = km_phev_el * 6.0 / 100.0 + km_bev_tot * economia["consumo_carburante_l100km"] / 100.0
        carburante_evitato = litri_evitati * p_carb
    risparmio = carburante_evitato - kwh * p_en
    return {"kwh_anno": round(kwh), "litri_evitati_anno": round(litri_evitati),
            "carburante_evitato_eur_anno": round(carburante_evitato),
            "quota_elettrica_phev_pct": round(quota * 100, 1),
            "veicoli_nel_risparmio": n_bev + n_phev,
            "base_carburante": "spesa reale" if spesa else "km x consumo",
            "prezzo_energia_medio_eur_kwh": round(p_en, 4), "risparmio_energia_eur_anno": round(risparmio)}


def _n_wallbox(p: dict) -> int:
    """Wallbox domestiche: la quota pct_casa di ogni gruppo, arrotondata per gruppo (x,5 per eccesso)."""
    return int(sum(math.floor(g["n_veicoli"] * p["pct_casa"] / 100.0 + 0.5) for g in p["gruppi"])) if p["pct_casa"] > 0 else 0


def _mesi_conversione(p: dict) -> list:
    """Mese di conversione di ogni veicolo da convertire, dal calendario NLT; [] se non indicato.
    scadenze_nlt_mesi: un mese per veicolo. conversioni_per_anno: veicoli convertiti in ogni
    anno, distribuiti in modo uniforme nell'anno."""
    if p.get("scadenze_nlt_mesi"):
        return sorted(p["scadenze_nlt_mesi"])
    mesi = []
    for anno, k in enumerate(p.get("conversioni_per_anno") or []):
        k = int(round(k))
        mesi += [12 * anno + (j + 0.5) * 12.0 / k for j in range(k)] if k > 0 else []
    return mesi


ANTICIPO_INSTALLAZIONE_MESI = 3   # ogni scaglione si installa prima delle scadenze che lo richiedono


def _piano_temporale(mesi: list, n_esistenti: int, n_totale: int, capex: float, anni: int = 5):
    """(rampa del risparmio per anno, CAPEX per anno) dal calendario delle conversioni.

    Rampa: mesi-veicolo convertiti in ogni anno / (veicoli x 12). CAPEX: cresce con i plug-in
    in sede (gia' in flotta + convertiti entro fine anno + ANTICIPO mesi)."""
    n = len(mesi)
    rampa = [sum(min(12.0, max(0.0, 12 * (a + 1) - m)) for m in mesi) / (12.0 * n) for a in range(anni)]
    cum = [min(1.0, (n_esistenti + sum(1 for m in mesi if m < 12 * (a + 1) + ANTICIPO_INSTALLAZIONE_MESI))
               / max(1, n_totale)) for a in range(anni)]
    cum[-1] = 1.0
    quote = [cum[0]] + [max(0.0, cum[a] - cum[a - 1]) for a in range(1, anni)]
    capex_anni = [round(capex * q) for q in quote]
    while len(capex_anni) > 1 and capex_anni[-1] == 0:
        capex_anni.pop()
    return tuple(round(r, 4) for r in rampa), capex_anni


def _business_case(capex: float, rete: float, risparmio: float, tasso: float, piano=None,
                   rete_inclusa: bool = True) -> dict:
    """Business case con e senza l'adeguamento della rete, sempre entrambi.
    Il risultato principale (payback_mesi, van_eur) include la rete se rete_inclusa
    (convenzione prudente, come nei report dei casi A e B), altrimenti no (caso C).
    Convenzione dei report Y35: flussi a fine anno (anche il CAPEX del primo anno)."""
    if piano:
        rampa, capex_anni = piano
        con_rete = [capex_anni[0] + rete] + capex_anni[1:]
        base = business_case_infrastruttura(capex_anni, risparmio, rampa_adozione=rampa, tasso_sconto=tasso, capex_a_t0=False)
        rete_bc = business_case_infrastruttura(con_rete, risparmio, rampa_adozione=rampa, tasso_sconto=tasso, capex_a_t0=False)
        extra = {"rampa_adozione": list(rampa), "capex_per_anno_eur": capex_anni, "da_calendario_nlt": True}
    else:
        base = business_case_infrastruttura([capex], risparmio, tasso_sconto=tasso, capex_a_t0=False)
        rete_bc = business_case_infrastruttura([capex + rete], risparmio, tasso_sconto=tasso, capex_a_t0=False)
        extra = {"rampa_adozione": [0.30, 0.90, 1.00], "capex_per_anno_eur": [capex], "da_calendario_nlt": False}
    principale = rete_bc if rete_inclusa else base
    return dict(dataclasses.asdict(principale), **extra, adeguamento_rete_incluso=rete_inclusa,
                con_adeguamento_rete={"payback_mesi": rete_bc.payback_mesi, "van_eur": rete_bc.van_eur},
                senza_adeguamento_rete={"payback_mesi": base.payback_mesi, "van_eur": base.van_eur})


def _business_case_gruppo(sedi_out: list, profile: dict, capex_tot: float, risp_tot: float, tasso: float) -> dict:
    """Business case di gruppo. Con il calendario NLT per sede: somma dei CAPEX per anno e
    rampa pesata sul risparmio di ogni sede. Con il calendario solo a livello di flotta
    (campi generali conversioni_per_anno / scadenze_nlt_mesi): piano sul totale. Altrimenti
    rampa standard 30/90/100."""
    rete = sum(s["adeguamento_rete_eur"] for s in sedi_out)
    incl = all(s["business_case"]["adeguamento_rete_incluso"] for s in sedi_out)
    if any(s["business_case"]["da_calendario_nlt"] for s in sedi_out):
        anni = 5
        capex_anni = [0.0] * anni
        risp_anni = [0.0] * anni
        for s in sedi_out:
            bc, r = s["business_case"], s["economia"]["risparmio_a_regime_eur"]
            for i, c in enumerate(bc["capex_per_anno_eur"][:anni]):
                capex_anni[i] += c
            for i in range(anni):
                ramp = bc["rampa_adozione"]
                risp_anni[i] += r * (ramp[i] if i < len(ramp) else 1.0)
        rampa = tuple(x / risp_tot if risp_tot else 1.0 for x in risp_anni)
        while len(capex_anni) > 1 and capex_anni[-1] == 0:
            capex_anni.pop()
        return _business_case(capex_tot, rete, risp_tot, tasso, (rampa, [round(c) for c in capex_anni]), incl)
    if len(sedi_out) > 1:
        flotta = dict(scadenze_nlt_mesi=_lista(profile.get("scadenze_nlt_mesi")),
                      conversioni_per_anno=_lista(profile.get("conversioni_per_anno")))
        mesi = _mesi_conversione(flotta)
        if mesi:
            n_es = sum(s["n_plugin_gia_in_flotta"] for s in sedi_out)
            n_tot = sum(s["n_phev"] + s["n_bev"] for s in sedi_out)
            return _business_case(capex_tot, rete, risp_tot, tasso, _piano_temporale(mesi, n_es, n_tot, capex_tot), incl)
    return _business_case(capex_tot, rete, risp_tot, tasso, rete_inclusa=incl)


def _stress_gruppo(sedi_out: list) -> Optional[dict]:
    st = [(s, s["stress_wallbox_non_usata"]) for s in sedi_out if s.get("stress_wallbox_non_usata")]
    if not st:
        return None
    conta = lambda cfg, dc: sum(v for k, v in cfg.items() if ("DC" in k.upper()) == dc)
    return {
        "quota_wallbox_non_usata_pct": st[0][1]["quota_wallbox_non_usata_pct"],
        "punti_ac_necessari": sum(conta(x["configurazione_necessaria"], False) for _, x in st)
                              + sum(conta(s["configurazione_raccomandata"], False) for s in sedi_out if not s.get("stress_wallbox_non_usata")),
        "punti_ac_in_piu": sum(x["punti_ac_in_piu"] for _, x in st),
        "servizio_min_pct": min(x["servizio_con_configurazione_pct"] for _, x in st),
    }


def _lista(x) -> list:
    return [float(v) for v in x] if isinstance(x, (list, tuple)) else []


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
        stress_wb = _stress_wallbox_non_usata(gruppi, cat, policy, energia, cfg, tolleranza,
                                              p["budget_max_eur"], p["pct_casa"],
                                              economia["quota_wallbox_non_usata_pct"])
        raccomandata = _soluzione(sim, cfg)
        altre = [s for s in opt.get("soluzioni", []) if s["config"] != cfg][:4]

        nuovi = _punti_nuovi(cfg, p)
        capex_hw = _capex_hw(nuovi, cat)
        n_wallbox = _n_wallbox(p)
        capex_wallbox = n_wallbox * economia["costo_wallbox_casa_eur"]
        # CAPEX hardware = punti di sede + wallbox domestiche; l'adeguamento della rete e' a parte
        capex = capex_hw + capex_wallbox
        econ = _risparmio_annuo(p, economia)
        manut = economia["risparmio_manutenzione_eur_anno"] * (p["n_phev"] + p["n_bev"]) / max(
            1, sum(s["n_phev"] + s["n_bev"] for s in payload["sedi"]))
        risparmio = econ["risparmio_energia_eur_anno"] + manut
        mesi = _mesi_conversione(p)
        n_es = p["n_bev_esistenti"] + p["n_phev_esistenti"]
        piano = _piano_temporale(mesi, n_es, p["n_phev"] + p["n_bev"], capex) if mesi else None
        bc = _business_case(capex, p["adeguamento_rete_eur"], risparmio, tasso, piano, economia["rete_nel_business_case"])
        sedi_out.append({
            "nome": p["nome"], "n_phev": p["n_phev"], "n_bev": p["n_bev"], "pct_casa": p["pct_casa"],
            "km_giornalieri": p["km_giornalieri"], "mix_dichiarato": p["mix_dichiarato"],
            "finestra": p["finestra"], "notte_in_sede": p["notte_in_sede"],
            "configurazione_ottimizzatore": cfg0, "configurazione_raccomandata": cfg,
            "punti_esistenti": {"ac": p["punti_ac_esistenti"], "dc": p["punti_dc_esistenti"]},
            "punti_nuovi": nuovi,
            "energia_sede_kwh_giorno": round(float(sim["kpi"].get("e_int", 0.0)), 1),
            "copertura_pct": round(raccomandata["copertura_pct"], 1),
            "picco_kw": raccomandata["picco_kw"],
            "verifica_settimanale": ver,
            "stress_wallbox_non_usata": stress_wb,
            "capex_hardware_eur": capex_hw, "n_wallbox_casa": n_wallbox, "capex_wallbox_casa_eur": capex_wallbox,
            "adeguamento_rete_eur": p["adeguamento_rete_eur"],
            "adeguamento_rete_ipotesi": p["adeguamento_rete_ipotesi"], "capex_eur": capex,
            "economia": dict(econ, risparmio_manutenzione_eur_anno=round(manut), risparmio_a_regime_eur=round(risparmio)),
            "business_case": bc, "n_plugin_gia_in_flotta": n_es,
            "budget_indicato": p["budget_indicato"],
            "soluzioni": [raccomandata] + altre,
            "payload_usato": {"gruppi": p["gruppi"], "catalogo_hardware": p["catalogo_hardware"],
                              "policy": p["policy"], "budget_max_eur": p["budget_max_eur"]},
        })
        if p["adeguamento_rete_ipotesi"]:
            ipotesi.append(f"{p['nome']}: adeguamento rete {p['adeguamento_rete_eur']:,.0f} € (ipotesi standard Y35)".replace(",", "."))
        if p["notte_in_sede_ipotesi"]:
            ipotesi.append(f"{p['nome']}: " + (
                "di notte le auto restano in sede (non indicato)" if p["notte_in_sede"] else
                f"le auto lasciano la sede a fine finestra ({p['finestra'].split('-')[1]}): la ricarica in sede finisce entro quell'ora (non indicato)"))
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
        "punti_ac_nuovi": sum(v for s in sedi_out for k, v in s["punti_nuovi"].items() if "DC" not in k.upper()),
        "punti_dc_nuovi": sum(v for s in sedi_out for k, v in s["punti_nuovi"].items() if "DC" in k.upper()),
        "energia_sede_kwh_giorno": round(sum(s["energia_sede_kwh_giorno"] for s in sedi_out), 1),
        "capex_hardware_eur": sum(s["capex_hardware_eur"] for s in sedi_out),
        "n_wallbox_casa": sum(s["n_wallbox_casa"] for s in sedi_out),
        "capex_wallbox_casa_eur": sum(s["capex_wallbox_casa_eur"] for s in sedi_out),
        "adeguamento_rete_eur": sum(s["adeguamento_rete_eur"] for s in sedi_out),
        "capex_eur": capex_tot,
        "risparmio_a_regime_eur": risp_tot,
        "business_case": _business_case_gruppo(sedi_out, profile, capex_tot, risp_tot, tasso),
        "tutte_verificate": all(s["verifica_settimanale"]["superata"] for s in sedi_out),
        "stress_wallbox_non_usata": _stress_gruppo(sedi_out),
    }

    tco = []
    modelli = [m for m in (profile.get("modelli") or []) if isinstance(m, dict) and m.get("nome")]
    if modelli:
        n_tot = max(1, gruppo["n_veicoli"])
        km_medio = sum(s["km_giornalieri"] * (s["n_phev"] + s["n_bev"]) for s in sedi_out) / n_tot \
            * economia["giorni_lavorativi_anno"]
        readiness = sum(s["pct_casa"] * (s["n_phev"] + s["n_bev"]) for s in sedi_out) / n_tot
        prezzo = _prezzo_energia(readiness, economia)
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
            "rampa_adozione": gruppo["business_case"]["rampa_adozione"],
            "quota_ricarica_pubblica_pct": economia["quota_ricarica_pubblica_pct"],
            "convenzione_flussi": "fine anno (come i report Y35)",
            "presenza_pct": economia["presenza_pct"],
        },
    }
