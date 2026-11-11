"""Report dal nome dell'azienda e minimi DM 28/10/2025 (Tabelle 4 e 5).
Nessuna chiamata di rete: i dati OSM sono dati di esempio."""
from datetime import date

from josa_core.compliance_dm2025 import BuildingProfile, compute_dm2025, compare_with_hardware_config
from josa_core import report_azienda as R

OGGI = date(2026, 11, 11)


def dm(posti, tipo, pubblico=False):
    r = compute_dm2025(BuildingProfile(residenziale=False, posti_auto=posti, tipo_intervento=tipo,
                                       accesso_pubblico=pubblico), OGGI)
    return (r.punti_tipologia_a_minimi_a_regime, r.punti_tipologia_b_minimi_a_regime,
            r.punti_tipologia_a_applicabili_oggi, r.punti_tipologia_b_applicabili_oggi)


# --- Tabella 5 (accesso privato) ---
assert dm(10, "nuova_costruzione")[:2] == (0, 0)
assert dm(15, "nuova_costruzione")[:2] == (3, 0)            # 11-20: 3
assert dm(15, "ristrutturazione_importante")[:2] == (2, 0)  # 11-20: 2
assert dm(15, "esistente")[:2] == (0, 0)                    # esistenti da 21 posti
assert dm(40, "nuova_costruzione")[:2] == (6, 0)            # 3 ogni 20
assert dm(55, "esistente") == (9, 0, 3, 0)                  # 3 ogni 20; oggi 50% dei valori: 1 ogni 20
assert dm(100, "esistente")[:2] == (15, 0)
assert dm(101, "esistente")[:2] == (9, 1)                   # 3 ogni 50 + 1 B
assert dm(501, "nuova_costruzione")[:2] == (33, 2)
assert dm(1200, "ristrutturazione_importante")[:2] == (48, 2)
# --- Tabella 4 (accesso pubblico) ---
assert dm(55, "nuova_costruzione", True)[:2] == (6, 0)      # 2 ogni 20
assert dm(200, "nuova_costruzione", True)[:2] == (8, 1)
assert dm(300, "ristrutturazione_importante", True)[:2] == (6, 1)
assert dm(1200, "esistente", True)[:2] == (48, 4)

# 100 -> 101 posti: in equivalente A (1 B = 10 A) il minimo non scende
a100, b100 = dm(100, "esistente")[:2]
a101, b101 = dm(101, "esistente")[:2]
assert a101 + 10 * b101 >= a100 + 10 * b100

# DC sotto 50 kW non e' Tipologia B; sotto 7,4 kW non conta
r = compute_dm2025(BuildingProfile(residenziale=False, posti_auto=40, tipo_intervento="nuova_costruzione"), OGGI)
c = compare_with_hardware_config(r, {"AC 3.7kW": 4, "AC 22kW": 2, "DC 30kW": 1, "DC 60kW": 1},
                                 {"AC 3.7kW": {"p": 3.7}, "AC 22kW": {"p": 22}, "DC 30kW": {"p": 30}, "DC 60kW": {"p": 60}})
assert (c["punti_a_installati"], c["punti_b_installati"]) == (3, 1)

# --- candidati sede ---
e_ok = {"category": "building", "type": "industrial", "name": "GVS Corporate Office", "lat": "44.49", "lon": "11.22",
        "osm_type": "way", "osm_id": 1, "display_name": "GVS, Via Roma, Zola Predosa",
        "address": {"town": "Zola Predosa"}, "extratags": {"building:levels": "3"},
        "geojson": {"type": "Polygon", "coordinates": [[[11.22, 44.49], [11.221, 44.49], [11.221, 44.4905], [11.22, 44.4905], [11.22, 44.49]]]}}
c = R.candidato_da_nominatim(e_ok, "GVS")
assert c and c["tipo_sede"] == "industriale" and c["piani"] == 3 and c["comune"] == "Zola Predosa" and c["area_m2"] > 3000
assert R.candidato_da_nominatim({**e_ok, "category": "aeroway", "type": "helipad"}, "GVS") is None
assert R.candidato_da_nominatim({**e_ok, "category": "shop", "type": "appliance"}, "GVS") is None
assert R.candidato_da_nominatim(e_ok, "Valsoia") is None

# --- posti: dati OSM, da confermare, stima, ipotesi ---
st = R.stima_posti_da_edificio(4266, "industriale", False, 3)
assert st["centrale"] == 75 and st["basso"] < st["centrale"] < st["alto"]
p = R.posti_con_forbice(55, 16, st)
assert (p["basso"], p["centrale"], p["alto"], p["fonte"]) == (55, 55, 82, "osm")
p = R.posti_con_forbice(0, 413, None)
assert (p["basso"], p["centrale"], p["alto"], p["fonte"]) == (206, 413, 620, "stima")
p = R.posti_con_forbice(0, 0, st)
assert p["centrale"] == 75 and p["fonte"] == "stima"
p = R.posti_con_forbice(0, 0, None)
assert p["fonte"] == "ipotesi" and p["basso"] < p["centrale"] < p["alto"]

