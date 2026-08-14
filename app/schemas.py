"""
Schemi Pydantic per l'API josa_api.

Questi modelli sono il "contratto" tra il frontend (o qualunque sistema esterno,
Fleet Management/EMS/BMS/ERP) e il motore josa_core. Separarli dai modelli
interni del motore (josa_core.models) e' intenzionale: i due possono evolvere
a velocita' diverse — l'API deve restare stabile per i client esterni anche
se il motore interno cambia forma.
"""

from datetime import time
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class HardwareSpec(BaseModel):
    """Una voce di catalogo hardware (es. 'AC 22kW')."""
    nome: str
    potenza_kw: float = Field(gt=0)
    costo_acquisto_eur: float = Field(ge=0)
    costo_installazione_eur: float = Field(ge=0)
    costo_manutenzione_eur_anno: float = Field(ge=0)


class FleetGroup(BaseModel):
    """Un gruppo omogeneo di veicoli (es. 'Last Mile', 'Ufficio')."""
    gruppo: str
    profilo: str = Field(description="Last-mile | Sales | Long-haul | Office | Custom")
    n_veicoli: int = Field(ge=0)
    km_per_giro: float = Field(ge=0)
    giri_per_veicolo_giorno: float = Field(ge=0)
    giri_per_autonomia: float = Field(default=2.0, gt=0)
    consumo_kwh_km: float = Field(default=0.22, gt=0)
    batteria_kwh: float = Field(default=75.0, gt=0)
    tempo_disponibile_min: float = Field(default=60.0, ge=0)
    finestra_inizio: time = Field(default=time(9, 0))
    finestra_fine: time = Field(default=time(19, 0))
    contemporanei_max: int = Field(default=0, ge=0)
    quota_ricarica_deposito: float = Field(default=1.0, ge=0, le=1)
    k_factor: float = Field(default=1.0, ge=1.0, le=2.5)
    ricarica_domestica: Optional[bool] = Field(
        default=None,
        description=(
            "Questo gruppo può accedere alla ricarica domestica? None = usa la policy "
            "globale (hybrid_private_home_charging); True/False sovrascrive per questo "
            "gruppo specifico — utile per distinguere veicoli con parcheggio privato "
            "assegnato da veicoli pool/consegne che non possono caricare a casa."
        ),
    )
    ricarica_notturna_azienda: Optional[bool] = Field(
        default=None,
        description=(
            "Questo gruppo può caricare durante la notte in azienda? Indipendente da "
            "ricarica_domestica — impostarlo esplicitamente a True per i gruppi senza "
            "accesso domestico (es. veicoli pool), altrimenti restano senza NESSUNA "
            "finestra di ricarica notturna (né a casa né in azienda), potendo caricare "
            "solo nei brevi intervalli tra un giro e l'altro durante il giorno. None = "
            "usa il default della policy globale (che con ricarica ibrida attiva assume "
            "che la ricarica notturna aziendale NON sia necessaria per default)."
        ),
    )
    potenza_max_ricarica_ac_kw: float = Field(
        default=11.0, gt=0,
        description=(
            "Potenza massima che il caricatore DI BORDO dei veicoli di questo gruppo "
            "può accettare in AC — indipendente dalla potenza nominale della colonnina. "
            "Molti veicoli hanno un caricatore di bordo da 11 kW anche se collegati a "
            "una colonnina AC da 22 kW (limite del veicolo, non dell'infrastruttura). "
            "Default 11 kW (il piu' comune); alzarlo se la flotta ha veicoli con "
            "caricatore di bordo trifase da 22 kW."
        ),
    )
    pct_veicoli_con_casa: Optional[float] = Field(
        default=None, ge=0, le=100,
        description=(
            "Percentuale di VEICOLI del gruppo con accesso a wallbox domestico. "
            "Es. 40 = 40 veicoli su 100 caricano a casa, i restanti 60 solo in azienda. "
            "Ignorato se veicoli_con_casa e' impostato (quello ha priorita')."
        ),
    )
    veicoli_con_casa: Optional[list] = Field(
        default=None,
        description=(
            "Lista PUNTUALE dei vehicle_id con accesso wallbox domestico. "
            "Es. ['Dipendenti_1', 'Dipendenti_3', 'Dipendenti_7']. "
            "Ha priorita' su pct_veicoli_con_casa: se presente, solo questi veicoli "
            "caricano a casa, tutti gli altri dipendono solo dall'azienda. "
            "Gli ID sono nella forma NomeGruppo_N (es. 'Dipendenti_1' per il primo "
            "veicolo del gruppo chiamato 'Dipendenti'). "
            "Accetta anche un campo stringa separato da virgola per compatibilita' col frontend."
        ),
    )
    # Campo legacy mantenuto per compatibilita': ignorato se pct_veicoli_con_casa e' impostato
    quota_ricarica_domestica_pct: Optional[float] = Field(default=None, ge=0, le=100)
    probabilita_utilizzo_pct: Optional[float] = Field(
        default=None, ge=0, le=100,
        description=(
            "Probabilità che un dato veicolo di questo gruppo venga effettivamente "
            "usato (faccia almeno un giro) nel giorno simulato — pensato per flotte "
            "condivise (Pool Car) dove non tutti i veicoli sono in uso ogni giorno."
        ),
    )
    # --- Campi ibrido plug-in ---
    autonomia_elettrica_km: Optional[float] = Field(
        default=None, ge=0,
        description=(
            "Autonomia in modalità puramente elettrica (km). Per un ibrido plug-in: "
            "sotto questa soglia il veicolo consuma solo kWh, oltre passa a benzina. "
            "Se None, il veicolo è trattato come EV puro (nessuna parte a benzina)."
        ),
    )
    consumo_benzina_l100km: Optional[float] = Field(
        default=None, ge=0,
        description=(
            "Consumo benzina nella parte del percorso oltre l'autonomia elettrica "
            "(L/100km) — specifico per questo modello di ibrido, non un valore "
            "generico. Rilevante sia per il ROI (quanto si risparmia vs diesel puro) "
            "sia per la probabilità di utilizzo effettivo della colonnina (chi ha "
            "un ibrido con 60 km di autonomia e fa solo 40 km al giorno carica ogni "
            "volta; chi ne fa 200 potrebbe non caricare mai, abbassando la domanda "
            "reale sulle colonnine). Se None e autonomia_elettrica_km è impostato, "
            "usa il valore di sistema (diesel_l_per_100km nella policy)."
        ),
    )
    accetta_ricarica_dc: bool = Field(
        default=False,
        description=(
            "True solo se i veicoli di questo gruppo hanno presa CCS/CHAdeMO e "
            "accettano ricarica rapida DC. La maggior parte degli ibridi plug-in "
            "NON ha questa presa — il default è False. Se False, il motore non "
            "assegna mai questo gruppo a colonnine DC, indipendentemente da quante "
            "siano installate."
        ),
    )


