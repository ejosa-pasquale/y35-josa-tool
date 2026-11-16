"""
Prezzi indicizzati dell'energia al quarto d'ora, per l'orchestrazione a 30 giorni.

Da dove vengono i numeri
------------------------
- Prezzo all'ingrosso: esito del Mercato del Giorno Prima (MGP) del GME, per zona di
  mercato, al quarto d'ora (MTU 15 minuti dal 1 ottobre 2025; prima orario, qui
  ripetuto sui 4 quarti d'ora). Il GME pubblica gli esiti e li trasmette alla
  piattaforma di trasparenza ENTSO-E, che li ripubblica con un'API gratuita (token
  personale, riuso consentito citando la fonte). Il server li scarica da ENTSO-E se
  la variabile d'ambiente ENTSOE_TOKEN e' impostata.
- PUN Index GME: media nazionale dei prezzi zonali pesata sui consumi. Si trova solo
  sul sito GME (accesso automatico con registrazione): non ancora collegato.
- ARERA non pubblica prezzi al quarto d'ora: definisce la struttura della bolletta
  (perdite di rete, dispacciamento, trasporto e oneri, quota potenza, accise). Qui
  sono valori di partenza dichiarati, modificabili dal cliente.
- Senza token, o se la fonte non risponde, si usa una SERIE DI RISERVA: un profilo
  tipico stimato (forma giornaliera con picco serale e calo nelle ore solari),
  dichiarato come tale nel risultato. Non sono prezzi reali.

Zone di mercato (configurazione Terna in vigore dal 2021): Nord, Centro-Nord,
Centro-Sud, Sud, Calabria, Sicilia, Sardegna.
"""
from __future__ import annotations

import math
import os
import random
import time as _time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    _TZ_ROMA = ZoneInfo("Europe/Rome")
except Exception:  # pragma: no cover - zoneinfo assente
    _TZ_ROMA = None

SLOT_GIORNO = 96

ZONE = {
    # zona: (nome esteso, codice EIC dell'area di offerta su ENTSO-E)
    "NORD": ("Nord", "10Y1001A1001A73I"),
    "CNOR": ("Centro-Nord", "10Y1001A1001A70O"),
    "CSUD": ("Centro-Sud", "10Y1001A1001A71M"),
    "SUD": ("Sud", "10Y1001A1001A788"),
    "CALA": ("Calabria", "10Y1001C--00096J"),
    "SICI": ("Sicilia", "10Y1001A1001A75E"),
    "SARD": ("Sardegna", "10Y1001A1001A74G"),
}

# Regione -> zona di mercato (configurazione zonale Terna dal 1/1/2021).
REGIONE_ZONA = {
    "valle d'aosta": "NORD", "piemonte": "NORD", "liguria": "NORD", "lombardia": "NORD",
    "trentino-alto adige": "NORD", "veneto": "NORD", "friuli-venezia giulia": "NORD",
    "emilia-romagna": "NORD",
    "toscana": "CNOR", "marche": "CNOR",
    "umbria": "CSUD", "lazio": "CSUD", "abruzzo": "CSUD", "campania": "CSUD",
    "molise": "SUD", "puglia": "SUD", "basilicata": "SUD",
    "calabria": "CALA",
    "sicilia": "SICI",
    "sardegna": "SARD",
}


def zona_da_regione(regione: Optional[str]) -> str:
    """Zona di mercato della regione (nome italiano, maiuscole/trattini indifferenti).
    Regione assente o sconosciuta -> NORD (zona dei piloti Emilia-Romagna e Lombardia)."""
    if not regione:
        return "NORD"
    k = str(regione).strip().lower().replace("–", "-").replace(" - ", "-")
    k = k.replace("emilia romagna", "emilia-romagna").replace("friuli venezia giulia", "friuli-venezia giulia")
    k = k.replace("trentino alto adige", "trentino-alto adige").replace("valle d’aosta", "valle d'aosta")
    return REGIONE_ZONA.get(k, "NORD")


# ---------------------------------------------------------------------------
# Componenti della bolletta (struttura ARERA): valori di partenza dichiarati
# ---------------------------------------------------------------------------

@dataclass
class ComponentiBolletta:
    """Cosa si aggiunge al prezzo all'ingrosso per arrivare al costo al POD (IVA esclusa).

    Valori di partenza indicativi per un'utenza non domestica in bassa tensione: vanno
    verificati sulla bolletta del cliente e si possono modificare tutti."""
    perdite_rete_pct: float = 10.0          # BT; in MT circa 4%
    spread_fornitore_eur_kwh: float = 0.015
    dispacciamento_eur_kwh: float = 0.012
    trasporto_oneri_eur_kwh: float = 0.045  # quote energia di trasporto, ASOS, ARIM
    accisa_eur_kwh: float = 0.0125
    quota_potenza_eur_kw_mese: float = 3.0  # trasporto + oneri sulla potenza prelevata
    # V2G: energia ceduta in rete pagata al prezzo zonale (ritiro al prezzo di mercato)
    vendita_quota_prezzo_zonale: float = 1.0

    def prezzo_acquisto(self, ingrosso_eur_mwh: float) -> float:
        """EUR/kWh pagati per un kWh prelevato in quel quarto d'ora."""
        e = ingrosso_eur_mwh / 1000.0
        return (e * (1.0 + self.perdite_rete_pct / 100.0) + self.spread_fornitore_eur_kwh
                + self.dispacciamento_eur_kwh + self.trasporto_oneri_eur_kwh + self.accisa_eur_kwh)

    def prezzo_vendita(self, ingrosso_eur_mwh: float) -> float:
        return max(0.0, ingrosso_eur_mwh / 1000.0) * self.vendita_quota_prezzo_zonale

    def come_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Serie di prezzi
