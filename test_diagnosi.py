"""
Test della diagnosi energetica della ricarica (sede + flotta), su dati sintetici.

- Senza curva della sede il motore da' esattamente gli stessi risultati di prima.
- Con la curva il limite della ricarica diventa contratto - sede in ogni quarto d'ora:
  sede + flotta non superano mai il contratto.
- Import della curva POD nei formati lungo (anche con convenzione 00:15-24:00, virgola
  decimale, kWh) e largo (96 colonne per giorno).
- Profilo da bolletta: l'energia per fascia torna con quella fatturata.
- Endpoint /api/v1/diagnosi*.

Uso: python3 test_diagnosi.py
"""
import math
import random
from datetime import date, datetime, timedelta, time

from app import schemas, engine
from app.diagnosi import run_diagnosi
from josa_core import diagnosi as dg

CATALOGO = [
    schemas.HardwareSpec(nome="AC 22kW", potenza_kw=22, costo_acquisto_eur=1500,
                         costo_installazione_eur=1000, costo_manutenzione_eur_anno=0),
    schemas.HardwareSpec(nome="DC 30kW", potenza_kw=30, costo_acquisto_eur=12000,
                         costo_installazione_eur=6000, costo_manutenzione_eur_anno=0),
]
ENERGIA = schemas.EnergyCosts(prezzo_privato_eur_kwh=0.32, prezzo_pubblico_eur_kwh=0.69)
POLICY = schemas.EnginePolicy(p_rete_kw=100, p_shaving_kw=100, max_ac_veicoli_per_punto=4,
                              max_dc_veicoli_per_punto=6, limite_ora_turno=18,
                              hybrid_private_home_charging=False, company_buffer_pct=100, sim_days=1)
CONFIG = schemas.HardwareConfig(quantita={"AC 22kW": 15, "DC 30kW": 0})


def gruppi():
    """Caso A anonimizzato: 41 PHEV + 3 BEV, una sede, finestra 08-18."""
    comuni = dict(profilo="Office", km_per_giro=108, giri_per_veicolo_giorno=1, tempo_disponibile_min=600,
                  finestra_inizio=time(8, 0), finestra_fine=time(18, 0), quota_ricarica_deposito=1.0,
                  k_factor=1.1, ricarica_domestica=False, probabilita_utilizzo_pct=90)
    return [
        schemas.FleetGroup(gruppo="PHEV", n_veicoli=41, consumo_kwh_km=0.17, batteria_kwh=12 / 0.9,
                           contemporanei_max=41, potenza_max_ricarica_ac_kw=6.6, autonomia_elettrica_km=12 / 0.17,
                           consumo_benzina_l100km=6.0, accetta_ricarica_dc=False, **comuni),
        schemas.FleetGroup(gruppo="BEV", n_veicoli=3, consumo_kwh_km=0.18, batteria_kwh=70, contemporanei_max=3,
                           potenza_max_ricarica_ac_kw=9.0, accetta_ricarica_dc=True, **comuni),
    ]


def sede_ufficio(h: float) -> float:
    if 8 <= h < 18:
        return 70 + 25 * math.exp(-((h - 11) ** 2) / 2) + 20 * math.exp(-((h - 15) ** 2) / 2)
    return 25.0


def curva_sintetica(giorni=28, seed=7):
    """Curva quartoraria sintetica di un ufficio: (istante di inizio, kW)."""
    rnd = random.Random(seed)
    out = []
    t0 = datetime(2026, 9, 7)  # lunedi'
    for d in range(giorni):
        g = (t0 + timedelta(days=d)).weekday()
        for s in range(96):
            h = s / 4
            kw = sede_ufficio(h) if g < 5 else 22.0
            out.append((t0 + timedelta(days=d, minutes=15 * s), max(0.0, kw * rnd.uniform(0.93, 1.07))))
    return out


