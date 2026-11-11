"""Report informativo dal nome dell'azienda.

Flusso: nome dell'azienda -> sedi trovate su OpenStreetMap -> per la sede
scelta raccoglie i dati che OSM offre (parcheggi, servizi, fermate, colonnine
esistenti, strade, comune) e, dove un dato manca, usa un valore stimato
dichiarato. Il report riporta:

  - minimo di punti di ricarica DM 28/10/2025 (compliance_dm2025, invariato)
    per posti bassi / centrali / alti e per i tre tipi di intervento;
  - punteggio del sito (site_scoring, invariato) con valore centrale e forbice;
  - per ogni dato la fonte: "osm" (trovato), "stima" (valore stimato con
    metodo dichiarato), "ipotesi" (valore di default), "utente" (corretto).

Le funzioni di calcolo sono pure (testabili senza rete); la rete sta in
cerca_sedi() e raccogli_dati().
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import asdict
from datetime import date
from typing import Optional

from josa_core import parcheggi_osm as osm
from josa_core.compliance_dm2025 import TIPO_INTERVENTO_VALIDI, BuildingProfile, compute_dm2025
from josa_core.site_scoring import CriterioInput, PESI_DEFAULT, compute_site_score, _grado_da_punteggio

# ---------------------------------------------------------------- valori stimati

# Superficie lorda per addetto (m2) per tipo di edificio: ordini di grandezza
# usati nella pianificazione urbanistica (manifattura ~100-150, logistica ~150,
# uffici ~25, commercio ~40). Servono solo a stimare i posti quando OSM non ha
# i parcheggi mappati.
M2_PER_ADDETTO = {"industriale": 120.0, "logistica": 150.0, "uffici": 25.0, "commerciale": 40.0, "altro": 60.0}
PIANI_DEFAULT = {"industriale": 1, "logistica": 1, "uffici": 3, "commerciale": 1, "altro": 2}
COPERTURA_LOTTO = 0.35     # quota del lotto coperta da edifici, se OSM da' solo l'area industriale
QUOTA_AUTO = 0.7           # addetti che arrivano in auto (circa 7 su 10, pendolarismo ISTAT)
FORBICE_POSTI = (0.5, 1.5) # basso / alto rispetto al valore centrale stimato

# Traffico giornaliero tipico per classe di strada (veicoli/giorno, ordini di grandezza)
TRAFFICO_PER_STRADA = {"motorway": 40000, "trunk": 25000, "primary": 15000, "secondary": 8000,
                       "tertiary": 4000, "altro": 1500}
VISIBILITA_PER_STRADA = {"motorway": "alta", "trunk": "alta", "primary": "alta", "secondary": "media",
                         "tertiary": "media", "altro": "bassa"}
NOME_STRADA = {"motorway": "autostrada", "trunk": "superstrada", "primary": "strada statale/principale",
               "secondary": "strada provinciale/secondaria", "tertiary": "strada di collegamento",
               "altro": "strada locale"}
_VIS_ORDINE = ["bassa", "media", "alta"]

POTENZA_IPOTESI_KW = (20.0, 50.0, 250.0)  # basso, centrale, alto

TIPI_SEDE = {
    ("building", "industrial"): "industriale", ("building", "factory"): "industriale",
    ("building", "manufacture"): "industriale", ("man_made", "works"): "industriale",
    ("landuse", "industrial"): "industriale", ("building", "warehouse"): "logistica",
    ("building", "office"): "uffici", ("building", "commercial"): "uffici",
    ("landuse", "commercial"): "uffici", ("building", "retail"): "commerciale",
    ("landuse", "retail"): "commerciale", ("building", "yes"): "altro",
}
CATEGORIE_UFFICIO = {"office"}


def tipo_sede(categoria: str, tipo: str) -> Optional[str]:
    if categoria in CATEGORIE_UFFICIO:
        return "uffici"
    return TIPI_SEDE.get((categoria, tipo))


def stima_posti_da_edificio(area_m2: float, tipo: str, e_lotto: bool, piani: Optional[int]) -> dict:
    """Posti auto stimati dalla superficie su OSM: superficie lorda -> addetti -> auto."""
    tipo = tipo if tipo in M2_PER_ADDETTO else "altro"
    impronta = area_m2 * (COPERTURA_LOTTO if e_lotto else 1.0)
    n_piani = piani or PIANI_DEFAULT[tipo]
    slp = impronta * n_piani
    addetti = slp / M2_PER_ADDETTO[tipo]
    centrale = max(0, round(addetti * QUOTA_AUTO))
    basso, alto = (max(0, round(centrale * f)) for f in FORBICE_POSTI)
    metodo = (f"{'lotto ' + format(round(area_m2), ',').replace(',', '.') + ' m² × ' + str(COPERTURA_LOTTO).replace('.', ',') + ' coperto' if e_lotto else 'edificio ' + format(round(area_m2), ',').replace(',', '.') + ' m²'}"
              f" × {n_piani} pian{'o' if n_piani == 1 else 'i'}{'' if piani else ' (ipotesi)'}"
              f" ÷ {M2_PER_ADDETTO[tipo]:.0f} m² per addetto ({tipo}) ≈ {addetti:.0f} addetti × {QUOTA_AUTO:.0%} in auto".replace('.0%', '%'))
    return {"basso": basso, "centrale": centrale, "alto": alto, "addetti": round(addetti), "metodo": metodo}


def addetti_in_sede(sede: dict, profilo: Optional[dict]) -> Optional[dict]:
    """Addetti nella sede dal profilo web: dato della sede se c'e', altrimenti i
    dipendenti totali (una sola sede) o una forbice (piu' sedi)."""
    if sede.get("dipendenti_sede"):
        n = int(sede["dipendenti_sede"])
        return {"basso": n, "centrale": n, "alto": n, "testo": f"{n} addetti in questa sede (web)"}
    if not profilo or not profilo.get("dipendenti"):
        return None
    tot = int(profilo["dipendenti"])
    n_sedi = max(1, len(profilo.get("sedi") or []))
    if n_sedi == 1:
        return {"basso": tot, "centrale": tot, "alto": tot, "testo": f"{tot} dipendenti dell'azienda (web), una sola sede"}
    return {"basso": max(1, round(tot / n_sedi)), "centrale": max(1, round(tot / n_sedi)), "alto": tot,
            "testo": f"{tot} dipendenti (web) su {n_sedi} sedi: tra {max(1, round(tot / n_sedi))} e {tot} in questa sede"}


def posti_da_addetti(add: dict) -> dict:
    """Posti = addetti in sede x quota che arriva in auto (forbice 50-90%)."""
    return {"basso": max(1, round(add["basso"] * 0.5)), "centrale": max(1, round(add["centrale"] * QUOTA_AUTO)),
            "alto": max(1, round(add["alto"] * 0.9)), "fonte": "stima",
            "spiegazione": f"Dai dipendenti: {add['testo']} × circa {QUOTA_AUTO:.0%} in auto (forbice 50-90%).".replace(".0%", "%")}


# Art. 41-sexies L. 1150/1942 (introdotto dalla L. 765/1967: 1 m² ogni 20 m³) come
# modificato dalla L. 122/1989 "Tognoli": nelle nuove costruzioni spazi per parcheggio
# non inferiori a 1 m² ogni 10 m³ di costruzione.
# Come si calcola il volume dipende dalla regione (definizioni tecniche uniformi regionali).
# Le norme del Comune (PGT, RUE/PUG) possono fissare regole diverse: il report lo dice.
REGOLE_VOLUME = {
    # D.G.R. XI/695/2018 Allegato B, DTU 44-45: volume urbanistico = SL x altezza urbanistica
    # fissata dal PGT; Milano (NTA PdR art. 5 c. 10, art. 12) e Rovato (NTA art. 12 c. 6) usano
    # 3,00 m per qualsiasi destinazione, produttivo compreso.
    "Lombardia": {"altezze": {"industriale": 3.0, "logistica": 3.0, "uffici": 3.0, "commerciale": 3.0, "altro": 3.0},
                  "testo": "Lombardia: volume urbanistico = superficie lorda × altezza urbanistica del PGT "
                           "(D.G.R. XI/695/2018, DTU 44-45); 3 m per ogni destinazione come a Milano e Rovato",
                  "convenzionale": True},
    # D.G.R. 922/2017 Allegato II, voce 26: volume totale = superficie di ogni piano x altezza lorda
    # reale (nessuna altezza convenzionale). Le dotazioni P3 del produttivo le fissa il RUE/PUG.
    "Emilia-Romagna": {"altezze": {"industriale": 7.0, "logistica": 9.0, "uffici": 3.3, "commerciale": 5.0, "altro": 3.3},
                       "testo": "Emilia-Romagna: volume totale = superficie di ogni piano × altezza lorda reale "
                                "(D.G.R. 922/2017, voce 26), qui stimata; il RUE/PUG del Comune fissa i parcheggi "
                                "pertinenziali (P3) del produttivo e può dare un numero diverso",
                       "convenzionale": False},
}
# Altre regioni: altezza convenzionale ("altezza virtuale", es. D.P.G.R. Toscana 39/R/2018 art. 24):
# 3,5 m per industriale, artigianale, commerciale e depositi, 3 m per gli altri usi.
ALTEZZA_PIANO_M = {"industriale": 3.5, "logistica": 3.5, "uffici": 3.0, "commerciale": 3.5, "altro": 3.0}
TESTO_NAZIONALE = ("regola nazionale con altezza convenzionale (3,5 m produttivo, 3 m altri usi, come la Toscana "
                   "D.P.G.R. 39/R/2018 art. 24); le norme del Comune possono fissare regole diverse")
M3_PER_M2_PARCHEGGIO = 10.0
_SIGLE_REGIONE = {
    "Lombardia": {"MI", "BG", "BS", "CO", "CR", "LC", "LO", "MN", "MB", "PV", "SO", "VA"},
    "Emilia-Romagna": {"BO", "FE", "FC", "MO", "PR", "PC", "RA", "RE", "RN"},
}


def regione_sede(sede: dict) -> Optional[str]:
    """Regione della sede (solo quelle con regole proprie), dall'indirizzo di OSM o dalla sigla."""
    testo = " ".join(str(sede.get(k) or "") for k in ("indirizzo", "provincia", "regione")).lower()
    for reg in REGOLE_VOLUME:
        if reg.lower() in testo or reg.lower().replace("-", " ") in testo:
            return reg
    sigla = str(sede.get("provincia") or "").strip().upper()
    for reg, sigle in _SIGLE_REGIONE.items():
        if sigla in sigle:
            return reg
    return None


def posti_41sexies(sede: dict, profilo: Optional[dict] = None) -> Optional[dict]:
    """Posti minimi di parcheggio per l'art. 41-sexies (1 m² ogni 10 m³), dal volume
    stimato dell'edificio secondo la regola della regione. E' il minimo urbanistico per gli
    edifici autorizzati dopo il 1989 (meta' per il 1967-1989); non dice quante auto arrivano."""
    tipo = sede.get("tipo_sede") if sede.get("tipo_sede") in ALTEZZA_PIANO_M else "altro"
    impronta, fonte = None, ""
    if sede.get("superficie_coperta_m2"):
        impronta, fonte = float(sede["superficie_coperta_m2"]), "superficie coperta dichiarata (web)"
    elif sede.get("area_m2") and not sede.get("e_lotto"):
        impronta, fonte = float(sede["area_m2"]), "impronta dell'edificio su OpenStreetMap"
    elif sede.get("area_m2"):
        impronta, fonte = float(sede["area_m2"]) * COPERTURA_LOTTO, f"area su OpenStreetMap × {COPERTURA_LOTTO:.0%} coperto".replace(".0%", "%")
    if not impronta:
        return None
    regione = regione_sede(sede)
    regola = REGOLE_VOLUME.get(regione)
    piani = sede.get("piani") or (PIANI_DEFAULT.get(tipo, 1) if tipo in ("uffici", "altro") else 1)
    if regola:
        altezza = regola["altezze"][tipo]
        if not regola["convenzionale"] and piani > 1 and tipo in ("industriale", "logistica"):
            altezza = 4.0  # edificio produttivo su più piani: ~4 m a piano
        testo_regola, tipo_altezza = regola["testo"], ("altezza convenzionale" if regola["convenzionale"] else "altezza lorda stimata")
    else:
        altezza, testo_regola, tipo_altezza = ALTEZZA_PIANO_M[tipo], TESTO_NAZIONALE, "altezza convenzionale"
    volume = impronta * piani * altezza
    area_park = volume / M3_PER_M2_PARCHEGGIO
    posti = max(1, round(area_park / osm.M2_PER_POSTO))
    return {
        "posti": posti, "posti_1967_1989": max(1, round(posti / 2)), "volume_m3": round(volume),
        "area_parcheggio_m2": round(area_park), "regione": regione, "regola": testo_regola,
        "metodo": (f"{fonte}: {format(round(impronta), ',').replace(',', '.')} m² × {piani} pian{'o' if piani == 1 else 'i'} × "
                   f"{('%g' % altezza).replace('.', ',')} m ({tipo_altezza}) ≈ {format(round(volume), ',').replace(',', '.')} m³ ÷ 10 = "
                   f"{format(round(area_park), ',').replace(',', '.')} m² di parcheggio ÷ {osm.M2_PER_POSTO:.0f} m² a posto"),
    }


def posti_con_forbice(posti_osm_inclusi: int, posti_osm_da_confermare: int, stima_edificio: Optional[dict]) -> dict:
    """Combina i posti dei parcheggi OSM con la stima dall'edificio.
    Con parcheggi mappati il valore alto e' +50% (parcheggi non mappati), fino al
    doppio se la stima dall'edificio e' piu' alta."""
    stima_c = stima_edificio["centrale"] if stima_edificio else 0
    if posti_osm_inclusi > 0:
        n = posti_osm_inclusi
        alto = round(max(n * 1.5, min(stima_c, n * 2)))
        return {"basso": n, "centrale": n, "alto": alto, "fonte": "osm",
                "spiegazione": f"{n} posti nei parcheggi aziendali o probabili mappati su OpenStreetMap; "
                               f"il valore alto considera parcheggi non mappati"
                               + (f" (stima dall'edificio: {stima_c})" if stima_edificio else "") + "."}
    if posti_osm_da_confermare > 0:
        n = posti_osm_da_confermare
        return {"basso": round(n * 0.5), "centrale": n, "alto": round(max(n * 1.5, min(stima_c, n * 2))), "fonte": "stima",
                "spiegazione": f"{n} posti in parcheggi privati vicini alla sede, non sicuramente aziendali: da confermare"
                               + (f" (stima dall'edificio: {stima_c})" if stima_edificio else "") + "."}
    if stima_edificio and stima_c > 0:
        return {**{k: stima_edificio[k] for k in ("basso", "centrale", "alto")}, "fonte": "stima",
                "spiegazione": f"Nessun parcheggio aziendale mappato. Stima dall'edificio: {stima_edificio['metodo']}."}
    return {"basso": 20, "centrale": 50, "alto": 100, "fonte": "ipotesi",
            "spiegazione": "Nessun parcheggio né edificio misurabile su OpenStreetMap: forbice di ipotesi 20-100 posti."}


# ---------------------------------------------------------------- DM con sensitivity

ETICHETTA_INTERVENTO = {"esistente": "Edificio esistente", "ristrutturazione_importante": "Ristrutturazione importante",
                        "nuova_costruzione": "Nuova costruzione"}


def _dm(posti: int, tipo: str, pmi: bool, oggi: date):
    return compute_dm2025(BuildingProfile(residenziale=False, posti_auto=int(posti), tipo_intervento=tipo,
                                          pmi_proprietaria_e_occupante=pmi), data_riferimento=oggi)


def dm_sensitivity(posti: dict, tipo_base: str = "esistente", pmi: Optional[bool] = None,
                   oggi: Optional[date] = None, pmi_dimensione: Optional[bool] = None) -> dict:
    """Minimo DM per posti basso/centrale/alto e per i tre tipi di intervento."""
    oggi = oggi or date.today()
    if pmi is True:
        r = _dm(posti["centrale"], tipo_base, True, oggi)
        return {"esente": True, "motivo_esenzione": r.motivo_esenzione, "tabella": [], "base": None,
                "disclaimer": r.disclaimer}
    tabella = []
    for tipo in ("esistente", "ristrutturazione_importante", "nuova_costruzione"):
        riga = {"tipo_intervento": tipo, "etichetta": ETICHETTA_INTERVENTO[tipo], "valori": {}}
        for k in ("basso", "centrale", "alto"):
            r = _dm(posti[k], tipo, False, oggi)
            riga["valori"][k] = {
                "posti": posti[k],
                "a_oggi": r.punti_tipologia_a_applicabili_oggi, "b_oggi": r.punti_tipologia_b_applicabili_oggi,
                "a_regime": r.punti_tipologia_a_minimi_a_regime, "b_regime": r.punti_tipologia_b_minimi_a_regime,
            }
        tabella.append(riga)
    base = _dm(posti["centrale"], tipo_base, False, oggi)
    note = []
    if posti["basso"] <= 100 < posti["alto"]:
        note.append("La forbice dei posti scavalca i 100: sopra i 100 posti il DM passa da 'per ogni 20' a "
                    "'per ogni 50 posti' più i punti Tipologia B, quindi il minimo può cambiare a gradino.")
    if pmi is None and pmi_dimensione:
        note.append("Per dimensione è una PMI (dati dal web): se è proprietaria e occupante dell'edificio è esente "
                    "dagli obblighi (art. 4, comma 1-bis, lett. f, D.Lgs. 192/2005). Se è in affitto, valgono i numeri qui sopra.")
    elif pmi is None:
        note.append("Se l'azienda è una PMI proprietaria e occupante dell'edificio, è esente dagli obblighi "
                    "(art. 4, comma 1-bis, lett. f, D.Lgs. 192/2005). Dal solo nome non si può sapere.")
    return {
        "esente": False, "motivo_esenzione": None, "tabella": tabella,
        "base": {"tipo_intervento": tipo_base, "posti": posti["centrale"],
                 "a_oggi": base.punti_tipologia_a_applicabili_oggi, "b_oggi": base.punti_tipologia_b_applicabili_oggi,
                 "a_regime": base.punti_tipologia_a_minimi_a_regime, "b_regime": base.punti_tipologia_b_minimi_a_regime,
                 "fase_transitoria": base.fase_transitoria, "note_tecniche": base.note_tecniche,
                 "fonti_da_verificare": base.fonti_da_verificare},
        "note": note, "disclaimer": base.disclaimer,
    }


# ---------------------------------------------------------------- punteggio con forbice

def _vis_sposta(v: str, d: int) -> str:
    i = _VIS_ORDINE.index(v) if v in _VIS_ORDINE else 1
    return _VIS_ORDINE[max(0, min(2, i + d))]


def input_punteggio(dati: dict, posti: dict, override: Optional[dict] = None) -> dict:
    """Valori basso/centrale/alto (nel senso del punteggio: peggiore/centrale/migliore)
    per i nove criteri, con fonte e spiegazione."""
    override = override or {}
    strada = dati.get("strada_classe") or "altro"
    traffico = TRAFFICO_PER_STRADA.get(strada, TRAFFICO_PER_STRADA["altro"])
    vis = VISIBILITA_PER_STRADA.get(strada, "bassa")
    serv = int(dati.get("n_servizi_300m") or 0)
    d_tp = dati.get("distanza_trasporto_km")
    d_cp = dati.get("distanza_colonnina_km")
    d_art = dati.get("distanza_arteria_km")
    dens_ab = dati.get("densita_abitanti_km2")
    industriale = dati.get("tipo_sede") in ("industriale", "logistica")
    pot = override.get("potenza_kw")

    v = {
        "traffico": {"peggiore": traffico * 0.5, "centrale": traffico, "migliore": traffico * 1.5, "fonte": "stima",
                     "spiegazione": f"Dal tipo di strada più vicina ({NOME_STRADA.get(strada, strada)}): circa {format(traffico, ',').replace(',', '.')} veicoli/giorno"},
        "accesso_facile": {"peggiore": False, "centrale": True, "migliore": True, "fonte": "ipotesi",
                           "spiegazione": "Ingresso agevole assunto: da verificare in sopralluogo"},
        "distanza_arteria_km": ({"peggiore": d_art, "centrale": d_art, "migliore": d_art, "fonte": "osm",
                                 "spiegazione": f"{d_art:.1f} km dalla strada principale o casello più vicino (OSM)"}
                                if d_art is not None else
                                {"peggiore": 5.0, "centrale": 2.0, "migliore": 0.5, "fonte": "ipotesi",
                                 "spiegazione": "Nessuna strada principale trovata nei dintorni su OSM"}),
        "densita_abitanti_km2": ({"peggiore": dens_ab, "centrale": dens_ab, "migliore": dens_ab, "fonte": "osm",
                                  "spiegazione": f"{format(round(dens_ab), ',').replace(',', '.')} ab/km² nel comune ({dati.get('comune', '')}, OSM)"}
                                 if dens_ab is not None else
                                 {"peggiore": 200, "centrale": 800, "migliore": 2000, "fonte": "ipotesi",
                                  "spiegazione": "Popolazione del comune non disponibile su OSM"}),
        "densita_aziende_km2": ({"peggiore": 20, "centrale": 60, "migliore": 150, "fonte": "ipotesi",
                                 "spiegazione": "Sede in area produttiva: densità di aziende assunta 20-150/km²"}
                                if industriale else
                                {"peggiore": 10, "centrale": 30, "migliore": 60, "fonte": "ipotesi",
                                 "spiegazione": "Densità di aziende non disponibile: assunta 10-60/km²"}),
        "n_servizi_300m": {"peggiore": serv, "centrale": serv, "migliore": round(serv * 1.5) + 1, "fonte": "osm",
                           "spiegazione": f"{serv} tra bar, ristoranti e supermercati entro 300 m su OSM (OSM può non averli tutti)"},
        "potenza_disponibile_kw": ({"peggiore": pot, "centrale": pot, "migliore": pot, "fonte": "utente",
                                    "spiegazione": f"{pot:.0f} kW indicati da te"}
                                   if pot is not None else
                                   {"peggiore": POTENZA_IPOTESI_KW[0], "centrale": POTENZA_IPOTESI_KW[1],
                                    "migliore": POTENZA_IPOTESI_KW[2], "fonte": "ipotesi",
                                    "spiegazione": "Potenza libera senza potenziamento non nota: ipotesi 50 kW (forbice 20-250 kW). Va chiesta al distributore"}),
        "posti_parcheggio_disponibili": {"peggiore": posti["basso"], "centrale": posti["centrale"], "migliore": posti["alto"],
                                         "fonte": posti["fonte"], "spiegazione": posti["spiegazione"]},
        "distanza_trasporto_pubblico_km": ({"peggiore": d_tp, "centrale": d_tp, "migliore": round(d_tp * 0.5, 2), "fonte": "osm",
                                            "spiegazione": f"Fermata più vicina su OSM a {d_tp:.1f} km"}
                                           if d_tp is not None else
                                           {"peggiore": 3.0, "centrale": 2.5, "migliore": 1.0, "fonte": "ipotesi",
                                            "spiegazione": "Nessuna fermata entro 2 km su OSM"}),
        "distanza_competitor_km": ({"peggiore": d_cp, "centrale": d_cp, "migliore": round(d_cp * 0.5, 2), "fonte": "osm",
                                    "spiegazione": f"Colonnina pubblica più vicina su OSM a {d_cp:.1f} km (OSM può non averle tutte)"}
                                   if d_cp is not None else
                                   {"peggiore": 10.0, "centrale": 10.0, "migliore": 5.0, "fonte": "ipotesi",
                                    "spiegazione": "Nessuna colonnina entro 10 km su OSM"}),
        "visibilita": {"peggiore": _vis_sposta(vis, -1), "centrale": vis, "migliore": _vis_sposta(vis, +1), "fonte": "stima",
                       "spiegazione": f"Dal tipo di strada più vicina ({NOME_STRADA.get(strada, strada)})"},
    }
    return v


_CRITERIO_INPUT = {
    "traffico": ["traffico"], "accessibilita": ["accesso_facile", "distanza_arteria_km"],
    "demografia": ["densita_abitanti_km2", "densita_aziende_km2"], "servizi": ["n_servizi_300m"],
    "connessione_rete": ["potenza_disponibile_kw"], "parcheggio": ["posti_parcheggio_disponibili"],
    "trasporto_pubblico": ["distanza_trasporto_pubblico_km"], "distanza_competitor": ["distanza_competitor_km"],
    "visibilita": ["visibilita"],
}
_CHIAVI_CRITERI = ["traffico", "accessibilita", "demografia", "servizi", "connessione_rete", "parcheggio",
                   "trasporto_pubblico", "distanza_competitor", "visibilita"]


def _criterio_input(vals: dict, scenario: str) -> CriterioInput:
    g = lambda k: vals[k][scenario]
    return CriterioInput(
        traffico_veicoli_giorno=float(g("traffico")), accesso_facile=bool(g("accesso_facile")),
        distanza_arteria_km=float(g("distanza_arteria_km")), densita_abitanti_km2=float(g("densita_abitanti_km2")),
        densita_aziende_km2=float(g("densita_aziende_km2")), n_servizi_300m=int(g("n_servizi_300m")),
        potenza_disponibile_kw=float(g("potenza_disponibile_kw")),
        posti_parcheggio_disponibili=int(g("posti_parcheggio_disponibili")),
        distanza_trasporto_pubblico_km=float(g("distanza_trasporto_pubblico_km")),
        distanza_competitor_km=float(g("distanza_competitor_km")), visibilita=str(g("visibilita")),
    )


def punteggio_con_forbice(vals: dict) -> dict:
    """Punteggio centrale e forbice: per ogni criterio il minimo e il massimo dei
    punti sui tre scenari (il calcolo resta quello di site_scoring)."""
    ris = {s: compute_site_score(_criterio_input(vals, s), e_deposito_aziendale=True)
           for s in ("peggiore", "centrale", "migliore")}
    criteri = []
    tot_min = tot_max = 0.0
    for i, chiave in enumerate(_CHIAVI_CRITERI):
        punti = [ris[s].criteri[i].punteggio_0_100 for s in ris]
        c = ris["centrale"].criteri[i]
        p_min, p_max = min(punti), max(punti)
        tot_min += p_min * c.peso
        tot_max += p_max * c.peso
        fonti = {vals[k]["fonte"] for k in _CRITERIO_INPUT[chiave]}
        fonte = next((f for f in ("ipotesi", "stima", "osm", "utente") if f in fonti), "osm")
        criteri.append({
            "chiave": chiave, "nome": c.nome, "peso": c.peso, "punteggio": c.punteggio_0_100,
            "min": p_min, "max": p_max, "fonte": fonte,
            "spiegazione": " · ".join(vals[k]["spiegazione"] for k in _CRITERIO_INPUT[chiave]),
        })
    centrale = ris["centrale"]
    return {
        "punteggio": centrale.punteggio_totale_0_100, "grado": centrale.grado,
        "min": round(tot_min, 1), "max": round(tot_max, 1),
        "grado_min": _grado_da_punteggio(tot_min), "grado_max": _grado_da_punteggio(tot_max),
        "criteri": criteri, "nota_contesto": centrale.nota_contesto,
        "input_centrali": asdict(_criterio_input(vals, "centrale")),
    }


def calcola_report(sede: dict, dati: dict, override: Optional[dict] = None, oggi: Optional[date] = None,
                   profilo: Optional[dict] = None) -> dict:
    """Report completo da dati gia' raccolti (nessuna chiamata di rete).
    profilo: dati dell'azienda trovati sul web (dipendenti, fatturato, sedi), se disponibili."""
    override = override or {}
    stima = dati.get("stima_edificio")
    if override.get("posti") is not None:
        n = int(override["posti"])
        posti = {"basso": n, "centrale": n, "alto": n, "fonte": "utente", "spiegazione": f"{n} posti indicati da te."}
    else:
        osm_inc = int(dati.get("posti_osm_inclusi") or 0)
        add = addetti_in_sede(sede, profilo)
        if add and osm_inc == 0:
            posti = posti_da_addetti(add)
        else:
            posti = posti_con_forbice(osm_inc, int(dati.get("posti_osm_da_confermare") or 0), stima)
            if add:  # parcheggi mappati: i dipendenti allargano la forbice in alto
                pa = posti_da_addetti(add)
                posti["alto"] = max(posti["alto"], pa["alto"])
                posti["spiegazione"] += f" Dai dipendenti: circa {pa['centrale']} posti."
    tipo = override.get("tipo_intervento") or "esistente"
    if tipo not in TIPO_INTERVENTO_VALIDI:
        raise ValueError(f"tipo_intervento deve essere uno di {TIPO_INTERVENTO_VALIDI}")
    pmi = override.get("pmi")  # True / False / None (non so)
    from josa_core.ricerca_web import classe_pmi
    pmi_dimensione = classe_pmi(profilo)
    if pmi is None and pmi_dimensione is False:
        pmi = False  # grande impresa: l'esenzione PMI non si applica
    vals = input_punteggio(dati, posti, override)
    punteggio = punteggio_con_forbice(vals)
    dm = dm_sensitivity(posti, tipo, pmi, oggi, pmi_dimensione)
    urb = posti_41sexies(sede, profilo)
    if urb and not dm["esente"]:
        r = _dm(urb["posti"], tipo, False, oggi or date.today())
        urb.update({"a_oggi": r.punti_tipologia_a_applicabili_oggi, "b_oggi": r.punti_tipologia_b_applicabili_oggi,
                    "a_regime": r.punti_tipologia_a_minimi_a_regime, "b_regime": r.punti_tipologia_b_minimi_a_regime})
    dm["urbanistica"] = urb

    da_confermare = []
    if posti["fonte"] != "utente" and posti["fonte"] != "osm":
        da_confermare.append({"chiave": "posti", "testo": "Numero di posti auto del parcheggio aziendale"})
    if pmi is None:
        da_confermare.append({"chiave": "pmi", "testo": "Se l'azienda è proprietaria e occupante dell'edificio (esenzione PMI)"
                              if pmi_dimensione else "Se l'azienda è una PMI proprietaria e occupante dell'edificio (esenzione)"})
    if not override.get("tipo_intervento"):
        da_confermare.append({"chiave": "tipo_intervento", "testo": "Tipo di intervento: edificio esistente, ristrutturazione importante o nuova costruzione"})
    if override.get("potenza_kw") is None:
        da_confermare.append({"chiave": "potenza_kw", "testo": "Potenza disponibile senza potenziamento (dal distributore)"})
    if sede.get("approssimata"):
        da_confermare.insert(0, {"chiave": "posizione", "testo": "Posizione esatta della sede: l'azienda non è su "
                                 "OpenStreetMap, il report usa il punto dell'indirizzo e le distanze sono indicative"})
    return {
        "sede": sede, "posti": posti, "dm": dm, "profilo_web": profilo, "pmi_dimensione": pmi_dimensione, "punteggio": punteggio, "da_confermare": da_confermare,
        "dati_osm": {k: v for k, v in dati.items() if k != "stima_edificio"}, "stima_edificio": stima,
        "attribuzione": osm.ATTRIBUZIONE,
    }


# ---------------------------------------------------------------- rete

_CAT_SEDE_OK = {"building", "landuse", "man_made", "office", "industrial"}


def _haversine_km(a: tuple, b: tuple) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _viewbox(lat: float, lon: float, raggio_m: float) -> str:
    dlat = raggio_m / 111_320.0
    dlon = raggio_m / (111_320.0 * math.cos(math.radians(lat)))
    return f"{lon - dlon},{lat + dlat},{lon + dlon},{lat - dlat}"


def _area_geojson(g: dict, lat: float, lon: float) -> float:
    proj = osm._proiettore(lat, lon)
    poly = osm._geojson_a_poly(g or {}, proj)
    return osm.area_m2(poly) if len(poly) >= 3 else 0.0


def _int_o_none(v) -> Optional[int]:
    try:
        n = int(str(v).split(";")[0].strip())
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def candidato_da_nominatim(e: dict, nome: str) -> Optional[dict]:
    """Trasforma un risultato Nominatim in una sede candidata (o None se non e' una sede)."""
    cat, typ = e.get("category", ""), e.get("type", "")
    if cat not in _CAT_SEDE_OK:
        return None
    ts = tipo_sede(cat, typ)
    if ts is None:
        return None
    tags = {"name": e.get("name") or e.get("display_name", ""), **(e.get("extratags") or {})}
    if not osm._nome_corrisponde(tags, nome):
        return None
    lat, lon = float(e["lat"]), float(e["lon"])
    addr = e.get("address") or {}
    comune = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or ""
    return {
        "id": f"{e.get('osm_type', '')}/{e.get('osm_id', '')}", "nome": e.get("name") or nome,
        "indirizzo": e.get("display_name", ""), "comune": comune, "provincia": addr.get("county", ""),
        "lat": lat, "lon": lon, "categoria": cat, "tipo_osm": typ, "tipo_sede": ts,
        "e_lotto": cat == "landuse", "area_m2": round(_area_geojson(e.get("geojson"), lat, lon)),
        "piani": _int_o_none((e.get("extratags") or {}).get("building:levels")),
    }


def cerca_sedi(nome: str, comune: Optional[str] = None, limite: int = 8) -> list[dict]:
    nome = (nome or "").strip()
    if len(nome) < 3:
        raise ValueError("Scrivi almeno 3 caratteri del nome dell'azienda.")
    q = f"{nome} {comune.strip()}" if comune and comune.strip() else nome
    res = osm._nominatim("search", {"q": q, "countrycodes": "it", "limit": 20, "polygon_geojson": 1,
                                    "extratags": 1, "addressdetails": 1})
    sedi, visti = [], set()
    for e in res:
        c = candidato_da_nominatim(e, nome)
        if not c or c["id"] in visti:
            continue
        # stesso stabilimento mappato due volte (edificio + area): tieni il primo entro 150 m
        if any(_haversine_km((c["lat"], c["lon"]), (s["lat"], s["lon"])) < 0.15 for s in sedi):
            continue
        visti.add(c["id"])
        sedi.append(c)
    return sedi[:limite]


def sede_da_indirizzo(nome: str, indirizzo: str) -> dict:
    """Sede dall'indirizzo quando l'azienda non e' mappata su OpenStreetMap. Se nel
    raggio di 300 m c'e' un edificio o un'area col nome dell'azienda, si usa quello."""
    g = osm.geocodifica(indirizzo)
    vicino = osm._nominatim("search", {"q": nome, "viewbox": _viewbox(g["lat"], g["lon"], 300.0), "bounded": 1,
                                       "limit": 5, "polygon_geojson": 1, "extratags": 1, "addressdetails": 1})
    for e in vicino:
        c = candidato_da_nominatim(e, nome)
        if c:
            return c
    testo = indirizzo.lower()
    tipo = "industriale" if any(k in testo for k in ("industrial", "z.i.", "zona ind", "artigian")) else "altro"
    return {
        "id": "", "nome": nome, "indirizzo": g["indirizzo_trovato"], "comune": "", "provincia": "",
        "lat": g["lat"], "lon": g["lon"], "categoria": "", "tipo_osm": "", "tipo_sede": tipo,
        "e_lotto": False, "area_m2": 0, "piani": None, "approssimata": True,
    }


def cerca_azienda(nome: str, comune: Optional[str] = None) -> dict:
    """Sedi da OpenStreetMap + profilo dal web (se ANTHROPIC_API_KEY e' impostata).
    Le sedi trovate sul web vengono geocodificate; se cadono vicino a una sede OSM
    la arricchiscono, altrimenti si aggiungono come sedi nuove."""
    from josa_core import ricerca_web
    try:
        sedi = cerca_sedi(nome, comune)
    except ValueError:
        raise
    except Exception:
        sedi = []
    for s in sedi:
        s["origine"] = "osm"
    profilo = ricerca_web.cerca_profilo(nome, comune)
    if profilo:
        for w in profilo["sedi"]:
            testo = ", ".join(x for x in (w["indirizzo"], w["comune"], w["provincia"]) if x)
            try:
                c = sede_da_indirizzo(nome, testo)
            except Exception:
                # via non trovata su OSM: si ripiega sul comune, posizione da confermare
                try:
                    c = sede_da_indirizzo(nome, ", ".join(x for x in (w["comune"], w["provincia"]) if x))
                    c["approssimata"] = True
                    c["via_non_trovata"] = True
                except Exception:
                    continue
            c["tipo_web"] = w["tipo"]
            c["provincia"] = c.get("provincia") or w["provincia"]
            c["superficie_coperta_m2"] = w.get("superficie_coperta_m2")
            tw = w["tipo"].lower()
            if c.get("tipo_sede") in (None, "altro"):
                c["tipo_sede"] = ("industriale" if "stabiliment" in tw or "produ" in tw else
                                  "logistica" if "magazzin" in tw else
                                  "uffici" if "uffic" in tw or "amministr" in tw else c.get("tipo_sede") or "altro")
            c["dipendenti_sede"] = w.get("dipendenti_sede")
            vicina = next((s for s in sedi if _haversine_km((s["lat"], s["lon"]), (c["lat"], c["lon"])) < 0.3), None)
            if vicina:
                vicina.update({"origine": "osm+web", "tipo_web": w["tipo"], "dipendenti_sede": w.get("dipendenti_sede"),
                               "superficie_coperta_m2": w.get("superficie_coperta_m2")})
                continue
            c["origine"] = "web"
            c["indirizzo"] = testo if not c.get("id") else c["indirizzo"]
            c["comune"] = c.get("comune") or w["comune"]
            if c.get("approssimata") and not c.get("via_non_trovata") and re.search(r"\d", w["indirizzo"] or ""):
                c["approssimata"] = False  # indirizzo con numero civico: posizione affidabile
            sedi.append(c)
    return {"sedi": sedi[:10], "profilo_web": profilo, "ricerca_web_attiva": ricerca_web.disponibile()}


def _piu_vicino(lat: float, lon: float, query: str, raggio_m: float) -> Optional[tuple[float, dict]]:
    try:
        res = osm._nominatim("search", {"q": query, "viewbox": _viewbox(lat, lon, raggio_m), "bounded": 1, "limit": 50})
    except Exception:
        return None
    best = None
    for e in res:
        d = _haversine_km((lat, lon), (float(e["lat"]), float(e["lon"])))
        if best is None or d < best[0]:
            best = (d, e)
    return best


def _conta_entro(lat: float, lon: float, queries: list[str], raggio_m: float) -> int:
    ids = set()
    for q in queries:
        try:
            res = osm._nominatim("search", {"q": q, "viewbox": _viewbox(lat, lon, raggio_m), "bounded": 1, "limit": 50})
        except Exception:
            continue
        for e in res:
            if _haversine_km((lat, lon), (float(e["lat"]), float(e["lon"]))) * 1000 <= raggio_m:
                ids.add(f"{e.get('osm_type')}/{e.get('osm_id')}")
    return len(ids)


_cache_dati: dict = {}


def raccogli_dati(sede: dict, nome: str) -> dict:
    """Interroga OpenStreetMap (Nominatim) intorno alla sede scelta."""
    chiave = (round(sede["lat"], 5), round(sede["lon"], 5), nome.strip().lower(), sede.get("area_m2"))
    hit = _cache_dati.get(chiave)
    if hit and time.time() - hit[0] < 24 * 3600:
        return hit[1]
    lat, lon = sede["lat"], sede["lon"]
    dati: dict = {"tipo_sede": sede.get("tipo_sede")}

    # parcheggi (stesso modulo della card "Parcheggi della sede")
    try:
        p = osm.cerca_parcheggi(lat=lat, lon=lon, raggio_m=300.0, azienda=nome)
        dati["posti_osm_inclusi"] = int(p.get("posti_stimati") or 0)
        dati["posti_osm_da_confermare"] = sum((x.posti or 0) for x in p["parcheggi"] if x.tipo == "da_confermare")
        dati["parcheggi_trovati"] = len(p["parcheggi"])
    except Exception:
        dati["posti_osm_inclusi"] = 0
        dati["posti_osm_da_confermare"] = 0
        dati["parcheggi_trovati"] = None

    if sede.get("area_m2"):
        dati["stima_edificio"] = stima_posti_da_edificio(sede["area_m2"], sede.get("tipo_sede") or "altro",
                                                         bool(sede.get("e_lotto")), sede.get("piani"))

    dati["n_servizi_300m"] = _conta_entro(lat, lon, ["[restaurant]", "[cafe]", "[bar]", "[supermarket]"], 300.0)

    fermate = [x for x in (_piu_vicino(lat, lon, "[bus_stop]", 2000.0), _piu_vicino(lat, lon, "[station]", 2000.0)) if x]
    dati["distanza_trasporto_km"] = round(min(f[0] for f in fermate), 2) if fermate else None

    col = _piu_vicino(lat, lon, "[charging_station]", 10000.0)
    dati["distanza_colonnina_km"] = round(col[0], 2) if col else None

    # strada principale vicina: la classe piu' alta entro 500 m
    dati["strada_classe"] = "altro"
    for classe in ("motorway", "trunk", "primary", "secondary", "tertiary"):
        if _piu_vicino(lat, lon, f"[highway={classe}]", 500.0):
            dati["strada_classe"] = classe
            break
    # arteria: casello autostradale o strada principale piu' vicina
    cand = []
    if dati["strada_classe"] in ("motorway", "trunk", "primary"):
        cand.append(0.2)
    j = _piu_vicino(lat, lon, "[highway=motorway_junction]", 15000.0)
    if j:
        cand.append(j[0])
    if not cand:
        pr = _piu_vicino(lat, lon, "[highway=primary]", 3000.0)
        if pr:
            cand.append(pr[0])
    dati["distanza_arteria_km"] = round(min(cand), 1) if cand else None

    # comune: popolazione e superficie
    try:
        c = osm._nominatim("reverse", {"lat": lat, "lon": lon, "zoom": 10, "extratags": 1, "polygon_geojson": 1})
        pop = _int_o_none((c.get("extratags") or {}).get("population"))
        area_km2 = _area_geojson(c.get("geojson"), float(c["lat"]), float(c["lon"])) / 1e6
        dati["comune"] = c.get("name") or ""
        dati["densita_abitanti_km2"] = round(pop / area_km2) if pop and area_km2 > 0.1 else None
    except Exception:
        dati["comune"] = sede.get("comune", "")
        dati["densita_abitanti_km2"] = None

    _cache_dati[chiave] = (time.time(), dati)
    return dati
