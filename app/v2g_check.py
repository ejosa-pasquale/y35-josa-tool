"""
Ricarica gestita (V1G) e verifica V2G nella diagnosi della sede.

Due domande, sullo stesso giorno di riferimento della diagnosi (curva vera della sede):

1. Ricarica gestita (V1G): quanto vale far caricare le auto secondo la potenza che la
   sede lascia libera, invece di lasciarle caricare a piena potenza appena collegate.
   Si confrontano tre simulazioni del motore con la stessa flotta e le stesse colonnine:
   senza gestione (nessun limite), limite fisso (come oggi) e limite variabile sulla sede.

2. Check V2G: vale la pena che alcune auto, oltre a caricare, restituiscano energia alla
   sede (V2B) o alla rete? Si usa l'ottimizzatore di dispacciamento gia' presente
   (josa_core.ems.dispatch.solve_dispatch) due volte sulla stessa giornata:
     A) le auto sulle colonnine bidirezionali possono solo caricare (ricarica ottimizzata);
     B) le stesse auto possono anche scaricare.
   La differenza B - A e' il valore della sola scarica, al netto di degrado e perdite.
   Le auto che restano su colonnine normali caricano come nella simulazione del motore.
   Ogni auto riparte con la stessa energia che le da' il motore: il V2G non toglie
   autonomia rispetto al dimensionamento.

Nessuna funzione modifica il motore: si leggono i risultati di run_simulate e si
richiama l'ottimizzatore di dispacciamento esistente.
"""
from __future__ import annotations

import math

from app import schemas, engine
from josa_core import diagnosi as dg
from josa_core import fasce_orarie
from josa_core.ems.assets import VehicleAsset, ChargerAsset, DispatchHorizon, SiteForecast
from josa_core.ems.dispatch import solve_dispatch

SLOT_H = dg.SLOT_H
N = dg.SLOT_GIORNO
SOGLIA_KW = 0.5          # sotto questa potenza una scarica non e' mostrata come finestra
SOSTA_MIN_H = 2.0        # soste piu' brevi non valgono una colonnina bidirezionale
PAYBACK_CONVIENE = 7.0   # anni: entro questo il V2G si ripaga ampiamente nella vita del punto
PAYBACK_FORSE = 10.0     # anni: vita utile tipica di una colonnina


# ---------------------------------------------------------------------------
# 1. Ricarica gestita (V1G)
# ---------------------------------------------------------------------------
def simula_senza_gestione(req, base: dict, rif: dict) -> dict:
    """Stesso giorno di riferimento, nessun limite di potenza: ogni auto carica alla
    potenza della colonnina appena puo'. Stesso seme e stessi km del giorno."""
    potenze = {h.nome: float(h.potenza_kw) for h in req.catalogo_hardware}
    installata = sum(q * potenze.get(t, 0.0) for t, q in req.configurazione.quantita.items())
    illimitato = max(float(req.policy.p_rete_kw), installata, 1.0) * 10.0
    pol = req.policy.model_copy(update={"p_rete_kw": illimitato, "p_shaving_kw": illimitato,
                                        "carico_sede_kw": None, "potenza_contrattuale_kw": None})
    gruppi = [g.model_copy(update={"km_per_giro": g.km_per_giro * rif["fattore_km"]}) for g in req.gruppi]
    return engine.run_simulate(schemas.SimulateRequest(**{**base, "gruppi": gruppi, "policy": pol}),
                               seed=rif["giorno"] + 1)


def _gruppi_giorno(req, rif) -> list:
    return [g.model_copy(update={"km_per_giro": g.km_per_giro * rif["fattore_km"]}) for g in req.gruppi]


