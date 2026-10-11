"""
josa_core.tco_powertrain — TCO per modello a tre colonne (ICE / PHEV / BEV) e
business case dell'infrastruttura con rampa di adozione.

Calibrato sui casi reali della metodologia Y35:
- il costo mensile di un veicolo e' canone NLT + energia/carburante;
- il prezzo dell'energia di PHEV e BEV dipende dalla readiness domestica: la quota
  ricaricata a casa paga la tariffa domestica, il resto la tariffa aziendale (se c'e'
  infrastruttura in sede) o quella pubblica (se non c'e');
- il PHEV percorre in elettrico la quota_elettrica dei km (70% con ricarica regolare)
  e il resto a carburante;
- il payback dell'infrastruttura usa una rampa di adozione (30% / 90% / 100%) e un
  unico tasso di sconto (TASSO_SCONTO_DEFAULT).

Modulo nuovo e additivo: non modifica compute_tco_analysis (Diesel vs EV).
"""

from dataclasses import dataclass, field
from typing import Optional

from .utils import TASSO_SCONTO_DEFAULT

RAMPA_ADOZIONE_DEFAULT = (0.30, 0.90, 1.00)


def prezzo_energia_da_readiness(
    readiness_pct: float,
    prezzo_domestico_eur_kwh: float,
    prezzo_aziendale_eur_kwh: float,
    prezzo_pubblico_eur_kwh: float,
    copertura_aziendale_pct: float = 100.0,
) -> float:
    """Prezzo medio dell'energia per un veicolo elettrificato.

    readiness_pct: quota di ricarica fatta a casa (0-100). Puo' essere 0.
    copertura_aziendale_pct: quota della ricarica non domestica che avviene in sede
    (100 = infrastruttura aziendale disponibile, 0 = solo ricarica pubblica).
    """
    r = max(0.0, min(100.0, float(readiness_pct))) / 100.0
    c = max(0.0, min(100.0, float(copertura_aziendale_pct))) / 100.0
    fuori_casa = c * prezzo_aziendale_eur_kwh + (1.0 - c) * prezzo_pubblico_eur_kwh
    return r * prezzo_domestico_eur_kwh + (1.0 - r) * fuori_casa


@dataclass
class ModelloVeicolo:
    """Canoni NLT mensili per powertrain (None = variante non disponibile)."""
    nome: str
    canone_ice_mese_eur: Optional[float] = None
    canone_phev_mese_eur: Optional[float] = None
    canone_bev_mese_eur: Optional[float] = None
    consumo_ice_l100km: float = 6.0
    consumo_phev_kwh100km: float = 17.0
    consumo_phev_l100km: float = 6.0
    consumo_bev_kwh100km: float = 18.0


@dataclass
class TCOModelloResult:
    modello: str
    km_anno: float
    prezzo_energia_eur_kwh: float
    ice_mese_eur: Optional[float]
    phev_mese_eur: Optional[float]
    bev_mese_eur: Optional[float]
    energia_ice_mese_eur: Optional[float]
    energia_phev_mese_eur: Optional[float]
    energia_bev_mese_eur: Optional[float]
    raccomandazione: str
    # Km/anno oltre i quali il primo powertrain costa meno del secondo (None = mai / sempre)
    km_pareggio_phev_vs_ice: Optional[float] = None
    km_pareggio_bev_vs_ice: Optional[float] = None
    km_pareggio_bev_vs_phev: Optional[float] = None
    note: list = field(default_factory=list)


def _costo_km(m: ModelloVeicolo, prezzo_carburante: float, prezzo_energia: float, quota_elettrica: float) -> dict:
    return {
        "ice": m.consumo_ice_l100km / 100.0 * prezzo_carburante,
        "phev": (quota_elettrica * m.consumo_phev_kwh100km / 100.0 * prezzo_energia
                 + (1.0 - quota_elettrica) * m.consumo_phev_l100km / 100.0 * prezzo_carburante),
        "bev": m.consumo_bev_kwh100km / 100.0 * prezzo_energia,
    }


def _km_pareggio(canone_a, canone_b, costo_km_a, costo_km_b) -> Optional[float]:
    """Km/anno oltre cui A (canone piu' alto, costo/km piu' basso) costa meno di B."""
    if canone_a is None or canone_b is None:
        return None
    delta_canone_anno = (canone_a - canone_b) * 12.0
    delta_km = costo_km_b - costo_km_a
    if delta_canone_anno <= 0:
        return 0.0 if delta_km >= 0 else None  # A costa meno gia' da 0 km
    if delta_km <= 0:
        return None  # A non recupera mai il canone piu' alto
    return round(delta_canone_anno / delta_km, 0)


