"""
josa_core.site_scoring — valutazione della qualità di un sito per infrastruttura
di ricarica, su nove criteri: traffico, accessibilità, demografia, servizi
nell'area, connessione rete, disponibilità parcheggio, trasporto pubblico,
distanza da competitor, visibilità.

Metodologia PROPRIA, non conformità letterale a una norma. I criteri (traffico,
demografia, accessibilità, servizi, connessione rete, parcheggio, trasporto
pubblico, distanza competitor, visibilità) sono quelli comunemente usati nella
prassi di settore per la valutazione siti di ricarica pubblica (li abbiamo
verificati sia in letteratura sulla pianificazione — es. DIN SPEC 91433, una
linea guida di processo, non un algoritmo di punteggio — sia osservando quali
categorie usano strumenti concorrenti). Non sono proprietà di nessuno: sono
fattori di buon senso nella scelta di un sito. La FORMULA — pesi, soglie,
bande di punteggio — è una costruzione originale nostra, documentata riga per
riga, non un tentativo di replicare l'algoritmo di un concorrente (che resta
comunque non pubblicamente disponibile).

Pensato soprattutto per siti a uso pubblico o semi-pubblico — per un deposito
aziendale ad uso esclusivo della propria flotta, i criteri "traffico",
"servizi", "trasporto pubblico", "distanza competitor" e "visibilità" contano
economicamente meno: lo strumento lo segnala esplicitamente nel risultato,
non lo nasconde.
"""

from dataclasses import dataclass
from typing import Optional


# Pesi dei nove criteri — sommano a 1.0, modificabili esplicitamente dal chiamante.
# "Connessione rete" ha il peso più alto: per la ricarica EV specificamente (a
# differenza della scelta siti retail generica) è spesso il vincolo più critico
# e costoso da risolvere se manca — un sito perfetto sotto ogni altro aspetto
# ma senza potenza disponibile in rete richiede comunque un investimento enorme
# (potenziamento cabina, allacciamento) prima di essere utilizzabile.
PESI_DEFAULT = {
    "traffico": 0.14,
    "accessibilita": 0.14,
    "demografia": 0.10,
    "servizi": 0.09,
    "connessione_rete": 0.20,
    "parcheggio": 0.10,
    "trasporto_pubblico": 0.06,
    "distanza_competitor": 0.09,
    "visibilita": 0.08,
}


@dataclass
class CriterioInput:
    """Input grezzi per ciascun criterio — l'utente inserisce dati che conosce
    o puo' reperire (es. da un sopralluogo, da dati comunali, da Google Maps,
    da un preventivo del gestore di rete), lo strumento non pretende di avere
    accesso a dati in tempo reale: non c'e' alcuna fonte esterna collegata."""
    traffico_veicoli_giorno: float = 0.0  # transito medio giornaliero sulla strada adiacente
    accesso_facile: bool = True  # ingresso/uscita agevole per veicoli (anche furgoni/mezzi pesanti)
    distanza_arteria_km: float = 1.0  # distanza dalla strada principale/autostrada piu' vicina
    densita_abitanti_km2: float = 0.0  # densita' di popolazione nell'area (ab/km²) — 0 se zona industriale/logistica
    densita_aziende_km2: float = 0.0  # densita' di aziende/uffici nell'area — rilevante per contesto B2B/flotte
    n_servizi_300m: int = 0  # bar, ristoranti, negozi, supermercati entro ~300m
    potenza_disponibile_kw: float = 50.0  # potenza di rete disponibile SENZA potenziamento (da verificare col gestore di rete)
    posti_parcheggio_disponibili: int = 10  # posti auto disponibili nell'area per punti di ricarica + attesa
    distanza_trasporto_pubblico_km: float = 1.0  # distanza dalla fermata di trasporto pubblico piu' vicina
    distanza_competitor_km: float = 2.0  # distanza dal punto di ricarica pubblico esistente piu' vicino
    visibilita: str = "media"  # 'alta' | 'media' | 'bassa' — visibilita' del sito da strada/passanti


@dataclass
class CriterioScore:
    nome: str
    punteggio_0_100: float
    peso: float
    contributo_ponderato: float
    spiegazione: str