# ---------------------------------------------------------------------------

@dataclass
class SeriePrezzi:
    zona: str
    giorni: list            # list[date], uno per giorno
    eur_mwh: list           # list[list[float]] 96 valori per giorno (ora locale italiana)
    fonte: str              # "entsoe" | "riserva"
    descrizione: str
    risoluzione_min: int = 15
    avvisi: list = field(default_factory=list)


# Livello medio della serie di riserva per zona (EUR/MWh). Ordine di grandezza dei
# prezzi MGP 2025; la forma della giornata conta piu' del livello per il confronto tra
# scenari, il livello conta per il costo assoluto.
_LIVELLO_RISERVA = {"NORD": 110.0, "CNOR": 110.0, "CSUD": 107.0, "SUD": 100.0,
                    "CALA": 98.0, "SICI": 115.0, "SARD": 105.0}

# Forma della giornata (moltiplicatore sul livello medio), ore 0..23 locali.
_FORMA_FERIALE = [0.88, 0.84, 0.82, 0.81, 0.82, 0.87, 0.97, 1.08, 1.10, 1.02, 0.92, 0.86,
                  0.82, 0.80, 0.84, 0.92, 1.03, 1.16, 1.30, 1.36, 1.30, 1.16, 1.04, 0.95]
_FORMA_FESTIVA = [0.86, 0.82, 0.79, 0.78, 0.78, 0.80, 0.84, 0.88, 0.86, 0.80, 0.74, 0.70,
                  0.67, 0.66, 0.70, 0.78, 0.90, 1.05, 1.20, 1.27, 1.22, 1.10, 1.00, 0.93]


def serie_riserva(zona: str, giorni: list) -> SeriePrezzi:
    """Profilo tipico stimato, deterministico (stessi giorni -> stessi prezzi)."""
    livello = _LIVELLO_RISERVA.get(zona, 110.0)
    out = []
    for g in giorni:
        rng = random.Random(g.toordinal() * 31 + sum(map(ord, zona)))
        lv = livello * (1.0 + rng.uniform(-0.10, 0.10))
        forma = _FORMA_FESTIVA if g.weekday() >= 5 else _FORMA_FERIALE
        fest = 0.88 if g.weekday() >= 5 else 1.0
        giorno = []
        for s in range(SLOT_GIORNO):
            h = s / 4.0
            h0 = int(h)
            fr = h - h0
            m = forma[h0] * (1 - fr) + forma[(h0 + 1) % 24] * fr
            giorno.append(round(lv * fest * m * (1.0 + rng.uniform(-0.03, 0.03)), 2))
        out.append(giorno)
    return SeriePrezzi(
        zona=zona, giorni=list(giorni), eur_mwh=out, fonte="riserva",
        descrizione=("Serie di riserva: profilo tipico stimato (picco serale, calo nelle ore solari, "
                     f"livello medio ~{livello:.0f} EUR/MWh). Non sono prezzi reali di mercato."),
    )


def ultimi_giorni(n: int = 30, oggi: Optional[date] = None) -> list:
    """Gli ultimi n giorni completi (fino a ieri), ora italiana."""
    if oggi is None:
        oggi = datetime.now(_TZ_ROMA).date() if _TZ_ROMA else date.today()
    return [oggi - timedelta(days=n - i) for i in range(n)]


# ---- ENTSO-E (prezzi day-ahead per zona, documentType A44) ----

_ENTSOE_URL = "https://web-api.tp.entsoe.eu/api"
_CACHE: dict = {}
_CACHE_TTL_S = 6 * 3600


def _durata_iso_min(s: str) -> int:
    s = (s or "").upper()
    if s == "PT15M":
        return 15
    if s == "PT30M":
        return 30
    if s in ("PT60M", "PT1H"):
        return 60
    raise ValueError(f"risoluzione non gestita: {s}")


