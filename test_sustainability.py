"""Test del Sustainability Score SS-1.0 (python3 test_sustainability.py)."""
from datetime import time

from josa_core import sustainability as ss
from app import schemas, engine


def _base(**kw):
    d = dict(riduzione_pct=50.0, intensita_g_km=100.0, km_totali=1000.0, km_elettrici=500.0,
             kwh_ricarica_totali=100.0, kwh_rinnovabili=50.0, kwh_sede=50.0, kwh_pubblica=50.0,
             picco_kw=75.0, potenza_rete_kw=100.0)
    d.update(kw)
    return ss.compute(**d)


def test_meta_valori():
    r = _base()
    assert abs(r["score"] - 50.0) < 1e-6, r["score"]
    assert r["classe"] == "C"
    assert sum(p["peso"] for p in r["pilastri"]) == 100


def test_estremi():
    top = _base(riduzione_pct=100, intensita_g_km=0, km_elettrici=1000, kwh_rinnovabili=100,
                kwh_pubblica=0, picco_kw=40)
    assert top["score"] == 100.0 and top["classe"] == "A"
    zero = _base(riduzione_pct=0, intensita_g_km=250, km_elettrici=0, kwh_rinnovabili=0,
                 kwh_sede=0, picco_kw=120)
    assert zero["score"] == 0.0 and zero["classe"] == "E"


def test_peso_ridistribuito():
    r = _base(potenza_rete_kw=None)
    assert r["peso_ridistribuito"] and r["kpi_non_disponibili"]
    assert abs(r["score"] - 50.0) < 1e-6  # tutti gli altri KPI a meta' -> resta 50
    assert abs(sum(p["punti_max"] for p in r["pilastri"]) - 100.0) < 0.2


def test_soglie_assolute_monotone():
    # stesso input -> stesso score (confrontabile nel tempo); piu' FV -> score piu' alto
    assert _base()["score"] == _base()["score"]
    assert _base(kwh_rinnovabili=80)["score"] > _base()["score"]
    assert _base(picco_kw=60)["score"] > _base()["score"]


def test_dicitura():
    r = _base()
    assert "non è un rating ESG certificato" in r["dicitura"]
    assert r["versione_metodologia"] == "SS-1.0"
    assert "certificat" not in r["dicitura"].replace("non è un rating ESG certificato", "")


def test_simulazione_aggiunge_score():
    g = schemas.FleetGroup(gruppo="BEV", profilo="Office", n_veicoli=6, km_per_giro=80, giri_per_veicolo_giorno=1,
                           consumo_kwh_km=0.18, batteria_kwh=60, tempo_disponibile_min=600,
                           finestra_inizio=time(8, 0), finestra_fine=time(18, 0), contemporanei_max=6)
    cat = [schemas.HardwareSpec(nome="AC 22kW", potenza_kw=22, costo_acquisto_eur=1500,
                                costo_installazione_eur=1000, costo_manutenzione_eur_anno=0)]
    req = schemas.SimulateRequest(gruppi=[g], catalogo_hardware=cat,
                                  configurazione=schemas.HardwareConfig(quantita={"AC 22kW": 3}),
                                  policy=schemas.EnginePolicy(p_rete_kw=100, p_shaving_kw=100))
    r = engine.run_simulate(req)
    s = r["kpi"]["sustainability"]
    assert s["score"] is not None and 0 <= s["score"] <= 100, s
    assert s["affidabilita"] == "stimato"
    km = next(k for k in s["kpi"] if k["codice"] == "km_elettrici")
    assert km["valore"] == 100.0  # flotta tutta BEV


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("Tutti i test Sustainability Score superati")
