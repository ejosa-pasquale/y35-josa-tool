"""Schema unifilare (tav. E01, A3 orizzontale) del quadro di ricarica QG-EV.

Disegnato in vettoriale con ReportLab a partire dal risultato di
`calcoli.dimensiona_impianto`: punto di consegna, quadro generale esistente con
la protezione della dorsale, quadro QG-EV (generale, SPD, misura DLM,
ausiliari) e una partenza per ogni punto di ricarica, con tabella dati sotto
ogni partenza (sigla, protezione, differenziale, cavo, lunghezza, Ib, ΔV, fase).
"""
from __future__ import annotations

from io import BytesIO
from math import ceil

from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .relazione_pdf import BLU, FONT, FONT_B, GRIGIO, VERDE, info_documento, registra_font

W, H = landscape(A3)
NERO = colors.black
ROSSO = colors.HexColor("#B91C1C")

COL = 22 * mm                 # larghezza di una partenza
X0_TAB = 16 * mm              # inizio tabella partenze (colonna etichette)
X_LABEL_W = 30 * mm
X_PRIMA = X0_TAB + X_LABEL_W  # prima colonna partenze
X_MAX = W - 14 * mm

Y_BARRA = 186 * mm
Y_QEV_TOP = 214 * mm
Y_QEV_BOT = 138 * mm
Y_TAB_TOP = 114 * mm
RIGA = 5.6 * mm


def _num(v, dec=1):
    return f"{v:.{dec}f}".replace(".", ",")


# ----------------------------------------------------------------------------- simboli
def interruttore(c, x, y_top, diff=False, sezionatore=False, lungh=12 * mm):
    """Interruttore (CEI EN 60617): contatto mobile inclinato, croce sul fisso. Ritorna y finale."""
    c.setStrokeColor(NERO)
    c.setLineWidth(0.7)
    y_fisso = y_top - 3 * mm
    y_mob = y_top - lungh + 3 * mm
    c.line(x, y_top, x, y_fisso)
    if sezionatore:
        c.circle(x, y_fisso - 0.6 * mm, 0.6 * mm, stroke=1, fill=0)
    else:
        d = 1.0 * mm
        c.line(x - d, y_fisso - d, x + d, y_fisso + d)
        c.line(x - d, y_fisso + d, x + d, y_fisso - d)
    c.line(x, y_mob, x - 3.2 * mm, y_fisso + 0.2 * mm)
    c.line(x, y_mob, x, y_top - lungh)
    y_end = y_top - lungh
    if diff:
        yd = y_end - 3 * mm
        c.line(x, y_end, x, yd - 3 * mm)
        c.ellipse(x - 4 * mm, yd - 1.6 * mm, x + 4 * mm, yd + 1.6 * mm, stroke=1, fill=0)
        c.line(x + 4 * mm, yd, x + 6 * mm, yd)
        c.rect(x + 6 * mm, yd - 1.2 * mm, 2.4 * mm, 2.4 * mm, stroke=1, fill=0)
        y_end = yd - 3 * mm
    return y_end


def conduttori(c, x, y, n):
    c.setLineWidth(0.5)
    c.line(x - 1.4 * mm, y - 1.2 * mm, x + 1.4 * mm, y + 1.2 * mm)
    c.setFont(FONT, 5.5)
    c.drawString(x + 1.6 * mm, y - 0.6 * mm, str(n))


def terra(c, x, y):
    c.setLineWidth(0.7)
    c.line(x, y, x, y - 2 * mm)
    for i, w in enumerate((3.2, 2.2, 1.2)):
        yy = y - 2 * mm - i * 0.9 * mm
        c.line(x - w / 2 * mm, yy, x + w / 2 * mm, yy)


def spd(c, x, y_top):
    c.setLineWidth(0.7)
    c.line(x, y_top, x, y_top - 2 * mm)
    c.rect(x - 2.5 * mm, y_top - 10 * mm, 5 * mm, 8 * mm, stroke=1, fill=0)
    c.line(x, y_top - 3 * mm, x, y_top - 8.5 * mm)
    c.line(x, y_top - 8.5 * mm, x - 1 * mm, y_top - 7 * mm)
    c.line(x, y_top - 8.5 * mm, x + 1 * mm, y_top - 7 * mm)
    c.line(x, y_top - 10 * mm, x, y_top - 12 * mm)
    terra(c, x, y_top - 12 * mm)


