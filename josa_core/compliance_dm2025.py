"""
josa_core.compliance_dm2025 — Verifica obblighi minimi di infrastrutturazione EV
secondo il Decreto Ministeriale 28 ottobre 2025 (MASE, Gazzetta Ufficiale
n. 283 del 5 dicembre 2025, in vigore dal 3 giugno 2026), che aggiorna il DM 26
giugno 2015 sui Requisiti Minimi degli edifici — Allegato 1, Capitolo 6
("Requisiti e prescrizioni per l'integrazione delle tecnologie per la ricarica
dei veicoli elettrici").

Fonte: testo ufficiale del decreto, Capitolo 6, punto 6.2:
  - Tabella 4: parcheggi ad accesso pubblico (comma 1);
  - Tabella 5: parcheggi ad accesso privato, es. parcheggi aziendali e di
    flotta (comma 2);
  - colonne per nuova costruzione, ristrutturazione importante e "tutti gli
    edifici non residenziali esistenti, anche se non sottoposti a
    ristrutturazione" (50% arrotondato per difetto entro il 01/01/2025, 100%
    entro il 01/01/2030);
  - comma 3: 1 punto Tipologia B equivale a 10 punti Tipologia A; nei
    parcheggi privati 1 punto B si puo' sostituire con 10 punti A; smart
    charging V1G obbligatorio.

============================== AVVISO IMPORTANTE ==============================
Questo modulo NON e' un parere legale e non sostituisce la verifica di un
professionista abilitato. Punti che il testo lascia aperti e che qui sono
risolti con una scelta dichiarata:
  - sopra i 100 posti la cella "X per ogni 50 posti auto" (Tipologia A) e'
    letta come valida per tutte le fasce oltre 100 e i numeri delle fasce
    successive come punti Tipologia B (lettura del testo estratto, coerente
    con le principali sintesi tecniche): confermare sulle pagine 30-31 della GU;
  - "per ogni 20/50 posti" e' arrotondato per eccesso (ogni gruppo iniziato
    conta): scelta prudenziale, il decreto non indica l'arrotondamento;
  - per gli edifici esistenti il 50% "arrotondato per difetto, dei valori
    indicati nelle colonne" e' applicato ai valori della tabella (es. 3 per
    ogni 20 -> 1 per ogni 20), come scritto; la lettura alternativa (50% del
    totale) e' riportata nelle note;
  - le esclusioni (art. 4, comma 1-bis, lettera f del D.Lgs. 192/2005: PMI
    proprietaria e occupante, costo oltre 7% della ristrutturazione,
    microsistemi isolati, edifici pubblici gia' conformi) vanno verificate
    caso per caso: non sono derivabili da un dataset di flotta.
================================================================================
"""

from dataclasses import dataclass, field
from datetime import date
from math import ceil, floor
from typing import Optional

DISCLAIMER = (
    "Calcolo basato sul DM 28/10/2025 (GU n. 283 del 5/12/2025), Allegato 1, Capitolo 6, "
    "Tabelle 4 e 5. Non costituisce parere legale: arrotondamenti e lettura delle fasce oltre "
    "100 posti sono scelte dichiarate. Validare con un professionista abilitato prima di "
    "decisioni di investimento o pratiche edilizie."
)

# Punto 6.2, comma 3: al posto di 10 punti Tipologia A (Pn >= 7,4 kW, >= 32 A per fase)
# si puo' installare 1 sistema Tipologia B (corrente continua, Pn >= 50 kW).
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


# DM 28/10/2025, Allegato 1, Cap. 6, punto 6.2 — Tabella 4 (accesso pubblico) e
# Tabella 5 (accesso privato). Per ogni caso:
#   a_11_20: punti Tipologia A per 11-20 posti (numero fisso; gli esistenti partono da 21)
#   a_20:    punti A "per ogni 20 posti auto" (21-100 posti)
#   a_50:    punti A "per ogni 50 posti auto" (oltre 100 posti)
#   b:       punti Tipologia B per fascia oltre 100 posti [(fino a posti, punti B), ...]
_INF = float("inf")
TABELLE_DM2025 = {
    ("pubblico", "nuova_costruzione"):          {"a_11_20": 2, "a_20": 2, "a_50": 2, "b": [(250, 1), (500, 2), (1000, 3), (_INF, 4)]},
    ("pubblico", "ristrutturazione_importante"): {"a_11_20": 1, "a_20": 1, "a_50": 1, "b": [(250, 0), (500, 1), (1000, 2), (_INF, 3)]},
    ("pubblico", "esistente"):                  {"a_11_20": 0, "a_20": 2, "a_50": 2, "b": [(250, 1), (500, 2), (1000, 3), (_INF, 4)]},
    ("privato", "nuova_costruzione"):           {"a_11_20": 3, "a_20": 3, "a_50": 3, "b": [(500, 1), (1000, 2), (_INF, 3)]},
    ("privato", "ristrutturazione_importante"):  {"a_11_20": 2, "a_20": 2, "a_50": 2, "b": [(500, 0), (1000, 1), (_INF, 2)]},
    ("privato", "esistente"):                   {"a_11_20": 0, "a_20": 3, "a_50": 3, "b": [(500, 1), (1000, 2), (_INF, 3)]},
}


