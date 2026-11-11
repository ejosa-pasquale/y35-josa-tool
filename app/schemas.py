"""
Schemi Pydantic per l'API josa_api.

Questi modelli sono il "contratto" tra il frontend (o qualunque sistema esterno,
Fleet Management/EMS/BMS/ERP) e il motore josa_core. Separarli dai modelli
interni del motore (josa_core.models) e' intenzionale: i due possono evolvere
a velocita' diverse — l'API deve restare stabile per i client esterni anche
se il motore interno cambia forma.
"""

from datetime import date, time
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


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
    n_veicoli: int = Field(ge=0, le=5000)
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
    # --- Carbon Accounting: baseline per gruppo (opzionale) ---
    baseline_km_l: Optional[float] = Field(
        default=None, gt=0,
        description=(
            "Consumo del veicolo a combustione che questo gruppo sostituisce (km/L), "
            "per la baseline del Carbon Accounting. None = usa fuel.diesel_km_l."
        ),
    )
    baseline_combustibile: Optional[str] = Field(
        default=None,
        description="'diesel' o 'benzina': combustibile del veicolo sostituito. None = quello delle ipotesi carbon.",
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
    tolleranza_tra_veicoli_min: float = Field(
        default=0.0, ge=0, le=240,
        description=(
            "Minuti tra la fine di una ricarica e l'inizio della successiva sulla stessa "
            "colonnina (spostare l'auto, collegare la prossima). 0 = colonnina subito "
            "libera (comportamento storico). La metodologia Y35 usa 60 min come valore "
            "prudente e 30 min quando il cambio auto e' organizzato."
        ),
    )
    carico_sede_kw: Optional[list[float]] = Field(
        default=None,
        description=(
            "Consumo della sede senza la flotta, kW medi per quarto d'ora da mezzanotte: "
            "96 valori (giorno tipo) oppure 672 (settimana da lunedi'). Con la potenza "
            "contrattuale il limite della ricarica in ogni quarto d'ora diventa "
            "min(p_shaving_kw, contratto - sede). Assente = comportamento storico."
        ),
    )
    potenza_contrattuale_kw: Optional[float] = Field(default=None, gt=0, le=100000,
        description="Potenza del contratto del POD (kW). Serve solo insieme a carico_sede_kw.")

    @model_validator(mode="after")
    def _sede_coerente(self):
        if self.carico_sede_kw is not None:
            if len(self.carico_sede_kw) not in (96, 672):
                raise ValueError("carico_sede_kw deve avere 96 valori (giorno) o 672 (settimana)")
            if any((x is None) or x < 0 or x > 100000 for x in self.carico_sede_kw):
                raise ValueError("carico_sede_kw: valori tra 0 e 100000 kW")
            if self.potenza_contrattuale_kw is None:
                raise ValueError("Con carico_sede_kw serve anche potenza_contrattuale_kw")
        return self


class FuelCostsIn(BaseModel):
    diesel_km_l: float = Field(default=10.0, gt=0)
    diesel_eur_l: float = Field(default=1.7, gt=0)
    staff_eur_h: float = Field(default=20.0, ge=0)


class PrezziFasceIn(BaseModel):
    """Prezzo dell'energia in sede per fascia oraria ARERA. Una fascia vuota usa il
    prezzo privato (stesso costo in tutte le ore)."""
    prezzo_f1_eur_kwh: Optional[float] = Field(default=None, ge=0, le=10, description="F1: lun-ven 8-19")
    prezzo_f2_eur_kwh: Optional[float] = Field(default=None, ge=0, le=10, description="F2: lun-ven 7-8 e 19-23, sabato 7-23")
    prezzo_f3_eur_kwh: Optional[float] = Field(default=None, ge=0, le=10, description="F3: notte 23-7, domenica e festivi")

    def come_dict(self) -> dict:
        return {"F1": self.prezzo_f1_eur_kwh, "F2": self.prezzo_f2_eur_kwh, "F3": self.prezzo_f3_eur_kwh}


class EnergyCosts(BaseModel):
    prezzo_privato_eur_kwh: float = Field(default=0.25, ge=0)
    prezzo_pubblico_eur_kwh: float = Field(default=0.55, ge=0)
    prezzo_domestico_eur_kwh: Optional[float] = Field(default=None, ge=0, description="Tariffa energia ricaricata a casa del dipendente (ricarica ibrida). Se non specificato, usa lo stesso prezzo della ricarica privata aziendale.")
    prezzi_fasce: Optional[PrezziFasceIn] = Field(default=None, description=(
        "Flessibilita' energetica: prezzi per fascia oraria F1/F2/F3 dell'energia in sede. "
        "Assente = stesso prezzo (prezzo_privato) in tutte le ore, risultati invariati."))

    def costi_motore(self) -> dict:
        """Dizionario costi per il simulatore. 'fasce' solo se indicate."""
        c = {"pri": self.prezzo_privato_eur_kwh, "pub": self.prezzo_pubblico_eur_kwh}
        if self.prezzi_fasce is not None:
            c["fasce"] = self.prezzi_fasce.come_dict()
        return c


MAX_PUNTI_PER_TIPO = 1000  # limite di sicurezza: il simulatore crea un oggetto per punto


class HardwareConfig(BaseModel):
    """Quantita' installate per tipo hardware, es. {'AC 22kW': 8, 'DC 30kW': 1}."""
    quantita: dict[str, int]

    @field_validator("quantita")
    @classmethod
    def _no_negative(cls, v):
        for k, q in v.items():
            if q < 0:
                raise ValueError(f"Quantita' negativa non ammessa per '{k}'")
            if q > MAX_PUNTI_PER_TIPO:
                raise ValueError(f"Quantita' oltre il massimo ({MAX_PUNTI_PER_TIPO}) per '{k}'")
        if len(v) > 50:
            raise ValueError("Troppi tipi di hardware nella configurazione (massimo 50)")
        return v


class CarbonAssumptionsIn(BaseModel):
    anno_fattori: Optional[int] = Field(default=None, description="Anno del fattore di rete. None = il piu' recente definitivo in libreria")
    baseline_combustibile: Literal["diesel", "benzina"] = "diesel"
    metodo_elettricita: Literal["location", "market"] = "location"
    quota_rinnovabile_deposito_pct: float = Field(default=0.0, ge=0, le=100, description="Quota di energia di deposito coperta da GO/PPA documentate (solo market-based)")
    fe_residual_mix_kg_kwh: Optional[float] = Field(default=None, ge=0, description="Residual mix AIB (kgCO2/kWh), obbligatorio con metodo market-based")
    fv_autoconsumo_kwh_anno: float = Field(default=0.0, ge=0, description="Energia da fotovoltaico in sito usata per la ricarica (kWh/anno)")
    profilo_orario_kg_kwh: Optional[list[float]] = Field(default=None, description="24 fattori orari (kgCO2/kWh) applicati alla curva di ricarica del deposito")
    includi_well_to_tank: bool = False
    fe_upstream_elettricita_kg_kwh: Optional[float] = Field(default=None, ge=0, description="Obbligatorio se includi_well_to_tank")
    quota_biocarburante_pct: float = Field(default=0.0, ge=0, le=100)
    leakage_tco2e_anno: float = Field(default=0.0, ge=0)
    incertezza_pct: float = Field(default=0.0, ge=0, le=50, description="Sconto conservativo applicato alla riduzione")
    giorni_operativi_anno: int = Field(default=365, ge=1, le=366)
    orizzonte_anni: int = Field(default=10, ge=1, le=30)
    anno_inizio: int = Field(default=2026, ge=2000, le=2100)
    prezzo_carbonio_modalita: Literal["nessuno", "ombra", "ets2", "libero"] = "nessuno"
    prezzo_carbonio_eur_t: float = Field(default=0.0, ge=0, le=2000)
    anno_inizio_ets2: int = Field(default=2028, ge=2025, le=2100)
    fattori_override: Optional[dict] = Field(default=None, description="{id_fattore: {valore, fonte, anno}} per aggiornare la libreria; tracciato nel pacchetto evidenze")

    @model_validator(mode="before")
    @classmethod
    def _campi_vuoti_al_predefinito(cls, data):
        # Una tendina non scelta arriva come "" (o null): si usa il valore predefinito
        if isinstance(data, dict):
            vuoti = {"anno_fattori", "baseline_combustibile", "metodo_elettricita", "prezzo_carbonio_modalita"}
            data = {k: v for k, v in data.items()
                    if not (k in vuoti and (v is None or (isinstance(v, str) and not v.strip())))}
        return data


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
    carbon: CarbonAssumptionsIn = Field(default_factory=CarbonAssumptionsIn, description="Ipotesi Carbon Accounting usate per il KPI co2 (riduzione netta)")
    gantt_settimanale: bool = Field(
        default=False,
        description=(
            "Se True, il campo gantt_veicoli nella risposta copre un'intera settimana "
            "(168h, Lun-Ven lavorativi) invece di un solo giorno tipo (24h). Il "
            "dimensionamento (CAPEX, copertura) non cambia in nessun caso — resta "
            "sempre calcolato sul giorno tipo singolo."
        ),
    )
    giornata: bool = Field(
        default=False,
        description=(
            "Se True, la risposta include 'giornata': il giorno tipo a passi da 15 minuti "
            "per rivederlo in animazione (kW per colonnina, presenze e SOC per veicolo, "
            "curva flotta e sede, limite, fasce F1/F2/F3). Solo lettura dei risultati: "
            "il dimensionamento non cambia."
        ),
    )


class ScenarioSensitivita(BaseModel):
    """Una variante della settimana tipo per la verifica di robustezza."""
    nome: str
    moltiplicatore_km: float = Field(default=1.0, gt=0)
    presenza_piena: bool = Field(default=False, description="Se True tutti i veicoli sono presenti ogni giorno")
    fattore_potenza: float = Field(default=1.0, gt=0, le=1.0, description="Quota della potenza di rete/peak shaving disponibile")
    punti_ac_guasti: int = Field(default=0, ge=0)


def _scenari_default() -> list[ScenarioSensitivita]:
    return [
        ScenarioSensitivita(nome="Base"),
        ScenarioSensitivita(nome="Km +50% (picco)", moltiplicatore_km=1.5),
        ScenarioSensitivita(nome="Presenza 100%", presenza_piena=True),
        ScenarioSensitivita(nome="Potenza -25%", fattore_potenza=0.75),
        ScenarioSensitivita(nome="1 punto AC guasto", punti_ac_guasti=1),
    ]


class WeeklyCheckRequest(SimulateRequest):
    """Verifica della configurazione su settimane lavorative simulate (metodologia Y35).

    Ogni giorno lavorativo ha una presenza diversa (profilo_presenza moltiplica la
    probabilita' di utilizzo dei gruppi) e un'estrazione casuale diversa. Il servizio
    e' giudicato con i criteri C1 (copertura media, sullo scenario base = il primo
    della lista) e C5 (copertura nel 5% di giorni peggiori, su ogni scenario)."""
    tolleranza_min: float = Field(default=60.0, ge=0, le=240,
        description="Tolleranza tra veicoli sulla stessa colonnina: 60 min prudente, 30 min con cambio auto organizzato")
    settimane: int = Field(default=10, ge=1, le=52)
    profilo_presenza: list[float] = Field(default_factory=lambda: [0.75, 1.10, 1.25, 1.10, 0.80],
        description="Fattore di presenza lun-ven rispetto alla media")
    scenari: list[ScenarioSensitivita] = Field(default_factory=_scenari_default)
    soglia_media_pct: float = Field(default=99.0, ge=0, le=100)
    soglia_giorni_peggiori_pct: float = Field(default=95.0, ge=0, le=100)


class ModelloVeicoloIn(BaseModel):
    """Canoni NLT reali per modello e powertrain (None = variante non disponibile)."""
    nome: str
    canone_ice_mese_eur: Optional[float] = Field(default=None, ge=0)
    canone_phev_mese_eur: Optional[float] = Field(default=None, ge=0)
    canone_bev_mese_eur: Optional[float] = Field(default=None, ge=0)
    consumo_ice_l100km: float = Field(default=6.0, gt=0)
    consumo_phev_kwh100km: float = Field(default=17.0, gt=0)
    consumo_phev_l100km: float = Field(default=6.0, gt=0)
    consumo_bev_kwh100km: float = Field(default=18.0, gt=0)


class TCOPowertrainRequest(BaseModel):
    """TCO mensile per modello a tre colonne (ICE / PHEV / BEV)."""
    modelli: list[ModelloVeicoloIn]
    km_anno: float = Field(gt=0)
    prezzo_carburante_eur_l: float = Field(gt=0)
    readiness_domestica_pct: float = Field(default=0.0, ge=0, le=100, description="Quota di ricarica a casa; puo' essere 0")
    copertura_aziendale_pct: float = Field(default=100.0, ge=0, le=100,
        description="Quota della ricarica non domestica fatta in sede (100 = infrastruttura aziendale, 0 = solo pubblica)")
    prezzo_domestico_eur_kwh: float = Field(default=0.26, ge=0)
    prezzo_aziendale_eur_kwh: float = Field(default=0.32, ge=0)
    prezzo_pubblico_eur_kwh: float = Field(default=0.69, ge=0)
    quota_elettrica_phev_pct: float = Field(default=70.0, ge=0, le=100)


class BusinessCaseRequest(BaseModel):
    """Payback e VAN dell'infrastruttura con rampa di adozione dei risparmi."""
    capex_per_anno_eur: list[float] = Field(min_length=1)
    risparmio_a_regime_eur: float
    anni: int = Field(default=5, ge=1, le=30)
    rampa_adozione: list[float] = Field(default_factory=lambda: [0.30, 0.90, 1.00])
    tasso_sconto_pct: float = Field(default=8.0, ge=0, le=30)  # = josa_core.utils.TASSO_SCONTO_DEFAULT
    capex_a_t0: bool = True


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
    giornata: Optional[dict] = Field(default=None, description="Giorno tipo a passi da 15 minuti per l'animazione 'Rivedi la giornata' (solo se richiesto)")


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
    carbon: CarbonAssumptionsIn = Field(default_factory=CarbonAssumptionsIn)


class RankedSolution(BaseModel):
    config: dict[str, int]
    kpi: dict
    capex_eur: float
    copertura_pct: float
    ammissibile: bool = Field(default=True, description="False se e' la migliore trovata entro budget ma non copre il 100% del fabbisogno aziendale")
    gap_kwh_da_coprire: Optional[float] = Field(default=None, description="kWh/giorno di fabbisogno aziendale ancora scoperti, se ammissibile=False")
    carbon: Optional[dict] = Field(default=None, description="Sintesi Carbon Accounting della soluzione (informativa: non entra nel ranking)")


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
    tasso_sconto_pct: float = Field(default=8.0, ge=0, le=30)  # = josa_core.utils.TASSO_SCONTO_DEFAULT
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
    carbon: Optional[dict] = Field(default=None, description="Sintesi Carbon Accounting dello scenario")


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
    carbon: CarbonAssumptionsIn = Field(default_factory=CarbonAssumptionsIn)


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


# Limiti di sicurezza del dispacciamento V2G (problema MILP: cresce con veicoli x timestep)
MAX_V2G_VEICOLI = 200
MAX_V2G_TIMESTEP = 2016


class V2GDispatchRequest(BaseModel):
    durata_timestep_h: float = Field(default=1.0, gt=0)
    veicoli: list[V2GVehicleInput] = Field(max_length=MAX_V2G_VEICOLI)
    carico_edificio_kw: list[float] = Field(max_length=MAX_V2G_TIMESTEP)
    produzione_fv_kw: list[float] = Field(max_length=MAX_V2G_TIMESTEP)
    prezzo_acquisto_eur_kwh: list[float] = Field(max_length=MAX_V2G_TIMESTEP)
    prezzo_vendita_eur_kwh: list[float] = Field(max_length=MAX_V2G_TIMESTEP)
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
    veicoli: list[V2GVehicleWeeklyInput] = Field(max_length=MAX_V2G_VEICOLI)
    carico_edificio_kw: list[float] = Field(max_length=MAX_V2G_TIMESTEP)
    produzione_fv_kw: list[float] = Field(max_length=MAX_V2G_TIMESTEP)
    prezzo_acquisto_eur_kwh: list[float] = Field(max_length=MAX_V2G_TIMESTEP)
    prezzo_vendita_eur_kwh: list[float] = Field(max_length=MAX_V2G_TIMESTEP)
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


class ParcheggiOsmRequest(BaseModel):
    """Parcheggi della sede da OpenStreetMap: indirizzo (o coordinate) e, se noto,
    il nome dell'azienda per riconoscere edificio/perimetro e parcheggi col suo nome."""
    indirizzo: Optional[str] = Field(default=None, max_length=300)
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)
    azienda: Optional[str] = Field(default=None, max_length=120)
    raggio_m: float = Field(default=250.0, ge=50, le=1000, description="Raggio di ricerca intorno alla sede")


