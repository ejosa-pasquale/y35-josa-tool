# Y35 / JoSa Fleet Charging Sizing Tool

Motore di simulazione e dimensionamento per infrastrutture di ricarica flotte aziendali.

## Architettura

```
02_api_backend/
├── app/
│   ├── main.py          # FastAPI entrypoint — serve anche il frontend su GET /
│   ├── auth.py          # Gate di accesso con token firmato (ACCESS_PASSWORD + ACCESS_TOKEN_SECRET)
│   ├── engine.py        # Glue layer: traduce request → josa_core → response
│   └── schemas.py       # Pydantic schemas (request/response)
├── josa_core/
│   ├── fleet_events.py  # Generazione eventi flotta (drive, charge) per 4 business case
│   ├── simulation.py    # Simulazione SOC + DLM slot-per-slot + Gantt per veicolo/colonnina
│   ├── optimizer.py     # Beam search con classifica AC-first + costo-first
│   ├── site_scoring.py  # Scoring sito A-F (9 criteri, ispirato DIN SPEC 91433)
│   ├── compliance_dm2025.py  # Verifica obblighi DM 28/10/2025
│   ├── tco.py           # Total Cost of Ownership EV vs Diesel
│   └── ems/             # V2G dispatch (MPC rolling horizon)
├── frontend/
│   └── index.html       # SPA completa — servita direttamente dal backend
├── requirements.txt
└── test_api_engine.py   # Batteria di regressione
```

## Business Case supportati

| Business Case | Profilo motore | Campo chiave |
|---|---|---|
| Dipendenti aziendali | Office | Finestra diurna, quota ricarica domestica |
| Distribuzione / Logistica | Last-mile | Giri multipli, sosta breve tra giri |
| Pool Car | Last-mile | `probabilita_utilizzo_pct` — rotazione reale |
| Furgoni operativi | Last-mile | Pernotto in deposito, nessun accesso domestico |

## Dynamic Load Management (DLM)

Il motore distribuisce la potenza disponibile slot per slot (15 min), rispettando
sempre `p_shaving_kw` come ceiling. La potenza installata totale può superare quella
di rete (es. 5 colonnine AC 11 kW su una rete da 40 kW): ogni colonnina riceve
`min(p_nominale, p_disponibile_in_quel_slot)`, rallentando la ricarica senza mai
bloccarla. Configurazioni bloccate solo se `p_installata > 1.5 × p_rete`.

## Ranking soluzioni (AC-first)

La classifica segue la priorità operativa:
1. Veicoli non serviti → 0
2. Fabbisogno scoperto (buffer gap) → 0  
3. Copertura → 100%
4. **Unità DC → minimo** (si parte sempre da AC)
5. **Costo → minimo** (tra configurazioni equivalenti)
6. Attesa/coda (solo spareggio finale)

## Deploy su Render.com

```bash
# Build Command
pip install -r requirements.txt

# Start Command
uvicorn app.main:app --host 0.0.0.0 --port $PORT

# Variabili d'ambiente obbligatorie
ACCESS_PASSWORD=<password-per-i-lead>
ACCESS_TOKEN_SECRET=<stringa-casuale-32-bytes>
# python3 -c "import secrets; print(secrets.token_hex(32))"
```

Il backend serve il frontend su `GET /` — nessun hosting separato richiesto.

## Variabili FleetGroup notevoli

| Campo | Tipo | Significato |
|---|---|---|
| `probabilita_utilizzo_pct` | float 0-100 | Pool Car: % di giorni in cui il veicolo viene usato |
| `quota_ricarica_domestica_pct` | float 0-100 | Dipendenti: % fabbisogno coperto a casa |
| `ricarica_domestica` | bool\|None | Accesso ricarica domestica (None = policy globale) |
| `ricarica_notturna_azienda` | bool\|None | Ricarica notturna in deposito |

## Endpoint principali

| Metodo | Path | Funzione |
|---|---|---|
| `GET` | `/` | Frontend SPA |
| `GET` | `/health` | Health check (no auth) |
| `POST` | `/api/v1/access/verify` | Verifica password → token 12h |
| `POST` | `/api/v1/simulate` | Simula una configurazione specifica |
| `POST` | `/api/v1/optimize` | Beam search → lista soluzioni ranked |
| `POST` | `/api/v1/scenarios/compare` | Confronto scenari + analisi marginale |
| `POST` | `/api/v1/site-scoring` | Scoring sito A-F (9 criteri) |
| `POST` | `/api/v1/compliance/dm2025` | Verifica DM 28/10/2025 |
| `POST` | `/api/v1/v2g/dispatch` | Dispacciamento V2G giorno singolo |
| `POST` | `/api/v1/v2g/weekly-dispatch` | Dispacciamento V2G settimana |

## Autori

EV Field Service Srl — y35.eu / e-josa.it
