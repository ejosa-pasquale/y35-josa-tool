"""Sustainability Score Y35 (0-100) — metodologia SS-1.0.

Indice interno Y35 dell'impatto ambientale dell'elettrificazione della flotta.
Usa solo grandezze gia' calcolate dal motore (Carbon Accounting, simulazione a
15 minuti, FV in sito, picco di ricarica): non modifica nessun altro risultato.

Cinque pilastri, soglie ASSOLUTE (non relative ad altri clienti), cosi' lo
stesso valore vale lo stesso punteggio in ogni anno e per ogni cliente:

  1. Decarbonizzazione      40  riduzione netta CO2e % (25) + intensita' g/km (15)
  2. Elettrificazione       20  quota km percorsi in elettrico
  3. Energia rinnovabile    15  quota kWh di ricarica da FV in sito o GO
  4. Ricarica in sede       10  energia in sede / (sede + pubblica)
  5. Impatto sulla rete     15  picco di ricarica / potenza di rete disponibile

Un KPI non disponibile (es. potenza di rete assente) non vale zero: il suo peso
viene ridistribuito in proporzione sugli altri e il KPI e' segnalato.
L'affidabilita' del dato (stimato / parzialmente misurato / misurato) e'
riportata accanto al punteggio ma non entra nel calcolo.
"""
from __future__ import annotations

from typing import Optional

VERSIONE = "SS-1.0"
DICITURA = (
    "Indice interno Y35, non è un rating ESG certificato. "
    "I KPI sono utilizzabili come input per la rendicontazione (ESRS E1 / VSME)."
)

# Soglie della metodologia SS-1.0. Cambiarle = nuova versione (e ricalcolo
# dell'anno precedente per mantenere il confronto nel tempo).
INTENSITA_ZERO_G_KM = 200.0     # >= 200 g CO2e/km -> 0 punti
RETE_PIENO_PCT = 50.0           # picco <= 50% della potenza disponibile -> pieno
RETE_ZERO_PCT = 100.0           # picco >= 100% -> 0 punti

CLASSI = [(80.0, "A"), (65.0, "B"), (50.0, "C"), (35.0, "D"), (0.0, "E")]

AFFIDABILITA = {
    "stimato": "Stimato (simulazione ex-ante)",
    "parzialmente_misurato": "Parzialmente misurato",
    "misurato": "Misurato (sessioni OCPP / curva POD)",
}


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def classe(score: float) -> str:
    for soglia, c in CLASSI:
        if score >= soglia:
            return c
    return "E"


def _kpi(cod, pilastro, nome, valore, unita, peso, livello, regola):
    return {
        "codice": cod, "pilastro": pilastro, "nome": nome,
        "valore": None if valore is None else round(float(valore), 1),
        "unita": unita, "peso": peso,
        "livello_pct": None if livello is None else round(livello * 100.0, 1),
        "disponibile": livello is not None,
        "regola": regola,
    }


