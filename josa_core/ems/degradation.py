"""
josa_core.ems.degradation — stima del costo di degrado batteria per kWh ciclato.

Approccio deliberatamente semplice (costo lineare per kWh), non un modello
elettrochimico: e' sufficiente perche' il motore di ottimizzazione lineare
"impari" a scaricare un veicolo solo quando il beneficio economico supera
il costo di usura, senza bisogno di regole scritte a mano. Un modello piu'
sofisticato (dipendenza da profondita' di scarica, temperatura, ecc.) puo'
sostituire questa funzione senza cambiare il motore di dispacciamento, che
tratta il costo di degrado come un numero in EUR/kWh qualunque sia la sua origine.
"""


def costo_degrado_da_sostituzione(
    costo_sostituzione_batteria_eur: float,
    capacita_kwh: float,
    cicli_vita_attesi: int,
    profondita_scarica_tipica_v2g_pct: float = 30.0,
) -> float:
    """Stima il costo di degrado in EUR/kWh ciclato in scarica.

    Logica: il costo di sostituzione della batteria, diviso per l'energia totale
    che ci si aspetta di poter ciclare nella sua vita utile (cicli attesi x
    capacita' x profondita' di scarica tipica), da' un costo per kWh ciclato.
    Questo numero entra linearmente nella funzione obiettivo del motore di
    dispacciamento (vedi dispatch.py) come costo per ogni kWh scaricato in V2G.

    Esempio: batteria da 75 kWh, costo sostituzione 8.000 EUR, 3.000 cicli attesi
    a profondita' di scarica tipica 30% -> costo ~ 8000 / (3000 * 75 * 0.30) = 0.119 EUR/kWh.

    Nota: e' una stima prudenziale, non un valore certificato dal produttore —
    va aggiornato se/quando si hanno dati reali di garanzia batteria per il
    modello di veicolo specifico.
    """
    if cicli_vita_attesi <= 0 or capacita_kwh <= 0:
        raise ValueError("cicli_vita_attesi e capacita_kwh devono essere positivi")
    energia_totale_ciclabile_kwh = cicli_vita_attesi * capacita_kwh * (profondita_scarica_tipica_v2g_pct / 100.0)
    if energia_totale_ciclabile_kwh <= 0:
        raise ValueError("energia totale ciclabile non valida")
    return float(costo_sostituzione_batteria_eur / energia_totale_ciclabile_kwh)