def simula_con_limite(req, base: dict, rif: dict, settimana: list[float], limite_kw: float,
                      giornata: bool = False) -> dict:
    """Il giorno di riferimento con il DLM che vede la sede e un tetto di prelievo
    `limite_kw` al posto del contratto: con limite = contratto e' esattamente la
    simulazione «limite variabile» della diagnosi."""
    pol = req.policy.model_copy(update={"carico_sede_kw": [float(x) for x in settimana],
                                        "potenza_contrattuale_kw": float(limite_kw)})
    return engine.run_simulate(schemas.SimulateRequest(**{**base, "gruppi": _gruppi_giorno(req, rif), "policy": pol,
                                                          "giornata": giornata}),
                               seed=rif["giorno"] + 1, giorno=rif["giorno"])


def _flotta_giorno(sim: dict) -> list[float]:
    out = [0.0] * N
    for s, p in enumerate(sim.get("timeline_p_kw") or []):
        out[s % N] += float(p)
    return out


def limite_minimo(req, base: dict, rif: dict, settimana: list[float], contratto: float, costo_esterno) -> float:
    """Il tetto di prelievo piu' basso con cui la ricarica gestita serve la flotta come con
    il contratto pieno (stessa copertura, stessa energia fuori sede): e' il picco che la
    sola gestione della ricarica puo' raggiungere, senza V2G. Ricerca binaria sul motore."""
    cop0 = float(rif["var"]["copertura_pct"])
    ext0 = costo_esterno(rif["var"])
    lo, hi = max(0.0, min(rif["sede"])), float(contratto)
    for _ in range(10):
        if hi - lo <= 0.5:
            break
        mid = (lo + hi) / 2
        sim = simula_con_limite(req, base, rif, settimana, mid)
        if sim["copertura_pct"] >= cop0 - 0.05 and costo_esterno(sim) <= ext0 + 1.0:
            hi = mid
        else:
            lo = mid
    return float(math.ceil(hi * 2) / 2)


def ricarica_gestita(req, base: dict, rif: dict, settimana: list[float], contratto: float, tariffe: dict,
                     costo_esterno) -> dict:
    sede = rif["sede"]
    g = rif["giorno"]
    senza = simula_senza_gestione(req, base, rif)
    int_senza = dg.integra(sede, _flotta_giorno(senza), contratto, g)
    int_oggi, int_var = rif["int_oggi"], rif["int_var"]
    limite = limite_minimo(req, base, rif, settimana, contratto, costo_esterno)
    minimo = simula_con_limite(req, base, rif, settimana, limite)
    int_min = dg.integra(sede, _flotta_giorno(minimo), contratto, g)

    quota = float(tariffe["quota_potenza_eur_kw_anno"])
    contributo = float(tariffe["contributo_aumento_potenza_eur_kw"])
    kw_aumento_senza = math.ceil(max(0.0, int_senza["sforamento_max_kw"]))
    kw_aumento_min = math.ceil(max(0.0, int_min["sforamento_max_kw"]))
    kw_evitati = round(max(0.0, int_senza["picco_aggiunto_dalla_flotta_kw"]
                           - int_min["picco_aggiunto_dalla_flotta_kw"]), 1)
    kw_oltre_contratto = round(max(0.0, int_var["picco_totale_kw"] - int_min["picco_totale_kw"]), 1)
    mancante = max(0.0, costo_esterno(minimo) - costo_esterno(senza))
    una_tantum = round(max(0, kw_aumento_senza - kw_aumento_min) * contributo)

    def riga(nome, sim, integ):
        return {"scenario": nome, "picco_flotta_kw": round(max(_flotta_giorno(sim)), 1),
                "picco_totale_kw": integ["picco_totale_kw"],
                "picco_aggiunto_dalla_flotta_kw": integ["picco_aggiunto_dalla_flotta_kw"],
                "minuti_oltre_contratto": integ["minuti_oltre_contratto"],
                "sforamento_max_kw": integ["sforamento_max_kw"],
                "copertura_pct": sim["copertura_pct"]}

    scenari = [riga("Senza gestione: ogni auto a piena potenza appena collegata", senza, int_senza),
               riga("Limite fisso (come oggi)", rif["oggi"], int_oggi),
               riga("Gestita sulla sede, fino al contratto", rif["var"], int_var),
               riga(f"Gestita sulla sede, picco minimo ({_num_it(limite)} kW)", minimo, int_min)]

    if kw_evitati <= 0.05 and kw_aumento_senza == 0:
        nota = ("La flotta non crea picchi che la gestione possa togliere: anche caricando tutte insieme "
                "le auto non alzano il picco della sede")
    elif kw_aumento_senza > 0 and kw_aumento_min == 0:
        nota = (f"Senza gestione servirebbe un aumento di potenza di {kw_aumento_senza} kW. Con la ricarica "
                f"gestita le stesse auto stanno nel contratto e il picco del sito puo' scendere a "
                f"{_num_it(int_min['picco_totale_kw'])} kW")
    else:
        nota = (f"La ricarica gestita toglie {_num_it(kw_evitati)} kW dal picco: le auto caricano nei quarti "
                "d'ora in cui la sede consuma meno")
    return {
        "scenari": scenari,
        "limite_minimo_kw": limite,
        "kw_evitati": kw_evitati,
        "kw_sotto_il_contratto": kw_oltre_contratto,
        "aumento_potenza_senza_gestione_kw": kw_aumento_senza,
        "aumento_potenza_con_gestione_kw": kw_aumento_min,
        "risparmio_una_tantum_eur": una_tantum,
        "risparmio_quota_potenza_eur_anno": round(kw_evitati * quota),
        "costo_energia_mancante_eur_anno": round(mancante),
        "risparmio_netto_eur_anno": round(kw_evitati * quota - mancante),
        "serie_flotta_senza_gestione_kw": [round(x, 1) for x in _flotta_giorno(senza)],
        "serie_flotta_picco_minimo_kw": [round(x, 1) for x in _flotta_giorno(minimo)],
        "nota": nota,
    }