class SediAziendaRequest(BaseModel):
    """Cerca su OpenStreetMap le sedi di un'azienda dal nome (e, se serve, dal comune)."""
    nome: str = Field(min_length=3, max_length=120)
    comune: Optional[str] = Field(default=None, max_length=120)
    indirizzo: Optional[str] = Field(default=None, max_length=300, description="Se l'azienda non e' su OSM: indirizzo della sede")


class SedeAzienda(BaseModel):
    id: str = Field(default="", max_length=60)
    nome: str = Field(default="", max_length=200)
    indirizzo: str = Field(default="", max_length=400)
    comune: str = Field(default="", max_length=120)
    provincia: str = Field(default="", max_length=120)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    categoria: str = Field(default="", max_length=40)
    tipo_osm: str = Field(default="", max_length=40)
    tipo_sede: Optional[str] = Field(default=None, max_length=20)
    e_lotto: bool = False
    area_m2: float = Field(default=0, ge=0)
    piani: Optional[int] = Field(default=None, ge=1, le=60)
    approssimata: bool = False
    origine: str = Field(default="osm", max_length=20)
    tipo_web: Optional[str] = Field(default=None, max_length=40)
    dipendenti_sede: Optional[int] = Field(default=None, ge=0, le=500000)
    superficie_coperta_m2: Optional[float] = Field(default=None, ge=0, le=5000000)


