"""Quantificazione della riduzione di emissioni: ER = BE - PE - LE.

- BE (baseline): la stessa flotta, stessi km, con veicoli a combustione.
- PE (progetto): elettricita' di ricarica per fonte (deposito, casa, pubblica)
  piu' la benzina dei PHEV oltre l'autonomia elettrica.
- LE (leakage): dichiarato dall'utente, default 0.

Tutte le funzioni sono pure: ricevono attivita' gia' aggregate (km, kWh, litri)
e restituiscono un dict con breakdown, fattori usati e confine di progetto.
L'ex-ante passa dai KPI della simulazione (energy_from_kpi), l'ex-post dai dati
misurati (monitoring.py): il calcolo e' lo stesso.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from .factors import FactorLibrary, load_default_library
from .methodology import DICITURA_METODOLOGIA, METODOLOGIA_VERSIONE, project_boundary

_PENDOLARE = ("pendolare", "pendolare aziendale", "commuter")


@dataclass
class CarbonAssumptions:
    anno_fattori: Optional[int] = None
    baseline_combustibile: str = "diesel"          # diesel | benzina
    metodo_elettricita: str = "location"           # location | market
    quota_rinnovabile_deposito_pct: float = 0.0    # GO/PPA documentate, solo market-based
    fe_residual_mix_kg_kwh: Optional[float] = None # obbligatorio con metodo market
    fv_autoconsumo_kwh_anno: float = 0.0           # FV in sito consumato dalla ricarica
    profilo_orario_kg_kwh: Optional[list] = None   # 24 valori, applicati al prelievo di deposito
    includi_well_to_tank: bool = False
    fe_upstream_elettricita_kg_kwh: Optional[float] = None  # obbligatorio con WTT
    quota_biocarburante_pct: float = 0.0           # quota biogenica (energia) del carburante di baseline
    leakage_tco2e_anno: float = 0.0
    incertezza_pct: float = 0.0
    giorni_operativi_anno: int = 365
    orizzonte_anni: int = 10
    anno_inizio: int = 2026
    prezzo_carbonio_modalita: str = "nessuno"      # nessuno | ombra | ets2 | libero
    prezzo_carbonio_eur_t: float = 0.0
    anno_inizio_ets2: int = 2028
    fattori_override: Optional[dict] = None

    def validate(self) -> None:
        if self.baseline_combustibile not in ("diesel", "benzina"):
            raise ValueError("baseline_combustibile deve essere 'diesel' o 'benzina'")
        if self.metodo_elettricita not in ("location", "market"):
            raise ValueError("metodo_elettricita deve essere 'location' o 'market'")
        if self.metodo_elettricita == "market" and self.fe_residual_mix_kg_kwh is None:
            raise ValueError("Con metodo market-based serve fe_residual_mix_kg_kwh (residual mix AIB dell'anno)")
        if self.includi_well_to_tank and self.fe_upstream_elettricita_kg_kwh is None:
            raise ValueError("Con includi_well_to_tank serve fe_upstream_elettricita_kg_kwh, "
                             "altrimenti la baseline includerebbe l'upstream e il progetto no (non conservativo)")
        if self.profilo_orario_kg_kwh is not None and len(self.profilo_orario_kg_kwh) != 24:
            raise ValueError("profilo_orario_kg_kwh deve avere 24 valori (uno per ora)")
        if self.prezzo_carbonio_modalita not in ("nessuno", "ombra", "ets2", "libero"):
            raise ValueError("prezzo_carbonio_modalita deve essere nessuno, ombra, ets2 o libero")
        if not 0 <= self.incertezza_pct <= 50:
            raise ValueError("incertezza_pct deve essere tra 0 e 50")
        if not 0 <= self.quota_rinnovabile_deposito_pct <= 100:
            raise ValueError("quota_rinnovabile_deposito_pct deve essere tra 0 e 100")
        if not 0 <= self.quota_biocarburante_pct <= 100:
            raise ValueError("quota_biocarburante_pct deve essere tra 0 e 100")


@dataclass
class GroupActivity:
    """Attivita' di un gruppo flotta su un periodo (giorno tipo o periodo monitorato)."""
    gruppo: str
    n_veicoli: int
    km: float                       # km totali del gruppo nel periodo
    km_termici_phev: float = 0.0    # quota dei km percorsa a benzina (solo PHEV)
    l100km_benzina_phev: float = 0.0
    baseline_km_l: float = 10.0
    baseline_combustibile: Optional[str] = None  # None = quello delle ipotesi
    litri_benzina_misurati: Optional[float] = None  # ex-post: litri da fuel card


