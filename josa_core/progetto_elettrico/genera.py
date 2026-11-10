"""Progetto elettrico completo: relazione A4 + tavola unifilare A3 in un unico PDF."""
from __future__ import annotations

from io import BytesIO
from typing import Any

from .calcoli import dimensiona_impianto
from .relazione_pdf import genera_relazione_pdf
from .unifilare_pdf import genera_unifilare_pdf


def genera_progetto_pdf(req: dict[str, Any], solo: str | None = None) -> tuple[bytes, dict]:
    """Ritorna (pdf, dimensionamento). `solo` = 'relazione' | 'unifilare' per un solo documento."""
    from pypdf import PdfReader, PdfWriter

    d = dimensiona_impianto(req)
    if solo == "unifilare":
        return genera_unifilare_pdf(req, d), d
    rel = genera_relazione_pdf(req, d)
    if solo == "relazione":
        return rel, d
    uni = genera_unifilare_pdf(req, d)
    w = PdfWriter()
    for blob in (rel, uni):
        for pg in PdfReader(BytesIO(blob)).pages:
            w.add_page(pg)
    w.add_metadata({"/Title": "Progetto elettrico infrastruttura di ricarica", "/Creator": "Y35"})
    out = BytesIO()
    w.write(out)
    return out.getvalue(), d