class ReportAziendaRequest(BaseModel):
    """Report informativo per una sede: minimo DM 28/10/2025 e punteggio del sito,
    con valori stimati dove mancano i dati e forbice dei risultati. I campi
    facoltativi correggono le stime e il report si ricalcola."""
    nome: str = Field(min_length=3, max_length=120)
    sede: SedeAzienda
    posti: Optional[int] = Field(default=None, ge=0, le=20000)
    tipo_intervento: Optional[str] = Field(default=None, description="esistente | ristrutturazione_importante | nuova_costruzione")
    pmi: Optional[bool] = Field(default=None, description="PMI proprietaria e occupante: True esente, False no, None non so")
    potenza_kw: Optional[float] = Field(default=None, ge=0, le=100000)
    superficie_m2: Optional[float] = Field(default=None, gt=0, le=5000000, description="Superficie coperta dell'edificio (m²), se nota")
    profilo_web: Optional[dict] = Field(default=None, description="Profilo dal web restituito dalla ricerca sedi")


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


# ============== CARBON ACCOUNTING ==============
# Metodologia allineata alla ISO 14064-2, verificabile da terza parte.
# La dicitura arriva sempre dal campo 'dicitura' della risposta.

class CarbonQuantifyRequest(SimulateRequest):
    """Stessi input di /simulate; 'carbon' contiene le ipotesi di quantificazione."""


