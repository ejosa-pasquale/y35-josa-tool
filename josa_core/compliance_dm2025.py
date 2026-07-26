"""
josa_core.compliance_dm2025 — Verifica obblighi minimi di infrastrutturazione EV
secondo il Decreto Ministeriale 28 ottobre 2025 (pubblicato in Gazzetta Ufficiale
n. 283 del 5 dicembre 2025, efficace dal 3 giugno 2026), che aggiorna il DM 26
giugno 2015 sui Requisiti Minimi degli edifici — Allegato 1, Capitolo 6
("Requisiti e prescrizioni per l'integrazione delle tecnologie per la ricarica
dei veicoli elettrici").

============================== AVVISO IMPORTANTE ==============================
Questo modulo NON è un parere legale e non sostituisce la verifica di un
professionista abilitato (energy manager, progettista, consulente normativo).
È stato costruito sulla base di fonti giornalistiche/tecniche secondarie che
riassumono il decreto, non sul testo ufficiale con le tabelle numeriche complete
(Tabelle 4/5/D dell'Allegato 1), che non è stato possibile consultare articolo
per articolo. In particolare:
  - le soglie "punti fast (Tipologia B) proporzionali alla capienza" per
    parcheggi privati di nuova costruzione sono approssimate, non esatte;
  - le esclusioni (PMI, permesso ante 2021, soglia 7% costo, microsistemi
    isolati) richiedono verifica caso per caso con dati che questo modulo non
    può derivare automaticamente da un dataset di flotta;
  - la scadenza del 1/1/2025 per gli edifici esistenti potrebbe essere
    prorogata dal MASE (Ministero dell'Ambiente e della Sicurezza Energetica).
Ogni risultato di questo modulo va confermato prima di essere usato come base
per una decisione di investimento o per una pratica edilizia.
================================================================================
"""

from dataclasses import dataclass, field
from datetime import date
from math import ceil, floor
from typing import Optional

DISCLAIMER = (
    "Verifica best-effort basata su fonti secondarie del DM 28/10/2025, non sul testo "
    "ufficiale integrale. Non costituisce parere legale. Validare con un professionista "
    "abilitato prima di prendere decisioni di investimento o pratiche edilizie."
)

# Tabella D (equivalenza): 1 punto Tipologia B (DC >= 50 kW) equivale a 10 punti
# Tipologia A (AC >= 7.4 kW, >= 32A per fase).
EQUIVALENZA_B_IN_A = 10

TIPO_INTERVENTO_VALIDI = ("nuova_costruzione", "ristrutturazione_importante", "esistente")


@dataclass
class BuildingProfile:
    """Descrive l'edificio/parcheggio da verificare rispetto al DM 28/10/2025."""

    residenziale: bool
    posti_auto: int
    tipo_intervento: str  # uno di TIPO_INTERVENTO_VALIDI
    accesso_pubblico: bool = False  # rilevante solo se residenziale=False

    # Esclusioni (Capitolo 6, Allegato 1) — il chiamante deve fornirle esplicitamente,
    # non sono derivabili automaticamente dai dati operativi della flotta.
    pmi_proprietaria_e_occupante: bool = False
    permesso_costruire_ante_2021_03_10: bool = False
    costo_ricarica_pct_su_ristrutturazione: Optional[float] = None  # 0-100
    microsistema_isolato_critico: bool = False
    edificio_pubblico_gia_conforme_dlgs257: bool = False

    def __post_init__(self):
        if self.tipo_intervento not in TIPO_INTERVENTO_VALIDI:
            raise ValueError(f"tipo_intervento deve essere uno di {TIPO_INTERVENTO_VALIDI}")
        if self.posti_auto < 0:
            raise ValueError("posti_auto non puo' essere negativo")


