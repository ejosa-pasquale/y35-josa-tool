"""
Test della ricarica gestita (V1G) e del check V2G nella diagnosi.

- Con il check spento la diagnosi e' identica: le nuove voci si aggiungono soltanto.
- Ricarica gestita: con il limite sulla sede la flotta non supera mai il contratto e non
  aggiunge piu' picco di quanto ne aggiunge senza gestione.
- V2G: il picco con la scarica non e' mai piu' alto della sola ricarica ottimizzata, le voci
  del risparmio tornano con il netto, ogni auto riparte con l'energia che le da' il motore.
- Casi limite: nessun gruppo bidirezionale, flotta di soli ibridi, degrado altissimo.

Uso: python3 test_v2g_check.py
"""
import math
from datetime import time

from app import schemas, engine, v2g_check
from app.diagnosi import run_diagnosi
from test_diagnosi import CATALOGO, curva_sintetica, csv_largo, _richiesta

N = 96


def sede_picco(h: float) -> float:
    """Ufficio con un picco forte e breve a meta' mattina."""
    return (60 + 70 * math.exp(-((h - 10.5) ** 2) / 0.8)) if 8 <= h < 18 else 20.0


def richiesta_bev(**v2g):
    v2g.setdefault("extra_costo_punto_eur", 5000.0)
    g = schemas.FleetGroup(gruppo="BEV", profilo="Office", n_veicoli=20, km_per_giro=50, giri_per_veicolo_giorno=1,
                           tempo_disponibile_min=540, finestra_inizio=time(8, 0), finestra_fine=time(17, 0),
                           quota_ricarica_deposito=1.0, consumo_kwh_km=0.18, batteria_kwh=60, contemporanei_max=20,
                           potenza_max_ricarica_ac_kw=11, ricarica_domestica=False, accetta_ricarica_dc=True,
                           probabilita_utilizzo_pct=90)
    pol = schemas.EnginePolicy(p_rete_kw=150, p_shaving_kw=150, hybrid_private_home_charging=False,
                               company_buffer_pct=100, sim_days=1, limite_ora_turno=17, max_ac_veicoli_per_punto=2)
    en = schemas.EnergyCosts(prezzo_privato_eur_kwh=0.25, prezzo_pubblico_eur_kwh=0.6,
                             prezzi_fasce={"prezzo_f1_eur_kwh": 0.30, "prezzo_f2_eur_kwh": 0.26, "prezzo_f3_eur_kwh": 0.22})
    return schemas.DiagnosiRequest(
        gruppi=[g], catalogo_hardware=CATALOGO, configurazione=schemas.HardwareConfig(quantita={"AC 22kW": 10}),
        policy=pol, energia=en, ottimizza_mix=False, v2g=schemas.V2GCheckIn(**v2g),
        sede=schemas.SedeInput(potenza_contrattuale_kw=160, carico_sede_kw=[sede_picco(s / 4) for s in range(N)]))


def test_spento_identico():
    acceso = run_diagnosi(_richiesta(potenza_contrattuale_kw=150, curva_csv=csv_largo(curva_sintetica())))
    req = _richiesta(potenza_contrattuale_kw=150, curva_csv=csv_largo(curva_sintetica()))
    spento = run_diagnosi(req.model_copy(update={"v2g": schemas.V2GCheckIn(attivo=False)}))
    assert spento["ricarica_gestita"] is None and spento["v2g"] is None
    for k in spento:
        if k not in ("ricarica_gestita", "v2g"):
            assert acceso[k] == spento[k], f"la voce '{k}' cambia con il check V2G acceso"
    print("[v2g] check acceso o spento: tutte le voci della diagnosi identiche")


