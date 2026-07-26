"""
josa_core.tco — Modello finanziario Diesel vs EV (TCO, NPV, Payback, ROI, BCR).

Porting fedele della sezione "Strategic Consulting: Financial Model" del
main.py originale, estratta come funzione pura riusabile da API/frontend.
Non reintroduce il vecchio layer manuale V1G/V2X (disabilitato di default
nell'originale): quel ruolo e' oggi coperto meglio dal motore di
dispacciamento reale (josa_core.ems) e dal confronto CAPEX vs Pay-per-Use
(josa_core.business_model). Questo modulo copre il caso base Diesel vs EV.
"""

from dataclasses import dataclass, field
from typing import Optional

from .utils import npv as _npv, payback_year as _payback_year


@dataclass
class VehicleCostAssumptions:
    """Costi a livello di veicolo (non di infrastruttura), Diesel vs EV.

    Default ricalibrati su dati pubblici (non piu' inventati): studio Motus-E
    "Il TCO per la logistica" (ottobre 2025) su veicoli commerciali leggeri —
    premio di acquisto EV ~59%, manutenzione EV -37%, TCO complessivo EV -21%
    dopo 6 anni. Sono comunque medie di settore: verificare sempre contro i
    listini/preventivi reali del cliente prima di una decisione, specialmente
    per il prezzo di acquisto (il piu' variabile per modello/marca).
    """
    canone_diesel_mese_eur: float = 550.0
    canone_ev_mese_eur: float = 550.0  # allineato al diesel di default: la vecchia versione assumeva
    # un canone EV piu' alto (650) senza fonte — con leasing "a TCO" (sempre piu' diffuso) il canone
    # gia' incorpora il risparmio di carburante, quindi assumere parita' e' piu' prudente e verificabile
    # del vecchio default arbitrario. Personalizzare col preventivo reale del leasing, se disponibile.
    manutenzione_diesel_anno_eur: float = 800.0
    manutenzione_ev_anno_eur: float = 504.0  # -37% vs diesel (Motus-E), non piu' un -62% arbitrario
    prezzo_acquisto_diesel_eur: float = 40000.0
    prezzo_acquisto_ev_eur: float = 63600.0  # +59% vs diesel (Motus-E) — premio piu' realistico
    # del vecchio +20%: il vecchio default SOTTOSTIMAVA il vero costo di acquisto EV.
    incentivo_ev_eur: float = 5000.0  # verificare sempre il bando attivo: soggetto a esaurimento fondi
    # (es. incentivi FUA 2026 in Italia risultavano esauriti al momento della verifica)
    valore_residuo_diesel_pct_5y: float = 37.2  # dati mercato usato italiano 2026 (alVolante)
    valore_residuo_ev_pct_5y: float = 46.1  # idem — l'EV deprezza PIU' del diesel in Italia oggi,
    # non meno: il vecchio default (35% vs 25%, EV migliore) era ottimistico e non verificato,
    # esattamente il tipo di assunzione silenziosamente favorevole che va evitata quanto quelle sfavorevoli.
    costo_gestione_diesel_annuo_eur: float = 150.0  # NUOVO: tempo/logistica rifornimento diesel
    # (deviazioni al distributore, gestione fuel card) — prima assente, mentre il costo equivalente
    # lato EV (staff_ext_annuo_eur, gestione ricarica pubblica) era gia' conteggiato: asimmetria
    # corretta qui, non un valore a favore dell'uno o dell'altro.
    costo_restrizioni_diesel_annuo_eur: float = 0.0  # opzionale: ZTL, accessi limitati, congestion
    # charge — sempre piu' rilevanti nei centri urbani italiani ma molto variabili per comune/settore
    # di attivita': lasciato a 0 di default (non inventare un numero senza contesto locale), ma il
    # cliente puo' valorizzarlo se la sua flotta opera in zone con restrizioni diesel reali.


@dataclass
class FinancialAssumptions:
    orizzonte_anni: int = 10
    tasso_sconto: float = 0.08
    modalita: str = "leasing"  # "leasing" | "acquisto"


@dataclass
class TCOReconciliationRow:
    voce: str
    diesel_eur: float
    ev_eur: float
    delta_eur: float  # Diesel - EV, positivo = EV piu' conveniente