# --- report completo da dati di esempio ---
SEDE = {"nome": "GVS", "comune": "Zola Predosa", "indirizzo": "Via Roma", "lat": 44.49, "lon": 11.22,
        "tipo_sede": "industriale", "area_m2": 4266, "e_lotto": False, "piani": 3}
DATI = {"tipo_sede": "industriale", "posti_osm_inclusi": 55, "posti_osm_da_confermare": 16, "n_servizi_300m": 0,
        "distanza_trasporto_km": 0.1, "distanza_colonnina_km": 1.26, "strada_classe": "secondary",
        "distanza_arteria_km": 0.2, "comune": "Zola Predosa", "densita_abitanti_km2": 511, "stima_edificio": st}
rep = R.calcola_report(SEDE, DATI, oggi=OGGI)
assert rep["dm"]["base"]["tipo_intervento"] == "esistente"
assert (rep["dm"]["base"]["a_oggi"], rep["dm"]["base"]["a_regime"]) == (3, 9)
assert [r["tipo_intervento"] for r in rep["dm"]["tabella"]] == ["esistente", "ristrutturazione_importante", "nuova_costruzione"]
pt = rep["punteggio"]
assert pt["min"] <= pt["punteggio"] <= pt["max"] and len(pt["criteri"]) == 9
assert {c["fonte"] for c in pt["criteri"]} >= {"osm", "ipotesi"}
assert any(x["chiave"] == "pmi" for x in rep["da_confermare"])
# il centrale coincide col calcolo del modulo Valuta il sito con gli stessi input
from josa_core.site_scoring import CriterioInput, compute_site_score
assert compute_site_score(CriterioInput(**pt["input_centrali"]), e_deposito_aziendale=True).punteggio_totale_0_100 == pt["punteggio"]

# correzioni dell'utente
rep2 = R.calcola_report(SEDE, DATI, {"posti": 120, "tipo_intervento": "nuova_costruzione", "pmi": False, "potenza_kw": 300}, oggi=OGGI)
assert rep2["posti"]["fonte"] == "utente" and rep2["posti"]["basso"] == rep2["posti"]["alto"] == 120
assert (rep2["dm"]["base"]["a_oggi"], rep2["dm"]["base"]["b_oggi"]) == (9, 1)
assert not rep2["da_confermare"] or all(x["chiave"] not in ("posti", "pmi", "potenza_kw") for x in rep2["da_confermare"])
assert next(c for c in rep2["punteggio"]["criteri"] if c["chiave"] == "connessione_rete")["fonte"] == "utente"
rep3 = R.calcola_report(SEDE, DATI, {"pmi": True}, oggi=OGGI)
assert rep3["dm"]["esente"] is True

rep4 = R.calcola_report({**SEDE, "approssimata": True}, DATI, oggi=OGGI)
assert rep4["da_confermare"][0]["chiave"] == "posizione"
assert "Zola Predosa, OSM" in next(c for c in rep["punteggio"]["criteri"] if c["chiave"] == "demografia")["spiegazione"]

# --- art. 41-sexies L. 1150/1942 (L. 122/1989): 1 m² di parcheggio ogni 10 m³ ---
u = R.posti_41sexies({"tipo_sede": "industriale", "superficie_coperta_m2": 20000})
assert (u["volume_m3"], u["area_parcheggio_m2"], u["posti"], u["posti_1967_1989"]) == (70000, 7000, 280, 140)
assert "20.000 m²" in u["metodo"] and "3,5 m" in u["metodo"]
u = R.posti_41sexies({"tipo_sede": "uffici", "area_m2": 1200, "e_lotto": False})
assert u["posti"] == 43 and "3 m" in u["metodo"]         # 1200 x 3 piani x 3 m / 10 / 25
assert R.posti_41sexies({"tipo_sede": "industriale"}) is None
urb = rep["dm"]["urbanistica"]                              # GVS: 4266 m² x 3 piani x 3,5 m
assert urb["posti"] == 179 and urb["a_regime"] > rep["dm"]["base"]["a_regime"]
assert rep3["dm"]["urbanistica"] is None or "a_oggi" not in rep3["dm"]["urbanistica"]

print("OK: minimi DM 28/10/2025 (Tabelle 4 e 5), stime dei posti, report dal nome dell'azienda.")