def test_ricarica_gestita():
    r = run_diagnosi(_richiesta(potenza_contrattuale_kw=150, curva_csv=csv_largo(curva_sintetica())))
    g = r["ricarica_gestita"]
    senza, oggi, gestita, minimo = g["scenari"]
    for x in (gestita, minimo):
        assert x["minuti_oltre_contratto"] == 0 and x["picco_totale_kw"] <= 150.05
    assert senza["picco_aggiunto_dalla_flotta_kw"] >= gestita["picco_aggiunto_dalla_flotta_kw"] \
        >= minimo["picco_aggiunto_dalla_flotta_kw"]
    assert minimo["picco_totale_kw"] <= g["limite_minimo_kw"] + 0.05
    assert minimo["copertura_pct"] >= gestita["copertura_pct"] - 0.05, "il picco minimo non deve lasciare auto scariche"
    assert g["kw_evitati"] == round(senza["picco_aggiunto_dalla_flotta_kw"] - minimo["picco_aggiunto_dalla_flotta_kw"], 1)
    assert g["aumento_potenza_senza_gestione_kw"] > 0 and g["aumento_potenza_con_gestione_kw"] == 0
    assert abs(g["risparmio_netto_eur_anno"] - (g["risparmio_quota_potenza_eur_anno"]
                                                - g["costo_energia_mancante_eur_anno"])) <= 1
    # coerente con la diagnosi: le righe «come oggi» e «fino al contratto» sono quelle gia' calcolate
    assert gestita["picco_totale_kw"] == r["limite_variabile"]["picco_totale_kw"]
    assert oggi["picco_totale_kw"] == r["come_oggi"]["picco_totale_kw"]
    print(f"[v1g] Caso A: senza gestione +{senza['picco_aggiunto_dalla_flotta_kw']} kW, fino al contratto "
          f"+{gestita['picco_aggiunto_dalla_flotta_kw']} kW, picco minimo {g['limite_minimo_kw']} kW "
          f"(+{minimo['picco_aggiunto_dalla_flotta_kw']} kW); evitati {g['aumento_potenza_senza_gestione_kw']} kW di "
          f"aumento, {g['risparmio_una_tantum_eur']} EUR una tantum + {g['risparmio_netto_eur_anno']} EUR/anno")


def test_limite_uguale_al_motore():
    """Con il tetto uguale al contratto la simulazione e' la stessa «limite variabile» della diagnosi."""
    from app import diagnosi as ad
    req = _richiesta(potenza_contrattuale_kw=150, curva_csv=csv_largo(curva_sintetica()))
    sede = ad.profilo_sede(req.sede)
    base = {k: getattr(req, k) for k in schemas.SimulateRequest.model_fields}
    base["gantt_settimanale"] = False
    giorni = ad._simula_giorni(req, base, sede["settimana"], 150.0)
    for rif in giorni[:2]:
        sim = v2g_check.simula_con_limite(req, base, rif, sede["settimana"], 150.0)
        assert sim["timeline_p_kw"] == rif["var"]["timeline_p_kw"]
        assert sim["copertura_pct"] == rif["var"]["copertura_pct"]
        con_giornata = v2g_check.simula_con_limite(req, base, rif, sede["settimana"], 150.0, giornata=True)
        assert con_giornata["timeline_p_kw"] == rif["var"]["timeline_p_kw"]
    print("[v1g] tetto = contratto: stessa simulazione della diagnosi, anche con la giornata")


def test_v2g_coerenza():
    r = run_diagnosi(richiesta_bev())
    x = r["v2g"]
    assert x["verdetto"] in ("conviene", "conviene_se", "non_conviene") and x["risparmio"]
    v = x["risparmio"]
    assert v["netto_eur_anno"] == x["risparmio"]["netto_eur_anno"]
    assert abs(v["picco_eur_anno"] + v["energia_eur_anno"] + v["vendita_eur_anno"] + v["degrado_eur_anno"]
               - v["netto_eur_anno"]) <= 2, "le voci devono tornare con il netto (arrotondamenti)"
    assert x["picco_con_v2g_kw"] <= x["picco_solo_ricarica_kw"] + 1e-6
    assert all(c["picco_con_v2g_kw"] <= x["picco_solo_ricarica_kw"] + 0.1 for c in x["confronto_punti"])
    assert x["kwh_scaricati_giorno"] > 0 and x["finestre_scarica"]
    scelto = next(c for c in x["confronto_punti"] if c["punti"] == x["punti_bidirezionali"])
    assert all((c["payback_anni"] or 1e9) >= (scelto["payback_anni"] or 1e9) for c in x["confronto_punti"])
    assert len(x["serie"]) == N
    for l in x["leve"]:
        assert l["delta_eur_anno"] > 0 or (l["payback_anni"] or 1e9) < (x["payback_anni"] or 1e9)
    print(f"[v2g] 20 BEV, picco sede 130 kW: {x['punti_bidirezionali']} punti, picco "
          f"{x['picco_solo_ricarica_kw']} -> {x['picco_con_v2g_kw']} kW, netto {v['netto_eur_anno']} EUR/anno, "
          f"payback {x['payback_anni']} anni ({x['verdetto']}); scarica {x['finestre_scarica']}")