# ---------------------------------------------------------------------------
# 2. Check V2G
# ---------------------------------------------------------------------------
def _gruppi_bidirezionali(req) -> list[str]:
    cfg = req.v2g
    if cfg.gruppi_bidirezionali is not None:
        return list(cfg.gruppi_bidirezionali)
    # Ipotesi dichiarata: elettriche pure (batteria da almeno 30 kWh). Gli ibridi plug-in
    # oggi non supportano la scarica verso la rete.
    return [g.gruppo for g in req.gruppi if g.batteria_kwh >= 30 and g.n_veicoli > 0]


def _slot_da(h: float) -> int:
    return max(0, min(N, int(math.ceil(h / SLOT_H - 1e-9))))


def _slot_a(h: float) -> int:
    return max(0, min(N, int(math.floor(h / SLOT_H + 1e-9))))


def _candidati(giornata: dict, gruppi_ok: set, prob_utilizzo: dict) -> list[dict]:
    """Auto che possono stare su una colonnina bidirezionale: gruppo bidirezionale e una
    sosta in sede di almeno SOSTA_MIN_H ore. Per ognuna: finestra (slot), energia che il
    motore le da' in quella sosta, stato di carica all'arrivo e curva della sua ricarica."""
    out = []
    for v in giornata.get("veicoli") or []:
        if v.get("gruppo") not in gruppi_ok:
            continue
        soste = [(s, f) for s, f in v.get("presenze") or [] if f - s >= SOSTA_MIN_H]
        if not soste:
            continue
        s, f = max(soste, key=lambda x: x[1] - x[0])
        # La finestra di ricarica del motore puo' partire dopo l'arrivo (rotazione delle
        # auto sulle colonnine): su una colonnina bidirezionale dedicata l'auto e'
        # collegata dall'arrivo in sede, cioe' dalla fine dell'ultima guida prima della sosta.
        arrivi = [g[1] for g in v.get("guide") or [] if g[1] <= s + 1e-6]
        if arrivi:
            s = max(0.0, max(arrivi))
        a, b = _slot_da(s), _slot_a(f)
        if b - a < int(SOSTA_MIN_H / SLOT_H):
            continue
        batt = float(v.get("batteria_kwh") or 0.0)
        if batt <= 0:
            continue
        curva = [0.0] * N
        energia = 0.0
        soc_arrivo = None
        for ses in v.get("sessioni") or []:
            k0 = int(ses["slot"])
            if not (a <= k0 < b):
                continue
            energia += float(ses.get("energia_kwh") or 0.0)
            if soc_arrivo is None and ses.get("soc_inizio_kwh") is not None:
                soc_arrivo = float(ses["soc_inizio_kwh"])
            for j, p in enumerate(ses.get("kw") or []):
                if 0 <= k0 + j < N:
                    curva[k0 + j] += float(p)
        if soc_arrivo is None:
            guida = sum(float(g[2]) for g in v.get("guide") or [] if g[1] <= s + 1e-6)
            soc_arrivo = max(0.0, float(v.get("soc_start_kwh") or 0.0) - guida)
        soc_arrivo = min(batt, soc_arrivo)
        soc_partenza = min(batt, soc_arrivo + energia)
        out.append({"id": v["id"], "gruppo": v.get("gruppo"), "batt": batt, "a": a, "b": b,
                    "energia_kwh": energia, "soc_arrivo_kwh": soc_arrivo, "soc_partenza_kwh": soc_partenza,
                    "curva_motore": curva, "prob": prob_utilizzo.get(v.get("gruppo"), 1.0)})
    # Le soste piu' lunghe prima: sono le auto che restano piu' a lungo sulla colonnina.
    out.sort(key=lambda c: (-(c["b"] - c["a"]), c["id"]))
    return out