class CarbonQuickRequest(BaseModel):
    """Stima rapida dalla home: pochi dati, nessuna simulazione a slot.

    La flotta e' descritta da numero di veicoli e km annui; lo scenario da quota
    elettrificata e quota di ricarica in azienda. Il calcolo e' quello del modulo
    Carbon Accounting (stessi fattori, stessa formula ER = BE - PE - LE).
    """
    n_veicoli: int = Field(ge=1, le=100_000)
    km_anno_veicolo: float = Field(default=25_000, gt=0, le=500_000)
    elettrificazione_pct: float = Field(default=100.0, ge=0, le=100, description="Quota della flotta sostituita con veicoli elettrici")
    ricarica_aziendale_pct: float = Field(default=70.0, ge=0, le=100, description="Quota dell'energia caricata in azienda (deposito)")
    ricarica_domestica_pct: float = Field(default=0.0, ge=0, le=100, description="Quota caricata a casa del dipendente; il resto e' ricarica pubblica")
    consumo_kwh_km: float = Field(default=0.22, gt=0, le=2)
    fuel: FuelCostsIn = FuelCostsIn()
    carbon: CarbonAssumptionsIn = Field(default_factory=CarbonAssumptionsIn)

    @model_validator(mode="after")
    def _quote(self):
        if self.ricarica_aziendale_pct + self.ricarica_domestica_pct > 100 + 1e-9:
            raise ValueError("ricarica_aziendale_pct + ricarica_domestica_pct non puo' superare 100")
        return self


