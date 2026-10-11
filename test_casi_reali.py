"""
Benchmark del motore su due casi reali anonimizzati (flotte aziendali dimensionate
con la metodologia Y35). I valori attesi vengono dai report consegnati ai clienti;
i nomi dei clienti non compaiono.

- Caso A: una sede, 41 ibridi plug-in + 3 BEV, 100 kW, finestra 08-18, nessuna
  ricarica domestica. Report: 13-15 punti AC, ~544 kWh/giorno nel giorno lavorativo.
- Caso B: sedi con readiness domestica 30-35%, mix 63% PHEV / 27% BEV, 100 kW, 8 h.
  Report: energia aziendale per sede (Sede 3: 521 kWh, Sede 4: 236 kWh).

Uso: python3 test_casi_reali.py
"""
from datetime import time

from app import schemas, engine

CATALOGO = [
    schemas.HardwareSpec(nome="AC 22kW", potenza_kw=22, costo_acquisto_eur=1500,
                         costo_installazione_eur=1000, costo_manutenzione_eur_anno=0),
    schemas.HardwareSpec(nome="DC 30kW", potenza_kw=30, costo_acquisto_eur=12000,
                         costo_installazione_eur=6000, costo_manutenzione_eur_anno=0),
]
ENERGIA = schemas.EnergyCosts(prezzo_privato_eur_kwh=0.32, prezzo_pubblico_eur_kwh=0.69)


def _gruppi(n_phev, n_bev, km, inizio, fine, pct_casa=None, presenza=None):
    comuni = dict(profilo="Office", km_per_giro=km, giri_per_veicolo_giorno=1,
                  tempo_disponibile_min=(fine.hour - inizio.hour) * 60,
                  finestra_inizio=inizio, finestra_fine=fine, quota_ricarica_deposito=1.0,
                  k_factor=1.1, probabilita_utilizzo_pct=presenza,
                  ricarica_domestica=pct_casa is not None, pct_veicoli_con_casa=pct_casa)
    return [
        # PHEV: batteria utile 12 kWh, 0,17 kWh/km, caricatore di bordo 6,6 kW, niente DC
        schemas.FleetGroup(gruppo="PHEV", n_veicoli=n_phev, consumo_kwh_km=0.17, batteria_kwh=12 / 0.7,
                           contemporanei_max=n_phev, potenza_max_ricarica_ac_kw=6.6,
                           autonomia_elettrica_km=12 / 0.17, consumo_benzina_l100km=6.0,
                           accetta_ricarica_dc=False, **comuni),
        schemas.FleetGroup(gruppo="BEV", n_veicoli=n_bev, consumo_kwh_km=0.18, batteria_kwh=70,
                           contemporanei_max=n_bev, potenza_max_ricarica_ac_kw=9.0,
                           accetta_ricarica_dc=True, **comuni),
    ]


def _policy(fine_h, casa, max_ac=3, buffer_pct=100):
    return schemas.EnginePolicy(p_rete_kw=100, p_shaving_kw=100, max_ac_veicoli_per_punto=max_ac,
                                max_dc_veicoli_per_punto=6, limite_ora_turno=fine_h,
                                hybrid_private_home_charging=casa, company_buffer_pct=buffer_pct)


def _simula(gruppi, policy, ac, dc=0):
    return engine.run_simulate(schemas.SimulateRequest(
        gruppi=gruppi, catalogo_hardware=CATALOGO, policy=policy, energia=ENERGIA,
        configurazione=schemas.HardwareConfig(quantita={"AC 22kW": ac, "DC 30kW": dc})))


# --- Caso A: energia del giorno lavorativo (108 km) entro il 10% del report ---
gruppi_a = _gruppi(41, 3, 108, time(8, 0), time(18, 0), presenza=90)
pol_a = _policy(18, casa=False)
res = _simula(gruppi_a, pol_a, ac=15)
e_int = res["kpi"]["e_int"]
print(f"[caso A] energia in sede {e_int:.0f} kWh (report ~544)")
assert abs(e_int - 544) / 544 <= 0.10

