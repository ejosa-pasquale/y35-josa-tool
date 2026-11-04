"""
Test della flessibilita' energetica (prezzi per fascia oraria F1/F2/F3).

1. Calendario ARERA corretto.
2. Senza fasce, e con fasce tutte uguali al prezzo privato, i KPI economici e il
   dimensionamento sono identici al comportamento storico.
3. Con F3 piu' economica la ricarica notturna costa meno, il costo usa la curva
   simulata e il risparmio da spostamento non e' mai negativo.

Uso: python3 test_fasce_orarie.py
"""
from datetime import time

from app import schemas, engine
from josa_core import fasce_orarie as fo

# 1. calendario
assert fo.fascia_ora(0, 10) == "F1" and fo.fascia_ora(4, 18.75) == "F1"
assert fo.fascia_ora(0, 7.5) == "F2" and fo.fascia_ora(0, 19) == "F2" and fo.fascia_ora(0, 22.75) == "F2"
assert fo.fascia_ora(5, 10) == "F2" and fo.fascia_ora(5, 6) == "F3"
assert fo.fascia_ora(0, 23) == "F3" and fo.fascia_ora(1, 3) == "F3" and fo.fascia_ora(6, 12) == "F3"
assert fo.fascia_slot(96 + 40) == "F1"  # martedi' 10:00

# Furgoni che restano in sede la notte (ricarica serale/notturna) + auto Office (ricarica di giorno)
gruppi = [
    schemas.FleetGroup(
        gruppo="Furgoni", profilo="Last-mile", n_veicoli=10, km_per_giro=90,
        giri_per_veicolo_giorno=2.0, giri_per_autonomia=2.0, consumo_kwh_km=0.25,
        batteria_kwh=75, tempo_disponibile_min=30,
        finestra_inizio=time(8, 0), finestra_fine=time(17, 0),
        contemporanei_max=10, quota_ricarica_deposito=1.0, k_factor=1.2,
        ricarica_domestica=False, ricarica_notturna_azienda=True,
    ),
    schemas.FleetGroup(
        gruppo="Ufficio", profilo="Office", n_veicoli=6, km_per_giro=40,
        giri_per_veicolo_giorno=1.0, giri_per_autonomia=1.0, consumo_kwh_km=0.18,
        batteria_kwh=60, tempo_disponibile_min=480,
        finestra_inizio=time(9, 0), finestra_fine=time(17, 0),
        contemporanei_max=6, quota_ricarica_deposito=1.0, k_factor=1.1,
    ),
]
catalogo = [
    schemas.HardwareSpec(nome="AC 22kW", potenza_kw=22.0, costo_acquisto_eur=1000.0,
                         costo_installazione_eur=1600.0, costo_manutenzione_eur_anno=60.0),
    schemas.HardwareSpec(nome="DC 30kW", potenza_kw=30.0, costo_acquisto_eur=5000.0,
                         costo_installazione_eur=5000.0, costo_manutenzione_eur_anno=350.0),
]
policy = schemas.EnginePolicy(p_rete_kw=100.0, p_shaving_kw=100.0, max_ac_veicoli_per_punto=4,
                              max_dc_veicoli_per_punto=6, limite_ora_turno=19.0,
                              hybrid_private_home_charging=False)
cfg = schemas.HardwareConfig(quantita={"AC 22kW": 8})


def sim(energia):
    return engine.run_simulate(schemas.SimulateRequest(
        gruppi=gruppi, catalogo_hardware=catalogo, policy=policy, energia=energia, configurazione=cfg))


CHIAVI = ("risp", "risp_teorico", "e_int", "e_ext", "c_cap", "copertura_reale_pct", "pena_en", "staff_ext")
base = sim(schemas.EnergyCosts())
uguali = sim(schemas.EnergyCosts(prezzi_fasce=schemas.PrezziFasceIn(
    prezzo_f1_eur_kwh=0.25, prezzo_f2_eur_kwh=0.25, prezzo_f3_eur_kwh=0.25)))
vuote = sim(schemas.EnergyCosts(prezzi_fasce=schemas.PrezziFasceIn()))
for r in (uguali, vuote):
    for c in CHIAVI:
        assert r["kpi"][c] == base["kpi"][c], (c, r["kpi"][c], base["kpi"][c])
    assert r["timeline_p_kw"] == base["timeline_p_kw"]
fl = base["kpi"]["flessibilita"]
assert fl["attiva"] is False and fl["impatto_fasce_eur_anno"] == 0 and fl["risparmio_potenziale_spostamento_eur_anno"] == 0
assert abs(sum(fl["kwh_giorno_per_fascia"].values()) - sum(base["timeline_p_kw"]) * 0.25) < 1.0
assert base["kpi"]["prezzo_sede_eur_kwh"] == 0.25
print("[fasce] default e fasce uguali: risultati identici;", fl["quota_pct_per_fascia"])

# 3. F3 economica: stessa ricarica (il motore non sposta nulla), costo diverso
f3 = sim(schemas.EnergyCosts(prezzi_fasce=schemas.PrezziFasceIn(
    prezzo_f1_eur_kwh=0.30, prezzo_f2_eur_kwh=0.25, prezzo_f3_eur_kwh=0.15)))
k = f3["kpi"]
assert f3["timeline_p_kw"] == base["timeline_p_kw"] and k["e_int"] == base["kpi"]["e_int"]
fl3 = k["flessibilita"]
assert fl3["attiva"] and 0.15 <= k["prezzo_sede_eur_kwh"] <= 0.30
atteso = sum(fl3["kwh_giorno_per_fascia"][f] * fl3["prezzi_eur_kwh"][f] for f in fo.FASCE) / max(1e-9, sum(fl3["kwh_giorno_per_fascia"].values()))
assert abs(k["prezzo_sede_eur_kwh"] - atteso) < 0.002, (k["prezzo_sede_eur_kwh"], atteso)
# il risparmio annuo cambia esattamente della differenza di costo dell'energia in sede
d_costo = k["e_int"] * (k["prezzo_sede_eur_kwh"] - 0.25) * 365
assert abs((base["kpi"]["risp"] - k["risp"]) - d_costo) < 1.0
assert fl3["risparmio_potenziale_spostamento_eur_anno"] >= 0
print(f"[fasce] F1 0.30 / F2 0.25 / F3 0.15: prezzo medio sede {k['prezzo_sede_eur_kwh']:.3f}, "
      f"impatto {fl3['impatto_fasce_eur_anno']:+.0f} EUR/anno, spostamento fino a "
      f"{fl3['risparmio_potenziale_spostamento_eur_anno']:.0f} EUR/anno")

# ottimizzatore: con fasce uguali stessa soluzione
def opt(energia):
    return engine.run_optimize(schemas.OptimizeRequest(
        gruppi=gruppi, catalogo_hardware=catalogo, policy=policy, energia=energia,
        tipi_hardware_da_esplorare=["AC 22kW", "DC 30kW"], budget_max_eur=60000,
        beam_size=3, patience=4, max_steps=20))
o0 = opt(schemas.EnergyCosts())
o1 = opt(schemas.EnergyCosts(prezzi_fasce=schemas.PrezziFasceIn(prezzo_f1_eur_kwh=0.25)))
assert [s["config"] for s in o0["soluzioni"]] == [s["config"] for s in o1["soluzioni"]]
print("OK: flessibilita' energetica per fasce orarie, default invariato.")
