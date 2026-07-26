"""
josa_core.business_model — confronto CAPEX vs Pay-per-Use, alimentato dai
risultati del motore di dispacciamento (josa_core.ems).

Punto centrale (vedi report di design, §10): i due modelli commerciali non sono
lo stesso calcolo con un'etichetta diversa — hanno funzioni obiettivo diverse:
  - CAPEX: il cliente possiede l'infrastruttura. Il beneficio economico del
    dispacciamento intelligente (risparmio energia, peak shaving, eventuale
    ricavo V2G) accresce il NPV del CLIENTE.
  - Pay-per-Use: y35 possiede l'infrastruttura e fattura a consumo (margine
    per kWh erogato). Il beneficio del dispacciamento intelligente accresce
    il NPV di Y35 come operatore — il cliente non vede il CAPEX ma paga il
    margine concordato.

Questo modulo non inventa i numeri di risparmio: li riceve da una run reale
di josa_core.ems (rolling MPC vs baseline "ricarica ingenua"), annualizzati.
"""

from dataclasses import dataclass, field
from typing import Optional

from .utils import npv, payback_year
from .ems.rolling_mpc import MultiDayResult

MODELLI_VALIDI = ("capex", "pay_per_use")


@dataclass
class DispatchSavingsAnnualized:
    """Risultato di un confronto smart-dispatch vs baseline, annualizzato da una
    simulazione multi-giorno rappresentativa. Vedi annualize_dispatch_comparison().
    """
    giorni_simulati: int
    kwh_erogati_annui: float             # energia totale caricata ai veicoli, annualizzata
    risparmio_energia_annuo_eur: float   # risparmio su costo energia (scala con i giorni simulati)
    risparmio_potenza_annuo_eur: float   # risparmio su potenza impegnata (scala con i periodi di fatturazione, NON con i giorni)
    risparmio_annuo_eur: float           # somma dei due sopra, per comodita'
    ricavo_v2g_annuo_eur: float          # ricavo da vendita/flessibilita' del dispacciamento smart, annualizzato
    picco_ridotto_kw: float              # riduzione di picco tra baseline e smart dispatch
    nota_rappresentativita: str = (
        "Estrapolazione lineare da una finestra simulata: non cattura stagionalita' "
        "(es. domanda estiva vs invernale, tariffe che cambiano nel tempo). Usare come "
        "stima di primo ordine, da raffinare con dati stagionali quando disponibili. "
        "Il risparmio su potenza impegnata assume che il picco osservato nella finestra "
        "simulata sia rappresentativo del picco mensile reale — se la finestra e' breve "
        "(pochi giorni), questa e' un'approssimazione: idealmente si simula un mese intero "
        "prima di usare questo numero per una decisione di investimento."
    )


def annualize_dispatch_comparison(
    smart_result: MultiDayResult,
    baseline_result: MultiDayResult,
    giorni_simulati: int,
    kwh_erogati_nella_simulazione: float,
    periodi_fatturazione_potenza_anno: int = 12,
) -> DispatchSavingsAnnualized:
    """Estrapola un confronto smart-dispatch (rolling MPC) vs baseline (ricarica ingenua)
    simulato su una finestra di `giorni_simulati` giorni a una stima annua.

    ATTENZIONE (bug corretto): il costo di potenza impegnata NON viene annualizzato con
    lo stesso fattore (365/giorni_simulati) del costo energia. Il costo energia si ripete
    davvero ogni giorno, quindi scala linearmente con i giorni. Il costo di potenza
    impegnata rappresenta tipicamente una tariffa PERIODICA (es. mensile: si paga per il
    picco del mese, non per il picco di ogni singolo giorno) — usare lo stesso fattore
    giornaliero lo sovrastimava di un ordine di grandezza in una versione precedente di
    questo modulo. Qui si annualizza moltiplicando per `periodi_fatturazione_potenza_anno`
    (default 12 = mensile), assumendo che il picco osservato nella finestra simulata sia
    rappresentativo del picco del periodo di fatturazione reale.
    """
    if giorni_simulati <= 0:
        raise ValueError("giorni_simulati deve essere positivo")
    if periodi_fatturazione_potenza_anno <= 0:
        raise ValueError("periodi_fatturazione_potenza_anno deve essere positivo")

    fattore_annuo_energia = 365.0 / giorni_simulati

    risparmio_energia = (
        (baseline_result.costo_energia_reale_eur + baseline_result.costo_degrado_reale_eur)
        - (smart_result.costo_energia_reale_eur + smart_result.costo_degrado_reale_eur)
    ) * fattore_annuo_energia

    risparmio_potenza_finestra = (
        baseline_result.costo_potenza_impegnata_reale_eur - smart_result.costo_potenza_impegnata_reale_eur
    )
    risparmio_potenza_annuo = risparmio_potenza_finestra * periodi_fatturazione_potenza_anno

    ricavo_v2g_finestra = smart_result.ricavo_vendita_reale_eur

    return DispatchSavingsAnnualized(
        giorni_simulati=giorni_simulati,
        kwh_erogati_annui=kwh_erogati_nella_simulazione * fattore_annuo_energia,
        risparmio_energia_annuo_eur=risparmio_energia,
        risparmio_potenza_annuo_eur=risparmio_potenza_annuo,
        risparmio_annuo_eur=risparmio_energia + risparmio_potenza_annuo,
        ricavo_v2g_annuo_eur=ricavo_v2g_finestra * fattore_annuo_energia,
        picco_ridotto_kw=max(0.0, baseline_result.picco_reale_kw - smart_result.picco_reale_kw),
    )