class CarbonMonitoringPeriod(BaseModel):
    periodo_da: date
    periodo_a: date
    kwh_deposito: float = Field(ge=0)
    kwh_domestica: float = Field(default=0.0, ge=0)
    kwh_pubblica: float = Field(default=0.0, ge=0)
    km_per_gruppo: dict[str, float]
    litri_benzina_phev: float = Field(default=0.0, ge=0)
    fonte_dati: str = Field(default="", description="Es. 'CPMS', 'contatore MID', 'telematica', 'fuel card'")


class CarbonExPostRequest(BaseModel):
    gruppi: list[FleetGroup]
    fuel: FuelCostsIn = FuelCostsIn()
    carbon: CarbonAssumptionsIn = CarbonAssumptionsIn()
    periodi: list[CarbonMonitoringPeriod] = Field(min_length=1, max_length=120)


class CarbonSessionsImportRequest(BaseModel):
    csv_text: str = Field(max_length=20_000_000)
    periodo_da: Optional[date] = None
    periodo_a: Optional[date] = None


class CarbonEvidenceRequest(BaseModel):
    tipo: Literal["ex_ante", "ex_post"]
    ex_ante: Optional[CarbonQuantifyRequest] = None
    ex_post: Optional[CarbonExPostRequest] = None


# ---------------------------------------------------------------------------
# Diagnosi energetica della ricarica (sede + flotta)
# ---------------------------------------------------------------------------

