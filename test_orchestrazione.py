"""
Test dell'orchestrazione energetica a 30 giorni (/api/v1/orchestrazione).

1. Stessa energia caricata in tutti gli scenari (il servizio alla flotta non cambia).
2. La ricarica intelligente non costa mai piu' di quella di oggi e non supera il limite.
3. V2G spento -> nessuno scenario V2G; prezzi piatti senza quota potenza -> nessun risparmio.
4. Con la curva della sede il prelievo resta entro il contratto.
5. Lettura dei prezzi ENTSO-E (15 e 60 minuti, punti compressi) e passaggio all'ora italiana.
6. Regione -> zona di mercato.
7. L'endpoint risponde.

Uso: python3 test_orchestrazione.py
"""
import math
from datetime import date, time

from fastapi.testclient import TestClient

from app import schemas
from app.main import app
from app.orchestrazione import run_orchestrazione
from josa_core import prezzi_mercato as pm


def gruppi():
    return [
        schemas.FleetGroup(gruppo="PHEV", profilo="Office", n_veicoli=20, km_per_giro=100, giri_per_veicolo_giorno=1,
            consumo_kwh_km=0.17, batteria_kwh=13.3, tempo_disponibile_min=600,
            finestra_inizio=time(8, 0), finestra_fine=time(18, 0), contemporanei_max=20, quota_ricarica_deposito=1.0,
            k_factor=1.1, ricarica_domestica=False, potenza_max_ricarica_ac_kw=6.6, accetta_ricarica_dc=False,
            autonomia_elettrica_km=50),
        schemas.FleetGroup(gruppo="Furgoni", profilo="Last-mile", n_veicoli=6, km_per_giro=60,
            giri_per_veicolo_giorno=1.7, giri_per_autonomia=2.0, consumo_kwh_km=0.22, batteria_kwh=75,
            tempo_disponibile_min=30, finestra_inizio=time(8, 0), finestra_fine=time(19, 0),
            contemporanei_max=6, quota_ricarica_deposito=1.0, k_factor=1.3),
    ]


CAT = [schemas.HardwareSpec(nome="AC 22kW", potenza_kw=22, costo_acquisto_eur=1500, costo_installazione_eur=1000, costo_manutenzione_eur_anno=0),
       schemas.HardwareSpec(nome="DC 30kW", potenza_kw=30, costo_acquisto_eur=12000, costo_installazione_eur=6000, costo_manutenzione_eur_anno=0)]
CFG = schemas.HardwareConfig(quantita={"AC 22kW": 8, "DC 30kW": 1})
POL = schemas.EnginePolicy(p_rete_kw=100, p_shaving_kw=100, limite_ora_turno=18,
                           hybrid_private_home_charging=False, company_buffer_pct=100, sim_days=1)


def req(policy=POL, **kw):
    kw.setdefault("usa_prezzi_reali", False)
    return schemas.OrchestrazioneRequest(gruppi=gruppi(), catalogo_hardware=CAT, configurazione=CFG, policy=policy, **kw)


# 1-2. energia, costo, limite
r = run_orchestrazione(req())
sc = r["scenari"]
assert set(sc) == {"oggi", "v1g", "v2g"}
e0 = sc["oggi"]["energia_caricata_kwh"]
assert e0 > 0
assert abs(sc["v1g"]["energia_caricata_kwh"] - e0) <= 0.005 * e0 + 0.5, (sc["v1g"]["energia_caricata_kwh"], e0)
netto_v2g = sc["v2g"]["energia_caricata_kwh"] - sc["v2g"]["energia_restituita_kwh"] / 0.92
assert abs(netto_v2g - e0) <= 0.005 * e0 + 0.5, (netto_v2g, e0)
assert sc["v1g"]["costo_flotta_eur"] <= sc["oggi"]["costo_flotta_eur"] + 0.01
assert sc["v1g"]["picco_kw"] <= max(POL.p_shaving_kw, sc["oggi"]["picco_kw"]) + 0.01
assert r["giorni_simulati"] == 30 and r["giorni_operativi"] >= 20
assert r["prezzi"]["fonte"] == "riserva" and r["avvisi"]
assert r["v2g"]["verdetto"] in ("conviene", "da_valutare", "non_conviene")
print(f"[energia] {e0:.0f} kWh in tutti gli scenari; costo oggi {sc['oggi']['costo_flotta_eur']:.0f} EUR, "
      f"V1G {sc['v1g']['costo_flotta_eur']:.0f}, V2G {sc['v2g']['costo_flotta_eur']:.0f}")

