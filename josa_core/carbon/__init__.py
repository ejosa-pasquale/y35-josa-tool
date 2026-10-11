"""Carbon Accounting: quantificazione della riduzione di emissioni GHG ottenuta
con l'elettrificazione della flotta.

Metodologia allineata alla ISO 14064-2 (quantificazione a livello di progetto),
verificabile da terza parte secondo la ISO 14064-3. Vedi docs/carbon_methodology.md.
"""

from .methodology import DICITURA_METODOLOGIA, METODOLOGIA_VERSIONE
from .factors import FactorLibrary, load_default_library
from .quantify import (
    CarbonAssumptions,
    GroupActivity,
    EnergyActivity,
    quantify,
    activities_from_groups,
    energy_from_kpi,
)

__all__ = [
    "DICITURA_METODOLOGIA",
    "METODOLOGIA_VERSIONE",
    "FactorLibrary",
    "load_default_library",
    "CarbonAssumptions",
    "GroupActivity",
    "EnergyActivity",
    "quantify",
    "activities_from_groups",
    "energy_from_kpi",
]
