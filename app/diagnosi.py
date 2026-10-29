"""
Diagnosi energetica della ricarica: la flotta simulata da Y35 dentro i consumi della sede.

Passi:
  1. profilo della sede dalla fonte migliore disponibile (curva POD > bolletta > dichiarato);
  2. ogni feriale simulato a se': partenze, rientri, presenze e km cambiano da un giorno
     all'altro, quindi cambiano lo scarico delle batterie e la ricarica al rientro;
     ogni giorno e' sommato ai consumi della sede di quel giorno;
  3. per ogni giorno, simulazione "come oggi" (limite fisso p_shaving_kw) e con limite
     variabile (contratto - sede in ogni quarto d'ora);
  4. giorno di riferimento: il feriale con il picco totale piu' alto;
  5. mix AC/DC: l'ottimizzatore del dimensionamento senza e con il limite della sede nel
     giorno di riferimento; il mix consigliato e' poi verificato su tutti i feriali;
  6. capacita' libera, costo del picco e alternative con le tariffe ARERA di riferimento.

Il dimensionamento non cambia: si usano run_simulate e le funzioni di josa_core.diagnosi.
"""
from __future__ import annotations

import random

from app import schemas, engine
from josa_core import diagnosi as dg


def _settimana(valori: list[float]) -> list[float]:
    return list(valori) * 7 if len(valori) == dg.SLOT_GIORNO else list(valori)


def profilo_sede(sede: "schemas.SedeInput") -> dict:
    """Profilo settimanale della sede (672 valori), livello di affidabilita' e note."""
    note: list[str] = []
    dettaglio = None
    if sede.curva_csv:
        dettaglio = dg.importa_curva_pod(sede.curva_csv, sede.unita_curva)
        settimana = dettaglio["settimana_p90_kw"]
        note += dettaglio["avvertenze"]
        note.append(f"Curva POD: {dettaglio['giorni_letti']} giorni letti, per ogni quarto d'ora "
                    "si usa il 90o percentile dei giorni (scelta prudente)")
        livello = 2
        dettaglio = {k: v for k, v in dettaglio.items() if not k.startswith("settimana_")}
    elif sede.carico_sede_kw:
        settimana = _settimana(sede.carico_sede_kw)
        livello = 2
        note.append("Curva della sede fornita direttamente in kW per quarto d'ora")
    elif sede.bolletta is not None:
        b = sede.bolletta
        settimana = dg.profilo_da_fasce(sede.tipo_sede, b.kwh_f1, b.kwh_f2, b.kwh_f3, b.giorni)
        livello = 1
        note.append(f"Profilo ricostruito dai consumi per fascia della bolletta con la forma tipica "
                    f"'{sede.tipo_sede}': e' una stima, la curva quartoraria del POD la rende precisa")
    elif sede.picco_sede_kw:
        settimana = dg.profilo_tipico(sede.tipo_sede, sede.picco_sede_kw)
        livello = 0
        note.append(f"Profilo tipico '{sede.tipo_sede}' scalato sul picco dichiarato di "
                    f"{sede.picco_sede_kw:g} kW: e' una stima, bolletta o curva POD la migliorano")
    else:
        raise ValueError("Per la diagnosi serve almeno uno tra: curva del POD, consumi della bolletta "
                         "per fascia, oppure il picco della sede dichiarato")
    return {"settimana": settimana, "livello": livello, "note": note, "curva": dettaglio}


def _finestra(gruppi) -> tuple[float, float]:
    inizio = min(g.finestra_inizio.hour + g.finestra_inizio.minute / 60 for g in gruppi)
    fine = max(g.finestra_fine.hour + g.finestra_fine.minute / 60 for g in gruppi)
    if fine <= inizio:
        fine += 24
    return inizio, fine


def _giorno_flotta(timeline: list[float]) -> list[float]:
    """Curva della flotta riportata alle 24 ore: la ricarica oltre mezzanotte cade
    all'inizio del giorno dopo, che nella giornata tipo ripetuta e' lo stesso orario."""
    out = [0.0] * dg.SLOT_GIORNO
    for s, p in enumerate(timeline or []):
        out[s % dg.SLOT_GIORNO] += float(p)
    return out


def _veicoli_in_uso(sim: dict) -> int:
    """Veicoli che quel giorno sono usati (almeno un tratto di guida nel Gantt)."""
    return sum(1 for v in sim.get("gantt_veicoli") or []
               if any(seg.get("stato") == "lavoro" for seg in v.get("segmenti", [])))


