"""
Flessibilita' energetica: prezzo dell'energia per fascia oraria ARERA (F1/F2/F3).

Calendario ARERA (festivita' nazionali ignorate, si trattano come giorni normali):
  F1  lun-ven 08-19
  F2  lun-ven 07-08 e 19-23, sabato 07-23
  F3  lun-sab 23-07, domenica e festivi tutto il giorno

La simulazione parte da mezzanotte del primo giorno lavorativo (lunedi'), slot da
15 minuti: lo slot k cade nell'ora (k/4) % 24 del giorno k // 96.

Senza prezzi per fascia (default) il prezzo e' lo stesso in tutte le ore e nessun
numero del motore cambia: questo modulo misura solo come si distribuisce la
ricarica in sede tra le fasce. Con prezzi diversi:
  - il costo dell'energia di sede usa il prezzo medio pesato sulla curva di ricarica
    simulata (quanto si carica davvero in F1, F2, F3);
  - si stima quanto si risparmierebbe spostando ogni ricarica nelle ore piu'
    economiche della sua sosta (stima teorica: stessa potenza media della sessione,
    stesso orario di arrivo e partenza, senza ricontrollare il limite di potenza
    della sede).
"""
from __future__ import annotations

import math
from typing import Optional

SLOT_H = 0.25
FASCE = ("F1", "F2", "F3")


def fascia_ora(giorno: int, ora: float) -> str:
    """Fascia ARERA per il giorno (0 = lunedi') e l'ora del giorno [0, 24)."""
    dow = int(giorno) % 7
    h = float(ora) % 24.0
    if dow == 6:  # domenica
        return "F3"
    if h < 7.0 or h >= 23.0:
        return "F3"
    if dow == 5:  # sabato 07-23
        return "F2"
    if 8.0 <= h < 19.0:
        return "F1"
    return "F2"


