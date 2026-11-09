"""Lettura di un preventivo di infrastruttura di ricarica (PDF o testo).

Estrae, per ogni sede/parcheggio, i punti di ricarica AC e DC con la potenza
e la potenza disponibile al quadro, piu' il totale imponibile. Il parser e'
a regole (deterministico): l'utente rivede sempre i dati estratti prima della
verifica, e ogni dato mancante viene segnalato negli avvisi.

Formati riconosciuti (anche combinati):
- sezioni per sede: una riga con il nome della sede seguita da voci
  "n. 13 wallbox da muro AC 1x22 kW", "n. 1 colonnina DC Fast fino a 60 kW";
- tabella di configurazione con una colonna per sede: righe
  "Potenza disponibile al quadro 350 kW 280 kW", "Stazioni DC Fast 60 kW 1 1";
- riepilogo unico: "n. 19 stazioni AC 22 kW e n. 2 stazioni DC 60 kW".
"""
from __future__ import annotations

import io
import re
from typing import Optional

PAROLE_PUNTO = r"(?:wallbox|colonnin\w*|stazion\w*|punt\w* di ricarica|caricator\w*|charger\w*|presa|prese)"
ESCLUSI = re.compile(r"plint|quadr|interruttor|pozzett|cavo|cavi|canalizzaz|switch|differenzial", re.I)
RE_DC = re.compile(r"\bDC\b|\bfast\b|\bHPC\b|ultra", re.I)
RE_AC = re.compile(r"\bAC\b|wallbox|trifase|monofase|22\s*kW|11\s*kW|7[,.]4\s*kW", re.I)
RE_KW = re.compile(r"(\d+(?:[.,]\d+)?)\s*kW\b", re.I)
RE_VOCE = re.compile(r"^\s*(?:[-••*·]\s*)?n\.?\s*(\d{1,4})\s+(.*)$", re.I)
RE_EURO = r"(\d{1,3}(?:\.\d{3})+(?:,\d{2})?|\d+(?:,\d{2})?)"


def estrai_testo_pdf(dati: bytes) -> str:
    """Testo del PDF, pagina per pagina. Solleva ValueError se il PDF non e' leggibile."""
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise ValueError("Lettura PDF non disponibile sul server (manca pypdf).") from e
    try:
        reader = PdfReader(io.BytesIO(dati))
        pagine = [(p.extract_text() or "") for p in reader.pages[:40]]
    except Exception as e:
        raise ValueError("Il file non sembra un PDF leggibile.") from e
    return "\n".join(pagine)


def _num(s: str) -> float:
    s = s.strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def _euro(s: str) -> float:
    s = s.strip()
    if "," in s or re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def _kw(testo: str) -> Optional[float]:
    m = re.search(r"\d\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*kW", testo, re.I)
    if m:
        return _num(m.group(1))
    m = RE_KW.search(testo)
    return _num(m.group(1)) if m else None


def _tipo_voce(desc: str) -> Optional[str]:
    if ESCLUSI.search(desc) or not re.search(PAROLE_PUNTO, desc, re.I):
        return None
    if RE_DC.search(desc):
        return "DC"
    if RE_AC.search(desc):
        return "AC"
    return None


def _righe(testo: str) -> list[str]:
    return [re.sub(r"[ \t ]+", " ", r).strip() for r in testo.splitlines() if r.strip()]


def _e_titolo_sede(riga: str) -> bool:
    """Riga corta senza numeri che puo' essere il nome di una sede (es. 'Parcheggio Ovest')."""
    if len(riga) > 60 or re.search(r"\d", riga) or riga.endswith((".", ";", ":", ",")):
        return False
    return bool(re.match(r"(?i)(parcheggio|sede|sito|stabiliment|location|deposito|magazzino|plesso|area|palazzina|edificio)\b", riga))


