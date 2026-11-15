"""
Orchestrazione energetica a 30 giorni: digital twin della flotta sull'infrastruttura scelta.

Per ogni giorno degli ultimi 30 (calendario reale, ora italiana):
  1. la flotta del cliente viene simulata dal motore Y35 (stesso codice del
     dimensionamento) sulla configurazione scelta: ogni giorno lavorativo ha la sua
     presenza e la sua estrazione casuale, come nella verifica settimanale;
  2. scenario "Come oggi": la curva di ricarica che produce il DLM del motore;
  3. scenario "Ricarica intelligente (V1G)": stesse sessioni (stessa colonnina, stesso
     aggancio, stessa energia), potenza spostata nei quarti d'ora piu' convenienti;
  4. scenario "V1G + V2G": in piu' le auto elettriche sui punti bidirezionali possono
     restituire energia alla sede o alla rete, pagando il degrado della batteria.
Costi con i prezzi al quarto d'ora della zona di mercato + componenti della bolletta,
oppure con i prezzi F1/F2/F3 impostati dal cliente.

Nessun numero del dimensionamento cambia: e' una fase successiva, in sola lettura.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from josa_core import EngineConfig, simulazione_soc, fasce_orarie
from josa_core import prezzi_mercato as pm
from josa_core.orchestrazione import SessioneFlessibile, risolvi_giorno

from . import schemas
from .engine import (_build_fuel, _build_soc_policy, _hw_catalog_to_db, _prepare_events)

SL = 0.25
N_G = 96          # slot per giorno
H = 144           # orizzonte di ogni giornata simulata (36 h, come il motore)
GIORNI_IT = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]


def _sede_kw(policy, dow: int) -> Optional[list]:
    sede = getattr(policy, "carico_sede_kw", None)
    if not sede:
        return None
    if len(sede) == 672:
        return [float(x) for x in sede[dow * N_G:(dow + 1) * N_G]]
    return [float(sede[i % len(sede)]) for i in range(N_G)]


def _sessioni_flessibili(res: dict, vehicles_map: dict, hw_db: dict, gruppi_bev: set,
                         tipi_v2g: Optional[set], v2g: bool, soc_min_pct: float,
                         soc_max_pct: float, tolleranza_h: float) -> list:
    """Le sessioni della simulazione come finestre flessibili sulla stessa colonnina."""
    grezze = []
    for s in res.get("sessions", []) or []:
        lp = s.get("log_p") or {}
        st = lp.get("st")
        e = float(s.get("caricato", 0.0) or 0.0)
        if not st or lp.get("i") is None or e <= 1e-3:
            continue
        t0 = float(lp["i"])
        t_fine_base = float(lp.get("ec", t0) or t0)
        fin = s.get("finestra")
        partenza = float(fin[1]) if fin else float(s.get("f", t_fine_base) or t_fine_base)
        kw_slot = s.get("kw_slot") or []
        slot0 = int(s["slot0"]) if s.get("slot0") is not None else int(math.floor(t0 / SL + 1e-9))
        if kw_slot:
            t_fine_base = max(t_fine_base, (slot0 + len(kw_slot)) * SL)
        grezze.append({"st": st, "vid": s.get("vid"), "t0": t0, "t_fine_base": t_fine_base,
                       "partenza": max(partenza, t_fine_base), "e": e,
                       "soc0": s.get("soc_before"), "kw_max_base": max([float(x) for x in kw_slot] or [0.0])})
    # finestra: fino alla partenza o al prossimo aggancio sulla stessa colonnina
    per_st = {}
    for g in grezze:
        per_st.setdefault(g["st"], []).append(g)
    out = []
    for st, lst in per_st.items():
        lst.sort(key=lambda x: x["t0"])
        tipo = st.rsplit("_", 1)[0]
        p_hw = float(hw_db.get(tipo, {}).get("p", 22.0))
        for i, g in enumerate(lst):
            fine = g["partenza"]
            if i + 1 < len(lst):
                fine = min(fine, lst[i + 1]["t0"] - tolleranza_h)
            fine = max(fine, g["t_fine_base"])
            a = int(math.floor(g["t0"] / SL + 1e-9))
            b = int(math.ceil(fine / SL - 1e-9))
            b_base = int(math.ceil(g["t_fine_base"] / SL - 1e-9))
            a = max(0, min(a, H - 1))
            b = max(a + 1, min(max(b, b_base), H))
            v = vehicles_map.get(g["vid"], {})
            p = p_hw
            if "AC" in tipo.upper():
                p = min(p_hw, float(v.get("potenza_max_ac_kw", 11.0) or 11.0))
            p = max(p, g["kw_max_base"], g["e"] / ((b - a) * SL))
            batt = float(v.get("batt", 0.0) or 0.0)
            ok_v2g = (v2g and batt > 0 and v.get("group") in gruppi_bev
                      and (tipi_v2g is None or tipo in tipi_v2g))
            soc0 = float(g["soc0"]) if g["soc0"] is not None else max(0.0, batt * soc_max_pct / 100 - g["e"])
            out.append(SessioneFlessibile(
                a=a, b=b, energia_kwh=g["e"], p_max_kw=p, v2g=bool(ok_v2g),
                soc0_kwh=soc0, soc_min_kwh=batt * soc_min_pct / 100.0,
                soc_max_kwh=max(batt * soc_max_pct / 100.0, soc0 + g["e"]),
            ))
    return out


def _prezzi(serie: pm.SeriePrezzi, comp: pm.ComponentiBolletta, manuali: Optional[dict]):
    """Prezzo di acquisto e vendita (EUR/kWh) per ogni slot dei giorni + 2 giorni di coda."""
    n = len(serie.giorni)
    acq, ven, ingr, fasce = [], [], [], []
    for d in range(n + 2):
        dd = min(d, n - 1)
        g = serie.giorni[dd]
        dow = (g.weekday() + (d - dd)) % 7
        for s in range(N_G):
            p = serie.eur_mwh[dd][s]
            f = fasce_orarie.fascia_ora(dow, s * SL)
            fasce.append(f)
            ingr.append(p)
            if manuali and manuali.get(f) is not None:
                acq.append(float(manuali[f]))
            else:
                acq.append(comp.prezzo_acquisto(p))
            ven.append(comp.prezzo_vendita(p))
    return np.array(acq), np.array(ven), np.array(ingr), fasce


def _eur(x: float) -> str:
    return f"{x:,.0f}".replace(",", ".")


def _costi(prelievo, immissione, scarica, acq, ven, comp, degrado, n_slot):
    imp = prelievo[:n_slot]; exp = immissione[:n_slot]
    energia = float((imp * acq[:n_slot]).sum() * SL)
    vendita = float((exp * ven[:n_slot]).sum() * SL)
    deg = float(scarica[:n_slot].sum() * SL * degrado)
    picco = float(imp.max()) if len(imp) else 0.0
    mesi = n_slot / N_G / 30.0
    potenza = picco * comp.quota_potenza_eur_kw_mese * mesi
    return {"energia_eur": energia, "vendita_eur": vendita, "degrado_eur": deg,
            "potenza_eur": potenza, "picco_kw": picco,
            "totale_eur": energia + potenza + deg - vendita}


def run_orchestrazione(req: "schemas.OrchestrazioneRequest") -> dict:
    hw_db = _hw_catalog_to_db(req.catalogo_hardware)
    fuel = _build_fuel(req.fuel)
    soc_policy = _build_soc_policy(req.policy)
    costi_motore = req.energia.costi_motore()
    config = {k: int(v) for k, v in req.configurazione.quantita.items() if int(v) > 0}
    if not config:
        raise ValueError("Nessuna colonnina nella configurazione.")
    for t in config:
        if t not in hw_db:
            raise ValueError(f"Tipo di colonnina '{t}' assente dal catalogo.")

    zona = (req.zona or "").upper() if req.zona and req.zona.upper() in pm.ZONE else pm.zona_da_regione(req.regione)
    serie = pm.serie_prezzi(zona, req.giorni, usa_rete=req.usa_prezzi_reali)
    comp = pm.ComponentiBolletta(**req.componenti.model_dump())
    manuali = req.prezzi_manuali.come_dict() if req.prezzi_manuali is not None else None
    if manuali and all(v is None for v in manuali.values()):
        manuali = None
    acq, ven, ingr, fasce = _prezzi(serie, comp, manuali)

    nG = len(serie.giorni)
    L = (nG + 2) * N_G
    n_slot = nG * N_G
    sede_presente = bool(getattr(req.policy, "carico_sede_kw", None))
    contratto = float(req.policy.potenza_contrattuale_kw or req.policy.p_rete_kw) if sede_presente else None

    sede = np.zeros(L)
    for d in range(nG + 2):
        dow = (serie.giorni[0].weekday() + d) % 7
        s = _sede_kw(req.policy, dow)
        if s:
            sede[d * N_G:(d + 1) * N_G] = s

    gruppi_bev = {g.gruppo for g in req.gruppi if g.autonomia_elettrica_km is None}
    tipi_v2g = set(req.v2g_tipi_colonnina) if req.v2g_tipi_colonnina else None
    profilo = list(req.profilo_presenza)[:5] or [1.0] * 5
    toll_h = float(req.policy.tolleranza_tra_veicoli_min) / 60.0
    scen_v2g = bool(req.v2g_attivo)

    flotta = {"oggi": np.zeros(L), "v1g": np.zeros(L), "v2g": np.zeros(L)}
    scarica = {"oggi": np.zeros(L), "v1g": np.zeros(L), "v2g": np.zeros(L)}
    giorni_out, avvisi = [], list(serie.avvisi)
    lp_falliti = 0
    energia_flotta = 0.0
    n_sess_v2g = 0
    punti_v2g = sum(q for t, q in config.items() if tipi_v2g is None or t in tipi_v2g)

    for d, giorno in enumerate(serie.giorni):
        dow = giorno.weekday()
        o = d * N_G
        if dow >= 5:
            giorni_out.append({"data": giorno.isoformat(), "giorno": GIORNI_IT[dow], "operativo": False,
                               "energia_kwh": 0.0, "sessioni": 0})
            continue
        fattore = float(profilo[dow]) if dow < len(profilo) else 1.0
        gruppi = []
        for g in req.gruppi:
            base = 100.0 if g.probabilita_utilizzo_pct is None else float(g.probabilita_utilizzo_pct)
            gruppi.append(g.model_copy(update={"probabilita_utilizzo_pct": min(100.0, base * fattore)}))
        events, vehicles_map, _, fleet_km = _prepare_events(gruppi, req.policy, seed=d + 1)
        sede_g = tuple(_sede_kw(req.policy, dow)) if sede_presente else None
        res = simulazione_soc(
            config, events, vehicles_map, costi_motore, hw_db,
            p_shave_limit=req.policy.p_shaving_kw,
            max_ac_v=req.policy.max_ac_veicoli_per_punto,
            max_dc_v=req.policy.max_dc_veicoli_per_punto,
            limit_h=req.policy.limite_ora_turno,
            engine_cfg=EngineConfig(p_rete=req.policy.p_rete_kw, allow_oversizing=True,
                                    dc_fixed_power=req.policy.dc_fixed_power,
                                    dc_redundancy=req.policy.dc_redundancy,
                                    carico_sede_kw=sede_g,
                                    potenza_contrattuale_kw=req.policy.potenza_contrattuale_kw),
            fuel=fuel, soc_policy=soc_policy, fleet_km_day_total=fleet_km, sim_days=1,
            hybrid_private_home_charging=req.policy.hybrid_private_home_charging,
            company_buffer_pct=req.policy.company_buffer_pct / 100.0,
            tolleranza_min=req.policy.tolleranza_tra_veicoli_min,
        )
        if res is None:
            raise ValueError("Configurazione non simulabile: il limite di ricarica supera la potenza di rete.")
        tl = np.array([float(x) for x in (res.get("timeline_p") if res.get("timeline_p") is not None else [])][:H])
        base_g = np.zeros(H); base_g[:len(tl)] = tl
        flotta["oggi"][o:o + H] += base_g

        sess = _sessioni_flessibili(res, vehicles_map, hw_db, gruppi_bev, tipi_v2g, False,
                                    req.v2g_soc_min_pct, req.policy.soc_max_pct, toll_h)
        e_giorno = sum(s.energia_kwh for s in sess)
        energia_flotta += e_giorno
        # "Come oggi" eroga tutta l'energia registrata nelle sessioni? Se il DLM ne ha
        # registrata una parte fuori dalle sessioni, la curva resta quella del motore.
        giorni_out.append({"data": giorno.isoformat(), "giorno": GIORNI_IT[dow], "operativo": True,
                           "energia_kwh": round(e_giorno, 1), "sessioni": len(sess)})

        limite_base = req.policy.p_shaving_kw if not sede_presente else contratto
        for nome in (["v1g", "v2g"] if scen_v2g else ["v1g"]):
            ss = sess if nome == "v1g" else _sessioni_flessibili(
                res, vehicles_map, hw_db, gruppi_bev, tipi_v2g, True,
                req.v2g_soc_min_pct, req.policy.soc_max_pct, toll_h)
            if nome == "v2g":
                n_sess_v2g += sum(1 for s in ss if s.v2g)
            spill = flotta[nome][o:o + H] - scarica[nome][o:o + H]
            base_lp = sede[o:o + H] + spill
            # Limite: quello della policy, mai sotto quanto gia' prelevato dalla curva del motore
            limite = np.maximum(limite_base, base_lp + base_g)
            prel_prec = np.maximum(0.0, sede[:o] + flotta[nome][:o] - scarica[nome][:o])
            picco_prec = float(prel_prec.max()) if o > 0 else 0.0
            esito = risolvi_giorno(
                ss, base_lp.tolist(), limite.tolist(), acq[o:o + H].tolist(), ven[o:o + H].tolist(),
                vendita_ammessa=(nome == "v2g" and req.v2g_vendita_in_rete),
                degrado_eur_kwh=req.degrado_eur_kwh,
                rendimento_scarica=req.rendimento_scarica_pct / 100.0,
                quota_potenza_eur_kw=comp.quota_potenza_eur_kw_mese,
                picco_precedente_kw=picco_prec,
            )
            if esito.ok:
                flotta[nome][o:o + H] += np.array(esito.carica_kw)
                scarica[nome][o:o + H] += np.array(esito.scarica_kw)
            else:
                lp_falliti += 1
                flotta[nome][o:o + H] += base_g

    if lp_falliti:
        avvisi.append(f"In {lp_falliti} casi l'ottimizzazione non ha trovato soluzione: per quei giorni vale la ricarica come oggi.")

    # --- costi ---
    ris = {}
    solo_sede = _costi(np.maximum(0, sede), np.zeros(L), np.zeros(L), acq, ven, comp, 0.0, n_slot) if sede_presente else None
    nomi = ["oggi", "v1g"] + (["v2g"] if scen_v2g else [])
    etichette = {"oggi": "Come oggi", "v1g": "Ricarica intelligente (V1G)", "v2g": "V1G + V2G"}
    for nome in nomi:
        netto = sede + flotta[nome] - scarica[nome]
        prel = np.maximum(0.0, netto)
        imm = np.maximum(0.0, -netto)
        c = _costi(prel, imm, scarica[nome], acq, ven, comp, req.degrado_eur_kwh, n_slot)
        costo_flotta = c["totale_eur"] - (solo_sede["totale_eur"] if solo_sede else 0.0)
        kwh_fasce = {f: 0.0 for f in fasce_orarie.FASCE}
        for t in range(n_slot):
            kwh_fasce[fasce[t]] += float(flotta[nome][t]) * SL
        e_car = float(flotta[nome][:n_slot].sum() * SL)
        e_sca = float(scarica[nome][:n_slot].sum() * SL)
        ris[nome] = {
            "nome": etichette[nome],
            "costo_totale_eur": round(c["totale_eur"], 2),
            "costo_flotta_eur": round(costo_flotta, 2),
            "energia_eur": round(c["energia_eur"], 2),
            "potenza_eur": round(c["potenza_eur"], 2),
            "degrado_eur": round(c["degrado_eur"], 2),
            "vendita_eur": round(c["vendita_eur"], 2),
            "picco_kw": round(c["picco_kw"], 1),
            "energia_caricata_kwh": round(e_car, 1),
            "energia_restituita_kwh": round(e_sca, 1),
            "eur_kwh_flotta": round(costo_flotta / e_car, 4) if e_car > 0 else None,
            "kwh_per_fascia": {f: round(v, 1) for f, v in kwh_fasce.items()},
        }

    fatt_anno = 365.0 / nG
    base_c = ris["oggi"]["costo_flotta_eur"]
    for nome in nomi:
        r = ris[nome]
        r["risparmio_eur"] = round(base_c - r["costo_flotta_eur"], 2)
        r["risparmio_anno_eur"] = round((base_c - r["costo_flotta_eur"]) * fatt_anno, 0)
        r["risparmio_pct"] = round(100.0 * (base_c - r["costo_flotta_eur"]) / base_c, 1) if base_c > 0 else 0.0

    v2g_out = None
    if scen_v2g:
        extra = ris["v1g"]["costo_flotta_eur"] - ris["v2g"]["costo_flotta_eur"]
        extra_anno = extra * fatt_anno
        capex = req.extra_costo_bidirezionale_eur * punti_v2g
        if extra_anno <= 1.0:
            verdetto = "non_conviene"
            testo = ("Il V2G non aggiunge risparmio rispetto alla ricarica intelligente: con questi prezzi "
                     "la differenza tra ore care ed economiche non copre degrado, perdite e oneri.")
        elif capex <= 0:
            verdetto = "conviene"
            testo = (f"Il V2G aggiunge circa {_eur(extra_anno)} EUR all'anno alla ricarica intelligente, "
                     "senza costi extra per i punti bidirezionali.")
        else:
            pb = capex / extra_anno
            verdetto = "conviene" if pb <= 6 else ("da_valutare" if pb <= 10 else "non_conviene")
            testo = (f"Il V2G aggiunge circa {_eur(extra_anno)} EUR all'anno; il costo extra dei punti "
                     f"bidirezionali ({_eur(capex)} EUR) rientra in {str(round(pb, 1)).replace('.', ',')} anni.")
        v2g_out = {
            "punti_bidirezionali": int(punti_v2g),
            "sessioni_v2g": int(n_sess_v2g),
            "extra_vs_v1g_eur": round(extra, 2),
            "extra_vs_v1g_anno_eur": round(extra_anno, 0),
            "extra_costo_punti_eur": round(capex, 0),
            "vendita_in_rete": bool(req.v2g_vendita_in_rete),
            "verdetto": verdetto,
            "testo": testo,
        }

    # --- giorno tipo (media dei giorni lavorativi) e mappa giorno x quarto d'ora ---
    operativi = [d for d, g in enumerate(serie.giorni) if g.weekday() < 5]
    def media(arr):
        if not operativi:
            return [0.0] * N_G
        m = np.mean([arr[d * N_G:(d + 1) * N_G] for d in operativi], axis=0)
        return [round(float(x), 2) for x in m]
    giorno_tipo = {
        "prezzo_eur_kwh": media(acq),
        "ingrosso_eur_mwh": media(ingr),
        "sede_kw": media(sede) if sede_presente else None,
        "flotta_kw": {n: media(flotta[n]) for n in nomi},
        "scarica_kw": {n: media(scarica[n]) for n in nomi},
    }
    mappa = {
        "giorni": [g.isoformat() for g in serie.giorni],
        "ingrosso_eur_mwh": [[round(float(x), 1) for x in serie.eur_mwh[d]] for d in range(nG)],
        "flotta_kw": {n: [[round(float(x), 1) for x in flotta[n][d * N_G:(d + 1) * N_G]] for d in range(nG)] for n in nomi},
        "scarica_kw": ({"v2g": [[round(float(x), 1) for x in scarica["v2g"][d * N_G:(d + 1) * N_G]] for d in range(nG)]}
                       if scen_v2g else None),
    }
    ingr_arr = np.array(serie.eur_mwh, dtype=float)
    return {
        "zona": zona,
        "zona_nome": pm.ZONE[zona][0],
        "prezzi": {
            "fonte": serie.fonte,
            "descrizione": serie.descrizione,
            "dal": serie.giorni[0].isoformat(),
            "al": serie.giorni[-1].isoformat(),
            "ingrosso_medio_eur_mwh": round(float(ingr_arr.mean()), 1),
            "ingrosso_min_eur_mwh": round(float(ingr_arr.min()), 1),
            "ingrosso_max_eur_mwh": round(float(ingr_arr.max()), 1),
            "prezzi_manuali": manuali,
            "componenti": comp.come_dict(),
            "acquisto_medio_eur_kwh": round(float(acq[:n_slot].mean()), 4),
        },
        "configurazione": config,
        "giorni_simulati": nG,
        "giorni_operativi": len(operativi),
        "sede_presente": sede_presente,
        "limite_kw": contratto if sede_presente else float(req.policy.p_shaving_kw),
        "energia_flotta_kwh": round(energia_flotta, 1),
        "scenari": ris,
        "v2g": v2g_out,
        "giorni": giorni_out,
        "giorno_tipo": giorno_tipo,
        "mappa": mappa,
        "avvisi": avvisi,
        "ipotesi": [
            "Stessa infrastruttura, stessi veicoli, stessi agganci ed energia della simulazione del motore Y35: cambia solo quando si carica.",
            "Giorni lavorativi lun-ven con presenza variabile come nella verifica settimanale; sabato e domenica la flotta non si ricarica in sede.",
            "Prezzo di acquisto = prezzo all'ingrosso x (1 + perdite) + spread + dispacciamento + trasporto e oneri + accisa (IVA esclusa); quota potenza sul picco del periodo.",
            "V2G solo per auto elettriche (non ibride) su punti bidirezionali; energia ceduta pagata al prezzo zonale; degrado batteria sull'energia scaricata.",
            "Ottimizzazione con prezzi noti del giorno prima (il MGP si chiude la vigilia): è il risparmio ottenibile con un sistema di gestione che li legge.",
        ],
    }
