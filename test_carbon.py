"""
Test del modulo Carbon Accounting (josa_core/carbon + endpoint /api/v1/carbon/*).

Uso: python3 test_carbon.py
"""
import os
import re
from datetime import date, time
from pathlib import Path

from app import schemas, engine
from josa_core import carbon
from josa_core.carbon.monitoring import import_sessions_csv

ROOT = Path(__file__).parent
lib = carbon.load_default_library()
FE_DIESEL = lib.get("gasolio_combustione")["valore"]
FE_BENZ = lib.get("benzina_combustione")["valore"]
FE_EL = lib.electricity_location()["valore"]


def close(a, b, tol=1e-3):
    return abs(a - b) <= tol


# --- 1. Caso calcolato a mano ---
# 10 furgoni x 100 km/giorno, diesel 10 km/L, 365 giorni.
# Elettricita' 200 kWh/giorno: 70% deposito, 20% casa, 10% pubblica.
groups = [carbon.GroupActivity(gruppo="Van", n_veicoli=10, km=1000.0, baseline_km_l=10.0)]
energy = carbon.EnergyActivity(kwh_deposito=140.0, kwh_domestica=40.0, kwh_pubblica=20.0)
r = carbon.quantify(groups, energy, carbon.CarbonAssumptions())
be = 1000 * 365 / 10 * FE_DIESEL / 1000
pe = 200 * 365 * FE_EL / 1000
assert close(r["baseline_tco2e"], be), (r["baseline_tco2e"], be)
assert close(r["progetto_tco2e"], pe), (r["progetto_tco2e"], pe)
assert close(r["riduzione_netta_tco2e_anno"], be - pe), r["riduzione_netta_tco2e_anno"]
assert sum(x["tco2e"] for x in r["breakdown"] if x["scenario"] == "progetto") - pe < 1e-3
print(f"[mano] BE {be:.3f} t | PE {pe:.3f} t | ER {be - pe:.3f} t/anno | {r['riduzione_pct']}%")

# --- 2. Elettricita': non si applicano perdite in piu' (gia' nel fattore ISPRA consumi) ---
assert "perdite" in lib.electricity_location()["note"].lower()
assert lib.electricity_location()["stato"] == "definitivo"
assert lib.electricity_location(2024)["stato"] == "preliminare"

# --- 3. PHEV: la benzina oltre l'autonomia entra nel progetto ---
fg_phev = schemas.FleetGroup(gruppo="PHEV", profilo="Sales", n_veicoli=5, km_per_giro=80,
                             giri_per_veicolo_giorno=1, autonomia_elettrica_km=50,
                             consumo_benzina_l100km=6.0)
acts = carbon.activities_from_groups([fg_phev], diesel_km_l=12.0)
assert close(acts[0].km, 400.0) and close(acts[0].km_termici_phev, 150.0)
r_phev = carbon.quantify(acts, carbon.EnergyActivity(kwh_deposito=250 * 0.2), carbon.CarbonAssumptions())
attesa = 150 * 6 / 100 * 365 * FE_BENZ / 1000
assert close(r_phev["progetto_per_fonte_tco2e"]["phev_benzina"], attesa), r_phev["progetto_per_fonte_tco2e"]

# --- 4. Pool car: km attesi ridotti dalla probabilita' di utilizzo; pendolari 1 giro ---
pool = schemas.FleetGroup(gruppo="Pool", profilo="Last-mile", n_veicoli=10, km_per_giro=40,
                          giri_per_veicolo_giorno=2, probabilita_utilizzo_pct=50)
pend = schemas.FleetGroup(gruppo="Pend", profilo="Pendolare", n_veicoli=2, km_per_giro=70,
                          giri_per_veicolo_giorno=3)
a = {x.gruppo: x for x in carbon.activities_from_groups([pool, pend], 10.0)}
assert close(a["Pool"].km, 400.0) and close(a["Pend"].km, 140.0)

# --- 5. Conservativita': incertezza e WTT non gonfiano la riduzione ---
r_unc = carbon.quantify(groups, energy, carbon.CarbonAssumptions(incertezza_pct=10))
assert r_unc["riduzione_netta_tco2e_anno"] < r["riduzione_netta_tco2e_anno"]
assert close(r_unc["riduzione_netta_tco2e_anno"], r["riduzione_lorda_tco2e"] * 0.9)
r_bio = carbon.quantify(groups, energy, carbon.CarbonAssumptions(quota_biocarburante_pct=7))
assert r_bio["riduzione_netta_tco2e_anno"] < r["riduzione_netta_tco2e_anno"]
try:
    carbon.quantify(groups, energy, carbon.CarbonAssumptions(includi_well_to_tank=True))
    raise AssertionError("WTT senza fattore upstream elettrico deve essere rifiutato")