def fascia_slot(slot: int) -> str:
    t = slot * SLOT_H
    return fascia_ora(int(t // 24), t % 24.0)


def prezzi_fasce(prezzi_in: Optional[dict], prezzo_unico: float) -> dict:
    """{'F1','F2','F3'} -> EUR/kWh. Una fascia non indicata usa il prezzo unico."""
    prezzi_in = prezzi_in or {}
    out = {}
    for f in FASCE:
        v = prezzi_in.get(f)
        out[f] = float(prezzo_unico) if v is None else float(v)
    return out


def fasce_diverse(prezzi: Optional[dict]) -> bool:
    if not prezzi:
        return False
    vals = [float(prezzi[f]) for f in FASCE]
    return (max(vals) - min(vals)) > 1e-12


def prezzo_medio_pesato(timeline_p, prezzi: dict, prezzo_unico: float) -> float:
    """Prezzo medio dell'energia caricata in sede, pesato sulla curva di potenza.
    Con fasce tutte uguali restituisce esattamente il prezzo unico."""
    if not fasce_diverse(prezzi):
        return float(prezzo_unico)
    e_tot = 0.0
    costo = 0.0
    for k, p in enumerate(timeline_p if timeline_p is not None else []):
        p = float(p)
        if p <= 0:
            continue
        e = p * SLOT_H
        e_tot += e
        costo += e * prezzi[fascia_slot(k)]
    return (costo / e_tot) if e_tot > 1e-9 else float(prezzo_unico)


def _prezzo_slot_medio(prezzi: dict, t0: float, t1: float) -> float:
    a = int(math.floor(t0 / SLOT_H + 1e-9))
    b = max(a + 1, int(math.ceil(t1 / SLOT_H - 1e-9)))
    vals = [prezzi[fascia_slot(k)] for k in range(a, b)]
    return sum(vals) / len(vals)


def _costo_minimo_finestra(prezzi: dict, energia: float, p_media: float, s: float, f: float) -> float:
    """Costo minimo caricando 'energia' alla potenza media p nelle ore piu' economiche di [s, f)."""
    a = int(math.ceil(s / SLOT_H - 1e-9))
    b = int(math.floor(f / SLOT_H + 1e-9))
    slots = sorted(prezzi[fascia_slot(k)] for k in range(a, b))
    e_slot = p_media * SLOT_H
    resto = energia
    costo = 0.0
    for pr in slots:
        if resto <= 1e-9:
            break
        e = min(e_slot, resto)
        costo += e * pr
        resto -= e
    if resto > 1e-9:  # finestra troppo corta per lo spostamento: il resto resta dov'e'
        costo += resto * (slots[-1] if slots else 0.0)
    return costo


def analisi_flessibilita(timeline_p, sessions, prezzi: dict, prezzo_unico: float,
                         e_int_giorno: float, sim_days: int = 1, giorni_anno: int = 365) -> dict:
    """Come si distribuisce la ricarica in sede tra le fasce e quanto vale spostarla."""
    sim_days = max(1, int(sim_days or 1))
    kwh = {f: 0.0 for f in FASCE}
    for k, p in enumerate(timeline_p if timeline_p is not None else []):
        p = float(p)
        if p > 0:
            kwh[fascia_slot(k)] += p * SLOT_H
    kwh = {f: v / sim_days for f, v in kwh.items()}
    tot = sum(kwh.values())
    quota = {f: (round(100.0 * v / tot, 1) if tot > 1e-9 else 0.0) for f, v in kwh.items()}

    attiva = fasce_diverse(prezzi)
    p_medio = prezzo_medio_pesato(timeline_p, prezzi, prezzo_unico)
    costo_fasce = float(e_int_giorno) * p_medio * giorni_anno
    costo_unico = float(e_int_giorno) * float(prezzo_unico) * giorni_anno

    kwh_spostabili = 0.0
    risparmio = 0.0
    if attiva:
        for s in sessions or []:
            e = float(s.get("caricato", 0.0) or 0.0)
            lp = s.get("log_p") or {}
            fin = s.get("finestra")
            if fin is None and s.get("s") is not None and s.get("f") is not None:
                fin = (s["s"], s["f"])
            if e <= 1e-6 or fin is None or lp.get("i") is None or lp.get("ec") is None:
                continue
            i, ec = float(lp["i"]), float(lp["ec"])
            durata = ec - i
            if durata <= 1e-6:
                continue
            ws, wf = min(float(fin[0]), i), max(float(fin[1]), ec)
            attuale = e * _prezzo_slot_medio(prezzi, i, ec)
            minimo = _costo_minimo_finestra(prezzi, e, e / durata, ws, wf)
            if attuale - minimo > 1e-6:
                risparmio += attuale - minimo
                kwh_spostabili += e
        risparmio = risparmio / sim_days * giorni_anno
        kwh_spostabili /= sim_days

    return {
        "attiva": bool(attiva),
        "prezzi_eur_kwh": {f: round(prezzi[f], 4) for f in FASCE},
        "kwh_giorno_per_fascia": {f: round(v, 1) for f, v in kwh.items()},
        "quota_pct_per_fascia": quota,
        "prezzo_medio_sede_eur_kwh": round(p_medio, 4),
        "costo_energia_sede_eur_anno": round(costo_fasce, 0),
        "costo_energia_sede_prezzo_unico_eur_anno": round(costo_unico, 0),
        "impatto_fasce_eur_anno": round(costo_fasce - costo_unico, 0),
        "kwh_giorno_spostabili": round(kwh_spostabili, 1),
        "risparmio_potenziale_spostamento_eur_anno": round(risparmio, 0),
        "calendario": "ARERA: F1 lun-ven 8-19; F2 lun-ven 7-8 e 19-23, sabato 7-23; F3 notte, domenica e festivi",
        "nota": ("Stesso prezzo in tutte le fasce: la ripartizione mostra solo quando si carica in sede."
                 if not attiva else
                 "Costo calcolato sulla curva di ricarica simulata. Il risparmio da spostamento e' una stima "
                 "teorica: ogni ricarica spostata nelle ore piu' economiche della sua sosta, stessa potenza "
                 "media, senza ricontrollare il limite di potenza della sede."),
    }
