"""
josa_api — API REST sopra josa_core.

Punto di ingresso: `uvicorn app.main:app --reload` in locale,
o il comando equivalente configurato per il deploy (vedi README_DEPLOY.md).

Serve anche il frontend (frontend/index.html) sulla route "/", cosi' backend
e frontend sono UN SOLO servizio da mettere online — niente Netlify separato,
niente indirizzo da copiare a mano tra i due.
"""

import os

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import auth, engine, schemas

app = FastAPI(
    title="Y35 / JoSa Sizing Engine API",
    description="API per simulazione e dimensionamento infrastrutture di ricarica flotte aziendali.",
    version="0.1.0",
)

# In produzione, restringere allow_origins al dominio del frontend reale
# (es. https://tool.y35.eu) invece di "*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


FRONTEND_INDEX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "index.html")


@app.get("/", include_in_schema=False)
def serve_frontend():
    if not os.path.isfile(FRONTEND_INDEX):
        raise HTTPException(status_code=404, detail="frontend/index.html non trovato accanto a questo servizio.")
    return FileResponse(FRONTEND_INDEX)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.post("/api/v1/access/verify", tags=["meta"])
def verify_access(body: dict):
    """Verifica la password di accesso e rilascia un token temporaneo (12 ore).

    Il gate è disattivato (tutte le password sono accettate) se la variabile
    d'ambiente ACCESS_PASSWORD non è impostata sul server — utile in sviluppo.
    """
    password = str(body.get("password", ""))
    if not auth.verifica_password(password):
        raise HTTPException(status_code=401, detail="Password non corretta.")
    return {"token": auth.genera_token(), "gate_attivo": auth.gate_attivo()}


@app.post("/api/v1/simulate", response_model=schemas.SimulateResponse, tags=["engine"])
def simulate(req: schemas.SimulateRequest, _=Depends(auth.richiede_accesso_valido)):
    """Simula una configurazione hardware specifica sulla flotta data."""
    try:
        return engine.run_simulate(req)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/v1/optimize", response_model=schemas.OptimizeResponse, tags=["engine"])
def optimize(req: schemas.OptimizeRequest, _=Depends(auth.richiede_accesso_valido)):
    """Esegue la beam search e ritorna le configurazioni ammissibili, ranked."""
    try:
        return engine.run_optimize(req)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/v1/scenarios/compare", response_model=schemas.ScenarioCompareResponse, tags=["engine"])
def scenarios_compare(req: schemas.ScenarioCompareRequest, _=Depends(auth.richiede_accesso_valido)):
    """Confronta TUTTI gli scenari ammissibili trovati (non solo il migliore),
    più i benchmark standard 1:1/1:2/1:4 auto/punto — per ciascuno: KPI operativi,
    impatto TCO/ROI/NPV/payback, e piano di ricarica giornaliero (timeline di potenza).
    """
    try:
        return engine.run_scenario_compare(req)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/v1/site-scoring", response_model=schemas.SiteScoringResponse, tags=["site-scoring"])
def site_scoring(req: schemas.SiteScoringRequest, _=Depends(auth.richiede_accesso_valido)):
    """Valuta la qualità di un sito per infrastruttura di ricarica su quattro
    criteri (traffico, accessibilità, demografia, servizi nell'area), grado A-F.

    Metodologia propria — non conformità letterale a una norma specifica. Vedi
    il campo 'metodologia' nella risposta per il dettaglio.
    """
    try:
        return engine.run_site_scoring(req)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/v1/compliance/dm2025", response_model=schemas.ComplianceDM2025Response, tags=["compliance"])
def compliance_dm2025(req: schemas.ComplianceDM2025Request, _=Depends(auth.richiede_accesso_valido)):
    """Verifica obblighi minimi DM 28/10/2025 per un parcheggio aziendale.

    ATTENZIONE: risultato best-effort basato su fonti secondarie, non un parere
    legale — vedi il campo 'disclaimer' e 'fonti_da_verificare' nella risposta.
    """
    try:
        return engine.run_compliance_check(req)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/v1/v2g/dispatch", response_model=schemas.V2GDispatchResponse, tags=["v2g"])
def v2g_dispatch(req: schemas.V2GDispatchRequest, _=Depends(auth.richiede_accesso_valido)):
    """Risolve un singolo passo di dispacciamento V2G: ogni veicolo trattato come
    asset energetico ('batteria distribuita'), con vincolo hard di mobilità
    (SoC minimo all'orario di partenza), peak shaving, prezzi dinamici, FV,
    costo di degrado batteria. Vedi josa_core.ems.dispatch per i dettagli.
    """
    try:
        return engine.run_v2g_dispatch(req)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/v1/v2g/weekly-dispatch", response_model=schemas.V2GWeeklyDispatchResponse, tags=["v2g"])
def v2g_weekly_dispatch(req: schemas.V2GWeeklyDispatchRequest, _=Depends(auth.richiede_accesso_valido)):
    """Dispacciamento V2G su un orizzonte multi-giorno (tipicamente una settimana
    lavorativa, 168 ore), con MPC a orizzonte scorrevole — mostra come il motore
    decide quando caricare, sospendere, scaricare in V2G o preservare la batteria
    di ogni veicolo lungo l'intera settimana, in funzione dei turni (partenze).
    """
    try:
        return engine.run_v2g_weekly_dispatch(req)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
