"""Relazione tecnico-specialistica (A4) dell'impianto di ricarica.

Struttura e testi ripresi dalla libreria RelazioneTecnicaEV (capitoli 1-6,
copertina con cartiglio, revisioni, "Pagina X di Y"), alimentati dai risultati
del dimensionamento automatico di `calcoli.dimensiona_impianto`.
"""
from __future__ import annotations

import base64
import re
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (Flowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

QUI = Path(__file__).parent
BLU = colors.HexColor("#0B5F7D")
VERDE = colors.HexColor("#3AA548")
GRIGIO = colors.HexColor("#6B7280")

FONT = "DejaVu"
FONT_B = "DejaVu-Bold"


def registra_font() -> None:
    if FONT in pdfmetrics.getRegisteredFontNames():
        return
    pdfmetrics.registerFont(TTFont(FONT, str(QUI / "fonts" / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_B, str(QUI / "fonts" / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_B, italic=FONT, boldItalic=FONT_B)


def _testo(nome: str) -> str:
    return (QUI / "testi" / nome).read_text(encoding="utf-8")


def _num(v: float, dec: int = 1) -> str:
    s = f"{v:.{dec}f}".replace(".", ",")
    return s


def _sez(v: float) -> str:
    return f"{v:g}".replace(".", ",")


def _p(text: str, style) -> Paragraph:
    return Paragraph(escape(text or "").replace("\n", "<br/>"), style)


def _vuoto(v: Any) -> bool:
    return v is None or not str(v).strip()


# ----------------------------------------------------------------------------- stili
def _stili():
    ss = getSampleStyleSheet()
    base = ParagraphStyle("base", parent=ss["BodyText"], fontName=FONT, fontSize=9.5, leading=13.2, spaceAfter=4)
    return {
        "body": base,
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=10, textColor=GRIGIO),
        "h1": ParagraphStyle("h1", parent=base, fontName=FONT_B, fontSize=14, leading=18, spaceBefore=4, spaceAfter=10, textColor=BLU),
        "h2": ParagraphStyle("h2", parent=base, fontName=FONT_B, fontSize=12, leading=15, spaceBefore=10, spaceAfter=6, textColor=BLU),
        "h3": ParagraphStyle("h3", parent=base, fontName=FONT_B, fontSize=10, leading=13, spaceBefore=8, spaceAfter=4),
        "th": ParagraphStyle("th", parent=base, fontName=FONT_B, fontSize=7.2, leading=8.6, spaceAfter=0),
        "tc": ParagraphStyle("tc", parent=base, fontSize=7.2, leading=8.6, spaceAfter=0),
        "box": ParagraphStyle("box", parent=base, fontSize=9, leading=12, textColor=colors.HexColor("#1F2937")),
    }


def _tabella(righe, larghezze, header=True, zebra=True):
    t = Table(righe, colWidths=larghezze, repeatRows=1 if header else 0, hAlign="LEFT")
    st = [
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#9CA3AF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    if header:
        st.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E6F0F4")))
    if zebra:
        for i in range(2 if header else 1, len(righe), 2):
            st.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F8FAFB")))
    t.setStyle(TableStyle(st))
    return t


def _kv(rows, s, larghezze=(58 * mm, 116 * mm)):
    return _tabella([[_p(a, s["th"]), _p(str(b), s["tc"])] for a, b in rows], list(larghezze), header=False)


# ----------------------------------------------------------------------------- testo capitolo 3
_RE_TITOLO = re.compile(r"^(\d+\.\d+(?:\.\d+)?)\s+\S")


def _criterio_flowables(d: dict, s) -> list:
    p = d["parametri"]
    cavi = sorted({l["cavo"].split(" ")[0] for l in d["linee"]} | {z["dorsale"]["cavo"].split(" ")[0] for z in d["zone"]})
    testo = (_testo("criterio_progetto.txt")
             .replace("«COSPHI»", _num(p["cosphi"], 2))
             .replace("«DVMAX»", _num(p["dv_max_pct"], 1))
             .replace("«CAVI»", ", ".join(cavi)))
    out, buf = [], []

    def flush():
        if buf:
            out.append(_p("\n".join(buf).strip("\n"), s["body"]))
            buf.clear()

    for riga in testo.split("\n"):
        if _RE_TITOLO.match(riga.strip()) and len(riga) < 90:
            flush()
            out.append(_p(riga.strip(), s["h3"]))
        else:
            buf.append(riga)
    flush()
    return out


# ----------------------------------------------------------------------------- copertina
class Copertina(Flowable):
    """Copertina con cartiglio tecnico e spazio timbro (impostazione RelazioneTecnicaEV)."""

    def __init__(self, info: dict, margini=(18 * mm, 18 * mm)):
        super().__init__()
        self.info = info
        self.margini = margini

    def wrap(self, aw, ah):
        return aw, ah

    def draw(self):
        c = self.canv
        w, h = A4
        c.saveState()
        c.translate(-self.margini[0], -self.margini[1])
        i = self.info
        mx = 18 * mm
        # fascia di testa
        c.setFillColor(BLU)
        c.rect(0, h - 14 * mm, w, 14 * mm, stroke=0, fill=1)
        c.setFillColor(VERDE)
        c.rect(0, h - 15.2 * mm, w, 1.2 * mm, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont(FONT_B, 11)
        c.drawString(mx, h - 9 * mm, "PROGETTO ELETTRICO · INFRASTRUTTURA DI RICARICA")

        c.setFillColor(colors.black)
        c.setFont(FONT_B, 20)
        c.drawCentredString(w / 2, h - 50 * mm, i.get("titolo") or "RELAZIONE TECNICO-SPECIALISTICA")
        c.setFont(FONT_B, 15)
        c.setFillColor(BLU)
        c.drawCentredString(w / 2, h - 62 * mm, (i.get("nome_progetto") or i.get("oggetto") or "")[:70])
        c.setFillColor(colors.black)
        c.setFont(FONT, 11)
        c.drawCentredString(w / 2, h - 71 * mm, "IMPIANTO ELETTRICO PER LA RICARICA DEI VEICOLI ELETTRICI")

        y = h - 90 * mm
        for lab, val in (("Committente:", i.get("committente")), ("Luogo:", i.get("indirizzo")),
                         ("Oggetto:", i.get("oggetto"))):
            c.setFont(FONT_B, 9.5)
            c.drawString(mx, y, lab)
            c.setFont(FONT, 9.5)
            c.drawString(mx + 28 * mm, y, (val or "")[:95])
            y -= 6 * mm
        c.setLineWidth(0.8)
        c.setStrokeColor(BLU)
        c.line(mx, y - 3 * mm, w - mx, y - 3 * mm)

        # sintesi dei numeri chiave
        k = i.get("sintesi") or []
        yy = y - 16 * mm
        bw = (w - 2 * mx - 3 * 4 * mm) / 4
        for n, (val, lab) in enumerate(k[:4]):
            x = mx + n * (bw + 4 * mm)
            c.setFillColor(colors.HexColor("#F1F6F8"))
            c.setStrokeColor(colors.HexColor("#D5E3EA"))
            c.roundRect(x, yy - 18 * mm, bw, 22 * mm, 2 * mm, stroke=1, fill=1)
            c.setFillColor(BLU)
            c.setFont(FONT_B, 15)
            c.drawCentredString(x + bw / 2, yy - 5 * mm, val)
            c.setFillColor(GRIGIO)
            c.setFont(FONT, 7.5)
            c.drawCentredString(x + bw / 2, yy - 12 * mm, lab)

        # cartiglio
        bh, bwid = 52 * mm, 92 * mm
        bx, by = w - mx - bwid, 18 * mm
        c.setStrokeColor(colors.black)
        c.setFillColor(colors.black)
        c.setLineWidth(0.8)
        c.rect(mx, by, 82 * mm, bh)
        c.setFont(FONT_B, 8)
        c.drawString(mx + 3 * mm, by + bh - 5 * mm, "Spazio timbro / firma")
        if i.get("timbro_bytes"):
            try:
                c.drawImage(ImageReader(BytesIO(i["timbro_bytes"])), mx + 3 * mm, by + 3 * mm, 76 * mm, bh - 12 * mm,
                            preserveAspectRatio=True, anchor="c", mask="auto")
            except Exception:
                pass
        c.rect(bx, by, bwid, bh)
        rows = [("Cod. progetto", i.get("cod_progetto")), ("N. documento", i.get("n_doc")),
                ("Revisione", i.get("revisione")), ("Data", i.get("data")),
                ("Progettista", i.get("progettista_nome")), ("Committente", i.get("committente"))]
        rh = bh / len(rows)
        split = bx + 28 * mm
        c.line(split, by, split, by + bh)
        for n, (lab, val) in enumerate(rows):
            if n:
                c.line(bx, by + bh - n * rh, bx + bwid, by + bh - n * rh)
            yt = by + bh - (n + 0.68) * rh
            c.setFont(FONT_B, 7.8)
            c.drawString(bx + 2 * mm, yt, lab)
            c.setFont(FONT, 7.8)
            c.drawString(split + 2 * mm, yt, str(val or "")[:38])
        c.setFont(FONT, 7.2)
        c.setFillColor(GRIGIO)
        c.drawString(mx, by + bh + 6 * mm,
                     "Documento di progetto a supporto della DiCo ex D.M. 37/08. Da firmare a cura del progettista abilitato.")
        c.restoreState()


class _CanvasNumerato(canvas.Canvas):
    """'Pagina X di Y' (stesso schema di RelazioneTecnicaEV)."""

    def __init__(self, *a, **kw):
        self._info = kw.pop("info", {})
        super().__init__(*a, **kw)
        self._stati: list[dict] = []

    def showPage(self):
        self._stati.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        tot = len(self._stati)
        for st in self._stati:
            self.__dict__.update(st)
            if self._pageNumber > 1:
                self._testata(tot)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)

    def _testata(self, tot):
        w, h = A4
        i = self._info
        self.saveState()
        self.setFont(FONT, 7.8)
        self.setFillColor(GRIGIO)
        testa = "Relazione tecnico-specialistica · impianto di ricarica"
        if i.get("cod_progetto"):
            testa += f" · Cod. {i['cod_progetto']}"
        self.drawString(18 * mm, h - 11 * mm, testa)
        self.drawRightString(w - 18 * mm, h - 11 * mm, (i.get("committente") or "")[:60])
        self.setStrokeColor(colors.HexColor("#D1D5DB"))
        self.setLineWidth(0.3)
        self.line(18 * mm, h - 13 * mm, w - 18 * mm, h - 13 * mm)
        self.line(18 * mm, 14 * mm, w - 18 * mm, 14 * mm)
        meta = " · ".join(x for x in (f"Rev. {i['revisione']}" if i.get("revisione") else "",
                                      f"Data {i['data']}" if i.get("data") else "") if x)
        self.drawString(18 * mm, 9.5 * mm, meta)
        self.drawCentredString(w / 2, 9.5 * mm, "Calcoli generati con Y35")
        self.drawRightString(w - 18 * mm, 9.5 * mm, f"Pagina {self._pageNumber} di {tot}")
        self.restoreState()


# ----------------------------------------------------------------------------- documento
def info_documento(req: dict) -> dict:
    oggi = date.today().strftime("%d/%m/%Y")
    timbro = None
    if req.get("timbro_png_base64"):
        try:
            timbro = base64.b64decode(req["timbro_png_base64"].split(",")[-1])
        except Exception:
            timbro = None
    blocco = (req.get("progettista_blocco") or "").strip()
    nome = (req.get("progettista_nome") or (blocco.splitlines()[0] if blocco else "")).strip()
    return {
        "committente": (req.get("committente") or "").strip(),
        "indirizzo": (req.get("indirizzo") or "").strip(),
        "oggetto": (req.get("oggetto") or "Nuova infrastruttura di ricarica per la flotta aziendale").strip(),
        "nome_progetto": (req.get("nome_progetto") or "").strip(),
        "cod_progetto": (req.get("cod_progetto") or "").strip(),
        "n_doc": (req.get("n_doc") or "").strip(),
        "revisione": (req.get("revisione") or "00").strip(),
        "data": (req.get("data") or oggi).strip(),
        "progettista_nome": nome,
        "progettista_blocco": blocco,
        "impresa": (req.get("impresa") or "").strip(),
        "fonte_dati": (req.get("fonte_dati") or "Committente").strip(),
        "luogo_firma": (req.get("luogo_firma") or "").strip(),
        "tipologia": req.get("tipologia") or "Nuova realizzazione",
        "timbro_bytes": timbro,
    }


def genera_relazione_pdf(req: dict, d: dict) -> bytes:
    """req: dati di richiesta (documento + sito); d: risultato di dimensiona_impianto."""
    registra_font()
    s = _stili()
    info = info_documento(req)
    p = d["parametri"]
    r = d["riepilogo"]
    info["sintesi"] = [
        (f"{r['punti_ac']} AC + {r['punti_dc']} DC", "punti di ricarica"),
        (f"{_num(d['potenza_installata_kw'], 0)} kW", "potenza installata"),
        (f"{_num(d['potenza_ricarica_kw'], 0)} kW", "potenza ricarica" + (" (DLM)" if d["dlm"] else "")),
        (f"{_num(r['dv_max_raggiunta_pct'], 1)} %", f"ΔV massima (limite {_num(p['dv_max_pct'], 0)} %)"),
    ]
    luogo = info["indirizzo"] or "—"
    committente = info["committente"] or "—"

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=20 * mm,
                            bottomMargin=20 * mm, title="Relazione tecnico-specialistica impianto di ricarica",
                            author=info["progettista_nome"] or "Y35")
    st: list = [Copertina(info), PageBreak()]

    # ---------------- revisioni e dati identificativi
    st.append(_p("ELENCO DELLE REVISIONI", s["h1"]))
    st.append(_tabella([[_p("Rev.", s["th"]), _p("Data", s["th"]), _p("Descrizione", s["th"])],
                        [_p(info["revisione"], s["tc"]), _p(info["data"], s["tc"]), _p("Emissione documento", s["tc"])]],
                       [18 * mm, 30 * mm, 126 * mm]))
    st.append(Spacer(1, 10))
    pot_pod = d.get("potenza_disponibile_kw")
    st.append(_kv([
        ("Committente", committente), ("Luogo di installazione", luogo), ("Oggetto intervento", info["oggetto"]),
        ("Tipologia impianto", info["tipologia"]), ("Sistema di distribuzione", p["sistema"]),
        ("Tensione / frequenza", "230/400 V - 50 Hz"),
        ("Potenza disponibile (POD)", f"{_num(pot_pod, 0)} kW" if pot_pod else "da confermare"),
        ("Cod. progetto", info["cod_progetto"] or "—"), ("N. documento", info["n_doc"] or "—"),
        ("Revisione", info["revisione"]), ("Data", info["data"]),
    ], s))
    st.append(Spacer(1, 10))
    if info["progettista_blocco"]:
        st.append(_p("TECNICO PROGETTISTA / REDATTORE", s["h2"]))
        st.append(_p(info["progettista_blocco"], s["body"]))
    if info["impresa"]:
        st.append(_p("IMPRESA INSTALLATRICE", s["h2"]))
        st.append(_p(info["impresa"], s["body"]))
    st.append(_p("NOTA", s["h2"]))
    st.append(_p("I calcoli e le verifiche riportati sono di progetto e sono generati a partire dal dimensionamento "
                 "della flotta (numero, tipo e potenza dei punti di ricarica). Lunghezze, condizioni di posa, corrente "
                 "di cortocircuito e resistenza di terra vanno confermati in sopralluogo dal progettista, che firma il "
                 "documento. Le verifiche non sostituiscono quelle strumentali previste dalla CEI 64-8 Parte 6.", s["body"]))
    st.append(PageBreak())

    # ---------------- cap. 1 premessa
    premessa = (_testo("premessa.txt").replace("«COMMITTENTE»", committente).replace("«OGGETTO»", info["oggetto"])
                .replace("«LUOGO»", luogo).replace("«FONTE»", info["fonte_dati"]).replace("«DATA»", info["data"]))
    st.append(_p("CAPITOLO 1 - PREMESSA", s["h2"]))
    st.append(_p(premessa, s["body"]))

    # ---------------- cap. 2 norme
    st.append(_p("CAPITOLO 2 - RIFERIMENTI LEGISLATIVI E NORMATIVI", s["h2"]))
    st.append(_p(
        "Si riportano i principali riferimenti legislativi e normativi applicabili (elenco non esaustivo):\n\n"
        "• D.M. 22/01/2008 n. 37.\n• Legge 01/03/1968 n. 186.\n• D.Lgs. 09/04/2008 n. 81 e s.m.i.\n"
        "• D.P.R. 22/10/2001 n. 462 (ove applicabile).\n"
        "• CEI 64-8 (in particolare Parte 7, Sezione 722 \"Alimentazione dei veicoli elettrici\"), CEI 64-14, "
        "CEI EN 61439-1/2, CEI EN 60529, CEI 0-21 / CEI 0-16, CEI 81-10 (ove pertinente).\n"
        "• CEI EN IEC 61851-1 (sistemi di ricarica conduttiva), CEI EN IEC 62196 (prese e connettori).\n"
        "• CEI-UNEL 35024/1, 35026 (portata dei cavi) e CEI-UNEL 35023 (cadute di tensione).\n"
        "• Regolamento Prodotti da Costruzione (UE) 305/2011 (CPR) e norme CEI-UNEL per i cavi.\n"
        "• Regolamento (UE) 2023/1804 (AFIR) sulle infrastrutture per i combustibili alternativi.\n"
        "• Circolare VV.F. 2/2018 prot. U.0015000 del 05/11/2018 (se l'installazione è in attività soggetta).",
        s["body"]))

    # ---------------- cap. 3 criteri
    st.append(PageBreak())
    st.append(_p("CAPITOLO 3 - CRITERI DI PROGETTO DEGLI IMPIANTI", s["h2"]))
    st.extend(_criterio_flowables(d, s))

    # ---------------- cap. 4 soluzione
    st.append(PageBreak())
    st.append(_p("CAPITOLO 4 - SOLUZIONE PROGETTUALE ADOTTATA", s["h2"]))
    st.append(_p("4.1 Dati tecnici di base", s["h3"]))
    st.append(_kv([
        ("Sistema di distribuzione", p["sistema"]),
        ("Tensione nominale", "400 V trifase / 230 V monofase, 50 Hz"),
        ("Potenza disponibile al POD", f"{_num(pot_pod, 0)} kW" if pot_pod else "da confermare con il Distributore"),
        ("Corrente di cortocircuito presunta", f"{_num(p['ik_ka'], 0)} kA (potere di interruzione scelto ≥ {_num(p['icn_ka'], 0)} kA)"),
        ("Posa delle condutture", p["posa_descrizione"] if len({z["posa"] for z in d["zone"]}) == 1 else
         "; ".join(f"{z['nome']}: {z['posa_descrizione']}" for z in d["zone"])),
        ("Cavo", f"{p['cavo']} 0,6/1 kV, rame, isolamento EPR/HEPR 90 °C, CPR"),
        ("Coefficiente correttivo portata K", f"{_num(p['k_correttivo'], 2)} (temperatura e raggruppamento)"),
        ("Fattore di potenza di progetto", _num(p["cosphi"], 2)),
        ("Caduta di tensione massima", f"{_num(p['dv_max_pct'], 1)} % (dal punto di consegna al punto di ricarica)"),
    ], s))

    zone = d["zone"]
    multi = len(zone) > 1
    th, tc = s["th"], s["tc"]

    st.append(_p("4.2 Descrizione dell'impianto", s["h3"]))
    tipi: dict[str, int] = {}
    for l in d["linee"]:
        tipi[l["tipo"]] = tipi.get(l["tipo"], 0) + 1
    elenco = ", ".join(f"{n} × {t}" for t, n in tipi.items())
    intro = (f"L'intervento prevede la realizzazione di {r['punti']} punti di ricarica ({elenco}) presso {luogo}, "
             f"per una potenza installata complessiva di {_num(d['potenza_installata_kw'], 1)} kW.\n")
    if multi:
        intro += (f"I punti sono distribuiti in {len(zone)} zone, ciascuna alimentata da un proprio quadro di ricarica, "
                  "derivato con una dorsale dedicata dal quadro esistente più vicino:\n")
        intro += "\n".join(f"• {z['nome']}: {z['riepilogo']['punti']} punti, quadro {z['quadro']['nome']} alimentato da "
                           f"{z['quadro_origine']} con dorsale {z['dorsale']['cavo']} di {_num(z['dorsale']['lunghezza_m'], 0)} m."
                           for z in zone) + "\n"
    else:
        z = zone[0]
        intro += (f"Da {z['quadro_origine'].lower() if z['quadro_origine'].startswith('Quadro') else z['quadro_origine']} "
                  f"si deriva, con protezione dedicata ({z['dorsale']['protezione']}), una dorsale {z['dorsale']['cavo']} "
                  f"di {_num(z['dorsale']['lunghezza_m'], 0)} m che alimenta il nuovo quadro di ricarica {z['quadro']['nome']}.\n")
    intro += ("Da ogni quadro di ricarica parte una linea dedicata per ciascun punto di connessione, protetta da un proprio "
              "interruttore magnetotermico e da un proprio differenziale da 30 mA, come richiesto dalla CEI 64-8 Sezione 722.\n"
              "Le wallbox monofase sono distribuite a rotazione sulle tre fasi per contenere lo squilibrio di carico.")
    st.append(_p(intro, s["body"]))
    if multi:
        rows = [[Paragraph(h, th) for h in ("Zona", "Quadro di origine", "Quadro ricarica", "Punti", "Installata<br/>(kW)",
                                            "Limite<br/>(kW)", "Dorsale", "Posa")]]
        for z in zone:
            rows.append([_p(z["nome"], tc), _p(z["quadro_origine"], tc), _p(z["quadro"]["nome"], tc),
                         _p(f"{z['riepilogo']['punti_ac']} AC + {z['riepilogo']['punti_dc']} DC", tc),
                         _p(_num(z["potenza_installata_kw"], 0), tc), _p(_num(z["potenza_ricarica_kw"], 0), tc),
                         _p(f"{z['dorsale']['cavo']} · {_num(z['dorsale']['lunghezza_m'], 0)} m", tc),
                         _p(z["posa"].replace("_", " "), tc)])
        rows.append([_p("Totale sede", th), _p("", tc), _p("", tc), _p(f"{r['punti_ac']} AC + {r['punti_dc']} DC", th),
                     _p(_num(d["potenza_installata_kw"], 0), th), _p(_num(d["potenza_ricarica_kw"], 0), th),
                     _p(f"POD {_num(pot_pod, 0)} kW" if pot_pod else "", tc), _p("", tc)])
        st.append(_tabella(rows, [24 * mm, 30 * mm, 18 * mm, 18 * mm, 16 * mm, 14 * mm, 40 * mm, 14 * mm]))

    st.append(_p("4.3 Gestione dinamica del carico (DLM)", s["h3"]))
    norma_dlm = ("La CEI 64-8 Sezione 722 richiede di considerare ogni punto di connessione alla sua corrente nominale, "
                 "con fattore di contemporaneità pari a 1, salvo la presenza di un sistema di controllo del carico.")
    if d["dlm"]:
        txt = (f"La potenza installata ({_num(d['potenza_installata_kw'], 0)} kW) supera la potenza assegnata alla ricarica "
               f"({_num(d['potenza_ricarica_kw'], 0)} kW). È previsto un sistema di gestione dinamica del carico che misura "
               "la potenza assorbita e modula le colonnine per non superare il limite impostato. " + norma_dlm + " ")
        if multi:
            txt += "Per ogni zona la dorsale e il quadro sono dimensionati sulla potenza che la zona può ricevere:\n"
            txt += "\n".join(f"• {z['nome']} ({z['quadro']['nome']}): " +
                              (f"{_num(z['potenza_ricarica_kw'], 0)} kW" +
                               (" (limite di sede, controllore coordinato)" if z["dlm_di_sede"] else " (limite di zona)")
                               if z["dlm"] else f"{_num(z['potenza_installata_kw'], 0)} kW, senza DLM")
                              for z in zone)
            txt += ("\nCon un limite unico di sede serve un controllore che coordini tutti i quadri; ogni dorsale "
                    "è comunque dimensionata per ricevere l'intero limite.")
        else:
            txt += f"Per questo la dorsale e il quadro sono dimensionati sul limite DLM di {_num(d['potenza_ricarica_kw'], 0)} kW."
        txt += (" Il limite deriva dalla simulazione della giornata di ricarica della flotta (Y35) e va impostato e "
                "piombato nel sistema di gestione in fase di messa in servizio.")
        st.append(_p(txt, s["body"]))
    else:
        st.append(_p("Non è previsto un sistema di gestione dinamica del carico: le dorsali e i quadri sono dimensionati "
                     f"sulla somma delle potenze dei punti ({_num(d['potenza_installata_kw'], 0)} kW). " + norma_dlm, s["body"]))

    st.append(_p("4.4 Quadri di ricarica" if multi else f"4.4 Quadro di ricarica {zone[0]['quadro']['nome']}", s["h3"]))
    for z in zone:
        q = z["quadro"]
        if multi:
            st.append(_p(f"{q['nome']} · {z['nome']}", s["body"]))
        righe_q = [("Norma", "CEI EN 61439-1/2"), ("Alimentazione", f"da {z['quadro_origine']}, dorsale {z['dorsale']['id']}"),
                   ("Grado di protezione", q["ip"]), ("Interruttore generale", q["generale"])]
        if q.get("spd"):
            righe_q.append(("Protezione da sovratensioni", q["spd"]))
        if q.get("dlm"):
            righe_q.append(("Gestione del carico", q["dlm"]))
        righe_q += [("Ausiliari", q["ausiliari"]),
                    ("Partenze", f"{q['n_partenze']} linee dedicate ai punti di ricarica "
                                 f"({z['linee'][0]['id']} … {z['linee'][-1]['id']})")]
        st.append(_kv(righe_q, s))
        st.append(Spacer(1, 6))

    st.append(_p("4.5 Elenco circuiti, cavi e protezioni", s["h3"]))
    hdr = ["Linea", "Utenza", "Fase", "L<br/>(m)", "Ib<br/>(A)", "In<br/>(A)", "Protezione", "Cavo", "Iz<br/>(A)",
           "ΔV<br/>tot %", "Esito"]
    rows = [[Paragraph(h, th) for h in hdr]]
    righe_zona = []
    for z in zone:
        dors = z["dorsale"]
        if multi:
            righe_zona.append(len(rows))
            rows.append([_p(f"{z['nome']} · {z['quadro']['nome']}", th)] + [""] * 10)
        rows.append([_p(dors["id"], tc), _p(f"Dorsale → {z['quadro']['nome']}", tc), _p("L1-L2-L3", tc),
                     _p(_num(dors["lunghezza_m"], 0), tc), _p(_num(dors["ib_a"]), tc), _p(str(dors["in_a"]), tc),
                     _p(dors["protezione"], tc), _p(dors["cavo"], tc), _p(_num(dors["iz_a"]), tc),
                     _p(_num(dors["dv_pct"], 2), tc), _p("OK", tc)])
        for l in z["linee"]:
            rows.append([_p(l["id"], tc), _p(f"{l['punto']} · {l['tipo']}", tc), _p(l["fase"], tc), _p(_num(l["lunghezza_m"], 0), tc),
                         _p(_num(l["ib_a"]), tc), _p(str(l["in_a"]), tc), _p(l["protezione"], tc), _p(l["cavo"], tc),
                         _p(_num(l["iz_a"]), tc), _p(_num(l["dv_totale_pct"], 2), tc), _p(l["esito"], tc)])
    tab = _tabella(rows, [11 * mm, 25 * mm, 14 * mm, 8 * mm, 10 * mm, 9 * mm, 36 * mm, 30 * mm, 10 * mm, 11 * mm, 10 * mm])
    for i in righe_zona:
        tab.setStyle(TableStyle([("SPAN", (0, i), (-1, i)), ("BACKGROUND", (0, i), (-1, i), colors.HexColor("#EEF5E9"))]))
    st.append(tab)
    st.append(Spacer(1, 3))
    st.append(_p(f"Iz = portata tabellare × K ({_num(p['k_correttivo'], 2)}). Condizioni verificate per ogni linea: "
                 "Ib ≤ In ≤ Iz (art. 433.2), If ≤ 1,45·Iz garantita dagli interruttori CEI EN 60898/60947-2, "
                 "ΔV totale ≤ limite di progetto, I²t ≤ K²S² (art. 434.3). ΔV tot comprende la caduta sulla dorsale.",
                 s["small"]))

    st.append(_p("4.6 Verifica di cortocircuito (I²t ≤ K²S²)", s["h3"]))
    rows = [[Paragraph(h, th) for h in ("Linea", "Sezione (mm²)", "In (A)", "I²t passante (A²s)", "K²S² (A²s)", "Esito")]]
    viste = set()
    for l in [z["dorsale"] for z in zone] + d["linee"]:
        dorsale = l["id"].startswith("D")
        chiave = (l["sezione_mm2"], l["in_a"], dorsale and l["id"])
        if chiave in viste:
            continue
        viste.add(chiave)
        rows.append([_p(l["id"] if dorsale else f"{l['id']} e analoghe", tc), _p(_sez(l["sezione_mm2"]), tc),
                     _p(str(l["in_a"]), tc), _p(f"{l['i2t_passante']:,}".replace(",", "."), tc),
                     _p(f"{l['k2s2']:,}".replace(",", "."), tc), _p("OK" if l["k2s2"] >= l["i2t_passante"] else "NO", tc)])
    st.append(_tabella(rows, [34 * mm, 24 * mm, 18 * mm, 36 * mm, 36 * mm, 18 * mm]))
    st.append(_p(f"K = 143 (rame isolato in EPR). I²t passante indicativo per Ik = {_num(p['ik_ka'], 0)} kA: interruttori "
                 "modulari in classe di limitazione 3 (CEI EN 60898-1); per gli scatolati stima prudente Ik² × 10 ms. "
                 "Da confermare con le curve del costruttore scelto e con la Ik nel punto di origine di ciascuna zona.",
                 s["small"]))

    st.append(_p("4.7 Bilanciamento delle fasi", s["h3"]))
    righe_f = []
    for z in zone:
        cf = z["carichi_fase_kw"]
        righe_f.append((f"{z['quadro']['nome']} · L1 / L2 / L3",
                        f"{_num(cf['L1'])} / {_num(cf['L2'])} / {_num(cf['L3'])} kW (squilibrio {_num(z['squilibrio_pct'])} %)"))
    st.append(_kv(righe_f, s))

    # ---------------- cap. 5
    st.append(PageBreak())
    st.append(_p("CAPITOLO 5 - ULTERIORI INDICAZIONI", s["h2"]))
    st.append(_p("5.1 Protezione contro i contatti diretti e indiretti", s["h3"]))
    ci = d["contatti_indiretti"]
    tipi_diff = sorted({l["nota_differenziale"] for l in d["linee"]})
    if ci["sistema"] == "TT":
        ra = ci.get("ra_misurata_ohm")
        txt_ra = (f"Resistenza di terra misurata/indicata: Ra = {_num(float(ra), 1)} Ω → esito {ci['esito']}."
                  if ra is not None else "La resistenza di terra Ra va misurata prima della messa in servizio.")
        st.append(_p(
            "La protezione contro i contatti diretti è assicurata dall'isolamento delle parti attive e da involucri con "
            "grado di protezione almeno IPXXB (IPXXD sulle superfici orizzontali a portata di mano).\n"
            "La protezione contro i contatti indiretti nel sistema TT è assicurata dall'interruzione automatica "
            "dell'alimentazione coordinata con l'impianto di terra: Ra · Idn ≤ 50 V.\n"
            f"• Linee terminali, Idn = 30 mA: Ra ≤ {ci['ra_max_linee_ohm']} Ω.\n"
            f"• Dorsale, differenziale selettivo Idn = 300 mA: Ra ≤ {ci['ra_max_dorsale_ohm']} Ω (condizione vincolante).\n"
            f"{txt_ra}\n"
            "Differenziali sui punti di ricarica: " + "; ".join(tipi_diff) + ".", s["body"]))
    else:
        st.append(_p(
            "La protezione contro i contatti diretti è assicurata dall'isolamento delle parti attive e da involucri con "
            "grado di protezione almeno IPXXB.\n"
            f"Sistema {ci['sistema']}: {ci['nota']}\nDifferenziali sui punti di ricarica: " + "; ".join(tipi_diff) + ".",
            s["body"]))
    st.append(_p("5.2 Protezione contro le sovratensioni", s["h3"]))
    spd_q = d["zone"][0]["quadro"].get("spd")
    st.append(_p(((("In ogni quadro di ricarica: " if len(d["zone"]) > 1 else "") + spd_q + ". ") if spd_q else "Non previsto SPD. ") +
                 "La necessità di SPD è confermata dalla valutazione del rischio secondo CEI 64-8 Sezione 443 e, ove "
                 "pertinente, CEI EN 62305-2 (CEI 81-10).", s["body"]))
    st.append(_p("5.3 Verifiche, prove e collaudi", s["h3"]))
    st.append(_p(
        "Ad ultimazione dei lavori l'impianto è sottoposto alle verifiche previste dalla CEI 64-8 Parte 6 e dalla "
        "CEI 64-14, con registrazione delle prove strumentali:\n"
        "• esame a vista (targhe, identificazione circuiti, gradi IP, sigillature);\n"
        "• continuità dei conduttori di protezione ed equipotenziali;\n"
        "• resistenza di isolamento;\n"
        "• prova dei differenziali (Idn e tempo di intervento) su ogni punto di ricarica;\n"
        "• misura della resistenza di terra Ra (sistema TT) o dell'impedenza dell'anello di guasto Zs (sistema TN);\n"
        "• sequenza delle fasi e prova funzionale di ricarica con simulatore di veicolo (CEI EN IEC 61851-1);\n"
        "• prova della gestione dinamica del carico (DLM): rispetto del limite impostato con più punti in ricarica.",
        s["body"]))
    st.append(_p("5.4 Esercizio, manutenzione e avvertenze", s["h3"]))
    st.append(_p(_testo("manutenzione.txt"), s["body"]))

    # ---------------- cap. 6
    st.append(_p("CAPITOLO 6 - ALLEGATI", s["h2"]))
    st.append(_p("Costituiscono parte integrante della presente relazione:\n"
                 + "".join(f"• Tav. E{z['n']:02d} - Schema unifilare del quadro {z['quadro']['nome']}"
                           + (f" ({z['nome']})" if len(d['zone']) > 1 else "") + " e della sua dorsale (allegata).\n"
                           for z in d["zone"]) +
                 "• Tabella circuiti, cavi e protezioni (paragrafo 4.5).\n"
                 "Da completare a cura dell'Impresa installatrice: verbali delle prove strumentali, schede tecniche dei "
                 "componenti e dei punti di ricarica, dichiarazioni CE, report fotografico.", s["body"]))
    if d.get("avvisi"):
        st.append(_p("Punti da verificare in sopralluogo", s["h3"]))
        st.append(_p("\n".join("• " + a for a in d["avvisi"]), s["body"]))

    firma = [Spacer(1, 16)]
    luogo_f = info["luogo_firma"]
    firma.append(_p(f"Luogo e data: {luogo_f + ', ' if luogo_f else ''}{info['data']}", s["body"]))
    firma.append(Spacer(1, 6))
    firma.append(_p(f"Il progettista: {info['progettista_nome'] or '______________________'}", s["body"]))
    firma.append(_p("(timbro e firma)", s["small"]))
    st.append(KeepTogether(firma))

    doc.build(st, canvasmaker=lambda *a, **kw: _CanvasNumerato(*a, info=info, **kw))
    return buf.getvalue()