def tco_mensile_modello(
    modello: ModelloVeicolo,
    km_anno: float,
    prezzo_carburante_eur_l: float,
    prezzo_energia_eur_kwh: float,
    quota_elettrica_phev: float = 0.70,
) -> TCOModelloResult:
    """TCO mensile (canone + energia/carburante) per ICE, PHEV e BEV dello stesso modello."""
    ck = _costo_km(modello, prezzo_carburante_eur_l, prezzo_energia_eur_kwh, quota_elettrica_phev)
    canoni = {"ice": modello.canone_ice_mese_eur, "phev": modello.canone_phev_mese_eur, "bev": modello.canone_bev_mese_eur}
    energia = {k: (ck[k] * km_anno / 12.0 if canoni[k] is not None else None) for k in ck}
    totale = {k: (canoni[k] + energia[k] if canoni[k] is not None else None) for k in ck}

    disponibili = {k: v for k, v in totale.items() if v is not None}
    nomi = {"ice": "ICE", "phev": "PHEV", "bev": "BEV"}
    if disponibili:
        migliore = min(disponibili, key=disponibili.get)
        racc = f"{nomi[migliore]} il piu' conveniente"
    else:
        migliore, racc = None, "Nessun canone indicato"
    note = []
    if totale["phev"] is not None and totale["ice"] is not None and totale["phev"] > totale["ice"]:
        note.append("PHEV piu' caro dell'ICE: il premio di canone supera il risparmio di carburante a questa percorrenza.")
    if totale["phev"] is not None:
        note.append(f"PHEV calcolato con {quota_elettrica_phev*100:.0f}% dei km in elettrico: senza ricarica regolare costa piu' di un ICE.")

    return TCOModelloResult(
        modello=modello.nome,
        km_anno=km_anno,
        prezzo_energia_eur_kwh=round(prezzo_energia_eur_kwh, 4),
        ice_mese_eur=_r(totale["ice"]), phev_mese_eur=_r(totale["phev"]), bev_mese_eur=_r(totale["bev"]),
        energia_ice_mese_eur=_r(energia["ice"]), energia_phev_mese_eur=_r(energia["phev"]), energia_bev_mese_eur=_r(energia["bev"]),
        raccomandazione=racc,
        km_pareggio_phev_vs_ice=_km_pareggio(canoni["phev"], canoni["ice"], ck["phev"], ck["ice"]),
        km_pareggio_bev_vs_ice=_km_pareggio(canoni["bev"], canoni["ice"], ck["bev"], ck["ice"]),
        km_pareggio_bev_vs_phev=_km_pareggio(canoni["bev"], canoni["phev"], ck["bev"], ck["phev"]),
        note=note,
    )


def _r(x):
    return None if x is None else round(x, 1)


@dataclass
class BusinessCaseResult:
    cashflow_annuo_eur: list
    cashflow_cumulativo_eur: list
    payback_mesi: Optional[int]
    van_eur: float
    tasso_sconto: float
    capex_a_t0: bool


def business_case_infrastruttura(
    capex_per_anno_eur: list,
    risparmio_a_regime_eur: float,
    anni: int = 5,
    rampa_adozione: tuple = RAMPA_ADOZIONE_DEFAULT,
    tasso_sconto: float = TASSO_SCONTO_DEFAULT,
    capex_a_t0: bool = True,
) -> BusinessCaseResult:
    """Cash flow, payback (mesi) e VAN dell'infrastruttura con rampa di adozione.

    capex_per_anno_eur: CAPEX per anno (es. [103000] oppure [231000, 154000] se fasato).
    rampa_adozione: quota del risparmio a regime per anno 1, 2, ...; oltre l'ultima vale 1.
    capex_a_t0: True (convenzione standard) = il CAPEX del primo anno e' all'anno 0, non
    scontato; False = tutti i flussi a fine anno (convenzione usata in alcuni report).
    """
    risparmi = [risparmio_a_regime_eur * (rampa_adozione[i] if i < len(rampa_adozione) else 1.0) for i in range(anni)]
    capex = list(capex_per_anno_eur) + [0.0] * max(0, anni - len(capex_per_anno_eur))
    netti = [risparmi[i] - capex[i] for i in range(anni)]

    if capex_a_t0:
        flussi = [-capex[0]] + [risparmi[0]] + [netti[i] for i in range(1, anni)]
        van = sum(f / (1 + tasso_sconto) ** i for i, f in enumerate(flussi))
    else:
        flussi = netti
        van = sum(f / (1 + tasso_sconto) ** (i + 1) for i, f in enumerate(flussi))

    cum, cumulati, payback = 0.0, [], None
    for i, x in enumerate(netti):
        if payback is None and cum < 0 <= cum + x and x > 0:
            payback = int(round(12 * (i + (-cum) / x)))
        cum += x
        cumulati.append(round(cum, 0))
    return BusinessCaseResult(
        cashflow_annuo_eur=[round(x, 0) for x in netti],
        cashflow_cumulativo_eur=cumulati,
        payback_mesi=payback,
        van_eur=round(van, 0),
        tasso_sconto=tasso_sconto,
        capex_a_t0=capex_a_t0,
    )
