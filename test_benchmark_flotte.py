"""Test di regressione sul benchmark di popolazione (flotte reali, dati pubblici Motus-E).

Il motore non e' tarato su questi dati: il test controlla che, col profilo di riferimento
(una sede Office, 80 km al giorno, 30% wallbox a casa), Y35 resti nella fascia attesa di
auto elettriche per punto. Se una modifica sposta la mediana fuori da 2-4 va spiegata.
Usa le flotte fino a 40 EV per restare veloce (~30 s); la pagina usa i risultati salvati.
"""
import statistics
from josa_core.benchmark import flotte as B

righe = []
cache = {}
for f in B.flotte_confrontabili(ev_max=40):
    n = f["ev_stimati"]
    if n not in cache:
        cache[n] = sum(B.punti_y35(n))
    righe.append((n, cache[n]))

assert len(righe) >= 25, len(righe)
mediana = statistics.median(n / p for n, p in righe)
print(f"{len(righe)} flotte fino a 40 EV: mediana Y35 {mediana:.2f} EV per punto")
assert 2.0 <= mediana <= 4.0, f"mediana EV/punto {mediana:.2f} fuori da 2-4: spiegare la modifica al motore"
# piu' auto non devono mai chiedere meno punti
for (n1, p1), (n2, p2) in zip(sorted(cache.items()), sorted(cache.items())[1:]):
    assert p2 >= p1, f"{n2} EV -> {p2} punti, meno di {n1} EV -> {p1}"

# Risultati salvati coerenti con i dati e col profilo di riferimento
r = B.risultati()
assert r["profilo"]["km_giornalieri"] == B.KM_RIFERIMENTO == 80
assert r["n_flotte_pubbliche"] == len(B.flotte_confrontabili()) and r["n_flotte_pubbliche"] >= 40
assert 2.0 <= r["mediana_ev_per_punto_y35"] <= 4.0
assert all("nome" not in f for f in r["flotte"]), "niente nomi di aziende nei dati del repo"
# Il numero nel badge della home e' quello della pagina
with open("frontend/index.html", encoding="utf-8") as fh:
    home = fh.read()
n_tot = r["casi_completi"] + r["n_flotte_pubbliche"]
assert f'id="homeBenchmarkN">{n_tot}<' in home, f"badge home diverso da {n_tot}"
print("OK benchmark flotte")