def csv_lungo_fine_intervallo(punti):
    """Formato tipico di un export: POD in testa, data e ora di fine quarto d'ora
    (00:15 ... 24:00), energia in kWh con la virgola decimale."""
    righe = ["POD;Data;Ora;Energia attiva prelevata (kWh)"]
    for t, kw in punti:
        fine = t + timedelta(minutes=15)
        ora = "24:00" if fine.hour == 0 and fine.minute == 0 else fine.strftime("%H:%M")
        righe.append(f"IT001E00000000;{t.strftime('%d/%m/%Y')};{ora};" + f"{kw / 4:.4f}".replace(".", ","))
    return "\n".join(righe)


def csv_largo(punti):
    per_giorno = {}
    for t, kw in punti:
        per_giorno.setdefault(t.date(), []).append(kw)
    righe = ["Data;" + ";".join(f"Q{i + 1}" for i in range(96)) + ";unita kW"]
    for d, vals in per_giorno.items():
        righe.append(d.strftime("%Y-%m-%d") + ";" + ";".join(f"{v:.2f}" for v in vals))
    return "\n".join(righe)


def test_senza_sede_identico():
    base = engine.run_simulate(schemas.SimulateRequest(gruppi=gruppi(), catalogo_hardware=CATALOGO,
                                                       configurazione=CONFIG, policy=POLICY, energia=ENERGIA))
    pol = POLICY.model_copy(update={"carico_sede_kw": None, "potenza_contrattuale_kw": None})
    stessa = engine.run_simulate(schemas.SimulateRequest(gruppi=gruppi(), catalogo_hardware=CATALOGO,
                                                         configurazione=CONFIG, policy=pol, energia=ENERGIA))
    assert base["timeline_p_kw"] == stessa["timeline_p_kw"]
    assert base["copertura_pct"] == stessa["copertura_pct"]
    print(f"[identico] senza curva della sede: copertura {base['copertura_pct']}%, curva invariata")


def test_limite_variabile():
    sede = [round(sede_ufficio(s / 4), 2) for s in range(96)]
    risultati = {}
    for contratto in (150, 130, 110):
        pol = POLICY.model_copy(update={"carico_sede_kw": sede, "potenza_contrattuale_kw": contratto})
        r = engine.run_simulate(schemas.SimulateRequest(gruppi=gruppi(), catalogo_hardware=CATALOGO,
                                                        configurazione=CONFIG, policy=pol, energia=ENERGIA))
        tl = r["timeline_p_kw"]
        picco_sito = max(tl[i] + sede[i % 96] for i in range(len(tl)))
        assert picco_sito <= contratto + 1e-6, (contratto, picco_sito)
        risultati[contratto] = r["copertura_pct"]
    assert risultati[150] >= risultati[130] >= risultati[110]
    print(f"[limite variabile] sede + flotta entro il contratto; copertura 150/130/110 kW: "
          f"{risultati[150]}% / {risultati[130]}% / {risultati[110]}%")


def test_import_curva():
    punti = curva_sintetica()
    attesa_picco = max(kw for _, kw in punti)
    for nome, testo in (("lungo 00:15-24:00 kWh", csv_lungo_fine_intervallo(punti)), ("largo kW", csv_largo(punti))):
        c = dg.importa_curva_pod(testo)
        assert c["giorni_letti"] == 28, (nome, c["giorni_letti"])
        assert abs(c["picco_kw"] - attesa_picco) < 0.05, (nome, c["picco_kw"], attesa_picco)
        assert len(c["settimana_p90_kw"]) == 672
        # le 10:00 di un feriale: tra la media e il massimo dei giorni
        assert 80 < c["settimana_media_kw"][40] <= c["settimana_p90_kw"][40] < 110, nome
        # domenica bassa
        assert c["settimana_media_kw"][6 * 96 + 40] < 30, nome
        print(f"[curva POD] {nome}: {c['giorni_letti']} giorni, picco {c['picco_kw']} kW il {c['picco_quando']}")


