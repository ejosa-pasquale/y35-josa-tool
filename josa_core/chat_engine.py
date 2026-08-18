"""
Chat Engine — Conversational Layer sopra il motore di simulazione y35/JoSa.

Gestisce il dialogo multi-turno con l'utente, estrae i dati necessari dal
linguaggio naturale e li trasforma negli input strutturati del motore esistente.

Il motore non viene mai modificato — questo modulo è puramente un traduttore
tra linguaggio naturale e schemi Pydantic esistenti.

Architettura:
  1. INTAKE: Claude legge il testo libero e popola un FleetProfile parziale
  2. COMPLETAMENTO: per ogni turno, Claude aggiorna il profilo e decide
     se fare un'altra domanda o se i dati sono sufficienti
  3. HANDOFF: quando il profilo è completo, genera il payload per il motore
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import json


# Campi obbligatori minimi per lanciare l'analisi
REQUIRED_FIELDS = [
    "n_veicoli",        # numero veicoli
    "km_giornalieri",   # km medi per veicolo/giorno
    "profilo",          # tipo di utilizzo
    "finestra_ore",     # ore disponibili per la ricarica
]

# Campi opzionali ma utili
OPTIONAL_FIELDS = [
    "pct_casa",         # % veicoli con wallbox a casa
    "budget_eur",       # budget CAPEX infrastruttura
    "p_rete_kw",        # potenza allaccio disponibile
    "fv_kw",            # fotovoltaico installato
    "n_posti_auto",     # posti auto nel parcheggio
    "tipo_edificio",    # sede, magazzino, condominio...
    "marca_modello_ev", # preferenze veicolo EV
    "scenario",         # 100% EV, 50%, progressivo
]

SYSTEM_PROMPT = """Sei l'assistente AI di Y35, azienda specializzata in infrastrutture di ricarica per flotte aziendali.
Il tuo compito è raccogliere le informazioni necessarie per progettare l'infrastruttura di ricarica
E per proporre le migliori alternative elettriche ai veicoli attuali, guidando l'utente in modo naturale.

REGOLE:
1. Rispondi SEMPRE in italiano, tono professionale ma colloquiale.
2. Fai UNA SOLA domanda per volta — mai due domande nello stesso messaggio.
3. Quando l'utente fornisce informazioni, riconosci brevemente e poi fai la prossima domanda.
4. Non elencare mai i campi tecnici all'utente — parla in linguaggio naturale.
5. Quando hai abbastanza dati per l'analisi, non fare altre domande.
6. Cerca SEMPRE di capire quale tipo di veicolo usa attualmente l'utente (anche solo la categoria).
7. Alla fine di ogni risposta, includi SEMPRE un blocco JSON nascosto con lo stato attuale:

<data>
{
  "n_veicoli": null,
  "km_giornalieri": null,
  "profilo": null,
  "finestra_inizio": "09:00",
  "finestra_fine": "18:00",
  "finestra_ore": null,
  "pct_casa": 0,
  "budget_eur": null,
  "p_rete_kw": null,
  "fv_kw": null,
  "n_posti_auto": null,
  "tipo_edificio": null,
  "marca_modello_attuale": null,
  "categoria_veicolo": null,
  "alimentazione_attuale": null,
  "potenza_cv_attuale": null,
  "budget_ev_per_veicolo": null,
  "scenario": null,
  "pronto_per_analisi": false,
  "prossima_domanda": "qual_e_la_prossima_info_mancante"
}
</data>

PROFILI disponibili: "Office" (dipendenti, arrivo mattino/uscita sera), "Last-mile" (consegne, molti giri brevi),
"Pool" (auto aziendali condivise), "Furgoni" (operativi pesanti), "Ibrido" (PHEV).

CATEGORIE VEICOLO: "berlina", "SUV", "SUV compatto", "station wagon", "monovolume", 
"furgone", "furgone grande", "pickup", "citycar", "sportiva".

INFERENZA CATEGORIA: se l'utente dice "Fiat Tipo" → berlina; "BMW X5" → SUV; 
"Volkswagen Transporter" → furgone; "Ford Transit" → furgone grande.
Se non conosce il modello esatto, chiedi "che tipo di veicolo è? berlina, SUV, furgone...".

ALIMENTAZIONE: se l'utente dice "diesel", "benzina", "ibrido", "GPL" estrarlo.
Se non specificato e il veicolo è vecchio (>3 anni), assume Diesel.

