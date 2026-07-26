"""
josa_core.ems.assets — modelli dati per il motore di dispacciamento energetico.

Ogni veicolo e' rappresentato come una risorsa energetica distribuita (DER):
non solo "una batteria", ma una batteria con un vincolo di mobilita' che una
batteria stazionaria non ha (deve raggiungere un SoC minimo entro un orario
di partenza). Questo modulo definisce le strutture dati; la logica di
ottimizzazione vive in dispatch.py.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class VehicleAsset:
    """Un veicolo trattato come asset energetico per l'orizzonte di dispacciamento.

    Tutti i profili temporali (disponibilita', vincoli) sono espressi come liste
    allineate ai timestep dell'orizzonte di ottimizzazione (vedi DispatchHorizon).
    """
    id: str
    capacita_kwh: float
    soc_iniziale_pct: float  # 0-100
    soc_min_pct: float = 10.0   # riserva minima sempre garantita
    soc_max_pct: float = 100.0
    rendimento_carica: float = 0.95
    rendimento_scarica: float = 0.95

    # Vincolo di mobilita': indice del timestep di partenza previsto e SoC minimo richiesto in quel momento.
    # None se il veicolo non ha una partenza prevista nell'orizzonte corrente.
    timestep_partenza: Optional[int] = None
    soc_minimo_alla_partenza_pct: float = 80.0

    # Disponibilita' fisica: True nei timestep in cui il veicolo e' collegato al caricatore
    # (in deposito), False quando e' in movimento/fuori sede.
    disponibile: list = field(default_factory=list)  # list[bool], lunghezza = n. timestep

    # Priorita' relativa (peso nella funzione obiettivo, quanto piu' alto tanto meno
    # il motore tendera' a scaricare questo veicolo rispetto ad altri a parita' di beneficio economico).
    priorita: float = 1.0

    # Probabilita' che il veicolo venga effettivamente utilizzato nell'orizzonte corrente
    # (0-1). Piu' bassa = il motore puo' scaricarlo in modo piu' aggressivo.
    probabilita_utilizzo: float = 1.0

    # Costo di degrado batteria per kWh ciclato in scarica (EUR/kWh). Vedi degradation.py
    # per un modo per stimarlo da costo di sostituzione e cicli di vita attesi.
    costo_degrado_eur_kwh: float = 0.03


@dataclass
class ChargerAsset:
    """Punto di ricarica assegnato a un veicolo per l'orizzonte corrente."""
    vehicle_id: str
    potenza_kw: float
    v2g_capace: bool = False  # se False, la scarica e' vincolata a zero
    tipo: str = "generico"  # es. "AC 22kW", "DC 30kW" — usato per il vincolo di pool
    # condiviso (vedi solve_dispatch, punti_disponibili_per_tipo): veicoli con lo
    # stesso tipo condividono lo stesso numero limitato di punti fisici, invece di
    # assumere che ciascuno abbia sempre un punto dedicato disponibile.


@dataclass
class DispatchHorizon:
    """Orizzonte temporale discretizzato per l'ottimizzazione a orizzonte scorrevole (MPC)."""
    n_timestep: int
    durata_timestep_h: float  # es. 0.25 per intervalli da 15 minuti

    @property
    def durata_totale_h(self) -> float:
        return self.n_timestep * self.durata_timestep_h


@dataclass
class SiteForecast:
    """Previsioni/dati del sito per l'orizzonte di dispacciamento.

    Tutte le liste devono avere lunghezza pari a DispatchHorizon.n_timestep.
    """
    carico_edificio_kw: list       # fabbisogno non EV (uffici, impianti, ecc.)
    produzione_fv_kw: list         # 0 se assente impianto fotovoltaico
    prezzo_acquisto_eur_kwh: list  # prezzo dinamico/ToU in acquisto
    prezzo_vendita_eur_kwh: list   # prezzo di vendita eccedenze/flessibilita' (0 se non applicabile)
    p_rete_max_kw: float           # limite di potenza di rete disponibile (vincolo hard)
    costo_potenza_impegnata_eur_kw: float = 0.0  # costo per il picco massimo nell'orizzonte
