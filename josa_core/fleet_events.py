"""
Generazione eventi flotta (drive/charge) da gruppi omogenei.

Estratto identico da main.py — genera_eventi_da_gruppi (righe 862-966),
genera_timeline_soc_da_gruppi (righe 980-1264), genera_timeline_soc_multi_day
(righe 1265-1319). Nessuna dipendenza da variabili globali: gia' pure
nell'originale.
"""

from datetime import time

import numpy as np
import pandas as pd

from .utils import _time_to_hh, _speed_kmh_for_profile


def genera_eventi_da_gruppi(df):
    """Trasforma gruppi omogenei in una lista di eventi di ricarica compatibili con il motore attuale.

    Ogni riga del df rappresenta un gruppo (es. last-mile). Gli eventi sono le 'sessioni' di rientro/ricarica.
    """
    eventi = []
    nv_tot = 0
    km_day_tot = 0.0
    # consumo medio pesato per km (per TCO)
    cons_num, cons_den = 0.0, 0.0

    for _, r in df.iterrows():
        nome = str(r.get('Gruppo', 'Gruppo')).strip() or 'Gruppo'
        profilo = str(r.get('Profilo', 'Custom'))
        n = int(r.get('N_veicoli', 0) or 0)
        if n <= 0:
            continue
        nv_tot += n

        km_giro = float(r.get('Km_per_giro', 0) or 0)
        giri_gg = float(r.get('Giri_per_veicolo_giorno', 0) or 0)
        cons = float(r.get('Consumo_kWh_km', 0.22) or 0.22)
        batt = float(r.get('Batteria_kWh', 75) or 75)
        giri_aut = float(r.get('Giri_per_autonomia', 2) or 2)
        quota_dep = float(r.get('Quota_ricarica_deposito', 1.0) or 1.0)
        quota_dep = max(0.0, min(1.0, quota_dep))
        t_disp_min = float(r.get('Tempo_disponibile_min', 60) or 60)
        conc_max = int(r.get('Contemporanei_max', 0) or 0)
        k_factor = float(r.get('K_factor', 1.0) or 1.0)
        k_factor = max(1.0, min(2.5, k_factor))

        # KPI flotta reali (per TCO)
        km_day_group = n * km_giro * giri_gg
        km_day_tot += km_day_group
        cons_num += (km_day_group * cons)
        cons_den += km_day_group

        # Sessioni/giorno (logica Excel): N * (giri_gg / giri_autonomia)
        sess_per_vehicle = (giri_gg / giri_aut) if giri_aut > 0 else 0
        sess_tot = int(round(n * sess_per_vehicle * quota_dep))
        if sess_tot <= 0:
            continue

        # Energia per sessione coerente: (km_giro * giri_autonomia) * cons
        e_sess = max(0.1, km_giro * giri_aut * cons)

        # Finestre rientro (in ore). K comprime la finestra e riduce tempo disponibile.
        t_start = r.get('Finestra_inizio', time(9, 0))
        t_end = r.get('Finestra_fine', time(19, 0))
        if not isinstance(t_start, time):
            t_start = time(9, 0)
        if not isinstance(t_end, time):
            t_end = time(19, 0)
        h0, h1 = _time_to_hh(t_start), _time_to_hh(t_end)
        if h1 <= h0:
            h1 += 24
        window = max(0.5, (h1 - h0) / k_factor)
        center = (h0 + h1) / 2
        h0_eff = center - window/2
        h1_eff = center + window/2

        # Dwell time (tempo utile di ricarica) compresso da K
        dwell_h = max(0.25, (t_disp_min / k_factor) / 60.0)

        # Intensità di cluster: più alta per last-mile
        if 'last' in profilo.lower():
            peaks = [max(h0_eff, center-2), center, min(h1_eff, center+2)]
            peak_w = 0.8
        elif 'uff' in profilo.lower() or 'office' in profilo.lower():
            peaks = [center]
            peak_w = 2.0
        else:
            peaks = [center-1, center+1]
            peak_w = 1.4

        # Genera eventi: campiona tempi di arrivo attorno ai peak, con rumore proporzionale alla finestra
        rng = np.random.default_rng(42)
        for j in range(sess_tot):
            pk = float(peaks[j % len(peaks)])
            std = max(0.15, window / peak_w)
            arr = float(rng.normal(pk, std))
            arr = min(max(arr, h0_eff), h1_eff)
            dep = arr + dwell_h

            # Batteria: cap minimo per non tagliare la sessione; non superare batt reale.
            batt_eff = max(batt, e_sess * 1.2)
            eventi.append({
                "nome": f"{nome}_S{j+1}",
                "group": nome,
                "profilo": profilo,
                "e_req": e_sess,
                "km": 0.0,  # km non usato per KPI (TCO usa km_day_tot)
                "batt": batt_eff,
                "cons": cons,
                "s": arr,
                "f": dep,
                "night": True,
                "conc_max": conc_max
            })

    cons_avg = (cons_num/cons_den) if cons_den > 0 else 0.22
    return eventi, nv_tot, km_day_tot, cons_avg


