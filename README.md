# Y35 / JoSa — Pacchetto completo (aggiornato)

Snapshot completo di tutto il lavoro fatto fin qui. Tre cartelle:

```
01_streamlit_app/     L'app Streamlit originale, refactorizzata
02_api_backend/       API FastAPI + dashboard HTML (percorso guidato)
03_mockup_cliente/    Mockup statici pronti da mostrare/condividere, nessuna API richiesta
```

## 01_streamlit_app — l'app che già usi

Invariata nell'uso: `pip install -r requirements.txt` poi `streamlit run main.py`.
La logica è estratta in `josa_core/` (vedi `README_REFACTOR.md` dentro la cartella).

## 02_api_backend — l'API + la console interattiva

```bash
cd 02_api_backend
python3 -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 test_api_engine.py     # deve finire con "OK"
uvicorn app.main:app --reload
```
Poi apri `frontend/index.html` (istruzioni in `frontend/README_FRONTEND.md`).

**Il percorso guidato** (non più tab separate): Dimensiona la tua infrastruttura → Verifica una configurazione → Bilanciamento V2G → Compliance DM 2025, con una sidebar che accumula insight in tempo reale mentre si compila.

## 03_mockup_cliente — da aprire e condividere direttamente

Sei file HTML autonomi, dati reali già calcolati:

- **`mockup_completo.html`** ⭐ — il più completo: percorso input→output a scorrimento, **interattivo** (cambia scenario e parametri finanziari, grafici e KPI si aggiornano dal vivo), include break-even, analisi marginale, compliance, V2G. Il punto di partenza consigliato per una presentazione.
- **`mockup_interattivo_roi.html`** — solo la parte finanziaria interattiva (selettore scenari + grafico cashflow/NPV + parametri modificabili), senza il percorso input→output.
- **`mockup_mixed_home_charging.html`** — statico (nessun JS richiesto), evidenzia il flag per-gruppo di accesso alla ricarica domestica.
- **`mockup_cliente_input_output.html`** — statico, percorso input→output completo, scenario da 10 veicoli.
- **`mockup_10v_100posti.html`** — statico, stesso scenario in formato report a sezioni.
- **`report_5_veicoli.html`** — statico, scenario più piccolo con confronto multi-scenario espandibile.

I file "statico" (nessun JS richiesto per vedere numeri/grafici) restano utili per condivisione via canali che potrebbero bloccare JavaScript (anteprime email, alcuni viewer cloud). I file "interattivo" richiedono che chi li apre abbia JavaScript abilitato (praticamente sempre, in un browser normale) — usali quando vuoi mostrare dal vivo l'effetto di scenari/parametri diversi.

## Cosa contiene `josa_core` (identico in tutte le cartelle)

- **Motore di dimensionamento flotta** — simulazione SOC, beam search, ora con **5 profili di utilizzo**: Last-mile, Sales, Office, Long-haul, e il nuovo **Pendolare aziendale** (esce una volta al mattino, rientra una volta a sera, nessuna ricarica intermedia).
- **`smart_allocation.py` + `smart_bridge.py`** (nuovo) — allocazione intelligente della potenza: pool condiviso di colonnine per tipo invece di assegnazione esclusiva, picco minimizzato via programmazione lineare. **Nessun V2G in questa logica** (deliberatamente: il V2G non deve influenzare quante colonnine si dimensionano). Esposto nel confronto scenari come "picco intelligente", affiancato al picco del motore principale — non lo sostituisce.
- **`compliance_dm2025.py`** — verifica obblighi minimi DM 28/10/2025. **Non è un parere legale.**
- **`ems/`** — motore di dispacciamento V2G: dispacciamento giornaliero e **settimanale legato ai turni** (rolling MPC), ogni veicolo come asset energetico (SoC, priorità, probabilità di utilizzo, costo di degrado).
- **`business_model.py`** — confronto CAPEX vs Pay-per-Use.
- **`tco.py`** — modello finanziario completo Diesel vs EV: NPV, payback, ROI netto, Benefit/Cost ratio, riconciliazione OPEX riga per riga, cashflow.

## API — endpoint disponibili

`/api/v1/simulate` · `/api/v1/optimize` · `/api/v1/scenarios/compare` (tutti gli scenari ammissibili + benchmark 1:1/1:2/1:4 + frontiera compatibile, ciascuno con TCO/ROI e picco intelligente) · `/api/v1/compliance/dm2025` · `/api/v1/v2g/dispatch` (giornaliero) · `/api/v1/v2g/weekly-dispatch` (settimanale, turni)

## Bug reali trovati e corretti in questo giro di lavoro (per trasparenza)

1. **Confronto scenari**: mostrava solo 1 alternativa invece di tutte — mancava la "frontiera compatibile" (combinazioni sistematiche attorno alla soluzione migliore) che l'originale calcolava. Corretto: ora genera fino a 24 alternative aggiuntive.
2. **Allocazione intelligente**: usava il fabbisogno di guida totale invece del solo buffer aziendale quando la ricarica ibrida domestica è attiva, gonfiando il fabbisogno di un fattore ~3x. Corretto e verificato contro il KPI "energia interna" del motore principale.
3. **Scalabilità allocazione intelligente**: 22 secondi su 30 veicoli (MILP con variabili binarie) → 0,8 secondi rilassando a programma lineare continuo, **stesso risultato** su tutti gli scenari testati (verificato con MILP esatto come controllo).
4. **Griglia settimanale V2G ("gantt")**: rompeva il layout su mobile (celle a larghezza fissa, nessun contenitore scorrevole). Corretto con scroll orizzontale dedicato + colore basato su SoC% (come nell'analisi tecnica originale) invece di carica/scarica.

## Stato del progetto, onestamente

**Verificato**: tutti i moduli compilano, tutti i test automatici passano, inclusi i bug sopra — trovati testando attivamente su scenari diversi, non assunti corretti a priori.

**Non testato in questo ambiente**: Streamlit dal vivo, l'API in esecuzione reale, un vero browser — nessun accesso di rete per installare questi strumenti qui. Il primo avvio reale di ciascun pezzo resta il tuo.

**Cosa manca ancora rispetto alla visione completa**: multi-sede (analisi separata già consegnata), rilevamento automatico del profilo di utilizzo da dati osservati, integrazione dell'allocazione intelligente nell'ottimizzatore stesso (oggi è un confronto affiancato, non guida ancora la scelta della configurazione consigliata), autenticazione/multi-tenancy, persistenza scenari.
