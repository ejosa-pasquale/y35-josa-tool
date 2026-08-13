"""
josa_core — motore di simulazione e dimensionamento fleet charging (JoSa/Y35).

Estratto da main.py (v40) come libreria pura, senza dipendenze da Streamlit.
Nessuna logica di simulazione e' stata riscritta: le funzioni sono identiche
nel comportamento, cambia solo la collocazione e il modo in cui i parametri
prima impliciti (variabili globali dello script Streamlit) sono ora espliciti.
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
try:
    from . import ems
except ImportError:
    ems = None  # ems/assets.py opzionale — non blocca il deploy se manca
from . import business_model
from . import tco

__all__ = [
    "EngineConfig",
    "FuelCosts",
    "SocPolicy",
    "genera_eventi_da_gruppi",
    "genera_timeline_soc_da_gruppi",
    "genera_timeline_soc_multi_day",
    "simulazione",
    "simulazione_soc",
    "optimizer",
    "utils",
    "compliance_dm2025",
    "ems",
    "business_model",
    "tco",
]
