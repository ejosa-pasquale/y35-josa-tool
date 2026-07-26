"""
josa_core.smart_bridge — ponte tra la generazione eventi esistente
(josa_core.fleet_events) e il motore di allocazione intelligente
(josa_core.smart_allocation).

Nessuna duplicazione della logica di generazione flotta: si riusano
esattamente gli stessi eventi/vehicles_map che alimentano simulazione_soc,
cosi' il confronto tra le due logiche di allocazione (greedy esclusiva vs
pool condiviso intelligente) e' sullo stesso identico scenario di flotta.
"""

import numpy as np

from .smart_allocation import SmartVehicle, allocate_smart


def build_smart_vehicles(events, vehicles_map, n_timestep: int, durata_timestep_h: float, hw_types: list,
                          hybrid_private_home_charging: bool = False) -> list:
    """Costruisce la lista di SmartVehicle a partire da eventi/vehicles_map
    gia' generati da genera_timeline_soc_da_gruppi.

    disponibile[t] = True se il veicolo NON e' in un evento "drive" in quel
    timestep (cioe' e' potenzialmente in deposito). Il fabbisogno energetico
    giornaliero da coprire IN AZIENDA dipende dalla policy di ricarica:
    - hybrid_private_home_charging=True: il grosso del fabbisogno si copre a
      casa del conducente, l'azienda copre solo il buffer (company_buffer_target_kwh).
      BUG CORRETTO: la prima versione usava sempre 'daily_drive_kwh' (l'intero
      fabbisogno di guida) anche in questo caso, gonfiando artificialmente il
      fabbisogno da coprire in azienda di un fattore ~3x in scenari ibridi.
    - hybrid_private_home_charging=False: l'azienda copre tutto il fabbisogno
      di guida giornaliero ('daily_drive_kwh').
    """
    dt = durata_timestep_h
    disponibile = {vid: [True] * n_timestep for vid in vehicles_map}

    for ev in events:
        if ev.get("type") != "drive":
            continue
        vid = ev.get("vid")
        if vid not in disponibile:
            continue
        s, f = float(ev.get("s", 0.0)), float(ev.get("f", 0.0))
        t0 = max(0, int(s / dt))
        t1 = min(n_timestep, int(np.ceil(f / dt)) if f > 0 else 0)
        for t in range(t0, t1):
            disponibile[vid][t] = False

    smart_vehicles = []
    for vid, v in vehicles_map.items():
        # BUG CORRETTO: la scelta tra "buffer aziendale" e "fabbisogno pieno" deve
        # dipendere da can_home_night del SINGOLO veicolo, non dal flag globale —
        # un veicolo senza accesso domestico (es. furgone pool) ha bisogno di
        # caricare TUTTO il fabbisogno in azienda anche se la policy globale e'
        # "ibrida", altrimenti il suo target energetico risulta erroneamente ~0
        # (il campo "buffer aziendale" e' concettualmente 0 per chi non ha casa
        # come alternativa, ma quel veicolo ha comunque bisogno di energia).
        veicolo_ha_casa = bool(v.get("can_home_night", hybrid_private_home_charging))
        if veicolo_ha_casa:
            energia_richiesta = float(v.get("company_buffer_target_kwh", 0.0))
        else:
            energia_richiesta = float(v.get("daily_drive_kwh", 0.0))
        smart_vehicles.append(SmartVehicle(
            id=vid,
            capacita_kwh=float(v.get("batt", 0.0)),
            soc_iniziale_pct=100.0 * float(v.get("soc_start", 0.0)) / max(1e-6, float(v.get("batt", 1.0))),
            soc_min_pct=20.0,
            energia_richiesta_kwh=energia_richiesta,
            disponibile=disponibile[vid],
            tipi_ammessi=list(hw_types),
            potenza_max_ac_kw=float(v.get("potenza_max_ac_kw", 11.0)),
        ))
    return smart_vehicles


def run_smart_allocation_for_config(events, vehicles_map, hw_db: dict, config: dict,
                                      p_rete_max_kw: float, n_timestep: int = 24,
                                      durata_timestep_h: float = 1.0,
                                      connessione_binaria: bool = False,
                                      hybrid_private_home_charging: bool = False):
    """Punto di ingresso comodo: dato uno scenario flotta gia' generato e una
    configurazione hardware, calcola il picco minimo raggiungibile con
    allocazione intelligente a pool condiviso (nessun V2G).

    Default: risoluzione oraria (24 slot) + connessione continua (LP) per
    velocita' — adatto a esplorare molte configurazioni in un ottimizzatore.
    Per una verifica esatta finale su una configurazione gia' scelta, passare
    connessione_binaria=True (piu' lento, MILP esatto).

    hybrid_private_home_charging DEVE corrispondere allo stesso flag usato per
    generare events/vehicles_map (altrimenti il fabbisogno energetico calcolato
    non corrisponde allo scenario reale — vedi build_smart_vehicles)."""
    hw_types = [t for t, q in config.items() if int(q) > 0]
    vehicles = build_smart_vehicles(events, vehicles_map, n_timestep, durata_timestep_h, hw_types,
                                     hybrid_private_home_charging=hybrid_private_home_charging)
    return allocate_smart(vehicles, hw_db, config, n_timestep, durata_timestep_h, p_rete_max_kw,
                            connessione_binaria=connessione_binaria)
