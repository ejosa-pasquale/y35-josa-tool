"""
Chatbot: il profilo conversazionale deve arrivare al motore con gli stessi
parametri del percorso completo (casi reali anonimizzati, vedi test_casi_reali.py).
Non chiama l'API di Claude: simula il profilo che Claude estrarrebbe.

Uso: python3 test_chat.py
"""
from josa_core.chat_engine import profile_to_analysis_payload, BUDGET_SENZA_VINCOLO_EUR
from app.chat_analysis import run_chat_analysis

BASE = dict(km_giornalieri=60, finestra_inizio="09:00", finestra_fine="17:00", p_rete_kw=100)

# --- 1. Le wallbox domestiche non sono colonnine di sede ---
cat_con_wallbox = [
    {"nome": "Colonnina AC trifase", "potenza_kw": 22, "costo_acquisto_eur": 1500, "costo_installazione_eur": 1000, "costo_manutenzione_eur_anno": 0},
    {"nome": "Wallbox 7.4kW casa", "potenza_kw": 7.4, "costo_acquisto_eur": 700, "costo_installazione_eur": 800, "costo_manutenzione_eur_anno": 0},
]
p = profile_to_analysis_payload(dict(BASE, n_phev=10, n_bev=5, pct_casa=60), cat_con_wallbox)
assert all("casa" not in h["nome"].lower() for h in p["catalogo_hardware"])
assert p["policy"]["hybrid_private_home_charging"] and p["gruppi"][0]["pct_veicoli_con_casa"] == 60
p = profile_to_analysis_payload(dict(BASE, n_phev=10, n_bev=5, pct_casa=60))
ac = next(h for h in p["catalogo_hardware"] if h["nome"] == "Colonnina AC trifase")
assert ac["costo_acquisto_eur"] + ac["costo_installazione_eur"] == 2500
print("[chat] wallbox domestiche fuori dal catalogo di sede, punto AC 2.500 EUR")

# --- 2. Mix PHEV/BEV: due gruppi con i parametri calibrati ---
g = {x["gruppo"]: x for x in p["gruppi"]}
assert g["PHEV"]["n_veicoli"] == 10 and g["BEV"]["n_veicoli"] == 5
assert g["PHEV"]["consumo_kwh_km"] == 0.17 and abs(g["PHEV"]["batteria_kwh"] * 0.7 - 12) < 0.01
assert g["PHEV"]["accetta_ricarica_dc"] is False and g["PHEV"]["potenza_max_ricarica_ac_kw"] == 6.6
assert g["BEV"]["consumo_kwh_km"] == 0.18 and g["BEV"]["batteria_kwh"] == 70
assert g["PHEV"]["profilo"] == "Office" and g["PHEV"]["contemporanei_max"] == 10
# "ibrido" e' un'alimentazione, non un uso: resta profilo Office
p = profile_to_analysis_payload(dict(BASE, n_veicoli=12, profilo="Ibrido", alimentazione_attuale="PHEV"))
assert [x["gruppo"] for x in p["gruppi"]] == ["PHEV"] and p["gruppi"][0]["profilo"] == "Office"
assert any("mix PHEV/BEV non indicato" in x for x in run_chat_analysis(dict(BASE, n_veicoli=4, alimentazione_attuale="PHEV"))["ipotesi"])
print("[chat] mix PHEV/BEV in due gruppi con batteria e consumi calibrati")

# --- 3. Niente valori nascosti ---
p = profile_to_analysis_payload(dict(BASE, n_bev=8))
assert p["budget_max_eur"] == BUDGET_SENZA_VINCOLO_EUR and not p["budget_indicato"]
assert any("energia in azienda: 0.32" in x and "ipotesi standard Y35" in x for x in p["ipotesi"])
p = profile_to_analysis_payload(dict(BASE, n_bev=8, budget_eur=50000, prezzo_energia_aziendale_eur_kwh=0.25))
assert p["budget_max_eur"] == 50000 and not any("energia in azienda" in x for x in p["ipotesi"])
try:
    profile_to_analysis_payload(dict(BASE, n_bev=8, p_rete_kw=None))
    raise AssertionError("senza potenza disponibile non deve partire")
except ValueError as e:
    assert "potenza" in str(e)