class EnginePolicy(BaseModel):
    """Vincoli di rete, policy notturna e soglie SOC — un unico blocco di
    configurazione operativa, cosi' un client esterno lo passa una volta sola."""
    p_rete_kw: float = Field(gt=0, description="Potenza di rete disponibile")
    p_shaving_kw: float = Field(gt=0, description="Limite di peak shaving")
    allow_oversizing: bool = False
    dc_fixed_power: bool = True
    dc_redundancy: int = Field(default=2, ge=1)
    max_ac_veicoli_per_punto: int = Field(default=1, ge=1)
    max_dc_veicoli_per_punto: int = Field(default=1, ge=1)
    limite_ora_turno: float = Field(default=19.0, ge=0, le=24)
    soc_start_pct: float = Field(default=90.0, ge=0, le=100)
    soc_min_pct: float = Field(default=20.0, ge=0, le=100)
    soc_max_pct: float = Field(default=90.0, ge=0, le=100)
    soc_buffer_pct: float = Field(default=5.0, ge=0, le=100)
    hybrid_private_home_charging: bool = True
    company_buffer_pct: float = Field(default=30.0, ge=0, le=100)
    sim_days: int = Field(default=1, ge=1, le=14)


class FuelCostsIn(BaseModel):
    diesel_km_l: float = Field(default=10.0, gt=0)
    diesel_eur_l: float = Field(default=1.7, gt=0)
    staff_eur_h: float = Field(default=20.0, ge=0)


