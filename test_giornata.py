"""
Test di "Rivedi la giornata in sede" (giornata a 15 minuti nella risposta di /simulate).

1. Chiedere la giornata non cambia nulla del risultato (KPI, curva, Gantt).
2. La somma dei kW per colonnina coincide con la curva della flotta del motore.
3. Il SOC di ogni sessione piu' l'energia caricata resta entro la batteria.
4. Con la curva della sede, la giornata riporta sede e contratto e sede + flotta
   resta entro il contratto.
5. Il Gantt settimanale risponde (prima dava 500 per un argomento non previsto).

Uso: python3 test_giornata.py
"""
import math
from datetime import time

from fastapi.testclient import TestClient

from app import schemas, engine
from app.main import app


def gruppi():
    return [
        schemas.FleetGroup(gruppo="PHEV", profilo="Office", n_veicoli=20, km_per_giro=100, giri_per_veicolo_giorno=1,
            consumo_kwh_km=0.17, batteria_kwh=13.3, tempo_disponibile_min=600,
            finestra_inizio=time(8, 0), finestra_fine=time(18, 0), contemporanei_max=20, quota_ricarica_deposito=1.0,
            k_factor=1.1, ricarica_domestica=False, potenza_max_ricarica_ac_kw=6.6, accetta_ricarica_dc=False),
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
    return schemas.SimulateRequest(gruppi=gruppi(), catalogo_hardware=CAT, configurazione=CFG, policy=policy, **kw)


# 1. invarianza
base = engine.run_simulate(req())
con = engine.run_simulate(req(giornata=True))
assert base["giornata"] is None and con["giornata"] is not None
for k in ("config", "kpi", "timeline_p_kw", "timeline_q", "gantt_veicoli", "gantt_colonnine", "capex_eur", "copertura_pct"):
    assert base[k] == con[k], k
print("[invarianza] KPI, curva e Gantt identici con e senza giornata")

# 2. colonnine = curva della flotta
g = con["giornata"]
assert g["n_slot"] == 96 and len(g["flotta_kw"]) == 96 and len(g["fasce"]) == 96
assert len(g["colonnine"]) == 9 and len(g["veicoli"]) == 26
for i in range(96):
    s = sum(c["kw"][i] for c in g["colonnine"])
    assert abs(s - g["flotta_kw"][i]) < 0.05, (i, s, g["flotta_kw"][i])
for c in g["colonnine"]:
    assert max(c["kw"]) <= c["p_kw"] + 1e-6
assert max(g["flotta_kw"]) <= POL.p_shaving_kw + 1e-6
print(f"[colonnine] somma per slot = curva flotta; picco {max(g['flotta_kw']):.1f} kW")

# 3. SOC coerente
n_sess = 0
for v in g["veicoli"]:
    for s in v["sessioni"]:
        n_sess += 1
        e = sum(s["kw"]) * g["slot_h"]
        assert abs(e - s["energia_kwh"]) < 0.05, (v["id"], e, s["energia_kwh"])
        if s["soc_inizio_kwh"] is not None:
            assert s["soc_inizio_kwh"] + s["energia_kwh"] <= v["batteria_kwh"] + 0.05
assert n_sess > 0
print(f"[soc] {n_sess} sessioni coerenti con energia e batteria")

# 4. con la curva della sede
sede = []
for i in range(96):
    h = i / 4
    k = math.sin(math.pi * (h - 7) / 12) if 7 < h < 19 else 0
    sede.append(round(15 + 45 * k, 1))
pol_sede = POL.model_copy(update={"carico_sede_kw": sede, "potenza_contrattuale_kw": 120, "p_rete_kw": 120, "p_shaving_kw": 120})
gs = engine.run_simulate(req(policy=pol_sede, giornata=True))["giornata"]
assert gs["sede_kw"] == sede and gs["contratto_kw"] == 120
assert max(a + b for a, b in zip(gs["sede_kw"], gs["flotta_kw"])) <= 120 + 1e-6
print(f"[sede] picco sede + flotta {max(a + b for a, b in zip(gs['sede_kw'], gs['flotta_kw'])):.1f} kW entro il contratto 120 kW")

# 5. API: giornata e Gantt settimanale
cli = TestClient(app)
body = req(giornata=True).model_dump(mode="json")
r = cli.post("/api/v1/simulate", json=body)
assert r.status_code == 200 and r.json()["giornata"]["n_slot"] == 96, r.text[:300]
r = cli.post("/api/v1/simulate", json={**body, "giornata": False, "gantt_settimanale": True})
assert r.status_code == 200 and r.json()["gantt_orizzonte_h"] == 168.0, r.text[:300]
print("[api] /simulate con giornata e Gantt settimanale ok")

print("\nOK: giornata in sede letta dai risultati del motore, dimensionamento invariato.")