@dataclass
class BusinessModelInputs:
    capex_eur: float
    orizzonte_anni: int
    tasso_sconto_annuo: float = 0.08
    om_annuo_eur: float = 0.0

    # Solo per Pay-per-Use:
    margine_ppu_eur_kwh: float = 0.25
    quota_ricavo_v2g_operatore_pct: float = 100.0  # quota di ricavo V2G che va a y35 (resto al cliente)


@dataclass
class BusinessModelResult:
    modello: str
    npv_eur: float
    payback_anni: Optional[int]
    flussi_di_cassa_annui: list
    ricavo_o_risparmio_annuo_eur: float
    nota: str


def evaluate_business_model(
    modello: str,
    inputs: BusinessModelInputs,
    savings: DispatchSavingsAnnualized,
) -> BusinessModelResult:
    """Valuta NPV/payback per il modello commerciale scelto, usando i risparmi/ricavi
    annualizzati prodotti da una run reale del motore di dispacciamento (non stimati a mano).
    """
    if modello not in MODELLI_VALIDI:
        raise ValueError(f"modello deve essere uno di {MODELLI_VALIDI}")

    if modello == "capex":
        # Il beneficio del dispacciamento intelligente (risparmio energetico + ricavo V2G)
        # accresce il NPV del cliente, che possiede l'infrastruttura.
        beneficio_annuo = savings.risparmio_annuo_eur + savings.ricavo_v2g_annuo_eur - inputs.om_annuo_eur
        nota = (
            "Prospettiva CLIENTE (possiede l'infrastruttura): il beneficio del "
            "dispacciamento intelligente riduce i suoi costi operativi."
        )
    else:  # pay_per_use
        quota = max(0.0, min(1.0, inputs.quota_ricavo_v2g_operatore_pct / 100.0))
        ricavo_margine = inputs.margine_ppu_eur_kwh * savings.kwh_erogati_annui
        ricavo_v2g_operatore = savings.ricavo_v2g_annuo_eur * quota
        beneficio_annuo = ricavo_margine + ricavo_v2g_operatore - inputs.om_annuo_eur
        nota = (
            "Prospettiva OPERATORE (y35 possiede l'infrastruttura, fattura a consumo): "
            f"ricavo da margine PPU ({ricavo_margine:,.0f} EUR/anno) + quota ricavo V2G "
            f"({ricavo_v2g_operatore:,.0f} EUR/anno, {inputs.quota_ricavo_v2g_operatore_pct:.0f}% del totale). "
            "Il risparmio energetico diretto (savings.risparmio_annuo_eur) NON entra qui: "
            "in Pay-per-Use il cliente paga a consumo, non vede il conto energia del gestore."
        )

    cashflows = [-inputs.capex_eur] + [beneficio_annuo] * inputs.orizzonte_anni

    npv_value = npv(inputs.tasso_sconto_annuo, cashflows)
    payback = payback_year(cashflows)

    return BusinessModelResult(
        modello=modello,
        npv_eur=npv_value,
        payback_anni=payback,
        flussi_di_cassa_annui=cashflows,
        ricavo_o_risparmio_annuo_eur=beneficio_annuo,
        nota=nota,
    )


def compare_business_models(
    inputs: BusinessModelInputs,
    savings: DispatchSavingsAnnualized,
) -> dict:
    """Valuta entrambi i modelli sugli stessi risparmi/ricavi di dispacciamento e li
    confronta fianco a fianco — utile per la conversazione commerciale col cliente:
    quale modello conviene a CHI, non un singolo numero "migliore" in assoluto.
    """
    capex_result = evaluate_business_model("capex", inputs, savings)
    ppu_result = evaluate_business_model("pay_per_use", inputs, savings)
    return {
        "capex": capex_result,
        "pay_per_use": ppu_result,
        "nota_confronto": (
            "I due NPV rispondono a domande diverse: il primo e' il ritorno per il CLIENTE "
            "se possiede l'impianto; il secondo e' il ritorno per Y35 come operatore se lo "
            "possiede e fattura a consumo. Non sono direttamente comparabili come 'il migliore "
            "in assoluto' — dipende da chi sostiene il CAPEX e da cosa l'azienda vuole ottimizzare "
            "(minimizzare il proprio investimento vs costruire un flusso di ricavi ricorrente)."
        ),
    }