except ValueError:
    pass
r_wtt = carbon.quantify(groups, energy, carbon.CarbonAssumptions(includi_well_to_tank=True, fe_upstream_elettricita_kg_kwh=0.05))
assert r_wtt["baseline_tco2e"] > r["baseline_tco2e"] and r_wtt["progetto_tco2e"] > r["progetto_tco2e"]

# --- 6. Market-based: GO 100% azzera il deposito, casa e pubblica restano su residual mix ---
r_mkt = carbon.quantify(groups, energy, carbon.CarbonAssumptions(
    metodo_elettricita="market", quota_rinnovabile_deposito_pct=100, fe_residual_mix_kg_kwh=0.45))
assert r_mkt["progetto_per_fonte_tco2e"]["deposito"] == 0.0
assert close(r_mkt["progetto_per_fonte_tco2e"]["domestica"], 40 * 365 * 0.45 / 1000)
try:
    carbon.quantify(groups, energy, carbon.CarbonAssumptions(metodo_elettricita="market"))
    raise AssertionError("market-based senza residual mix deve essere rifiutato")
except ValueError:
    pass
# Location-based ignora le GO (con avviso)
r_loc_go = carbon.quantify(groups, energy, carbon.CarbonAssumptions(quota_rinnovabile_deposito_pct=100))
assert close(r_loc_go["progetto_tco2e"], r["progetto_tco2e"])
# FV in sito: azzera la quota autoconsumata in entrambi i metodi
r_fv = carbon.quantify(groups, energy, carbon.CarbonAssumptions(fv_autoconsumo_kwh_anno=140 * 365))
assert r_fv["progetto_per_fonte_tco2e"]["deposito"] == 0.0

# --- 7. Override fattori tracciato ---
r_ov = carbon.quantify(groups, energy, carbon.CarbonAssumptions(
    fattori_override={"elettricita_it_consumi_2023": {"valore": 0.3, "fonte": "test"}}))
f_ov = [f for f in r_ov["fattori_usati"] if f["id"] == "elettricita_it_consumi_2023"][0]
assert f_ov["stato"] == "override_utente" and f_ov["valore"] == 0.3

# --- 8. Valore economico: ETS2 solo dal 2028 e solo sul carburante fossile ---
r_ets = carbon.quantify(groups, energy, carbon.CarbonAssumptions(
    prezzo_carbonio_modalita="ets2", prezzo_carbonio_eur_t=45, anno_inizio=2026, orizzonte_anni=4))
v = r_ets["valore_economico"]
assert v["eur_per_anno"][0] == 0 and v["eur_per_anno"][1] == 0
assert close(v["eur_per_anno"][2], round(be * 45, 0), tol=1)
r_ombra = carbon.quantify(groups, energy, carbon.CarbonAssumptions(prezzo_carbonio_modalita="ombra", prezzo_carbonio_eur_t=100))
assert close(r_ombra["valore_economico"]["eur_anno"], round((be - pe) * 100, 0), tol=1)
assert r["valore_economico"]["eur_anno"] == 0

# --- 9. Integrazione con la simulazione: KPI co2 = riduzione netta ---
gruppi = [
    schemas.FleetGroup(gruppo="Last Mile", profilo="Last-mile", n_veicoli=12, km_per_giro=60,
                       giri_per_veicolo_giorno=1.7, consumo_kwh_km=0.22, batteria_kwh=75,
                       tempo_disponibile_min=20, finestra_inizio=time(9, 0), finestra_fine=time(19, 0),
                       contemporanei_max=9, k_factor=1.5),
]
catalogo = [
    schemas.HardwareSpec(nome="AC 22kW", potenza_kw=22.0, costo_acquisto_eur=1000.0,
                         costo_installazione_eur=1600.0, costo_manutenzione_eur_anno=60.0),
    schemas.HardwareSpec(nome="DC 30kW", potenza_kw=30.0, costo_acquisto_eur=5000.0,
                         costo_installazione_eur=5000.0, costo_manutenzione_eur_anno=350.0),
]
policy = schemas.EnginePolicy(p_rete_kw=250.0, p_shaving_kw=250.0, max_ac_veicoli_per_punto=4,
                              max_dc_veicoli_per_punto=6, limite_ora_turno=19.0)
qreq = schemas.CarbonQuantifyRequest(gruppi=gruppi, catalogo_hardware=catalogo, policy=policy,
                                     configurazione=schemas.HardwareConfig(quantita={"AC 22kW": 6, "DC 30kW": 1}))
