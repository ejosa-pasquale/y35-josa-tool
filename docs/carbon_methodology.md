# Carbon Accounting: descrizione della metodologia

**Metodologia allineata alla ISO 14064-2, verificabile da terza parte.**
Versione metodologia 1.0.0 · libreria fattori `2026.10-2` · modulo `josa_core/carbon/`.

Questo documento descrive come il tool Y35 quantifica la riduzione delle emissioni GHG ottenuta elettrificando una flotta aziendale. È strutturato come la descrizione di progetto richiesta dalla ISO 14064-2 e va consegnato al verificatore insieme al pacchetto evidenze (`POST /api/v1/carbon/evidence-pack`).

## 1. Descrizione del progetto

Il progetto sostituisce veicoli a combustione (gasolio o benzina) con veicoli elettrici (BEV) o ibridi plug-in (PHEV), ricaricati in deposito, a casa dei dipendenti o su colonnine pubbliche. Il servizio fornito resta lo stesso: gli stessi km percorsi dagli stessi gruppi flotta.

- **Proponente**: l'azienda che gestisce la flotta.
- **Unità funzionale**: km percorsi dalla flotta nel periodo.
- **Gas considerati**: CO2. CH4 e N2O della combustione sono esclusi in modo conservativo (vedi §3); il modulo supporta fattori in CO2e e GWP AR5/AR6 per estensioni future.

## 2. Scenario di baseline

Lo scenario di baseline è la continuazione della flotta a combustione per gli stessi km. È lo scenario più plausibile in assenza del progetto, perché il veicolo sostituito è proprio quello in uso.

`BE = Σ_gruppi km × (1 / consumo_baseline km/L) × FE_combustibile × (1 − quota_biogenica)`

- `consumo_baseline`: `fuel.diesel_km_l`, oppure `baseline_km_l` del gruppo.
- `combustibile`: `carbon.baseline_combustibile`, oppure `baseline_combustibile` del gruppo.
- `quota_biogenica`: `carbon.quota_biocarburante_pct`, default 0 (vedi §7).
- **Km nell'ex-ante**: stesse regole del generatore eventi della simulazione. I km per gruppo sono `n_veicoli × km_per_giro × giri_per_veicolo_giorno × probabilità_utilizzo`, con un solo giro al giorno per i gruppi pendolari.

## 3. Confine di progetto (SSR)

| Sorgente / assorbitore / serbatoio | Scenario | Inclusa | Motivazione |
|---|---|---|---|
| Combustione carburante nei veicoli | baseline | sì (CO2) | Sorgente principale controllata. CH4/N2O < 1% allo scarico: escluderli riduce la baseline (conservativo). |
| Benzina dei PHEV oltre l'autonomia elettrica | progetto | sì | Sorgente controllata. |
| Elettricità di rete per la ricarica (deposito, casa, pubblica) | progetto | sì | Sorgente correlata. |
| Perdite di trasmissione e distribuzione | progetto | sì | Già comprese nel fattore ISPRA dei consumi elettrici. |
| Fotovoltaico in sito autoconsumato | progetto | sì | Emissioni operative nulle, solo per la quota dichiarata. |
| Well-to-tank di carburanti ed elettricità | entrambi | no (opzionale) | L'esclusione abbassa la baseline più del progetto: conservativa. Se si include, serve anche il fattore upstream dell'elettricità. |
| Produzione di veicoli e batterie | entrambi | no | Ciclo di vita, fuori dal perimetro operativo. |
| Costruzione dell'infrastruttura di ricarica | progetto | no | Una tantum, non significativa. |
| Refrigeranti A/C | entrambi | no | Equivalenti nei due scenari. |

La tabella è generata dal codice (`methodology.project_boundary`) e inclusa in ogni risultato.

## 4. Quantificazione delle emissioni di progetto