# --- Il limite di veicoli per punto vale anche per la ricarica diurna (DLM) ---
for limite in (1, 3):
    r = _simula(gruppi_a, _policy(18, casa=False, max_ac=limite), ac=6)
    massimo = max(c["n_veicoli_serviti"] for c in r["gantt_colonnine"])
    print(f"[caso A] limite {limite} veicoli/punto -> massimo osservato {massimo}")
    assert massimo <= limite

# --- Ottimizzatore: minimo CAPEX a parita' di servizio, AC-first, budget ampio ---
out = engine.run_optimize(schemas.OptimizeRequest(
    gruppi=gruppi_a, catalogo_hardware=CATALOGO, policy=pol_a, energia=ENERGIA,
    tipi_hardware_da_esplorare=["AC 22kW", "DC 30kW"], budget_max_eur=200000,
    beam_size=4, patience=8, max_steps=100))
migliore = out["soluzioni"][0]
n_ac = migliore["config"].get("AC 22kW", 0)
print(f"[caso A] ottimizzatore: {migliore['config']} | CAPEX {migliore['capex_eur']:.0f} EUR (report: 13-15 AC)")
assert migliore["copertura_pct"] >= 99.0
assert migliore["config"].get("DC 30kW", 0) == 0, "con PHEV senza presa DC non serve DC per la capacita'"
assert 13 <= n_ac <= 16
assert migliore["capex_eur"] <= min(s["capex_eur"] for s in out["soluzioni"] if s["copertura_pct"] >= 99.0)

# --- Caso B: la readiness domestica riduce la domanda in sede ---
energie = []
for readiness in (0, 30, 66, 100):
    r = _simula(_gruppi(36, 15, 88.5, time(9, 0), time(17, 0), pct_casa=readiness),
                _policy(17, casa=True, max_ac=4, buffer_pct=30), ac=14, dc=2)
    energie.append(r["kpi"]["e_int"])
print("[caso B] energia in sede per readiness 0/30/66/100%:", [round(e) for e in energie])
assert all(a > b for a, b in zip(energie, energie[1:])), "piu' readiness deve significare meno energia in sede"

for nome, n, km, readiness, attesa in (("Sede 3", 57, 88.5, 30, 521), ("Sede 4", 28, 69.9, 35, 236)):
    r = _simula(_gruppi(round(n * 0.63), round(n * 0.27), km, time(9, 0), time(17, 0), pct_casa=readiness),
                _policy(17, casa=True, max_ac=4, buffer_pct=30), ac=20, dc=2)
    e = r["kpi"]["e_int"]
    print(f"[caso B] {nome}: energia in sede {e:.0f} kWh (report {attesa})")
    assert abs(e - attesa) / attesa <= 0.10

print("\nOK: il motore riproduce i casi reali A e B entro le tolleranze.")


# --- Verifica settimanale con tolleranza tra veicoli (metodologia Y35) ---
def _verifica(ac, tolleranza):
    req = schemas.WeeklyCheckRequest(
        gruppi=gruppi_a, catalogo_hardware=CATALOGO, policy=_policy(18, casa=False, max_ac=6),
        energia=ENERGIA, configurazione=schemas.HardwareConfig(quantita={"AC 22kW": ac, "DC 30kW": 1}),
        tolleranza_min=tolleranza, settimane=8)
    return engine.run_weekly_check(req)

def _minimo_ac(tolleranza):
    return next(ac for ac in range(8, 25) if _verifica(ac, tolleranza)["superata"])

min_60, min_30 = _minimo_ac(60), _minimo_ac(30)
print(f"[caso A] verifica settimanale: minimo {min_60} AC + 1 DC a 60 min, {min_30} AC + 1 DC a 30 min (report: 14 + 1 a 60 min)")
assert 13 <= min_60 <= 16
assert min_30 < min_60, "una tolleranza piu' breve deve richiedere meno punti"
assert not _verifica(11, 60)["superata"]

print("\nOK: verifica settimanale coerente con il caso A.")
