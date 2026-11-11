"""Profilo azienda dal web (Claude + ricerca web): parsing, PMI, posti dai dipendenti,
unione con le sedi OSM. Nessuna chiamata di rete: client e geocodifica finti."""
import os
import sys
import types
from datetime import date

from josa_core import ricerca_web as W
from josa_core import report_azienda as R

# --- parsing della risposta ---
testo = 'Ho trovato i dati.\n```json\n{"trovata": true, "ragione_sociale": "COSTRUIT SERVICE S.R.L.", "dipendenti": "18", ' \
        '"fatturato_eur": "3.200.000", "sedi": [{"tipo": "sede operativa", "indirizzo": "Zona Industriale Piano Lago", ' \
        '"comune": "Mangone", "provincia": "CS"}, {"comune": ""}], "fonti": ["https://www.costruitservice.it/", "non-url"]}\n```'
p = W.normalizza(W.estrai_json(testo))
assert p["dipendenti"] == 18 and p["fatturato_eur"] == 3_200_000
assert len(p["sedi"]) == 1 and p["fonti"] == ["https://www.costruitservice.it/"]
assert W.estrai_json("niente json") is None
assert W.normalizza({"trovata": False}) is None

# --- PMI per dimensione ---
assert W.classe_pmi(p) is True
assert W.classe_pmi({"dipendenti": 900, "fatturato_eur": None}) is False
assert W.classe_pmi({"dipendenti": None, "fatturato_eur": 80e6}) is False
assert W.classe_pmi({"dipendenti": None, "fatturato_eur": None}) is None

# --- posti dai dipendenti ---
add = R.addetti_in_sede({}, p)
assert add["centrale"] == 18
pd = R.posti_da_addetti(add)
assert (pd["basso"], pd["centrale"], pd["alto"]) == (9, 13, 16)
due_sedi = {**p, "dipendenti": 100, "sedi": p["sedi"] * 2}
a2 = R.addetti_in_sede({}, due_sedi)
assert (a2["centrale"], a2["alto"]) == (50, 100)
assert R.addetti_in_sede({"dipendenti_sede": 40}, due_sedi)["centrale"] == 40

SEDE = {"nome": "Costruit Service", "comune": "Mangone", "indirizzo": "Zona Industriale Piano Lago", "lat": 39.21,
        "lon": 16.31, "tipo_sede": "industriale", "area_m2": 0, "e_lotto": False, "piani": None, "approssimata": True}
DATI = {"tipo_sede": "industriale", "posti_osm_inclusi": 0, "posti_osm_da_confermare": 0, "n_servizi_300m": 0,
        "distanza_trasporto_km": 0.3, "distanza_colonnina_km": 10.4, "strada_classe": "primary",
        "distanza_arteria_km": 0.2, "comune": "Mangone", "densita_abitanti_km2": 158}
rep = R.calcola_report(SEDE, DATI, oggi=date(2026, 11, 11), profilo=p)
assert rep["posti"]["centrale"] == 13 and rep["pmi_dimensione"] is True
assert rep["dm"]["esente"] is False and any("PMI" in n for n in rep["dm"]["note"])
assert rep["dm"]["base"]["a_oggi"] == 0  # 13 posti, edificio esistente: sotto i 21
grande = {**p, "dipendenti": 1200, "fatturato_eur": 400e6}
rep_g = R.calcola_report(SEDE, DATI, oggi=date(2026, 11, 11), profilo=grande)
assert rep_g["pmi_dimensione"] is False and not any(x["chiave"] == "pmi" for x in rep_g["da_confermare"])

# --- cerca_profilo: client finto, pause_turn e cache ---
chiamate = []
class _Blocco:
    def __init__(self, t, text=""):
        self.type, self.text = t, text
class _Risp:
    def __init__(self, stop, content):
        self.stop_reason, self.content = stop, content
class _Msgs:
    def create(self, **kw):
        chiamate.append(kw)
        if len(chiamate) == 1:
            return _Risp("pause_turn", [_Blocco("server_tool_use")])
        return _Risp("end_turn", [_Blocco("text", testo)])
class _Client:
    def __init__(self, **kw):
        self.beta = types.SimpleNamespace(messages=_Msgs())
sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=_Client)
os.environ["ANTHROPIC_API_KEY"] = "test"
pr = W.cerca_profilo("Costruit Service")
assert pr["dipendenti"] == 18 and len(chiamate) == 2
assert chiamate[0]["tools"][0]["type"] == "web_search_20260209" and chiamate[0]["fallbacks"] == "default"
assert chiamate[1]["messages"][-1]["role"] == "assistant"  # turno ripreso, nessun "continua"
W.cerca_profilo("Costruit Service")
assert len(chiamate) == 2  # dalla cache

# --- cerca_azienda: sede dal web aggiunta quando OSM non ha nulla ---
R.cerca_sedi = lambda nome, comune=None: []
R.sede_da_indirizzo = lambda nome, testo: {"id": "", "nome": nome, "indirizzo": testo, "comune": "", "provincia": "",
                                           "lat": 39.21, "lon": 16.31, "tipo_sede": "industriale", "e_lotto": False,
                                           "area_m2": 0, "piani": None, "approssimata": True, "categoria": "", "tipo_osm": ""}
out = R.cerca_azienda("Costruit Service")
assert out["profilo_web"]["dipendenti"] == 18 and len(out["sedi"]) == 1
s0 = out["sedi"][0]
assert s0["origine"] == "web" and s0["comune"] == "Mangone" and s0["approssimata"] is True

# senza chiave: nessuna ricerca web
del os.environ["ANTHROPIC_API_KEY"]
W._cache.clear()
assert W.cerca_profilo("Altra azienda") is None

print("OK: profilo dal web, PMI per dimensione, posti dai dipendenti, sedi dal web.")