print("[chat] budget non indicato = nessun vincolo, potenza obbligatoria, ipotesi dichiarate")

# --- 4. Piu' sedi in una conversazione, con totale di gruppo ---
out = run_chat_analysis(dict(finestra_inizio="09:00", finestra_fine="17:00", sedi=[
    dict(nome="Nord", n_phev=8, n_bev=2, km_giornalieri=50, p_rete_kw=60),
    dict(nome="Sud", n_phev=4, n_bev=0, km_giornalieri=40, p_rete_kw=30, pct_casa=50)]))
assert [s["nome"] for s in out["sedi"]] == ["Nord", "Sud"]
assert out["gruppo"]["n_veicoli"] == 14
assert out["gruppo"]["capex_eur"] == sum(s["capex_eur"] for s in out["sedi"])
assert out["sedi"][1]["adeguamento_rete_eur"] == 20000  # sede minore, ipotesi standard Y35
assert out["soluzioni"] == out["sedi"][0]["soluzioni"]  # compatibile con il frontend
print("[chat] due sedi, totale di gruppo coerente")

# --- 5. Caso A dal chatbot = percorso completo (15 AC, ~531 kWh, report 13-15 AC e ~544 kWh) ---
caso_a = dict(n_veicoli=44, n_phev=41, n_bev=3, km_giornalieri=108, profilo="Office", finestra_inizio="08:00",
              finestra_fine="18:00", pct_casa=0, p_rete_kw=100, prezzo_energia_aziendale_eur_kwh=0.32,
              prezzo_energia_pubblica_eur_kwh=0.66, prezzo_carburante_eur_l=1.80, presenza_pct=90)
s = run_chat_analysis(caso_a)["sedi"][0]
n_ac = s["configurazione_raccomandata"].get("Colonnina AC trifase", 0)
print(f"[caso A] chat: {s['configurazione_raccomandata']} | energia {s['energia_sede_kwh_giorno']:.0f} kWh | "
      f"payback {s['business_case']['payback_mesi']} mesi")
assert abs(s["energia_sede_kwh_giorno"] - 544) / 544 <= 0.10
assert 13 <= n_ac <= 16 and s["verifica_settimanale"]["superata"]

# --- 6. Caso B dal chatbot: punti AC totali (il tool non conosce i punti esistenti se non indicati) ---
# Il report dimensiona 85 AC + 5 DC in totale (35 AC gia' esistenti + 50 nuovi) sul caso
# peggiore: il 70% di chi ha la wallbox ricarica in sede. Il tool dimensiona sul giorno
# normale (ogni sede supera la verifica) e mostra il caso peggiore come stress test.
sedi_b = [("Sede 1", 69, 48.5, 66, 10), ("Sede 2", 61, 61.7, 35, 20), ("Sede 3", 57, 88.5, 30, 5),
          ("Sede 4", 28, 69.9, 35, 0), ("Sede 5", 22, 54.3, 30, 0)]
out = run_chat_analysis(dict(profilo="Office", finestra_inizio="09:00", finestra_fine="17:00", p_rete_kw=100,
                             prezzo_carburante_eur_l=2.19,
                             sedi=[dict(nome=n, n_phev=round(v * .63), n_bev=round(v * .27), km_giornalieri=km,
                                        pct_casa=r, punti_ac_esistenti=e) for n, v, km, r, e in sedi_b]))
gr = out["gruppo"]
st = gr["stress_wallbox_non_usata"]
print(f"[caso B] chat: {gr['punti_ac']} AC totali nel giorno normale ({gr['punti_ac_nuovi']} nuovi), "
      f"{st['punti_ac_necessari']} nel caso peggiore (report 85 AC + 5 DC)")
assert gr["tutte_verificate"], "con la ricarica a casa parziale ogni sede deve superare la verifica"
assert all("Wallbox" not in k for s in out["sedi"] for k in s["configurazione_raccomandata"])
assert abs(st["punti_ac_necessari"] - 85) / 85 <= 0.15 and st["punti_ac_in_piu"] > 0
assert gr["punti_ac_nuovi"] < gr["punti_ac"] and gr["capex_hardware_eur"] == 2500 * gr["punti_ac_nuovi"]

