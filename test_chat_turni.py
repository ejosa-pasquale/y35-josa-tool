"""
Chat: i dati raccolti in tutti i turni arrivano all'analisi e il pulsante
"Avvia analisi infrastruttura" compare solo quando il motore ha i dati
indispensabili. Simula le risposte di Claude (nessuna chiamata reale).

Uso: python3 -m pytest -q test_chat_turni.py
"""
import json
import os

os.environ["ANTHROPIC_API_KEY"] = "chiave-finta-per-i-test"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.main import app  # noqa: E402
from josa_core.chat_engine import extract_data_block, stato_turno  # noqa: E402

client = TestClient(app)
H = {"X-Access-Token": auth.genera_token("email")}

DATI_SEDE = {"n_phev": 20, "n_bev": 10, "km_giornalieri": 60, "finestra_inizio": "09:00",
             "finestra_fine": "18:00", "p_rete_kw": 100, "notte_in_sede": False,
             "budget_chiesto": True, "prezzi_chiesti": False, "sedi": []}
PRONTO = ("Perfetto, registro 2,15 €/litro.\n\n✅ Ho tutto quello che mi serve. Premi il pulsante verde "
          "**Avvia analisi infrastruttura** qui sotto per vedere il progetto completo.")


def _risposta(testo, dati):
    return f"{testo}\n<data>\n{json.dumps(dati)}\n</data>"


class _FintoClaude:
    """Sostituisce httpx.AsyncClient: restituisce le risposte in coda, in ordine."""
    coda: list = []
    chiamate = 0

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, *a, **k):
        _FintoClaude.chiamate += 1
        stato, corpo = _FintoClaude.coda.pop(0)
        return httpx.Response(stato, json=corpo)


def _turno(monkeypatch, history, risposte, msg="ok"):
    monkeypatch.setattr(httpx, "AsyncClient", _FintoClaude)
    import asyncio
    async def _niente(*a, **k): pass
    monkeypatch.setattr(asyncio, "sleep", _niente)
    _FintoClaude.coda = list(risposte)
    _FintoClaude.chiamate = 0
    r = client.post("/api/v1/chat/message", json={"history": history, "message": msg}, headers=H)
    return r


def _ok(testo):
    return (200, {"content": [{"type": "text", "text": testo}], "stop_reason": "end_turn"})


def test_blocco_data_tollerante():
    t = 'x <data>\n```json\n{"n_bev": 3, "pronto_per_analisi": "true",}\n```\n</data>'
    assert extract_data_block(t) == {"n_bev": 3, "pronto_per_analisi": "true"}


def test_ultimo_turno_parziale_tiene_i_dati_precedenti(monkeypatch):
    storia = [{"role": "user", "content": "30 auto"},
              {"role": "assistant", "content": _risposta("Grazie.", DATI_SEDE)}]
    # ultimo turno: il modello scrive solo il dato nuovo
    r = _turno(monkeypatch, storia, [_ok(_risposta(PRONTO, {"prezzo_carburante_eur_l": 2.15,
                                                             "pronto_per_analisi": True}))], "2,15")
    d = r.json()
    assert r.status_code == 200 and d["pronto"] is True
    assert d["profile"]["n_phev"] == 20 and d["profile"]["prezzo_carburante_eur_l"] == 2.15
    assert "**Avvia analisi infrastruttura**" in d["reply"]
    # l'analisi parte anche se il browser manda un profilo vuoto: conta la conversazione
    a = client.post("/api/v1/chat/analyze", json={"profile": {}, "history": d["history"]}, headers=H)
    assert a.status_code == 200, a.text
    assert a.json()["gruppo"]["n_veicoli"] == 30


def test_pulsante_annunciato_senza_dati_chiede_il_mancante(monkeypatch):
    r = _turno(monkeypatch, [], [_ok(_risposta(PRONTO, {"prezzo_carburante_eur_l": 2.15,
                                                         "pronto_per_analisi": True}))])
    d = r.json()
    assert d["pronto"] is False
    assert "Avvia analisi" not in d["reply"] and "Ho tutto quello" not in d["reply"]
    assert "numero di veicoli" in d["reply"]


def test_servizio_occupato_ritenta(monkeypatch):
    occupato = (529, {"type": "error", "error": {"type": "overloaded_error"}})
    r = _turno(monkeypatch, [], [occupato, _ok(_risposta("Quanti veicoli?", {"n_veicoli": None}))])
    assert r.status_code == 200 and _FintoClaude.chiamate == 2
    r = _turno(monkeypatch, [], [occupato, occupato, occupato])
    assert r.status_code == 502 and _FintoClaude.chiamate == 3


def test_stato_turno_senza_frase():
    p, pronto, manca = stato_turno(_risposta("Quanti kW?", {"n_bev": 4}), [])
    assert not pronto and manca is None and p["n_bev"] == 4