MAX_CURVA_CSV_CHARS = 6_000_000  # ~ un anno di quarti d'ora con margine


class BollettaFasce(BaseModel):
    """Consumi di una bolletta per fascia ARERA (livello 1 della diagnosi)."""
    kwh_f1: float = Field(ge=0, le=1e8)
    kwh_f2: float = Field(ge=0, le=1e8)
    kwh_f3: float = Field(ge=0, le=1e8)
    giorni: int = Field(default=30, ge=1, le=366, description="Giorni del periodo fatturato")


class SedeInput(BaseModel):
    """Consumi della sede, dalla fonte migliore disponibile:
    curva quartoraria (CSV o valori) > bolletta per fasce > tipo di sede e picco dichiarato."""
    potenza_contrattuale_kw: float = Field(gt=0, le=100000, description="Potenza del contratto del POD (kW)")
    curva_csv: Optional[str] = Field(default=None, max_length=MAX_CURVA_CSV_CHARS,
                                     description="Testo del CSV quartorario del distributore o del fornitore")
    unita_curva: str = Field(default="auto", description="auto | kWh | Wh | kW")
    carico_sede_kw: Optional[list[float]] = Field(default=None, description="96 o 672 valori kW per quarto d'ora")
    bolletta: Optional[BollettaFasce] = None
    tipo_sede: str = Field(default="ufficio", description="ufficio | produzione_1_turno | produzione_2_turni | produzione_3_turni | logistica | commerciale")
    picco_sede_kw: Optional[float] = Field(default=None, gt=0, le=100000,
                                           description="Picco della sede dichiarato (solo se non ci sono documenti)")

    @field_validator("carico_sede_kw")
    @classmethod
    def _lunghezza(cls, v):
        if v is not None:
            if len(v) not in (96, 672):
                raise ValueError("carico_sede_kw deve avere 96 o 672 valori")
            if any(x < 0 or x > 100000 for x in v):
                raise ValueError("carico_sede_kw: valori tra 0 e 100000 kW")
        return v


class TariffeIn(BaseModel):
    """Voci di costo del picco. Vuote = valori ARERA di riferimento (dichiarati come ipotesi)."""
    contributo_aumento_potenza_eur_kw: Optional[float] = Field(default=None, ge=0, le=10000)
    quota_potenza_eur_kw_anno: Optional[float] = Field(default=None, ge=0, le=10000)
    costo_batteria_eur_kwh: Optional[float] = Field(default=None, ge=0, le=10000)


class V2GCheckIn(BaseModel):
    """Verifica V2G nella diagnosi: quanto vale far scaricare le auto verso la sede (V2B) o
    la rete, oltre alla sola ricarica gestita. Valori vuoti = ipotesi Y35 dichiarate."""
    attivo: bool = True
    gruppi_bidirezionali: Optional[list[str]] = Field(
        default=None, description="Gruppi con auto che supportano il V2G. Vuoto = gruppi elettrici puri (batteria >= 30 kWh)")
    punti_bidirezionali: Optional[int] = Field(
        default=None, ge=0, le=200, description="Colonnine bidirezionali. Vuoto = una per ogni auto idonea (fino a max_punti_calcolo)")
    potenza_punto_kw: float = Field(default=11.0, gt=0, le=350, description="Potenza della colonnina bidirezionale in carica e in scarica")
    extra_costo_punto_eur: float = Field(default=0.0, ge=0, le=200000,
        description="Costo in piu' di una colonnina bidirezionale rispetto a una normale. 0 = stesso costo (dato EV Field Service)")
    costo_degrado_eur_kwh: float = Field(default=0.03, ge=0, le=5, description="Degrado batteria per kWh scaricato")
    rendimento: float = Field(default=0.95, gt=0.5, le=1, description="Rendimento in carica e in scarica")
    prezzo_vendita_eur_kwh: float = Field(default=0.0, ge=0, le=5, description="0 = nessuna vendita alla rete, solo V2B")
    fv_aggiuntivo_kwp: float = Field(default=0.0, ge=0, le=100000,
        description="Fotovoltaico nuovo, non ancora nella curva del POD (quello esistente e' gia' dentro la curva)")
    giorni_feriali_anno: int = Field(default=250, ge=1, le=366)
    max_punti_calcolo: int = Field(default=20, ge=1, le=40, description="Limite di calcolo: auto ottimizzate insieme")


