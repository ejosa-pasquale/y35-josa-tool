"""
Diagnosi energetica della ricarica — come la flotta si integra con i consumi della sede.

Il dimensionamento (simulation.py) produce la curva di potenza della flotta; questo
modulo la mette accanto al consumo della sede e al contratto del POD:
  - profilo della sede da tre fonti, in ordine di affidabilita':
      0. tipo di sede dichiarato (profilo tipico scalato sul picco dichiarato)
      1. bolletta: consumi per fascia F1/F2/F3 -> profilo tipico ricostruito
      2. curva quartoraria del POD (CSV del distributore o del fornitore)
  - capacita' libera per quarto d'ora (contratto - sede)
  - finestre critiche: quando sede + flotta superano il contratto, di quanto, per quanto
  - costo del picco e delle alternative, con tariffe ARERA dichiarate come ipotesi.

Nessuna funzione qui modifica il motore: lavora sulle curve che il motore restituisce.
Tutte le curve sono kW medi per slot di 15 minuti da mezzanotte: 96 valori per giorno,
672 per una settimana che parte da lunedi'.
"""

from __future__ import annotations

import csv
import io
import math
import re
from datetime import datetime, timedelta
from typing import Optional

SLOT_H = 0.25
SLOT_GIORNO = 96
SLOT_SETTIMANA = 672
GIORNI = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]

# Tariffe e costi di riferimento. Valori indicativi da aggiornare: finiscono sempre
# nella lista "ipotesi" del risultato, e il cliente puo' sostituirli con i suoi.
TARIFFE_ARERA = {
    # TIC 2026, Tabella 1, quota potenza connessioni permanenti in BT (delibera 575/2025/R/eel)
    "contributo_aumento_potenza_eur_kw": 78.81,
    # Quota potenza BTA (trasporto e misura 34,67 + oneri 19,24 EUR/kW/anno, IV trim. 2026).
    # Oltre 16,5 kW si applica alla potenza massima prelevata nel mese: ogni kW di picco
    # in piu' portato dalla flotta costa questa cifra ogni anno.
    "quota_potenza_eur_kw_anno": 53.91,
    # Ipotesi Y35 per un accumulo stazionario installato (da sostituire con un'offerta)
    "costo_batteria_eur_kwh": 500.0,
    "profondita_scarica_batteria": 0.9,
}
FONTI_TARIFFE = [
    "ARERA, delibera 575/2025/R/eel, Allegato C (TIC 2026), Tab. 1: quota potenza 78,81 EUR/kW",
    "Quota potenza BTA altri usi, IV trimestre 2026: 34,67 + 19,24 EUR/kW/anno (valori fino a 16,5 kW, usati come riferimento)",
]

AFFIDABILITA = {
    0: "Stima su dati dichiarati",
    1: "Stima da bolletta",
    2: "Dato reale (curva quartoraria del POD)",
}


# ---------------------------------------------------------------------------
# Fasce ARERA e profili tipici
# ---------------------------------------------------------------------------

def fascia(giorno: int, slot: int) -> str:
    """Fascia oraria ARERA di uno slot (festivita' non considerate).
    F1: lun-ven 8-19. F2: lun-ven 7-8 e 19-23, sabato 7-23. F3: il resto e la domenica."""
    h = slot / 4.0
    if giorno == 6:
        return "F3"
    if giorno == 5:
        return "F2" if 7 <= h < 23 else "F3"
    if 8 <= h < 19:
        return "F1"
    if 7 <= h < 8 or 19 <= h < 23:
        return "F2"
    return "F3"


