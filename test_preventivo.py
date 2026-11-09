"""
Verifica di un preventivo caricato in PDF (Caso D, anonimizzato: due parcheggi,
19 AC 22 kW + 2 DC 60 kW, 29 auto reali).

- Il parser legge sedi, punti, potenze e totale dal testo del preventivo.
- La verifica nei due sensi: il preventivo regge molte piu' auto di quelle reali
  (sovradimensionato con 4 auto per punto AC) e per le auto reali bastano meno punti.

Uso: python3 test_preventivo.py
"""
from app import schemas, engine
from josa_core.preventivo import analizza_testo

TESTO = """
La proposta comprende:
 n. 19 stazioni di ricarica AC 22 kW trifase e n. 2 stazioni DC Fast fino a 60 kW;
 n. 2 quadri elettrici 1000 A da esterni, predisposti per l'espansione futura;
Configurazione dell'impianto
VOCE PARCHEGGIO OVEST · SEDE PARCHEGGIO EST · NUOVA
PALAZZINA
Potenza disponibile al quadro 350 kW 280 kW
Stazioni AC 22 kW trifase 13 wallbox a parete 6 colonnine a terra
Stazioni DC Fast fino a 60 kW 1 1
Potenza nominale installata fino a 346 kW fino a 192 kW
Predisposizione quadro 15 AC + 1 DC 20 AC + 2 DC
Descrizione della fornitura
Parcheggio Ovest
 n. 1 colonnina di ricarica DC Fast fino a 60 kW;
 n. 13 wallbox da muro AC 1×22 kW;
 n. 1 quadro elettrico 1000 A da esterni configurato con: n. 15 interruttori differenziali
Parcheggio Est
 n. 1 colonnina di ricarica DC Fast fino a 60 kW;
 n. 6 colonnine per fissaggio a terra AC 1×22 kW;
Opere elettriche
Parcheggio Ovest
 installazione e collegamento di n. 13 wallbox e n. 1 colonnina DC;
Opere civili
Parcheggio Est
 fornitura e posa di n. 6 plinti in CLS per colonnine AC 22 kW;
Totale imponibile 191.500,00 €
"""

p = analizza_testo(TESTO)
print("[parser]", [(s["nome"], s["n_ac"], s["kw_ac"], s["n_dc"], s["kw_dc"], s["potenza_disponibile_kw"]) for s in p["sedi"]],
      p["totale_imponibile_eur"], p["avvisi"])
assert [(s["n_ac"], s["n_dc"]) for s in p["sedi"]] == [(13, 1), (6, 1)]
assert [s["potenza_disponibile_kw"] for s in p["sedi"]] == [350, 280]
assert all(s["kw_ac"] == 22 and s["kw_dc"] == 60 for s in p["sedi"])
assert p["totale_imponibile_eur"] == 191500 and not p["avvisi"]
assert p["sedi"][1]["predisposizione"] == {"n_ac": 20, "n_dc": 2}

# Solo riepilogo (nessuna ripartizione per sede): una sede unica
u = analizza_testo("Offerta: n. 8 wallbox AC 22 kW e n. 1 colonnina DC 50 kW. Totale 40.000,00 € + IVA")
assert [(s["n_ac"], s["n_dc"], s["kw_dc"]) for s in u["sedi"]] == [(8, 1, 50)], u
assert u["totale_imponibile_eur"] == 40000

# Verifica nei due sensi
req = schemas.VerificaPreventivoRequest(
    n_auto_totali=29, totale_imponibile_eur=191500, rotazioni=[1, 4],
    sedi=[schemas.PreventivoSedeIn(nome=s["nome"], n_ac=s["n_ac"], kw_ac=s["kw_ac"], n_dc=s["n_dc"],
                                   kw_dc=s["kw_dc"], potenza_disponibile_kw=s["potenza_disponibile_kw"])
          for s in p["sedi"]])
r = engine.run_verifica_preventivo(req)
t = r["totale"]
print("[verifica]", r["esito"], "capacita'", t["capacita_per_rotazione"], "AC ideali", t["ac_ideali"],
      "potenza ideale", t["potenza_ideale_kw"], "kW")
assert [x["auto_flotta_reale"] for x in r["sedi"]] == [19, 10]   # ripartite sui punti
assert r["esito"] == "sovradimensionato"
assert t["capacita_auto"] >= 50                                  # regge ~2 volte le auto reali
assert t["capacita_per_rotazione"]["1"] < 29                      # senza spostare l'auto non basta
assert t["ac_ideali"] < 19
assert t["potenza_ideale_kw"] < t["potenza_disponibile_kw"] / 4
# Un'auto per punto tutto il giorno: la potenza minima resta quella che fa entrare
# l'energia del giorno nelle 9 ore di sosta (~246 kWh / 9 h a Ovest -> 30 kW), non 5 kW.
r1 = engine.run_verifica_preventivo(req.model_copy(update={"rotazioni": [1], "rotazione_riferimento": 1}))
ovest = r1["sedi"][0]["ideale"]["potenza_kw"]
print("[1 auto per punto] potenza minima Ovest", ovest, "kW")
assert ovest >= 30
print("OK")
