"""Profilo dell'azienda dal web con Claude (ricerca web lato server).

Dal nome (ed eventualmente dal comune) Claude cerca sul web: indirizzi delle
sedi operative, numero di dipendenti, fatturato, settore. Servono al report
"dal nome dell'azienda" quando OpenStreetMap non basta:
  - le sedi da geocodificare (tipico delle PMI, non mappate su OSM);
  - i dipendenti per stimare i posti auto;
  - dipendenti e fatturato per capire se l'azienda e' una PMI (esenzione DM).

Richiede ANTHROPIC_API_KEY (la stessa della chat). Senza chiave, o se la
ricerca fallisce, restituisce None e il report usa solo OpenStreetMap.
Il risultato e' informativo: le fonti vengono riportate nel report.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

log = logging.getLogger(__name__)

MODELLO = os.environ.get("RICERCA_WEB_MODELLO", "claude-opus-5-5")
MAX_RICERCHE = 6
_CACHE_TTL_S = 7 * 24 * 3600
_cache: dict = {}

SISTEMA = (
    "Sei un analista che prepara la scheda di un'azienda italiana per uno studio sulla ricarica dei "
    "veicoli elettrici nei parcheggi aziendali. Cerca sul web (sito dell'azienda, registri e siti di "
    "dati camerali, notizie) e riporta solo dati che trovi in una fonte; se un dato non c'è, lascialo "
    "null. Non inventare indirizzi o numeri. Se trovi più aziende con nomi simili, scegli quella che "
    "corrisponde meglio al nome e al comune indicati e dillo nelle note."
)

FORMATO = """Alla fine rispondi SOLO con un oggetto JSON (nessun altro testo dopo) con questi campi:
{
  "trovata": true/false,
  "ragione_sociale": "string o null",
  "partita_iva": "string o null",
  "settore": "string breve o null",
  "dipendenti": numero intero o null,
  "dipendenti_anno": anno del dato o null,
  "fatturato_eur": numero o null,
  "fatturato_anno": anno del dato o null,
  "sedi": [{"tipo": "sede legale | stabilimento | sede operativa | uffici | magazzino",
            "indirizzo": "via e numero civico se presenti", "comune": "...", "provincia": "sigla o null",
            "dipendenti_sede": numero o null}],
  "fonti": ["url", ...],
  "note": "string breve: dubbi, omonimie, dati mancanti"
}
Metti nelle sedi solo luoghi fisici dove lavorano persone o parcheggiano auto (non la sola sede legale presso un commercialista)."""


def disponibile() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def estrai_json(testo: str) -> Optional[dict]:
    """Ultimo oggetto JSON nel testo (anche dentro ```json ... ```)."""
    if not testo:
        return None
    blocchi = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", testo, flags=re.S)
    candidati = blocchi[::-1] or []
    if not candidati:
        inizio = testo.find("{")
        fine = testo.rfind("}")
        if inizio >= 0 and fine > inizio:
            candidati = [testo[inizio:fine + 1]]
    for c in candidati:
        try:
            d = json.loads(c)
            if isinstance(d, dict):
                return d
        except json.JSONDecodeError:
            continue
    return None


def _num(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v >= 0 else None
    fascia = re.search(r"(\d+)\s*(?:-|–|a|e)\s*(\d+)", str(v))
    if fascia and "." not in fascia.group(0) and "," not in fascia.group(0):  # fascia, es. "da 10 a 19"
        a, b = int(fascia.group(1)), int(fascia.group(2))
        if a < b:
            return (a + b) / 2
    s = re.sub(r"[^\d,.]", "", str(v)).replace(".", "").replace(",", ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def normalizza(d: Optional[dict]) -> Optional[dict]:
    """Ripulisce la risposta del modello: tipi, limiti, campi mancanti."""
    if not d or not d.get("trovata"):
        return None
    dip = _num(d.get("dipendenti"))
    fat = _num(d.get("fatturato_eur"))
    sedi = []
    for s in (d.get("sedi") or [])[:8]:
        if not isinstance(s, dict):
            continue
        comune = str(s.get("comune") or "").strip()
        indirizzo = str(s.get("indirizzo") or "").strip()
        if not comune and not indirizzo:
            continue
        ds = _num(s.get("dipendenti_sede"))
        sedi.append({
            "tipo": str(s.get("tipo") or "sede").strip()[:40],
            "indirizzo": indirizzo[:200], "comune": comune[:80],
            "provincia": str(s.get("provincia") or "").strip()[:20],
            "dipendenti_sede": int(ds) if ds is not None and ds < 200000 else None,
        })
    fonti = [str(u)[:300] for u in (d.get("fonti") or []) if str(u).startswith("http")][:8]
    return {
        "ragione_sociale": str(d.get("ragione_sociale") or "").strip()[:160] or None,
        "partita_iva": str(d.get("partita_iva") or "").strip()[:20] or None,
        "settore": str(d.get("settore") or "").strip()[:120] or None,
        "dipendenti": int(dip) if dip is not None and dip < 500000 else None,
        "dipendenti_anno": d.get("dipendenti_anno"),
        "fatturato_eur": fat,
        "fatturato_anno": d.get("fatturato_anno"),
        "sedi": sedi, "fonti": fonti,
        "note": str(d.get("note") or "").strip()[:400],
    }


def classe_pmi(profilo: Optional[dict]) -> Optional[bool]:
    """True se per dimensione e' una PMI (<250 dipendenti e fatturato <=50 M EUR),
    False se e' grande, None se i dati non bastano. L'esenzione del DM richiede
    anche che sia proprietaria e occupante dell'edificio: non si ricava dal web."""
    if not profilo:
        return None
    dip, fat = profilo.get("dipendenti"), profilo.get("fatturato_eur")
    if dip is not None and dip >= 250:
        return False
    if fat is not None and fat > 50_000_000 and dip is None:
        return False
    if dip is not None and dip < 250 and (fat is None or fat <= 50_000_000):
        return True
    return None


def cerca_profilo(nome: str, comune: Optional[str] = None) -> Optional[dict]:
    """Chiede a Claude di cercare l'azienda sul web. None se non disponibile o se fallisce."""
    if not disponibile():
        return None
    chiave = (nome.strip().lower(), (comune or "").strip().lower())
    hit = _cache.get(chiave)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    try:
        import anthropic
    except ImportError:
        log.warning("Pacchetto anthropic non installato: ricerca web disattivata")
        return None

    richiesta = f"Azienda: {nome.strip()}" + (f"\nComune indicato: {comune.strip()}" if comune and comune.strip() else "")
    messages = [{"role": "user", "content": richiesta + "\n\n" + FORMATO}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_RICERCHE,
              "user_location": {"type": "approximate", "country": "IT"}}]
    try:
        client = anthropic.Anthropic(timeout=180.0, max_retries=1)
        risposta = None
        for _ in range(4):  # pause_turn: il server riprende da dove si era fermato
            risposta = client.beta.messages.create(
                model=MODELLO, max_tokens=16000, system=SISTEMA, tools=tools, messages=messages,
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
            if risposta.stop_reason != "pause_turn":
                break
            messages = messages[:1] + [{"role": "assistant", "content": risposta.content}]
        if risposta is None or risposta.stop_reason == "refusal":
            return None
        testo = "".join(getattr(b, "text", "") for b in risposta.content if getattr(b, "type", "") == "text")
        profilo = normalizza(estrai_json(testo))
    except Exception as e:  # rete, chiave, limiti: il report continua senza
        log.warning("Ricerca web non riuscita per %s: %s", nome, e)
        return None
    _cache[chiave] = (time.time(), profilo)
    return profilo