def _simula_giorni(req: "schemas.DiagnosiRequest", base: dict, settimana: list[float],
                   contratto: float) -> list[dict]:
    """Simula i cinque feriali, ciascuno con il suo seme (presenze, partenze, numero di
    giri) e un fattore sui km: lo scarico al rientro e quindi la ricarica cambiano ogni giorno."""
    rng = random.Random(7)
    v = req.variazione_km_pct / 100.0
    pol_var = req.policy.model_copy(update={"carico_sede_kw": [float(x) for x in settimana],
                                            "potenza_contrattuale_kw": contratto})
    giorni = []
    for d in range(5):
        fattore_km = 1.0 + rng.uniform(-v, v)
        gruppi = [g.model_copy(update={"km_per_giro": g.km_per_giro * fattore_km}) for g in req.gruppi]
        sede_d = settimana[d * dg.SLOT_GIORNO:(d + 1) * dg.SLOT_GIORNO]
        oggi = engine.run_simulate(schemas.SimulateRequest(**{**base, "gruppi": gruppi}), seed=d + 1)
        var = engine.run_simulate(schemas.SimulateRequest(**{**base, "gruppi": gruppi, "policy": pol_var}),
                                  seed=d + 1, giorno=d)
        flotta_oggi = _giorno_flotta(oggi["timeline_p_kw"])
        flotta_var = _giorno_flotta(var["timeline_p_kw"])
        giorni.append({"giorno": d, "fattore_km": fattore_km, "sede": sede_d,
                       "oggi": oggi, "var": var, "flotta_oggi": flotta_oggi, "flotta_var": flotta_var,
                       "int_oggi": dg.integra(sede_d, flotta_oggi, contratto, d),
                       "int_var": dg.integra(sede_d, flotta_var, contratto, d)})
    return giorni


def _costo_esterno(sim: dict) -> float:
    """Costo annuo dell'energia che la flotta non carica in sede, come lo calcola il motore:
    sovrapprezzo della ricarica pubblica rispetto a quella aziendale piu' il tempo del personale."""
    k = sim.get("kpi") or {}
    return float(k.get("pena_en", 0.0)) + float(k.get("staff_ext", 0.0))


def _descrivi_mix(config: dict, catalogo) -> dict:
    potenze = {h.nome: float(h.potenza_kw) for h in catalogo}
    config = {k: int(v) for k, v in config.items() if int(v) > 0}
    return {"config": config,
            "n_ac": sum(v for k, v in config.items() if "DC" not in k.upper()),
            "n_dc": sum(v for k, v in config.items() if "DC" in k.upper()),
            "potenza_installata_kw": round(sum(v * potenze.get(k, 0.0) for k, v in config.items()), 1)}


def _ottimizza(req: "schemas.DiagnosiRequest", policy) -> dict | None:
    out = engine.run_optimize(schemas.OptimizeRequest(
        gruppi=req.gruppi, catalogo_hardware=req.catalogo_hardware, policy=policy, fuel=req.fuel,
        energia=req.energia, tipi_hardware_da_esplorare=[h.nome for h in req.catalogo_hardware],
        budget_max_eur=req.budget_max_eur))
    if not out["soluzioni"]:
        return None
    s = out["soluzioni"][0]
    return {**_descrivi_mix(s["config"], req.catalogo_hardware), "capex_eur": s["capex_eur"],
            "copertura_pct": s["copertura_pct"], "ammissibile": s["ammissibile"],
            "picco_flotta_kw": s["picco_kw"]}