class DiagnosiRequest(SimulateRequest):
    """La flotta e la configurazione come in /simulate, piu' i consumi della sede."""
    sede: SedeInput
    tariffe: TariffeIn = Field(default_factory=TariffeIn)
    # Ogni feriale ha km diversi (fattore casuale entro +/- questa percentuale) oltre a
    # partenze, rientri e presenze estratti con un seme diverso per giorno.
    variazione_km_pct: float = Field(default=15.0, ge=0, le=50)
    # Mix AC/DC consigliato: l'ottimizzatore del dimensionamento con e senza i consumi della sede.
    ottimizza_mix: bool = True
    budget_max_eur: float = Field(default=500_000.0, gt=0)
    # Ricarica gestita (V1G) e verifica V2G sul giorno di riferimento.
    v2g: V2GCheckIn = Field(default_factory=V2GCheckIn)


class CurvaPodRequest(BaseModel):
    curva_csv: str = Field(max_length=MAX_CURVA_CSV_CHARS)
    unita_curva: str = "auto"


class ProfiloBollettaRequest(BaseModel):
    bolletta: BollettaFasce
    tipo_sede: str = "ufficio"


# ============== VERIFICA PREVENTIVO (PDF) ==============

class PreventivoPdfRequest(BaseModel):
    """PDF del preventivo in base64 (oppure il testo gia' estratto)."""
    pdf_base64: Optional[str] = Field(default=None, max_length=14_000_000,
        description="Contenuto del PDF codificato in base64 (max ~10 MB)")
    testo: Optional[str] = Field(default=None, max_length=400_000)


class PreventivoSedeIn(BaseModel):
    nome: str = Field(default="Sede", max_length=120)
    n_ac: int = Field(default=0, ge=0, le=100)
    kw_ac: float = Field(default=22.0, gt=0, le=50)
    n_dc: int = Field(default=0, ge=0, le=20)
    kw_dc: float = Field(default=60.0, gt=0, le=400)
    potenza_disponibile_kw: Optional[float] = Field(default=None, gt=0, le=10000)
    n_auto: Optional[int] = Field(default=None, ge=0, le=400,
        description="Auto elettriche che caricano in questa sede. Se manca, il totale viene ripartito in proporzione ai punti.")


class VerificaPreventivoRequest(BaseModel):
    """Verifica di un preventivo sulla flotta reale, nei due sensi:
    quante auto regge il preventivo e quale configurazione servirebbe per la flotta reale."""
    sedi: list[PreventivoSedeIn] = Field(min_length=1, max_length=6)
    n_auto_totali: int = Field(ge=1, le=600)
    km_giorno: float = Field(default=80.0, gt=0, le=1000)
    consumo_kwh_km: float = Field(default=0.18, gt=0, le=1.0)
    batteria_kwh: float = Field(default=70.0, gt=0, le=250)
    potenza_ac_auto_kw: float = Field(default=9.0, gt=0, le=22,
        description="Potenza massima che l'auto accetta in AC (caricatore di bordo)")
    pct_con_casa: float = Field(default=0.0, ge=0, le=100,
        description="Quota di auto con wallbox a casa (caricano in sede solo una parte del fabbisogno)")
    pct_ibride_plugin: float = Field(default=0.0, ge=0, le=100,
        description="Quota di ibride plug-in (12 kWh utili, 6,6 kW in AC, niente DC); il resto sono elettriche pure")
    presenza_pct: float = Field(default=90.0, gt=0, le=100)
    arrivo: time = Field(default=time(8, 30))
    partenza: time = Field(default=time(17, 30))
    rotazioni: list[int] = Field(default_factory=lambda: [1, 2, 4],
        max_length=4, description="Auto al giorno per punto AC da confrontare (1 = nessuno sposta l'auto)")
    rotazione_riferimento: int = Field(default=4, ge=1, le=8,
        description="Auto al giorno per punto AC su cui si giudica il preventivo (4 = standard del tool)")
    totale_imponibile_eur: Optional[float] = Field(default=None, ge=0)
    settimane: int = Field(default=4, ge=1, le=8)


# ============================================================================
# Progetto elettrico (relazione tecnica + schema unifilare in PDF)
# ============================================================================

class PuntoProgettoIn(BaseModel):
    """Un tipo di punto di ricarica da alimentare (dalla configurazione scelta da Y35)."""
    tipo: str = Field(description="Es. 'AC 22kW', 'DC 50kW'")
    potenza_kw: float = Field(gt=0, le=400)
    quantita: int = Field(ge=0, le=200)
    ac: Optional[bool] = Field(default=None, description="None = dedotto dal nome (DC se contiene 'DC')")
    fasi: Optional[Literal[1, 3]] = Field(default=None, description="None = monofase se AC fino a 7,4 kW, altrimenti trifase")
    lunghezza_m: Optional[float] = Field(default=None, gt=0, le=500, description="Distanza dal quadro QG-EV del punto più vicino")
    lunghezza_max_m: Optional[float] = Field(default=None, gt=0, le=500, description="Distanza del punto più lontano (lunghezze distribuite tra i due valori)")


