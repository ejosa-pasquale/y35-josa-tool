"""
Modelli dati espliciti per il motore di simulazione.

Nel main.py originale questi valori vivevano come variabili a livello di modulo
(impostate dai widget Streamlit) e venivano letti implicitamente dentro le
funzioni di simulazione via closure/global lookup. Questo li rende espliciti:
stessa semantica, ma ora la funzione dichiara davvero da cosa dipende.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class EngineConfig:
    """Vincoli di rete e comportamento hardware.

    p_rete: potenza di rete disponibile (kW).
    allow_oversizing: se True, la potenza installata puo' eccedere p_rete
        (si assume load management / oversizing prese).
    dc_fixed_power: se True, le colonnine DC erogano sempre alla potenza
        nominale quando attive (non modulabile, "a spunto").
    dc_redundancy: fattore di ridondanza usato nel limite pratico di DC
        installabili a parita' di peak shaving.
    carico_sede_kw: consumo della sede senza la flotta, kW medi per slot di 15 minuti
        a partire da mezzanotte (96 valori = un giorno tipo ripetuto, 672 = una
        settimana da lunedi'). None = sede non nota (comportamento storico).
    potenza_contrattuale_kw: potenza del contratto del POD. Con carico_sede_kw il
        limite della ricarica in ogni slot diventa min(p_shaving, contratto - sede).
    """
    p_rete: float
    allow_oversizing: bool = False
    dc_fixed_power: bool = True
    dc_redundancy: int = 2
    carico_sede_kw: Optional[tuple] = None
    potenza_contrattuale_kw: Optional[float] = None

    def potenza_occupata_sede(self, p_shave_limit: float, n_slots: int) -> list:
        """kW da sottrarre al limite di peak shaving in ogni slot perche' gia' usati
        dalla sede. Tutti zero se la sede non e' nota: il motore resta identico."""
        if not self.carico_sede_kw or self.potenza_contrattuale_kw is None:
            return [0.0] * n_slots
        sede = self.carico_sede_kw
        n = len(sede)
        out = []
        for s in range(n_slots):
            libera = max(0.0, float(self.potenza_contrattuale_kw) - float(sede[s % n]))
            out.append(max(0.0, float(p_shave_limit) - libera))
        return out


@dataclass(frozen=True)
class FuelCosts:
    """Parametri di confronto economico Diesel vs EV.

    km_l: consumo diesel di riferimento (km/L).
    e_l: costo diesel (EUR/L).
    h_rate: costo staff per gestione ricariche esterne (EUR/h).
    """
    km_l: float
    e_l: float
    h_rate: float


@dataclass(frozen=True)
class SocPolicy:
    """Soglie SOC operative, in percentuale (0-100), come nell'UI originale.

    soc_start_pct: SOC target a inizio giornata.
    soc_min_pct: riserva minima operativa.
    soc_max_pct: SOC massimo operativo (limite di carica).
    soc_buffer_pct: margine di sicurezza aggiuntivo sui target diurni.
    """
    soc_start_pct: float
    soc_min_pct: float
    soc_max_pct: float
    soc_buffer_pct: float

    def as_fractions(self) -> dict:
        """Utile per le funzioni che lavorano in frazioni 0-1 (es. genera_timeline_soc_da_gruppi)."""
        return {
            "soc_start": self.soc_start_pct / 100.0,
            "soc_min": self.soc_min_pct / 100.0,
            "soc_max": self.soc_max_pct / 100.0,
            "soc_buffer": self.soc_buffer_pct / 100.0,
        }