def compute(
    *,
    riduzione_pct: Optional[float],
    intensita_g_km: Optional[float],
    km_totali: float,
    km_elettrici: float,
    kwh_ricarica_totali: float,
    kwh_rinnovabili: float,
    kwh_sede: float,
    kwh_pubblica: float,
    picco_kw: Optional[float],
    potenza_rete_kw: Optional[float],
    affidabilita: str = "stimato",
    versione_fattori: Optional[str] = None,
) -> dict:
    """Calcola lo score. Tutti gli input sono grandezze del giorno tipo o annue
    (le quote sono rapporti, quindi l'unita' di periodo non conta)."""
    kpis = []

    liv = None if riduzione_pct is None else _clip01(riduzione_pct / 100.0)
    kpis.append(_kpi("riduzione_co2", "Decarbonizzazione", "Riduzione netta CO2e vs flotta termica",
                     riduzione_pct, "%", 25, liv, "lineare 0% → 0 punti, 100% → pieno"))

    liv = None if intensita_g_km is None else _clip01(1.0 - intensita_g_km / INTENSITA_ZERO_G_KM)
    kpis.append(_kpi("intensita_co2", "Decarbonizzazione", "Intensità emissiva della flotta",
                     intensita_g_km, "g CO2e/km", 15, liv,
                     f"lineare 0 g/km → pieno, ≥{INTENSITA_ZERO_G_KM:.0f} g/km → 0 punti"))

    q = km_elettrici / km_totali * 100.0 if km_totali > 0 else None
    kpis.append(_kpi("km_elettrici", "Elettrificazione", "Quota di km percorsi in elettrico",
                     q, "%", 20, None if q is None else _clip01(q / 100.0),
                     "lineare 0% → 0 punti, 100% → pieno (PHEV: solo km entro l'autonomia elettrica)"))

    q = kwh_rinnovabili / kwh_ricarica_totali * 100.0 if kwh_ricarica_totali > 0 else None
    kpis.append(_kpi("energia_rinnovabile", "Energia rinnovabile", "Quota di ricarica da FV in sito o garanzie d'origine",
                     q, "%", 15, None if q is None else _clip01(q / 100.0),
                     "lineare 0% → 0 punti, 100% → pieno"))

    den = kwh_sede + kwh_pubblica
    q = kwh_sede / den * 100.0 if den > 0 else None
    kpis.append(_kpi("ricarica_sede", "Ricarica in sede", "Quota di energia caricata in sede (vs colonnine pubbliche)",
                     q, "%", 10, None if q is None else _clip01(q / 100.0),
                     "lineare 0% → 0 punti, 100% → pieno; la ricarica domestica pianificata non penalizza"))

    if picco_kw is not None and potenza_rete_kw and potenza_rete_kw > 0:
        q = picco_kw / potenza_rete_kw * 100.0
        liv = _clip01((RETE_ZERO_PCT - q) / (RETE_ZERO_PCT - RETE_PIENO_PCT))
    else:
        q, liv = None, None
    kpis.append(_kpi("impatto_rete", "Impatto sulla rete", "Picco di ricarica sulla potenza di rete disponibile",
                     q, "%", 15, liv,
                     f"≤{RETE_PIENO_PCT:.0f}% → pieno, ≥{RETE_ZERO_PCT:.0f}% → 0 punti (DLM, F2/F3 e V2G lo migliorano)"))

    peso_disp = sum(k["peso"] for k in kpis if k["disponibile"])
    fattore = 100.0 / peso_disp if peso_disp > 0 else 0.0
    score = 0.0
    for k in kpis:
        if k["disponibile"]:
            k["punti"] = round(k["livello_pct"] / 100.0 * k["peso"] * fattore, 1)
            k["punti_max"] = round(k["peso"] * fattore, 1)
            score += k["livello_pct"] / 100.0 * k["peso"] * fattore
        else:
            k["punti"], k["punti_max"] = None, 0.0

    pilastri = []
    for nome in ["Decarbonizzazione", "Elettrificazione", "Energia rinnovabile", "Ricarica in sede", "Impatto sulla rete"]:
        ks = [k for k in kpis if k["pilastro"] == nome]
        pilastri.append({
            "nome": nome,
            "peso": sum(k["peso"] for k in ks),
            "punti": round(sum(k["punti"] or 0.0 for k in ks), 1),
            "punti_max": round(sum(k["punti_max"] for k in ks), 1),
        })

    score = round(score, 1) if peso_disp > 0 else None
    mancanti = [k["nome"] for k in kpis if not k["disponibile"]]
    return {
        "score": score,
        "classe": classe(score) if score is not None else None,
        "pilastri": pilastri,
        "kpi": kpis,
        "kpi_non_disponibili": mancanti,
        "peso_ridistribuito": bool(mancanti),
        "affidabilita": affidabilita,
        "affidabilita_label": AFFIDABILITA.get(affidabilita, affidabilita),
        "versione_metodologia": VERSIONE,
        "versione_fattori": versione_fattori,
        "dicitura": DICITURA,
    }