# 3. V2G spento; prezzi piatti senza quota potenza
r2 = run_orchestrazione(req(v2g_attivo=False))
assert set(r2["scenari"]) == {"oggi", "v1g"} and r2["v2g"] is None
assert r2["scenari"]["oggi"] == sc["oggi"]
piatti = schemas.PrezziFasceIn(prezzo_f1_eur_kwh=0.2, prezzo_f2_eur_kwh=0.2, prezzo_f3_eur_kwh=0.2)
r3 = run_orchestrazione(req(v2g_attivo=False, prezzi_manuali=piatti,
                            componenti=schemas.ComponentiBollettaIn(quota_potenza_eur_kw_mese=0)))
s3 = r3["scenari"]
assert abs(s3["oggi"]["costo_flotta_eur"] - s3["v1g"]["costo_flotta_eur"]) < 0.5, s3
assert abs(s3["oggi"]["costo_flotta_eur"] - 0.2 * s3["oggi"]["energia_caricata_kwh"]) < 0.5
print("[override] V2G spento e prezzi F1/F2/F3 manuali rispettati")

# 4. con la curva della sede: prelievo entro il contratto
sede = [(25 + 45 * max(0, math.sin(math.pi * (i / 4 - 7) / 12)) if 7 <= i / 4 <= 19 else 20) for i in range(96)]
pol_sede = POL.model_copy(update={"carico_sede_kw": sede, "potenza_contrattuale_kw": 120})
r4 = run_orchestrazione(req(policy=pol_sede))
for n, s in r4["scenari"].items():
    assert s["picco_kw"] <= 120.01, (n, s["picco_kw"])
assert r4["scenari"]["v2g"]["costo_flotta_eur"] <= r4["scenari"]["v1g"]["costo_flotta_eur"] + 1.0
print(f"[sede] picchi {[s['picco_kw'] for s in r4['scenari'].values()]} kW entro 120 kW")

# 5. ENTSO-E
XML = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
 <TimeSeries><Period><timeInterval><start>2026-03-28T23:00Z</start><end>2026-03-29T22:00Z</end></timeInterval>
  <resolution>PT60M</resolution>
  <Point><position>1</position><price.amount>100</price.amount></Point>
  <Point><position>3</position><price.amount>80</price.amount></Point>
 </Period></TimeSeries>
 <TimeSeries><Period><timeInterval><start>2026-11-09T23:00Z</start><end>2026-11-10T23:00Z</end></timeInterval>
  <resolution>PT15M</resolution>
  """ + "".join(f"<Point><position>{i}</position><price.amount>{i}</price.amount></Point>" for i in range(1, 97)) + """
 </Period></TimeSeries>
</Publication_MarketDocument>"""
p = pm.parse_entsoe_xml(XML)
g, mancanti = pm._griglia_locale(p, [date(2026, 3, 29), date(2026, 11, 10)])
assert g[0][0] == 100 and g[0][7] == 100 and g[0][8] == 80 and g[0][95] == 80  # ora legale: 23 ore, punti compressi
assert mancanti == 0  # l'ora 02-03 inesistente nel cambio d'ora riceve il prezzo dell'ora successiva
assert g[1] == [float(i) for i in range(1, 97)]
print("[entsoe] 15 e 60 minuti, punti compressi e cambio d'ora gestiti")

# 6. zone
assert pm.zona_da_regione("Emilia-Romagna") == "NORD" and pm.zona_da_regione("lombardia") == "NORD"
assert pm.zona_da_regione("Toscana") == "CNOR" and pm.zona_da_regione("Lazio") == "CSUD"
assert pm.zona_da_regione("Puglia") == "SUD" and pm.zona_da_regione("Calabria") == "CALA"
assert pm.zona_da_regione("Sicilia") == "SICI" and pm.zona_da_regione("Sardegna") == "SARD"
assert pm.zona_da_regione(None) == "NORD"
assert run_orchestrazione(req(regione="Sicilia", v2g_attivo=False))["zona"] == "SICI"
print("[zone] regione -> zona di mercato")

# 7. endpoint
c = TestClient(app)
resp = c.post("/api/v1/orchestrazione", json=req().model_dump(mode="json"))
assert resp.status_code == 200, resp.text
assert resp.json()["scenari"]["v1g"]["costo_flotta_eur"] == sc["v1g"]["costo_flotta_eur"]
print("[api] /api/v1/orchestrazione risponde")
print("OK")