def _prezzi_giorno(req, giorno: int) -> list[float]:
    e = req.energia
    prezzi = fasce_orarie.prezzi_fasce(e.prezzi_fasce.come_dict() if e.prezzi_fasce else None,
                                       e.prezzo_privato_eur_kwh)
    return [prezzi[fasce_orarie.fascia_ora(giorno, s * SLOT_H)] for s in range(N)]


def _fv(kwp: float) -> list[float]:
    """Produzione media di un giorno feriale per kWp installato: campana 7-19 con
    3,3 kWh/kWp al giorno (circa 1.200 kWh/kWp all'anno, media Italia)."""
    picco = 3.3 / (12 * 2 / math.pi)
    out = []
    for s in range(N):
        h = (s + 0.5) * SLOT_H
        out.append(kwp * picco * math.sin(math.pi * (h - 7) / 12) if 7 <= h < 19 else 0.0)
    return out


def _dispatch(scelti: list[dict], carico: list[float], fv: list[float], prezzi: list[float],
              cfg, policy, quota_giorno: float, contratto: float, potenza_punto: float, v2g: bool):
    horizon = DispatchHorizon(n_timestep=N, durata_timestep_h=SLOT_H)
    veicoli, punti = [], {}
    for c in scelti:
        disp = [c["a"] <= t < c["b"] for t in range(N)]
        soc0 = 100.0 * c["soc_arrivo_kwh"] / c["batt"]
        # Mai piu' di quanto la colonnina bidirezionale riesce a dare nella sosta.
        massimo = c["soc_arrivo_kwh"] + potenza_punto * (c["b"] - c["a"]) * SLOT_H * cfg.rendimento * 0.98
        socp = 100.0 * min(c["soc_partenza_kwh"], massimo) / c["batt"]
        veicoli.append(VehicleAsset(
            id=c["id"], capacita_kwh=c["batt"], soc_iniziale_pct=soc0,
            soc_min_pct=min(soc0, policy.soc_min_pct), soc_max_pct=max(socp, soc0, policy.soc_max_pct),
            rendimento_carica=cfg.rendimento, rendimento_scarica=cfg.rendimento,
            timestep_partenza=c["b"] - 1, soc_minimo_alla_partenza_pct=socp, disponibile=disp,
            probabilita_utilizzo=c["prob"], costo_degrado_eur_kwh=cfg.costo_degrado_eur_kwh))
        punti[c["id"]] = ChargerAsset(vehicle_id=c["id"], potenza_kw=potenza_punto, v2g_capace=v2g)
    base_netto = [carico[t] - fv[t] for t in range(N)]
    limite = max(contratto, max(base_netto) + 1e-3)
    forecast = SiteForecast(carico_edificio_kw=list(carico), produzione_fv_kw=list(fv),
                            prezzo_acquisto_eur_kwh=list(prezzi),
                            prezzo_vendita_eur_kwh=[cfg.prezzo_vendita_eur_kwh] * N,
                            p_rete_max_kw=limite, costo_potenza_impegnata_eur_kw=quota_giorno)
    res = solve_dispatch(horizon, veicoli, punti, forecast)
    if not res.successo and limite <= contratto:
        forecast.p_rete_max_kw = max(base_netto) + sum(potenza_punto for _ in scelti) + 1.0
        res = solve_dispatch(horizon, veicoli, punti, forecast)
    return res


