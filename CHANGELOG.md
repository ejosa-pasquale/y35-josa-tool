# Changelog

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
