# Changelog

## Report dal nome dell'azienda e DM 28/10/2025 sul testo ufficiale (2026-11-11)

- **Calcolo DM 28/10/2025 corretto sul testo ufficiale** (GU n. 283 del 5/12/2025, Allegato 1, Cap. 6,
  Tabelle 4 e 5), prima basato su sintesi secondarie. Parcheggio privato esistente: 3 punti A ogni 20 posti
  (prima 1); oltre 100 posti 3 (privato) o 2 (pubblico) ogni 50 più punti Tipologia B per fascia (privato:
  1 B 101-500, 2 B 501-1000, 3 B oltre 1000; prima B solo sopra 500); 11-20 posti per nuove costruzioni e
  ristrutturazioni (prima 0). Fase transitoria degli esistenti: 50% arrotondato per difetto dei valori della
  tabella (lettura alternativa sul totale nelle note). Nel confronto con una configurazione un DC sotto 50 kW
  non conta come Tipologia B e una presa sotto 7,4 kW non conta. Esempio: 55 posti esistenti 9 A a regime,
  3 oggi (prima 3 e 1). Casi A/B/C invariati (non usano il DM).
- **Report dal nome dell'azienda** in Valuta il sito (`josa_core/report_azienda.py`,
  `POST /api/v1/site-scoring/sedi-azienda`, `POST /api/v1/site-scoring/report-azienda`): sedi trovate su
  OpenStreetMap, poi minimo DM (oggi e 2030) per i tre tipi di intervento e per posti bassi/centrali/alti, e
  punteggio del sito con forbice. Ogni dato ha la fonte (trovato su OSM, stimato, ipotesi, tuo dato); dove
  manca si usa una stima dichiarata (posti dall'edificio: superficie, addetti, auto; traffico e visibilità
  dal tipo di strada; densità dal comune; potenza 50 kW, forbice 20-250). Posti, tipo di intervento, PMI e
  potenza si correggono e il report si ricalcola; i valori si copiano nei campi del modulo. Se l'azienda non è su OpenStreetMap (tipico delle PMI) si scrive
  l'indirizzo della sede e il report parte da lì, con la posizione segnalata come da confermare. Logica DM e
  punteggio usate così come sono. frontend_size atteso 548230.
- **Conta i posti sulla foto aerea** (nel report): foto aerea della sede, l'utente disegna il contorno dei
  parcheggi e i posti (area / 25 m²) entrano nel report. Nessuna analisi automatica delle immagini. Sorgente
  in `GET /api/v1/site-scoring/foto-aerea`: variabile MAPBOX_TOKEN (token pubblico, satellite Mapbox, ricalco
  consentito) oppure FOTO_AEREA_URL ({z}/{x}/{y}) + FOTO_AEREA_ATTRIBUZIONE; senza variabili il riquadro non compare.

## Progetto elettrico dalla home (2026-11-10)

- Nuovo strumento in home "Progetto elettrico e unifilare": apre lo stesso modulo del progetto
  esecutivo con in più la sezione "Punti di ricarica" (quantità per AC 7,4/11/22 kW, DC 30/50/60 kW e una
  potenza libera AC o DC), così il progetto si fa anche senza aver prima dimensionato la flotta.
  Zone, DLM, link per il progettista e PDF funzionano come dai risultati. Aperto dai risultati, il modulo
  resta com'era (sezione manuale nascosta). frontend_size atteso 523246.

## Progetto elettrico e schema unifilare in PDF (2026-11-10)

- **Dimensionamento delle linee** (`josa_core/progetto_elettrico/calcoli.py`, `POST /api/v1/progetto-elettrico/dimensiona`):
  dai punti scelti da Y35 calcola dorsale dal quadro esistente, quadro QG-EV e una linea dedicata per ogni
  punto (CEI 64-8 sez. 722): Ib, In, sezione con Iz da CEI-UNEL 35024/1-35026 x K, caduta di tensione
  (dorsale + linea), PE (tab. 54F), cortocircuito I²t ≤ K²S², differenziale 30 mA tipo A/B, SPD tipo 2.
  Con limite DLM la dorsale si dimensiona sul limite, senza sulla somma delle potenze (contemporaneità 1).
  Wallbox monofase ruotate sulle fasi, verifica TT Ra·Idn ≤ 50 V, avvisi da confermare in sopralluogo.