def _finestre(serie: list[float]) -> list[dict]:
    out, cur = [], None
    for s in range(N + 1):
        v = serie[s] if s < N else 0.0
        if v > SOGLIA_KW:
            if cur is None:
                cur = {"inizio_slot": s, "kwh": 0.0, "kw_max": 0.0}
            cur["kwh"] += v * SLOT_H
            cur["kw_max"] = max(cur["kw_max"], v)
            cur["fine_slot"] = s + 1
        elif cur is not None:
            out.append({"inizio": dg._ora(cur["inizio_slot"]),
                        "fine": dg._ora(cur["fine_slot"]) if cur["fine_slot"] < N else "24:00",
                        "kwh": round(cur["kwh"], 1), "kw_max": round(cur["kw_max"], 1)})
            cur = None
    return out


def _valuta(scelti, carico, fv, prezzi, cfg, policy, quota_anno, contratto, n_punti) -> dict | None:
    """Ottimizzazione A (solo ricarica) e B (ricarica + scarica) e il loro confronto annuo."""
    gg = cfg.giorni_feriali_anno
    quota_giorno = quota_anno / gg
    a = _dispatch(scelti, carico, fv, prezzi, cfg, policy, quota_giorno, contratto, cfg.potenza_punto_kw, False)
    b = _dispatch(scelti, carico, fv, prezzi, cfg, policy, quota_giorno, contratto, cfg.potenza_punto_kw, True)
    if not (a.successo and b.successo):
        return None
    picco = (a.picco_kw - b.picco_kw) * quota_anno
    energia = (a.costo_energia_eur - b.costo_energia_eur) * gg
    vendita = (b.ricavo_vendita_eur - a.ricavo_vendita_eur) * gg
    degrado = (b.costo_degrado_eur - a.costo_degrado_eur) * gg
    netto = picco + energia + vendita - degrado
    scarica = [sum(p.scarica_kw[t] for p in b.piani_veicolo) for t in range(N)]
    carica_b = [sum(p.carica_kw[t] for p in b.piani_veicolo) for t in range(N)]
    kwh_scaricati = sum(scarica) * SLOT_H
    extra = n_punti * cfg.extra_costo_punto_eur
    payback = (extra / netto) if netto > 1e-6 else None
    return {"a": a, "b": b, "scarica": scarica, "carica": carica_b,
            "risparmio": {"picco_eur_anno": round(picco), "energia_eur_anno": round(energia),
                          "vendita_eur_anno": round(vendita), "degrado_eur_anno": round(-degrado),
                          "netto_eur_anno": round(netto)},
            "kwh_scaricati_giorno": round(kwh_scaricati, 1),
            "kwh_scaricati_anno": round(kwh_scaricati * gg),
            "extra_capex_eur": round(extra),
            "payback_anni": None if payback is None else round(payback, 1)}


