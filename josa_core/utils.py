"""
Funzioni di utilita' pure, estratte identiche da main.py.
"""

from datetime import time

import numpy as np
import pandas as pd


def _coerce_series(x):
    if x is None:
        return []
    if isinstance(x, np.ndarray):
        return x.astype(float).flatten().tolist()
    if isinstance(x, (list, tuple, pd.Series)):
        return list(x)
    try:
        return list(x)
    except Exception:
        return []


def _safe_max_series(x, default=0.0):
    seq = _coerce_series(x)
    if not seq:
        return float(default)
    try:
        return float(max(seq))
    except Exception:
        return float(default)


def _safe_len_series(x):
    return len(_coerce_series(x))


def _time_to_hh(t) -> float:
    if isinstance(t, time):
        return t.hour + t.minute / 60.0
    return 9.0


def _speed_kmh_for_profile(profilo: str) -> float:
    p = (profilo or "").lower()
    if "last" in p:
        return 25.0  # urbano stop&go
    if "sales" in p or "vend" in p:
        return 40.0
    if "office" in p or "uff" in p:
        return 35.0
    if "long" in p:
        return 60.0
    return 35.0


def fmt_eur(x) -> str:
    try:
        return f"€ {float(x):,.0f}"
    except Exception:
        return "—"


def npv(rate: float, cfs) -> float:
    return sum(cf / ((1 + rate) ** t) for t, cf in enumerate(cfs))


def payback_year(cfs):
    cum = 0.0
    for i, cf in enumerate(cfs):
        cum += cf
        if cum >= 0 and i > 0:
            return i
    return None
