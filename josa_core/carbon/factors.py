"""Libreria dei fattori di emissione con fonte, anno e stato."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Optional

_DEFAULT_PATH = Path(__file__).with_name("factors_it.json")


class FactorLibrary:
    def __init__(self, data: dict):
        self.meta = {k: v for k, v in data.items() if k != "fattori"}
        self._by_id = {f["id"]: dict(f) for f in data["fattori"]}

    @property
    def versione(self) -> str:
        return str(self.meta.get("versione", ""))

    def all(self) -> list[dict]:
        return [copy.deepcopy(f) for f in self._by_id.values()]

    def get(self, factor_id: str) -> dict:
        if factor_id not in self._by_id:
            raise ValueError(f"Fattore di emissione sconosciuto: '{factor_id}'")
        return copy.deepcopy(self._by_id[factor_id])

    def by_category(self, categoria: str) -> list[dict]:
        return [copy.deepcopy(f) for f in self._by_id.values() if f.get("categoria") == categoria]

    def electricity_location(self, anno: Optional[int] = None) -> dict:
        """Fattore rete location-based. Senza anno: il piu' recente con stato
        'definitivo' (i preliminari sono selezionabili solo indicando l'anno)."""
        cands = self.by_category("elettricita_location")
        if anno is not None:
            sel = [f for f in cands if int(f["anno"]) == int(anno)]
            if not sel:
                anni = sorted({int(f["anno"]) for f in cands})
                raise ValueError(f"Nessun fattore elettrico per l'anno {anno}. Anni disponibili: {anni}")
            return sel[0]
        # Un override non cambia quale fattore viene scelto: conta lo stato di libreria.
        defin = [f for f in cands if f.get("stato_originale", f.get("stato")) == "definitivo"]
        return max(defin or cands, key=lambda f: int(f["anno"]))

    def with_overrides(self, overrides: Optional[dict]) -> "FactorLibrary":
        """Copia della libreria con valori sostituiti dall'utente. Ogni override
        resta tracciato (stato='override_utente', valore originale in nota)."""
        if not overrides:
            return self
        lib = FactorLibrary({**self.meta, "fattori": self.all()})
        for fid, ov in overrides.items():
            base = lib._by_id.get(fid)
            if base is None:
                raise ValueError(f"Override per fattore sconosciuto: '{fid}'")
            if isinstance(ov, (int, float)):
                ov = {"valore": float(ov)}
            if "valore" not in ov or float(ov["valore"]) < 0:
                raise ValueError(f"Override non valido per '{fid}': serve 'valore' >= 0")
            orig = base["valore"]
            base["valore"] = float(ov["valore"])
            base["fonte"] = str(ov.get("fonte") or "Valore fornito dall'utente (fonte da documentare)")
            if ov.get("anno"):
                base["anno"] = int(ov["anno"])
            base["stato_originale"] = base.get("stato_originale", base.get("stato"))
            base["stato"] = "override_utente"
            base["note"] = f"Sostituisce il valore di libreria {orig} {base['unita']}. " + str(ov.get("note", ""))
        return lib


def load_default_library() -> FactorLibrary:
    with open(_DEFAULT_PATH, encoding="utf-8") as fh:
        return FactorLibrary(json.load(fh))