@dataclass
class TCOResult:
    risparmio_operativo_annuo_eur: float
    capex_differenziale_t0_eur: float
    npv_eur: float
    payback_anni: Optional[int]
    delta_tco_eur: float
    roi_netto_pct: float
    benefit_cost_ratio: float
    diesel_costo_annuo_eur: float
    ev_costo_annuo_eur: float
    diesel_tco_totale_eur: float
    ev_tco_totale_eur: float
    reconciliation: list  # list[TCOReconciliationRow]
    cashflow_annuo_eur: list  # lunghezza orizzonte_anni + 1 (anno 0 incluso)
    cashflow_cumulativo_eur: list
    cashflow_cumulativo_scontato_eur: list


@dataclass
class BreakEvenResult:
    """Soglie di convenienza EV vs Diesel — porting fedele del tab 'Break-even EV'
    del modello Streamlit originale. Risponde a: cosa deve succedere perché lo
    scenario EV diventi (o resti) conveniente in TCO, non solo "è conveniente sì/no".
    """
    stato: str  # "EV conveniente" | "EV non conveniente"
    quota_interna_attuale_pct: float
    quota_interna_breakeven_pct: Optional[float]  # None se il breakeven non è raggiungibile aumentando la quota
    quota_interna_breakeven_raggiungibile: bool  # False = anche al 100% di ricarica interna il gap non si chiude
    gap_quota_interna_pt: Optional[float]  # punti percentuali mancanti, 0 se già sufficiente
    energia_interna_richiesta_kwh_g: Optional[float]
    gap_energia_interna_kwh_g: Optional[float]
    prezzo_medio_attuale_eur_kwh: float
    prezzo_medio_breakeven_eur_kwh: Optional[float]
    prezzo_pubblico_breakeven_eur_kwh: Optional[float]
    capex_massimo_sostenibile_eur: float
    capex_margine_o_gap_eur: float  # positivo = margine disponibile, negativo = quanto va ridotto/incentivato
    # Serie per i grafici di sensitività (stessa logica dell'originale: 51 punti)
    sensitivita_quota_interna_pct: list  # asse x: 0-100%
    sensitivita_quota_interna_delta_tco: list  # asse y: delta TCO corrispondente
    sensitivita_prezzo_pubblico_eur_kwh: list  # asse x
    sensitivita_prezzo_pubblico_delta_tco: list  # asse y
    azioni_consigliate: list  # frasi pronte, stesso stile dell'originale