class EnergyCosts(BaseModel):
    prezzo_privato_eur_kwh: float = Field(default=0.25, ge=0)
    prezzo_pubblico_eur_kwh: float = Field(default=0.55, ge=0)
    prezzo_domestico_eur_kwh: Optional[float] = Field(default=None, ge=0, description="Tariffa energia ricaricata a casa del dipendente (ricarica ibrida). Se non specificato, usa lo stesso prezzo della ricarica privata aziendale.")


class HardwareConfig(BaseModel):
    """Quantita' installate per tipo hardware, es. {'AC 22kW': 8, 'DC 30kW': 1}."""
    quantita: dict[str, int]

    @field_validator("quantita")
    @classmethod
    def _no_negative(cls, v):
        for k, q in v.items():
            if q < 0:
                raise ValueError(f"Quantita' negativa non ammessa per '{k}'")
        return v


class SimulateRequest(BaseModel):
    gruppi: list[FleetGroup]
    catalogo_hardware: list[HardwareSpec]
    configurazione: HardwareConfig
    policy: EnginePolicy
    fuel: FuelCostsIn = FuelCostsIn()
    energia: EnergyCosts = EnergyCosts()
    stress_test: bool = False
    stress_extra_consumo_pct: float = 0.0
    stress_ritardo_min: float = 0.0
    gantt_settimanale: bool = Field(
        default=False,
        description=(
            "Se True, il campo gantt_veicoli nella risposta copre un'intera settimana "
            "(168h, Lun-Ven lavorativi) invece di un solo giorno tipo (24h). Il "
            "dimensionamento (CAPEX, copertura) non cambia in nessun caso — resta "
            "sempre calcolato sul giorno tipo singolo."
        ),
    )


class GanttSegmento(BaseModel):
    inizio: float = Field(description="Ora di inizio dall'inizio dell'orizzonte (0-24 per un giorno, 0-168 per una settimana)")
    fine: float = Field(description="Ora di fine")
    stato: str = Field(description="'lavoro' | 'carica_azienda' | 'finestra_domestica' | 'sosta'")


class GanttVeicolo(BaseModel):
    vehicle_id: str
    gruppo: Optional[str] = None
    ricarica_domestica: bool = Field(description="Se True, il veicolo ha accesso alla ricarica domestica (can_home_night)")
    segmenti: list[GanttSegmento]


class SimulateResponse(BaseModel):
    config: dict[str, int]
    kpi: dict
    veicoli_totali: int
    veicoli_serviti: int
    copertura_pct: float
    capex_eur: float
    timeline_p_kw: list[float] = Field(default_factory=list, description="Piano di ricarica giornaliero: potenza istantanea, passi da 15 min")
    timeline_q: list[float] = Field(default_factory=list, description="Coda veicoli in attesa, stessi passi")
    gantt_veicoli: list[GanttVeicolo] = Field(default_factory=list, description="Un giorno tipo (o una settimana, se richiesto) per ogni veicolo della flotta: quando lavora, dove e quando carica")
    gantt_orizzonte_h: float = Field(default=24.0, description="168.0 se gantt_settimanale=True nella richiesta, altrimenti 24.0")
    gantt_colonnine: list[dict] = Field(default_factory=list, description="Per ogni colonnina fisica: quando è occupata, da quale veicolo, tasso di utilizzo — complementare a gantt_veicoli")