def _num_it(x: float) -> str:
    return f"{x:g}".replace(".", ",")


def _verdetto(v: dict | None, n_punti: int) -> tuple[str, str]:
    if v is None:
        return "non_calcolabile", "Il V2G non e' calcolabile con questi dati"
    netto, pb = v["risparmio"]["netto_eur_anno"], v["payback_anni"]
    if netto <= 0 or v["kwh_scaricati_giorno"] < 0.5:
        return "non_conviene", ("Non conviene: con questa sede e questi orari far scaricare le auto "
                                "non fa risparmiare piu' di quanto costano degrado e perdite. "
                                "Basta la ricarica gestita")
    if v["extra_capex_eur"] <= 0:
        return "conviene", (f"Conviene: le colonnine bidirezionali costano come quelle normali e "
                            f"{n_punti} auto in V2G fanno risparmiare circa {netto} EUR/anno")
    if pb is not None and pb <= PAYBACK_CONVIENE:
        return "conviene", (f"Conviene: {n_punti} colonnine bidirezionali si ripagano in circa "
                            f"{_num_it(pb)} anni")
    if pb is not None and pb <= PAYBACK_FORSE:
        return "conviene_se", (f"Al limite: le colonnine bidirezionali si ripagano in circa {_num_it(pb)} anni, "
                               "vicino alla loro vita utile. Conviene se migliora almeno una delle leve")
    return "non_conviene", (f"Non conviene oggi: si risparmiano circa {netto} EUR/anno, troppo poco "
                            "per ripagare le colonnine bidirezionali")