@dataclass
class DM2025Result:
    esente: bool
    motivo_esenzione: Optional[str]
    canalizzazione_richiesta: bool
    canalizzazione_quota_posti: Optional[float]  # frazione 0-1 dei posti auto, None se non richiesta
    punti_tipologia_a_minimi_a_regime: int
    punti_tipologia_b_minimi_a_regime: int
    punti_tipologia_a_applicabili_oggi: int  # dopo eventuale fase transitoria (esistenti)
    punti_tipologia_b_applicabili_oggi: int
    fase_transitoria: Optional[str]
    smart_charging_v1g_richiesto: bool = True
    registrazione_pun_richiesta: bool = False  # solo se accesso pubblico
    note_tecniche: list = field(default_factory=list)
    fonti_da_verificare: list = field(default_factory=list)
    disclaimer: str = DISCLAIMER


def _check_esenzioni(p: BuildingProfile) -> Optional[str]:
    if p.pmi_proprietaria_e_occupante:
        return ("Esenzione: PMI (<250 dipendenti, fatturato <=50M EUR o bilancio <=43M EUR) "
                "proprietaria e occupante dell'edificio.")
    if p.permesso_costruire_ante_2021_03_10:
        return "Esenzione: permesso a costruire presentato entro il 10 marzo 2021."
    if p.costo_ricarica_pct_su_ristrutturazione is not None and p.costo_ricarica_pct_su_ristrutturazione > 7.0:
        return (f"Esenzione: costo installazioni ricarica+canalizzazione "
                f"({p.costo_ricarica_pct_su_ristrutturazione:.1f}%) supera il 7% del costo "
                f"totale della ristrutturazione importante.")
    if p.microsistema_isolato_critico:
        return "Esenzione: infrastruttura basata su microsistema isolato con criticita' di stabilita' rete locale."
    if p.edificio_pubblico_gia_conforme_dlgs257:
        return "Esenzione: edificio pubblico gia' conforme al D.Lgs. 257/2016."
    return None


# Tasso "punti Tipologia A ogni 20 posti" per parcheggi non residenziali, <= 100 posti.
# Fonte: sintesi giornalistiche del DM 28/10/2025 (Tabella 5 per accesso privato).
_RATE_PER_20 = {
    ("privato", "nuova_costruzione"): 3,
    ("privato", "ristrutturazione_importante"): 2,
    ("privato", "esistente"): 1,  # documentato esplicitamente per accesso pubblico;
                                   # trattato qui come baseline prudenziale anche per il privato — VERIFICARE.
    ("pubblico", "nuova_costruzione"): 2,
    ("pubblico", "ristrutturazione_importante"): 2,  # non trovato un valore distinto in fonti secondarie — VERIFICARE.
    ("pubblico", "esistente"): 1,
}

# Soglia oltre cui diventa obbligatoria almeno una colonnina "fast" (Tipologia B),
# per parcheggi ad accesso privato. Documentato con certezza solo per
# ristrutturazione importante (>500 posti); per nuova costruzione le fonti parlano
# di "proporzionale alla capienza" senza una soglia numerica netta — approssimato.
_SOGLIA_FAST_PRIVATO = {
    "nuova_costruzione": 500,   # approssimato, VERIFICARE contro Tabella 5 ufficiale
    "ristrutturazione_importante": 500,  # documentato
    "esistente": 500,  # approssimato per analogia, VERIFICARE
}


def _punti_tipologia_a(p: BuildingProfile) -> int:
    if p.posti_auto <= 20:
        return 0
    accesso = "pubblico" if p.accesso_pubblico else "privato"
    rate = _RATE_PER_20.get((accesso, p.tipo_intervento), 1)
    if p.posti_auto <= 100:
        return int(ceil(p.posti_auto / 20) * rate)
    # oltre 100 posti: stessa aliquota applicata a blocchi da 50 (documentato per
    # accesso privato: "3 ogni 50" / "2 ogni 50" — stesso numero della fascia <=100)
    return int(ceil(p.posti_auto / 50) * rate)