@dataclass
class SiteScoringResult:
    punteggio_totale_0_100: float
    grado: str  # 'A'..'F'
    criteri: list  # list[CriterioScore]
    e_deposito_aziendale: bool
    nota_contesto: str


def _grado_da_punteggio(p: float) -> str:
    if p >= 85: return "A"
    if p >= 70: return "B"
    if p >= 55: return "C"
    if p >= 40: return "D"
    if p >= 25: return "E"
    return "F"


def _banda(v: float, soglie: list) -> float:
    """Applica una scala a bande crescenti: [(soglia, punti), ...] in ordine crescente."""
    for soglia, punti in soglie:
        if v < soglia:
            return punti
    return soglie[-1][1]


def _score_traffico(v: float) -> tuple:
    """Più transito = più visibilità/domanda potenziale, con un plateau oltre
    una soglia (il beneficio marginale si appiattisce oltre un certo volume)."""
    punti = _banda(v, [(1000, 15), (5000, 40), (15000, 70), (30000, 90), (float("inf"), 100)])
    return punti, f"{v:.0f} veicoli/giorno stimati"


def _score_accessibilita(accesso_facile: bool, distanza_arteria_km: float) -> tuple:
    base = 70 if accesso_facile else 30
    if distanza_arteria_km <= 0.5: bonus = 30
    elif distanza_arteria_km <= 2.0: bonus = 20
    elif distanza_arteria_km <= 5.0: bonus = 10
    else: bonus = 0
    punti = min(100, base + bonus)
    desc = f"ingresso/uscita {'agevole' if accesso_facile else 'difficoltoso'}, {distanza_arteria_km:.1f} km dall'arteria principale"
    return punti, desc


def _score_demografia(densita_abitanti_km2: float, densita_aziende_km2: float) -> tuple:
    """Combina densità abitativa e densità di aziende — usa il massimo delle
    due bande, non la somma: un'area può essere valida per motivi diversi
    (residenziale denso, oppure zona industriale con molte aziende)."""
    p1 = _banda(densita_abitanti_km2, [(500, 10), (1500, 35), (4000, 65), (8000, 85), (float("inf"), 100)])
    p2 = _banda(densita_aziende_km2, [(20, 10), (60, 40), (150, 70), (float("inf"), 95)])
    punti = max(p1, p2)
    desc = f"{densita_abitanti_km2:.0f} ab/km², {densita_aziende_km2:.0f} aziende/km²"
    return punti, desc


def _score_servizi(n_servizi_300m: int) -> tuple:
    punti = _banda(n_servizi_300m, [(1, 15), (3, 40), (7, 65), (15, 85), (float("inf"), 100)])
    return punti, f"{n_servizi_300m} servizi entro 300m"


def _score_connessione_rete(potenza_disponibile_kw: float) -> tuple:
    """La potenza disponibile SENZA necessità di potenziamento è spesso il
    vincolo più critico e costoso per la ricarica EV specificamente — un sito
    ottimo su ogni altro fronte ma senza potenza disponibile richiede comunque
    un investimento enorme (cabina, allacciamento) prima di essere utilizzabile."""
    punti = _banda(potenza_disponibile_kw, [(20, 10), (50, 35), (100, 60), (250, 85), (float("inf"), 100)])
    return punti, f"{potenza_disponibile_kw:.0f} kW disponibili senza potenziamento"


def _score_parcheggio(posti: int) -> tuple:
    punti = _banda(posti, [(5, 15), (15, 45), (40, 70), (100, 90), (float("inf"), 100)])
    return punti, f"{posti} posti auto disponibili"


def _score_trasporto_pubblico(distanza_km: float) -> tuple:
    """Più vicino = meglio (chi lascia l'auto in carica può usare il mezzo
    pubblico) — rilevante soprattutto per ricarica pubblica, meno per un
    deposito aziendale dove i dipendenti non aspettano lì."""
    if distanza_km <= 0.2: punti = 100
    elif distanza_km <= 0.5: punti = 75
    elif distanza_km <= 1.0: punti = 50
    elif distanza_km <= 2.0: punti = 25
    else: punti = 10
    return punti, f"{distanza_km:.1f} km dalla fermata più vicina"


