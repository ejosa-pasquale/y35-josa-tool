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

# --- 6. Caso B dal chatbot: punti AC di gruppo vicini al report (85) ---
sedi_b = [("Sede 1", 69, 48.5, 66), ("Sede 2", 61, 61.7, 35), ("Sede 3", 57, 88.5, 30), ("Sede 4", 28, 69.9, 35), ("Sede 5", 22, 54.3, 30)]
out = run_chat_analysis(dict(profilo="Office", finestra_inizio="09:00", finestra_fine="17:00", p_rete_kw=100,
                             prezzo_carburante_eur_l=2.19,
                             sedi=[dict(nome=n, n_phev=round(v * .63), n_bev=round(v * .27), km_giornalieri=km, pct_casa=r)
                                   for n, v, km, r in sedi_b]))
gr = out["gruppo"]
print(f"[caso B] chat: {gr['punti_ac']} AC + {gr['punti_dc']} DC (report 85 + 5), energia {gr['energia_sede_kwh_giorno']:.0f} kWh")
assert abs(gr["punti_ac"] - 85) / 85 <= 0.15
assert all("Wallbox" not in k for s in out["sedi"] for k in s["configurazione_raccomandata"])

print("\nOK: il chatbot arriva al motore con gli stessi parametri del percorso completo.")