class OptimizeRequest(BaseModel):
    gruppi: list[FleetGroup]
    catalogo_hardware: list[HardwareSpec]
    policy: EnginePolicy
    fuel: FuelCostsIn = FuelCostsIn()
    energia: EnergyCosts = EnergyCosts()
    tipi_hardware_da_esplorare: list[str] = Field(
        description="Sottoinsieme del catalogo su cui far girare la beam search, es. ['AC 22kW', 'DC 30kW']"
    )
    budget_max_eur: float = Field(gt=0)
    beam_size: int = Field(default=4, ge=1, le=20)
    patience: int = Field(default=8, ge=1, le=50)
    max_steps: int = Field(default=100, ge=1, le=1000)


class RankedSolution(BaseModel):
    config: dict[str, int]
    kpi: dict
    capex_eur: float
    copertura_pct: float
    ammissibile: bool = Field(default=True, description="False se e' la migliore trovata entro budget ma non copre il 100% del fabbisogno aziendale")
    gap_kwh_da_coprire: Optional[float] = Field(default=None, description="kWh/giorno di fabbisogno aziendale ancora scoperti, se ammissibile=False")


class OptimizeResponse(BaseModel):
    soluzioni: list[RankedSolution]
    nodi_esplorati: int
    ammissibili_trovate: int


class VehicleCostAssumptionsIn(BaseModel):
    """Costi veicolo Diesel vs EV per il modello TCO.

    Default ricalibrati su dati pubblici (studio Motus-E, ottobre 2025, veicoli
    commerciali leggeri): premio di acquisto EV ~59%, manutenzione EV -37%,
    valori residuo da dati mercato usato italiano 2026. Personalizzabili —
    specialmente il prezzo di acquisto, il più variabile per modello/marca."""
    canone_diesel_mese_eur: float = Field(default=550.0, ge=0)
    canone_ev_mese_eur: float = Field(default=550.0, ge=0)
    manutenzione_diesel_anno_eur: float = Field(default=800.0, ge=0)
    manutenzione_ev_anno_eur: float = Field(default=504.0, ge=0)
    prezzo_acquisto_diesel_eur: float = Field(default=40000.0, ge=0)
    prezzo_acquisto_ev_eur: float = Field(default=63600.0, ge=0)
    incentivo_ev_eur: float = Field(default=5000.0, ge=0, description="Verificare sempre il bando attivo: soggetto a esaurimento fondi")
    valore_residuo_diesel_pct_5y: float = Field(default=37.2, ge=0, le=100)
    valore_residuo_ev_pct_5y: float = Field(default=46.1, ge=0, le=100)
    costo_gestione_diesel_annuo_eur: float = Field(default=150.0, ge=0, description="Tempo/logistica rifornimento diesel — simmetrico al costo di gestione ricarica pubblica EV")
    costo_restrizioni_diesel_annuo_eur: float = Field(default=0.0, ge=0, description="ZTL, congestion charge — 0 di default, valorizzare se la flotta opera in zone con restrizioni reali")


class FinancialAssumptionsIn(BaseModel):
    orizzonte_anni: int = Field(default=10, ge=1, le=30)
    tasso_sconto_pct: float = Field(default=8.0, ge=0, le=30)
    modalita: str = Field(default="leasing", description="'leasing' oppure 'acquisto'")


class TCOReconciliationRowOut(BaseModel):
    voce: str
    diesel_eur: float
    ev_eur: float
    delta_eur: float


class TCOResultOut(BaseModel):
    risparmio_operativo_annuo_eur: float
    capex_differenziale_t0_eur: float
    npv_eur: float
    payback_anni: Optional[int]
    delta_tco_eur: float
    roi_netto_pct: float
    benefit_cost_ratio: float
    diesel_costo_annuo_eur: float
    ev_costo_annuo_eur: float
    reconciliation: list[TCOReconciliationRowOut]
    cashflow_cumulativo_scontato_eur: list[float]