def _mix(req, base, settimana, contratto, g_rif, rif) -> dict:
    """Mix inserito, ottimo senza la sede (dimensionamento classico) e ottimo con la sede."""
    pol_rif = req.policy.model_copy(update={"carico_sede_kw": [float(x) for x in rif["sede"]],
                                            "potenza_contrattuale_kw": contratto})
    inserito = {**_descrivi_mix(req.configurazione.quantita, req.catalogo_hardware),
                "capex_eur": rif["oggi"]["capex_eur"], "copertura_pct": rif["var"]["copertura_pct"],
                "ammissibile": None, "picco_flotta_kw": round(max(rif["flotta_var"]), 1)}
    senza = _ottimizza(req, req.policy)
    con = _ottimizza(req, pol_rif)
    verifica = []
    if con:
        base_con = {**base, "configurazione": schemas.HardwareConfig(quantita=con["config"])}
        for x in _simula_giorni(req, base_con, settimana, contratto):
            verifica.append({"giorno": dg.GIORNI[x["giorno"]], "copertura_pct": x["var"]["copertura_pct"],
                             "picco_totale_kw": x["int_var"]["picco_totale_kw"],
                             "minuti_oltre_contratto": x["int_var"]["minuti_oltre_contratto"],
                             "picco_aggiunto_dalla_flotta_kw": x["int_var"]["picco_aggiunto_dalla_flotta_kw"],
                             "costo_esterno_eur_anno": round(_costo_esterno(x["var"]))})
    if con and senza and con["config"] != senza["config"]:
        nota = ("I consumi della sede cambiano il mix: con la potenza che resta davvero libera "
                "servono colonnine diverse da quelle del dimensionamento senza sede")
    elif con:
        nota = "I consumi della sede non cambiano il mix ottimale: basta un DLM che legga il contatore"
    else:
        nota = "Nessun mix entro il budget: alza il budget o rivedi orari e contratto"
    return {"inserito": inserito, "ottimo_senza_sede": senza, "consigliato": con,
            "verifica_giorni": verifica, "giorno_ottimizzazione": dg.GIORNI[g_rif], "nota": nota}


