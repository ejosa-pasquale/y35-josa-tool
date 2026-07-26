# Dashboard locale — come usarla

Questa pagina (`index.html`) è un'interfaccia semplice che parla con l'API
`josa_api` — non è ancora il prodotto finale (quello resta il frontend
Next.js discusso nel report), ma è un salto enorme in usabilità rispetto a
Swagger/`/docs`.

## Come avviarla

1. Assicurati che l'API sia in esecuzione (vedi `README_DEPLOY.md` nella cartella
   principale): `uvicorn app.main:app --reload` deve essere attivo in un
   terminale, con l'API raggiungibile su `http://127.0.0.1:8000`.

2. Apri `frontend/index.html` con doppio click — si apre nel browser di default.

3. Se in alto a sinistra il pallino accanto al campo indirizzo API diventa
   **verde**, l'API è raggiunta correttamente. Se resta **rosso/grigio**, vedi
   "Risoluzione problemi" sotto.

## Le tre schede

- **Simula**: valuta una configurazione hardware specifica sulla flotta data
  (equivalente a `/api/v1/simulate`). Precompilata con lo stesso scenario di
  test usato per validare il motore (gruppi "Last Mile" + "Ufficio").
- **Ottimizza**: fa girare la beam search per trovare le configurazioni
  migliori dato un budget (equivalente a `/api/v1/optimize`).
- **Compliance DM 2025**: verifica gli obblighi minimi del decreto (vedi il
  disclaimer nella risposta — non è un parere legale).

Ogni scheda ha una sezione "Avanzato" richiudibile per i parametri meno usati
di frequente (SOC, finestre orarie, esclusioni normative).

## Risoluzione problemi

**Pallino rosso, nessun risultato, errore "Failed to fetch"**: quasi sempre
significa che l'API non è in esecuzione, o gira su una porta diversa da
quella nel campo in alto. Controlla il terminale dove hai lanciato `uvicorn`.

**Se il pallino resta rosso anche con l'API attiva** (può succedere in alcuni
browser quando si apre il file con doppio click, per via di restrizioni di
sicurezza sui file locali): invece di aprire il file direttamente, apri un
secondo terminale nella cartella `frontend/` e lancia:
```bash
python3 -m http.server 5500
```
poi apri `http://127.0.0.1:5500` nel browser invece di aprire il file
direttamente. Questo evita eventuali blocchi del browser sulle richieste da
pagine locali.

## Limite onesto

Non ho potuto aprire un vero browser in questo ambiente per testare la
pagina interattivamente — ho verificato la sintassi JavaScript (corretta) e
riletto con attenzione la logica, ma il primo utilizzo reale è il tuo. Se
qualcosa si comporta in modo strano, apri la console del browser (F12 →
tab "Console") e mandami quello che vedi lì: è la prima cosa che guarderei
per capire cosa è successo.