`PE = kWh_deposito_rete × FE_dep + kWh_casa × FE_rete + kWh_pubblica × FE_rete + litri_PHEV × FE_benzina`

| Fonte | Ex-ante (simulazione) | Ex-post (monitoraggio) |
|---|---|---|
| Deposito | `kpi.e_int` | contatore / export sessioni CPMS |
| Casa | `kpi.e_home_private` | wallbox o rimborsi |
| Pubblica | `kpi.e_ext` + `kpi.e_unserved` (l'energia non servita viene attribuita alla pubblica, scelta conservativa) | fatture o app dei CPO |
| Benzina PHEV | km oltre `autonomia_elettrica_km` × `consumo_benzina_l100km` | fuel card |

**Fattore di rete.**
- **Location-based (default)**: fattore ISPRA dei consumi elettrici, il più recente con stato *definitivo*. È un fattore di sola CO2 che include perdite di rete e import, quindi il modulo non aggiunge perdite.
- **Market-based**: la quota di deposito coperta da GO/PPA documentate vale zero. Il resto del deposito, la ricarica domestica e la pubblica usano il residual mix AIB, che va fornito dall'utente.
- **Profilo orario (opzionale, location-based)**: 24 fattori orari pesati sulla curva di ricarica del deposito.
- **FV in sito**: la quota dichiarata del prelievo di deposito vale zero, fino al massimo del prelievo stesso.

L'energia simulata deriva dal consumo dei veicoli (`consumo_kwh_km`). Se il dato è un consumo WLTP, è già misurato alla presa e comprende le perdite di ricarica.

## 5. Riduzione

`ER_lorda = BE − PE − LE`; `ER_netta = ER_lorda × (1 − incertezza_pct)`

- `LE` (leakage): default 0. La flotta sostituita esce dal parco dell'azienda e il modulo non attribuisce riduzioni per i veicoli rivenduti. È valorizzabile in `leakage_tco2e_anno`.
- `incertezza_pct`: sconto conservativo opzionale, da 0 a 50%.
- **Ex-ante**: giorno tipo × `giorni_operativi_anno` (default 365, come i calcoli economici del tool). È sempre etichettata come stima non verificata.
- **Ex-post**: totali per periodo misurato, più un totale complessivo calcolato con gli stessi fattori. Solo l'ex-post è adatto alla verifica da terza parte.

## 6. Fattori di emissione (libreria `factors_it.json`)

| ID | Valore | Fonte |
|---|---|---|
| `elettricita_it_consumi_2023` | 0,2347 kgCO2/kWh (definitivo, default) | ISPRA Rapporti 413/2025, Tab. 1.3, "Consumi elettrici" |
| `elettricita_it_consumi_2024p` | 0,1989 kgCO2/kWh (preliminare) | ISPRA Rapporti 413/2025, Tab. 1.3 |
| `gasolio_combustione` | 2,630 kgCO2/L | MASE, Tabella parametri standard nazionali 2022-2024 (fonte ISPRA, validi per il 2025), "Gasolio per autotrazione" 3,150 tCO2/t × 0,835 kg/L |
| `benzina_combustione` | 2,348 kgCO2/L | MASE, Tabella parametri standard nazionali 2022-2024, "Benzina senza piombo per autotrazione" 3,152 tCO2/t × 0,745 kg/L |
| `gasolio_wtt` | 0,626 kgCO2e/L | DESNZ 2025, methodology paper, Tab. 5 (17,5 kgCO2e/GJ) |
| `benzina_wtt` | 0,588 kgCO2e/L | DESNZ 2025, methodology paper, Tab. 5 (18,3 kgCO2e/GJ) |

**Aggiornamento.** Quando ISPRA pubblica una nuova edizione (es. Rapporti 430/2026 con il dato 2024 definitivo), si aggiunge la voce al file e si incrementa `versione`. Per un singolo calcolo si può usare `fattori_override`: il valore sostituito resta tracciato con stato `override_utente`, il valore originale e la fonte dichiarata. Un override non cambia quale fattore viene selezionato di default.

**Aggiornamento annuale.** La tabella MASE dei parametri standard viene ripubblicata ogni anno con la media degli ultimi tre anni di inventario. A ogni nuova edizione vanno aggiornate le due righe "per autotrazione" e la versione della libreria. Le densità (0,835 e 0,745 kg/L) sono valori tipici di norma: se il fornitore di carburante dichiara le proprie, quelle sono più precise.

## 7. Conservatività e punti aperti

- Esclusi CH4/N2O nella baseline e WTT di default: entrambe le scelte riducono la riduzione dichiarata.
- Energia non servita attribuita alla ricarica pubblica.
- Fattore di rete definitivo più recente, invece del preliminare più basso.
- **Quota biogenica del gasolio**: default 0%. Il gasolio commerciale contiene biodiesel, la cui CO2 è biogenica: con 0% la baseline è leggermente sovrastimata. Il modulo lo segnala tra le avvertenze. In una verifica va valorizzata con la quota dichiarata dal fornitore o con il dato nazionale dell'anno.

## 8. Piano di monitoraggio (ex-post)

| Parametro | Unità | Fonte dati | Frequenza | Controlli QA/QC |
|---|---|---|---|---|
| kWh deposito | kWh | contatore MID o export sessioni CPMS (OCPP) | mensile | sessioni duplicate, energia negativa o > 500 kWh per sessione scartate e riportate |
| kWh casa | kWh | wallbox con misura o rimborsi | mensile | coerenza kWh/km |
| kWh pubblica | kWh | fatture e app dei CPO | mensile | coerenza kWh/km |
| km per gruppo | km | telematica o odometri | mensile | gruppi mancanti o sconosciuti |
| Litri benzina PHEV | L | fuel card | mensile | — |

Controlli automatici (`monitoring.quality_checks`): periodi sovrapposti o con buchi, consumo medio fuori 0,10-0,60 kWh/km, gruppi mancanti o non presenti nella flotta, fonte dati non indicata.

**Import sessioni.** `POST /api/v1/carbon/sessions/import` accetta l'export transazioni dei CPMS OCPP (`transactionId`, `chargeBoxId`, `idTag`, `startTimestamp`, `stopTimestamp`, `meterStart`/`meterStop` in Wh) oppure un CSV semplice con una colonna energia in kWh o Wh. Le sessioni sono attribuite al giorno di inizio.

## 9. Riproducibilità e verifica (ISO 14064-3)

Il pacchetto evidenze contiene:
- input completo;
- ipotesi;
- fattori con fonte e URL;
- risultato con calcolo passo-passo (anche in CSV);
- confine SSR;
- versioni di metodologia, fattori e tool;
- hash SHA-256 di input e risultato.

Lo stesso input produce sempre lo stesso risultato e lo stesso hash: la simulazione ex-ante usa un seme fisso. Le istruzioni di verifica sono nel campo `istruzioni_verifica`.

## 10. Valore economico (separato dal TCO)

`carbon.prezzo_carbonio_modalita`:
- `ets2`: CO2 fossile da carburante evitata × prezzo, dall'anno `anno_inizio_ets2` (default 2028). È un costo di cassa evitato ma indiretto: l'ETS2 grava sui fornitori di carburante e si riflette sul prezzo alla pompa.
- `ombra` / `libero`: riduzione netta × prezzo. Valore non monetario, per decisioni e rendicontazione ESG.

Il valore è mostrato come voce separata e non entra nel NPV del TCO.

## 11. Dicitura

In UI, report ed export si usa solo la formula "Metodologia allineata alla ISO 14064-2, verificabile da terza parte", restituita dall'API nel campo `dicitura`. Un test (`test_carbon.py`, punto 14) fallisce se nel codice compaiono formule diverse da questa riferite alla ISO 14064.