def _punti_tipologia_b(p: BuildingProfile) -> int:
    if p.posti_auto <= 20 or p.accesso_pubblico:
        # Per l'accesso pubblico le fonti raccolte non specificano una soglia fast
        # altrettanto netta quanto per il privato: 0 di default, da VERIFICARE.
        return 0
    soglia = _SOGLIA_FAST_PRIVATO.get(p.tipo_intervento, 500)
    if p.posti_auto <= soglia:
        return 0
    # Placeholder prudenziale: almeno 1 punto fast oltre soglia, proporzionale
    # in modo grezzo ogni 500 posti aggiuntivi. DA VERIFICARE contro la tabella
    # ufficiale per il valore esatto.
    return int(1 + (p.posti_auto - soglia) // 500)


def _canalizzazione(p: BuildingProfile) -> tuple[bool, Optional[float]]:
    """Ritorna (richiesta, quota_posti) per nuova costruzione / ristrutturazione importante."""
    if p.tipo_intervento not in ("nuova_costruzione", "ristrutturazione_importante"):
        return False, None
    if p.posti_auto <= 10:
        return False, None
    if p.residenziale:
        return True, 1.0  # tutti i posti auto
    return True, 0.2  # almeno 1 posto ogni 5


def _fase_transitoria(p: BuildingProfile, data_riferimento: date) -> tuple[float, Optional[str]]:
    """Per edifici esistenti non sottoposti a intervento: 50% dal 1/1/2025, 100% dal 1/1/2030.

    Ritorna (percentuale_applicabile, descrizione). Nota: la scadenza 1/1/2025 potrebbe
    essere prorogata dal MASE — verificare lo stato aggiornato prima di usare questo valore.
    """
    if p.tipo_intervento != "esistente":
        return 1.0, None
    if data_riferimento >= date(2030, 1, 1):
        return 1.0, "Edificio esistente: soglia 100% raggiunta (dal 1/1/2030)."
    if data_riferimento >= date(2025, 1, 1):
        return 0.5, ("Edificio esistente in fase transitoria: applicabile il 50% del valore a "
                      "regime (arrotondato per difetto) fino al 1/1/2030. "
                      "ATTENZIONE: questa scadenza potrebbe essere prorogata dal MASE — verificare.")
    return 0.0, "Edificio esistente: obblighi non ancora applicabili (prima del 1/1/2025)."


def compute_dm2025(profile: BuildingProfile, data_riferimento: Optional[date] = None) -> DM2025Result:
    """Calcola gli obblighi minimi DM 28/10/2025 per il profilo edificio/parcheggio dato.

    Vedi il disclaimer del modulo: risultato best-effort, non un parere legale.
    """
    data_riferimento = data_riferimento or date.today()

    motivo = _check_esenzioni(profile)
    if motivo:
        return DM2025Result(
            esente=True,
            motivo_esenzione=motivo,
            canalizzazione_richiesta=False,
            canalizzazione_quota_posti=None,
            punti_tipologia_a_minimi_a_regime=0,
            punti_tipologia_b_minimi_a_regime=0,
            punti_tipologia_a_applicabili_oggi=0,
            punti_tipologia_b_applicabili_oggi=0,
            fase_transitoria=None,
            smart_charging_v1g_richiesto=False,
            registrazione_pun_richiesta=False,
            note_tecniche=["Nessun obbligo di infrastrutturazione EV per esenzione applicabile."],
            fonti_da_verificare=[],
        )

    canalizzazione_ok, quota = _canalizzazione(profile)

    if profile.residenziale:
        punti_a_regime = 0
        punti_b_regime = 0
        pct, desc_fase = 1.0, None
        note = []
        if canalizzazione_ok:
            note.append(
                f"Canalizzazione obbligatoria per tutti i posti auto ({profile.posti_auto}): "
                "tubi corrugati conformi a CEI EN 61386/CEI EN 50626, diametro >=25mm "
                "(interno muro) o >=90mm (interrato)."
            )
        fonti_verifica = []
    else:
        punti_a_regime = _punti_tipologia_a(profile)
        punti_b_regime = _punti_tipologia_b(profile)
        pct, desc_fase = _fase_transitoria(profile, data_riferimento)
        note = []
        if canalizzazione_ok:
            note.append(
                f"Canalizzazione obbligatoria per almeno 1 posto auto ogni 5 "
                f"({ceil(profile.posti_auto * quota)} su {profile.posti_auto}): "
                "tubi corrugati conformi a CEI EN 61386/CEI EN 50626, diametro >=25mm "
                "(interno muro) o >=90mm (interrato)."
            )
        if punti_a_regime > 0:
            note.append(
                f"Punti Tipologia A (AC >= 7.4 kW, >= 32A/fase): {punti_a_regime} a regime "
                f"(parcheggio {'pubblico' if profile.accesso_pubblico else 'privato'}, "
                f"{profile.tipo_intervento})."
            )
        if punti_b_regime > 0:
            note.append(
                f"Punti Tipologia B ('fast', DC >= 50 kW): {punti_b_regime} a regime "
                f"(1 punto B equivale a {EQUIVALENZA_B_IN_A} punti A per la Tabella D — "
                f"puoi sostituire punti A con punti B usando questa equivalenza)."
            )
        fonti_verifica = [
            "Soglie 'punti fast' per parcheggi privati approssimate: verificare Tabella 5 ufficiale.",
            "Tasso per accesso pubblico in ristrutturazione importante non confermato da fonti multiple: verificare.",
            "Baseline 'esistente' per accesso privato assunta uguale al pubblico: verificare.",
        ]
        if desc_fase:
            note.append(desc_fase)

    punti_a_oggi = int(floor(punti_a_regime * pct))
    punti_b_oggi = int(floor(punti_b_regime * pct))

    return DM2025Result(
        esente=False,
        motivo_esenzione=None,
        canalizzazione_richiesta=canalizzazione_ok,
        canalizzazione_quota_posti=quota,
        punti_tipologia_a_minimi_a_regime=punti_a_regime,
        punti_tipologia_b_minimi_a_regime=punti_b_regime,
        punti_tipologia_a_applicabili_oggi=punti_a_oggi,
        punti_tipologia_b_applicabili_oggi=punti_b_oggi,
        fase_transitoria=desc_fase,
        smart_charging_v1g_richiesto=True,
        registrazione_pun_richiesta=bool(profile.accesso_pubblico and not profile.residenziale),
        note_tecniche=note or ["Nessun obbligo di installazione colonnine (sotto soglia posti auto)."],
        fonti_da_verificare=fonti_verifica,
    )


def compare_with_hardware_config(
    dm_result: DM2025Result,
    hw_config: dict,
    hw_db: dict,
) -> dict:
    """Confronta una configurazione hardware (come quella prodotta da josa_core.optimizer)
    con gli obblighi minimi DM 28/10/2025, convertendo i punti DC in equivalenti Tipologia A
    tramite la Tabella D (1 B = 10 A) per un confronto omogeneo.

    Non sostituisce una verifica di conformità puntuale (tipo di connettore, potenza minima
    per fase, ecc.) — e' un controllo di soglia aggregata pensato per un primo screening.
    """
    punti_a_installati = 0
    punti_b_installati = 0
    for tipo, qty in (hw_config or {}).items():
        qty = int(qty)
        if qty <= 0:
            continue
        p_kw = float(hw_db.get(tipo, {}).get("p", 0.0))
        if "DC" in str(tipo) or p_kw >= 50.0:
            punti_b_installati += qty
        else:
            punti_a_installati += qty

    equivalente_a_installato = punti_a_installati + punti_b_installati * EQUIVALENZA_B_IN_A
    equivalente_a_richiesto = (
        dm_result.punti_tipologia_a_applicabili_oggi
        + dm_result.punti_tipologia_b_applicabili_oggi * EQUIVALENZA_B_IN_A
    )

    conforme = dm_result.esente or (equivalente_a_installato >= equivalente_a_richiesto)
    gap = max(0, equivalente_a_richiesto - equivalente_a_installato)

    return {
        "conforme_su_soglia_aggregata": conforme,
        "punti_a_installati": punti_a_installati,
        "punti_b_installati": punti_b_installati,
        "equivalente_tipologia_a_installato": equivalente_a_installato,
        "equivalente_tipologia_a_richiesto": equivalente_a_richiesto,
        "gap_equivalente_tipologia_a": gap,
        "nota": (
            "Confronto aggregato su equivalenza Tabella D (1 Tipologia B = 10 Tipologia A). "
            "Non verifica requisiti puntuali (potenza minima per fase, canalizzazione, "
            "smart charging V1G, sicurezza antincendio). " + DISCLAIMER
        ),
    }
