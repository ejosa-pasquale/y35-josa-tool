"""
Diagnosi energetica della ricarica: la flotta simulata da Y35 dentro i consumi della sede.

Passi:
  1. profilo della sede dalla fonte migliore disponibile (curva POD > bolletta > dichiarato);
  2. giorno di riferimento: il feriale con piu' consumo della sede nella finestra di ricarica;
  3. simulazione "come oggi" (limite fisso p_shaving_kw) e integrazione con la sede:
     picchi, finestre critiche, energia extra;
  4. simulazione con limite variabile (contratto - sede in ogni quarto d'ora): quanta
     copertura resta se la ricarica non deve mai far sforare il contratto;
  5. capacita' libera, costo del picco e alternative con le tariffe ARERA di riferimento.

Il dimensionamento non cambia: si usano run_simulate e le funzioni di josa_core.diagnosi.
"""
from __future__ import annotations

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


def _giorno_riferimento(settimana: list[float], inizio_h: float, fine_h: float) -> int:
    """Feriale con piu' energia della sede nella finestra di ricarica (il piu' stretto)."""
    def energia(g):
        base = g * dg.SLOT_GIORNO
        return sum(settimana[base + (s % dg.SLOT_GIORNO)] for s in range(int(inizio_h * 4), int(fine_h * 4)))
    return max(range(5), key=energia)


def _giorno_flotta(timeline: list[float]) -> list[float]:
    """Curva della flotta riportata alle 24 ore: la ricarica oltre mezzanotte cade
    all'inizio del giorno dopo, che nella giornata tipo ripetuta e' lo stesso orario."""
    out = [0.0] * dg.SLOT_GIORNO
    for s, p in enumerate(timeline or []):
        out[s % dg.SLOT_GIORNO] += float(p)
    return out


def run_diagnosi(req: "schemas.DiagnosiRequest") -> dict:
    sede = profilo_sede(req.sede)
    settimana = sede["settimana"]
    contratto = float(req.sede.potenza_contrattuale_kw)
    inizio_h, fine_h = _finestra(req.gruppi)
    g_rif = _giorno_riferimento(settimana, inizio_h, fine_h)
    sede_giorno = settimana[g_rif * dg.SLOT_GIORNO:(g_rif + 1) * dg.SLOT_GIORNO]

    base = {k: getattr(req, k) for k in schemas.SimulateRequest.model_fields}
    base["gantt_settimanale"] = False

    # 1. Come oggi: la flotta vede solo il limite fisso p_shaving_kw.
    oggi = engine.run_simulate(schemas.SimulateRequest(**base))
    flotta_oggi = _giorno_flotta(oggi["timeline_p_kw"])
    int_oggi = dg.integra(sede_giorno, flotta_oggi, contratto, g_rif)

    # 2. Limite variabile: in ogni quarto d'ora al massimo contratto - sede.
    pol_var = req.policy.model_copy(update={"carico_sede_kw": [float(x) for x in sede_giorno],
                                            "potenza_contrattuale_kw": contratto})
    var = engine.run_simulate(schemas.SimulateRequest(**{**base, "policy": pol_var}))
    flotta_var = _giorno_flotta(var["timeline_p_kw"])
    int_var = dg.integra(sede_giorno, flotta_var, contratto, g_rif)

    # 3. Settimana: la flotta ricarica nei feriali, sabato e domenica restano alla sede.
    flotta_sett = []
    for g in range(7):
        flotta_sett += flotta_oggi if g < 5 else [0.0] * dg.SLOT_GIORNO
    int_sett = dg.integra(settimana, flotta_sett, contratto)

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
    opzioni = []
    if int_oggi["minuti_oltre_contratto"] > 0:
        opzioni = [
            {"opzione": "Limite di potenza variabile (il DLM vede i consumi della sede)",
             "costo_una_tantum_eur": 0, "costo_annuo_eur": quota_var,
             "copertura_pct": var["copertura_pct"], "veicoli_serviti": var["veicoli_serviti"],
             "nota": "Nessun lavoro sull'allaccio: serve un DLM collegato al contatore della sede"},
            {"opzione": f"Aumento di potenza di {costi['aumento_potenza']['kw']} kW",
             "costo_una_tantum_eur": costi["aumento_potenza"]["costo_una_tantum_eur"],
             "costo_annuo_eur": costi["aumento_potenza"]["costo_annuo_eur"],
             "copertura_pct": oggi["copertura_pct"], "veicoli_serviti": oggi["veicoli_serviti"],
             "nota": "Contributo di connessione ARERA; tempi e lavori dipendono dal distributore"},
            {"opzione": f"Batteria da {costi['batteria']['kwh']:g} kWh / {costi['batteria']['kw']:g} kW",
             "costo_una_tantum_eur": costi["batteria"]["costo_eur"], "costo_annuo_eur": quota_var,
             "copertura_pct": oggi["copertura_pct"], "veicoli_serviti": oggi["veicoli_serviti"],
             "nota": "Copre la finestra critica piu' energivora; costo per kWh da sostituire con un'offerta"},
        ]

    if int_oggi["minuti_oltre_contratto"] == 0:
        esito = "La flotta sta nel contratto anche con i consumi della sede"
    elif var["copertura_pct"] >= oggi["copertura_pct"] - 0.5:
        esito = "Si sfora il contratto, ma basta un DLM che veda la sede: stessa copertura senza lavori"
    else:
        esito = "Si sfora il contratto e il solo DLM non basta: serve potenza in piu' o un accumulo"

    serie = [{"ora": dg._ora(s), "sede_kw": round(sede_giorno[s], 1), "flotta_oggi_kw": round(flotta_oggi[s], 1),
              "flotta_limite_variabile_kw": round(flotta_var[s], 1), "libera_kw": libera[s]}
             for s in range(dg.SLOT_GIORNO)]

    ipotesi = list(sede["note"])
    ipotesi.append(f"Giorno di riferimento: {dg.GIORNI[g_rif]}, il feriale con piu' consumo della sede "
                   f"nella finestra di ricarica {inizio_h:g}-{fine_h:g}")
    ipotesi.append("Nella vista settimanale la flotta ricarica dal lunedi' al venerdi'")
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
        "capacita": {"energia_libera_finestra_kwh": e_libera, "kwh_per_veicolo": round(kwh_veicolo, 1),
                     "veicoli_teorici_max": veicoli_teorici,
                     "nota": "Limite energetico teorico: quanti veicoli come quelli simulati entrano nei kWh "
                             "liberi della finestra; il numero reale dipende da orari e colonnine"},
        "costi": {k: v for k, v in costi.items() if k != "tariffe_usate"},
        "tariffe": t,
        "opzioni": opzioni,
        "serie_giorno": serie,
        "settimana_sede_kw": [round(x, 1) for x in settimana],
        "ipotesi": ipotesi,
    }