def _score_distanza_competitor(distanza_km: float) -> tuple:
    """Troppo vicino a un punto di ricarica esistente = cannibalizzazione del
    mercato (punteggio basso). Una distanza moderata è ideale: vicino abbastanza
    da confermare che c'è domanda EV nella zona, lontano abbastanza da non
    competere direttamente. Molto lontano non è necessariamente meglio: può
    significare zona senza alcuna domanda EV, non la 'assenza di concorrenza'."""
    if distanza_km < 0.3: punti = 20  # troppo vicino: cannibalizzazione diretta
    elif distanza_km < 1.0: punti = 55
    elif distanza_km < 3.0: punti = 90  # punto ottimale: domanda confermata, non in competizione diretta
    elif distanza_km < 8.0: punti = 70
    else: punti = 45  # molto isolato: incerto se c'e' davvero domanda EV
    return punti, f"{distanza_km:.1f} km dal punto di ricarica più vicino"


def _score_visibilita(visibilita: str) -> tuple:
    mappa = {"alta": 100, "media": 60, "bassa": 20}
    v = str(visibilita).strip().lower()
    punti = mappa.get(v, 60)
    return punti, f"visibilità {v}"


def compute_site_score(
    input_dati: CriterioInput,
    pesi: Optional[dict] = None,
    e_deposito_aziendale: bool = False,
) -> SiteScoringResult:
    """Calcola il punteggio del sito sui nove criteri, con pesi espliciti.

    e_deposito_aziendale: se True, aggiunge una nota esplicita che alcuni
    criteri (traffico, servizi, trasporto pubblico, distanza competitor,
    visibilità) contano economicamente meno per un deposito ad uso esclusivo
    della propria flotta (nessun cliente esterno da attrarre) — non altera il
    calcolo, informa chi legge il risultato.
    """
    pesi = pesi or PESI_DEFAULT
    tot_pesi = sum(pesi.values())
    if abs(tot_pesi - 1.0) > 1e-6:
        raise ValueError(f"I pesi devono sommare a 1.0 (somma attuale: {tot_pesi:.3f})")

    calcoli = [
        ("Traffico", "traffico", _score_traffico(input_dati.traffico_veicoli_giorno)),
        ("Accessibilità", "accessibilita", _score_accessibilita(input_dati.accesso_facile, input_dati.distanza_arteria_km)),
        ("Demografia", "demografia", _score_demografia(input_dati.densita_abitanti_km2, input_dati.densita_aziende_km2)),
        ("Servizi nell'area", "servizi", _score_servizi(input_dati.n_servizi_300m)),
        ("Connessione rete", "connessione_rete", _score_connessione_rete(input_dati.potenza_disponibile_kw)),
        ("Disponibilità parcheggio", "parcheggio", _score_parcheggio(input_dati.posti_parcheggio_disponibili)),
        ("Trasporto pubblico", "trasporto_pubblico", _score_trasporto_pubblico(input_dati.distanza_trasporto_pubblico_km)),
        ("Distanza competitor", "distanza_competitor", _score_distanza_competitor(input_dati.distanza_competitor_km)),
        ("Visibilità", "visibilita", _score_visibilita(input_dati.visibilita)),
    ]

    criteri = []
    for nome, chiave, (punti, desc) in calcoli:
        peso = pesi.get(chiave, 0.0)
        criteri.append(CriterioScore(nome, punti, peso, punti * peso, desc))

    punteggio_totale = sum(c.contributo_ponderato for c in criteri)
    grado = _grado_da_punteggio(punteggio_totale)

    nota = ""
    if e_deposito_aziendale:
        nota = (
            "Sito segnalato come deposito aziendale ad uso esclusivo della flotta: i criteri "
            "'Traffico', 'Servizi nell'area', 'Trasporto pubblico', 'Distanza competitor' e "
            "'Visibilità' hanno un impatto economico minore rispetto a un sito pubblico (non ci "
            "sono clienti esterni da attrarre o concorrenti da considerare) — il punteggio resta "
            "calcolato con gli stessi pesi per coerenza, ma va letto con questo contesto. "
            "'Connessione rete' e 'Accessibilità' restano rilevanti in ogni caso."
        )

    return SiteScoringResult(
        punteggio_totale_0_100=round(punteggio_totale, 1),
        grado=grado,
        criteri=criteri,
        e_deposito_aziendale=e_deposito_aziendale,
        nota_contesto=nota,
    )
