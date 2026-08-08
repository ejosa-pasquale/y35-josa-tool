"""
DLM Scheduler — logica di scheduling per colonnine aziendali.

Principio (equivalente alla logica Streamlit originale):
  - Ogni slot temporale (SLOT_H = 15 min) il sistema decide chi carica.
  - Si conosce per ogni veicolo: energia ancora da caricare, finestra disponibile,
    potenza massima accettabile.
  - Si conosce per ogni colonnina: potenza nominale, occupazione corrente.
  - Il DLM distribuisce la potenza disponibile (p_shave_limit) tra i veicoli attivi,
    ordinando per urgenza (Least Laxity First: chi ha meno tempo relativo all'energia
    rimanente viene servito prima).
  - Una colonnina può servire veicoli diversi in sequenza nello stesso giorno
    (rotazione continua), massimizzando il tasso di utilizzo.
  - Un veicolo non può essere collegato a più colonnine contemporaneamente.

Output:
  - sessions: lista di sessioni di carica {vid, colonnina, t_start, t_end, energia_kwh}
  - power_timeline: potenza totale per slot [kW]
  - soc_finale per veicolo
"""

from __future__ import annotations
import math
from typing import Optional

SLOT_H = 0.25  # 15 minuti per slot


def schedule_dlm(
    vehicles_charging: list[dict],
    stations: list[dict],
    p_shave_limit: float,
    t_start_h: float,
    t_end_h: float,
    orizzonte_h: float = 24.0,
) -> dict:
    """
    vehicles_charging: lista di dict con:
      - vid: str
      - soc_kwh: float (SOC attuale in kWh)
      - soc_target_kwh: float (SOC obiettivo)
      - p_max_kw: float (potenza max accettata dal veicolo)
      - t_avail_start: float (ora da cui il veicolo è disponibile)
      - t_avail_end: float (ora entro cui deve aver finito)
      - accetta_dc: bool

    stations: lista di dict con:
      - nome: str
      - tipo: str ('AC ...' o 'DC ...')
      - p_kw: float (potenza nominale)

    Ritorna:
      sessions: lista di sessioni complete
      power_per_slot: {slot_idx: kW}
      soc_per_vid: {vid: soc_finale_kwh}
      veicoli_serviti: set di vid con carica completata
    """
    n_slots = int(math.ceil((t_end_h - t_start_h) / SLOT_H))
    if n_slots <= 0:
        return {"sessions": [], "power_per_slot": {}, "soc_per_vid": {v["vid"]: v["soc_kwh"] for v in vehicles_charging}, "veicoli_serviti": set()}

    # Stato mutabile per ogni veicolo
    veh_state = {}
    for v in vehicles_charging:
        veh_state[v["vid"]] = {
            "soc_kwh": float(v["soc_kwh"]),
            "soc_target_kwh": float(v["soc_target_kwh"]),
            "p_max_kw": float(v.get("p_max_kw", 11.0)),
            "t_avail_start": float(v.get("t_avail_start", t_start_h)),
            "t_avail_end": float(v.get("t_avail_end", t_end_h)),
            "accetta_dc": bool(v.get("accetta_dc", True)),
            "colonnina_attuale": None,  # None = non connesso
        }

    # Stato colonnine: libere all'inizio
    stn_state = {s["nome"]: {"libera_da": t_start_h, "tipo": s["tipo"], "p_kw": float(s["p_kw"])} for s in stations}

    sessions = []
    power_per_slot = {}
    sessione_corrente = {}  # vid -> {colonnina, t_start, energia_acc}

    for slot_i in range(n_slots):
        t = t_start_h + slot_i * SLOT_H
        t_fine_slot = t + SLOT_H
        potenza_usata = 0.0

        # Aggiorna connessioni: chi finisce in questo slot si disconnette
        da_disconnettere = []
        for vid, sess in sessione_corrente.items():
            vs = veh_state[vid]
            energia_rimanente = vs["soc_target_kwh"] - vs["soc_kwh"]
            if energia_rimanente <= 1e-4:
                da_disconnettere.append(vid)
            elif t >= vs["t_avail_end"]:
                da_disconnettere.append(vid)

        for vid in da_disconnettere:
            sess = sessione_corrente.pop(vid)
            if sess["energia_acc"] > 1e-4:
                sessions.append({
                    "vid": vid,
                    "colonnina": sess["colonnina"],
                    "t_start": sess["t_start"],
                    "t_end": t,
                    "energia_kwh": round(sess["energia_acc"], 3),
                })
            veh_state[vid]["colonnina_attuale"] = None
            stn_state[sess["colonnina"]]["libera_da"] = t

        # Libera colonnine la cui sessione è già terminata prima di questo slot
        for stn_nome, stn in stn_state.items():
            if stn["libera_da"] <= t and stn_nome not in [s["colonnina"] for s in sessione_corrente.values()]:
                stn["libera_da"] = min(stn["libera_da"], t)

        # Veicoli disponibili ma non connessi, con ancora energia da caricare
        da_connettere = []
        for vid, vs in veh_state.items():
            if vid in sessione_corrente:
                continue
            if vs["t_avail_start"] > t:
                continue
            if vs["t_avail_end"] <= t:
                continue
            energia_rimanente = vs["soc_target_kwh"] - vs["soc_kwh"]
            if energia_rimanente <= 1e-4:
                continue
            # Urgenza: Least Laxity First
            tempo_rimasto = vs["t_avail_end"] - t
            tempo_necessario = energia_rimanente / max(vs["p_max_kw"], 0.1)
            laxity = tempo_rimasto - tempo_necessario
            da_connettere.append((laxity, vid))

        da_connettere.sort(key=lambda x: x[0])  # meno laxity = più urgente

        # Connetti nuovi veicoli alle colonnine libere
        for _, vid in da_connettere:
            vs = veh_state[vid]
            # Trova la colonnina libera più adatta (AC prima, poi DC)
            colonnina_scelta = None
            for stn_nome, stn in sorted(stn_state.items(), key=lambda x: (0 if "AC" in x[1]["tipo"] else 1, x[0])):
                if stn["libera_da"] > t + 1e-6:
                    continue
                if "DC" in stn["tipo"] and not vs["accetta_dc"]:
                    continue
                if stn_nome in [s["colonnina"] for s in sessione_corrente.values()]:
                    continue
                colonnina_scelta = stn_nome
                break

            if colonnina_scelta is None:
                continue  # nessuna colonnina disponibile in questo slot

            stn_state[colonnina_scelta]["libera_da"] = t_fine_slot + 1e-9
            veh_state[vid]["colonnina_attuale"] = colonnina_scelta
            sessione_corrente[vid] = {
                "colonnina": colonnina_scelta,
                "t_start": t,
                "energia_acc": 0.0,
            }

        # Calcola potenza per ogni veicolo connesso (DLM: rispetta p_shave_limit)
        if sessione_corrente:
            n_connessi = len(sessione_corrente)
            p_disponibile_per_veh = min(p_shave_limit / max(n_connessi, 1), 1e9)

            for vid, sess in sessione_corrente.items():
                vs = veh_state[vid]
                stn = stn_state[sess["colonnina"]]
                p_erogabile = min(
                    stn["p_kw"],         # limite colonnina
                    vs["p_max_kw"],      # limite caricatore di bordo
                    p_disponibile_per_veh,  # limite DLM
                )
                p_erogabile = max(0.0, p_erogabile)
                energia_slot = p_erogabile * SLOT_H
                energia_rimanente = vs["soc_target_kwh"] - vs["soc_kwh"]
                energia_effettiva = min(energia_slot, energia_rimanente)

                vs["soc_kwh"] += energia_effettiva
                sess["energia_acc"] += energia_effettiva
                potenza_usata += p_erogabile

        power_per_slot[slot_i] = potenza_usata

    # Chiudi sessioni aperte alla fine della finestra
    for vid, sess in sessione_corrente.items():
        if sess["energia_acc"] > 1e-4:
            sessions.append({
                "vid": vid,
                "colonnina": sess["colonnina"],
                "t_start": sess["t_start"],
                "t_end": t_end_h,
                "energia_kwh": round(sess["energia_acc"], 3),
            })

    veicoli_serviti = {
        v["vid"] for v in vehicles_charging
        if veh_state[v["vid"]]["soc_kwh"] >= veh_state[v["vid"]]["soc_target_kwh"] - 1e-3
    }

    return {
        "sessions": sessions,
        "power_per_slot": power_per_slot,
        "soc_per_vid": {vid: vs["soc_kwh"] for vid, vs in veh_state.items()},
        "veicoli_serviti": veicoli_serviti,
    }