# Forma oraria relativa (0-1) per giorno feriale, sabato e domenica.
_FORME = {
    "ufficio": (
        [0.25] * 7 + [0.5, 0.85, 0.95, 1.0, 1.0, 0.9, 0.85, 0.95, 0.95, 0.9, 0.8, 0.55, 0.35] + [0.28] * 4,
        [0.25] * 24,
        [0.22] * 24,
    ),
    "produzione_1_turno": (
        [0.2] * 6 + [0.6, 0.95, 1.0, 1.0, 1.0, 1.0, 0.8, 0.95, 1.0, 1.0, 0.7, 0.3] + [0.2] * 6,
        [0.2] * 24,
        [0.15] * 24,
    ),
    "produzione_2_turni": (
        [0.25] * 6 + [0.85, 1.0, 1.0, 1.0, 1.0, 1.0, 0.9, 0.95, 1.0, 1.0, 1.0, 1.0, 0.95, 0.9, 0.85, 0.5] + [0.25] * 2,
        [0.3] * 6 + [0.6] * 8 + [0.3] * 10,
        [0.2] * 24,
    ),
    "produzione_3_turni": (
        [0.85] * 6 + [0.95, 1.0, 1.0, 1.0, 1.0, 1.0, 0.95, 1.0, 1.0, 1.0, 1.0, 1.0, 0.95, 0.95, 0.9, 0.9, 0.85, 0.85],
        [0.7] * 24,
        [0.5] * 24,
    ),
    "logistica": (
        [0.45] * 5 + [0.7, 0.9, 1.0, 1.0, 0.95, 0.9, 0.9, 0.85, 0.9, 0.95, 0.95, 0.9, 0.8, 0.7, 0.6, 0.55, 0.5, 0.45, 0.45],
        [0.5] * 6 + [0.7] * 8 + [0.5] * 10,
        [0.35] * 24,
    ),
    "commerciale": (
        [0.3] * 7 + [0.5, 0.8, 0.95, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.95, 0.85, 0.5] + [0.3] * 3,
        [0.3] * 7 + [0.5, 0.8, 0.95, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.95, 0.85, 0.5] + [0.3] * 3,
        [0.3] * 24,
    ),
}
TIPI_SEDE = sorted(_FORME)