# --- Nuovo helper: timeline SOC (giri + rientri + ricarica notturna) ---


def genera_timeline_soc_da_gruppi(df, soc_params, night_plug_h: float = 18.0, night_policy: str = "asap", seed: int = 42, rientro_stagger_min: int = 0, hybrid_private_home_charging: bool | None = None, company_buffer_pct: float | None = None):
    """Genera una timeline di eventi (drive/charge) e una mappa veicoli per simulazione SOC.

    Obiettivo: modellare che i veicoli NON sono sempre in deposito.
    - Ogni veicolo esegue N giri (probabilistici se non interi)
    - Ogni giro consuma energia
    - Tra i giri: finestra breve di ricarica (Tempo_disponibile)
    - Notte: finestra notturna lunga per tornare a SOC target (di default a partire dalle 18:00)

    soc_params: dict con soc_start, soc_min, soc_max, soc_buffer (tutti fra 0 e 1)
    night_plug_h: ora (in ore) in cui i mezzi vengono tipicamente "messi in carica" per la notte.
    """
    events = []
    vehicles = {}
    nv_tot = 0
    km_day_tot = 0.0
    cons_num, cons_den = 0.0, 0.0

    rng = np.random.default_rng(int(seed))
    soc_start = float(soc_params.get("soc_start", 0.9))
    soc_min = float(soc_params.get("soc_min", 0.2))
    soc_max = float(soc_params.get("soc_max", 0.9))
    # buffer applicato ai target diurni
    soc_buffer = float(soc_params.get("soc_buffer", 0.05))
    if hybrid_private_home_charging is None:
        hybrid_private_home_charging = bool(soc_params.get("hybrid_private_home_charging", True))
    if company_buffer_pct is None:
        company_buffer_pct = float(soc_params.get("company_buffer_pct", 0.30))
    company_buffer_pct = max(0.0, min(1.0, float(company_buffer_pct)))

    for _, r in df.iterrows():
        nome_g = str(r.get("Gruppo", "Gruppo")).strip() or "Gruppo"
        profilo = str(r.get("Profilo", "Custom"))
        _prof_l = str(profilo or "").strip().lower()
        _grp_l = str(nome_g or "").strip().lower()
        is_office_group = (_prof_l == "office") or ("ufficio" in _grp_l)
        # Profilo "Pendolare aziendale": il veicolo esce una volta al mattino e rientra
        # una volta a sera, senza soste intermedie in deposito. Convenzione (nessun nuovo
        # campo nello schema): per questo profilo, Km_per_giro = km giornalieri totali
        # (andata+ritorno) e Giri_per_veicolo_giorno = 1. Vedi ramo dedicato piu' sotto.
        is_pendolare_group = _prof_l in ("pendolare", "pendolare aziendale", "commuter")
        n = int(r.get("N_veicoli", 0) or 0)
        if n <= 0:
            continue
        nv_tot += n

        km_giro = float(r.get("Km_per_giro", 0) or 0)
        giri_gg = float(r.get("Giri_per_veicolo_giorno", 0) or 0)
        cons = float(r.get("Consumo_kWh_km", 0.22) or 0.22)
        batt = float(r.get("Batteria_kWh", 75) or 75)
        giri_aut = float(r.get("Giri_per_autonomia", 2) or 2)
        quota_dep = float(r.get("Quota_ricarica_deposito", 1.0) or 1.0)
        quota_dep = max(0.0, min(1.0, quota_dep))
        t_disp_min = float(r.get("Tempo_disponibile_min", 60) or 60)
        conc_max = int(r.get("Contemporanei_max", 0) or 0)
        k_factor = float(r.get("K_factor", 1.0) or 1.0)
        k_factor = max(1.0, min(2.5, k_factor))

        # Policy notturna per gruppo v54:
        # - Ricarica_domestica = Si: il ripristino lungo avviene a casa/esterno e in azienda resta solo il buffer.
        # - Ricarica_notturna_azienda = Si: il veicolo puo restare in sede e usare una finestra overnight aziendale.
        # Le due selezioni sono indipendenti: se entrambe sono No, non viene aggiunta ricarica notturna automatica.
        def _yes_no(v, default=False):
            if pd.isna(v):
                return bool(default)
            s = str(v).strip().lower()
            if s in ["si", "sì", "yes", "true", "1", "on"]:
                return True
            if s in ["no", "false", "0", "off"]:
                return False
            return bool(default)

        legacy_night_raw = r.get("Ricarica_notte", "")
        legacy_mode = "" if pd.isna(legacy_night_raw) else str(legacy_night_raw or "").strip().lower()
        legacy_home = any(x in legacy_mode for x in ["casa", "home", "privata", "private", "esterna", "external"])
        legacy_company = any(x in legacy_mode for x in ["azienda", "depot", "overnight", "deposito", "sede"])
        legacy_none = any(x in legacy_mode for x in ["non", "none", "nessuna", "no night"]) and not legacy_home and not legacy_company

        can_home_night = _yes_no(r.get("Ricarica_domestica", None), default=(legacy_home or (not legacy_mode and bool(hybrid_private_home_charging))))
        can_company_overnight = _yes_no(r.get("Ricarica_notturna_azienda", None), default=(legacy_company or (not legacy_mode and not bool(hybrid_private_home_charging))))
        no_night_charge = (not can_home_night) and (not can_company_overnight)
        potenza_max_ac_kw = r.get("Potenza_max_ricarica_ac_kW", None)
        if pd.isna(potenza_max_ac_kw):
            potenza_max_ac_kw = 11.0  # default storico: caricatore di bordo AC piu' comune
        if can_home_night and can_company_overnight:
            night_mode = "casa privata/esterna + azienda overnight"
        elif can_home_night:
            night_mode = "casa privata/esterna"
        elif can_company_overnight:
            night_mode = "azienda overnight"
        else:
            night_mode = "non disponibile"

        # BUG CORRETTO: prima, quando il veicolo NON aveva accesso alla ricarica
        # domestica (can_home_night=False), l'obiettivo di ricarica in azienda
        # veniva messo a ZERO — esattamente al contrario di quanto dovrebbe
        # succedere. Se non c'e' casa come alternativa, l'azienda deve puntare al
        # 100% del fabbisogno (nessun altro posto dove caricare), non allo 0%.
        # Il "buffer" ridotto (company_buffer_pct, es. 30%) ha senso SOLO quando
        # il veicolo puo' davvero completare il resto a casa — altrimenti il
        # deposito deve provare a coprire tutto, anche solo con la ricarica
        # diurna (finestra Office) o quella notturna aziendale se abilitata.
        if can_home_night:
            try:
                _buf_raw = r.get("Buffer_azienda_pct", company_buffer_pct * 100.0)
                if pd.isna(_buf_raw):
                    _buf_raw = company_buffer_pct * 100.0
                group_buffer_pct = float(_buf_raw) / 100.0
            except Exception:
                group_buffer_pct = float(company_buffer_pct)
        else:
            group_buffer_pct = 1.0
        group_buffer_pct = max(0.0, min(1.0, float(group_buffer_pct)))

        # KPI flotta (per TCO)
        km_day_group = n * km_giro * giri_gg
        km_day_tot += km_day_group
        cons_num += (km_day_group * cons)
        cons_den += km_day_group

        # Finestre rientro (ore). K comprime la finestra e riduce il tempo disponibile.
        t_start = r.get("Finestra_inizio", time(9, 0))
        t_end = r.get("Finestra_fine", time(19, 0))
        if not isinstance(t_start, time):
            t_start = time(9, 0)
        if not isinstance(t_end, time):
            t_end = time(19, 0)
        h0, h1 = _time_to_hh(t_start), _time_to_hh(t_end)
        if h1 <= h0:
            h1 += 24
        window_len = max(1.0, h1 - h0)
        window_eff = max(0.75, window_len / k_factor)
        center = (h0 + h1) / 2
        h0_eff = center - window_eff / 2
        h1_eff = center + window_eff / 2

        dwell_h = max(0.10, (t_disp_min / k_factor) / 60.0)
        speed = _speed_kmh_for_profile(profilo)
        drive_h = max(0.15, km_giro / max(speed, 1.0))

        # Ibrido plug-in: il fabbisogno energetico di ricarica è solo la parte
        # percorsa in modalità elettrica — min(km_giro, autonomia_elettrica_km).
        # Oltre l'autonomia elettrica il veicolo passa a benzina autonomamente,
        # senza bisogno di altra ricarica. Per un EV puro, autonomia_elettrica_km
        # è None e il calcolo è invariato (tutta la distanza consuma kWh).
        _autonomia_el_raw = r.get("Autonomia_elettrica_km", None)
        autonomia_elettrica_km = None
        if _autonomia_el_raw is not None and not (isinstance(_autonomia_el_raw, float) and pd.isna(_autonomia_el_raw)):
            try:
                autonomia_elettrica_km = float(_autonomia_el_raw)
            except (ValueError, TypeError):
                pass

        if autonomia_elettrica_km is not None and autonomia_elettrica_km >= 0:
            km_elettrici_giro = min(km_giro, autonomia_elettrica_km)
            e_giro = max(0.0, km_elettrici_giro * cons)
        else:
            e_giro = max(0.0, km_giro * cons)

        # Blocco DC per veicoli ibridi che non hanno la presa CCS/CHAdeMO
        accetta_dc_raw = r.get("Accetta_ricarica_dc", True)
        accetta_ricarica_dc = bool(accetta_dc_raw) if not (isinstance(accetta_dc_raw, float) and pd.isna(accetta_dc_raw)) else True

        base_giri = int(np.floor(giri_gg))
        p_extra = max(0.0, min(1.0, giri_gg - base_giri))

        _prob_uso_raw = r.get("Probabilita_utilizzo_pct", None)
        if _prob_uso_raw is None or (isinstance(_prob_uso_raw, float) and pd.isna(_prob_uso_raw)):
            prob_utilizzo = 1.0  # comportamento invariato: sempre usato (altri business case)
        else:
            prob_utilizzo = max(0.0, min(1.0, float(_prob_uso_raw) / 100.0))

        for i in range(n):
            # Pool Car / flotte condivise: questo specifico veicolo potrebbe non
            # essere usato affatto oggi (resta fermo in sede, disponibile per
            # caricare tutto il giorno) — estrazione probabilistica PRIMA di
            # generare qualunque giro, cosi' il dimensionamento riflette una
            # rotazione reale, non l'assunzione che ogni veicolo esca sempre.
            veicolo_non_usato_oggi = rng.random() >= prob_utilizzo
            # giri per singolo veicolo (stocastico per rispettare la media)
            n_trips = 0 if veicolo_non_usato_oggi else (1 if is_pendolare_group else (base_giri + (1 if rng.random() < p_extra else 0)))
            vid = f"{nome_g}_{i+1}"
            vehicles[vid] = {
                "nome": vid,
                "group": nome_g,
                "profilo": profilo,
                "batt": batt,
                "cons": cons,
                "soc": batt * soc_start,
                "soc_start": batt * soc_start,
                "drive_kwh": 0.0,
                "depot_kwh": 0.0,
                "ext_kwh": 0.0,
                "ext_events": 0,
                "trips": int(n_trips),
                "conc_max": conc_max,
                "daily_drive_kwh": float(n_trips) * float(e_giro),
                "company_buffer_target_kwh": float(n_trips) * float(e_giro) * float(group_buffer_pct),
                "company_buffer_charged_kwh": 0.0,
                "home_private_kwh": 0.0,
                "night_charging_mode": night_mode,
                "can_home_night": bool(can_home_night),
                "can_company_overnight": bool(can_company_overnight),
                "no_night_charge": bool(no_night_charge),
                "potenza_max_ac_kw": float(potenza_max_ac_kw),
                "accetta_ricarica_dc": bool(accetta_ricarica_dc),
                "autonomia_elettrica_km": autonomia_elettrica_km,
            }

            if is_pendolare_group:
                # Il veicolo e' fuori sede per l'intera finestra (esce al mattino, rientra
                # a sera): un solo evento drive copre [h0_eff, h1_eff], non un giro breve
                # seguito da ricarica intermedia. Nessuna ricarica diurna: non essendo mai
                # in deposito durante il giorno, non ha prese disponibili fino al rientro.
                dep, arr = float(h0_eff), float(h1_eff)
                events.append({"type": "drive", "vid": vid, "s": dep, "f": arr, "e_drive": float(e_giro)})
                arr_last = arr
                last_charge_end = None
                if can_company_overnight and (not no_night_charge):
                    night_start = arr if night_policy == "asap" else max(arr, float(night_plug_h))
                    night_end = h0_eff + 24
                    if night_end > night_start + 1e-6:
                        events.append({
                            "type": "charge", "kind": "night", "vid": vid,
                            "s": float(night_start), "f": float(night_end),
                            "e_next": float(e_giro),
                            "soc_max": soc_max, "soc_min": soc_min, "soc_buffer": soc_buffer, "soc_start": soc_start,
                            "charge_reason": "company_overnight",
                        })
                continue

            if n_trips <= 0:
                # Veicolo fermo (tipicamente Office/Ufficio):
                # - Giorno: puo' iniziare a caricare al mattino (es. 09:00) mentre i furgoni sono in giro
                # - Notte: finestra per garantire SOC_start (ma se e' gia' pieno, non occupera' prese)
                if quota_dep > 0:
                    if is_office_group or veicolo_non_usato_oggi:
                        events.append({
                            "type": "charge",
                            "kind": "day",
                            "vid": vid,
                            "s": float(h0),
                            "f": float(h1),
                            "e_next": 0.0,
                            "soc_max": soc_max,
                            "soc_min": soc_min,
                            "soc_buffer": soc_buffer,
                            "soc_start": soc_start,
                        })
                    if can_company_overnight and not no_night_charge:
                        events.append({
                            "type": "charge",
                            "kind": "night",
                            "vid": vid,
                            "s": 0.0 if night_policy == "asap" else float(night_plug_h),
                            "f": h0_eff + 24,
                            "e_next": 0.0,
                            "soc_max": soc_max,
                            "soc_min": soc_min,
                            "soc_buffer": soc_buffer,
                            "soc_start": soc_start,
                            "charge_reason": "company_overnight",
                        })
                continue

            # Distribuzione partenze lungo la finestra (con jitter)
            latest_depart = max(h0_eff, h1_eff - drive_h)
            if latest_depart <= h0_eff:
                # finestra troppo stretta: il giro è "incompatibile" → verrà evidenziato come bisogno esterno
                dep_times = np.array([h0_eff] * n_trips, dtype=float)
            else:
                dep_times = np.linspace(h0_eff, latest_depart, n_trips)
                dep_times += rng.normal(0.0, 0.10, size=n_trips)
                dep_times = np.clip(dep_times, h0_eff, latest_depart)
                # Office/Ufficio: interpreta la finestra come "arrivo in sede".
                # Sposta le partenze indietro di drive_h cosi' che l'arrivo sia intorno a h0_eff (es. 09:00).
                if is_office_group:
                    dep_times = dep_times - drive_h
                    dep_times = np.clip(dep_times, max(0.0, h0_eff - drive_h), max(0.0, latest_depart - drive_h))
                if int(rientro_stagger_min) > 0:
                    # jitter extra per ridurre simultaneità rientri (minuti -> ore)
                    sig = (float(rientro_stagger_min) / 60.0) / 2.5
                    dep_times += rng.normal(0.0, sig, size=n_trips)
                    dep_times = np.clip(dep_times, h0_eff, latest_depart)

            arr_last = None
            last_charge_end = None
            for j in range(n_trips):
                dep = float(dep_times[j])
                arr = dep + drive_h
                arr_last = arr
                # evento di guida (consumo)
                events.append({
                    "type": "drive",
                    "vid": vid,
                    "s": dep,
                    "f": arr,
                    "e_drive": e_giro,
                })

                if quota_dep <= 0:
                    continue

                # finestra di ricarica tra giri (opportunity charging).
                # Per il profilo Office/Ufficio, il veicolo resta parcheggiato in
                # sede fino alla prossima partenza (o fino a fine finestra se e'
                # l'ultimo giro) — "Tempo disponibile" non si applica, quel campo
                # rappresenta una sosta breve tra giri per altri profili (es.
                # consegne), non "resta fermo tutto il giorno". Prima di questa
                # correzione, un veicolo Office con un giro reale (arrivo in sede)
                # otteneva solo pochi minuti di ricarica invece di tutta la giornata.
                next_dep = float(dep_times[j + 1]) if j + 1 < n_trips else h1_eff
                if is_office_group:
                    end = next_dep
                else:
                    end = min(arr + dwell_h, next_dep)
                if end > arr + 1e-6:
                    e_next = e_giro if (j + 1 < n_trips) else 0.0
                    events.append({
                        "type": "charge",
                        "kind": "day",
                        "vid": vid,
                        "s": float(arr),
                        "f": float(end),
                        "e_next": float(e_next),
                        "soc_max": soc_max,
                        "soc_min": soc_min,
                        "soc_buffer": soc_buffer,
                        "soc_start": soc_start,
                        "charge_reason": "company_buffer" if hybrid_private_home_charging else "opportunity",
                    })
                    if j == n_trips - 1:
                        last_charge_end = float(end)
                elif j == n_trips - 1:
                    last_charge_end = float(arr)

            # In logica ibrida la ricarica lunga notturna e domestica/privata, non aziendale.
            # Il deposito aziendale resta per opportunity/fast diurno e buffer minimo.
            # In logica deposito pura manteniamo invece la finestra notturna aziendale.
            if can_company_overnight and (not no_night_charge) and quota_dep > 0 and arr_last is not None:
                # Strategia deposito: se "asap" attacco appena rientra (o finita l'eventuale opportunity dell'ultimo giro).
                # Se "plug" attacco dall'orario serale fissato.
                _start_base = float(last_charge_end) if last_charge_end is not None else float(arr_last)
                night_start = _start_base if night_policy == "asap" else max(_start_base, float(night_plug_h))
                if int(rientro_stagger_min) > 0:
                    # jitter anche sull'attacco serale (senza anticipare prima del rientro)
                    sig = (float(rientro_stagger_min) / 60.0) / 3.0
                    night_start = max(_start_base, night_start + float(rng.normal(0.0, sig)))
                night_end = h0_eff + 24
                if night_end > night_start + 1e-6:
                    events.append({
                        "type": "charge",
                        "kind": "night",
                        "vid": vid,
                        "s": float(night_start),
                        "f": float(night_end),
                        "e_next": float(e_giro),
                        "soc_max": soc_max,
                        "soc_min": soc_min,
                        "soc_buffer": soc_buffer,
                        "soc_start": soc_start,
                        "charge_reason": "company_overnight",
                    })

    cons_avg = (cons_num / cons_den) if cons_den > 0 else 0.22
    events = sorted(events, key=lambda x: (x.get("s", 0.0), 0 if x.get("type") == "drive" else 1))
    return events, vehicles, nv_tot, km_day_tot, cons_avg