def test_profilo_bolletta():
    p = dg.profilo_da_fasce("ufficio", kwh_f1=12000, kwh_f2=3000, kwh_f3=4500, giorni=30)
    energia = {"F1": 0.0, "F2": 0.0, "F3": 0.0}
    for i, kw in enumerate(p):
        energia[dg.fascia(i // 96, i % 96)] += kw * 0.25
    for f, kwh in (("F1", 12000), ("F2", 3000), ("F3", 4500)):
        assert abs(energia[f] * 30 / 7 - kwh) / kwh < 0.01, (f, energia[f])
    print(f"[bolletta] fasce ricostruite: picco stimato {max(p):.0f} kW")


def _richiesta(**sede):
    return schemas.DiagnosiRequest(gruppi=gruppi(), catalogo_hardware=CATALOGO, configurazione=CONFIG,
                                   policy=POLICY, energia=ENERGIA, sede=schemas.SedeInput(**sede))


def test_diagnosi_livelli():
    r0 = run_diagnosi(_richiesta(potenza_contrattuale_kw=150, picco_sede_kw=95, tipo_sede="ufficio"))
    r1 = run_diagnosi(_richiesta(potenza_contrattuale_kw=150,
                                 bolletta={"kwh_f1": 15000, "kwh_f2": 3500, "kwh_f3": 5000, "giorni": 30}))
    r2 = run_diagnosi(_richiesta(potenza_contrattuale_kw=150, curva_csv=csv_largo(curva_sintetica())))
    assert (r0["livello"], r1["livello"], r2["livello"]) == (0, 1, 2)
    for r in (r0, r1, r2):
        assert r["limite_variabile"]["minuti_oltre_contratto"] == 0
        assert r["limite_variabile"]["picco_totale_kw"] <= 150.05
        assert len(r["serie_giorno"]) == 96 and r["ipotesi"]
    c = r2["come_oggi"]
    assert c["minuti_oltre_contratto"] > 0 and r2["opzioni"], "con questa curva il Caso A sfora alle 17:30"
    print(f"[diagnosi] curva POD: oggi picco {c['picco_totale_kw']} kW, {c['minuti_oltre_contratto']} min oltre; "
          f"limite variabile copertura {r2['limite_variabile']['copertura_pct']}%; esito: {r2['esito']}")
    largo = run_diagnosi(_richiesta(potenza_contrattuale_kw=400, picco_sede_kw=95))
    assert largo["come_oggi"]["minuti_oltre_contratto"] == 0 and not largo["opzioni"]
    print("[diagnosi] contratto ampio: nessuna finestra critica, nessuna opzione proposta")


def test_giorni_dinamici():
    """Ogni feriale ha il suo scarico e la sua ricarica: curve diverse, limite rispettato ogni giorno."""
    r = run_diagnosi(_richiesta(potenza_contrattuale_kw=150, curva_csv=csv_largo(curva_sintetica())))
    g = r["giorni"]
    assert [x["giorno"] for x in g] == dg.GIORNI[:5]
    assert len({x["energia_flotta_kwh"] for x in g}) > 1, "i feriali hanno la stessa ricarica"
    assert all(x["picco_totale_limite_variabile_kw"] <= 150.05 for x in g)
    assert r["giorno_riferimento"] == max(g, key=lambda x: x["picco_totale_kw"])["giorno"]
    flotta = r["settimana_flotta_kw"]
    assert len(flotta) == 672 and flotta[96 * 0:96] != flotta[96:192] and not any(flotta[5 * 96:])
    # stessa richiesta, stesso risultato
    assert run_diagnosi(_richiesta(potenza_contrattuale_kw=150, curva_csv=csv_largo(curva_sintetica())))["giorni"] == g
    print("[giorni] " + ", ".join(f"{x['giorno']} {x['energia_flotta_kwh']:.0f} kWh picco {x['picco_totale_kw']:.0f} kW"
                                  for x in g) + f"; riferimento {r['giorno_riferimento']}")


def test_mix_con_sede():
    """Con la sede l'ottimizzatore puo' cambiare il mix; il consigliato regge il contratto ogni giorno."""
    r = run_diagnosi(_richiesta(potenza_contrattuale_kw=130, curva_csv=csv_largo(curva_sintetica())))
    m = r["mix"]
    assert m["inserito"]["config"] == {"AC 22kW": 15}
    assert m["consigliato"] and m["ottimo_senza_sede"]
    assert len(m["verifica_giorni"]) == 5
    assert all(g["minuti_oltre_contratto"] == 0 and g["picco_totale_kw"] <= 130.05 for g in m["verifica_giorni"])
    senza = run_diagnosi(_richiesta(potenza_contrattuale_kw=130, curva_csv=csv_largo(curva_sintetica())).model_copy(
        update={"ottimizza_mix": False}))
    assert senza["mix"] is None
    c, o = m["consigliato"], m["ottimo_senza_sede"]
    print(f"[mix] senza sede {o['n_ac']} AC + {o['n_dc']} DC, {o['capex_eur']:.0f} EUR; con la sede a 130 kW "
          f"{c['n_ac']} AC + {c['n_dc']} DC, {c['capex_eur']:.0f} EUR, copertura minima "
          f"{min(g['copertura_pct'] for g in m['verifica_giorni'])}%")


def test_ottimizzatore_rumore_float():
    """A parita' di servizio vince la configurazione piu' economica, anche se i kWh
    scoperti differiscono solo per rumore da virgola mobile."""
    from josa_core import optimizer
    ctx = optimizer.OptimizerContext(hw_db={}, budget_max=500000, p_rete=100, p_shaving=100, fleet_nv=44,
                                     h_limit=18, h_plug=18, policy_mode="asap")
    def res(capex, gap):
        k = {"c_cap": capex, "perc": 79.4, "company_buffer_gap_kwh": gap, "e_ext": gap}
        return {"kpi": k, "stress": {"kpi": dict(k)}, "config": {"AC 22kW": int(capex // 2500)}}
    economica, cara = res(27500, 109.33333333333354), res(65000, 109.33333333333348)
    assert optimizer.score(ctx, economica) < optimizer.score(ctx, cara)
    print("[ottimizzatore] differenze da 1e-13 kWh non battono il costo")


def test_errori():
    for sede in ({"potenza_contrattuale_kw": 150},
                 {"potenza_contrattuale_kw": 150, "curva_csv": "solo testo\nsenza date"}):
        try:
            run_diagnosi(_richiesta(**sede))
        except ValueError:
            continue
        raise AssertionError(f"doveva fallire: {sede}")
    try:
        schemas.EnginePolicy(p_rete_kw=100, p_shaving_kw=100, carico_sede_kw=[1.0] * 50, potenza_contrattuale_kw=100)
    except Exception:
        pass
    else:
        raise AssertionError("curva della sede di 50 valori accettata")
    print("[errori] input mancanti o malformati rifiutati con un messaggio chiaro")


def test_api():
    import os
    os.environ.pop("ACCESS_PASSWORD", None)
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    corpo = _richiesta(potenza_contrattuale_kw=150, picco_sede_kw=95).model_dump(mode="json")
    r = c.post("/api/v1/diagnosi", json=corpo)
    assert r.status_code == 200, r.text[:300]
    r = c.post("/api/v1/diagnosi/curva-pod", json={"curva_csv": csv_largo(curva_sintetica(giorni=7))})
    assert r.status_code == 200 and r.json()["giorni_letti"] == 7, r.text[:300]
    r = c.post("/api/v1/diagnosi/profilo-bolletta", json={"bolletta": {"kwh_f1": 1000, "kwh_f2": 200, "kwh_f3": 300}})
    assert r.status_code == 200 and len(r.json()["settimana_kw"]) == 672
    r = c.post("/api/v1/diagnosi", json={**corpo, "sede": {"potenza_contrattuale_kw": 150}})
    assert r.status_code == 422
    print("[api] /api/v1/diagnosi, /curva-pod, /profilo-bolletta ok")


if __name__ == "__main__":
    test_senza_sede_identico()
    test_limite_variabile()
    test_import_curva()
    test_profilo_bolletta()
    test_diagnosi_livelli()
    test_giorni_dinamici()
    test_mix_con_sede()
    test_ottimizzatore_rumore_float()
    test_errori()
    test_api()
    print("\nOK: diagnosi energetica — limite variabile, curva POD, bolletta e API verificati.")
