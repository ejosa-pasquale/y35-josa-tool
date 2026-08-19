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



@app.get("/version", include_in_schema=False)
def version():
    import os
    size = os.path.getsize(FRONTEND_INDEX) if os.path.isfile(FRONTEND_INDEX) else 0
    with open(FRONTEND_INDEX) as f:
        html = f.read()
    return {
        "frontend_size": size,
        "stage_report_count": html.count("stage-report"),
        "business_report_count": html.count("business-report"),
        "last_modified": os.path.getmtime(FRONTEND_INDEX)
    }

@app.get("/", include_in_schema=False)
def serve_frontend():
    if not os.path.isfile(FRONTEND_INDEX):
        raise HTTPException(status_code=404, detail="frontend/index.html non trovato accanto a questo servizio.")
    return FileResponse(
        FRONTEND_INDEX,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


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


@app.post("/api/v1/tco/hybrid-roi", tags=["tco"])
def hybrid_roi(req: dict, _=Depends(auth.richiede_accesso_valido)):
    """ROI a tre colonne per flotte ibride plug-in: Diesel puro / Ibrido / EV puro.

    Parametri attesi nel body JSON:
    - n_veicoli, km_giornalieri_per_veicolo, autonomia_elettrica_km
    - consumo_elettrico_kwh_km, consumo_benzina_l100km (ibrido oltre autonomia)
    - consumo_benzina_diesel_l100km (diesel puro di confronto)
    - prezzo_benzina_eur_l, prezzo_energia_kwh
    - infra_capex_eur, orizzonte_anni
    - probabilita_ricarica (0-1, opzionale)
    """
    from josa_core.tco import compute_hybrid_roi
    import dataclasses
    try:
        result = compute_hybrid_roi(
            n_veicoli=int(req.get("n_veicoli", 1)),
            km_giornalieri_per_veicolo=float(req.get("km_giornalieri_per_veicolo", 50)),
            autonomia_elettrica_km=float(req.get("autonomia_elettrica_km", 50)),
            consumo_elettrico_kwh_km=float(req.get("consumo_elettrico_kwh_km", 0.18)),
            consumo_benzina_l100km=float(req.get("consumo_benzina_l100km", 6.0)),
            consumo_benzina_diesel_l100km=float(req.get("consumo_benzina_diesel_l100km", 8.0)),
            prezzo_benzina_eur_l=float(req.get("prezzo_benzina_eur_l", 1.85)),
            prezzo_energia_kwh=float(req.get("prezzo_energia_kwh", 0.25)),
            infra_capex_eur=float(req.get("infra_capex_eur", 5000.0)),
            infra_om_annuo_eur=float(req.get("infra_om_annuo_eur", 200.0)),
            orizzonte_anni=int(req.get("orizzonte_anni", 10)),
            probabilita_ricarica=req.get("probabilita_ricarica"),
        )
        return dataclasses.asdict(result)
    except (ValueError, TypeError, KeyError) as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/v1/sensitivity", tags=["analisi"])
def sensitivity_analysis(req: dict, _=Depends(auth.richiede_accesso_valido)):
    """Matrice di sensitivity: per ogni configurazione hardware ragionevole,
    mostra cambi/colonnina, attesa, copertura, picco e zona operativa.
    Include confronto AC vs AC+DC per mostrare l'impatto della ricarica rapida.
    """
    import dataclasses
    from josa_core.sensitivity import calcola_sensitivity
    from app import schemas, engine as eng

    try:
        # Ricostruisce request simulate dal body
        gruppi_raw = req.get("gruppi", [])
        catalogo_raw = req.get("catalogo_hardware", [])
        policy_raw = req.get("policy", {})

        gruppi = [schemas.FleetGroup(**g) for g in gruppi_raw]
        catalogo = [schemas.HardwareSpec(**h) for h in catalogo_raw]
        policy = schemas.EnginePolicy(**policy_raw)

        budget_max = float(req.get("budget_max_eur", 999999.0))
        soglia_cambi = int(req.get("soglia_cambi_per_colonnina", 4))
        fattori = req.get("fattori_settimanali", [1.0, 1.0, 1.0, 1.0, 0.8])

        # Stima parametri flotta dal primo gruppo
        g0 = gruppi[0]
        n_veicoli = int(g0.n_veicoli)
        km_giornalieri = float(g0.km_per_giro) * float(g0.giri_per_veicolo_giorno)
        consumo = float(g0.consumo_kwh_km)
        p_max_ac = float(g0.potenza_max_ricarica_ac_kw)
        h_start = g0.finestra_inizio.hour + g0.finestra_inizio.minute / 60.0
        h_end = g0.finestra_fine.hour + g0.finestra_fine.minute / 60.0
        finestra_h = max(1.0, h_end - h_start)

        catalogo_dicts = [
            {"nome": h.nome, "potenza_kw": h.potenza_kw,
             "costo_acq": h.costo_acquisto_eur, "costo_ins": h.costo_installazione_eur}
            for h in catalogo
        ]

        def sim_fn(cfg: dict):
            sim_req = schemas.SimulateRequest(
                gruppi=gruppi,
                catalogo_hardware=catalogo,
                configurazione=schemas.HardwareConfig(quantita=cfg),
                policy=policy,
            )
            return eng.run_simulate(sim_req)

        rows = calcola_sensitivity(
            sim_fn=sim_fn,
            catalogo=catalogo_dicts,
            n_veicoli=n_veicoli,
            km_giornalieri=km_giornalieri,
            consumo_kwh_km=consumo,
            p_max_ac_kw=p_max_ac,
            finestra_h=finestra_h,
            p_shave_kw=policy.p_shaving_kw,
            soglia_cambi=soglia_cambi,
            budget_max=budget_max,
            fattori_settimanali=fattori,
        )

        return {
            "righe": [dataclasses.asdict(r) for r in rows],
            "soglia_cambi": soglia_cambi,
            "n_verde": sum(1 for r in rows if r.zona == "verde"),
            "raccomandazione_principale": rows[0].raccomandazione if rows else "Nessuna configurazione trovata",
        }
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/v1/business-report", tags=["analisi"])
def business_report(req: dict, _=Depends(auth.richiede_accesso_valido)):
    """Business Report completo — tutti i KPI del Business Advisor Streamlit:
    decisione GO/REVIEW, snapshot, finanziario completo, TCO grafico km,
    ESG, confronto soluzioni, Gantt per stazione.
    """
    from josa_core.business_report import compute_business_report
    from app import schemas, engine as eng
    try:
        gruppi = [schemas.FleetGroup(**g) for g in req.get("gruppi", [])]
        catalogo = [schemas.HardwareSpec(**h) for h in req.get("catalogo_hardware", [])]
        policy = schemas.EnginePolicy(**req.get("policy", {}))
        config = req.get("config", {})
        soluzioni = req.get("soluzioni", [])
        budget_max = float(req.get("budget_max_eur", 30000.0))
        tco_params = req.get("tco_params", {})

        # Simula configurazione selezionata (base + stress)
        sim_req = schemas.SimulateRequest(
            gruppi=gruppi, catalogo_hardware=catalogo,
            configurazione=schemas.HardwareConfig(quantita=config), policy=policy)
        res_base = eng.run_simulate(sim_req)

        # Stress: aumenta i giri del 20% se supportato
        try:
            policy_stress = schemas.EnginePolicy(**{**req.get("policy", {}),
                "extra_trips_pct": 20, "delay_minutes": 15})
            res_stress = eng.run_simulate(schemas.SimulateRequest(
                gruppi=gruppi, catalogo_hardware=catalogo,
                configurazione=schemas.HardwareConfig(quantita=config), policy=policy_stress))
        except Exception:
            res_stress = res_base

        # Parametri flotta
        fleet_nv = sum(g.n_veicoli for g in gruppi)
        fleet_km_day = sum(g.km_per_giro * g.giri_per_veicolo_giorno * g.n_veicoli for g in gruppi)
        fleet_cons = sum(g.consumo_kwh_km * g.km_per_giro * g.giri_per_veicolo_giorno * g.n_veicoli
                         for g in gruppi) / max(fleet_km_day, 1)

        # Estrai sessioni da gantt_veicoli (formato reale del response)
        gantt_raw = []
        for v in res_base.get("gantt_veicoli", []):
            for s in v.get("segmenti", []):
                if s.get("stato") == "carica_azienda" and s.get("colonnina"):
                    gantt_raw.append({
                        "log_p": {"st": s.get("colonnina"), "i": s.get("inizio"), "ec": s.get("fine")},
                        "vid": v.get("vehicle_id", "EV"),
                        "caricato": s.get("energia_kwh", 0.0),
                    })

        report = compute_business_report(
            kpi=res_base.get("kpi", {}),
            kpi_stress=res_stress.get("kpi", {}),
            config=config,
            timeline_p=res_base.get("timeline_p_kw", []),
            sessions=gantt_raw,
            soluzioni=soluzioni,
            fleet_nv=fleet_nv,
            fleet_km_day_total=fleet_km_day,
            fleet_cons_avg=fleet_cons,
            budget_max=budget_max,
            p_shaving=float(policy.p_shaving_kw),
            c_pri_medio=float(tco_params.get("c_pri_medio", 0.25)),
            c_pub=float(tco_params.get("c_pub", 0.65)),
            km_l=float(tco_params.get("km_l", 11.0)),
            e_l=float(tco_params.get("e_l", 1.85)),
            c_mnt_die=float(tco_params.get("c_mnt_die", 0.08)),
            c_mnt_ev=float(tco_params.get("c_mnt_ev", 0.03)),
            c_acq_die=float(tco_params.get("c_acq_die", 25000.0)),
            c_acq_ev=float(tco_params.get("c_acq_ev", 35000.0)),
            tco_period=int(tco_params.get("tco_period", 36)),
            fin_horizon_years=int(tco_params.get("fin_horizon_years", 10)),
            fin_discount_rate=float(tco_params.get("fin_discount_rate", 0.05)),
        )
        return report
    except Exception as e:
        import traceback
        raise HTTPException(status_code=422, detail=f"{e}\n{traceback.format_exc()}")


@app.post("/api/v1/vehicle-matching", tags=["analisi"])
async def vehicle_matching(req: dict, _=Depends(auth.richiede_accesso_valido)):
    """Vehicle Matching — chiama Claude API lato server per evitare CORS.
    Riceve la lista di veicoli e restituisce le alternative EV con scoring.
    """
    import os, httpx
    
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY non configurata sul server.")
    
    prompt = req.get("prompt", "")
    if not prompt:
        raise HTTPException(status_code=422, detail="Campo 'prompt' mancante.")
    
    try:
        import json
        async with httpx.AsyncClient(timeout=90.0) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 6000,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
        if r.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Claude API error {r.status_code}: {r.text[:300]}")
        data = r.json()
        if "error" in data:
            raise HTTPException(status_code=500, detail=f"Claude API error: {data['error']}")
        text = data.get("content", [{}])[0].get("text", "")
        if not text:
            raise HTTPException(status_code=500, detail=f"Claude API risposta vuota.")
        clean = text.replace("```json", "").replace("```", "").strip()
        # Gestisci JSON troncato: tenta parse, se fallisce cerca il punto di troncamento
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            # Trova l'ultimo oggetto completo nell'array "veicoli"
            try:
                # Cerca di chiudere il JSON troncato
                last_complete = clean.rfind('},\n    {')
                if last_complete == -1:
                    last_complete = clean.rfind('}]')
                if last_complete > 0:
                    # Tenta di completare il JSON fino all'ultimo veicolo completo
                    partial = clean[:last_complete+1]
                    # Chiudi veicoli[], scenari{}, etc.
                    partial += '],"scenari":{},"incentivi":{},"raccomandazione":"Analisi parziale - risposta troncata"}'
                    return json.loads(partial)
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=f"JSON parse error — risposta troppo lunga. Testo: {clean[:200]}")
    except HTTPException:
        raise
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"JSON parse error: {e}. Testo: {text[:300] if 'text' in dir() else 'N/A'}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore Claude API: {type(e).__name__}: {str(e)}")


