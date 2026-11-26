"""Benchmark di popolazione: flotte aziendali reali (dati pubblici Motus-E) contro il dimensionamento Y35.

Non tara il motore: confronta quante auto elettriche per punto di ricarica raccomanda Y35 con
quante ne hanno davvero le aziende. Profilo di riferimento fisso, uguale per tutte:
una sede Office 08:30-17:30, tutte BEV, KM_RIFERIMENTO km al giorno, PCT_CASA_RIFERIMENTO% con
wallbox a casa, rete senza limite. Usato dal test di regressione e dalla pagina /benchmark.
"""
from __future__ import annotations
import json, os, statistics

DIR = os.path.dirname(os.path.abspath(__file__))
FILE_FLOTTE = os.path.join(DIR, "flotte_motus_e.json")
FILE_RISULTATI = os.path.join(DIR, "risultati_y35.json")
KM_RIFERIMENTO = 80
PCT_CASA_RIFERIMENTO = 30
EV_MIN, EV_MAX = 3, 150
CASI_COMPLETI = 4  # report di clienti anonimi (casi A, B, C, D): restano fuori dal repo


def flotte_confrontabili(ev_max: int = EV_MAX) -> list:
    """Flotte con punti dichiarati e tra EV_MIN e ev_max auto elettriche."""
    with open(FILE_FLOTTE, encoding="utf-8") as f:
        flotte = json.load(f)["flotte"]
    return [f for f in flotte if f["punti_dichiarati"] and EV_MIN <= f["ev_stimati"] <= ev_max]


def profilo_riferimento(n_ev: int, km: int = KM_RIFERIMENTO, pct_casa: int = PCT_CASA_RIFERIMENTO) -> dict:
    sede = dict(nome="Sede", n_bev=n_ev, n_phev=0, km_giornalieri_bev=km, km_giornalieri_phev=km,
                pct_casa=pct_casa, p_rete_kw=10000)
    return dict(profilo="Office", finestra_inizio="08:30", finestra_fine="17:30", notte_in_sede=False,
                presenza_pct=90, budget_chiesto=True, prezzi_chiesti=True, sedi=[sede])


def punti_y35(n_ev: int, km: int = KM_RIFERIMENTO, pct_casa: int = PCT_CASA_RIFERIMENTO) -> tuple:
    """(punti AC, punti DC) raccomandati da Y35 per n_ev auto col profilo di riferimento."""
    from app.chat_analysis import run_chat_analysis
    g = run_chat_analysis(profilo_riferimento(n_ev, km, pct_casa))["gruppo"]
    return int(g["punti_ac"]), int(g["punti_dc"])


def mediana_ev_per_punto(righe: list, chiave_punti: str) -> float:
    return statistics.median(r["ev_stimati"] / max(1, r[chiave_punti]) for r in righe)


def calcola(ev_max: int = EV_MAX) -> dict:
    cache, righe = {}, []
    for f in flotte_confrontabili(ev_max):
        n = f["ev_stimati"]
        if n not in cache:
            cache[n] = punti_y35(n)
        ac, dc = cache[n]
        righe.append(dict(f, y35_ac=ac, y35_dc=dc, y35_punti=ac + dc))
    return {
        "profilo": {"km_giornalieri": KM_RIFERIMENTO, "pct_casa": PCT_CASA_RIFERIMENTO, "sede": "Office 08:30-17:30, tutte BEV"},
        "casi_completi": CASI_COMPLETI,
        "n_flotte_pubbliche": len(righe),
        "mediana_ev_per_punto_dichiarata": round(mediana_ev_per_punto(righe, "punti_dichiarati"), 2),
        "mediana_ev_per_punto_y35": round(mediana_ev_per_punto(righe, "y35_punti"), 2),
        "flotte": righe,
    }


def risultati() -> dict:
    """Risultati salvati (rigenerare con: python3 -m josa_core.benchmark.flotte)."""
    with open(FILE_RISULTATI, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    import datetime, subprocess
    out = calcola()
    try:
        out["commit"] = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    except Exception:
        pass
    out["data"] = datetime.date.today().isoformat()
    with open(FILE_RISULTATI, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(out["n_flotte_pubbliche"], out["mediana_ev_per_punto_dichiarata"], out["mediana_ev_per_punto_y35"])