@dataclass
class EnergyActivity:
    """kWh di ricarica per fonte nel periodo."""
    kwh_deposito: float = 0.0
    kwh_domestica: float = 0.0
    kwh_pubblica: float = 0.0
    profilo_deposito_kw: Optional[list] = None  # curva a 15 min (96 o piu' valori) per il profilo orario


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def activities_from_groups(gruppi, diesel_km_l: float) -> list[GroupActivity]:
    """Km per giorno tipo da gruppi flotta (pydantic FleetGroup o dict).

    Rispetta le stesse regole del generatore eventi (fleet_events):
    - i gruppi pendolari fanno un solo giro al giorno;
    - probabilita_utilizzo_pct riduce i km attesi (pool car);
    - per i PHEV i km oltre autonomia_elettrica_km per giro sono a benzina.
    """
    out = []
    for g in gruppi:
        n = int(_get(g, "n_veicoli", 0) or 0)
        if n <= 0:
            continue
        km_giro = float(_get(g, "km_per_giro", 0) or 0)
        profilo = str(_get(g, "profilo", "") or "").strip().lower()
        giri = 1.0 if profilo in _PENDOLARE else float(_get(g, "giri_per_veicolo_giorno", 0) or 0)
        prob = _get(g, "probabilita_utilizzo_pct", None)
        prob = 1.0 if prob is None else max(0.0, min(1.0, float(prob) / 100.0))
        km = n * km_giro * giri * prob

        aut = _get(g, "autonomia_elettrica_km", None)
        km_term = 0.0
        l100 = 0.0
        if aut is not None:
            km_term = n * max(0.0, km_giro - min(km_giro, float(aut))) * giri * prob
            l100 = _get(g, "consumo_benzina_l100km", None)
            l100 = float(l100) if l100 is not None else 100.0 / float(diesel_km_l)

        base_kml = _get(g, "baseline_km_l", None)
        out.append(GroupActivity(
            gruppo=str(_get(g, "gruppo", "")),
            n_veicoli=n,
            km=km,
            km_termici_phev=km_term,
            l100km_benzina_phev=l100,
            baseline_km_l=float(base_kml) if base_kml else float(diesel_km_l),
            baseline_combustibile=_get(g, "baseline_combustibile", None),
        ))
    return out


def energy_from_kpi(kpi: dict, timeline_p_kw: Optional[list] = None) -> EnergyActivity:
    """kWh/giorno per fonte dai KPI della simulazione.

    L'energia non servita (e_unserved) viene attribuita alla ricarica pubblica:
    i km della baseline restano gli stessi, quindi quell'energia va comunque
    prelevata da qualche parte (scelta conservativa).
    """
    return EnergyActivity(
        kwh_deposito=float(kpi.get("e_int", 0.0) or 0.0),
        kwh_domestica=float(kpi.get("e_home_private", 0.0) or 0.0),
        kwh_pubblica=float(kpi.get("e_ext", 0.0) or 0.0) + float(kpi.get("e_unserved", 0.0) or 0.0),
        profilo_deposito_kw=[float(x) for x in timeline_p_kw] if timeline_p_kw is not None and len(timeline_p_kw) > 0 else None,
    )