Imposta pronto_per_analisi=true quando hai: n_veicoli, km_giornalieri, profilo, finestra_ore.
Non è necessario avere i dati veicolo per avviare l'analisi infrastruttura."""


def build_messages(history: list[dict], new_message: str) -> list[dict]:
    """Costruisce la lista messaggi per la chiamata Claude."""
    messages = []
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": new_message})
    return messages


def extract_data_block(text: str) -> dict:
    """Estrae il blocco <data>...</data> dalla risposta di Claude."""
    start = text.find("<data>")
    end = text.find("</data>")
    if start == -1 or end == -1:
        return {}
    try:
        json_str = text[start + 6:end].strip()
        return json.loads(json_str)
    except Exception:
        return {}


def clean_response(text: str) -> str:
    """Rimuove il blocco data dalla risposta visibile all'utente."""
    start = text.find("<data>")
    end = text.find("</data>")
    if start == -1 or end == -1:
        return text.strip()
    return (text[:start] + text[end + 7:]).strip()


def profile_to_analysis_payload(profile: dict, catalogo_default: list) -> dict:
    """
    Converte il profilo conversazionale negli input strutturati del motore.
    Non modifica nessuna logica del motore — solo traduzione dei campi.
    """
    n_veicoli = int(profile.get("n_veicoli") or 5)
    km_gg = float(profile.get("km_giornalieri") or 40)
    profilo_raw = (profile.get("profilo") or "Office").lower()

    # Mappa profilo naturale → profilo motore
    profilo_map = {
        "office": "Office", "dipendenti": "Office", "ufficio": "Office",
        "last-mile": "Last-mile", "consegne": "Last-mile", "corriere": "Last-mile",
        "pool": "Pool Car", "pool car": "Pool Car", "condivise": "Pool Car",
        "furgoni": "Furgoni operativi", "operativi": "Furgoni operativi",
        "ibrido": "Ibrido plug-in", "phev": "Ibrido plug-in",
    }
    profilo = next((v for k, v in profilo_map.items() if k in profilo_raw), "Office")

    # Finestra di ricarica
    finestra_inizio = profile.get("finestra_inizio") or "09:00"
    finestra_fine = profile.get("finestra_fine") or "18:00"

    # Parametri hardware di default
    p_rete = float(profile.get("p_rete_kw") or 50)
    budget = float(profile.get("budget_eur") or 30000)
    pct_casa = float(profile.get("pct_casa") or 0)

    gruppi = [{
        "gruppo": profilo,
        "profilo": profilo,
        "n_veicoli": n_veicoli,
        "km_per_giro": km_gg,
        "giri_per_veicolo_giorno": 1,
        "giri_per_autonomia": 2,
        "consumo_kwh_km": 0.20,
        "batteria_kwh": 60,
        "tempo_disponibile_min": 20,
        "finestra_inizio": finestra_inizio,
        "finestra_fine": finestra_fine,
        "contemporanei_max": min(n_veicoli, 9),
        "quota_ricarica_deposito": 1.0,
        "k_factor": 1.2,
        "ricarica_domestica": pct_casa > 0,
        "pct_veicoli_con_casa": pct_casa,
    }]

    catalogo = catalogo_default or [{
        "nome": "AC 22kW",
        "potenza_kw": 22.0,
        "costo_acquisto_eur": 1000.0,
        "costo_installazione_eur": 1600.0,
        "costo_manutenzione_eur_anno": 60.0,
    }]

    policy = {
        "p_rete_kw": p_rete,
        "p_shaving_kw": p_rete,
        "max_ac_veicoli_per_punto": 4,
        "max_dc_veicoli_per_punto": 6,
        "limite_ora_turno": 19.0,
        "soc_start_pct": 90,
        "soc_min_pct": 20,
        "soc_max_pct": 90,
        "soc_buffer_pct": 5,
        "hybrid_private_home_charging": pct_casa > 0,
        "company_buffer_pct": 100 if pct_casa == 0 else 30,
        "sim_days": 1,
        "allow_oversizing": False,
    }

    return {
        "gruppi": gruppi,
        "catalogo_hardware": catalogo,
        "policy": policy,
        "budget_max_eur": budget,
        "tipi_hardware_da_esplorare": [c["nome"] for c in catalogo],
        "beam_size": 3,
        "patience": 4,
        "max_steps": 20,
        "profilo_conversazionale": profile,
    }
