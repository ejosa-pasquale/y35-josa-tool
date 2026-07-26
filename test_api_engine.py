"""
Test del layer app/engine.py — verifica che schemi API e josa_core si parlino
correttamente. Non richiede FastAPI/uvicorn in esecuzione: chiama direttamente
le funzioni che gli endpoint useranno, con gli stessi oggetti Pydantic.

Uso: python3 test_api_engine.py
"""
from datetime import time

from app import schemas, engine

gruppi = [
    schemas.FleetGroup(
        gruppo="Last Mile", profilo="Last-mile", n_veicoli=12, km_per_giro=60,
        giri_per_veicolo_giorno=1.7, giri_per_autonomia=2.0, consumo_kwh_km=0.22,
        batteria_kwh=75, tempo_disponibile_min=20,
        finestra_inizio=time(9, 0), finestra_fine=time(19, 0),
        contemporanei_max=9, quota_ricarica_deposito=1.0, k_factor=1.5,
    ),
    schemas.FleetGroup(
        gruppo="Ufficio", profilo="Office", n_veicoli=4, km_per_giro=20,
        giri_per_veicolo_giorno=1.0, giri_per_autonomia=1.0, consumo_kwh_km=0.18,
        batteria_kwh=60, tempo_disponibile_min=480,
        finestra_inizio=time(9, 0), finestra_fine=time(17, 0),
        contemporanei_max=4, quota_ricarica_deposito=1.0, k_factor=1.1,
    ),
]

catalogo = [
    schemas.HardwareSpec(nome="AC 22kW", potenza_kw=22.0, costo_acquisto_eur=1000.0,
                          costo_installazione_eur=1600.0, costo_manutenzione_eur_anno=60.0),
    schemas.HardwareSpec(nome="DC 30kW", potenza_kw=30.0, costo_acquisto_eur=5000.0,
                          costo_installazione_eur=5000.0, costo_manutenzione_eur_anno=350.0),
]

policy = schemas.EnginePolicy(
    p_rete_kw=250.0, p_shaving_kw=250.0,
    max_ac_veicoli_per_punto=4, max_dc_veicoli_per_punto=6,
    limite_ora_turno=19.0,
)

# --- Test 1: /api/v1/simulate ---
sim_req = schemas.SimulateRequest(
    gruppi=gruppi, catalogo_hardware=catalogo,
    configurazione=schemas.HardwareConfig(quantita={"AC 22kW": 8, "DC 30kW": 1}),
    policy=policy,
)
result = engine.run_simulate(sim_req)
print(f"[simulate] copertura {result['copertura_pct']}% | CAPEX {result['capex_eur']:.0f} EUR | "
      f"veicoli {result['veicoli_serviti']}/{result['veicoli_totali']}")
assert 0 <= result["copertura_pct"] <= 100
assert result["capex_eur"] > 0

# --- Test 2: /api/v1/optimize ---
opt_req = schemas.OptimizeRequest(
    gruppi=gruppi, catalogo_hardware=catalogo, policy=policy,
    tipi_hardware_da_esplorare=["AC 22kW", "DC 30kW"],
    budget_max_eur=120000.0, beam_size=3, patience=5, max_steps=40,
)
out = engine.run_optimize(opt_req)
print(f"[optimize] nodi esplorati: {out['nodi_esplorati']} | ammissibili: {out['ammissibili_trovate']}")
assert out["nodi_esplorati"] > 0

# --- Test 3: /api/v1/compliance/dm2025 ---
compliance_req = schemas.ComplianceDM2025Request(
    residenziale=False, accesso_pubblico=False, posti_auto=40,
    tipo_intervento="nuova_costruzione",
    configurazione_da_verificare=schemas.HardwareConfig(quantita={"AC 22kW": 4, "DC 30kW": 1}),
    catalogo_hardware=catalogo,
)
compliance_out = engine.run_compliance_check(compliance_req)
print(f"[compliance] punti A minimi a regime: {compliance_out['punti_tipologia_a_minimi_a_regime']} | "
      f"conforme (screening aggregato): {compliance_out['confronto_con_configurazione']['conforme_su_soglia_aggregata']}")
assert compliance_out["punti_tipologia_a_minimi_a_regime"] == 6
assert compliance_out["disclaimer"]

print("\nOK: il layer API (schemas + engine) funziona correttamente sopra josa_core, incluso il modulo compliance DM 28/10/2025.")
