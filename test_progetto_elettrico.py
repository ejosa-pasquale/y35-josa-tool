"""
Progetto elettrico (relazione tecnica + schema unifilare in PDF) a partire dal
dimensionamento Y35.

- Calcoli: Ib, In, sezione, Iz, caduta di tensione, PE, differenziali, DLM,
  rotazione delle fasi, verifica TT.
- Caso A (anonimizzato, 41 PHEV + 3 BEV, 100 kW): la configurazione scelta
  dall'ottimizzatore diventa un progetto con dorsale dimensionata sul limite DLM.
- Endpoint: PDF completo (relazione A4 + tavola A3), solo unifilare, errori 422.

Uso: python3 test_progetto_elettrico.py
"""
import os

os.environ["ACCESS_PASSWORD"] = "password-di-prova"
os.environ.pop("RESEND_API_KEY", None)

from io import BytesIO  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from pypdf import PdfReader  # noqa: E402

from app import auth  # noqa: E402
from app.main import app  # noqa: E402
from josa_core.progetto_elettrico import dimensiona_impianto, genera_progetto_pdf  # noqa: E402
from josa_core.progetto_elettrico.calcoli import PORTATE, corrente_impiego, sezione_pe  # noqa: E402


def _coerenza(d):
    """Ib <= In <= Iz, caduta di tensione nel limite e cortocircuito per ogni linea."""
    lim = d["parametri"]["dv_max_pct"]
    for l in [d["dorsale"]] + d["linee"]:
        assert l["ib_a"] <= l["in_a"] <= l["iz_a"], l
        assert l["k2s2"] >= l["i2t_passante"], l
        assert l.get("dv_totale_pct", l["dv_pct"]) <= lim + 1e-9, l


def test_calcoli_base():
    # 22 kW trifase a cosφ 0,99: 32,1 A -> interruttore 40 A
    assert round(corrente_impiego(22, 3, 0.99), 1) == 32.1
    assert sezione_pe(10) == 10 and sezione_pe(25) == 16 and sezione_pe(95) == 50
    d = dimensiona_impianto({"punti": [
        {"tipo": "AC 22kW", "potenza_kw": 22, "quantita": 4, "lunghezza_m": 10, "lunghezza_max_m": 70},
        {"tipo": "AC 7.4kW", "potenza_kw": 7.4, "quantita": 3},
        {"tipo": "DC 50kW", "potenza_kw": 50, "quantita": 1, "lunghezza_m": 30},
    ]})
    _coerenza(d)
    l22 = [l for l in d["linee"] if l["tipo"] == "AC 22kW"]
    assert all(l["in_a"] == 40 and l["poli"] == 4 for l in l22)
    # la linea piu' lunga non ha una sezione minore della piu' corta
    assert l22[-1]["sezione_mm2"] >= l22[0]["sezione_mm2"]
    mono = [l for l in d["linee"] if l["fasi"] == 1]
    assert [l["fase"] for l in mono] == ["L1", "L2", "L3"], "le monofase ruotano sulle tre fasi"
    dc = [l for l in d["linee"] if not l["ac"]][0]
    assert dc["tipo_differenziale"] == "B" and dc["potenza_assorbita_kw"] > 50
    # senza limite DLM la dorsale porta tutta la potenza installata (contemporaneita' 1, CEI 64-8 722)
    assert d["dlm"] is False and d["potenza_ricarica_kw"] == d["potenza_installata_kw"]
    assert any("Resistenza di terra" in a for a in d["avvisi"])
    print("[calcoli] ok:", d["riepilogo"])


def test_dlm_e_tt():
    base = {"punti": [{"tipo": "AC 22kW", "potenza_kw": 22, "quantita": 10}], "ra_ohm": 40}
    senza = dimensiona_impianto(dict(base))
    con = dimensiona_impianto(dict(base, potenza_ev_kw=80))
    assert con["dlm"] and con["potenza_ricarica_kw"] == 80
    assert con["dorsale"]["in_a"] < senza["dorsale"]["in_a"], "con DLM la dorsale e' piu' piccola"
    assert con["contatti_indiretti"]["esito"] == "OK"
    assert dimensiona_impianto(dict(base, ra_ohm=300))["contatti_indiretti"]["esito"] == "NON OK"
    # wallbox senza RDC-DD: differenziale tipo B
    assert dimensiona_impianto(dict(base, rdc_dd_integrato=False))["linee"][0]["tipo_differenziale"] == "B"
    # potenza della ricarica oltre il POD: avviso
    assert any("POD" in a for a in dimensiona_impianto(dict(base, potenza_disponibile_kw=100))["avvisi"])
    print("[dlm] dorsale", senza["dorsale"]["in_a"], "A ->", con["dorsale"]["in_a"], "A con DLM 80 kW")


def test_portate_crescenti():
    for posa, t in PORTATE.items():
        for n, tab in t.items():
            v = list(tab.values())
            assert v == sorted(v), (posa, n)
            assert t[2][16] >= t[3][16]