def compute_breakeven_analysis(
    tco_result: "TCOResult",
    e_int_kwh_g: float,
    e_tot_kwh_g: float,
    prezzo_energia_privato_eur_kwh: float,
    prezzo_energia_pubblico_eur_kwh: float,
    orizzonte_anni: int,
) -> "BreakEvenResult":
    """Calcola le soglie di break-even a partire da un TCOResult già calcolato.

    Fedele alla logica originale: individua di quanto dovrebbe cambiare il mix
    di ricarica (interna vs pubblica) o il prezzo dell'energia perché il Delta
    TCO (Diesel - EV) passi da negativo a zero, e quanto CAPEX aggiuntivo il
    business case potrebbe sostenere mantenendo tutto il resto fisso.
    """
    diesel_tco = tco_result.diesel_tco_totale_eur
    ev_tco = tco_result.ev_tco_totale_eur
    delta_tco = tco_result.delta_tco_eur
    capex_delta0 = tco_result.capex_differenziale_t0_eur

    e_ext_kwh_g = max(0.0, e_tot_kwh_g - e_int_kwh_g)
    ev_energy_y = (e_int_kwh_g * prezzo_energia_privato_eur_kwh + e_ext_kwh_g * prezzo_energia_pubblico_eur_kwh) * 365.0

    total_energy_y = max(0.0, e_tot_kwh_g * 365.0)
    internal_share = (e_int_kwh_g / e_tot_kwh_g) if e_tot_kwh_g > 0 else 0.0
    external_share = max(0.0, 1.0 - internal_share)
    current_avg_price = (ev_energy_y / total_energy_y) if total_energy_y > 0 else 0.0

    ev_tco_without_energy = ev_tco - (ev_energy_y * orizzonte_anni)
    max_energy_cost_y = ((diesel_tco - ev_tco_without_energy) / orizzonte_anni) if orizzonte_anni > 0 else None
    target_avg_price = (max_energy_cost_y / total_energy_y) if (max_energy_cost_y is not None and total_energy_y > 0) else None

    spread_pub_int = prezzo_energia_pubblico_eur_kwh - prezzo_energia_privato_eur_kwh
    required_internal_share = None
    required_internal_share_raggiungibile = True
    if target_avg_price is not None and spread_pub_int > 0:
        raw = (prezzo_energia_pubblico_eur_kwh - target_avg_price) / spread_pub_int
        required_internal_share = max(0.0, min(1.0, raw))
        required_internal_share_raggiungibile = raw <= 1.0  # raw<0 = gia' ampiamente sufficiente, non "irraggiungibile"
    elif target_avg_price is not None and target_avg_price >= current_avg_price:
        required_internal_share = 0.0
        required_internal_share_raggiungibile = True

    if required_internal_share is not None:
        required_internal_kwh = required_internal_share * e_tot_kwh_g
        gap_share_pt = max(0.0, (required_internal_share - internal_share) * 100.0)
        gap_kwh = max(0.0, required_internal_kwh - e_int_kwh_g)
    else:
        required_internal_kwh = None
        gap_share_pt = None
        gap_kwh = None

    target_public_price = None
    if target_avg_price is not None and external_share > 1e-9:
        target_public_price = (target_avg_price - internal_share * prezzo_energia_privato_eur_kwh) / external_share

    max_capex_delta0 = capex_delta0 + delta_tco
    capex_gap = max_capex_delta0 - capex_delta0  # == delta_tco, esplicitato per chiarezza semantica

    # --- Serie di sensitività (51 punti, stessa risoluzione dell'originale) ---
    n_points = 51
    shares = [i / (n_points - 1) for i in range(n_points)]
    avg_prices = [s * prezzo_energia_privato_eur_kwh + (1 - s) * prezzo_energia_pubblico_eur_kwh for s in shares]
    delta_tco_by_share = [
        diesel_tco - (ev_tco_without_energy + total_energy_y * p * orizzonte_anni)
        for p in avg_prices
    ]

    sens_pub_prices = []
    delta_tco_by_pub = []
    if total_energy_y > 0 and external_share > 1e-9:
        max_price = max(prezzo_energia_pubblico_eur_kwh * 1.5, prezzo_energia_privato_eur_kwh * 1.5, 0.1)
        sens_pub_prices = [i / (n_points - 1) * max_price for i in range(n_points)]
        for pp in sens_pub_prices:
            avg_p = internal_share * prezzo_energia_privato_eur_kwh + external_share * pp
            delta_tco_by_pub.append(diesel_tco - (ev_tco_without_energy + total_energy_y * avg_p * orizzonte_anni))

    azioni = []
    if required_internal_share is not None and gap_share_pt and gap_share_pt > 0:
        if required_internal_share_raggiungibile:
            azioni.append(f"Portare la quota di ricarica interna almeno al {required_internal_share*100:.1f}% (+{gap_share_pt:.1f} punti)")
        else:
            azioni.append(
                f"Anche portando la ricarica interna al 100% il gap non si chiude da solo — "
                f"serve anche agire su prezzo energia e/o CAPEX (vedi sotto)"
            )
    if target_public_price is not None and 0 < target_public_price < prezzo_energia_pubblico_eur_kwh:
        azioni.append(f"Negoziare il prezzo di ricarica pubblica sotto €{target_public_price:.3f}/kWh")
    if capex_gap < 0:
        azioni.append(f"Ridurre o incentivare il CAPEX differenziale di circa {abs(capex_gap):,.0f} EUR")
    if not azioni:
        azioni.append("Il mix e i costi attuali sono già sufficienti a rendere competitivo lo scenario EV.")

    return BreakEvenResult(
        stato="EV conveniente" if delta_tco >= 0 else "EV non conveniente",
        quota_interna_attuale_pct=internal_share * 100.0,
        quota_interna_breakeven_pct=(required_internal_share * 100.0 if required_internal_share is not None else None),
        quota_interna_breakeven_raggiungibile=required_internal_share_raggiungibile,
        gap_quota_interna_pt=gap_share_pt,
        energia_interna_richiesta_kwh_g=required_internal_kwh,
        gap_energia_interna_kwh_g=gap_kwh,
        prezzo_medio_attuale_eur_kwh=current_avg_price,
        prezzo_medio_breakeven_eur_kwh=target_avg_price,
        prezzo_pubblico_breakeven_eur_kwh=target_public_price,
        capex_massimo_sostenibile_eur=max_capex_delta0,
        capex_margine_o_gap_eur=capex_gap,
        sensitivita_quota_interna_pct=[s * 100.0 for s in shares],
        sensitivita_quota_interna_delta_tco=delta_tco_by_share,
        sensitivita_prezzo_pubblico_eur_kwh=sens_pub_prices,
        sensitivita_prezzo_pubblico_delta_tco=delta_tco_by_pub,
        azioni_consigliate=azioni,
    )


