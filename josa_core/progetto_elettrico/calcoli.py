"""Dimensionamento elettrico dell'infrastruttura di ricarica di una sede.

Parte dai punti di ricarica scelti dal motore Y35 (tipo, potenza, quantita') e
da pochi dati di sito (sistema TT/TN, distanze, posa, Ik) e restituisce:
dorsale dal quadro esistente, quadro di ricarica QG-EV, una linea dedicata per
ogni punto (CEI 64-8 sez. 722), con Ib, In, sezione, Iz, caduta di tensione,
PE, differenziale e verifiche essenziali.

Sono verifiche di progetto di massima: tabelle di portata e resistenze sono
valori di riferimento (CEI-UNEL 35024/1, 35026, IEC 60364-5-52) e il progetto
va firmato da un tecnico abilitato che conferma i dati di sito.
"""
from __future__ import annotations

from math import sqrt
from typing import Any, Optional

TENSIONE_LL = 400.0
TENSIONE_LN = 230.0

# Taglie normalizzate degli interruttori (A)
TAGLIE_IN = [6, 10, 13, 16, 20, 25, 32, 40, 50, 63, 80, 100, 125, 160, 200, 250, 320, 400, 500, 630]

SEZIONI = [2.5, 4, 6, 10, 16, 25, 35, 50, 70, 95, 120, 150, 185, 240]

# Portata Iz (A) cavi in rame isolati in EPR/XLPE 90 °C (FG16), 30 °C aria / 20 °C terreno.
# Valori di riferimento CEI-UNEL 35024/1 e 35026 (IEC 60364-5-52 tab. B.52.3/B.52.5).
# chiave: posa -> conduttori caricati (2 = monofase, 3 = trifase) -> sezione -> Iz
PORTATE: dict[str, dict[int, dict[float, float]]] = {
    # metodo B2: cavo multipolare in tubo/canale a vista su parete
    "a_vista": {
        2: {2.5: 30, 4: 40, 6: 51, 10: 69, 16: 91, 25: 119, 35: 146, 50: 175, 70: 221, 95: 265, 120: 305, 150: 334, 185: 384, 240: 459},
        3: {2.5: 26, 4: 35, 6: 44, 10: 60, 16: 80, 25: 105, 35: 128, 50: 154, 70: 194, 95: 233, 120: 268, 150: 300, 185: 340, 240: 398},
    },
    # metodo D1: cavo multipolare in tubo interrato
    "interrata": {
        2: {2.5: 33, 4: 43, 6: 53, 10: 71, 16: 91, 25: 116, 35: 139, 50: 164, 70: 203, 95: 239, 120: 271, 150: 306, 185: 343, 240: 395},
        3: {2.5: 28, 4: 36, 6: 44, 10: 58, 16: 75, 25: 96, 35: 116, 50: 135, 70: 167, 95: 197, 120: 223, 150: 251, 185: 281, 240: 324},
    },
    # metodo E: cavo multipolare su passerella perforata
    "passerella": {
        2: {2.5: 36, 4: 49, 6: 63, 10: 86, 16: 115, 25: 149, 35: 185, 50: 225, 70: 289, 95: 352, 120: 410, 150: 473, 185: 542, 240: 641},
        3: {2.5: 32, 4: 42, 6: 54, 10: 75, 16: 100, 25: 127, 35: 158, 50: 192, 70: 246, 95: 298, 120: 346, 150: 399, 185: 456, 240: 538},
    },
}

POSE_DESCRIZIONE = {
    "a_vista": "in tubo/canale a vista (metodo B2)",
    "interrata": "in tubo interrato (metodo D1, CEI-UNEL 35026)",
    "passerella": "su passerella perforata (metodo E)",
}

# Resistenza a 20 °C (ohm/km) conduttori flessibili classe 5 (CEI EN 60228) e reattanza indicativa.
R20_CLASSE5 = {2.5: 7.98, 4: 4.95, 6: 3.30, 10: 1.91, 16: 1.21, 25: 0.780, 35: 0.554, 50: 0.386,
               70: 0.272, 95: 0.206, 120: 0.161, 150: 0.129, 185: 0.106, 240: 0.0801}