sim = engine.run_simulate(qreq)
q = engine.run_carbon_quantify(qreq)
assert close(sim["kpi"]["co2"], q["riduzione_netta_tco2e_anno"]), (sim["kpi"]["co2"], q["riduzione_netta_tco2e_anno"])
assert sim["kpi"]["co2"] < sim["kpi"]["co2_lorda_scarico"]
print(f"[simulate] co2 netta {sim['kpi']['co2']:.2f} t/anno (lorda allo scarico {sim['kpi']['co2_lorda_scarico']:.2f})")

opt = engine.run_optimize(schemas.OptimizeRequest(
    gruppi=gruppi, catalogo_hardware=catalogo, policy=policy, tipi_hardware_da_esplorare=["AC 22kW", "DC 30kW"],
    budget_max_eur=120000.0, beam_size=3, patience=5, max_steps=30))
assert opt["soluzioni"] and all(s["carbon"]["dicitura"] == carbon.DICITURA_METODOLOGIA for s in opt["soluzioni"])

# --- 10. Ex-post e controlli di qualita' ---
ep = engine.run_carbon_ex_post(schemas.CarbonExPostRequest(
    gruppi=gruppi,
    periodi=[
        schemas.CarbonMonitoringPeriod(periodo_da=date(2026, 1, 1), periodo_a=date(2026, 1, 31),
                                       kwh_deposito=20000, kwh_domestica=0, kwh_pubblica=1500,
                                       km_per_gruppo={"Last Mile": 100000}, fonte_dati="CPMS"),
        schemas.CarbonMonitoringPeriod(periodo_da=date(2026, 2, 1), periodo_a=date(2026, 2, 28),
                                       kwh_deposito=19000, kwh_pubblica=1000,
                                       km_per_gruppo={"Last Mile": 90000}, fonte_dati="CPMS"),
    ]))
be_gen = 100000 / 10 * FE_DIESEL / 1000
pe_gen = 21500 * FE_EL / 1000
assert close(ep["periodi"][0]["riduzione_netta_tco2e"], be_gen - pe_gen)
assert close(ep["totale"]["riduzione_netta_tco2e"], sum(p["riduzione_netta_tco2e"] for p in ep["periodi"]), tol=2e-3)
assert ep["totale"]["giorni_periodo"] == 59
assert not [c for c in ep["controlli_qualita"] if c["livello"] == "errore"], ep["controlli_qualita"]
bad = engine.run_carbon_ex_post(schemas.CarbonExPostRequest(
    gruppi=gruppi,
    periodi=[schemas.CarbonMonitoringPeriod(periodo_da=date(2026, 1, 1), periodo_a=date(2026, 1, 31),
                                            kwh_deposito=90000, km_per_gruppo={"Last Mile": 10000})]))
assert any("kWh/km" in c["messaggio"] for c in bad["controlli_qualita"])

# --- 11. Import sessioni CPMS (OCPP) ---
csv_ocpp = """transactionId;chargeBoxId;connectorId;idTag;startTimestamp;stopTimestamp;meterStart;meterStop
1;CP-01;1;VAN1;2026-01-03T18:00:00Z;2026-01-03T22:00:00Z;100000;140500
2;CP-02;1;VAN2;2026-01-04T18:10:00Z;2026-01-04T21:00:00Z;5000;35000
2;CP-02;1;VAN2;2026-01-04T18:10:00Z;2026-01-04T21:00:00Z;5000;35000
3;CP-01;1;VAN3;2026-02-01T18:00:00Z;2026-02-01T20:00:00Z;140500;160500
4;CP-01;1;VAN3;2026-02-02T18:00:00Z;2026-02-02T20:00:00Z;abc;160500
"""
imp = import_sessions_csv(csv_ocpp)
assert imp["sessioni_valide"] == 3, imp
assert close(imp["kwh_deposito_totale"], 40.5 + 30 + 20)
assert imp["kwh_per_mese"] == {"2026-01": 70.5, "2026-02": 20.0}
assert len(imp["sessioni_scartate"]) == 2
csv_simple = "inizio,colonnina,energia_kwh\n03/01/2026 18:00,CP-01,\"12,5\"\n"
assert close(import_sessions_csv(csv_simple)["kwh_deposito_totale"], 12.5)

# --- 12. Pacchetto evidenze riproducibile ---
ev_req = schemas.CarbonEvidenceRequest(tipo="ex_ante", ex_ante=qreq)
ev1 = engine.run_carbon_evidence(ev_req, "test")
ev2 = engine.run_carbon_evidence(ev_req, "test")
assert ev1["hash_input_sha256"] == ev2["hash_input_sha256"]
assert ev1["hash_risultato_sha256"] == ev2["hash_risultato_sha256"]
assert ev1["dicitura"] == carbon.DICITURA_METODOLOGIA
assert "fattore_id" in ev1["breakdown_csv"]