def _hourly_weighted_factor(profile_kw: list, hourly_kg_kwh: list) -> Optional[float]:
    """Fattore medio pesato sulla curva di prelievo del deposito."""
    if not profile_kw:
        return None
    slots_per_h = max(1, len(profile_kw) // 24) if len(profile_kw) >= 24 else 1
    num = den = 0.0
    for i, p in enumerate(profile_kw):
        h = (i // slots_per_h) % 24
        num += float(p) * float(hourly_kg_kwh[h])
        den += float(p)
    return num / den if den > 0 else None


def quantify(
    groups: list[GroupActivity],
    energy: EnergyActivity,
    assumptions: CarbonAssumptions,
    library: Optional[FactorLibrary] = None,
    tipo: str = "ex_ante",
    giorni_periodo: Optional[float] = None,
) -> dict:
    """Quantifica baseline, progetto e riduzione.

    tipo='ex_ante': groups ed energy sono per giorno tipo, annualizzati con
    giorni_operativi_anno. tipo='ex_post': sono totali del periodo misurato
    (giorni_periodo), riportati anche su base annua per confronto.
    """
    a = assumptions
    a.validate()
    lib = (library or load_default_library()).with_overrides(a.fattori_override)

    if tipo == "ex_ante":
        mult = float(a.giorni_operativi_anno)
    else:
        if not giorni_periodo or giorni_periodo <= 0:
            raise ValueError("giorni_periodo obbligatorio per l'ex-post")
        mult = 1.0  # totali del periodo

    used: dict[str, dict] = {}

    def use(f: dict) -> float:
        used[f["id"]] = f
        return float(f["valore"])

    fe_fuel = {
        "diesel": lambda: use(lib.get("gasolio_combustione")),
        "benzina": lambda: use(lib.get("benzina_combustione")),
    }
    fe_wtt = {
        "diesel": lambda: use(lib.get("gasolio_wtt")),
        "benzina": lambda: use(lib.get("benzina_wtt")),
    }
    fossil_share = 1.0 - a.quota_biocarburante_pct / 100.0

    breakdown = []
    per_gruppo = []
    be_comb_kg = 0.0
    be_wtt_kg = 0.0
    pe_phev_kg = 0.0
    pe_phev_wtt_kg = 0.0
    km_tot = 0.0

    # ---- Baseline ----
    for g in groups:
        comb = g.baseline_combustibile or a.baseline_combustibile
        litri = g.km / g.baseline_km_l * mult if g.baseline_km_l > 0 else 0.0
        f_id = "gasolio_combustione" if comb == "diesel" else "benzina_combustione"
        kg = litri * fe_fuel[comb]() * fossil_share
        be_comb_kg += kg
        km_tot += g.km * mult
        breakdown.append({
            "scenario": "baseline", "voce": f"Combustione {comb} – {g.gruppo}",
            "quantita": round(litri, 1), "unita": "L", "fattore_id": f_id,
            "tco2e": round(kg / 1000.0, 4),
        })
        g_wtt = 0.0
        if a.includi_well_to_tank:
            g_wtt = litri * fe_wtt[comb]()
            be_wtt_kg += g_wtt
            breakdown.append({
                "scenario": "baseline", "voce": f"Well-to-tank {comb} – {g.gruppo}",
                "quantita": round(litri, 1), "unita": "L",
                "fattore_id": "gasolio_wtt" if comb == "diesel" else "benzina_wtt",
                "tco2e": round(g_wtt / 1000.0, 4),
            })

        # PHEV: benzina oltre l'autonomia elettrica
        g_phev = 0.0
        if g.litri_benzina_misurati is not None or g.km_termici_phev > 0:
            litri_b = (g.litri_benzina_misurati if g.litri_benzina_misurati is not None
                       else g.km_termici_phev * g.l100km_benzina_phev / 100.0 * mult)
            if litri_b > 0:
                g_phev = litri_b * fe_fuel["benzina"]()
                pe_phev_kg += g_phev
                breakdown.append({
                    "scenario": "progetto", "voce": f"Benzina PHEV oltre autonomia – {g.gruppo}",
                    "quantita": round(litri_b, 1), "unita": "L", "fattore_id": "benzina_combustione",
                    "tco2e": round(g_phev / 1000.0, 4),
                })
                if a.includi_well_to_tank:
                    w = litri_b * fe_wtt["benzina"]()
                    pe_phev_wtt_kg += w
                    breakdown.append({
                        "scenario": "progetto", "voce": f"Well-to-tank benzina PHEV – {g.gruppo}",
                        "quantita": round(litri_b, 1), "unita": "L", "fattore_id": "benzina_wtt",
                        "tco2e": round(w / 1000.0, 4),
                    })
        per_gruppo.append({
            "gruppo": g.gruppo, "n_veicoli": g.n_veicoli,
            "km_periodo": round(g.km * mult, 1),
            "baseline_tco2e": round((kg + g_wtt) / 1000.0, 3),
            "phev_benzina_tco2e": round(g_phev / 1000.0, 3),
        })

    # ---- Progetto: elettricita' ----
    f_loc = lib.electricity_location(a.anno_fattori)
    fe_loc = use(f_loc)
    avvertenze = []

    kwh_dep = energy.kwh_deposito * mult
    kwh_home = energy.kwh_domestica * mult
    kwh_pub = energy.kwh_pubblica * mult
    fv_periodo = a.fv_autoconsumo_kwh_anno * (mult / 365.0 if tipo == "ex_ante" else giorni_periodo / 365.0)
    kwh_fv = min(kwh_dep, max(0.0, fv_periodo))
    if fv_periodo > kwh_dep + 1e-9:
        avvertenze.append("Il fotovoltaico dichiarato supera l'energia caricata in deposito: conteggiato solo fino al prelievo del deposito.")
    kwh_dep_rete = kwh_dep - kwh_fv

    if a.metodo_elettricita == "market":
        fe_grid_other = float(a.fe_residual_mix_kg_kwh)
        id_grid_other = "residual_mix_utente"
        used[id_grid_other] = {
            "id": id_grid_other, "categoria": "elettricita_market", "valore": fe_grid_other,
            "unita": "kgCO2/kWh", "gas": "CO2", "anno": a.anno_fattori or 0, "stato": "fornito_utente",
            "fonte": "Residual mix fornito dall'utente (AIB European Residual Mix)", "url": "", "note": "",
        }
        kwh_go = kwh_dep_rete * a.quota_rinnovabile_deposito_pct / 100.0
        kwh_dep_grid = kwh_dep_rete - kwh_go
        fe_dep, id_dep = fe_grid_other, id_grid_other
    else:
        kwh_go = 0.0
        if a.quota_rinnovabile_deposito_pct > 0:
            avvertenze.append("La quota rinnovabile (GO/PPA) e' considerata solo con metodo market-based: ignorata nel location-based.")
        kwh_dep_grid = kwh_dep_rete
        fe_grid_other, id_grid_other = fe_loc, f_loc["id"]
        fe_dep, id_dep = fe_loc, f_loc["id"]

    if a.profilo_orario_kg_kwh is not None and a.metodo_elettricita == "location":
        hf = _hourly_weighted_factor(energy.profilo_deposito_kw or [], a.profilo_orario_kg_kwh)
        if hf is not None:
            fe_dep, id_dep = hf, "profilo_orario_utente"
            used[id_dep] = {
                "id": id_dep, "categoria": "elettricita_oraria", "valore": round(hf, 5),
                "unita": "kgCO2/kWh", "gas": "CO2", "anno": a.anno_fattori or 0, "stato": "fornito_utente",
                "fonte": "Profilo orario fornito dall'utente, pesato sulla curva di ricarica del deposito",
                "url": "", "note": "Valore medio pesato risultante.",
            }
        else:
            avvertenze.append("Profilo orario fornito ma curva di ricarica del deposito assente: usato il fattore medio annuo.")

    pe_dep = kwh_dep_grid * fe_dep
    pe_home = kwh_home * fe_grid_other
    pe_pub = kwh_pub * fe_grid_other
    for voce, q, fid, kg in (
        ("Elettricita' deposito (rete)", kwh_dep_grid, id_dep, pe_dep),
        ("Elettricita' deposito con GO/PPA", kwh_go, "go_ppa", 0.0),
        ("Elettricita' deposito da FV in sito", kwh_fv, "fv_sito", 0.0),
        ("Elettricita' ricarica domestica", kwh_home, id_grid_other, pe_home),
        ("Elettricita' ricarica pubblica (incl. energia non servita)", kwh_pub, id_grid_other, pe_pub),
    ):
        if q > 0 or voce.startswith("Elettricita' deposito (rete)"):
            breakdown.append({
                "scenario": "progetto", "voce": voce, "quantita": round(q, 1), "unita": "kWh",
                "fattore_id": fid, "tco2e": round(kg / 1000.0, 4),
            })
    pe_el_kg = pe_dep + pe_home + pe_pub

    pe_el_wtt = 0.0
    if a.includi_well_to_tank:
        kwh_grid_tot = kwh_dep_grid + kwh_go + kwh_home + kwh_pub
        pe_el_wtt = kwh_grid_tot * float(a.fe_upstream_elettricita_kg_kwh)
        used["upstream_elettricita_utente"] = {
            "id": "upstream_elettricita_utente", "categoria": "wtt_elettricita",
            "valore": float(a.fe_upstream_elettricita_kg_kwh), "unita": "kgCO2e/kWh", "gas": "CO2e",
            "anno": a.anno_fattori or 0, "stato": "fornito_utente",
            "fonte": "Fattore upstream elettricita' fornito dall'utente", "url": "", "note": "",
        }
        breakdown.append({
            "scenario": "progetto", "voce": "Well-to-tank elettricita' di rete", "quantita": round(kwh_grid_tot, 1),
            "unita": "kWh", "fattore_id": "upstream_elettricita_utente", "tco2e": round(pe_el_wtt / 1000.0, 4),
        })

    # ---- Totali ----
    be_t = (be_comb_kg + be_wtt_kg) / 1000.0
    pe_t = (pe_el_kg + pe_el_wtt + pe_phev_kg + pe_phev_wtt_kg) / 1000.0
    le_t = a.leakage_tco2e_anno * (1.0 if tipo == "ex_ante" else giorni_periodo / 365.0)
    er_lorda = be_t - pe_t - le_t
    er_netta = er_lorda * (1.0 - a.incertezza_pct / 100.0) if er_lorda > 0 else er_lorda
    if er_lorda <= 0:
        avvertenze.append("Le emissioni di progetto non sono inferiori alla baseline: nessuna riduzione.")
    if a.quota_biocarburante_pct == 0:
        avvertenze.append("Quota biogenica del carburante di baseline posta a 0%: se il gasolio contiene biodiesel "
                          "la baseline e' leggermente sovrastimata. Il verificatore puo' richiedere di valorizzarla.")
    if tipo == "ex_ante":
        avvertenze.insert(0, "Stima ex-ante dalla simulazione del giorno tipo: non e' un dato misurato e non e' verificata.")

    annual_factor = 1.0 if tipo == "ex_ante" else 365.0 / giorni_periodo
    er_anno = er_netta * annual_factor

    # Valore economico (separato dai risparmi di cassa del TCO)
    fossil_avoided_t = (be_comb_kg - pe_phev_kg) / 1000.0 * annual_factor
    valore = _carbon_value(a, er_anno, fossil_avoided_t)

    cumulata = []
    acc = 0.0
    for _ in range(int(a.orizzonte_anni)):
        acc += er_anno
        cumulata.append(round(acc, 2))

    return {
        "tipo": tipo,
        "giorni_periodo": float(a.giorni_operativi_anno) if tipo == "ex_ante" else float(giorni_periodo),
        "baseline_tco2e": round(be_t, 3),
        "progetto_tco2e": round(pe_t, 3),
        "leakage_tco2e": round(le_t, 3),
        "riduzione_lorda_tco2e": round(er_lorda, 3),
        "riduzione_netta_tco2e": round(er_netta, 3),
        "riduzione_netta_tco2e_anno": round(er_anno, 3),
        "riduzione_pct": round(er_netta / be_t * 100.0, 1) if be_t > 0 else 0.0,
        "progetto_per_fonte_tco2e": {
            "deposito": round(pe_dep / 1000.0, 3),
            "domestica": round(pe_home / 1000.0, 3),
            "pubblica": round(pe_pub / 1000.0, 3),
            "phev_benzina": round(pe_phev_kg / 1000.0, 3),
            "well_to_tank": round((pe_el_wtt + pe_phev_wtt_kg) / 1000.0, 3),
        },
        "energia_kwh": {
            "deposito_rete": round(kwh_dep_grid, 1), "deposito_go": round(kwh_go, 1),
            "deposito_fv": round(kwh_fv, 1), "domestica": round(kwh_home, 1), "pubblica": round(kwh_pub, 1),
        },
        "km_periodo": round(km_tot, 1),
        "intensita_baseline_g_km": round(be_t * 1e6 / km_tot, 1) if km_tot > 0 else 0.0,
        "intensita_progetto_g_km": round(pe_t * 1e6 / km_tot, 1) if km_tot > 0 else 0.0,
        "riduzione_cumulata_tco2e": cumulata,
        "valore_economico": valore,
        "per_gruppo": per_gruppo,
        "breakdown": breakdown,
        "confine": project_boundary(a.includi_well_to_tank, any(g.km_termici_phev > 0 or g.litri_benzina_misurati for g in groups), a.metodo_elettricita),
        "fattori_usati": list(used.values()),
        "ipotesi": asdict(a),
        "versione_metodologia": METODOLOGIA_VERSIONE,
        "versione_fattori": lib.versione,
        "dicitura": DICITURA_METODOLOGIA,
        "avvertenze": avvertenze,
    }


def _carbon_value(a: CarbonAssumptions, er_anno_t: float, fossil_avoided_t_anno: float) -> dict:
    """Valore economico delle riduzioni, tenuto separato dal TCO di cassa.

    - ombra / libero: riduzione netta x prezzo. Valore non monetario (decisionale/ESG).
    - ets2: CO2 fossile da carburante evitata x prezzo, dall'anno di avvio dell'ETS2.
      E' un costo di cassa evitato, ma indiretto: l'ETS2 grava sui fornitori di
      carburante e arriva all'azienda attraverso il prezzo alla pompa.
    """
    m = a.prezzo_carbonio_modalita
    prezzo = float(a.prezzo_carbonio_eur_t)
    anni = [a.anno_inizio + i for i in range(int(a.orizzonte_anni))]
    if m == "nessuno" or prezzo <= 0:
        return {"modalita": "nessuno", "prezzo_eur_t": 0.0, "eur_anno": 0.0, "eur_per_anno": [], "tipo_valore": "", "nota": ""}
    if m == "ets2":
        base = max(0.0, fossil_avoided_t_anno) * prezzo
        serie = [round(base if y >= a.anno_inizio_ets2 else 0.0, 0) for y in anni]
        return {
            "modalita": "ets2", "prezzo_eur_t": prezzo, "eur_anno": round(base, 0),
            "anni": anni, "eur_per_anno": serie,
            "tipo_valore": "cassa, indiretto",
            "nota": f"Costo ETS2 evitato sul carburante fossile non consumato, dal {a.anno_inizio_ets2}. "
                    "L'ETS2 grava sui fornitori di carburante e si riflette sul prezzo alla pompa; "
                    "prezzo e quota ribaltata sono ipotesi.",
        }
    base = max(0.0, er_anno_t) * prezzo
    return {
        "modalita": m, "prezzo_eur_t": prezzo, "eur_anno": round(base, 0),
        "anni": anni, "eur_per_anno": [round(base, 0)] * len(anni),
        "tipo_valore": "non monetario",
        "nota": "Prezzo ombra interno: valore per decisioni e rendicontazione ESG, non un flusso di cassa.",
    }