def _voci_per_sede(righe: list[str]) -> list[dict]:
    """Sezioni 'Nome sede' + elenco 'n. X ...'. Conta solo la prima sezione di ogni sede
    (le ripetizioni nelle opere elettriche/civili descrivono gli stessi punti)."""
    sedi: dict[str, dict] = {}
    ordine: list[str] = []
    corrente = None
    for r in righe:
        if _e_titolo_sede(r):
            if corrente and corrente.lower() in sedi and sedi[corrente.lower()]["voci"]:
                sedi[corrente.lower()]["_chiusa"] = True
            corrente = r
            continue
        m = RE_VOCE.match(r)
        if not (m and corrente):
            continue
        tipo = _tipo_voce(m.group(2))
        if not tipo:
            continue
        chiave = corrente.lower()
        if chiave not in sedi:
            sedi[chiave] = {"nome": corrente, "voci": [], "_chiusa": False}
            ordine.append(chiave)
        s = sedi[chiave]
        if s["_chiusa"]:
            continue
        s["voci"].append({"tipo": tipo, "n": int(m.group(1)), "kw": _kw(m.group(2)), "descrizione": m.group(2).rstrip(";. ")})
    return [{"nome": sedi[k]["nome"], "voci": sedi[k]["voci"]} for k in ordine]


def _riga_tabella(righe: list[str], etichetta: str) -> Optional[str]:
    for r in righe:
        if re.match(etichetta, r, re.I):
            return r
    return None