X_KM = {2.5: 0.075, 4: 0.070, 6: 0.068, 10: 0.065, 16: 0.062, 25: 0.060, 35: 0.058, 50: 0.056,
        70: 0.054, 95: 0.053, 120: 0.052, 150: 0.051, 185: 0.051, 240: 0.050}
# Riporto a 80 °C (temperatura media di esercizio, criterio CEI-UNEL 35023)
FATTORE_R_80C = 1 + 0.00393 * 60

K_EPR = 143  # CEI 64-8 art. 434.3, rame isolato in EPR/XLPE

RENDIMENTO_DC = 0.95  # potenza assorbita in AC da una colonnina DC = potenza DC / rendimento


def _taglia(ib: float) -> int:
    for t in TAGLIE_IN:
        if t >= ib - 1e-9:
            return t
    raise ValueError(f"Corrente {ib:.0f} A oltre le taglie gestite ({TAGLIE_IN[-1]} A): serve un progetto dedicato.")


def corrente_impiego(p_kw: float, fasi: int, cosphi: float) -> float:
    p_w = p_kw * 1000.0
    if fasi == 1:
        return p_w / (TENSIONE_LN * cosphi)
    return p_w / (sqrt(3) * TENSIONE_LL * cosphi)


def caduta_tensione_pct(ib: float, l_m: float, s: float, fasi: int, cosphi: float) -> float:
    sinphi = sqrt(max(0.0, 1 - cosphi ** 2))
    r = R20_CLASSE5[s] * FATTORE_R_80C
    x = X_KM[s]
    term = (r * cosphi + x * sinphi) * l_m / 1000.0
    if fasi == 1:
        return 2 * ib * term / TENSIONE_LN * 100
    return sqrt(3) * ib * term / TENSIONE_LL * 100


def sezione_pe(s: float) -> float:
    """CEI 64-8/5 art. 543.1.2 tab. 54F."""
    if s <= 16:
        return s
    if s <= 35:
        return 16
    return _sezione_sup(s / 2)


def _sezione_sup(v: float) -> float:
    for s in SEZIONI:
        if s >= v:
            return s
    return SEZIONI[-1]


def i2t_passante(in_a: int, ik_ka: float) -> float:
    """Energia specifica passante indicativa (A^2 s).
    Interruttori modulari fino a 63 A: limiti classe 3 (CEI EN 60898-1);
    oltre (scatolati): stima prudente Ik^2 x 10 ms. Da confermare con le curve del costruttore."""
    ik = ik_ka * 1000.0
    if in_a <= 63:
        if in_a <= 16:
            base = 35_000 if ik_ka <= 6 else 70_000
        elif in_a <= 32:
            base = 45_000 if ik_ka <= 6 else 90_000
        else:
            base = 100_000 if ik_ka <= 6 else 160_000
        return base if ik_ka <= 10 else ik * ik * 0.01
    return ik * ik * 0.01


def potere_interruzione(ik_ka: float) -> float:
    for v in (6, 10, 15, 25, 36, 50):
        if v >= ik_ka:
            return v
    return 70


def scegli_sezione(ib: float, in_a: int, l_m: float, fasi: int, cosphi: float, posa: str,
                   k_corr: float, dv_max_pct: float, ik_ka: float, s_min: float = 2.5) -> dict:
    """Sezione minima che rispetta Ib <= In <= Iz, la caduta di tensione e I^2t <= K^2 S^2.
    Se un solo cavo non basta si passa a 2-4 conduttori in parallelo per fase (sezioni >= 50 mm²)."""
    tab = PORTATE[posa][2 if fasi == 1 else 3]
    i2t = i2t_passante(in_a, ik_ka)
    for n_par in (1, 2, 3, 4):
        if n_par > 1 and (fasi == 1 or in_a <= 125):
            break  # conduttori in parallelo solo per correnti elevate
        for s in SEZIONI:
            if s < s_min or (n_par > 1 and s < 50):
                continue
            iz = tab[s] * k_corr * n_par
            dv = caduta_tensione_pct(ib / n_par, l_m, s, fasi, cosphi)
            cc_ok = (K_EPR ** 2) * (s ** 2) >= i2t
            if iz >= in_a and dv <= dv_max_pct and cc_ok:
                return {"sezione_mm2": s, "paralleli": n_par, "iz_a": round(iz, 1), "dv_pct": round(dv, 2),
                        "i2t_passante": round(i2t), "k2s2": round(K_EPR ** 2 * s ** 2)}
    raise ValueError(
        f"Nessuna sezione (fino a {SEZIONI[-1]} mm², in parallelo oltre 125 A) soddisfa portata e caduta di tensione per "
        f"{l_m:.0f} m a {ib:.0f} A: accorcia la linea, alza il limite di caduta o dividi il carico.")