def test_caso_a():
    """Configurazione dell'ottimizzatore sul Caso A -> progetto elettrico."""
    from datetime import time
    from app import engine, schemas

    cat = [schemas.HardwareSpec(nome="AC 22kW", potenza_kw=22, costo_acquisto_eur=1500, costo_installazione_eur=1000,
                                costo_manutenzione_eur_anno=0),
           schemas.HardwareSpec(nome="DC 30kW", potenza_kw=30, costo_acquisto_eur=12000, costo_installazione_eur=6000,
                                costo_manutenzione_eur_anno=0)]
    comuni = dict(profilo="Office", km_per_giro=108, giri_per_veicolo_giorno=1, tempo_disponibile_min=600,
                  finestra_inizio=time(8, 0), finestra_fine=time(18, 0), quota_ricarica_deposito=1.0, k_factor=1.1,
                  probabilita_utilizzo_pct=90, ricarica_domestica=False)
    gruppi = [schemas.FleetGroup(gruppo="PHEV", n_veicoli=41, consumo_kwh_km=0.17, batteria_kwh=12 / 0.7,
                                 contemporanei_max=41, potenza_max_ricarica_ac_kw=6.6, autonomia_elettrica_km=12 / 0.17,
                                 consumo_benzina_l100km=6.0, accetta_ricarica_dc=False, **comuni),
              schemas.FleetGroup(gruppo="BEV", n_veicoli=3, consumo_kwh_km=0.18, batteria_kwh=70, contemporanei_max=3,
                                 potenza_max_ricarica_ac_kw=9.0, accetta_ricarica_dc=True, **comuni)]
    pol = schemas.EnginePolicy(p_rete_kw=100, p_shaving_kw=100, max_ac_veicoli_per_punto=3, max_dc_veicoli_per_punto=6,
                               limite_ora_turno=18, hybrid_private_home_charging=False, company_buffer_pct=100)
    out = engine.run_optimize(schemas.OptimizeRequest(
        gruppi=gruppi, catalogo_hardware=cat, policy=pol,
        energia=schemas.EnergyCosts(prezzo_privato_eur_kwh=0.32, prezzo_pubblico_eur_kwh=0.69),
        tipi_hardware_da_esplorare=["AC 22kW", "DC 30kW"], budget_max_eur=200000, beam_size=4, patience=8, max_steps=100))
    cfg = out["soluzioni"][0]["config"]
    kw = {h.nome: h.potenza_kw for h in cat}
    req = {"punti": [{"tipo": t, "potenza_kw": kw[t], "quantita": q, "lunghezza_m": 10, "lunghezza_max_m": 45}
                     for t, q in cfg.items() if q > 0],
           "potenza_ev_kw": pol.p_shaving_kw, "potenza_disponibile_kw": 150, "ra_ohm": 25,
           "committente": "Caso A", "indirizzo": "Sede unica", "cod_progetto": "CASO-A"}
    pdf, d = genera_progetto_pdf(req)
    _coerenza(d)
    n_punti = sum(q for q in cfg.values())
    assert d["riepilogo"]["punti"] == n_punti and d["dlm"]
    assert d["potenza_ricarica_kw"] == 100 and d["dorsale"]["in_a"] == 160
    pagine = PdfReader(BytesIO(pdf)).pages
    a3 = [p for p in pagine if float(p.mediabox.width) > 1000]
    assert len(a3) >= 1, "manca la tavola A3 dell'unifilare"
    testo = "".join(p.extract_text() for p in pagine)
    for atteso in ("CAPITOLO 4", "Q1.1", "SCHEMA UNIFILARE", "Ra ≤", "DLM"):
        assert atteso in testo, atteso
    open("/tmp/progetto_caso_a.pdf", "wb").write(pdf)
    print(f"[caso A] {cfg} -> {len(pagine)} pagine ({len(a3)} A3), dorsale {d['dorsale']['cavo']} "
          f"In {d['dorsale']['in_a']} A, linee {d['riepilogo']['sezioni']} mm², ΔV max {d['riepilogo']['dv_max_raggiunta_pct']} %")


def test_endpoint():
    client = TestClient(app)
    h = {"x-access-token": auth.genera_token("email")}
    body = {"punti": [{"tipo": "AC 22kW", "potenza_kw": 22, "quantita": 3}], "committente": "Prova"}
    r = client.post("/api/v1/progetto-elettrico/pdf", json=body, headers=h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf" and r.content[:4] == b"%PDF"
    r = client.post("/api/v1/progetto-elettrico/pdf", json=dict(body, documento="unifilare"), headers=h)
    assert r.status_code == 200 and len(PdfReader(BytesIO(r.content)).pages) == 1
    r = client.post("/api/v1/progetto-elettrico/dimensiona", json=body, headers=h)
    assert r.status_code == 200 and len(r.json()["linee"]) == 3
    # linea impossibile (1 km a 22 kW con ΔV 1%): errore leggibile, non 500
    r = client.post("/api/v1/progetto-elettrico/dimensiona", headers=h, json={
        "punti": [{"tipo": "AC 22kW", "potenza_kw": 22, "quantita": 1, "lunghezza_m": 500}], "dv_max_pct": 1})
    assert r.status_code == 422, r.text
    r = client.post("/api/v1/progetto-elettrico/pdf", json=body)
    assert r.status_code == 401
    print("[endpoint] ok")


if __name__ == "__main__":
    test_calcoli_base()
    test_dlm_e_tt()
    test_portate_crescenti()
    test_caso_a()
    test_endpoint()
    print("tutti i test del progetto elettrico passati")
