"""Costanti di metodologia: versione, dicitura, confine di progetto (SSR).

La dicitura e' l'unica formula ammessa per descrivere la metodologia in UI,
report ed export. Non va mai riscritta a mano altrove: si importa da qui.
"""

METODOLOGIA_VERSIONE = "1.0.0"

DICITURA_METODOLOGIA = "Metodologia allineata alla ISO 14064-2, verificabile da terza parte"

# GWP a 100 anni (IPCC). Usati solo per fattori con gas separati (CH4, N2O);
# i fattori attuali in libreria sono gia' in CO2 o CO2e.
GWP = {
    "AR5": {"CO2": 1.0, "CH4": 28.0, "N2O": 265.0},
    "AR6": {"CO2": 1.0, "CH4": 29.8, "N2O": 273.0},
}


def project_boundary(includi_wtt: bool, ha_phev: bool, metodo_elettricita: str) -> list[dict]:
    """Tabella SSR (sorgenti, assorbitori, serbatoi) inclusi ed esclusi, con motivazione."""
    rows = [
        {
            "ssr": "Combustione carburante nei veicoli (scarico)",
            "scenario": "baseline",
            "incluso": True,
            "gas": ["CO2"],
            "motivazione": "Sorgente principale controllata dal proponente. CH4 e N2O esclusi: "
                           "inferiori all'1% delle emissioni allo scarico, escluderli riduce la baseline (scelta conservativa).",
        },
        {
            "ssr": "Combustione benzina dei veicoli ibridi plug-in oltre l'autonomia elettrica",
            "scenario": "progetto",
            "incluso": True,
            "gas": ["CO2"],
            "motivazione": "Sorgente controllata dal proponente." + ("" if ha_phev else " Nessun ibrido plug-in nella flotta: contributo nullo."),
        },
        {
            "ssr": "Produzione dell'elettricita' prelevata dalla rete per la ricarica (deposito, casa, pubblica)",
            "scenario": "progetto",
            "incluso": True,
            "gas": ["CO2"],
            "motivazione": ("Sorgente correlata. Metodo location-based con fattore ISPRA dei consumi elettrici."
                            if metodo_elettricita == "location" else
                            "Sorgente correlata. Metodo market-based: GO/PPA documentate a zero, residuo con fattore residual mix."),
        },
        {
            "ssr": "Perdite di trasmissione e distribuzione",
            "scenario": "progetto",
            "incluso": True,
            "gas": ["CO2"],
            "motivazione": "Gia' comprese nel fattore ISPRA dei consumi elettrici: non vengono aggiunte di nuovo.",
        },
        {
            "ssr": "Elettricita' da fotovoltaico in sito autoconsumata",
            "scenario": "progetto",
            "incluso": True,
            "gas": ["CO2"],
            "motivazione": "Emissioni operative nulle; conteggiata solo per la quota dichiarata e misurata.",
        },
        {
            "ssr": "Estrazione, raffinazione e trasporto dei carburanti; produzione a monte dei combustibili per l'elettricita' (well-to-tank)",
            "scenario": "baseline e progetto",
            "incluso": bool(includi_wtt),
            "gas": ["CO2e"],
            "motivazione": ("Incluso su richiesta, con fattore upstream dell'elettricita' fornito dall'utente."
                            if includi_wtt else
                            "Escluso: la baseline si riduce piu' del progetto, quindi l'esclusione e' conservativa."),
        },
        {
            "ssr": "Produzione di veicoli e batterie",
            "scenario": "baseline e progetto",
            "incluso": False,
            "gas": [],
            "motivazione": "Emissioni di ciclo di vita fuori dal perimetro operativo del progetto.",
        },
        {
            "ssr": "Costruzione e installazione dell'infrastruttura di ricarica",
            "scenario": "progetto",
            "incluso": False,
            "gas": [],
            "motivazione": "Emissione una tantum non significativa rispetto alle emissioni operative sull'orizzonte del progetto.",
        },
        {
            "ssr": "Gas refrigeranti dei climatizzatori",
            "scenario": "baseline e progetto",
            "incluso": False,
            "gas": [],
            "motivazione": "Equivalenti nei due scenari.",
        },
    ]
    return rows