def compute_tco_analysis(
    n_veicoli: int,
    fleet_km_day_total: float,
    diesel_km_l: float,
    diesel_eur_l: float,
    e_int_kwh_g: float,
    e_ext_kwh_g: float,
    prezzo_energia_privato_eur_kwh: float,
    prezzo_energia_pubblico_eur_kwh: float,
    infra_capex_eur: float,
    infra_om_annuo_eur: float,
    staff_ext_annuo_eur: float,
    vehicle_costs: VehicleCostAssumptions,
    financial: FinancialAssumptions,
    e_home_kwh_g: float = 0.0,
    prezzo_energia_domestica_eur_kwh: Optional[float] = None,
) -> TCOResult:
    """Calcola il confronto finanziario Diesel vs EV su un orizzonte pluriennale.

    Stessa logica del modello Streamlit originale: due modalita' (leasing vs
    acquisto) che cambiano cosa entra nel CAPEX differenziale al tempo 0 e se
    un valore residuo dei veicoli entra nel cashflow terminale.

    e_home_kwh_g: energia ricaricata a casa del dipendente (ricarica ibrida
    domestica) — PRIMA OMESSA DAL CALCOLO, bug corretto: se il fabbisogno
    energetico reale della flotta include una quota domestica (spesso la
    maggioranza, quando hybrid_private_home_charging e' attivo), ignorarla
    fa apparire il costo energetico EV artificialmente basso, come se quella
    quota costasse zero. prezzo_energia_domestica_eur_kwh: se non specificato,
    usa lo stesso prezzo della ricarica privata aziendale (ragionevole come
    approssimazione, ma le tariffe domestiche reali possono differire — da
    personalizzare se il cliente ha un dato piu' preciso, es. se l'azienda
    rimborsa i dipendenti a una tariffa diversa dalla propria).
    """
    vc = vehicle_costs
    nv = max(0, n_veicoli)

    diesel_fuel_y = (fleet_km_day_total / diesel_km_l * diesel_eur_l) * 365.0
    prezzo_domestico = prezzo_energia_domestica_eur_kwh if prezzo_energia_domestica_eur_kwh is not None else prezzo_energia_privato_eur_kwh
    ev_energy_y = (
        e_int_kwh_g * prezzo_energia_privato_eur_kwh
        + e_ext_kwh_g * prezzo_energia_pubblico_eur_kwh
        + e_home_kwh_g * prezzo_domestico
    ) * 365.0
    ev_home_energy_y = e_home_kwh_g * prezzo_domestico * 365.0

    veh_mnt_diesel_y = vc.manutenzione_diesel_anno_eur * nv
    veh_mnt_ev_y = vc.manutenzione_ev_anno_eur * nv

    # Costi di gestione/logistica simmetrici: prima solo l'EV aveva un overhead conteggiato
    # (staff_ext_annuo_eur, gestione ricarica pubblica) mentre il diesel non aveva l'equivalente
    # (tempo/logistica rifornimento) — corretto qui.
    diesel_gestione_y = vc.costo_gestione_diesel_annuo_eur * nv
    diesel_restrizioni_y = vc.costo_restrizioni_diesel_annuo_eur * nv

    lease_diesel_y = vc.canone_diesel_mese_eur * 12 * nv
    lease_ev_y = vc.canone_ev_mese_eur * 12 * nv

    net_ev_price = max(0.0, vc.prezzo_acquisto_ev_eur - vc.incentivo_ev_eur)
    delta_vehicle_capex = nv * (net_ev_price - vc.prezzo_acquisto_diesel_eur)

    resid_diesel_h = (vc.valore_residuo_diesel_pct_5y / 100.0) ** (financial.orizzonte_anni / 5.0)
    resid_ev_h = (vc.valore_residuo_ev_pct_5y / 100.0) ** (financial.orizzonte_anni / 5.0)
    residual_diesel = nv * vc.prezzo_acquisto_diesel_eur * resid_diesel_h
    residual_ev = nv * net_ev_price * resid_ev_h
    residual_delta = residual_ev - residual_diesel

    is_leasing = financial.modalita == "leasing"

    if is_leasing:
        diesel_total_y = diesel_fuel_y + veh_mnt_diesel_y + lease_diesel_y + diesel_gestione_y + diesel_restrizioni_y
        ev_total_y = ev_energy_y + veh_mnt_ev_y + lease_ev_y + infra_om_annuo_eur + staff_ext_annuo_eur
        capex_delta0 = infra_capex_eur
        terminal_delta = 0.0
    else:
        diesel_total_y = diesel_fuel_y + veh_mnt_diesel_y + diesel_gestione_y + diesel_restrizioni_y
        ev_total_y = ev_energy_y + veh_mnt_ev_y + infra_om_annuo_eur + staff_ext_annuo_eur
        capex_delta0 = infra_capex_eur + delta_vehicle_capex
        terminal_delta = residual_delta

    annual_savings = diesel_total_y - ev_total_y  # positivo = EV migliore

    cfs = [-capex_delta0] + [annual_savings] * financial.orizzonte_anni
    if terminal_delta != 0.0:
        cfs[-1] += terminal_delta

    npv_val = _npv(financial.tasso_sconto, cfs)
    payback = _payback_year(cfs)

    if is_leasing:
        diesel_tco = diesel_total_y * financial.orizzonte_anni
        ev_tco = infra_capex_eur + ev_total_y * financial.orizzonte_anni
    else:
        diesel_tco = (nv * vc.prezzo_acquisto_diesel_eur) + diesel_total_y * financial.orizzonte_anni - residual_diesel
        ev_tco = (nv * net_ev_price) + infra_capex_eur + ev_total_y * financial.orizzonte_anni - residual_ev

    delta_tco = diesel_tco - ev_tco

    roi_net = ((sum(cfs[1:]) - cfs[0]) / abs(cfs[0]) * 100.0) if cfs[0] != 0 else 0.0
    bcr = (sum(max(0.0, x) for x in cfs[1:]) / abs(cfs[0])) if cfs[0] != 0 else 0.0

    reconciliation = [
        TCOReconciliationRow("Carburante Diesel (anno)", diesel_fuel_y, 0.0, diesel_fuel_y),
        TCOReconciliationRow("Energia EV interna+pubblica (anno)", 0.0, ev_energy_y - ev_home_energy_y, -(ev_energy_y - ev_home_energy_y)),
        TCOReconciliationRow("Energia EV domestica/ibrida (anno)", 0.0, ev_home_energy_y, -ev_home_energy_y),
        TCOReconciliationRow("Manutenzione veicoli (anno)", veh_mnt_diesel_y, veh_mnt_ev_y, veh_mnt_diesel_y - veh_mnt_ev_y),
        TCOReconciliationRow("O&M infrastruttura ricarica (anno)", 0.0, infra_om_annuo_eur, -infra_om_annuo_eur),
        TCOReconciliationRow("Gestione/logistica rifornimento (anno)", diesel_gestione_y, staff_ext_annuo_eur, diesel_gestione_y - staff_ext_annuo_eur),
        TCOReconciliationRow("Restrizioni diesel: ZTL/congestion charge (anno)", diesel_restrizioni_y, 0.0, diesel_restrizioni_y),
    ]
    if is_leasing:
        reconciliation.append(
            TCOReconciliationRow("Canoni veicoli (anno)", lease_diesel_y, lease_ev_y, lease_diesel_y - lease_ev_y)
        )
    reconciliation.append(
        TCOReconciliationRow("TOTALE OPEX annuo", diesel_total_y, ev_total_y, annual_savings)
    )

    cum = 0.0
    cashflow_cumulativo = []
    for cf in cfs:
        cum += cf
        cashflow_cumulativo.append(cum)
    cashflow_cumulativo_scontato = [
        sum(cfs[j] / ((1 + financial.tasso_sconto) ** j) for j in range(i + 1))
        for i in range(len(cfs))
    ]

    return TCOResult(
        risparmio_operativo_annuo_eur=annual_savings,
        capex_differenziale_t0_eur=capex_delta0,
        npv_eur=npv_val,
        payback_anni=payback,
        delta_tco_eur=delta_tco,
        roi_netto_pct=roi_net,
        benefit_cost_ratio=bcr,
        diesel_costo_annuo_eur=diesel_total_y,
        ev_costo_annuo_eur=ev_total_y,
        diesel_tco_totale_eur=diesel_tco,
        ev_tco_totale_eur=ev_tco,
        reconciliation=reconciliation,
        cashflow_annuo_eur=cfs,
        cashflow_cumulativo_eur=cashflow_cumulativo,
        cashflow_cumulativo_scontato_eur=cashflow_cumulativo_scontato,
    )