def test_v2g_autonomia():
    """Ogni auto riparte con almeno l'energia che le da' il motore."""
    req = richiesta_bev()
    r = run_diagnosi(req.model_copy(update={"v2g": schemas.V2GCheckIn(attivo=False)}))
    g = 0 if r["giorno_riferimento"] == "Lun" else ["Lun", "Mar", "Mer", "Gio", "Ven"].index(r["giorno_riferimento"])
    base = {k: getattr(req, k) for k in schemas.SimulateRequest.model_fields}
    sim = engine.run_simulate(schemas.SimulateRequest(**{**base, "giornata": True, "gantt_settimanale": False,
        "policy": req.policy.model_copy(update={"carico_sede_kw": [sede_picco(s / 4) for s in range(N)],
                                                "potenza_contrattuale_kw": 160.0})}), seed=g + 1, giorno=g)
    cand = v2g_check._candidati(sim["giornata"], {"BEV"}, {"BEV": 0.9})
    assert cand, "le BEV restano in sede a lungo: devono essere candidate"
    scelti = cand[:5]
    carico = [sede_picco(t / 4) for t in range(N)]
    res = v2g_check._dispatch(scelti, carico, [0.0] * N, [0.3] * N, req.v2g, req.policy, 0.2, 160.0, 11.0, True)
    assert res.successo
    for c, p in zip(scelti, res.piani_veicolo):
        socp = 100.0 * c["soc_partenza_kwh"] / c["batt"]
        assert p.soc_pct[c["b"] - 1] >= socp - 0.05, f"{c['id']} riparte con meno energia del motore"
        assert all(p.scarica_kw[t] == 0 and p.carica_kw[t] == 0 for t in range(N) if not (c["a"] <= t < c["b"]))
    print("[v2g] ogni auto riparte con l'energia del motore, nessuna carica/scarica fuori dalla sosta")


def test_senza_extra_costo():
    """Colonnina bidirezionale allo stesso prezzo di una normale (default): conviene se si
    risparmia qualcosa, e il numero di punti scelto e' quello che risparmia di piu'."""
    x = run_diagnosi(richiesta_bev(extra_costo_punto_eur=0.0))["v2g"]
    assert x["extra_capex_eur"] == 0 and x["payback_anni"] == 0
    assert x["verdetto"] == "conviene" and x["risparmio"]["netto_eur_anno"] > 0
    assert x["risparmio"]["netto_eur_anno"] == max(c["netto_eur_anno"] for c in x["confronto_punti"])
    assert not any("-30%" in l["leva"] for l in x["leve"])
    print(f"[v2g] nessun extra costo: {x['punti_bidirezionali']} punti, {x['risparmio']['netto_eur_anno']} EUR/anno, "
          f"picco {x['picco_solo_ricarica_kw']} -> {x['picco_con_v2g_kw']} kW")


def test_casi_limite():
    nessuno = run_diagnosi(richiesta_bev(gruppi_bidirezionali=[]))["v2g"]
    assert nessuno["verdetto"] == "non_calcolabile" and "bidirezionale" in nessuno["esito"]
    phev = run_diagnosi(_richiesta(potenza_contrattuale_kw=150, picco_sede_kw=95,
                                   ).model_copy(update={"gruppi": [g for g in _richiesta(potenza_contrattuale_kw=150,
                                   picco_sede_kw=95).gruppi if g.gruppo == "PHEV"]}))["v2g"]
    assert phev["verdetto"] == "non_calcolabile" and phev["gruppi_bidirezionali"] == []
    caro = run_diagnosi(richiesta_bev(costo_degrado_eur_kwh=2.0))["v2g"]
    assert caro["verdetto"] == "non_conviene" and caro["kwh_scaricati_giorno"] < 0.5
    fisso = run_diagnosi(richiesta_bev(punti_bidirezionali=3))["v2g"]
    assert fisso["punti_bidirezionali"] == 3 and len(fisso["confronto_punti"]) == 1
    print("[v2g] nessun gruppo bidirezionale, soli ibridi, degrado altissimo, punti fissati: risposte coerenti")


def test_api():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    corpo = richiesta_bev().model_dump(mode="json")
    r = c.post("/api/v1/diagnosi", json=corpo)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ricarica_gestita"]["scenari"] and d["v2g"]["verdetto"]
    corpo["v2g"] = {"attivo": False}
    assert c.post("/api/v1/diagnosi", json=corpo).json()["v2g"] is None
    print("[api] /api/v1/diagnosi con ricarica gestita e check V2G ok")


if __name__ == "__main__":
    test_spento_identico()
    test_ricarica_gestita()
    test_limite_uguale_al_motore()
    test_v2g_coerenza()
    test_v2g_autonomia()
    test_senza_extra_costo()
    test_casi_limite()
    test_api()
    print("\nOK: ricarica gestita e check V2G coerenti con la diagnosi, motore invariato.")
