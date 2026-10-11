"""Ex-post: quantificazione da dati misurati e import delle sessioni di ricarica.

Il piano di monitoraggio prevede per ogni periodo:
- kWh per fonte (deposito da contatore o CPMS, domestica da wallbox o rimborsi,
  pubblica da fatture/app dei CPO);
- km per gruppo (telematica o odometri);
- litri di benzina dei PHEV (fuel card).
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Optional

from .quantify import CarbonAssumptions, EnergyActivity, GroupActivity, quantify

# Intervallo plausibile di consumo flotta (kWh/km) per i controlli di qualita'.
_KWH_KM_MIN, _KWH_KM_MAX = 0.10, 0.60


def quality_checks(periodi: list[dict], gruppi_attesi: list[str]) -> list[dict]:
    """Controlli QA/QC sui dati di monitoraggio. Ogni esito: livello, periodo, messaggio."""
    out = []
    ordinati = sorted(periodi, key=lambda p: p["periodo_da"])
    for i, p in enumerate(ordinati):
        tag = f"{p['periodo_da']}→{p['periodo_a']}"
        if p["periodo_a"] < p["periodo_da"]:
            out.append({"livello": "errore", "periodo": tag, "messaggio": "Data di fine precedente alla data di inizio."})
        km = sum(float(v) for v in p.get("km_per_gruppo", {}).values())
        kwh = float(p.get("kwh_deposito", 0)) + float(p.get("kwh_domestica", 0)) + float(p.get("kwh_pubblica", 0))
        if km <= 0:
            out.append({"livello": "errore", "periodo": tag, "messaggio": "Nessun km registrato nel periodo."})
        elif kwh > 0:
            r = kwh / km
            if not (_KWH_KM_MIN <= r <= _KWH_KM_MAX):
                out.append({"livello": "avviso", "periodo": tag,
                            "messaggio": f"Consumo medio {r:.3f} kWh/km fuori dall'intervallo plausibile "
                                         f"{_KWH_KM_MIN}-{_KWH_KM_MAX}: verificare km o kWh."})
        mancanti = [g for g in gruppi_attesi if g not in p.get("km_per_gruppo", {})]
        if mancanti:
            out.append({"livello": "avviso", "periodo": tag, "messaggio": f"Km mancanti per i gruppi: {', '.join(mancanti)}."})
        sconosciuti = [g for g in p.get("km_per_gruppo", {}) if g not in gruppi_attesi]
        if sconosciuti:
            out.append({"livello": "errore", "periodo": tag, "messaggio": f"Gruppi non presenti nella flotta: {', '.join(sconosciuti)}."})
        if i > 0:
            prev = ordinati[i - 1]
            if p["periodo_da"] <= prev["periodo_a"]:
                out.append({"livello": "errore", "periodo": tag, "messaggio": "Periodo sovrapposto al precedente."})
            elif (p["periodo_da"] - prev["periodo_a"]).days > 1:
                out.append({"livello": "avviso", "periodo": tag, "messaggio": "Buco temporale rispetto al periodo precedente."})
        if not str(p.get("fonte_dati", "")).strip():
            out.append({"livello": "avviso", "periodo": tag, "messaggio": "Fonte dei dati non indicata."})
    return out


def quantify_ex_post(gruppi, diesel_km_l: float, periodi: list[dict], assumptions: CarbonAssumptions) -> dict:
    """Quantifica ogni periodo monitorato e il totale."""
    if not periodi:
        raise ValueError("Serve almeno un periodo di monitoraggio")
    by_name = {}
    for g in gruppi:
        name = g["gruppo"] if isinstance(g, dict) else g.gruppo
        by_name[name] = g

    def _ga(p: dict) -> list[GroupActivity]:
        acts = []
        litri_phev = float(p.get("litri_benzina_phev", 0) or 0)
        km_phev_tot = 0.0
        phev_names = set()
        for name, km in p.get("km_per_gruppo", {}).items():
            g = by_name[name]
            get = (lambda k: g.get(k)) if isinstance(g, dict) else (lambda k: getattr(g, k, None))
            if get("autonomia_elettrica_km") is not None:
                phev_names.add(name)
                km_phev_tot += float(km)
        for name, km in p.get("km_per_gruppo", {}).items():
            g = by_name[name]
            get = (lambda k: g.get(k)) if isinstance(g, dict) else (lambda k: getattr(g, k, None))
            # I litri PHEV misurati sono per periodo: ripartiti sui gruppi PHEV in proporzione ai km
            litri = None
            if name in phev_names:
                litri = litri_phev * (float(km) / km_phev_tot) if km_phev_tot > 0 else 0.0
            acts.append(GroupActivity(
                gruppo=name, n_veicoli=int(get("n_veicoli") or 0), km=float(km),
                baseline_km_l=float(get("baseline_km_l") or diesel_km_l),
                baseline_combustibile=get("baseline_combustibile"),
                litri_benzina_misurati=litri,
            ))
        return acts

    def _period(p: dict) -> dict:
        giorni = (p["periodo_a"] - p["periodo_da"]).days + 1
        energy = EnergyActivity(
            kwh_deposito=float(p.get("kwh_deposito", 0) or 0),
            kwh_domestica=float(p.get("kwh_domestica", 0) or 0),
            kwh_pubblica=float(p.get("kwh_pubblica", 0) or 0),
        )
        res = quantify(_ga(p), energy, assumptions, tipo="ex_post", giorni_periodo=giorni)
        res["periodo_da"] = p["periodo_da"].isoformat()
        res["periodo_a"] = p["periodo_a"].isoformat()
        res["fonte_dati"] = p.get("fonte_dati", "")
        return res

    risultati = [_period(p) for p in sorted(periodi, key=lambda p: p["periodo_da"])]

    # Totale: un'unica quantificazione sulla somma dei periodi (stessi fattori)
    tot = {
        "periodo_da": min(p["periodo_da"] for p in periodi),
        "periodo_a": max(p["periodo_a"] for p in periodi),
        "kwh_deposito": sum(float(p.get("kwh_deposito", 0) or 0) for p in periodi),
        "kwh_domestica": sum(float(p.get("kwh_domestica", 0) or 0) for p in periodi),
        "kwh_pubblica": sum(float(p.get("kwh_pubblica", 0) or 0) for p in periodi),
        "litri_benzina_phev": sum(float(p.get("litri_benzina_phev", 0) or 0) for p in periodi),
        "km_per_gruppo": {},
        "fonte_dati": "; ".join(sorted({str(p.get("fonte_dati", "")) for p in periodi if p.get("fonte_dati")})),
    }
    for p in periodi:
        for k, v in p.get("km_per_gruppo", {}).items():
            tot["km_per_gruppo"][k] = tot["km_per_gruppo"].get(k, 0.0) + float(v)
    totale = _period(tot)
    giorni_coperti = sum((p["periodo_a"] - p["periodo_da"]).days + 1 for p in periodi)
    totale["giorni_periodo"] = float(giorni_coperti)
    return {
        "periodi": risultati,
        "totale": totale,
        "controlli_qualita": quality_checks(periodi, list(by_name.keys())),
    }


# ---------------- Import sessioni CPMS (OCPP) ----------------

# Alias di colonna accettati (minuscolo, senza spazi). Coprono l'export transazioni
# tipico dei CPMS basati su OCPP 1.6/2.0.1 e un formato semplice "energia_kwh".
_ALIASES = {
    "start": ["starttimestamp", "start_time", "starttime", "start", "inizio", "data_inizio", "timestampstart"],
    "stop": ["stoptimestamp", "stop_time", "stoptime", "stop", "end", "fine", "data_fine", "timestampstop"],
    "meter_start": ["meterstart", "meter_start", "meterstartwh", "contatore_inizio_wh"],
    "meter_stop": ["meterstop", "meter_stop", "meterstopwh", "contatore_fine_wh"],
    "energy_kwh": ["energy_kwh", "energia_kwh", "kwh", "energy", "energiakwh", "consumedkwh"],
    "energy_wh": ["energy_wh", "energia_wh", "wh", "consumedwh"],
    "charge_point": ["chargeboxid", "charge_point_id", "chargepointid", "colonnina", "evse", "evseid", "stazione"],
    "id_tag": ["idtag", "id_tag", "tag", "vehicle", "veicolo", "vehicle_id", "rfid"],
    "tx_id": ["transactionid", "transaction_id", "tx_id", "id_sessione", "session_id", "id"],
}


def _norm(h: str) -> str:
    return "".join(ch for ch in h.strip().lower() if ch.isalnum() or ch == "_")


def _parse_ts(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    for fmt in (None, "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Data/ora non riconosciuta: '{s}'")


def _num(s: str) -> float:
    s = s.strip()
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    elif "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def import_sessions_csv(text: str, periodo_da: Optional[date] = None, periodo_a: Optional[date] = None) -> dict:
    """Legge l'export sessioni di un CPMS e restituisce i kWh di deposito aggregati.

    Energia per sessione: colonna energia (kWh o Wh) se presente, altrimenti
    meterStop - meterStart in Wh (convenzione OCPP). Le sessioni sono attribuite
    al giorno di inizio. Sessioni duplicate (stesso transactionId) contate una volta.
    """
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)
    if not rows:
        raise ValueError("File vuoto")
    header = [_norm(h) for h in rows[0]]
    col = {}
    for key, names in _ALIASES.items():
        for i, h in enumerate(header):
            if h in names:
                col[key] = i
                break
    if "start" not in col:
        raise ValueError("Colonna di inizio sessione non trovata (es. startTimestamp, inizio)")
    has_energy = "energy_kwh" in col or "energy_wh" in col
    has_meter = "meter_start" in col and "meter_stop" in col
    if not (has_energy or has_meter):
        raise ValueError("Serve una colonna energia (energy_kwh / energy_wh) oppure meterStart e meterStop in Wh")

    sessioni = []
    scartate = []
    visti = set()
    for n, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        try:
            start = _parse_ts(r[col["start"]])
            if "energy_kwh" in col and r[col["energy_kwh"]].strip():
                kwh = _num(r[col["energy_kwh"]])
            elif "energy_wh" in col and r[col["energy_wh"]].strip():
                kwh = _num(r[col["energy_wh"]]) / 1000.0
            else:
                kwh = (_num(r[col["meter_stop"]]) - _num(r[col["meter_start"]])) / 1000.0
        except (ValueError, IndexError) as e:
            scartate.append({"riga": n, "motivo": str(e)})
            continue
        if kwh < 0:
            scartate.append({"riga": n, "motivo": "Energia negativa"})
            continue
        if kwh > 500:
            scartate.append({"riga": n, "motivo": f"Energia anomala ({kwh:.1f} kWh in una sessione)"})
            continue
        tx = r[col["tx_id"]].strip() if "tx_id" in col and col["tx_id"] < len(r) else ""
        if tx:
            if tx in visti:
                scartate.append({"riga": n, "motivo": f"Sessione duplicata ({tx})"})
                continue
            visti.add(tx)
        d = start.date()
        if (periodo_da and d < periodo_da) or (periodo_a and d > periodo_a):
            continue
        sessioni.append({
            "data": d, "kwh": kwh,
            "colonnina": r[col["charge_point"]].strip() if "charge_point" in col and col["charge_point"] < len(r) else "",
            "id_tag": r[col["id_tag"]].strip() if "id_tag" in col and col["id_tag"] < len(r) else "",
        })

    per_mese = {}
    per_colonnina = {}
    for s in sessioni:
        m = s["data"].strftime("%Y-%m")
        per_mese[m] = per_mese.get(m, 0.0) + s["kwh"]
        per_colonnina[s["colonnina"] or "n/d"] = per_colonnina.get(s["colonnina"] or "n/d", 0.0) + s["kwh"]
    return {
        "sessioni_valide": len(sessioni),
        "sessioni_scartate": scartate,
        "kwh_deposito_totale": round(sum(s["kwh"] for s in sessioni), 3),
        "periodo_da": min((s["data"] for s in sessioni), default=None),
        "periodo_a": max((s["data"] for s in sessioni), default=None),
        "kwh_per_mese": {k: round(v, 3) for k, v in sorted(per_mese.items())},
        "kwh_per_colonnina": {k: round(v, 3) for k, v in sorted(per_colonnina.items())},
        "colonne_riconosciute": {k: rows[0][i] for k, i in col.items()},
    }