# --- 7. Orario di partenza: con le auto che la sera vanno via la ricarica finisce entro la finestra ---
from app import schemas, engine
for fine, fine_h in (("17:00", 17.0), ("13:00", 13.0)):
    p = profile_to_analysis_payload(dict(n_bev=30, km_giornalieri=120, finestra_inizio="09:00", finestra_fine=fine,
                                         p_rete_kw=100, notte_in_sede=False))
    r = engine.run_simulate(schemas.SimulateRequest(
        gruppi=[schemas.FleetGroup(**g) for g in p["gruppi"]],
        catalogo_hardware=[schemas.HardwareSpec(**h) for h in p["catalogo_hardware"]],
        policy=schemas.EnginePolicy(**p["policy"]),
        configurazione=schemas.HardwareConfig(quantita={"Colonnina AC trifase": 12})))
    ricariche = [x for v in r["gantt_veicoli"] for x in v["segmenti"] if x.get("colonnina")]
    picco, energia_curva = max(r["timeline_p_kw"]), sum(r["timeline_p_kw"]) * 0.25
    print(f"[orario] finestra 09:00-{fine}: ricariche fino alle {max(x['fine'] for x in ricariche):.2f}, "
          f"picco {picco:.0f} kW, copertura {r['copertura_pct']:.0f}%")
    assert max(x["fine"] for x in ricariche) <= fine_h + 1e-6
    assert 0 < picco <= 100 + 1e-6, "la curva di potenza deve vedere la ricarica DLM e restare entro i kW disponibili"
    assert abs(energia_curva - r["kpi"]["e_int"]) <= 1.0
corta = r["copertura_pct"]
assert corta < 90, "4 ore a 100 kW non bastano per 30 auto da 120 km: la finestra deve pesare"
p = profile_to_analysis_payload(dict(n_bev=5, km_giornalieri=50, finestra_inizio="09:00", finestra_fine="17:00", p_rete_kw=50))
assert p["gruppi"][0]["ricarica_notturna_azienda"] is False and p["notte_in_sede_ipotesi"]
p = profile_to_analysis_payload(dict(n_bev=5, km_giornalieri=50, profilo="Pool", finestra_inizio="09:00", finestra_fine="17:00", p_rete_kw=50))
assert p["gruppi"][0]["ricarica_notturna_azienda"] is True
print("[orario] fine finestra rispettata, picco entro i kW disponibili")

print("\nOK: il chatbot arriva al motore con gli stessi parametri del percorso completo.")


# --- 8. Caso C (calibrazioni dal report): wallbox nel CAPEX, rete a parte, calendario NLT,
#        km per gruppo, spesa carburante reale, quota pubblica ---
from app.chat_analysis import _n_wallbox, _piano_temporale
assert _n_wallbox({"pct_casa": 50, "gruppi": [{"n_veicoli": 31}, {"n_veicoli": 7}]}) == 20  # 15,5 -> 16 + 3,5 -> 4
rampa, capex_anni = _piano_temporale([6.0, 18.0], 0, 2, 1000.0)
assert rampa[0] == 0.25 and rampa[2] == 1.0 and sum(capex_anni) == 1000

CAT_C = [{"nome": "Colonnina AC trifase", "potenza_kw": 22.0, "costo_acquisto_eur": 1200.0, "costo_installazione_eur": 800.0,
          "costo_manutenzione_eur_anno": 0.0},
         {"nome": "DC 30kW", "potenza_kw": 30.0, "costo_acquisto_eur": 11000.0, "costo_installazione_eur": 6000.0,
          "costo_manutenzione_eur_anno": 0.0}]
