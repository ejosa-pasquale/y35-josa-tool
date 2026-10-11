"""Pacchetto evidenze per il verificatore (ISO 14064-3).

Contiene input, ipotesi, fattori con fonte, risultato e breakdown passo-passo,
con hash SHA-256 dell'input e del risultato: lo stesso input deve sempre
produrre lo stesso hash e lo stesso risultato (riproducibilita').
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone

from .methodology import DICITURA_METODOLOGIA, METODOLOGIA_VERSIONE


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def sha256_of(obj) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def breakdown_csv(result: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["scenario", "voce", "quantita", "unita", "fattore_id", "tco2e"])
    for r in result.get("breakdown", []):
        w.writerow([r["scenario"], r["voce"], r["quantita"], r["unita"], r["fattore_id"], r["tco2e"]])
    w.writerow([])
    w.writerow(["fattore_id", "valore", "unita", "anno", "stato", "fonte", "url"])
    for f in result.get("fattori_usati", []):
        w.writerow([f["id"], f["valore"], f["unita"], f.get("anno", ""), f.get("stato", ""), f.get("fonte", ""), f.get("url", "")])
    return buf.getvalue()


def build_evidence_pack(input_payload: dict, result: dict, tool_version: str) -> dict:
    """input_payload: la richiesta originale (serializzabile). result: output di quantify/ex-post."""
    input_hash = sha256_of(input_payload)
    # Il risultato non contiene timestamp: il suo hash e' riproducibile.
    result_hash = sha256_of(result)
    return {
        "dicitura": DICITURA_METODOLOGIA,
        "versione_metodologia": METODOLOGIA_VERSIONE,
        "versione_tool": tool_version,
        "generato_il": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hash_input_sha256": input_hash,
        "hash_risultato_sha256": result_hash,
        "input": input_payload,
        "risultato": result,
        "breakdown_csv": breakdown_csv(result.get("totale", result)),
        "istruzioni_verifica": [
            "1. Ricalcolare l'hash SHA-256 del campo 'input' serializzato in JSON con chiavi ordinate e separatori compatti: deve coincidere con hash_input_sha256.",
            "2. Inviare lo stesso 'input' all'endpoint di quantificazione della stessa versione di metodologia: il risultato deve coincidere (hash_risultato_sha256).",
            "3. Per ogni riga del breakdown verificare quantita x fattore = tCO2e, e la fonte di ogni fattore in 'fattori_usati'.",
            "4. Per l'ex-post, riconciliare i kWh e i km dichiarati con le fonti dati indicate per ogni periodo (contatori, CPMS, telematica, fuel card).",
        ],
    }