class BreakEvenResultOut(BaseModel):
    """Soglie di convenienza EV vs Diesel — non solo 'conveniente sì/no', ma cosa
    dovrebbe cambiare (mix di ricarica, prezzo energia, CAPEX) perché lo diventi."""
    stato: str
    quota_interna_attuale_pct: float
    quota_interna_breakeven_pct: Optional[float]
    quota_interna_breakeven_raggiungibile: bool
    gap_quota_interna_pt: Optional[float]
    energia_interna_richiesta_kwh_g: Optional[float]
    gap_energia_interna_kwh_g: Optional[float]
    prezzo_medio_attuale_eur_kwh: float
    prezzo_medio_breakeven_eur_kwh: Optional[float]
    prezzo_pubblico_breakeven_eur_kwh: Optional[float]
    capex_massimo_sostenibile_eur: float
    capex_margine_o_gap_eur: float
    sensitivita_quota_interna_pct: list[float]
    sensitivita_quota_interna_delta_tco: list[float]
    sensitivita_prezzo_pubblico_eur_kwh: list[float]
    sensitivita_prezzo_pubblico_delta_tco: list[float]
    azioni_consigliate: list[str]


class MarginalChargerOption(BaseModel):
    """Confronto marginale: aggiungere UNA unita' in piu' di questo tipo conviene
    rispetto a continuare a pagare il sovrapprezzo di ricarica pubblica per i
    veicoli non serviti internamente?"""
    tipo: str
    capex_incrementale_eur: float
    costo_inefficienza_attuale_eur_anno: float = Field(description="pena_en + staff_ext oggi: sovrapprezzo pubblico vs privato + overhead gestione")
    costo_inefficienza_dopo_eur_anno: float
    risparmio_annuo_eur: float
    npv_incrementale_eur: float
    payback_anni: Optional[int]
    conviene: bool
    veicoli_serviti_prima: int
    veicoli_serviti_dopo: int


class ScenarioResult(BaseModel):
    label: str
    is_benchmark: bool
    ammissibile: bool
    config: dict[str, int]
    capex_eur: float
    copertura_pct: float
    veicoli_serviti: int
    veicoli_totali: int
    tco: Optional[TCOResultOut] = None
    breakeven: Optional[BreakEvenResultOut] = None
    analisi_marginale: Optional[list[MarginalChargerOption]] = None
    timeline_p_kw: list[float] = Field(default_factory=list, description="Piano di ricarica giornaliero: potenza istantanea, passi da 15 min")
    timeline_q: list[float] = Field(default_factory=list, description="Coda veicoli in attesa, stessi passi")
    picco_intelligente_kw: Optional[float] = Field(default=None, description="Picco minimo raggiungibile con allocazione a pool condiviso (LP), stesso hardware — confronto, non sostituisce il picco del motore principale")
    copertura_intelligente_pct: Optional[float] = Field(default=None, description="Copertura raggiunta dall'allocazione intelligente (100 se energia richiesta interamente copribile, 0 se infeasible)")
    configurazione_abbondante: bool = Field(default=False, description="True per lo scenario ammissibile con piu' punti installati — meno attesa per il personale, base migliore per un futuro V2G")
    nota_configurazione_abbondante: Optional[str] = None


class ScenarioCompareRequest(BaseModel):
    gruppi: list[FleetGroup]
    catalogo_hardware: list[HardwareSpec]
    policy: EnginePolicy
    fuel: FuelCostsIn = FuelCostsIn()
    energia: EnergyCosts = EnergyCosts()
    tipi_hardware_da_esplorare: list[str]
    budget_max_eur: float = Field(gt=0)
    beam_size: int = Field(default=4, ge=1, le=20)
    patience: int = Field(default=8, ge=1, le=50)
    max_steps: int = Field(default=60, ge=1, le=1000)
    includi_benchmark: bool = Field(default=True, description="Include anche gli scenari standard 1:1, 1:2, 1:4 auto/punto")
    vehicle_costs: VehicleCostAssumptionsIn = VehicleCostAssumptionsIn()
    financial: FinancialAssumptionsIn = FinancialAssumptionsIn()


class ScenarioCompareResponse(BaseModel):
    scenari: list[ScenarioResult]
    nodi_esplorati: int