class ZonaProgettoIn(BaseModel):
    """Una zona della sede con il proprio quadro di ricarica, alimentato da un quadro esistente diverso."""
    nome: str = Field(default="", max_length=80, description="Es. 'Parcheggio Ovest'")
    quadro_origine: str = Field(default="", max_length=120, description="Quadro esistente da cui parte la dorsale")
    punti: list[PuntoProgettoIn] = Field(min_length=1, max_length=20)
    lunghezza_dorsale_m: Optional[float] = Field(default=None, gt=0, le=1000)
    lunghezza_linee_m: Optional[float] = Field(default=None, gt=0, le=500)
    posa: Optional[Literal["interrata", "a_vista", "passerella"]] = None
    potenza_ev_kw: Optional[float] = Field(default=None, gt=0, le=100000, description="Limite DLM della zona; se omesso vale quello di sede")
    dlm: Optional[bool] = None


class ProgettoElettricoRequest(BaseModel):
    punti: list[PuntoProgettoIn] = Field(default_factory=list, max_length=20)
    zone: list[ZonaProgettoIn] = Field(default_factory=list, max_length=12,
                                       description="Zone con quadri e dorsali diverse; se vuoto, una sola zona con 'punti'")
    # dati di sito
    sistema: Literal["TT", "TN-S", "TN-C-S"] = "TT"
    potenza_disponibile_kw: Optional[float] = Field(default=None, gt=0, le=100000, description="Potenza del POD")
    potenza_ev_kw: Optional[float] = Field(default=None, gt=0, le=100000, description="Potenza assegnata alla ricarica (limite DLM)")
    dlm: Optional[bool] = Field(default=None, description="None = automatico (DLM se la potenza installata supera potenza_ev_kw)")
    lunghezza_dorsale_m: float = Field(default=15, gt=0, le=1000)
    lunghezza_linee_m: float = Field(default=25, gt=0, le=500)
    posa: Literal["interrata", "a_vista", "passerella"] = "interrata"
    cavo: str = Field(default="FG16OR16", max_length=30)
    k_correttivo: float = Field(default=0.8, gt=0.3, le=1.2)
    cosphi: float = Field(default=0.99, ge=0.7, le=1.0)
    dv_max_pct: float = Field(default=4.0, ge=1.0, le=8.0)
    ik_ka: float = Field(default=6.0, gt=0, le=50)
    ra_ohm: Optional[float] = Field(default=None, gt=0, le=10000, description="Resistenza di terra misurata (TT)")
    rdc_dd_integrato: bool = Field(default=True, description="Wallbox con rilevazione 6 mA DC integrata: differenziale tipo A, altrimenti tipo B")
    spd: bool = True
    # dati documento
    committente: str = Field(default="", max_length=200)
    indirizzo: str = Field(default="", max_length=300)
    oggetto: str = Field(default="", max_length=300)
    nome_progetto: str = Field(default="", max_length=200)
    cod_progetto: str = Field(default="", max_length=60)
    n_doc: str = Field(default="", max_length=60)
    revisione: str = Field(default="00", max_length=10)
    data: str = Field(default="", max_length=20)
    progettista_nome: str = Field(default="", max_length=120)
    progettista_blocco: str = Field(default="", max_length=600)
    impresa: str = Field(default="", max_length=400)
    fonte_dati: str = Field(default="Committente", max_length=120)
    luogo_firma: str = Field(default="", max_length=120)
    timbro_png_base64: Optional[str] = Field(default=None, max_length=3_000_000)
    documento: Literal["completo", "relazione", "unifilare"] = "completo"
    fase: Literal["tipico", "esecutivo"] = Field(default="esecutivo", description="tipico = preliminare con valori di sito standard")
    da_tipico: bool = Field(default=False, description="Esecutivo che completa un tipico: revisioni 00 (tipico) e 01 (esecutivo)")
    link_completa: Optional[str] = Field(default=None, max_length=4000, pattern=r"^https?://\S+$",
                                         description="Link 'Completa il progetto' stampato come QR nel tipico")

    @model_validator(mode="after")
    def _punti_o_zone(self):
        if not self.punti and not self.zone:
            raise ValueError("Indica i punti di ricarica (punti) oppure le zone (zone).")
        return self