def contatore(c, x, y, testo="kWh"):
    c.setLineWidth(0.7)
    c.circle(x, y, 4.2 * mm, stroke=1, fill=0)
    c.setFont(FONT_B, 5.5)
    c.drawCentredString(x, y - 1 * mm, testo)


def colonnina(c, x, y_top, l):
    w, h = 13 * mm, 15 * mm
    c.setLineWidth(0.8)
    c.setStrokeColor(BLU)
    c.setFillColor(colors.HexColor("#EEF5F8"))
    c.roundRect(x - w / 2, y_top - h, w, h, 1.5 * mm, stroke=1, fill=1)
    c.setFillColor(BLU)
    c.setFont(FONT_B, 7)
    c.drawCentredString(x, y_top - 5 * mm, l["punto"])
    c.setFont(FONT, 5.6)
    c.drawCentredString(x, y_top - 9 * mm, ("AC " if l["ac"] else "DC ") + f"{l['potenza_kw']:g} kW")
    c.drawCentredString(x, y_top - 12.5 * mm, "monofase" if l["fasi"] == 1 else "trifase")
    c.setFillColor(NERO)
    c.setStrokeColor(NERO)


# ----------------------------------------------------------------------------- cornice e cartiglio
def cornice(c, info, foglio, fogli, d):
    c.setStrokeColor(NERO)
    c.setLineWidth(1.0)
    c.rect(8 * mm, 8 * mm, W - 16 * mm, H - 16 * mm)
    # cartiglio
    bw, bh = 170 * mm, 38 * mm
    bx, by = W - 8 * mm - bw, 8 * mm
    c.setLineWidth(0.8)
    c.rect(bx, by, bw, bh)
    c.setFillColor(BLU)
    c.rect(bx, by + bh - 8 * mm, bw, 8 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont(FONT_B, 9)
    c.drawString(bx + 3 * mm, by + bh - 5.6 * mm, "TAV. E01 · SCHEMA UNIFILARE QUADRO DI RICARICA QG-EV")
    c.setFillColor(VERDE)
    c.rect(bx, by + bh - 8.8 * mm, bw, 0.8 * mm, stroke=0, fill=1)
    c.setFillColor(NERO)
    righe = [
        ("Committente", info["committente"] or "—", "Data", info["data"]),
        ("Luogo", info["indirizzo"] or "—", "Revisione", info["revisione"]),
        ("Progetto", info["nome_progetto"] or info["oggetto"], "Cod. progetto", info["cod_progetto"] or "—"),
        ("Progettista", info["progettista_nome"] or "—", "Foglio", f"{foglio} di {fogli}"),
    ]
    rh = (bh - 8.8 * mm) / len(righe)
    for n, (a, b, e, f) in enumerate(righe):
        y = by + bh - 8.8 * mm - (n + 1) * rh
        c.setLineWidth(0.3)
        c.line(bx, y, bx + bw, y) if n < len(righe) - 1 else None
        yt = y + rh * 0.35
        c.setFont(FONT_B, 6.8)
        c.drawString(bx + 2 * mm, yt, a)
        c.drawString(bx + 118 * mm, yt, e)
        c.setFont(FONT, 6.8)
        c.drawString(bx + 22 * mm, yt, str(b)[:58])
        c.drawString(bx + 140 * mm, yt, str(f)[:16])
    c.setLineWidth(0.3)
    c.line(bx + 20 * mm, by, bx + 20 * mm, by + bh - 8.8 * mm)
    c.line(bx + 116 * mm, by, bx + 116 * mm, by + bh - 8.8 * mm)
    c.line(bx + 138 * mm, by, bx + 138 * mm, by + bh - 8.8 * mm)

    # note generali
    p = d["parametri"]
    note = [
        "NOTE",
        f"Sistema {p['sistema']} 230/400 V 50 Hz · Ik presunta {_num(p['ik_ka'], 0)} kA · potere di interruzione ≥ {_num(p['icn_ka'], 0)} kA.",
        f"Cavi {p['cavo']} 0,6/1 kV posa {p['posa_descrizione']}; K = {_num(p['k_correttivo'], 2)}; ΔV max {_num(p['dv_max_pct'], 0)} %.",
        "Una linea e un differenziale 30 mA dedicati per ogni punto di connessione (CEI 64-8 sez. 722).",
        "Fasi delle wallbox monofase ruotate L1/L2/L3. Lunghezze da confermare in sopralluogo.",
        "Schema generato da Y35 sul dimensionamento della flotta: da verificare e firmare a cura del progettista.",
    ]
    c.setFont(FONT_B, 7)
    c.drawString(14 * mm, 40 * mm, note[0])
    c.setFont(FONT, 6.6)
    for i, t in enumerate(note[1:]):
        c.drawString(14 * mm, 35.5 * mm - i * 4.2 * mm, t)


# ----------------------------------------------------------------------------- tabella partenze
ETICHETTE = ["Sigla", "Utenza", "Protezione", "Poli / In", "Differenziale", "Cavo", "Sezione / PE", "L (m)", "Ib (A)", "ΔV tot (%)", "Fase"]


def tabella_partenze(c, colonne):
    """colonne: lista di (x_centro, dict valori per etichetta)."""
    if not colonne:
        return
    x_end = colonne[-1][0] + COL / 2
    y_top = Y_TAB_TOP
    n = len(ETICHETTE)
    c.setLineWidth(0.4)
    c.setStrokeColor(NERO)
    c.rect(X0_TAB, y_top - n * RIGA, x_end - X0_TAB, n * RIGA)
    c.setFillColor(colors.HexColor("#E6F0F4"))
    c.rect(X0_TAB, y_top - n * RIGA, X_LABEL_W, n * RIGA, stroke=0, fill=1)
    c.setFillColor(NERO)
    for i, et in enumerate(ETICHETTE):
        y = y_top - (i + 1) * RIGA
        c.setLineWidth(0.25)
        if i < n - 1:
            c.line(X0_TAB, y, x_end, y)
        c.setFont(FONT_B, 6.2)
        c.drawString(X0_TAB + 1.5 * mm, y + 1.8 * mm, et)
    c.line(X0_TAB + X_LABEL_W, y_top, X0_TAB + X_LABEL_W, y_top - n * RIGA)
    for xc, val in colonne:
        c.setLineWidth(0.25)
        c.line(xc + COL / 2, y_top, xc + COL / 2, y_top - n * RIGA)
        for i, et in enumerate(ETICHETTE):
            y = y_top - (i + 1) * RIGA
            t = str(val.get(et, ""))
            size = 6.0
            while c.stringWidth(t, FONT, size) > COL - 2 * mm and size > 4.2:
                size -= 0.2
            c.setFont(FONT_B if et == "Sigla" else FONT, size)
            c.drawCentredString(xc, y + 1.8 * mm, t)


def _val_linea(l):
    mt = f"MTD C{l['in_a']}" if l["in_a"] <= 63 else f"Scatolato {l['in_a']} A"
    diff = f"30 mA tipo {l['tipo_differenziale']}"
    cavo = l["cavo"].split(" ")[0]
    form = " ".join(l["cavo"].split(" ")[1:])
    return {
        "Sigla": l["id"], "Utenza": f"{l['punto']} {l['tipo']}", "Protezione": mt,
        "Poli / In": f"{l['poli']}P {l['in_a']} A", "Differenziale": diff, "Cavo": cavo, "Sezione / PE": form,
        "L (m)": _num(l["lunghezza_m"], 0), "Ib (A)": _num(l["ib_a"]), "ΔV tot (%)": _num(l["dv_totale_pct"], 2),
        "Fase": l["fase"],
    }


# ----------------------------------------------------------------------------- disegno
def _monte(c, d):
    """Punto di consegna e quadro generale esistente con la protezione della dorsale (in alto a sinistra)."""
    dors = d["dorsale"]
    x = X_PRIMA - 8 * mm
    # POD
    c.setFont(FONT_B, 7)
    c.drawString(x + 7 * mm, 276 * mm, "Punto di consegna (POD)")
    c.setFont(FONT, 6.4)
    pod = d.get("potenza_disponibile_kw")
    c.drawString(x + 7 * mm, 272 * mm, f"Contatore del Distributore{(' · ' + _num(pod, 0) + ' kW') if pod else ''}")
    c.line(x, 281 * mm, x, 277 * mm)
    contatore(c, x, 273 * mm)
    c.line(x, 268.8 * mm, x, 258 * mm)
    # quadro esistente
    c.setDash(3, 2)
    c.setLineWidth(0.6)
    c.rect(x - 22 * mm, 222 * mm, 110 * mm, 36 * mm)
    c.setDash()
    c.setFont(FONT_B, 7)
    c.drawString(x - 20 * mm, 254 * mm, "QUADRO GENERALE ESISTENTE")
    c.setFont(FONT, 6)
    c.drawString(x - 20 * mm, 250.5 * mm, "(non oggetto di intervento, salvo la nuova partenza)")
    c.setLineWidth(0.7)
    c.line(x - 15 * mm, 246 * mm, x + 15 * mm, 246 * mm)  # sbarra esistente
    c.line(x, 258 * mm, x, 246 * mm)
    y = interruttore(c, x, 246 * mm, diff=True)
    c.setFont(FONT_B, 6.6)
    c.drawString(x + 10 * mm, 241 * mm, "Q0 · partenza dorsale QG-EV")
    c.setFont(FONT, 6.2)
    for i, t in enumerate(dors["protezione"].split(" + ")):
        c.drawString(x + 10 * mm, 237.5 * mm - i * 3.4 * mm, t)
    # dorsale
    c.line(x, y, x, Y_QEV_TOP + 4 * mm)
    conduttori(c, x, 218 * mm, 5)
    c.setFont(FONT_B, 6.6)
    c.drawString(x + 4 * mm, 219.5 * mm, f"D0 · {dors['cavo']}")
    c.setFont(FONT, 6.2)
    c.drawString(x + 4 * mm, 216.2 * mm, f"L = {_num(dors['lunghezza_m'], 0)} m · Ib {_num(dors['ib_a'])} A · Iz {_num(dors['iz_a'])} A · ΔV {_num(dors['dv_pct'], 2)} %")
    return x


def _qgev(c, d, x_ing, partenze, foglio, ultima_x):
    q = d["quadro"]
    # involucro
    c.setDash(4, 2)
    c.setLineWidth(0.8)
    c.setStrokeColor(BLU)
    c.rect(X0_TAB, Y_QEV_BOT, ultima_x - X0_TAB + COL / 2, Y_QEV_TOP - Y_QEV_BOT)
    c.setDash()
    c.setStrokeColor(NERO)
    c.setFillColor(BLU)
    c.setFont(FONT_B, 8)
    titolo = f"QUADRO DI RICARICA {q['nome']} · {q['ip']}" + ("" if foglio == 1 else " (segue)")
    c.drawString(X0_TAB + 2 * mm, Y_QEV_TOP - 5 * mm, titolo)
    c.setFillColor(NERO)
    # barra
    x_inizio_barra = X_PRIMA - COL / 2 + 2 * mm
    c.setLineWidth(2.0)
    c.line(x_inizio_barra, Y_BARRA, ultima_x + 4 * mm, Y_BARRA)
    c.setLineWidth(0.7)
    c.setFont(FONT, 6)
    c.drawString(ultima_x - 18 * mm, Y_BARRA + 1.5 * mm, "3F+N+PE 400 V")
    if foglio == 1:
        # generale + TA DLM
        c.line(x_ing, Y_QEV_TOP + 4 * mm, x_ing, Y_QEV_TOP - 7 * mm)
        y = interruttore(c, x_ing, Y_QEV_TOP - 7 * mm, sezionatore=True, lungh=10 * mm)
        c.setFont(FONT_B, 6.4)
        c.drawString(x_ing + 4 * mm, Y_QEV_TOP - 12 * mm, "QG · " + q["generale"].replace("Interruttore sezionatore generale", "Sezionatore generale"))
        if d["dlm"]:
            c.circle(x_ing, y - 2.5 * mm, 1.6 * mm, stroke=1, fill=0)
            c.setFont(FONT, 5.6)
            c.drawString(x_ing + 2.5 * mm, y - 3.4 * mm, "TA misura DLM")
        c.line(x_ing, y, x_ing, Y_BARRA)
    else:
        c.setFont(FONT, 6.4)
        c.drawString(x_inizio_barra, Y_BARRA + 3 * mm, f"← barra dal foglio {foglio - 1}")


def _partenza_ev(c, x, l):
    c.line(x, Y_BARRA, x, Y_BARRA - 3 * mm)
    y = interruttore(c, x, Y_BARRA - 3 * mm, diff=True, lungh=11 * mm)
    c.setFont(FONT_B, 6)
    c.drawString(x + 1.8 * mm, Y_BARRA - 9 * mm, l["id"])
    c.line(x, y, x, Y_QEV_BOT - 4 * mm)
    conduttori(c, x, Y_QEV_BOT + 6 * mm, 3 if l["fasi"] == 1 else 5)
    colonnina(c, x, Y_QEV_BOT - 4 * mm, l)


def _partenze_servizio(c, xs, d):
    """SPD, ausiliari e misura DLM (solo foglio 1). Ritorna colonne per la tabella."""
    q = d["quadro"]
    cols = []
    i = 0
    if q.get("spd"):
        x = xs[i]; i += 1
        c.line(x, Y_BARRA, x, Y_BARRA - 3 * mm)
        y = interruttore(c, x, Y_BARRA - 3 * mm, lungh=10 * mm)
        spd(c, x, y)
        c.setFont(FONT_B, 6)
        c.drawString(x + 1.8 * mm, Y_BARRA - 9 * mm, "QS")
        cols.append((x, {"Sigla": "QS", "Utenza": "SPD tipo 2", "Protezione": "MT C32", "Poli / In": "4P 32 A",
                         "Differenziale": "—", "Cavo": "FS17", "Sezione / PE": "6 / 6", "L (m)": "—", "Ib (A)": "—",
                         "ΔV tot (%)": "—", "Fase": "L1-L2-L3"}))
    x = xs[i]; i += 1
    c.line(x, Y_BARRA, x, Y_BARRA - 3 * mm)
    y = interruttore(c, x, Y_BARRA - 3 * mm, diff=True, lungh=10 * mm)
    c.line(x, y, x, y - 6 * mm)
    c.rect(x - 6 * mm, y - 16 * mm, 12 * mm, 10 * mm, stroke=1, fill=0)
    c.setFont(FONT_B, 5.6)
    c.drawCentredString(x, y - 10 * mm, "DLM" if d["dlm"] else "AUX")
    c.setFont(FONT, 5)
    c.drawCentredString(x, y - 13.5 * mm, "controllore" if d["dlm"] else "ausiliari")
    c.setFont(FONT_B, 6)
    c.drawString(x + 1.8 * mm, Y_BARRA - 9 * mm, "QA")
    if d["dlm"]:
        c.setDash(1.2, 1.2)
        c.setStrokeColor(VERDE)
        c.line(x + 6 * mm, y - 11 * mm, X_MAX - 4 * mm, y - 11 * mm)
        c.setFont(FONT, 5.4)
        c.setFillColor(VERDE)
        c.drawString(x + 8 * mm, y - 14 * mm, f"bus di comunicazione DLM verso le colonnine · limite {_num(d['potenza_ricarica_kw'], 0)} kW")
        c.setFillColor(NERO)
        c.setStrokeColor(NERO)
        c.setDash()
    cols.append((x, {"Sigla": "QA", "Utenza": "Ausiliari / DLM" if d["dlm"] else "Ausiliari", "Protezione": "MTD C10",
                     "Poli / In": "2P 10 A", "Differenziale": "30 mA tipo A", "Cavo": "FS17", "Sezione / PE": "2,5 / 2,5",
                     "L (m)": "—", "Ib (A)": "—", "ΔV tot (%)": "—", "Fase": "L1"}))
    return cols, i


def dati_e_legenda(c, d):
    """Riquadro con i dati del quadro e la legenda dei simboli (in alto a destra)."""
    x0, y0, w, h = 236 * mm, 224 * mm, 170 * mm, 58 * mm
    c.setLineWidth(0.5)
    c.setStrokeColor(colors.HexColor("#9CA3AF"))
    c.rect(x0, y0, w, h)
    c.setStrokeColor(NERO)
    c.setFont(FONT_B, 7.4)
    c.setFillColor(BLU)
    c.drawString(x0 + 3 * mm, y0 + h - 5 * mm, "DATI DEL QUADRO QG-EV")
    c.drawString(x0 + 92 * mm, y0 + h - 5 * mm, "LEGENDA")
    c.setFillColor(NERO)
    r = d["riepilogo"]
    cf = d["carichi_fase_kw"]
    righe = [
        ("Punti di ricarica", f"{r['punti_ac']} AC + {r['punti_dc']} DC"),
        ("Potenza installata", f"{_num(d['potenza_installata_kw'])} kW"),
        ("Potenza ricarica" + (" (limite DLM)" if d["dlm"] else ""), f"{_num(d['potenza_ricarica_kw'])} kW"),
        ("Potenza disponibile POD", f"{_num(d['potenza_disponibile_kw'], 0)} kW" if d.get("potenza_disponibile_kw") else "da confermare"),
        ("Carico L1 / L2 / L3", f"{_num(cf['L1'])} / {_num(cf['L2'])} / {_num(cf['L3'])} kW"),
        ("Squilibrio tra le fasi", f"{_num(d['squilibrio_pct'])} %"),
        ("ΔV massima", f"{_num(r['dv_max_raggiunta_pct'], 2)} % (limite {_num(d['parametri']['dv_max_pct'], 0)} %)"),
    ]
    for i, (a, b) in enumerate(righe):
        y = y0 + h - 11 * mm - i * 6 * mm
        c.setFont(FONT, 6.6)
        c.drawString(x0 + 3 * mm, y, a)
        c.setFont(FONT_B, 6.6)
        c.drawString(x0 + 46 * mm, y, b)
    # legenda
    lx = x0 + 96 * mm
    ly = y0 + h - 12 * mm
    interruttore(c, lx, ly + 3 * mm, lungh=8 * mm)
    c.setFont(FONT, 6.2)
    c.drawString(lx + 6 * mm, ly - 1 * mm, "Interruttore magnetotermico")
    ly -= 15 * mm
    interruttore(c, lx, ly + 6 * mm, diff=True, lungh=6 * mm)
    c.drawString(lx + 10 * mm, ly - 1 * mm, "Magnetotermico differenziale")
    ly -= 13 * mm
    c.line(lx, ly + 3 * mm, lx, ly - 3 * mm)
    conduttori(c, lx, ly, 5)
    c.setFont(FONT, 6.2)
    c.drawString(lx + 6 * mm, ly - 1 * mm, "Linea con n. conduttori (fasi, N, PE)")
    ly -= 9 * mm
    c.circle(lx, ly, 1.6 * mm, stroke=1, fill=0)
    c.drawString(lx + 6 * mm, ly - 1 * mm, "TA di misura per la gestione del carico")


def genera_unifilare_pdf(req: dict, d: dict) -> bytes:
    registra_font()
    info = info_documento(req)
    linee = d["linee"]
    per_riga = int((X_MAX - X_PRIMA) // COL)
    n_servizio = 1 + (1 if d["quadro"].get("spd") else 0)
    primo = per_riga - n_servizio
    resto = max(0, len(linee) - primo)
    fogli = 1 + ceil(resto / per_riga)

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle("Tav. E01 - Schema unifilare QG-EV")
    idx = 0
    for foglio in range(1, fogli + 1):
        cornice(c, info, foglio, fogli, d)
        dati_e_legenda(c, d)
        xs = [X_PRIMA + COL / 2 + i * COL for i in range(per_riga)]
        colonne = []
        k = 0
        if foglio == 1:
            x_ing = _monte(c, d)
            serv, k = _partenze_servizio(c, xs, d)
            colonne += serv
        else:
            x_ing = None
        blocco = linee[idx: idx + (len(xs) - k)]
        for j, l in enumerate(blocco):
            x = xs[k + j]
            _partenza_ev(c, x, l)
            colonne.append((x, _val_linea(l)))
        idx += len(blocco)
        ultima_x = colonne[-1][0] if colonne else X_PRIMA
        _qgev(c, d, x_ing, colonne, foglio, ultima_x)
        tabella_partenze(c, colonne)
        if d.get("avvisi") and foglio == 1:
            c.setFont(FONT_B, 6.6)
            c.setFillColor(ROSSO)
            c.drawString(14 * mm, 46 * mm, "Da verificare: " + d["avvisi"][0][:150])
            c.setFillColor(NERO)
        c.showPage()
    c.save()
    return buf.getvalue()