class V2GVehicleInput(BaseModel):
    """Un veicolo trattato come asset energetico ('batteria distribuita') per il
    dispacciamento V2G. Vedi josa_core.ems.assets.VehicleAsset per la semantica completa."""
    id: str
    capacita_kwh: float = Field(gt=0)
    soc_iniziale_pct: float = Field(ge=0, le=100)
    soc_min_pct: float = Field(default=10.0, ge=0, le=100)
    soc_max_pct: float = Field(default=100.0, ge=0, le=100)
    rendimento_carica: float = Field(default=0.95, gt=0, le=1)
    rendimento_scarica: float = Field(default=0.95, gt=0, le=1)
    timestep_partenza: Optional[int] = Field(default=None, description="Indice del timestep di partenza previsto, None se nessuna partenza nell'orizzonte")
    soc_minimo_alla_partenza_pct: float = Field(default=80.0, ge=0, le=100)
    disponibile: list[bool] = Field(description="True nei timestep in cui il veicolo e' collegato al caricatore")
    priorita: float = Field(default=1.0, gt=0, description="Peso relativo: piu' alto = il motore lo scarica meno volentieri")
    probabilita_utilizzo: float = Field(default=1.0, ge=0, le=1, description="0-1: piu' alta = riserva di SoC piu' prudente")
    costo_degrado_eur_kwh: float = Field(default=0.03, ge=0)
    potenza_caricatore_kw: float = Field(gt=0)
    v2g_capace: bool = Field(default=True, description="Se False, il veicolo puo' solo caricare, mai cedere energia")
    tipo_colonnina: str = Field(default="generico", description="Es. 'AC 22kW', 'DC 30kW' — veicoli con lo stesso tipo condividono il pool di punti fisici definito in punti_disponibili_per_tipo, invece di assumere un punto sempre dedicato")


class V2GDispatchRequest(BaseModel):
    durata_timestep_h: float = Field(default=1.0, gt=0)
    veicoli: list[V2GVehicleInput]
    carico_edificio_kw: list[float]
    produzione_fv_kw: list[float]
    prezzo_acquisto_eur_kwh: list[float]
    prezzo_vendita_eur_kwh: list[float]
    p_rete_max_kw: float = Field(gt=0)
    costo_potenza_impegnata_eur_kw: float = Field(default=0.0, ge=0)
    punti_disponibili_per_tipo: Optional[dict[str, int]] = Field(
        default=None,
        description=(
            "Es. {'AC 22kW': 3, 'DC 30kW': 1} — quanti punti fisici di ciascun tipo "
            "esistono davvero in sede. Se omesso, comportamento precedente: ogni "
            "veicolo assume un punto dedicato sempre disponibile (irrealistico se i "
            "veicoli superano i punti fisici installati)."
        ),
    )


class V2GVehiclePlanOut(BaseModel):
    vehicle_id: str
    carica_kw: list[float]
    scarica_kw: list[float]
    soc_pct: list[float]


class V2GDispatchResponse(BaseModel):
    successo: bool
    messaggio: str
    piani_veicolo: list[V2GVehiclePlanOut]
    prelievo_rete_kw: list[float]
    immissione_rete_kw: list[float]
    picco_kw: float
    costo_totale_eur: float
    costo_energia_eur: float
    costo_potenza_impegnata_eur: float
    costo_degrado_eur: float
    ricavo_vendita_eur: float


class V2GPartenza(BaseModel):
    """Una partenza programmata (es. inizio turno) durante l'orizzonte settimanale."""
    timestep: int = Field(description="Indice assoluto nell'orizzonte (0-based, in ore per una settimana oraria)")
    soc_minimo_pct: float = Field(default=80.0, ge=0, le=100)