# ============== CONVERSATIONAL LAYER ==============

@app.post("/api/v1/chat/message", tags=["chat"])
async def chat_message(req: dict, _=Depends(auth.richiede_accesso_valido)):
    """Conversational Layer — gestisce un turno della conversazione.
    Riceve la storia della chat e il nuovo messaggio, restituisce la risposta
    di Claude con il profilo dati estratto e il flag pronto_per_analisi.
    """
    import os, httpx
    from josa_core.chat_engine import SYSTEM_PROMPT, build_messages, extract_data_block, clean_response

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY non configurata.")

    history = req.get("history", [])
    message = req.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="Messaggio vuoto.")

    messages = build_messages(history, message)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-sonnet-4-6", "max_tokens": 1000,
                      "system": SYSTEM_PROMPT, "messages": messages}
            )
        data = r.json()
        if r.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Claude API: {data}")
        
        full_text = data.get("content", [{}])[0].get("text", "")
        profile = extract_data_block(full_text)
        visible_text = clean_response(full_text)
        
        return {
            "reply": visible_text,
            "profile": profile,
            "pronto": profile.get("pronto_per_analisi", False),
            "history": history + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": full_text}
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/chat/analyze", tags=["chat"])
async def chat_analyze(req: dict, _=Depends(auth.richiede_accesso_valido)):
    """Converte il profilo conversazionale in payload per il motore e lancia l'analisi."""
    from josa_core.chat_engine import profile_to_analysis_payload
    from app import schemas, engine as eng

    profile = req.get("profile", {})
    catalogo_raw = req.get("catalogo_hardware") or [{
        "nome": "AC 22kW", "potenza_kw": 22.0,
        "costo_acquisto_eur": 1000.0, "costo_installazione_eur": 1600.0,
        "costo_manutenzione_eur_anno": 60.0
    }]

    payload = profile_to_analysis_payload(profile, catalogo_raw)

    try:
        gruppi = [schemas.FleetGroup(**g) for g in payload["gruppi"]]
        catalogo = [schemas.HardwareSpec(**h) for h in payload["catalogo_hardware"]]
        policy = schemas.EnginePolicy(**payload["policy"])

        opt_req = schemas.OptimizeRequest(
            gruppi=gruppi, catalogo_hardware=catalogo, policy=policy,
            budget_max_eur=payload["budget_max_eur"],
            tipi_hardware_da_esplorare=payload["tipi_hardware_da_esplorare"],
            beam_size=payload["beam_size"], patience=payload["patience"],
            max_steps=payload["max_steps"])
        
        result = eng.run_optimize(opt_req)
        result["profilo_conversazionale"] = profile
        result["payload_usato"] = {
            "gruppi": payload["gruppi"],
            "catalogo_hardware": payload["catalogo_hardware"],
            "policy": payload["policy"],
            "budget_max_eur": payload["budget_max_eur"],
        }
        return result
    except Exception as e:
        import traceback
        raise HTTPException(status_code=422, detail=f"{e}\n{traceback.format_exc()[:500]}")


@app.get("/debug", include_in_schema=False)
def debug_features():
    """Mostra le feature attive nel deployment corrente."""
    import os
    fe_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "index.html")
    with open(fe_path) as f:
        html = f.read()
    
    from josa_core import chat_engine
    import inspect
    ce_src = inspect.getsource(chat_engine)
    
    return {
        "frontend_size": len(html.encode()),
        "features": {
            "chip_tipo_veicolo": "Berlina/Compatta" in html,
            "catalogo_ac_dc": "AC 7.4kW" in html and "DC 150kW" in html,
            "avviso_potenza": "avvisoPotenzeInsuff" in html,
            "gantt_fix": "gantt_veicoli" in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine.py")).read(),
            "consumo_realistico": "consumo_map" in ce_src,
            "catalogo_ce_ac11": "AC 11kW" in ce_src,
            "catalogo_ce_dc150": "DC 150kW" in ce_src,
            "token_30_giorni": "30 giorni" in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth.py")).read(),
            "chat_budget_separato": "budget COLONNINE" in ce_src,
        }
    }