def analizza_testo(testo: str) -> dict:
    """Dati strutturati del preventivo: sedi con punti AC/DC e potenze, totale, avvisi."""
    righe = _righe(testo)
    avvisi: list[str] = []

    sezioni = _voci_per_sede(righe)

    # Riga tabella: potenza disponibile per sede, nell'ordine delle colonne
    r_pot = _riga_tabella(righe, r"potenza (?:disponibile|al quadro|impegnata|contrattuale|in cabina)")
    potenze = [_num(x) for x in RE_KW.findall(r_pot)] if r_pot else []
    r_nom = _riga_tabella(righe, r"potenza nominale")
    nominali = [_num(x) for x in RE_KW.findall(r_nom)] if r_nom else []
    r_pred = _riga_tabella(righe, r"predisposizione")
    predisposizione = re.findall(r"(\d+)\s*AC(?:\s*\+\s*(\d+)\s*DC)?", r_pred, re.I) if r_pred else []

    # Tabella con una colonna per sede: "Stazioni AC 22 kW trifase 13 wallbox ... 6 colonnine ..."
    def _conteggi_tabella(etichetta):
        r = _riga_tabella(righe, etichetta)
        if not r:
            return None, None
        kw = _kw(r)
        resto = RE_KW.sub(" ", r)
        resto = re.sub(r"\d\s*[x×]", " ", resto)
        n = [int(x) for x in re.findall(r"(?<![\d.,])(\d{1,3})(?![\d.,])", resto)]
        return kw, n

    kw_ac_tab, n_ac_tab = _conteggi_tabella(r"(?:stazioni|punti|colonnine|wallbox)[^0-9]*\bAC\b")
    kw_dc_tab, n_dc_tab = _conteggi_tabella(r"(?:stazioni|punti|colonnine)[^0-9]*\b(?:DC|fast)\b")

    sedi = []
    if sezioni and any(s["voci"] for s in sezioni):
        for s in sezioni:
            ac = [v for v in s["voci"] if v["tipo"] == "AC"]
            dc = [v for v in s["voci"] if v["tipo"] == "DC"]
            sedi.append({
                "nome": s["nome"],
                "n_ac": sum(v["n"] for v in ac),
                "kw_ac": max((v["kw"] or 0) for v in ac) if ac else None,
                "n_dc": sum(v["n"] for v in dc),
                "kw_dc": max((v["kw"] or 0) for v in dc) if dc else None,
                "voci": s["voci"],
            })
    elif n_ac_tab or n_dc_tab:
        n_sedi = max(len(n_ac_tab or []), len(n_dc_tab or []), len(potenze), 1)
        for i in range(n_sedi):
            sedi.append({
                "nome": f"Sede {i + 1}",
                "n_ac": (n_ac_tab or [0] * n_sedi)[i] if i < len(n_ac_tab or []) else 0,
                "kw_ac": kw_ac_tab,
                "n_dc": (n_dc_tab or [0] * n_sedi)[i] if i < len(n_dc_tab or []) else 0,
                "kw_dc": kw_dc_tab,
                "voci": [],
            })

    # Riepilogo complessivo ("n. 19 stazioni ... AC 22 kW ... e n. 2 stazioni DC Fast fino a 60 kW")
    tot_ac = tot_dc = 0
    kw_ac_tot = kw_dc_tot = None
    for r in righe:
        for m in re.finditer(r"n\.?\s*(\d{1,4})\s+((?:(?!\bn\.\s*\d).)*?\d+(?:[.,]\d+)?\s*kW)", r, re.I):
            tipo = _tipo_voce(m.group(2))
            if tipo == "AC" and not tot_ac:
                tot_ac, kw_ac_tot = int(m.group(1)), _kw(m.group(2))
            elif tipo == "DC" and not tot_dc:
                tot_dc, kw_dc_tot = int(m.group(1)), _kw(m.group(2))
        if tot_ac and tot_dc:
            break

    if not sedi and (tot_ac or tot_dc):
        sedi.append({"nome": "Sede unica", "n_ac": tot_ac, "kw_ac": kw_ac_tot,
                     "n_dc": tot_dc, "kw_dc": kw_dc_tot, "voci": []})
        if len(potenze) > 1:
            avvisi.append("Il preventivo indica piu' potenze disponibili ma non la ripartizione dei punti per sede: verifica le sedi.")

    for i, s in enumerate(sedi):
        s["potenza_disponibile_kw"] = potenze[i] if i < len(potenze) else None
        s["potenza_nominale_kw"] = nominali[i] if i < len(nominali) else None
        if i < len(predisposizione):
            a, d = predisposizione[i]
            s["predisposizione"] = {"n_ac": int(a), "n_dc": int(d or 0)}
        if s["kw_ac"] in (None, 0) and s["n_ac"]:
            s["kw_ac"] = kw_ac_tab or kw_ac_tot or 22.0
            avvisi.append(f"{s['nome']}: potenza AC non trovata, uso {s['kw_ac']:g} kW.")
        if s["kw_dc"] in (None, 0) and s["n_dc"]:
            s["kw_dc"] = kw_dc_tab or kw_dc_tot or 50.0
            avvisi.append(f"{s['nome']}: potenza DC non trovata, uso {s['kw_dc']:g} kW.")
        if s["potenza_disponibile_kw"] is None:
            avvisi.append(f"{s['nome']}: potenza disponibile al quadro non trovata, inseriscila a mano.")

    somma_ac = sum(s["n_ac"] for s in sedi)
    somma_dc = sum(s["n_dc"] for s in sedi)
    if tot_ac and somma_ac != tot_ac:
        avvisi.append(f"Il riepilogo indica {tot_ac} punti AC ma nelle sedi ne ho trovati {somma_ac}: controlla.")
    if tot_dc and somma_dc != tot_dc:
        avvisi.append(f"Il riepilogo indica {tot_dc} punti DC ma nelle sedi ne ho trovati {somma_dc}: controlla.")
    if not sedi:
        avvisi.append("Non ho trovato punti di ricarica nel testo: inserisci la configurazione a mano.")

    totale = None
    for pattern in (r"totale imponibile[^0-9€]*€?\s*" + RE_EURO,
                    r"€\s*" + RE_EURO + r"\s*(?:\+|oltre)\s*IVA",
                    r"totale[^0-9€\n]*€?\s*" + RE_EURO + r"\s*€?\s*(?:\+\s*IVA|oltre IVA|IVA esclusa)"):
        m = re.search(pattern, testo, re.I)
        if m:
            totale = _euro(m.group(1))
            break

    return {
        "sedi": sedi,
        "totale_imponibile_eur": totale,
        "riepilogo": {"n_ac": tot_ac or somma_ac, "n_dc": tot_dc or somma_dc},
        "avvisi": avvisi,
    }