def run_diagnosi(req: "schemas.DiagnosiRequest") -> dict:
    sede = profilo_sede(req.sede)
    settimana = sede["settimana"]
    contratto = float(req.sede.potenza_contrattuale_kw)
    inizio_h, fine_h = _finestra(req.gruppi)

    base = {k: getattr(req, k) for k in schemas.SimulateRequest.model_fields}
    base["gantt_settimanale"] = False

    # 1-2. Ogni feriale con il suo scarico e la sua ricarica, sommato alla sede di quel giorno.
    giorni = _simula_giorni(req, base, settimana, contratto)

    # 3. Giorno di riferimento: il picco totale (sede + flotta) piu' alto della settimana.
    rif = max(giorni, key=lambda x: (x["int_oggi"]["picco_totale_kw"], x["int_oggi"]["minuti_oltre_contratto"]))
    g_rif = rif["giorno"]
    sede_giorno = rif["sede"]
    oggi, var = rif["oggi"], rif["var"]
    flotta_oggi, flotta_var = rif["flotta_oggi"], rif["flotta_var"]
    int_oggi, int_var = rif["int_oggi"], rif["int_var"]

    # Settimana: la curva di ogni feriale, sabato e domenica restano alla sede.
    flotta_sett = []
    for x in giorni:
        flotta_sett += x["flotta_oggi"]
    flotta_sett += [0.0] * (2 * dg.SLOT_GIORNO)
    int_sett = dg.integra(settimana, flotta_sett, contratto)

    riepilogo = [{"giorno": dg.GIORNI[x["giorno"]],
                  "km_pct": round((x["fattore_km"] - 1) * 100),
                  "veicoli_in_uso": _veicoli_in_uso(x["oggi"]),
                  "energia_flotta_kwh": x["int_oggi"]["energia_flotta_kwh"],
                  "picco_flotta_kw": round(max(x["flotta_oggi"]), 1),
                  "picco_totale_kw": x["int_oggi"]["picco_totale_kw"],
                  "picco_totale_quando": x["int_oggi"]["picco_totale_quando"],
                  "minuti_oltre_contratto": x["int_oggi"]["minuti_oltre_contratto"],
                  "copertura_oggi_pct": x["oggi"]["copertura_pct"],
                  "copertura_limite_variabile_pct": x["var"]["copertura_pct"],
                  "picco_totale_limite_variabile_kw": x["int_var"]["picco_totale_kw"]}
                 for x in giorni]

    mix = _mix(req, base, settimana, contratto, g_rif, rif) if req.ottimizza_mix else None

    # 4. Capacita' libera e limite energetico teorico della flotta.
    libera = dg.capacita_libera(sede_giorno, contratto, req.policy.p_shaving_kw)
    e_libera = dg.energia_libera_finestra(libera, inizio_h, fine_h)
    serviti = max(1, int(oggi["veicoli_serviti"]))
    kwh_veicolo = int_oggi["energia_flotta_kwh"] / serviti
    veicoli_teorici = int(e_libera // kwh_veicolo) if kwh_veicolo > 0 else None

    # 5. Costi e alternative.
    e_finestra_max = max((f["energia_extra_kwh"] for f in int_oggi["finestre_critiche"]), default=0.0)
    costi = dg.costo_picco(int_oggi["picco_aggiunto_dalla_flotta_kw"], int_oggi["sforamento_max_kw"],
                           e_finestra_max, req.tariffe.model_dump())
    t = costi["tariffe_usate"]
    quota_var = round(int_var["picco_aggiunto_dalla_flotta_kw"] * t["quota_potenza_eur_kw_anno"])
    # Energia che resta fuori dalla sede (auto che escono non piene): costo annuo in piu'
    # rispetto a "come oggi", media dei feriali, con la stessa valorizzazione del motore.
    ext_oggi = sum(_costo_esterno(x["oggi"]) for x in giorni) / len(giorni)
    mancante_var = max(0.0, sum(_costo_esterno(x["var"]) for x in giorni) / len(giorni) - ext_oggi)
    opzioni = []
    if int_oggi["minuti_oltre_contratto"] > 0:
        opzioni = [
            {"opzione": "Limite di potenza variabile (il DLM vede i consumi della sede)",
             "costo_una_tantum_eur": 0, "costo_annuo_eur": quota_var,
             "costo_energia_mancante_eur_anno": round(mancante_var),
             "copertura_pct": var["copertura_pct"], "veicoli_serviti": var["veicoli_serviti"],
             "nota": "Nessun lavoro sull'allaccio: serve un DLM collegato al contatore della sede. "
                     "La potenza non si supera mai; chi esce prima di aver finito parte non pieno"},
            {"opzione": f"Aumento di potenza di {costi['aumento_potenza']['kw']} kW",
             "costo_una_tantum_eur": costi["aumento_potenza"]["costo_una_tantum_eur"],
             "costo_annuo_eur": costi["aumento_potenza"]["costo_annuo_eur"],
             "costo_energia_mancante_eur_anno": 0,
             "copertura_pct": oggi["copertura_pct"], "veicoli_serviti": oggi["veicoli_serviti"],
             "nota": "Contributo di connessione ARERA; tempi e lavori dipendono dal distributore"},
            {"opzione": f"Batteria da {costi['batteria']['kwh']:g} kWh / {costi['batteria']['kw']:g} kW",
             "costo_una_tantum_eur": costi["batteria"]["costo_eur"], "costo_annuo_eur": quota_var,
             "costo_energia_mancante_eur_anno": 0,
             "copertura_pct": oggi["copertura_pct"], "veicoli_serviti": oggi["veicoli_serviti"],
             "nota": "Copre la finestra critica piu' energivora; costo per kWh da sostituire con un'offerta"},
        ]
    # Mix rivisto: le colonnine scelte dall'ottimizzatore con la sede, sempre con il DLM sulla sede.
    con = (mix or {}).get("consigliato")
    # Entra tra le alternative solo se la sede chiede piu' infrastruttura di quella inserita;
    # se il consigliato costa meno, lo dice gia' la tabella del mix.
    mix_cambia = bool(con) and con["config"] != mix["inserito"]["config"] \
        and con["capex_eur"] > mix["inserito"]["capex_eur"]
    if opzioni and mix_cambia:
        vg = mix["verifica_giorni"]
        picco_mix = max(g["picco_aggiunto_dalla_flotta_kw"] for g in vg)
        mancante_mix = max(0.0, sum(g["costo_esterno_eur_anno"] for g in vg) / len(vg) - ext_oggi)
        opzioni.insert(1, {
            "opzione": f"Mix rivisto: {con['n_ac']} AC + {con['n_dc']} DC con DLM sulla sede",
            "costo_una_tantum_eur": round(con["capex_eur"] - mix["inserito"]["capex_eur"]),
            "costo_annuo_eur": round(picco_mix * t["quota_potenza_eur_kw_anno"]),
            "costo_energia_mancante_eur_anno": round(mancante_mix),
            "copertura_pct": min(g["copertura_pct"] for g in vg), "veicoli_serviti": None,
            "nota": "Una tantum = differenza di CAPEX rispetto al mix inserito; nessun lavoro sull'allaccio"})
    for o in opzioni:
        o["costo_5_anni_eur"] = round(o["costo_una_tantum_eur"]
                                      + 5 * (o["costo_annuo_eur"] + o["costo_energia_mancante_eur_anno"]))
    # Confronto alla pari: la piu' economica su 5 anni, contando anche l'energia non caricata.
    opzioni.sort(key=lambda o: o["costo_5_anni_eur"])

    best = opzioni[0] if opzioni else None
    if int_oggi["minuti_oltre_contratto"] == 0:
        esito = "La flotta sta nel contratto anche con i consumi della sede"
    elif best["opzione"].startswith("Limite di potenza variabile"):
        if best["copertura_pct"] >= oggi["copertura_pct"] - 0.5:
            esito = "Si sfora il contratto, ma basta un DLM che veda la sede: stessa copertura senza lavori"
        else:
            esito = ("Si sfora il contratto, ma basta un DLM che veda la sede: qualche auto esce non piena "
                     "ed e' comunque la soluzione piu' economica")
    elif best["opzione"].startswith("Mix rivisto"):
        esito = ("Si sfora il contratto: con un mix AC/DC diverso e un DLM che veda la sede "
                 "si resta nel contratto senza aumentare la potenza")
    elif best["opzione"].startswith("Aumento"):
        esito = ("Si sfora il contratto: il DLM da solo lascia troppe auto non piene, "
                 "conviene aumentare la potenza")
    else:
        esito = "Si sfora il contratto: conviene un accumulo che copra la finestra critica"

    serie = [{"ora": dg._ora(s), "sede_kw": round(sede_giorno[s], 1), "flotta_oggi_kw": round(flotta_oggi[s], 1),
              "flotta_limite_variabile_kw": round(flotta_var[s], 1), "libera_kw": libera[s]}
             for s in range(dg.SLOT_GIORNO)]

    ipotesi = list(sede["note"])
    ipotesi.append(f"Giorno di riferimento: {dg.GIORNI[g_rif]}, il feriale con il picco totale "
                   "(sede + flotta) piu' alto della settimana")
    ipotesi.append(f"Ogni feriale e' simulato a se': partenze, rientri, presenze e numero di giri cambiano "
                   f"da un giorno all'altro e i km variano fino a +/-{req.variazione_km_pct:g}%, quindi "
                   "cambiano lo stato di carica al rientro e la ricarica; sabato e domenica la flotta non ricarica")
    if mix:
        ipotesi.append(f"Mix consigliato: ottimizzatore AC-first del dimensionamento sul {dg.GIORNI[g_rif]} con il "
                       f"limite contratto - sede, budget massimo {req.budget_max_eur:,.0f} EUR, poi verificato "
                       "su tutti i feriali".replace(",", "."))
    ipotesi.append("Energia non caricata: il motore la valorizza come ricarica pubblica (sovrapprezzo "
                   "rispetto a quella aziendale) piu' il tempo del personale, su 365 giorni come nel TCO del tool")
    ipotesi.append("Ogni giorno parte dallo stato di carica iniziale della policy: il residuo del giorno "
                   "prima non viene riportato")
    if not any(v is not None for v in req.tariffe.model_dump().values()):
        ipotesi += dg.FONTI_TARIFFE
    ipotesi.append(f"Accumulo: {t['costo_batteria_eur_kwh']:g} EUR/kWh installato, ipotesi Y35 da sostituire con un'offerta")

    return {
        "esito": esito,
        "livello": sede["livello"],
        "affidabilita": dg.AFFIDABILITA[sede["livello"]],
        "potenza_contrattuale_kw": contratto,
        "giorno_riferimento": dg.GIORNI[g_rif],
        "curva_pod": sede["curva"],
        "come_oggi": {"copertura_pct": oggi["copertura_pct"], "veicoli_serviti": oggi["veicoli_serviti"],
                      "veicoli_totali": oggi["veicoli_totali"], "capex_eur": oggi["capex_eur"], **int_oggi},
        "limite_variabile": {"copertura_pct": var["copertura_pct"], "veicoli_serviti": var["veicoli_serviti"],
                             "veicoli_totali": var["veicoli_totali"], **int_var},
        "settimana": {**{k: int_sett[k] for k in ("picco_totale_kw", "picco_totale_quando", "minuti_oltre_contratto",
                                                  "energia_extra_kwh")},
                      "giorni_critici": dg.riepilogo_giorni(int_sett["finestre_critiche"])},
        "giorni": riepilogo,
        "mix": mix,
        "capacita": {"energia_libera_finestra_kwh": e_libera, "kwh_per_veicolo": round(kwh_veicolo, 1),
                     "veicoli_teorici_max": veicoli_teorici,
                     "nota": "Limite energetico teorico: quanti veicoli come quelli simulati entrano nei kWh "
                             "liberi della finestra; il numero reale dipende da orari e colonnine"},
        "costi": {k: v for k, v in costi.items() if k != "tariffe_usate"},
        "tariffe": t,
        "opzioni": opzioni,
        "serie_giorno": serie,
        "settimana_sede_kw": [round(x, 1) for x in settimana],
        "settimana_flotta_kw": [round(x, 1) for x in flotta_sett],
        "ipotesi": ipotesi,
    }