def _forma_settimana(tipo: str) -> list[float]:
    if tipo not in _FORME:
        raise ValueError(f"Tipo di sede sconosciuto: {tipo}. Valori ammessi: {', '.join(TIPI_SEDE)}")
    feriale, sabato, domenica = _FORME[tipo]
    out = []
    for g in range(7):
        ore = feriale if g < 5 else (sabato if g == 5 else domenica)
        for slot in range(SLOT_GIORNO):
            out.append(float(ore[slot // 4]))
    return out


def profilo_tipico(tipo: str, picco_kw: float) -> list[float]:
    """Livello 0: profilo settimanale tipico del settore, scalato sul picco dichiarato."""
    if picco_kw <= 0:
        raise ValueError("Il picco della sede deve essere maggiore di zero")
    forma = _forma_settimana(tipo)
    m = max(forma)
    return [round(picco_kw * x / m, 3) for x in forma]


def profilo_da_fasce(tipo: str, kwh_f1: float, kwh_f2: float, kwh_f3: float, giorni: int = 30) -> list[float]:
    """Livello 1: profilo settimanale ricostruito dai consumi per fascia di una bolletta.
    La forma e' quella tipica del settore; ogni fascia e' scalata in modo che l'energia
    della settimana ricostruita, riportata al periodo della bolletta, sia quella fatturata."""
    if giorni <= 0:
        raise ValueError("Il periodo della bolletta deve avere almeno un giorno")
    if min(kwh_f1, kwh_f2, kwh_f3) < 0 or (kwh_f1 + kwh_f2 + kwh_f3) <= 0:
        raise ValueError("Servono consumi per fascia non negativi e non tutti zero")
    forma = _forma_settimana(tipo)
    target = {"F1": kwh_f1 * 7.0 / giorni, "F2": kwh_f2 * 7.0 / giorni, "F3": kwh_f3 * 7.0 / giorni}
    energia_forma = {"F1": 0.0, "F2": 0.0, "F3": 0.0}
    fasce = []
    for i in range(SLOT_SETTIMANA):
        f = fascia(i // SLOT_GIORNO, i % SLOT_GIORNO)
        fasce.append(f)
        energia_forma[f] += forma[i] * SLOT_H
    fattore = {f: (target[f] / energia_forma[f]) if energia_forma[f] > 0 else 0.0 for f in target}
    return [round(forma[i] * fattore[fasce[i]], 3) for i in range(SLOT_SETTIMANA)]


# ---------------------------------------------------------------------------
# Import della curva quartoraria del POD
# ---------------------------------------------------------------------------

_FORMATI_DATA = ["%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                 "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
                 "%d.%m.%Y %H:%M"]
_FORMATI_GIORNO = ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%Y%m%d"]


def _num(s: str) -> Optional[float]:
    s = (s or "").strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _giorno(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    for f in _FORMATI_GIORNO:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def _istante(s: str) -> Optional[datetime]:
    s = (s or "").strip().replace("24:00", "23:59:59.999")
    s = re.sub(r"(\+\d\d:?\d\d|Z)$", "", s)
    if "23:59:59.999" in s:
        base = _istante(s.replace("23:59:59.999", "00:00"))
        return base + timedelta(days=1) if base else None
    for f in _FORMATI_DATA:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def _fattore_unita(intestazione: str, unita: str) -> tuple[float, str]:
    """Converte il valore letto in kW medi del quarto d'ora."""
    u = (unita or "auto").lower()
    h = (intestazione or "").lower()
    if u == "auto":
        if "kwh" in h:
            u = "kwh"
        elif re.search(r"\bwh\b", h):
            u = "wh"
        elif re.search(r"\bkw\b", h) or "potenza" in h:
            u = "kw"
        else:
            return 4.0, "Unita' non indicata nel file: valori letti come kWh per quarto d'ora"
    if u == "kwh":
        return 4.0, ""
    if u == "wh":
        return 4.0 / 1000.0, ""
    if u == "kw":
        return 1.0, ""
    raise ValueError("unita deve essere 'auto', 'kWh', 'Wh' o 'kW'")


def importa_curva_pod(testo: str, unita: str = "auto") -> dict:
    """Legge una curva quartoraria (CSV del distributore o del fornitore) e la riduce
    a una settimana tipo. Formati accettati:
      - lungo: una riga per quarto d'ora, con data/ora in una colonna (o data e ora in
        due colonne) e il valore;
      - largo: una riga per giorno, data nella prima colonna e 96 valori (92-100 nei
        giorni del cambio d'ora).
    Il valore puo' essere energia (kWh o Wh nel quarto d'ora) o potenza (kW)."""
    if not testo or not testo.strip():
        raise ValueError("File della curva vuoto")
    # Separatore: ';' o tab se presenti (la virgola puo' essere il separatore decimale).
    prime = "\n".join(testo.splitlines()[:5])
    sep = ";" if prime.count(";") else ("\t" if prime.count("\t") else ",")
    righe = [r for r in csv.reader(io.StringIO(testo), delimiter=sep) if any(c.strip() for c in r)]
    if len(righe) < 2:
        raise ValueError("La curva deve avere almeno due righe")

    avvertenze: list[str] = []
    punti: dict[datetime, float] = {}

    # Intestazione = righe prima della prima riga con una data; la colonna della data
    # puo' non essere la prima (es. POD in testa).
    def _col_data(r):
        for j, c in enumerate(r[:4]):
            if _giorno(c) is not None or _istante(c) is not None:
                return j
        return None

    i0 = 0
    while i0 < min(len(righe), 20) and _col_data(righe[i0]) is None:
        i0 += 1
    if i0 >= len(righe) or _col_data(righe[i0]) is None:
        raise ValueError("Nessuna riga con una data riconoscibile")
    jd = _col_data(righe[i0])
    intestazione = " ".join(" ".join(r) for r in righe[:i0])
    dati = [r[jd:] for r in righe[i0:] if len(r) > jd]

    fattore, nota = _fattore_unita(intestazione, unita)
    if nota:
        avvertenze.append(nota)

    numerici = [sum(1 for c in r[1:] if _num(c) is not None) for r in dati[:10]]
    largo = max(numerici) >= 90

    if largo:
        for r in dati:
            g = _giorno(r[0]) or _istante(r[0])
            if g is None:
                continue
            g = datetime(g.year, g.month, g.day)
            valori = [_num(c) for c in r[1:]]
            valori = [v for v in valori if v is not None]
            for k, v in enumerate(valori[:SLOT_GIORNO]):
                punti[g + timedelta(minutes=15 * k)] = v * fattore
    else:
        stampe = []
        for r in dati:
            t = _istante(r[0])
            idx_val = 1
            if t is None and len(r) > 2:
                t = _istante(f"{r[0].strip()} {r[1].strip()}")
                idx_val = 2
            if t is None:
                continue
            v = None
            for c in r[idx_val:]:
                v = _num(c)
                if v is not None:
                    break
            if v is None:
                continue
            stampe.append((t, v * fattore))
        if not stampe:
            raise ValueError("Nessun valore riconoscibile: servono data/ora e un numero per riga")
        # Convenzione "fine intervallo" (00:15 ... 24:00): si riporta all'inizio.
        primo = min(t for t, _ in stampe)
        fine_intervallo = "24:00" in testo or (primo.hour == 0 and primo.minute == 15)
        for t, v in stampe:
            if fine_intervallo:
                t = t - timedelta(minutes=15)
            t = t.replace(second=0, microsecond=0, minute=(t.minute // 15) * 15)
            punti[t] = v

    if not punti:
        raise ValueError("Nessun quarto d'ora letto dalla curva")

    per_giorno: dict = {}
    for t, v in punti.items():
        per_giorno.setdefault(t.date(), {})[t.hour * 4 + t.minute // 15] = max(0.0, v)
    completi = {d: s for d, s in per_giorno.items() if len(s) >= 90}
    incompleti = len(per_giorno) - len(completi)
    if incompleti:
        avvertenze.append(f"{incompleti} giorni con meno di 90 quarti d'ora sono stati esclusi")
    if not completi:
        raise ValueError("Nessun giorno completo nella curva (servono almeno 90 quarti d'ora al giorno)")

    campioni: list[list[list[float]]] = [[[] for _ in range(SLOT_GIORNO)] for _ in range(7)]
    for d, slots in completi.items():
        g = d.weekday()
        ultimo = 0.0
        for s in range(SLOT_GIORNO):
            v = slots.get(s, ultimo)
            ultimo = v
            campioni[g][s].append(v)
    mancano = [GIORNI[g] for g in range(7) if not campioni[g][0]]
    if mancano:
        avvertenze.append("Giorni della settimana assenti nella curva, stimati dal giorno simile: " + ", ".join(mancano))

    def _quantile(xs: list[float], q: float) -> float:
        ys = sorted(xs)
        if not ys:
            return 0.0
        k = (len(ys) - 1) * q
        a, b = int(math.floor(k)), int(math.ceil(k))
        return ys[a] + (ys[b] - ys[a]) * (k - a)

    def _settimana(q: Optional[float]) -> list[float]:
        out = []
        for g in range(7):
            sorgente = g
            if not campioni[g][0]:
                vicini = [x for x in ([0, 1, 2, 3, 4] if g < 5 else [5, 6]) if campioni[x][0]]
                vicini = vicini or [x for x in range(7) if campioni[x][0]]
                sorgente = vicini[0]
            for s in range(SLOT_GIORNO):
                xs = campioni[sorgente][s]
                val = (sum(xs) / len(xs)) if q is None else _quantile(xs, q)
                out.append(round(val, 3))
        return out

    t_picco, v_picco = max(((t, v) for t, v in punti.items() if t.date() in completi), key=lambda x: x[1])
    energia = sum(v for t, v in punti.items() if t.date() in completi) * SLOT_H
    date = sorted(completi)
    return {
        "giorni_letti": len(completi),
        "giorni_esclusi": incompleti,
        "periodo": {"da": date[0].isoformat(), "a": date[-1].isoformat()},
        "picco_kw": round(v_picco, 2),
        "picco_quando": t_picco.isoformat(timespec="minutes"),
        "energia_kwh": round(energia, 1),
        "energia_media_giorno_kwh": round(energia / len(completi), 1),
        "settimana_media_kw": _settimana(None),
        "settimana_p90_kw": _settimana(0.9),
        "avvertenze": avvertenze,
    }


# ---------------------------------------------------------------------------
# Integrazione sede + flotta
# ---------------------------------------------------------------------------

def _ora(slot: int) -> str:
    s = slot % SLOT_GIORNO
    return f"{s // 4:02d}:{(s % 4) * 15:02d}"


def capacita_libera(sede_kw: list[float], contratto_kw: float, p_max_ricarica_kw: Optional[float] = None) -> list[float]:
    """kW disponibili per la ricarica in ogni slot: contratto - sede, al massimo la
    potenza che l'impianto di ricarica puo' assorbire."""
    out = []
    for v in sede_kw:
        libera = max(0.0, float(contratto_kw) - float(v))
        if p_max_ricarica_kw is not None:
            libera = min(libera, float(p_max_ricarica_kw))
        out.append(round(libera, 3))
    return out


def finestre_critiche(sede_kw: list[float], flotta_kw: list[float], contratto_kw: float, giorno: int = 0,
                      pausa_max_min: int = 30) -> list[dict]:
    """Tratti in cui sede + flotta superano il contratto. Due tratti separati da una pausa
    di al massimo `pausa_max_min` minuti sono una sola finestra (un picco a dente di sega
    resta un unico problema da risolvere)."""
    out = []
    corrente = None
    n = len(sede_kw)
    pausa_slot = max(0, int(pausa_max_min) // 15)
    for s in range(n + 1):
        sopra = 0.0
        if s < n:
            sopra = float(sede_kw[s]) + float(flotta_kw[s] if s < len(flotta_kw) else 0.0) - float(contratto_kw)
        if s < n and sopra > 1e-6:
            if corrente is None:
                corrente = {"inizio_slot": s, "max_kw": 0.0, "energia_kwh": 0.0}
            corrente["max_kw"] = max(corrente["max_kw"], sopra)
            corrente["energia_kwh"] += sopra * SLOT_H
            corrente["fine_slot"] = s + 1
            corrente["minuti"] = corrente.get("minuti", 0) + 15
        elif corrente is not None and s < n and s - corrente["fine_slot"] < pausa_slot \
                and (s // SLOT_GIORNO) == (corrente["inizio_slot"] // SLOT_GIORNO):
            continue
        elif corrente is not None:
            g = giorno + corrente["inizio_slot"] // SLOT_GIORNO
            out.append({
                "giorno": GIORNI[g % 7],
                "inizio": _ora(corrente["inizio_slot"]),
                "fine": _ora(corrente["fine_slot"]) if corrente["fine_slot"] % SLOT_GIORNO else "24:00",
                "durata_min": (corrente["fine_slot"] - corrente["inizio_slot"]) * 15,
                "minuti_oltre_contratto": corrente["minuti"],
                "sforamento_max_kw": round(corrente["max_kw"], 1),
                "energia_extra_kwh": round(corrente["energia_kwh"], 1),
            })
            corrente = None
    return out


def integra(sede_kw: list[float], flotta_kw: list[float], contratto_kw: float, giorno: int = 0) -> dict:
    """Somma sede e flotta slot per slot e restituisce picchi, margine e finestre critiche
    di un giorno (96 slot) o di una settimana (672)."""
    n = len(sede_kw)
    flotta = [float(flotta_kw[s]) if s < len(flotta_kw) else 0.0 for s in range(n)]
    totale = [float(sede_kw[s]) + flotta[s] for s in range(n)]
    finestre = finestre_critiche(sede_kw, flotta, contratto_kw, giorno)
    picco_sede = max(sede_kw) if n else 0.0
    picco_tot = max(totale) if n else 0.0
    s_picco = totale.index(picco_tot) if n else 0
    return {
        "picco_sede_kw": round(picco_sede, 1),
        "picco_totale_kw": round(picco_tot, 1),
        "picco_totale_quando": f"{GIORNI[(s_picco // SLOT_GIORNO) % 7] + ' ' if n > SLOT_GIORNO else ''}{_ora(s_picco)}",
        "picco_aggiunto_dalla_flotta_kw": round(max(0.0, picco_tot - picco_sede), 1),
        "margine_minimo_kw": round(min(float(contratto_kw) - t for t in totale), 1) if n else 0.0,
        "energia_flotta_kwh": round(sum(flotta) * SLOT_H, 1),
        "energia_extra_kwh": round(sum(f["energia_extra_kwh"] for f in finestre), 1),
        "minuti_oltre_contratto": sum(f["minuti_oltre_contratto"] for f in finestre),
        "sforamento_max_kw": round(max((f["sforamento_max_kw"] for f in finestre), default=0.0), 1),
        "finestre_critiche": finestre,
    }


def riepilogo_giorni(finestre: list[dict]) -> list[dict]:
    """Una riga per giorno della settimana con almeno una finestra critica."""
    per_giorno: dict = {}
    for f in finestre:
        r = per_giorno.setdefault(f["giorno"], {"giorno": f["giorno"], "finestre": 0, "minuti_oltre_contratto": 0,
                                                "sforamento_max_kw": 0.0, "energia_extra_kwh": 0.0})
        r["finestre"] += 1
        r["minuti_oltre_contratto"] += f["minuti_oltre_contratto"]
        r["sforamento_max_kw"] = max(r["sforamento_max_kw"], f["sforamento_max_kw"])
        r["energia_extra_kwh"] = round(r["energia_extra_kwh"] + f["energia_extra_kwh"], 1)
    return [per_giorno[g] for g in GIORNI if g in per_giorno]


def energia_libera_finestra(libera_kw: list[float], inizio_h: float, fine_h: float) -> float:
    """kWh che la capacita' libera permette di caricare nella finestra di ricarica di un giorno."""
    a, b = int(round(inizio_h * 4)), int(round(fine_h * 4))
    tot = 0.0
    for s in range(a, b):
        tot += libera_kw[s % len(libera_kw)] * SLOT_H
    return round(tot, 1)


def costo_picco(picco_aggiunto_kw: float, sforamento_kw: float, energia_extra_max_kwh: float,
                tariffe: Optional[dict] = None) -> dict:
    """Costo del picco portato dalla flotta e delle alternative per stare nel contratto."""
    t = dict(TARIFFE_ARERA)
    t.update({k: v for k, v in (tariffe or {}).items() if v is not None})
    kw_aumento = math.ceil(max(0.0, sforamento_kw))
    kwh_batt = (energia_extra_max_kwh / t["profondita_scarica_batteria"]) if energia_extra_max_kwh > 0 else 0.0
    return {
        "quota_potenza_annua_eur": round(max(0.0, picco_aggiunto_kw) * t["quota_potenza_eur_kw_anno"]),
        "aumento_potenza": {
            "kw": kw_aumento,
            "costo_una_tantum_eur": round(kw_aumento * t["contributo_aumento_potenza_eur_kw"]),
            # Oltre 16,5 kW la quota potenza si paga sul picco prelevato, non sul contratto:
            # con l'aumento il costo annuo e' quello del picco portato dalla flotta.
            "costo_annuo_eur": round(max(0.0, picco_aggiunto_kw) * t["quota_potenza_eur_kw_anno"]),
        },
        "batteria": {
            "kwh": round(kwh_batt, 1),
            "kw": round(max(0.0, sforamento_kw), 1),
            "costo_eur": round(kwh_batt * t["costo_batteria_eur_kwh"]),
        },
        "tariffe_usate": t,
    }