class V2GVehicleWeeklyInput(BaseModel):
    """Un veicolo su un orizzonte multi-giorno: stessa semantica di V2GVehicleInput,
    ma con piu' partenze (una per ogni turno/giorno lavorativo) invece di una sola."""
    id: str
    capacita_kwh: float = Field(gt=0)
    soc_iniziale_pct: float = Field(ge=0, le=100)
    soc_min_pct: float = Field(default=10.0, ge=0, le=100)
    soc_max_pct: float = Field(default=100.0, ge=0, le=100)
    rendimento_carica: float = Field(default=0.95, gt=0, le=1)
    rendimento_scarica: float = Field(default=0.95, gt=0, le=1)
    disponibile: list[bool] = Field(description="True nei timestep in cui il veicolo e' collegato al caricatore, lunghezza = orizzonte totale")
    partenze: list[V2GPartenza] = Field(default_factory=list, description="Una per ogni turno/rientro previsto nell'orizzonte")
    priorita: float = Field(default=1.0, gt=0)
    probabilita_utilizzo: float = Field(default=1.0, ge=0, le=1)
    costo_degrado_eur_kwh: float = Field(default=0.03, ge=0)
    potenza_caricatore_kw: float = Field(gt=0)
    v2g_capace: bool = Field(default=True)
    tipo_colonnina: str = Field(default="generico", description="Vedi V2GVehicleInput.tipo_colonnina")


class V2GWeeklyDispatchRequest(BaseModel):
    durata_timestep_h: float = Field(default=1.0, gt=0)
    orizzonte_lookahead_timestep: int = Field(default=24, ge=1, description="Finestra di lookahead dell'MPC ad ogni passo (ore)")
    veicoli: list[V2GVehicleWeeklyInput]
    carico_edificio_kw: list[float]
    produzione_fv_kw: list[float]
    prezzo_acquisto_eur_kwh: list[float]
    prezzo_vendita_eur_kwh: list[float]
    p_rete_max_kw: float = Field(gt=0)
    costo_potenza_impegnata_eur_kw: float = Field(default=0.0, ge=0)
    punti_disponibili_per_tipo: Optional[dict[str, int]] = Field(default=None, description="Vedi V2GDispatchRequest.punti_disponibili_per_tipo")


class V2GWeeklyVehiclePlanOut(BaseModel):
    vehicle_id: str
    soc_pct: list[float]
    carica_kw_stimata: list[float] = Field(description="Carica netta stimata per intervallo, derivata dalla traiettoria SoC")
    scarica_kw_stimata: list[float] = Field(description="Scarica netta stimata per intervallo, derivata dalla traiettoria SoC")


class V2GWeeklyDispatchResponse(BaseModel):
    successo: bool
    messaggio: str
    n_timestep: int
    piani_veicolo: list[V2GWeeklyVehiclePlanOut]
    prelievo_rete_kw: list[float]
    immissione_rete_kw: list[float]
    picco_kw: float
    costo_totale_eur: float
    costo_energia_eur: float
    costo_potenza_impegnata_eur: float
    costo_degrado_eur: float
    ricavo_vendita_eur: float
    vincoli_partenza_rispettati: bool
    dettaglio_violazioni: list[str]


class ComplianceDM2025Request(BaseModel):
    """Verifica obblighi minimi DM 28/10/2025 per un parcheggio aziendale.

    NOTA: risultato best-effort, non un parere legale — vedi disclaimer nella risposta.
    """
    residenziale: bool = False
    accesso_pubblico: bool = Field(default=False, description="Rilevante solo se residenziale=False")
    posti_auto: int = Field(default=0, ge=0)
    tipo_intervento: str = Field(default="esistente", description="nuova_costruzione | ristrutturazione_importante | esistente")
    data_riferimento: Optional[str] = Field(default=None, description="ISO date, default oggi")

    pmi_proprietaria_e_occupante: bool = False
    permesso_costruire_ante_2021_03_10: bool = False
    costo_ricarica_pct_su_ristrutturazione: Optional[float] = Field(default=None, ge=0, le=100)
    microsistema_isolato_critico: bool = False
    edificio_pubblico_gia_conforme_dlgs257: bool = False

    # Opzionale: confronta con una configurazione hardware gia' dimensionata
    configurazione_da_verificare: Optional[HardwareConfig] = None
    catalogo_hardware: Optional[list[HardwareSpec]] = None