def _tabella(p: BuildingProfile) -> dict:
    return TABELLE_DM2025[("pubblico" if p.accesso_pubblico else "privato", p.tipo_intervento)]


def _punti_a_con_valori(posti: int, t: dict) -> int:
    if posti <= 10:
        return 0
    if posti <= 20:
        return int(t["a_11_20"])
    if posti <= 100:
        return int(ceil(posti / 20) * t["a_20"])
    return int(ceil(posti / 50) * t["a_50"])


def _punti_b_con_valori(posti: int, t: dict) -> int:
    if posti <= 100:
        return 0
    for fino_a, punti in t["b"]:
        if posti <= fino_a:
            return int(punti)
    return 0


def _punti_tipologia_a(p: BuildingProfile) -> int:
    return _punti_a_con_valori(p.posti_auto, _tabella(p))


def _punti_tipologia_b(p: BuildingProfile) -> int:
    return _punti_b_con_valori(p.posti_auto, _tabella(p))


def _valori_ridotti(t: dict, pct: float) -> dict:
    """Valori della tabella ridotti alla percentuale, arrotondati per difetto
    (edifici esistenti: 'il 50%, arrotondato per difetto, dei valori indicati
    nelle colonne di appartenenza')."""
    return {
        "a_11_20": int(floor(t["a_11_20"] * pct)),
        "a_20": int(floor(t["a_20"] * pct)),
        "a_50": int(floor(t["a_50"] * pct)),
        "b": [(f, int(floor(v * pct))) for f, v in t["b"]],
    }


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
                f"(al posto di {EQUIVALENZA_B_IN_A} punti A si puo' installare 1 punto B — "
                f"punto 6.2, comma 3)."
            )
        fonti_verifica = [
            "Oltre 100 posti: 'per ogni 50 posti' letto su tutte le fasce e numeri delle fasce successive "
            "letti come punti Tipologia B — confermare sulle Tabelle 4/5 della GU (pp. 30-31).",
            "'Per ogni 20/50 posti' arrotondato per eccesso: il decreto non indica l'arrotondamento.",
        ]
        if profile.tipo_intervento == "ristrutturazione_importante":
            fonti_verifica.append(
                "Ristrutturazione importante: l'obbligo vale se il parcheggio e' interno o adiacente "
                "all'edificio e i lavori riguardano anche il parcheggio o i suoi impianti elettrici."
            )
        if punti_b_regime > 0 and not profile.accesso_pubblico:
            note.append(
                "Parcheggio privato: ogni punto Tipologia B puo' essere sostituito da 10 punti Tipologia A "
                "(punto 6.2, comma 3)."
            )
        if desc_fase:
            note.append(desc_fase)

    if profile.residenziale or pct >= 1.0:
        punti_a_oggi, punti_b_oggi = int(floor(punti_a_regime * pct)), int(floor(punti_b_regime * pct))
    else:
        ridotti = _valori_ridotti(_tabella(profile), pct)
        punti_a_oggi = _punti_a_con_valori(profile.posti_auto, ridotti)
        punti_b_oggi = _punti_b_con_valori(profile.posti_auto, ridotti)
        if pct > 0 and (punti_a_regime or punti_b_regime):
            alt_a, alt_b = int(floor(punti_a_regime * pct)), int(floor(punti_b_regime * pct))
            if (alt_a, alt_b) != (punti_a_oggi, punti_b_oggi):
                note.append(
                    f"Lettura alternativa della fase transitoria (50% del totale invece che dei valori "
                    f"della tabella): {alt_a} punti A e {alt_b} punti B oggi."
                )

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
    con l'equivalenza del punto 6.2, comma 3 (1 B = 10 A) per un confronto omogeneo.

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
        # Tipologia B: corrente continua >= 50 kW; Tipologia A: >= 7,4 kW.
        # Un DC sotto 50 kW conta come punto A; sotto 7,4 kW non conta.
        if "DC" in str(tipo).upper() and p_kw >= 50.0:
            punti_b_installati += qty
        elif p_kw >= 7.4:
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
            "Confronto aggregato con l'equivalenza del punto 6.2, comma 3 (1 Tipologia B = 10 Tipologia A). "
            "Non verifica requisiti puntuali (potenza minima per fase, canalizzazione, "
            "smart charging V1G, sicurezza antincendio). " + DISCLAIMER
        ),
    }