def genera_timeline_soc_multi_day(df, soc_params, days: int, night_plug_h: float, night_policy: str, trips_var_pct: int = 0, rientro_stagger_min: int = 0, hybrid_private_home_charging: bool | None = None, company_buffer_pct: float | None = None):
    """Replica lo scenario su più giorni portando avanti il SOC.

    - Crea eventi drive/charge per ciascun giorno e li shift-a di 24h.
    - Applica una variazione controllata dei giri (stress management) giorno per giorno.
    - Ritorna km medi/giorno (per KPI/TCO) e consumo medio pesato.
    """
    days = int(max(1, min(14, days)))
    rng = np.random.default_rng(123)
    events_all = []
    vehicles_map = None
    nv_tot = 0
    km_day_sum = 0.0
    cons_num, cons_den = 0.0, 0.0

    for d in range(days):
        # fattore giri (stress) attorno alla media
        if trips_var_pct > 0:
            amp = float(trips_var_pct) / 100.0
            # leggera tendenza pessimista (più probabile sopra media)
            factor = float(np.clip(rng.normal(loc=1.0 + amp * 0.25, scale=amp / 2.0), 1.0 - amp, 1.0 + amp))
        else:
            factor = 1.0

        df_d = df.copy()
        if "Giri_per_veicolo_giorno" in df_d.columns:
            df_d["Giri_per_veicolo_giorno"] = pd.to_numeric(df_d["Giri_per_veicolo_giorno"], errors="coerce").fillna(0.0) * factor

        ev_d, veh_d, nv_d, km_day_d, cons_avg_d = genera_timeline_soc_da_gruppi(
            df_d,
            soc_params,
            night_plug_h=night_plug_h,
            night_policy=night_policy,
            seed=42 + d,
            rientro_stagger_min=int(rientro_stagger_min),
            hybrid_private_home_charging=hybrid_private_home_charging,
            company_buffer_pct=company_buffer_pct,
        )

        if vehicles_map is None:
            vehicles_map = veh_d
            nv_tot = nv_d

        shift = 24.0 * d
        for ev in ev_d:
            ev2 = ev.copy()
            ev2["s"] = float(ev2.get("s", 0.0)) + shift
            ev2["f"] = float(ev2.get("f", 0.0)) + shift
            events_all.append(ev2)

        km_day_sum += float(km_day_d)
        cons_num += float(km_day_d) * float(cons_avg_d)
        cons_den += float(km_day_d)

    cons_avg = (cons_num / cons_den) if cons_den > 0 else 0.22
