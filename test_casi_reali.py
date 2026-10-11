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


# --- Fase 3: TCO per modello e business case (casi A e B) ---
from josa_core.tco_powertrain import (ModelloVeicolo, prezzo_energia_da_readiness,
                                      tco_mensile_modello, business_case_infrastruttura)

# Caso A con readiness 0% e infrastruttura in sede: il prezzo e' quello aziendale.
assert prezzo_energia_da_readiness(0, 0.26, 0.25, 0.66, 100) == 0.25

# Caso B, modello X (canoni reali ICE 615 / PHEV 730 / BEV 630), 25.000 km, carburante 2,19,
# readiness 66%. Il report usa casa + pubblica per la quota non domestica (copertura 0%):
# ICE 889 / PHEV 912 / BEV 781 EUR/mese.
mx = ModelloVeicolo("Modello X", 615, 730, 630)
r0 = tco_mensile_modello(mx, 25000, 2.19, prezzo_energia_da_readiness(66, 0.26, 0.32, 0.69, 0))
print(f"[caso B] modello X, senza ricarica in sede: ICE {r0.ice_mese_eur} / PHEV {r0.phev_mese_eur} / BEV {r0.bev_mese_eur} (report 889/912/781)")
for calc, rep in ((r0.ice_mese_eur, 889), (r0.phev_mese_eur, 912), (r0.bev_mese_eur, 781)):
    assert abs(calc - rep) / rep <= 0.02
assert r0.raccomandazione.startswith("BEV")
r1 = tco_mensile_modello(mx, 25000, 2.19, prezzo_energia_da_readiness(66, 0.26, 0.32, 0.69, 100))
print(f"[caso B] modello X, con ricarica in sede: ICE {r1.ice_mese_eur} / PHEV {r1.phev_mese_eur} / BEV {r1.bev_mese_eur}")
assert r1.phev_mese_eur < r0.phev_mese_eur and r1.bev_mese_eur < r0.bev_mese_eur

# Soglia di pareggio PHEV vs ICE, modello T (canoni 570 / 610): report ~10.600 km/anno.
mt = ModelloVeicolo("Modello T", 570, 610, None)
km_t = tco_mensile_modello(mt, 25000, 2.19, prezzo_energia_da_readiness(66, 0.26, 0.32, 0.69, 0)).km_pareggio_phev_vs_ice
print(f"[caso B] modello T: PHEV conviene sopra {km_t:.0f} km/anno (report ~10.600)")
assert abs(km_t - 10600) / 10600 <= 0.10

# Business case caso A: CAPEX 103.000, risparmio 33.719, rampa 30/90/100, sconto 10%.
bc_rep = business_case_infrastruttura([103000], 33719, tasso_sconto=0.10, capex_a_t0=False)
bc_std = business_case_infrastruttura([103000], 33719, tasso_sconto=0.10, capex_a_t0=True)
print(f"[caso A] payback {bc_rep.payback_mesi} mesi (report 46), VAN {bc_rep.van_eur:.0f} come nel report "
      f"(9.900), {bc_std.van_eur:.0f} con CAPEX all'anno 0")
assert bc_rep.payback_mesi == 46 and abs(bc_rep.van_eur - 9944) < 100 and bc_std.van_eur < bc_rep.van_eur

# Business case caso B: CAPEX fasato 231k + 154k, risparmio a regime 337.800, rampa 28/90/100.
bc_b = business_case_infrastruttura([231000, 154000], 337800, rampa_adozione=(93000/337800, 304000/337800, 1.0),
                                    tasso_sconto=0.10, capex_a_t0=False)
print(f"[caso B] payback {bc_b.payback_mesi} mesi (report 23), VAN {bc_b.van_eur:.0f} (report 692.800)")
assert bc_b.payback_mesi == 23 and abs(bc_b.van_eur - 692800) < 500

print("\nOK: TCO per modello e business case riproducono i report.")
