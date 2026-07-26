"""
Modelli dati espliciti per il motore di simulazione.

Nel main.py originale questi valori vivevano come variabili a livello di modulo
(impostate dai widget Streamlit) e venivano letti implicitamente dentro le
funzioni di simulazione via closure/global lookup. Questo li rende espliciti:
stessa semantica, ma ora la funzione dichiara davvero da cosa dipende.
"""

from dataclasses import dataclass


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
    """
    p_rete: float
    allow_oversizing: bool = False
    dc_fixed_power: bool = True
    dc_redundancy: int = 2


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
