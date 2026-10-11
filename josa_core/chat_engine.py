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
  3. HANDOFF: quando il profilo è completo, genera un payload per sede
     (stessi parametri del percorso completo calibrato sui casi reali)

Calibrazione sui casi reali (stessi valori di test_casi_reali.py):
  - PHEV e BEV sono due gruppi distinti, ognuno con batteria, consumo e
    caricatore di bordo propri;
  - le wallbox domestiche NON sono colonnine della sede: contano solo come
    readiness domestica (pct_casa);
  - se un dato economico manca si usa l'ipotesi standard Y35 e lo si dichiara
    (lista "ipotesi"), mai un valore nascosto;
  - una conversazione puo' descrivere piu' sedi (lista "sedi").
"""

from __future__ import annotations
from typing import Optional
import json


# Campi obbligatori minimi per lanciare l'analisi (per ogni sede)
REQUIRED_FIELDS = [
    "n_veicoli",        # numero veicoli (oppure n_phev + n_bev)
    "km_giornalieri",   # km medi per veicolo nel giorno lavorativo
    "finestra_ore",     # ore disponibili per la ricarica in sede
    "p_rete_kw",        # potenza disponibile per la ricarica in sede
]

# Campi opzionali ma utili
OPTIONAL_FIELDS = [
    "n_phev", "n_bev",  # mix target ibridi plug-in / elettriche
    "pct_casa",         # % veicoli con wallbox a casa (readiness domestica)
    "budget_eur",       # budget CAPEX infrastruttura (null = nessun vincolo)
    "presenza_pct",     # % veicoli presenti nel giorno di picco
    "prezzo_energia_aziendale_eur_kwh", "prezzo_energia_domestica_eur_kwh",
    "prezzo_energia_pubblica_eur_kwh", "prezzo_carburante_eur_l",
    "consumo_carburante_l100km", "km_anno",
    "adeguamento_rete_eur", "risparmio_manutenzione_eur_anno",
    "modelli",          # canoni NLT per modello (TCO per modello)
    "sedi",             # piu' sedi nella stessa conversazione
    "fv_kw", "n_posti_auto", "tipo_edificio", "marca_modello_ev", "scenario",
]

# Ipotesi standard della metodologia Y35 (report sui casi reali). Si usano solo
# quando l'utente non conosce il dato, e finiscono sempre nella lista "ipotesi".
STANDARD_Y35 = {
    "prezzo_energia_aziendale_eur_kwh": 0.32,
    "prezzo_energia_domestica_eur_kwh": 0.26,
    "prezzo_energia_pubblica_eur_kwh": 0.69,
    "prezzo_carburante_eur_l": 1.85,
    "consumo_carburante_l100km": 6.0,
    "presenza_pct": 90.0,
    "giorni_lavorativi_anno": 220,
    "quota_elettrica_phev_pct": 70.0,
    "adeguamento_rete_eur": 50000.0,          # per sede
    "adeguamento_rete_sede_minore_eur": 20000.0,
    "soglia_sede_minore_veicoli": 25,
    "tolleranza_tra_veicoli_min": 60.0,
    "tasso_sconto_pct": 10.0,
}

# Parametri veicolo calibrati sui casi reali (test_casi_reali.py)
PHEV_STD = {"batteria_utile_kwh": 12.0, "consumo_kwh_km": 0.17, "potenza_ac_kw": 6.6, "consumo_l100km": 6.0}
BEV_STD = {"batteria_kwh": 70.0, "consumo_kwh_km": 0.18, "potenza_ac_kw": 9.0}

# Catalogo standard di sede. Costi = ipotesi standard Y35: punto AC 2.500 EUR,
# punto DC 30-50 kW 18.000 EUR. Le wallbox domestiche non ci sono: non sono colonnine di sede.
CATALOGO_SEDE_STD = [
    # Colonnina trifase 22kW: la potenza INSTALLATA è 22kW ma la carica
    # effettiva dipende dall'OBC del veicolo (tipicamente 6.6-11kW).
    # Il motore DLM usa sempre la potenza OBC reale, non i 22kW nominali.
    {"nome": "Colonnina AC trifase", "potenza_kw": 22.0,  "costo_acquisto_eur": 1500.0,  "costo_installazione_eur": 1000.0,  "costo_manutenzione_eur_anno": 80.0},
    {"nome": "DC 30kW",              "potenza_kw": 30.0,  "costo_acquisto_eur": 12000.0, "costo_installazione_eur": 6000.0,  "costo_manutenzione_eur_anno": 400.0},
    {"nome": "DC 50kW",              "potenza_kw": 50.0,  "costo_acquisto_eur": 12000.0, "costo_installazione_eur": 6000.0,  "costo_manutenzione_eur_anno": 550.0},
    {"nome": "DC 100kW",             "potenza_kw": 100.0, "costo_acquisto_eur": 18000.0, "costo_installazione_eur": 12000.0, "costo_manutenzione_eur_anno": 900.0},
    {"nome": "DC 150kW",             "potenza_kw": 150.0, "costo_acquisto_eur": 30000.0, "costo_installazione_eur": 15000.0, "costo_manutenzione_eur_anno": 1400.0},
]

# Budget "nessun vincolo": l'ottimizzatore cerca comunque il CAPEX minimo
BUDGET_SENZA_VINCOLO_EUR = 10_000_000.0

SYSTEM_PROMPT = """Sei l'assistente AI di Y35, azienda specializzata in infrastrutture di ricarica per flotte aziendali.
Il tuo compito è raccogliere le informazioni necessarie per progettare l'infrastruttura di ricarica
E per proporre le migliori alternative elettriche ai veicoli attuali, guidando l'utente in modo naturale.