def _formazione(s: float, fasi: int, neutro: bool, cavo: str) -> str:
    pe = sezione_pe(s)
    sz = lambda v: (f"{v:g}").replace(".", ",")
    if fasi == 1:
        return f"{cavo} 3G{sz(s)}"
    nconduttori = 5 if neutro else 4
    if s <= 16:
        return f"{cavo} {nconduttori}G{sz(s)}"
    return f"{cavo} {nconduttori - 1}x{sz(s)} + 1G{sz(pe)} (PE)"


def _tipo_punto(p: dict) -> tuple[bool, int, float]:
    """(ac, fasi, potenza assorbita kW)."""
    tipo = str(p.get("tipo", ""))
    kw = float(p["potenza_kw"])
    ac = p.get("ac")
    if ac is None:
        ac = "DC" not in tipo.upper()
    fasi = p.get("fasi")
    if not fasi:
        fasi = 1 if (ac and kw <= 7.4) else 3
    p_ass = kw if ac else kw / RENDIMENTO_DC
    return bool(ac), int(fasi), p_ass


def dimensiona_impianto(inp: dict[str, Any]) -> dict[str, Any]:
    """Dimensiona dorsale, quadro QG-EV e linee terminali. `inp` e' il dict della richiesta."""
    punti_in = [p for p in (inp.get("punti") or []) if int(p.get("quantita") or 0) > 0]
    if not punti_in:
        raise ValueError("Nessun punto di ricarica da progettare.")
    posa = inp.get("posa") or "interrata"
    if posa not in PORTATE:
        raise ValueError(f"Posa '{posa}' non gestita: usa a_vista, interrata o passerella.")
    cavo = (inp.get("cavo") or "FG16OR16").strip()
    k_corr = float(inp.get("k_correttivo") or 0.8)
    cosphi = float(inp.get("cosphi") or 0.99)
    dv_max = float(inp.get("dv_max_pct") or 4.0)
    ik_ka = float(inp.get("ik_ka") or 6.0)
    sistema = (inp.get("sistema") or "TT").upper()
    rdc_dd = inp.get("rdc_dd_integrato", True)
    l_dorsale = float(inp.get("lunghezza_dorsale_m") or 15)
    icn = potere_interruzione(ik_ka)
    avvisi: list[str] = []

    # ---- espansione dei punti in linee singole
    linee_raw = []
    for p in punti_in:
        ac, fasi, p_ass = _tipo_punto(p)
        q = int(p["quantita"])
        l_min = float(p.get("lunghezza_m") or inp.get("lunghezza_linee_m") or 25)
        l_max = float(p.get("lunghezza_max_m") or l_min)
        for i in range(q):
            l = l_min if q == 1 else l_min + (l_max - l_min) * i / (q - 1)
            linee_raw.append({"tipo": p.get("tipo") or (f"{'AC' if ac else 'DC'} {p['potenza_kw']:g}kW"),
                              "potenza_kw": float(p["potenza_kw"]), "potenza_assorbita_kw": round(p_ass, 2),
                              "ac": ac, "fasi": fasi, "lunghezza_m": round(l, 1)})

    p_installata = sum(l["potenza_assorbita_kw"] for l in linee_raw)

    # ---- gestione dinamica del carico
    p_ev = inp.get("potenza_ev_kw")
    p_pod = inp.get("potenza_disponibile_kw")
    dlm = inp.get("dlm")
    if dlm is None:
        dlm = bool(p_ev and p_ev < p_installata - 1e-6)
    if dlm:
        limite = float(p_ev) if p_ev else p_installata
        limite = min(limite, p_installata)
    else:
        limite = p_installata
    if p_pod and limite > float(p_pod):
        avvisi.append(f"La potenza assegnata alla ricarica ({limite:.0f} kW) supera la potenza disponibile del POD "
                      f"({float(p_pod):.0f} kW): serve un aumento di potenza o un limite DLM più basso.")

    # ---- dorsale (QG esistente -> QG-EV)
    ib_d = corrente_impiego(limite, 3, cosphi)
    in_d = _taglia(ib_d)
    dv_dorsale_max = min(1.5, dv_max / 2)
    sel_d = scegli_sezione(ib_d, in_d, l_dorsale, 3, cosphi, posa, k_corr, dv_dorsale_max, ik_ka, s_min=6)
    dorsale = {
        "id": "D0", "descrizione": "Dorsale dal quadro generale esistente al quadro QG-EV",
        "potenza_kw": round(limite, 1), "fasi": 3, "lunghezza_m": l_dorsale,
        "ib_a": round(ib_d, 1), "in_a": in_d, "poli": 4,
        "protezione": (f"MT 4P C{in_d} A Icn ≥ {icn:g} kA" if in_d <= 63 else f"Interruttore scatolato 4P In {in_d} A Icu ≥ {icn:g} kA")
                      + " + differenziale tipo A selettivo (S) 300 mA",
        "cavo": _formazione(sel_d["sezione_mm2"], 3, True, cavo), "posa": posa, **sel_d,
    }
    if dorsale["paralleli"] > 1:
        sp = sezione_pe(dorsale["sezione_mm2"] * dorsale["paralleli"])
        dorsale["cavo"] = f"{cavo} {dorsale['paralleli']} × 4x(1x{dorsale['sezione_mm2']:g}) + 1x{sp:g} (PE)"
    elif dorsale["sezione_mm2"] > 16:
        dorsale["cavo"] = f"{cavo} 4x(1x{dorsale['sezione_mm2']:g}) + 1x{sezione_pe(dorsale['sezione_mm2']):g} (PE)"

    # ---- linee terminali: una per punto di connessione (CEI 64-8 722.311 / 722.531)
    carichi = {"L1": 0.0, "L2": 0.0, "L3": 0.0}
    rr = 0
    linee = []
    for n, l in enumerate(linee_raw, start=1):
        cos_l = cosphi
        ib = corrente_impiego(l["potenza_assorbita_kw"], l["fasi"], cos_l)
        in_a = _taglia(ib)
        sel = scegli_sezione(ib, in_a, l["lunghezza_m"], l["fasi"], cos_l, posa, k_corr,
                             dv_max - dorsale["dv_pct"], ik_ka)
        poli = 2 if l["fasi"] == 1 else 4
        if l["fasi"] == 1:
            fase = ["L1", "L2", "L3"][rr % 3]
            rr += 1
            carichi[fase] += l["potenza_assorbita_kw"]
        else:
            fase = "L1-L2-L3"
            for f in carichi:
                carichi[f] += l["potenza_assorbita_kw"] / 3
        if l["ac"]:
            tipo_diff = "A" if rdc_dd else "B"
            nota_diff = ("tipo A con rilevazione 6 mA DC (RDC-DD) integrata nella wallbox" if rdc_dd
                         else "tipo B (wallbox senza RDC-DD integrato)")
        else:
            tipo_diff = "B"
            nota_diff = "tipo B, salvo diversa indicazione del costruttore della colonnina DC"
        if in_a <= 63:
            prot = f"MTD {poli}P C{in_a} A Icn ≥ {icn:g} kA, Id 30 mA tipo {tipo_diff}"
        else:
            prot = f"Interruttore scatolato {poli}P In {in_a} A Icu ≥ {icn:g} kA + blocco differenziale 30 mA tipo {tipo_diff}"
        linee.append({
            "id": f"Q1.{n}", "punto": f"EV{n}", "tipo": l["tipo"], "potenza_kw": l["potenza_kw"],
            "potenza_assorbita_kw": l["potenza_assorbita_kw"], "ac": l["ac"], "fasi": l["fasi"], "fase": fase,
            "lunghezza_m": l["lunghezza_m"], "ib_a": round(ib, 1), "in_a": in_a, "poli": poli,
            "protezione": prot, "tipo_differenziale": tipo_diff, "nota_differenziale": nota_diff,
            "cavo": (_formazione(sel["sezione_mm2"], l["fasi"], True, cavo) if sel["paralleli"] == 1 else
                     f"{cavo} {sel['paralleli']} × 4x(1x{sel['sezione_mm2']:g}) + 1x{sezione_pe(sel['sezione_mm2'] * sel['paralleli']):g} (PE)"),
            "posa": posa,
            "pe_mm2": sezione_pe(sel["sezione_mm2"] * sel["paralleli"]),
            **sel, "dv_totale_pct": round(sel["dv_pct"] + dorsale["dv_pct"], 2),
            "esito": "OK",
        })

    # squilibrio tra fasi
    cmax, cmin = max(carichi.values()), min(carichi.values())
    media = sum(carichi.values()) / 3
    squilibrio = 0.0 if media <= 0 else (cmax - cmin) / media * 100
    if squilibrio > 20:
        avvisi.append(f"Squilibrio tra le fasi del {squilibrio:.0f}%: valuta di ruotare le fasi delle wallbox monofase.")

    # ---- quadro QG-EV
    generale = f"Interruttore sezionatore generale 4P {in_d} A" if in_d <= 125 else f"Interruttore scatolato 4P {in_d} A"
    quadro = {
        "nome": "QG-EV", "descrizione": "Quadro di distribuzione della ricarica (CEI EN 61439-1/2)",
        "generale": generale,
        "spd": "SPD tipo 2, 4P (3P+N), Uc 275 V, In 20 kA, Up ≤ 1,5 kV, protetto da MT 4P C32 A" if inp.get("spd", True) else None,
        "dlm": (f"Misuratore di energia con TA sulla dorsale per la gestione dinamica del carico (DLM): "
                f"limite {limite:.0f} kW su {p_installata:.0f} kW installati") if dlm else None,
        "ausiliari": "MT 2P C10 A + diff. 30 mA tipo A per ausiliari (DLM, router di comunicazione)",
        "ip": "IP55 se esterno, IP4X se interno",
        "n_partenze": len(linee),
    }

    # ---- protezione contro i contatti indiretti
    if sistema == "TT":
        ra = inp.get("ra_ohm")
        contatti = {
            "sistema": "TT",
            "ra_max_linee_ohm": round(50 / 0.03),
            "ra_max_dorsale_ohm": round(50 / 0.3),
            "ra_misurata_ohm": ra,
            "esito": None if ra is None else ("OK" if float(ra) * 0.3 <= 50 else "NON OK"),
        }
        if ra is not None and float(ra) * 0.3 > 50:
            avvisi.append(f"Ra = {float(ra):g} Ω: con il differenziale selettivo da 300 mA serve Ra ≤ 166 Ω.")
        if ra is None:
            avvisi.append("Resistenza di terra non indicata: va misurata (serve Ra ≤ 166 Ω con il differenziale di dorsale da 300 mA).")
    else:
        contatti = {"sistema": sistema, "nota": "Interruzione automatica garantita dai differenziali 30 mA su ogni punto (Zs·Idn ≤ U0)."}

    return {
        "dorsale": dorsale,
        "quadro": quadro,
        "linee": linee,
        "carichi_fase_kw": {k: round(v, 1) for k, v in carichi.items()},
        "squilibrio_pct": round(squilibrio, 1),
        "potenza_installata_kw": round(p_installata, 1),
        "potenza_ricarica_kw": round(limite, 1),
        "dlm": bool(dlm),
        "potenza_disponibile_kw": p_pod,
        "contatti_indiretti": contatti,
        "parametri": {"posa": posa, "posa_descrizione": POSE_DESCRIZIONE[posa], "cavo": cavo,
                      "k_correttivo": k_corr, "cosphi": cosphi, "dv_max_pct": dv_max, "ik_ka": ik_ka,
                      "icn_ka": icn, "sistema": sistema, "lunghezza_dorsale_m": l_dorsale},
        "avvisi": avvisi,
        "riepilogo": {
            "punti": len(linee),
            "punti_ac": sum(1 for l in linee if l["ac"]),
            "punti_dc": sum(1 for l in linee if not l["ac"]),
            "dv_max_raggiunta_pct": max(l["dv_totale_pct"] for l in linee),
            "sezioni": sorted({l["sezione_mm2"] for l in linee}),
        },
    }