def parse_entsoe_xml(xml_text: str) -> dict:
    """XML A44 -> {datetime UTC di inizio intervallo: EUR/MWh} a passi di 15 minuti.

    Gestisce risoluzioni PT15M/PT30M/PT60M (ripetute sui quarti d'ora) e la
    compressione A03 (punti mancanti = stesso prezzo del punto precedente)."""
    root = ET.fromstring(xml_text)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
    if root.tag.endswith("Acknowledgement_MarketDocument"):
        motivo = root.find(f".//{ns}Reason/{ns}text")
        raise ValueError("ENTSO-E: " + (motivo.text if motivo is not None else "nessun dato"))
    prezzi: dict = {}
    for ts in root.iter(f"{ns}TimeSeries"):
        for per in ts.iter(f"{ns}Period"):
            start = per.find(f"{ns}timeInterval/{ns}start").text
            end = per.find(f"{ns}timeInterval/{ns}end").text
            res = _durata_iso_min(per.find(f"{ns}resolution").text)
            t0 = datetime.strptime(start, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
            t1 = datetime.strptime(end, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
            n = int(round((t1 - t0).total_seconds() / 60 / res))
            punti = {}
            for p in per.iter(f"{ns}Point"):
                pos = int(p.find(f"{ns}position").text)
                punti[pos] = float(p.find(f"{ns}price.amount").text)
            ultimo = None
            for pos in range(1, n + 1):
                if pos in punti:
                    ultimo = punti[pos]
                if ultimo is None:
                    continue
                inizio = t0 + timedelta(minutes=res * (pos - 1))
                for q in range(res // 15):
                    prezzi.setdefault(inizio + timedelta(minutes=15 * q), ultimo)
    return prezzi


def _griglia_locale(prezzi_utc: dict, giorni: list) -> tuple[list, int]:
    """Riporta i prezzi su 96 quarti d'ora per giorno in ora italiana.
    Giorni di cambio ora: il quarto d'ora mancante prende il valore precedente, quello
    doppio tiene il primo. Ritorna (griglia, quarti d'ora mancanti riempiti)."""
    if _TZ_ROMA is None:
        raise RuntimeError("fuso orario Europe/Rome non disponibile")
    griglia, mancanti = [], 0
    for g in giorni:
        riga, prec = [], None
        for s in range(SLOT_GIORNO):
            loc = datetime(g.year, g.month, g.day, s // 4, 15 * (s % 4), tzinfo=_TZ_ROMA)
            v = prezzi_utc.get(loc.astimezone(timezone.utc))
            if v is None:
                mancanti += 1
                v = prec
            riga.append(v)
            prec = v if v is not None else prec
        if riga and riga[0] is None:
            primo = next((x for x in riga if x is not None), None)
            riga = [primo if x is None else x for x in riga]
        griglia.append(riga)
    return griglia, mancanti


def scarica_entsoe(zona: str, giorni: list, token: str, timeout_s: float = 20.0) -> SeriePrezzi:
    import httpx

    eic = ZONE[zona][1]
    t0 = datetime(giorni[0].year, giorni[0].month, giorni[0].day) - timedelta(days=1)
    t1 = datetime(giorni[-1].year, giorni[-1].month, giorni[-1].day) + timedelta(days=2)
    params = {
        "securityToken": token, "documentType": "A44",
        "in_Domain": eic, "out_Domain": eic,
        "periodStart": t0.strftime("%Y%m%d0000"), "periodEnd": t1.strftime("%Y%m%d0000"),
    }
    r = httpx.get(_ENTSOE_URL, params=params, timeout=timeout_s)
    if r.status_code != 200:
        raise ValueError(f"ENTSO-E ha risposto {r.status_code}")
    prezzi = parse_entsoe_xml(r.text)
    griglia, mancanti = _griglia_locale(prezzi, giorni)
    if any(v is None for riga in griglia for v in riga):
        raise ValueError("ENTSO-E: serie incompleta")
    avvisi = [f"{mancanti} quarti d'ora mancanti completati con il valore precedente"] if mancanti > 8 else []
    return SeriePrezzi(
        zona=zona, giorni=list(giorni), eur_mwh=griglia, fonte="entsoe",
        descrizione=(f"Prezzi MGP zona {ZONE[zona][0]} (GME), dalla piattaforma di trasparenza "
                     f"ENTSO-E, {giorni[0].strftime('%d/%m/%Y')} - {giorni[-1].strftime('%d/%m/%Y')}."),
        avvisi=avvisi,
    )


def serie_prezzi(zona: str = "NORD", n_giorni: int = 30, oggi: Optional[date] = None,
                 usa_rete: bool = True) -> SeriePrezzi:
    """Gli ultimi n_giorni di prezzi della zona: ENTSO-E se possibile, altrimenti riserva."""
    zona = zona if zona in ZONE else "NORD"
    giorni = ultimi_giorni(n_giorni, oggi)
    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    if not (usa_rete and token):
        s = serie_riserva(zona, giorni)
        s.avvisi.append("Prezzi reali non collegati (manca ENTSOE_TOKEN sul server): usata la serie di riserva.")
        return s
    chiave = (zona, giorni[0], giorni[-1])
    hit = _CACHE.get(chiave)
    if hit and _time.time() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    try:
        s = scarica_entsoe(zona, giorni, token)
    except Exception as e:  # rete, token, formato: si ripiega sulla riserva, dichiarandolo
        s = serie_riserva(zona, giorni)
        s.avvisi.append(f"Prezzi reali non disponibili ({str(e)[:120]}): usata la serie di riserva.")
        return s
    _CACHE[chiave] = (_time.time(), s)
    return s