def check_v2g(req, base: dict, rif: dict, settimana: list[float], contratto: float, tariffe: dict,
              limite_kw: float | None = None) -> dict:
    """limite_kw: il picco minimo della ricarica gestita. Le auto che restano su colonnine
    normali caricano come in quella simulazione, cosi' il V2G e' confrontato con la migliore
    ricarica gestita e non con una che riempie il contratto."""
    cfg = req.v2g
    g = rif["giorno"]
    gruppi_ok = set(_gruppi_bidirezionali(req))
    prob = {gr.gruppo: (gr.probabilita_utilizzo_pct / 100.0 if gr.probabilita_utilizzo_pct is not None else 1.0)
            for gr in req.gruppi}
    ipotesi = [
        "V2G calcolato sul giorno di riferimento della diagnosi con la curva della sede di quel giorno, "
        f"poi moltiplicato per {cfg.giorni_feriali_anno} feriali all'anno",
        "Ogni auto riparte con la stessa energia che le da' il motore del dimensionamento: il V2G "
        "non toglie autonomia. Le soste in sede (anche la notte, se la flotta ricarica in azienda) "
        "sono quelle della simulazione",
        f"Colonnina bidirezionale dedicata a un'auto per tutta la sosta, {_num_it(cfg.potenza_punto_kw)} kW in "
        "carica e in scarica, " + ("stesso costo di una colonnina normale" if cfg.extra_costo_punto_eur <= 0 else
        f"extra costo {f'{cfg.extra_costo_punto_eur:,.0f}'.replace(',', '.')} EUR per punto rispetto a una normale"),
        f"Degrado batteria {_num_it(cfg.costo_degrado_eur_kwh)} EUR per kWh scaricato, rendimento "
        f"{_num_it(cfg.rendimento)} in carica e in scarica",
        "Riserva prudente durante la scarica: fino a 20 punti di stato di carica in piu' secondo la "
        "probabilita' di utilizzo del gruppo",
        "Picco: quota potenza annua sul massimo prelevato, come nella diagnosi. Il confronto e' con la "
        "ricarica gestita al picco minimo: il V2G vale solo quello che la gestione della ricarica non fa gia'",
    ]
    if cfg.gruppi_bidirezionali is None:
        ipotesi.append("Auto bidirezionali: i gruppi elettrici puri (batteria da almeno 30 kWh); gli ibridi "
                       "plug-in oggi non scaricano verso la rete")
    if cfg.prezzo_vendita_eur_kwh <= 0:
        ipotesi.append("Nessuna vendita alla rete: l'energia delle auto serve solo i consumi della sede (V2B)")
    else:
        ipotesi.append(f"Vendita alla rete a {_num_it(cfg.prezzo_vendita_eur_kwh)} EUR/kWh")
    ipotesi.append("Servizi di flessibilita' per Terna (MSD, UVAM tramite aggregatore) non inclusi")

    vuoto = {"verdetto": "non_calcolabile", "esito": "", "punti_bidirezionali": 0, "auto_idonee": 0,
             "gruppi_bidirezionali": sorted(gruppi_ok), "ipotesi": ipotesi, "leve": []}
    if not gruppi_ok:
        return {**vuoto, "esito": "Nessun gruppo indicato come bidirezionale: indica quali auto supportano il V2G"}

    sim = simula_con_limite(req, base, rif, settimana, limite_kw or contratto, giornata=True)
    candidati = _candidati(sim.get("giornata") or {}, gruppi_ok, prob)
    if not candidati:
        return {**vuoto, "esito": ("Nessuna auto bidirezionale resta in sede abbastanza a lungo "
                                   f"(almeno {_num_it(SOSTA_MIN_H)} ore) per fare V2G")}

    n_max = min(len(candidati), cfg.max_punti_calcolo)
    flotta = _flotta_giorno(sim)
    prezzi = _prezzi_giorno(req, g)
    quota = float(tariffe["quota_potenza_eur_kw_anno"])
    fv = _fv(cfg.fv_aggiuntivo_kwp)

    def scenario(n: int, fv_kw: list[float], cfg_s=cfg):
        scelti = candidati[:n]
        # Le auto scelte escono dalla curva del motore: le ricarica l'ottimizzatore.
        carico = [rif["sede"][t] + flotta[t] - sum(c["curva_motore"][t] for c in scelti) for t in range(N)]
        carico = [max(0.0, x) for x in carico]
        return _valuta(scelti, carico, fv_kw, prezzi, cfg_s, req.policy, quota, contratto, n), scelti, carico

    # Quante colonnine bidirezionali: se non indicato, si provano alcune quantita' e si
    # sceglie quella che si ripaga prima (spesso bastano poche auto per tagliare il picco).
    if cfg.punti_bidirezionali is not None:
        prove = [min(int(cfg.punti_bidirezionali), n_max)]
    else:
        prove = sorted({n for n in (1, 2, 3, 5, 8, 12, 20, 30, 40, n_max) if 1 <= n <= n_max})
    if not prove or prove[0] == 0:
        return {**vuoto, "auto_idonee": len(candidati), "esito": "Nessuna colonnina bidirezionale indicata"}
    risultati = {}
    for n in prove:
        risultati[n] = scenario(n, fv)
    confronto = [{"punti": n, "netto_eur_anno": r[0]["risparmio"]["netto_eur_anno"],
                  "extra_capex_eur": r[0]["extra_capex_eur"], "payback_anni": r[0]["payback_anni"],
                  "picco_con_v2g_kw": round(r[0]["b"].picco_kw, 1)}
                 for n, r in risultati.items() if r[0] is not None]

    def ordine(n):
        v = risultati[n][0]
        if v is None:
            return (2, 0, n)
        pb = v["payback_anni"]
        netto = v["risparmio"]["netto_eur_anno"]
        # A parita' di payback (es. nessun extra costo: payback zero) vince il risparmio piu' alto.
        return (0, pb, -netto, n) if pb is not None else (1, 0, -netto, n)
    n_punti = min(risultati, key=ordine)
    val, scelti, carico = risultati[n_punti]
    verdetto, esito = _verdetto(val, n_punti)

    # Come migliorarlo: ogni leva ricalcolata con lo stesso numero di colonnine; si mostrano
    # solo quelle che migliorano il risultato.
    leve = []
    if val is not None:
        netto0, pb0 = val["risparmio"]["netto_eur_anno"], val["payback_anni"]

        def leva(nome, v, nota):
            if v is None:
                return
            netto, pb = v["risparmio"]["netto_eur_anno"], v["payback_anni"]
            if netto <= netto0 and not (pb is not None and (pb0 is None or pb < pb0)):
                return
            leve.append({"leva": nome, "netto_eur_anno": netto, "delta_eur_anno": netto - netto0,
                         "payback_anni": pb, "nota": nota})

        kwp = max(10.0, round(contratto * 0.3 / 10) * 10)
        leva(f"Fotovoltaico in piu': {_num_it(kwp)} kWp", scenario(n_punti, _fv(cfg.fv_aggiuntivo_kwp + kwp))[0],
             "Le auto assorbono il fotovoltaico a meta' giornata e lo restituiscono quando la sede ne ha bisogno")
        if cfg.prezzo_vendita_eur_kwh <= 0:
            leva("Vendita alla rete a 0,12 EUR/kWh",
                 scenario(n_punti, fv, cfg.model_copy(update={"prezzo_vendita_eur_kwh": 0.12}))[0],
                 "Serve un contratto di cessione dell'energia: oggi le auto servono solo la sede")
        if cfg.extra_costo_punto_eur > 0:
            leva("Colonnina bidirezionale a -30%",
                 scenario(n_punti, fv, cfg.model_copy(update={"extra_costo_punto_eur": cfg.extra_costo_punto_eur * 0.7}))[0],
                 "Stesso risparmio, hardware meno caro (offerte, incentivi)")
        leva("Degrado batteria dimezzato",
             scenario(n_punti, fv, cfg.model_copy(update={"costo_degrado_eur_kwh": cfg.costo_degrado_eur_kwh / 2}))[0],
             "Auto con garanzia che copre il V2G o batterie LFP: scaricare costa meno")
        leve.sort(key=lambda x: (x["payback_anni"] if x["payback_anni"] is not None else 1e9, -x["delta_eur_anno"]))

    out = {**vuoto, "verdetto": verdetto, "esito": esito, "punti_bidirezionali": n_punti,
           "auto_idonee": len(candidati), "leve": leve,
           "confronto_punti": confronto}
    if val is None:
        out["esito"] = "Ottimizzazione V2G non risolta con questi dati"
        return out
    a, b = val["a"], val["b"]
    out.update({
        "risparmio": val["risparmio"],
        "extra_capex_eur": val["extra_capex_eur"],
        "payback_anni": val["payback_anni"],
        "kwh_scaricati_giorno": val["kwh_scaricati_giorno"],
        "kwh_scaricati_anno": val["kwh_scaricati_anno"],
        "picco_solo_ricarica_kw": round(a.picco_kw, 1),
        "picco_con_v2g_kw": round(b.picco_kw, 1),
        "finestre_scarica": _finestre(val["scarica"]),
        "finestre_ricarica": _finestre(val["carica"]),
        "auto": [{"id": c["id"], "gruppo": c["gruppo"], "arrivo": dg._ora(c["a"]),
                  "partenza": dg._ora(c["b"]) if c["b"] < N else "24:00",
                  "energia_motore_kwh": round(c["energia_kwh"], 1),
                  "kwh_scaricati": round(sum(p for p in pv.scarica_kw) * SLOT_H, 1)}
                 for c, pv in zip(scelti, b.piani_veicolo)],
        "serie": [{"ora": dg._ora(t), "sede_e_altre_auto_kw": round(carico[t], 1),
                   "prelievo_solo_ricarica_kw": round(a.prelievo_rete_kw[t], 1),
                   "prelievo_con_v2g_kw": round(b.prelievo_rete_kw[t], 1),
                   "scarica_kw": round(val["scarica"][t], 1)} for t in range(N)],
    })
    return out