caso_c = dict(profilo="Office", finestra_inizio="08:30", finestra_fine="16:30", notte_in_sede=False,
              prezzo_energia_aziendale_eur_kwh=0.25, prezzo_energia_domestica_eur_kwh=0.25,
              prezzo_energia_pubblica_eur_kwh=0.65, prezzo_carburante_eur_l=2.20, presenza_pct=90,
              consumo_kwh_km_bev=0.20, consumo_kwh_km_phev=0.20, quota_ricarica_pubblica_pct=10,
              conversioni_per_anno=[12, 14, 6, 7], adeguamento_rete_nel_payback=False,
              sedi=[dict(nome="Sede 1", n_bev=31, n_phev=7, n_bev_esistenti=1, n_phev_esistenti=2,
                         km_giornalieri_bev=170, km_giornalieri_phev=80, pct_casa=50, p_rete_kw=100,
                         adeguamento_rete_eur=50000, spesa_carburante_eur_anno=137157),
                    dict(nome="Sede 2", n_bev=4, n_phev=0, km_giornalieri_bev=151, pct_casa=50, p_rete_kw=3500,
                         adeguamento_rete_eur=20000, spesa_carburante_eur_anno=15675)])
out = run_chat_analysis(caso_c, CAT_C)
g, s1 = out["gruppo"], out["sedi"][0]
bc = g["business_case"]
print(f"[caso C] chat: {s1['configurazione_raccomandata']} + {g['n_wallbox_casa']} wallbox, CAPEX {g['capex_eur']:.0f} "
      f"(report 67.000), risparmio {g['risparmio_a_regime_eur']:.0f} (report 71.128), payback {bc['payback_mesi']} mesi, "
      f"con rete {bc['con_adeguamento_rete']['payback_mesi']} mesi (report 32 e 47)")
assert g["n_wallbox_casa"] == 22 and g["capex_eur"] == 67000, "17 punti AC + 22 wallbox come nel report"
assert g["capex_eur"] == g["capex_hardware_eur"] + g["capex_wallbox_casa_eur"], "la rete e' una voce a parte"
assert abs(g["risparmio_a_regime_eur"] - 71128) / 71128 <= 0.10
assert s1["economia"]["quota_elettrica_phev_pct"] == 75.0  # 12 kWh / 0,20 = 60 km su 80
assert s1["economia"]["base_carburante"] == "spesa reale"
assert s1["economia"]["veicoli_nel_risparmio"] == 35       # i 3 plug-in gia' in flotta restano fuori
assert bc["da_calendario_nlt"] and bc["rampa_adozione"][0] < 0.30 and len(bc["capex_per_anno_eur"]) > 1
assert bc["con_adeguamento_rete"]["payback_mesi"] > bc["payback_mesi"]
base = next(sc for sc in s1["verifica_settimanale"]["scenari"] if sc["scenario"] == "Base")
assert 13 <= s1["configurazione_raccomandata"].get("Colonnina AC trifase", 0) <= 16 and base["copertura_media_pct"] >= 99.0

# Chi ha la wallbox non si collega in sede (come nel report del caso C): energia ~519 kWh
caso_c["quota_sede_con_wallbox_pct"] = 0
s1 = run_chat_analysis(caso_c, CAT_C)["sedi"][0]
print(f"[caso C] chi ha la wallbox non carica in sede: energia {s1['energia_sede_kwh_giorno']:.0f} kWh (report 519)")
assert abs(s1["energia_sede_kwh_giorno"] - 519) / 519 <= 0.10
print("[caso C] CAPEX, risparmio, calendario e quota in sede coerenti con il report")


# --- 9. Punti gia' installati: il tool calcola il totale, il CAPEX va solo sui nuovi ---
from app.chat_analysis import _punti_nuovi
assert _punti_nuovi({"Colonnina AC trifase": 18, "DC 30kW": 2}, {"punti_ac_esistenti": 10}) == {"Colonnina AC trifase": 8, "DC 30kW": 2}
assert _punti_nuovi({"Colonnina AC trifase": 7}, {"punti_ac_esistenti": 10}) == {}
out = run_chat_analysis(dict(n_phev=10, n_bev=4, km_giornalieri=60, finestra_inizio="09:00", finestra_fine="17:00",
                             p_rete_kw=100, punti_ac_esistenti=3))
s = out["sedi"][0]
n_tot = s["configurazione_raccomandata"].get("Colonnina AC trifase", 0)
assert s["punti_nuovi"].get("Colonnina AC trifase", 0) == max(0, n_tot - 3)
assert s["capex_hardware_eur"] == 2500 * max(0, n_tot - 3)
print(f"[chat] punti esistenti: totale {n_tot} AC, 3 gia' installati, CAPEX solo sui {n_tot - 3} nuovi")
