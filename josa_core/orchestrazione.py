"""
Orchestrazione della ricarica al minor costo (V1G) e con scarica verso sede/rete (V2G).

Un programma lineare per giornata, a passi di 15 minuti. Le sessioni sono quelle della
simulazione del motore Y35 (stessa infrastruttura, stessi veicoli, stessa colonnina,
stesso orario di aggancio e stessa energia caricata): l'orchestrazione decide solo
QUANDO erogare la potenza dentro la sosta e, con il V2G, se restituirne una parte.

Vincoli:
  - ogni sessione riceve la stessa energia netta della simulazione (servizio invariato);
  - potenza per sessione entro la potenza della colonnina (e dell'auto in AC);
  - prelievo dalla rete entro il limite (potenza per la ricarica, o contratto se c'e'
    la curva della sede);
  - con V2G: stato di carica sempre tra il minimo per il V2G e il massimo.
Obiettivo: costo dell'energia prelevata - ricavo dell'energia ceduta + degrado della
batteria sull'energia scaricata + quota potenza sul nuovo picco del mese.

Solver: scipy.optimize.linprog (HiGHS), matrici sparse.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import coo_matrix

SLOT_H = 0.25


@dataclass
class SessioneFlessibile:
    a: int                  # primo slot (incluso) della finestra in cui l'auto e' collegata
    b: int                  # ultimo slot (escluso)
    energia_kwh: float      # energia netta da consegnare (come nella simulazione)
    p_max_kw: float
    v2g: bool = False
    soc0_kwh: float = 0.0
    soc_min_kwh: float = 0.0
    soc_max_kwh: float = 1e9


@dataclass
class EsitoGiorno:
    ok: bool
    carica_kw: list = field(default_factory=list)     # somma flotta per slot
    scarica_kw: list = field(default_factory=list)
    prelievo_kw: list = field(default_factory=list)
    immissione_kw: list = field(default_factory=list)
    messaggio: str = ""


def risolvi_giorno(
    sessioni: list,
    base_kw: list,          # consumo della sede senza flotta (0 se assente), per slot
    limite_kw: list,        # prelievo massimo per slot
    acquisto: list,         # EUR/kWh per slot
    vendita: list,          # EUR/kWh per slot (energia ceduta in rete)
    vendita_ammessa: bool,
    degrado_eur_kwh: float,
    rendimento_scarica: float,
    quota_potenza_eur_kw: float,
    picco_precedente_kw: float,
) -> EsitoGiorno:
    H = len(base_kw)
    dt = SLOT_H
    eta = max(0.5, min(1.0, float(rendimento_scarica)))

    # --- indici delle variabili ---
    col_c, col_d = [], []          # per sessione: lista (t, col)
    n = 0
    for s in sessioni:
        cs = []
        for t in range(s.a, s.b):
            cs.append((t, n)); n += 1
        col_c.append(cs)
        ds = []
        if s.v2g:
            for t in range(s.a, s.b):
                ds.append((t, n)); n += 1
        col_d.append(ds)
    col_soc = []                   # stato di carica a fine slot, solo sessioni V2G
    for s in sessioni:
        ss = []
        if s.v2g:
            for t in range(s.a, s.b):
                ss.append((t, n)); n += 1
        col_soc.append(ss)
    i_imp = n; n += H
    i_exp = n; n += H
    i_pk = n; n += 1

    c = np.zeros(n)
    lb = np.zeros(n)
    ub = np.zeros(n)
    for k, s in enumerate(sessioni):
        for t, j in col_c[k]:
            ub[j] = s.p_max_kw
            c[j] = 1e-6 * t          # a parita' di costo, prima e' meglio (piu' robusto)
        for t, j in col_d[k]:
            ub[j] = s.p_max_kw
            c[j] = degrado_eur_kwh * dt
        for t, j in col_soc[k]:
            lb[j] = min(s.soc_min_kwh, s.soc0_kwh)
            ub[j] = max(s.soc_max_kwh, s.soc0_kwh + s.energia_kwh)
    for t in range(H):
        c[i_imp + t] = acquisto[t] * dt
        ub[i_imp + t] = max(0.0, limite_kw[t])
        c[i_exp + t] = -vendita[t] * dt
        ub[i_exp + t] = 1e9 if vendita_ammessa else 0.0
    c[i_pk] = quota_potenza_eur_kw
    lb[i_pk] = max(0.0, picco_precedente_kw)
    ub[i_pk] = 1e9

    # --- uguaglianze: energia per sessione, bilancio al POD ---
    er, ec, ev, beq = [], [], [], []
    r = 0
    for k, s in enumerate(sessioni):
        for _, j in col_c[k]:
            er.append(r); ec.append(j); ev.append(dt)
        for _, j in col_d[k]:
            er.append(r); ec.append(j); ev.append(-dt / eta)
        beq.append(s.energia_kwh); r += 1
    r_bil = r
    for t in range(H):
        er += [r_bil + t, r_bil + t]; ec += [i_imp + t, i_exp + t]; ev += [1.0, -1.0]
        beq.append(base_kw[t])
    for k in range(len(sessioni)):
        for t, j in col_c[k]:
            er.append(r_bil + t); ec.append(j); ev.append(-1.0)
        for t, j in col_d[k]:
            er.append(r_bil + t); ec.append(j); ev.append(1.0)
    # stato di carica: soc_t - soc_{t-1} - dt*c_t + dt/eta*d_t = 0 (soc_{a-1} = soc0)
    r = r_bil + H
    for k, s in enumerate(sessioni):
        if not s.v2g:
            continue
        cmap = dict(col_c[k]); dmap = dict(col_d[k]); smap = dict(col_soc[k])
        for t in range(s.a, s.b):
            er += [r, r, r]; ec += [smap[t], cmap[t], dmap[t]]; ev += [1.0, -dt, dt / eta]
            if t > s.a:
                er.append(r); ec.append(smap[t - 1]); ev.append(-1.0)
                beq.append(0.0)
            else:
                beq.append(s.soc0_kwh)
            r += 1
    A_eq = coo_matrix((ev, (er, ec)), shape=(r, n)).tocsr()

    # --- disuguaglianze: picco ---
    ur, uc, uv, bub = [], [], [], []
    r = 0
    for t in range(H):
        ur += [r, r]; uc += [i_imp + t, i_pk]; uv += [1.0, -1.0]; bub.append(0.0); r += 1
    A_ub = coo_matrix((uv, (ur, uc)), shape=(r, n)).tocsr()

    res = linprog(c, A_ub=A_ub, b_ub=np.array(bub), A_eq=A_eq, b_eq=np.array(beq),
                  bounds=np.column_stack([lb, ub]), method="highs",
                  options={"time_limit": 30.0})
    if res.status != 0 or res.x is None:
        return EsitoGiorno(ok=False, messaggio=str(res.message))
    x = res.x
    car = np.zeros(H); sca = np.zeros(H)
    for k in range(len(sessioni)):
        for t, j in col_c[k]:
            car[t] += x[j]
        for t, j in col_d[k]:
            sca[t] += x[j]
    return EsitoGiorno(
        ok=True,
        carica_kw=car.tolist(), scarica_kw=sca.tolist(),
        prelievo_kw=x[i_imp:i_imp + H].tolist(), immissione_kw=x[i_exp:i_exp + H].tolist(),
    )