class ComplianceDM2025Response(BaseModel):
    esente: bool
    motivo_esenzione: Optional[str]
    canalizzazione_richiesta: bool
    canalizzazione_quota_posti: Optional[float]
    punti_tipologia_a_minimi_a_regime: int
    punti_tipologia_b_minimi_a_regime: int
    punti_tipologia_a_applicabili_oggi: int
    punti_tipologia_b_applicabili_oggi: int
    fase_transitoria: Optional[str]
    smart_charging_v1g_richiesto: bool
    registrazione_pun_richiesta: bool
    note_tecniche: list[str]
    fonti_da_verificare: list[str]
    disclaimer: str
    confronto_con_configurazione: Optional[dict] = None


class SiteScoringRequest(BaseModel):
    """Valutazione della qualità di un sito per infrastruttura di ricarica.

    Metodologia propria (non conformità letterale a una norma) — criteri e pesi
    completamente trasparenti, vedi il campo 'metodologia' nella risposta."""
    traffico_veicoli_giorno: float = Field(default=0.0, ge=0, description="Transito medio giornaliero stimato sulla strada adiacente")
    accesso_facile: bool = Field(default=True, description="Ingresso/uscita agevole per i veicoli (anche furgoni/mezzi pesanti se rilevante)")
    distanza_arteria_km: float = Field(default=1.0, ge=0, description="Distanza dalla strada principale/autostrada più vicina")
    densita_abitanti_km2: float = Field(default=0.0, ge=0, description="Densità di popolazione nell'area, abitanti/km²")
    densita_aziende_km2: float = Field(default=0.0, ge=0, description="Densità di aziende/uffici nell'area, per contesto B2B/flotte")
    n_servizi_300m: int = Field(default=0, ge=0, description="Bar, ristoranti, negozi, supermercati entro ~300m")
    potenza_disponibile_kw: float = Field(default=50.0, ge=0, description="Potenza di rete disponibile SENZA potenziamento — da verificare col gestore di rete")
    posti_parcheggio_disponibili: int = Field(default=10, ge=0, description="Posti auto disponibili nell'area per punti di ricarica + attesa")
    distanza_trasporto_pubblico_km: float = Field(default=1.0, ge=0, description="Distanza dalla fermata di trasporto pubblico più vicina")
    distanza_competitor_km: float = Field(default=2.0, ge=0, description="Distanza dal punto di ricarica pubblico esistente più vicino")
    visibilita: str = Field(default="media", description="'alta' | 'media' | 'bassa' — visibilità del sito da strada/passanti")
    e_deposito_aziendale: bool = Field(default=False, description="Se True, il risultato include una nota sul minor peso economico di alcuni criteri per un deposito ad uso esclusivo della flotta")
    pesi_personalizzati: Optional[dict[str, float]] = Field(
        default=None,
        description="Sovrascrive i pesi di default — devono sommare a 1.0. Chiavi: traffico, accessibilita, demografia, servizi, connessione_rete, parcheggio, trasporto_pubblico, distanza_competitor, visibilita",
    )


class CriterioScoreOut(BaseModel):
    nome: str
    punteggio_0_100: float
    peso: float
    contributo_ponderato: float
    spiegazione: str


class SiteScoringResponse(BaseModel):
    punteggio_totale_0_100: float
    grado: str = Field(description="Da 'A' (ottimo) a 'F' (scarso)")
    criteri: list[CriterioScoreOut]
    e_deposito_aziendale: bool
    nota_contesto: str
    metodologia: str = Field(
        default=(
            "Metodologia propria di y35/JoSa — non conformità letterale a una norma specifica. "
            "I nove criteri (traffico, accessibilità, demografia, servizi nell'area, connessione "
            "rete, parcheggio, trasporto pubblico, distanza competitor, visibilità) sono quelli "
            "comunemente richiamati nella pianificazione dell'infrastruttura di ricarica pubblica "
            "(es. DIN SPEC 91433, una linea guida di processo per l'identificazione di aree/siti — "
            "non un algoritmo di punteggio). Pesi e soglie qui sono documentati apertamente e "
            "regolabili, non un 'punteggio nascosto'."
        ),
    )
