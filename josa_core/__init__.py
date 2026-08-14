"""
josa_core — motore di simulazione e dimensionamento fleet charging (JoSa/Y35).
"""

from .models import EngineConfig, FuelCosts, SocPolicy
from .fleet_events import (
    genera_eventi_da_gruppi,
    genera_timeline_soc_da_gruppi,
    genera_timeline_soc_multi_day,
)
from .simulation import simulazione, simulazione_soc
from . import optimizer
from . import utils
from . import compliance_dm2025

# Moduli opzionali — non bloccano il deploy se mancano
try:
    from . import ems
except (ImportError, ModuleNotFoundError):
    ems = None

try:
    from . import business_model
except (ImportError, ModuleNotFoundError):
    business_model = None

try:
    from . import tco
except (ImportError, ModuleNotFoundError):
    tco = None

# Compatibilità con versioni vecchie che importavano dispatch direttamente
try:
    from . import dispatch
except (ImportError, ModuleNotFoundError):
    dispatch = None

try:
    from .assets import VehicleAsset, ChargerAsset, DispatchHorizon, SiteForecast
except (ImportError, ModuleNotFoundError):
    VehicleAsset = ChargerAsset = DispatchHorizon = SiteForecast = None

__all__ = [
    "EngineConfig", "FuelCosts", "SocPolicy",
    "genera_eventi_da_gruppi", "genera_timeline_soc_da_gruppi",
    "genera_timeline_soc_multi_day", "simulazione", "simulazione_soc",
    "optimizer", "utils", "compliance_dm2025",
    "ems", "business_model", "tco",
]