REGOLE:
1. Rispondi SEMPRE in italiano, tono professionale ma colloquiale.
2. Fai UNA SOLA domanda per volta — mai due domande nello stesso messaggio.
3. Quando l'utente fornisce informazioni, riconosci brevemente e poi fai la prossima domanda.
4. Non elencare mai i campi tecnici all'utente — parla in linguaggio naturale.
5. NON chiedere mai l'email o dati di contatto — non serve.
6. Se l'utente incolla un documento o tanti dati insieme, estrai TUTTO quello che c'è e chiedi solo ciò che manca.
7. Quando imposti pronto_per_analisi=true, il TUO ULTIMO MESSAGGIO deve terminare SEMPRE con questa frase esatta:
   "✅ Ho tutto quello che mi serve. Premi il pulsante verde **Avvia analisi infrastruttura** qui sotto per vedere il progetto completo."
8. Alla fine di ogni risposta, includi SEMPRE un blocco JSON nascosto con lo stato attuale.
   Usa null per i dati che non conosci: NON inventare valori.

<data>
{
  "n_veicoli": null,
  "n_phev": null,
  "n_bev": null,
  "km_giornalieri": null,
  "km_anno": null,
  "profilo": null,
  "finestra_inizio": null,
  "finestra_fine": null,
  "finestra_ore": null,
  "pct_casa": null,
  "presenza_pct": null,
  "p_rete_kw": null,
  "budget_eur": null,
  "budget_chiesto": false,
  "prezzo_energia_aziendale_eur_kwh": null,
  "prezzo_energia_domestica_eur_kwh": null,
  "prezzo_energia_pubblica_eur_kwh": null,
  "prezzo_carburante_eur_l": null,
  "consumo_carburante_l100km": null,
  "prezzi_chiesti": false,
  "adeguamento_rete_eur": null,
  "risparmio_manutenzione_eur_anno": null,
  "modelli": [],
  "sedi": [],
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

MIX DEI VEICOLI (fondamentale per il dimensionamento):
- Chiedi SEMPRE quanti veicoli saranno ibridi plug-in (PHEV) e quanti elettrici puri (BEV) nella flotta target.
  Esempio: "41 PHEV e 3 BEV" → n_phev=41, n_bev=3, n_veicoli=44.
- Un PHEV ha una batteria piccola (circa 12 kWh utili) e carica in AC lentamente: NON trattarlo come un'elettrica.
- Se l'utente non sa il mix, chiedi se pensa più a ibridi plug-in o a elettriche; se proprio non sa, lascia n_phev e n_bev a null.
- "profilo" descrive l'USO dei veicoli, non l'alimentazione: "Office" (dipendenti, arrivo mattino/uscita sera),
  "Last-mile" (consegne), "Pool" (auto condivise), "Furgoni" (operativi pesanti). Le auto aziendali dei dipendenti sono "Office"
  anche se sono ibride.

PIÙ SEDI:
- Se la flotta è distribuita su più sedi, compila "sedi" con un oggetto per sede:
  {"nome": "...", "n_phev": .., "n_bev": .., "km_giornalieri": .., "pct_casa": .., "p_rete_kw": ..,
   "finestra_inizio": "HH:MM", "finestra_fine": "HH:MM", "adeguamento_rete_eur": null, "budget_eur": null}
- I dati comuni (prezzi, carburante, presenza, modelli) restano nei campi generali.
- Per ogni sede servono veicoli, km giornalieri, finestra e potenza disponibile.
- Con una sola sede lascia "sedi" vuoto e usa i campi generali.

RICARICA A CASA (readiness domestica):
Chiedi sempre quanti dipendenti possono ricaricare a casa con una wallbox. Aggiorna "pct_casa" con la percentuale (0-100).
Le wallbox domestiche NON sono colonnine della sede e non entrano nel CAPEX dell'infrastruttura aziendale:
riducono solo l'energia da erogare in sede.

POTENZA DISPONIBILE: chiedi sempre i kW disponibili per la ricarica in sede (p_rete_kw). Se l'utente non lo sa,
spiega che serve per dimensionare e chiedi una stima (es. potenza del contatore meno i consumi dell'edificio).

PREZZI (una sola domanda, dopo i dati operativi): chiedi se conosce il prezzo dell'energia in azienda, a casa,
alla colonnina pubblica e il prezzo del carburante. Imposta prezzi_chiesti=true dopo averlo chiesto.
Se non li conosce lascia null: verranno usate le ipotesi standard Y35, dichiarate nei risultati.
Se l'utente cita la spesa di manutenzione evitata o il costo di adeguamento della rete, salvali.

TCO PER MODELLO (facoltativo): se l'utente ha i canoni di noleggio per modello, salvali in "modelli" come
[{"nome": "...", "canone_ice_mese_eur": .., "canone_phev_mese_eur": .., "canone_bev_mese_eur": ..}]. Non insistere se non li ha.

HARDWARE DISPONIBILE e come spiegarlo all'utente:
- "Colonnina AC trifase": si installa in azienda su rete trifase. La potenza nominale è 22kW ma
  la carica EFFETTIVA dipende dall'OBC (On-Board Charger) del veicolo: tipicamente 6.6kW per un PHEV, 7.4-11kW per una BEV.
  Non dire mai "carica a 22kW" — è fuorviante. Di' "colonnina trifase" o "punto di ricarica AC".
- "DC 30/50/100/150kW": fast charge, utile per le BEV e per molti km. I PHEV di solito non accettano la DC.
NON usare mai "AC 22kW" — usa "colonnina trifase" o "punto di ricarica AC".

CATEGORIE VEICOLO: "berlina", "SUV", "SUV compatto", "station wagon", "monovolume", "furgone", "furgone grande", "pickup", "citycar".
INFERENZA CATEGORIA: "Fiat Tipo" → berlina; "BMW X5" → SUV; "Volkswagen Transporter" → furgone; "Ford Transit" → furgone grande.
ALIMENTAZIONE ATTUALE: se l'utente dice "diesel", "benzina", "ibrido", "GPL" estrarlo.

IMPORTANTE SUI BUDGET — ci sono due budget distinti:
1. "budget_eur" = budget COLONNINE (CAPEX infrastruttura di ricarica in sede). Questo è il nostro core business.
2. "budget_ev_per_veicolo" = budget per ACQUISTARE i veicoli elettrici. Usato solo nel modulo Vehicle Matching.
Chiedi sempre se c'è un budget per le colonnine e imposta budget_chiesto=true. Se non c'è un budget,
lascia budget_eur=null: il motore cercherà la configurazione con il CAPEX minimo. Non usare mai un budget di default.
Se l'utente menziona un budget non chiaro, chiedi a cosa si riferisce.

Imposta pronto_per_analisi=true quando, per ogni sede, hai: veicoli (meglio con il mix PHEV/BEV), km_giornalieri,
finestra di ricarica, p_rete_kw, e hai già chiesto budget e prezzi (budget_chiesto=true, prezzi_chiesti=true)."""


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
    """Rimuove il blocco <data> e qualsiasi JSON residuo dalla risposta visibile."""
    import re
    # Rimuovi blocco <data>...</data>
    start = text.find("<data>")
    end = text.find("</data>")
    if start >= 0 and end >= 0:
        text = (text[:start] + text[end + 7:]).strip()
    # Rimuovi blocchi JSON con chiavi riconoscibili
    text = re.sub(r'[\n]?\s*\{[^{}]*"pronto_per_analisi"[^{}]*\}', '', text, flags=re.DOTALL)
    text = re.sub(r'[\n]?\s*\{[^{}]*"n_veicoli"[^{}]*\}', '', text, flags=re.DOTALL)
    # Rimuovi backtick
    text = re.sub(r'```[\s\S]*?```', '', text)
    return text.strip()



# ---------------------------------------------------------------------------
# Traduzione profilo conversazionale → input del motore
# ---------------------------------------------------------------------------

def _num(x) -> Optional[float]:
    """Numero o None (accetta anche stringhe tipo "100", "0,25")."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(str(x).replace(",", ".").strip())
    except Exception:
        return None


def _parse_time(s, default_h, default_m=0):
    from datetime import time as dt_time
    try:
        parts = str(s or "").split(":")
        return dt_time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except Exception:
        return dt_time(default_h, default_m)


def _finestra(src: dict, base: dict):
    """Finestra di ricarica: inizio/fine espliciti, oppure inizio + finestra_ore."""
    from datetime import time as dt_time
    ini_raw = src.get("finestra_inizio") or base.get("finestra_inizio")
    fine_raw = src.get("finestra_fine") or base.get("finestra_fine")
    ore = _num(src.get("finestra_ore")) or _num(base.get("finestra_ore"))
    ini = _parse_time(ini_raw, 9)
    if fine_raw:
        fine = _parse_time(fine_raw, 18)
    elif ore:
        h = min(23.75, ini.hour + ini.minute / 60 + ore)
        fine = dt_time(int(h), int(round((h % 1) * 60)) % 60)
    else:
        fine = dt_time(18, 0)
    return ini, fine


def _profilo_motore(profile_raw) -> str:
    p = str(profile_raw or "Office").lower()
    profilo_map = {
        "last-mile": "Last-mile", "consegne": "Last-mile", "corriere": "Last-mile",
        "pool": "Pool Car", "condivise": "Pool Car",
        "furgoni": "Furgoni operativi", "operativi": "Furgoni operativi",
    }
    # Office/dipendenti/ufficio e anche "ibrido"/"phev" (alimentazione, non uso) → Office
    return next((v for k, v in profilo_map.items() if k in p), "Office")


def _consumo_bev_da_categoria(categoria: str) -> tuple[float, float]:
    """Consumo e batteria BEV per categoria; senza categoria i valori calibrati."""
    cat = (categoria or "").lower()
    consumo = {"citycar": 0.14, "berlina": 0.16, "compatta": 0.16, "suv compatto": 0.18, "suv grande": 0.23,
               "suv": 0.21, "station wagon": 0.18, "monovolume": 0.20, "furgone grande": 0.28,
               "furgone medio": 0.28, "furgone": 0.22, "pickup": 0.30}
    batteria = {"citycar": 40, "berlina": 50, "compatta": 50, "suv compatto": 60, "suv grande": 80, "suv": 70,
                "furgone grande": 75, "furgone medio": 75, "furgone": 50}
    c = next((v for k, v in consumo.items() if k in cat), BEV_STD["consumo_kwh_km"])
    b = next((v for k, v in batteria.items() if k in cat), BEV_STD["batteria_kwh"])
    return c, float(b)


def _mix(src: dict, base: dict) -> tuple[int, int, bool]:
    """(n_phev, n_bev, mix_dichiarato). Senza mix: alimentazione PHEV → tutti PHEV, altrimenti tutti BEV."""
    n_phev, n_bev = _num(src.get("n_phev")), _num(src.get("n_bev"))
    n_tot = _num(src.get("n_veicoli"))
    if n_phev is not None or n_bev is not None:
        n_phev, n_bev = int(n_phev or 0), int(n_bev or 0)
        if n_tot and n_phev + n_bev == 0:
            n_bev = int(n_tot)
        return n_phev, n_bev, True
    n = int(n_tot or 0)
    alim = str(src.get("alimentazione_attuale") or base.get("alimentazione_attuale") or "").lower()
    target = str(src.get("scenario") or base.get("scenario") or "").lower()
    if "phev" in alim or "plug" in alim or "phev" in target or "plug" in target:
        return n, 0, False
    return 0, n, False


def sedi_dal_profilo(profile: dict) -> list[dict]:
    """Lista delle sedi: dalla lista "sedi" se presente, altrimenti una sede dai campi generali."""
    sedi = [s for s in (profile.get("sedi") or []) if isinstance(s, dict)]
    if not sedi:
        return [dict(profile, nome=profile.get("nome_sede") or "Sede")]
    out = []
    for i, s in enumerate(sedi):
        merged = {k: v for k, v in profile.items() if k != "sedi"}
        merged.update({k: v for k, v in s.items() if v is not None})
        merged["nome"] = s.get("nome") or f"Sede {i + 1}"
        # n_veicoli/mix della sede non vanno ereditati dal totale di flotta
        for k in ("n_veicoli", "n_phev", "n_bev"):
            merged[k] = s.get(k)
        out.append(merged)
    return out


def parametri_economici(profile: dict) -> tuple[dict, list[str]]:
    """Prezzi e ipotesi economiche: valore dell'utente o standard Y35, con la lista
    delle ipotesi usate (mai valori nascosti)."""
    ipotesi = []
    etichette = {
        "prezzo_energia_aziendale_eur_kwh": ("energia in azienda", "€/kWh"),
        "prezzo_energia_domestica_eur_kwh": ("energia a casa", "€/kWh"),
        "prezzo_energia_pubblica_eur_kwh": ("ricarica pubblica", "€/kWh"),
        "prezzo_carburante_eur_l": ("carburante", "€/l"),
        "consumo_carburante_l100km": ("consumo carburante attuale", "l/100 km"),
        "presenza_pct": ("veicoli presenti nel giorno di picco", "%"),
    }
    out = {}
    for k, (nome, um) in etichette.items():
        v = _num(profile.get(k))
        if v is None:
            v = STANDARD_Y35[k]
            ipotesi.append(f"{nome}: {v:g} {um} (ipotesi standard Y35)")
        out[k] = v
    out["giorni_lavorativi_anno"] = STANDARD_Y35["giorni_lavorativi_anno"]
    out["quota_elettrica_phev_pct"] = STANDARD_Y35["quota_elettrica_phev_pct"]
    out["tasso_sconto_pct"] = STANDARD_Y35["tasso_sconto_pct"]
    out["tolleranza_tra_veicoli_min"] = STANDARD_Y35["tolleranza_tra_veicoli_min"]
    out["risparmio_manutenzione_eur_anno"] = _num(profile.get("risparmio_manutenzione_eur_anno")) or 0.0
    ipotesi.append(f"PHEV: {out['quota_elettrica_phev_pct']:g}% dei km in elettrico con ricarica regolare (ipotesi standard Y35)")
    ipotesi.append(f"tolleranza tra un veicolo e il successivo sulla stessa colonnina: {out['tolleranza_tra_veicoli_min']:g} min (prudente)")
    ipotesi.append(f"tasso di sconto VAN: {out['tasso_sconto_pct']:g}%, rampa di adozione 30/90/100%")
    if not out["risparmio_manutenzione_eur_anno"]:
        ipotesi.append("risparmio di manutenzione non indicato: escluso dal business case")
    return out, ipotesi


def _catalogo_sede(catalogo: Optional[list]) -> list:
    """Catalogo della sede senza wallbox domestiche (non sono colonnine di sede)."""
    cat = [h for h in (catalogo or CATALOGO_SEDE_STD)
           if "casa" not in str(h.get("nome", "")).lower() and "domestic" not in str(h.get("nome", "")).lower()]
    return cat or list(CATALOGO_SEDE_STD)


def _tipi_da_esplorare(catalogo: list) -> list:
    """Come il percorso completo: il punto AC e la DC piu' piccola. Con piu' taglie DC
    l'ottimizzatore aggiunge DC anche dove bastano le AC (es. 9 AC + 2 DC invece di 10 AC)."""
    ac = [c["nome"] for c in catalogo if "DC" not in str(c["nome"]).upper()]
    dc = sorted((c for c in catalogo if "DC" in str(c["nome"]).upper()), key=lambda c: c["potenza_kw"])
    return ac + [dc[0]["nome"]] if dc else ac


def payload_sede(sede: dict, economia: dict, catalogo: Optional[list] = None) -> dict:
    """Payload del motore per una sede, con gli stessi parametri del percorso completo."""
    n_phev, n_bev, mix_dichiarato = _mix(sede, sede)
    if n_phev + n_bev <= 0:
        raise ValueError(f"{sede.get('nome', 'Sede')}: numero di veicoli mancante")
    km_raw = _num(sede.get("km_giornalieri"))
    if not km_raw or km_raw <= 0:
        km_anno = _num(sede.get("km_anno"))
        if not km_anno:
            raise ValueError(f"{sede.get('nome', 'Sede')}: km giornalieri mancanti")
        km_raw = km_anno / economia["giorni_lavorativi_anno"]
    p_rete = _num(sede.get("p_rete_kw"))
    if not p_rete or p_rete <= 0:
        raise ValueError(f"{sede.get('nome', 'Sede')}: potenza disponibile (kW) mancante")
    ini, fine = _finestra(sede, sede)
    durata_min = max(15, (fine.hour * 60 + fine.minute) - (ini.hour * 60 + ini.minute))
    pct_casa = max(0.0, min(100.0, _num(sede.get("pct_casa")) or 0.0))
    casa = pct_casa > 0
    profilo = _profilo_motore(sede.get("profilo"))
    presenza = economia["presenza_pct"]

    comuni = dict(
        profilo=profilo, km_per_giro=km_raw, giri_per_veicolo_giorno=1,
        tempo_disponibile_min=durata_min, finestra_inizio=ini, finestra_fine=fine,
        quota_ricarica_deposito=1.0, k_factor=1.1, probabilita_utilizzo_pct=presenza,
        ricarica_domestica=casa, pct_veicoli_con_casa=pct_casa if casa else None,
    )
    gruppi = []
    if n_phev:
        gruppi.append(dict(
            gruppo="PHEV", n_veicoli=n_phev, consumo_kwh_km=PHEV_STD["consumo_kwh_km"],
            batteria_kwh=round(PHEV_STD["batteria_utile_kwh"] / 0.7, 3), contemporanei_max=n_phev,
            potenza_max_ricarica_ac_kw=PHEV_STD["potenza_ac_kw"],
            autonomia_elettrica_km=round(PHEV_STD["batteria_utile_kwh"] / PHEV_STD["consumo_kwh_km"], 1),
            consumo_benzina_l100km=PHEV_STD["consumo_l100km"], accetta_ricarica_dc=False, **comuni))
    if n_bev:
        consumo, batteria = _consumo_bev_da_categoria(sede.get("categoria_veicolo") if not n_phev else "")
        gruppi.append(dict(
            gruppo="BEV", n_veicoli=n_bev, consumo_kwh_km=consumo, batteria_kwh=batteria,
            contemporanei_max=n_bev, potenza_max_ricarica_ac_kw=BEV_STD["potenza_ac_kw"],
            accetta_ricarica_dc=True, **comuni))

    fine_h = fine.hour + fine.minute / 60.0
    policy = {
        "p_rete_kw": p_rete, "p_shaving_kw": p_rete,
        "max_ac_veicoli_per_punto": 4, "max_dc_veicoli_per_punto": 6,
        "limite_ora_turno": fine_h if fine_h > 0 else 19.0,
        "hybrid_private_home_charging": casa,
        "company_buffer_pct": 30 if casa else 100,
        "sim_days": 1, "allow_oversizing": False,
    }
    budget = _num(sede.get("budget_eur"))
    catalogo_sede = _catalogo_sede(catalogo)
    n_tot = n_phev + n_bev
    adeg = _num(sede.get("adeguamento_rete_eur"))
    adeg_ipotesi = adeg is None
    if adeg is None:
        adeg = (STANDARD_Y35["adeguamento_rete_sede_minore_eur"] if n_tot < STANDARD_Y35["soglia_sede_minore_veicoli"]
                else STANDARD_Y35["adeguamento_rete_eur"])
    km_anno_veicolo = _num(sede.get("km_anno")) or km_raw * economia["giorni_lavorativi_anno"]
    return {
        "nome": sede.get("nome") or "Sede",
        "gruppi": gruppi,
        "catalogo_hardware": catalogo_sede,
        "policy": policy,
        "budget_max_eur": budget if budget and budget > 0 else BUDGET_SENZA_VINCOLO_EUR,
        "budget_indicato": bool(budget and budget > 0),
        "tipi_hardware_da_esplorare": _tipi_da_esplorare(catalogo_sede),
        "beam_size": 5, "patience": 12, "max_steps": 120,
        "n_phev": n_phev, "n_bev": n_bev, "mix_dichiarato": mix_dichiarato,
        "pct_casa": pct_casa, "km_giornalieri": km_raw, "km_anno_veicolo": km_anno_veicolo,
        "adeguamento_rete_eur": adeg, "adeguamento_rete_ipotesi": adeg_ipotesi,
    }


def profile_to_analysis_payload(profile: dict, catalogo_default: Optional[list] = None) -> dict:
    """
    Converte il profilo conversazionale negli input strutturati del motore.
    Non modifica nessuna logica del motore — solo traduzione dei campi.
    Ritorna il payload della prima sede piu' la lista completa in "sedi".
    """
    economia, ipotesi = parametri_economici(profile)
    sedi = [payload_sede(s, economia, catalogo_default) for s in sedi_dal_profilo(profile)]
    primo = dict(sedi[0])
    primo.update({"sedi": sedi, "economia": economia, "ipotesi": ipotesi, "profilo_conversazionale": profile})
    return primo