# --- 13. API via HTTP (gate disattivato se ACCESS_PASSWORD non e' impostata) ---
if not os.environ.get("ACCESS_PASSWORD"):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    assert c.get("/api/v1/carbon/factors").json()["dicitura"] == carbon.DICITURA_METODOLOGIA
    resp = c.post("/api/v1/carbon/quantify", json=qreq.model_dump(mode="json"))
    assert resp.status_code == 200, resp.text
    assert close(resp.json()["riduzione_netta_tco2e_anno"], q["riduzione_netta_tco2e_anno"])
    bad_req = qreq.model_dump(mode="json")
    bad_req["carbon"]["metodo_elettricita"] = "market"
    assert c.post("/api/v1/carbon/quantify", json=bad_req).status_code == 422
    assert c.post("/api/v1/carbon/sessions/import", json={"csv_text": csv_ocpp}).status_code == 200
    rq = c.post("/api/v1/carbon/quick", json={"n_veicoli": 50, "elettrificazione_pct": 60, "ricarica_aziendale_pct": 70})
    assert rq.status_code == 200, rq.text
    assert c.post("/api/v1/carbon/quick", json={"n_veicoli": 5, "ricarica_aziendale_pct": 150}).status_code == 422
    print("[api] endpoint /api/v1/carbon/* ok")

# --- 13b. Stima rapida (home): 50 veicoli x 25.000 km, 60% elettrificati, 70% in azienda ---
qr = engine.run_carbon_quick(schemas.CarbonQuickRequest(
    n_veicoli=50, km_anno_veicolo=25000, elettrificazione_pct=60, ricarica_aziendale_pct=70))
be_ev = 30 * 25000 / 10.0 * FE_DIESEL / 1000.0
pe_ev = 30 * 25000 * 0.22 * FE_EL / 1000.0
assert close(qr["baseline_tco2e"], be_ev, 0.01), qr["baseline_tco2e"]
assert close(qr["progetto_tco2e"], pe_ev, 0.01), qr["progetto_tco2e"]
assert close(qr["riduzione_netta_tco2e_anno"], be_ev - pe_ev, 0.01)
sr = qr["stima_rapida"]
assert close(sr["baseline_flotta_tco2e_anno"], 50 * 25000 / 10.0 * FE_DIESEL / 1000.0, 0.01)
assert close(sr["riduzione_pct_flotta"], (be_ev - pe_ev) / sr["baseline_flotta_tco2e_anno"] * 100, 0.1)
assert sr["ricarica_pubblica_pct"] == 30.0
# Location-based senza FV: la % in azienda non cambia la CO2; con FV in sito si'
q0 = engine.run_carbon_quick(schemas.CarbonQuickRequest(n_veicoli=50, elettrificazione_pct=60, ricarica_aziendale_pct=10,
                                                        carbon={"fv_autoconsumo_kwh_anno": 100000}))
q1 = engine.run_carbon_quick(schemas.CarbonQuickRequest(n_veicoli=50, elettrificazione_pct=60, ricarica_aziendale_pct=90,
                                                        carbon={"fv_autoconsumo_kwh_anno": 100000}))
assert q1["riduzione_netta_tco2e_anno"] > q0["riduzione_netta_tco2e_anno"]
# Elettrificazione 0%: nessuna riduzione, nessun errore
qz = engine.run_carbon_quick(schemas.CarbonQuickRequest(n_veicoli=10, elettrificazione_pct=0))
assert qz["riduzione_netta_tco2e_anno"] == 0 and qz["stima_rapida"]["riduzione_pct_flotta"] == 0
try:
    schemas.CarbonQuickRequest(n_veicoli=10, ricarica_aziendale_pct=80, ricarica_domestica_pct=30)
    raise AssertionError("quote > 100% accettate")
except ValueError:
    pass
print("[quick] stima rapida dalla home ok")

# --- 14. Dicitura: mai "certificato/certificazione ISO 14064" nel prodotto ---
pattern = re.compile(r"certific\w*[^\n]{0,40}14064|14064[^\n]{0,40}certific\w*", re.IGNORECASE)
for folder in ("app", "josa_core", "frontend"):
    for f in (ROOT / folder).rglob("*"):
        if f.suffix in (".py", ".html", ".js", ".json", ".md") and f.is_file():
            hit = pattern.search(f.read_text(encoding="utf-8", errors="ignore"))
            assert not hit, f"Dicitura non ammessa in {f}: '{hit.group(0)}'"
assert "certific" not in carbon.DICITURA_METODOLOGIA.lower()

print("OK: Carbon Accounting — quantificazione, ex-post, import sessioni, evidenze e dicitura verificati.")