- **PDF** (`POST /api/v1/progetto-elettrico/pdf`): relazione tecnico-specialistica A4 (struttura e testi
  della libreria RelazioneTecnicaEV, ripuliti: paragrafi duplicati tolti, norme aggiornate a CEI 64-8 vigente,
  CEI EN IEC 61851-1:2019, AFIR) + tavola E01 schema unifilare A3 vettoriale, su più fogli se servono.
  Font DejaVu incluso per i simboli (Δ, φ, ≤). Nuova dipendenza: reportlab.
- **Zone**: punti in aree diverse alimentate da quadri esistenti diversi. Ogni zona ha dorsale, quadro
  QG-EV1/QG-EV2/… e tavola unifilare (E01, E02, …) propri; posa e limite DLM per zona, oppure limite unico di
  sede (ogni dorsale dimensionata per ricevere l'intero limite, avviso per il controllore coordinato).
  Riepilogo zone e controllo del totale rispetto al POD nella relazione.
- **Tipico preliminare e progetto esecutivo**: a fine dimensionamento (percorso completo e chat, per sede)
  il bottone "Tipico elettrico" emette subito un PDF con dicitura "non valido per l'esecuzione", elenco dei
  valori di sito assunti e QR/link "Completa il progetto". Il link porta la configurazione dentro l'URL
  (nessun dato salvato sul server) e apre il modulo precompilato per il progettista/installatore, che emette
  l'esecutivo con revisioni 00 (tipico) e 01 (esecutivo). Unifilare: partenze distribuite in modo uniforme
  tra i fogli.
- **Interfaccia**: bottone "Progetto elettrico + unifilare (PDF)" nei risultati, con dati di sito, progettista
  (salvato sul dispositivo) e timbro PNG facoltativo.
- Test: `python3 test_progetto_elettrico.py` (calcoli, DLM, TT, Caso A dall'ottimizzatore, endpoint).

## Verifica di un preventivo caricato in PDF (2026-11-09)

- **Lettura del PDF** (`POST /api/v1/preventivo/leggi`, `josa_core/preventivo.py`): estrae dal testo
  del preventivo sedi, punti AC/DC con la potenza, potenza disponibile al quadro, predisposizione e
  totale imponibile. Parser a regole (niente AI): sezioni per sede, tabella con una colonna per sede
  o riepilogo unico. I dati si rivedono sempre a mano prima della verifica; un PDF scansionato
  (senza testo) viene segnalato.
- **Verifica nei due sensi** (`POST /api/v1/preventivo/verifica`): per ogni sede, quante auto regge
  il preventivo e quanti punti AC e quanta potenza servirebbero per le auto reali. Criterio: la
  verifica settimanale gia' esistente (base, presenza 100%, un punto AC guasto), con ricerche sul
  numero di auto, di punti e sulla potenza. Il risultato cambia con quante auto al giorno usa un
  punto AC (1 = nessuno sposta l'auto), mostrato in tabella. Esito sovradimensionato / adeguato /
  sottodimensionato e spiegazione in parole.
- **Raccomandazione Y35 con DLM**: per ogni sede i punti AC minimi e il limite DLM minimo con cui
  tutte le auto ripartono cariche anche con il 50% di km in piu', presenza piena o un punto guasto.
- **Schermata**: card "Carica il preventivo in PDF" in cima a "Verifica un preventivo". La verifica
  manuale resta com'e'. Motore invariato: Casi A/B/C identici.

## Ricarica gestita e check V2G nella diagnosi (2026-11-09)

- **Ricarica gestita: quanto vale** (`ricarica_gestita` nella risposta di `/api/v1/diagnosi`). Sul
  giorno di riferimento, con la curva vera della sede, quattro simulazioni del motore con la stessa
  flotta e le stesse colonnine: senza gestione (ogni auto a piena potenza), limite fisso (come oggi),
  gestita sulla sede fino al contratto (il «limite variabile» gia' calcolato) e gestita al picco
  minimo (il tetto piu' basso con cui le auto ripartono cariche come con il contratto pieno, ricerca
  binaria sul motore). Risultato: kW di picco tolti, aumento di potenza evitato, risparmio una tantum
  (contributo ARERA) e annuo (quota potenza), meno l'energia che resterebbe fuori dalla sede.
- **Check V2G** (`v2g`): l'ottimizzatore di dispacciamento gia' presente (`josa_core/ems/dispatch.py`)
  lanciato due volte sulla stessa giornata, auto sulle colonnine bidirezionali che possono solo
  caricare oppure anche scaricare. Il confronto e' con la ricarica gestita al picco minimo, quindi
  il V2G vale solo quello che la gestione della ricarica non fa gia'. Ogni auto riparte con l'energia
  che le da' il motore. Esito (conviene / al limite / non conviene), risparmio per voce (picco,
  energia per fasce, vendita, degrado), payback dell'extra costo, orari di scarica e ricarica,
  numero di colonnine bidirezionali che si ripaga prima, leve con il loro effetto (fotovoltaico,
  vendita alla rete, hardware -30%, degrado dimezzato).
- **Input facoltativi** (`DiagnosiRequest.v2g`): gruppi bidirezionali (default: elettriche pure),
  numero di colonnine bidirezionali (default: Y35 sceglie), potenza, extra costo (default 0: la
  colonnina bidirezionale costa come una normale, dato EV Field Service), degrado, rendimento, prezzo di vendita (default nessuna vendita),
  fotovoltaico nuovo, `attivo=false` per spegnere il check.
- **Pagina /diagnosi**: campi facoltativi prezzi F1/F2/F3 e V2G, due nuove sezioni nei risultati.
- **Coerenza**: nessuna modifica al motore. Con il check spento tutte le voci della diagnosi sono
  identiche; con tetto = contratto la simulazione coincide con il «limite variabile». Tutti i test
  esistenti passano (Caso A, B, C compresi), nuovo `test_v2g_check.py`.

## Rivedi la giornata in sede (2026-11-05)

- **Pulsante "▶ Rivedi la giornata in sede"** sotto il titolo dei risultati (percorso completo e
  chatbot): anima il giorno tipo della configurazione consigliata ogni 15 minuti. Colonnine con i kW
  erogati e l'auto collegata, flotta con livello batteria e stato (fuori sede, in carica, carica
  completata), contatori (in sede, in carica, ricaricate, potenza, picco, energia), curva di ricarica
  contro la potenza disponibile con le fasce F1/F2/F3. Con la curva della sede (diagnosi) mostra
  sede + flotta contro il contratto. Play/pausa, velocità 1×/2×/4×, cursore per scorrere le ore.
- **API**: `SimulateRequest.giornata = true` aggiunge `giornata` alla risposta di `/api/v1/simulate`
  (96 slot: kW per colonnina, presenze, guide, sessioni con SOC all'aggancio, curva flotta e sede,
  limite, fasce). Il motore registra solo i kW per slot di ogni sessione: dimensionamento, KPI e
  Gantt identici (Caso A e B verificati, `test_giornata.py`).
- **Bug corretti**: errore JS "Cannot access 'top' before initialization" nel percorso completo;
  Gantt settimanale in errore 500 (argomento `tolleranza_min` non previsto), che impediva anche il
  Gantt per colonnina.

## Flessibilità energetica per fascia oraria (2026-11-04)

- **Prezzi F1/F2/F3 opzionali** (`EnergyCosts.prezzi_fasce`, calendario ARERA in
  `josa_core/fasce_orarie.py`). Senza prezzi per fascia (default) si assume lo stesso costo in tutte
  le ore e nessun numero cambia (verificato su Caso A, B, C e su tutti i test).
- Con i prezzi per fascia l'energia caricata in sede costa il prezzo medio pesato sulla curva di
  ricarica simulata: risparmio, TCO e break-even usano questo prezzo.
- Nuovo blocco `kpi.flessibilita`: kWh al giorno e quota per fascia, costo con le fasce rispetto al
  prezzo unico, kWh spostabili e risparmio teorico spostando ogni ricarica nelle ore più economiche
  della sua sosta (stessa potenza media, senza ricontrollare il limite della sede).
- Interfaccia: domanda "Il prezzo dell'energia in sede cambia nelle fasce orarie?" nella tappa
  ipotesi finanziarie (default "No") e riquadro nei risultati. Chatbot: i prezzi per fascia si
  chiedono dentro la domanda sui prezzi; il riepilogo mostra la ripartizione per fascia.

## Nuovo titolo della home (2026-10-29)

- Il titolo "Quanto costa ricaricare la tua flotta oggi?" diventa "Elettrificare la flotta è una
  scelta di libertà sostenibile. Farla bene è una questione di numeri.", con il sottotitolo
  "Colonnine, potenza, CAPEX e ritorno dell'investimento, calcolati sui consumi reali della tua sede."

## Diagnosi energetica della ricarica e fondamenta (2026-10-28)

La flotta simulata da Y35 adesso si vede dentro i consumi reali della sede.

- **Curva della sede nel DLM**: `EnginePolicy.carico_sede_kw` (96 o 672 valori da 15 minuti) e
  `potenza_contrattuale_kw`. Con la curva il limite della ricarica in ogni quarto d'ora è
  min(p_shaving, contratto - sede). Senza curva il motore dà esattamente gli stessi risultati
  (verificato su Caso A e Caso B e su tutti i test esistenti).
- **Diagnosi** (`josa_core/diagnosi.py`, `app/diagnosi.py`, `/api/v1/diagnosi`): profilo della sede
  da curva POD (CSV lungo o largo, kWh o kW, convenzione 00:15-24:00), da bolletta per fasce o dal
  picco dichiarato, con il livello di affidabilità dichiarato. Restituisce capacità libera, finestre
  critiche, energia extra, limite energetico teorico dei veicoli, costo del picco e tre alternative
  (limite variabile, aumento di potenza, batteria) con le tariffe ARERA di riferimento come ipotesi.
- **Scarica e ricarica giorno per giorno**: ogni feriale è simulato a sé (presenze, partenze e giri
  con un seme per giorno, km entro ±`variazione_km_pct`, default 15%) e sommato alla sede di quel
  giorno. Il giorno di riferimento è quello con il picco totale più alto; la risposta ha `giorni`
  e `settimana_flotta_kw`. `run_simulate` accetta `seed` e `giorno` (default invariati).
- **Mix AC/DC nella diagnosi**: l'ottimizzatore del dimensionamento gira senza e con il limite
  della sede (giorno di riferimento); il mix consigliato è verificato su tutti i feriali e, se
  diverso da quello inserito, entra tra le alternative con la differenza di CAPEX. Campi
  `ottimizza_mix` e `budget_max_eur`; risposta `mix`. Le alternative sono ordinate: prima quelle
  che tengono la copertura di oggi, poi la più economica su 5 anni.
- **Energia non caricata nelle alternative**: con il solo DLM la potenza non si supera mai, ma chi
  esce prima di aver finito parte non pieno. Quei kWh sono valorizzati come nel TCO del tool
  (ricarica pubblica + tempo del personale) e ogni alternativa ha il totale a 5 anni; sono
  ordinate su quello e l'esito segue la più economica.
- **Ottimizzatore, correzione**: nello score le metriche di servizio (copertura, kWh scoperti,
  energia esterna) sono arrotondate a 0,1. Prima differenze da virgola mobile (1e-13 kWh) tra
  configurazioni con lo stesso servizio passavano davanti al costo: su una flotta di prova
  sceglieva 26 AC invece di 11. I casi A/B e i test esistenti danno gli stessi risultati.
- **Pagina `/diagnosi`** nel tool, con le tabelle "Infrastruttura di ricarica" e "Giorno per giorno", e il riquadro "Diagnosi energetica della sede" nella home (tre ingressi: domande, bolletta, curva POD).
- **Fondamenta**: nessuna chiave dei token fissa nel codice, registrazione email ripulita e limitata,
  limiti di frequenza e dimensione sulle chiamate a Claude, limiti sugli input pesanti, time limit del
  solver V2G, nessun traceback nelle risposte, `frontend/` copiato nell'immagine Docker.

## Chatbot allineato al percorso completo (2026-10-11)

Dalla chat ora si ottengono gli stessi numeri del calcolo completo calibrato sui casi reali.
Il motore non cambia: cambia solo la traduzione dal profilo conversazionale agli input.

- **Wallbox domestiche fuori dal catalogo della sede**: contano solo come readiness domestica.
  Prima l'ottimizzatore le sceglieva come colonnine di sede perché costavano meno di un punto AC.
- **Mix PHEV/BEV**: il chatbot chiede quanti ibridi plug-in e quante elettriche ci sono e crea
  due gruppi con i parametri calibrati (PHEV 12 kWh utili, 0,17 kWh/km, 6,6 kW, niente DC;
  BEV 70 kWh, 0,18 kWh/km, 9 kW). "Ibrido" non è più un profilo d'uso: resta Office.
- **Stesso calcolo calibrato** (`app/chat_analysis.py`): ottimizzatore AC-first, poi verifica
  settimanale con 60 min di tolleranza (minimo numero di punti AC che la supera), business case
  con rampa 30/90/100% e VAN al 10%, TCO per modello se ci sono i canoni.
- **Niente valori nascosti**: senza budget non c'è vincolo (prima 30.000 €), la potenza
  disponibile è obbligatoria (prima 50 kW), i prezzi mancanti usano le ipotesi standard Y35 e
  compaiono nella lista "ipotesi" dei risultati. Punto AC 2.500 €, punto DC 30-50 kW 18.000 €.
- **Più sedi in una conversazione** (`sedi` nel profilo), con riepilogo per sede, totale di gruppo
  e dettaglio di ogni sede nella tappa Progetto.
- `test_chat.py`: regole sopra più casi A e B dal chatbot contro il percorso completo.
- **Orario di partenza**: il chatbot chiede se di notte le auto restano in sede. Se vanno via
  (default per le auto dei dipendenti) la fine della finestra è un limite vero: la ricarica in sede
  finisce entro quell'ora. Prima, senza ricarica a casa, il motore lasciava le auto in sede anche di
  notte e le ricariche proseguivano dopo l'uscita. Lo stesso vale per il gruppo "Dipendenti" del percorso guidato.
- **Curva di potenza** (`josa_core/simulation.py`, `_scheduler_dlm.py`): la ricarica pianificata dal DLM
  ora entra nella curva e nel picco (prima veniva calcolata ma non sommata: picco 0 kW con finestra
  rigida, sottostimato con readiness domestica), e il DLM usa solo la potenza lasciata libera dalle
  altre sessioni, così il totale resta entro i kW disponibili.
- Senza ricarica notturna i veicoli Office non vengono più scaglionati sull'arco della finestra
  (gli ultimi diventavano disponibili solo nell'ultimo quarto): la rotazione la fa il DLM.

## Release 2026-10 — Carbon Accounting

### Nuove funzionalità
- **Modulo Carbon Accounting** (`josa_core/carbon/`): riduzione netta delle emissioni GHG
  dell'elettrificazione (baseline a combustione − scenario elettrico − leakage), con
  confine di progetto, fattori con fonte (ISPRA, DESNZ), ex-ante dalla simulazione ed
  ex-post da dati misurati. Metodologia allineata alla ISO 14064-2, verificabile da terza
  parte: vedi `docs/carbon_methodology.md`.
- 5 endpoint `/api/v1/carbon/*`, tra cui import dell'export sessioni CPMS (OCPP) e
  pacchetto evidenze con hash SHA-256.
- **Stima rapida CO2 dalla home** (`POST /api/v1/carbon/quick`): numero veicoli, km/anno,
  % di flotta elettrificata e % di ricarica in azienda (più FV in sito opzionale), senza
  simulazione a slot. Stessi fattori e stessa formula del modulo completo.
- Nuova tappa "🌍 Carbon Accounting" nell'interfaccia; valore economico opzionale della CO2
  (ETS2 sul carburante evitato o prezzo ombra), separato dal TCO.

### Modifiche
- **KPI `co2`**: ora è la riduzione **netta** (prima: solo gasolio evitato allo scarico, senza
  sottrarre le emissioni dell'elettricità). Il valore precedente resta in `co2_lorda_scarico`.
  Lo stesso numero è usato in simulazione, ottimizzatore, scenari e Business Report.
- Fattore gasolio unico dalla libreria (prima 2,65 nella simulazione e 2,64 nel report), allineato alla
  tabella MASE dei parametri standard nazionali 2022-2024 (gasolio per autotrazione 3,150 tCO2/t → 2,630 kg/L).
- La classifica dell'ottimizzatore (AC-first) non cambia: la CO2 è solo informativa.

## Calibrazione su casi reali — fase 3: TCO (2026-10-11)

- **TCO per modello a tre colonne** (`josa_core/tco_powertrain.py`, `POST /api/v1/tco/powertrain`):
  canone NLT reale + energia/carburante per ICE, PHEV e BEV dello stesso modello, con
  raccomandazione e soglie di km/anno di pareggio (PHEV vs ICE, BEV vs ICE, BEV vs PHEV).
  Il PHEV usa la quota di km in elettrico (default 70%).
- **Prezzo energia dalla readiness**: `prezzo_energia_da_readiness` combina tariffa
  domestica (quota readiness, anche 0%), aziendale e pubblica secondo la copertura
  dell'infrastruttura in sede.
- **Business case infrastruttura** (`POST /api/v1/tco/business-case`): payback in mesi e
  VAN con CAPEX anche fasato, rampa di adozione dei risparmi (default 30/90/100%) e
  scelta esplicita della convenzione (CAPEX all'anno 0 o a fine anno 1).
- **Un solo tasso di sconto**: `josa_core.utils.TASSO_SCONTO_DEFAULT` (8%) usato da TCO,
  business model, business report e business case. Il business report usava il 5%
  (anche il frontend lo inviava fisso): ora usa il campo "Tasso di sconto" della UI.

## Calibrazione su casi reali — fase 2 (2026-10-11)

- **Tolleranza tra veicoli**: nuovo `policy.tolleranza_tra_veicoli_min` (default 0,
  comportamento invariato). Una colonnina torna disponibile solo dopo la tolleranza
  (spostare l'auto, collegare la successiva), sia nel loop principale sia nel DLM.
- **Verifica settimanale**: nuovo endpoint `POST /api/v1/verifica-settimanale`
  (`engine.run_weekly_check`). Simula N settimane lun-ven con profilo di presenza
  variabile e scenari di sensitività (km +50%, presenza 100%, potenza −25%, 1 punto AC
  guasto). Tolleranza di default 60 min (prudente), 30 min con cambio auto organizzato.
  Esito: C1 copertura media ≥ 99% sullo scenario base, C5 ≥ 95% nel 5% di giorni
  peggiori su ogni scenario.
- **Sessioni diurne contate due volte**: le soste lunghe di fine giornata venivano
  servite sia dal loop principale sia dal DLM, sulle stesse colonnine e senza vedersi
  (sessioni sovrapposte, capacità sovrastimata). Ora le pianifica solo il DLM, che
  vede anche le occupazioni del loop principale. I top-up tra i giri e le soste brevi
  (< 1 h) restano nel loop principale.
- **Gap buffer aziendale**: calcolato per veicolo, solo per le soste lunghe in sede e
  ignorando i veicoli già al SOC massimo.

## Calibrazione su casi reali — fase 1 (2026-10-11)

Il motore è stato confrontato con due casi reali anonimizzati (`test_casi_reali.py`).
La logica resta la stessa: queste modifiche correggono punti in cui il motore si
discostava dai casi reali.

- **Readiness domestica**: i veicoli con wallbox domestica caricano in sede solo la
  loro quota di buffer aziendale, non il pieno. Prima il DLM portava tutti al pieno e
  la readiness non cambiava la domanda in sede (stessa energia con 0%, 66% o 100%).
  Con readiness 0% il comportamento non cambia. Il target aziendale aggregato ora
  somma i target per veicolo (100% per chi non ha accesso domestico).
- **Veicoli per punto**: `max_ac_veicoli_per_punto` / `max_dc_veicoli_per_punto`
  valgono anche per la ricarica diurna gestita dal DLM, contando i veicoli già serviti
  nella stessa giornata.
- **Ottimizzatore**: a parità di servizio torna a vincere la configurazione più
  economica con meno DC (copertura → DC minimo → costo). "Budget non usato" e bonus
  DC venivano prima del costo e facevano spendere tutto il budget disponibile.
- **Vincolo di oversizing AC**: la potenza AC è valutata al caricatore di bordo della
  flotta (es. 6,6 kW per gli ibridi) invece che alla targa della colonnina, così
  configurazioni reali come 14 AC su 100 kW non vengono più scartate.

## Release 2026-08-03 (corrente)

### Correzioni critiche
- **DLM (Dynamic Load Management)**: rimosso il blocco che rifiutava configurazioni
  dove la potenza installata supera quella di rete. Il motore slot-per-slot già
  distribuiva correttamente la potenza disponibile — il bug era nel guard di ingresso.
  Nuova soglia: blocca solo se `p_installata > 1.5 × p_rete`.
- **AC-first**: rimossa la doppia penalizzazione delle soluzioni solo-AC che spingeva
  il motore verso DC anche quando AC era più economico e raggiungeva la stessa
  copertura. La classifica ora è: copertura → DC minimo → costo → attesa.
- **Copertura reale vs assunta**: la `copertura_pct` esposta via API riflette ora
  solo l'energia effettivamente erogata dalle colonnine simulate, non quella assunta
  coperta a casa. Aggiunta una scomposizione visibile nel frontend (colonnine /
  casa / pubblica) con avviso quando la quota "colonnine" è < 50%.
- **Profilo Office con 1 giro**: il motore permetteva solo una breve finestra di
  ricarica pari al "tempo disponibile" (pensato per soste brevi), invece di tutta
  la finestra fino alla partenza successiva. Corretto.
- **Target di ricarica azzerato**: con `ricarica_domestica=False`, il target di
  energia da caricare in azienda veniva erroneamente impostato a 0 invece di 100%.

### Nuove funzionalità
- **Gantt per colonnina**: nuova vista complementare che mostra, per ogni punto
  fisico, le sessioni di ricarica (quale veicolo, quando, quanta energia),
  il numero di veicoli serviti e il tasso di utilizzo.
- **4 Business Case guidati**: selettore a card nella tappa "Flotta" che mostra
  solo i campi rilevanti per il caso scelto (Dipendenti, Distribuzione/Logistica,
  Pool Car, Furgoni operativi). Pool Car introduce `probabilita_utilizzo_pct` per
  simulare la rotazione reale dei veicoli condivisi.
- **Gate di accesso**: autenticazione con password verificata lato server (non nel
  codice frontend), token JWT-like firmato con HMAC-SHA256, validità 12 ore.
- **Scomposizione energetica nel risultato**: la schermata "Progetto" mostra sempre
  la ripartizione tra energia erogata da colonnine (verificata), assunta a casa e
  assunta da pubblica, con avviso se la quota "colonnine" è bassa.
