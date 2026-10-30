"""
josa_core.investor — simulatore per l'investitore che compra contratti di
leasing dell'infrastruttura di ricarica gia' firmati da Y35.

Schema (ipotesi confermate da Pasquale il 2026-10-30):
  - l'investitore paga un prezzo per contratto (di default il CAPEX installato);
  - il cliente paga per ogni punto di ricarica una rata mensile per la durata
    del contratto; la rata e' divisa tra investitore e Y35 secondo la quota;
  - manutenzione a carico di EV Field Service (non pesa sull'investitore);
  - a fine contratto il cliente riscatta l'impianto al prezzo di riscatto;
  - nessuna imposta o incentivo nel calcolo;
  - il capitale si investe a progetto: un contratto ogni N mesi fino al budget.

I valori della rata e del prezzo di acquisto sono parametri: non c'e' nessun
numero commerciale "nascosto" nel codice.
"""

from dataclasses import dataclass, field
from typing import List, Optional

BUDGET_MAX_EUR = 1_000_000.0


@dataclass
class ContrattoTipo:
    """Un contratto di leasing tipo (un sito cliente)."""
    nome: str = "Sito tipo"
    punti_ac: int = 10
    punti_dc: int = 0
    capex_ac_eur: float = 2500.0
    capex_dc_eur: float = 15000.0
    rata_ac_mese_eur: float = 15.0   # per punto, pagata dal cliente
    rata_dc_mese_eur: float = 15.0
    durata_mesi: int = 48
    riscatto_eur: float = 1.0
    # Prezzo pagato dall'investitore per il contratto; None = CAPEX installato.
    prezzo_acquisto_eur: Optional[float] = None

    @property
    def capex_eur(self) -> float:
        return self.punti_ac * self.capex_ac_eur + self.punti_dc * self.capex_dc_eur

    @property
    def prezzo_eur(self) -> float:
        return self.capex_eur if self.prezzo_acquisto_eur is None else self.prezzo_acquisto_eur

    @property
    def rata_mese_eur(self) -> float:
        return self.punti_ac * self.rata_ac_mese_eur + self.punti_dc * self.rata_dc_mese_eur


@dataclass
class InvestitoreInputs:
    budget_eur: float = BUDGET_MAX_EUR
    contratto: ContrattoTipo = field(default_factory=ContrattoTipo)
    quota_investitore_pct: float = 50.0
    mesi_tra_progetti: int = 1        # 0 = tutti i contratti comprati subito
    insolvenza_annua_pct: float = 1.0  # quota di contratti che smette di pagare ogni anno
    recupero_insolvenza_pct: float = 0.0  # % del debito residuo recuperato all'insolvenza
    tasso_sconto_annuo: float = 0.08


def irr_mensile(cfs: List[float]) -> Optional[float]:
    """IRR mensile per bisezione; None se i flussi non cambiano segno."""
    if not cfs or min(cfs) >= 0 or max(cfs) <= 0:
        return None

    def van(r):
        return sum(cf / (1 + r) ** t for t, cf in enumerate(cfs))

    lo, hi = -0.99, 1.0
    f_lo, f_hi = van(lo), van(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = van(mid)
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def rata_per_rendimento(prezzo_punto_eur: float, durata_mesi: int, quota_pct: float,
                        rendimento_annuo: float, riscatto_eur: float = 0.0) -> float:
    """Rata mensile per punto che il cliente deve pagare perche' l'investitore
    ottenga il rendimento annuo indicato (senza insolvenze)."""
    r = (1 + rendimento_annuo) ** (1 / 12) - 1
    quota = quota_pct / 100.0
    if quota <= 0:
        raise ValueError("quota_investitore_pct deve essere > 0")
    vp_riscatto = riscatto_eur / (1 + r) ** durata_mesi
    fattore = durata_mesi if r == 0 else (1 - (1 + r) ** -durata_mesi) / r
    return (prezzo_punto_eur - vp_riscatto) / fattore / quota


def simula_investitore(inp: InvestitoreInputs) -> dict:
    if not 0 < inp.budget_eur <= BUDGET_MAX_EUR:
        raise ValueError(f"budget_eur deve essere tra 0 e {BUDGET_MAX_EUR:,.0f}")
    c = inp.contratto
    if c.durata_mesi <= 0:
        raise ValueError("durata_mesi deve essere > 0")
    if c.prezzo_eur <= 0:
        raise ValueError("il contratto deve avere almeno un punto di ricarica")

    n_contratti = int(inp.budget_eur // c.prezzo_eur)
    if n_contratti == 0:
        raise ValueError("budget inferiore al prezzo di un contratto")
    passo = max(0, int(inp.mesi_tra_progetti))
    quota = inp.quota_investitore_pct / 100.0
    sopravv_mese = (1 - inp.insolvenza_annua_pct / 100.0) ** (1 / 12)
    p_default_mese = 1 - sopravv_mese
    rata_inv = c.rata_mese_eur * quota

    orizzonte = passo * (n_contratti - 1) + c.durata_mesi + 1
    flussi = [0.0] * orizzonte
    investito = rata_tot = riscatti = recuperi = 0.0

    for k in range(n_contratti):
        m0 = passo * k
        flussi[m0] -= c.prezzo_eur
        investito += c.prezzo_eur
        vivo = 1.0
        for m in range(1, c.durata_mesi + 1):
            # Flusso atteso: il contratto paga se non e' ancora insolvente.
            insolventi = vivo * p_default_mese
            vivo -= insolventi
            incasso = vivo * rata_inv
            residuo = rata_inv * (c.durata_mesi - m)
            rec = insolventi * residuo * inp.recupero_insolvenza_pct / 100.0
            flussi[m0 + m] += incasso + rec
            rata_tot += incasso
            recuperi += rec
        risc = vivo * c.riscatto_eur * quota
        flussi[m0 + c.durata_mesi] += risc
        riscatti += risc

    irr_m = irr_mensile(flussi)
    irr_annuo = None if irr_m is None else (1 + irr_m) ** 12 - 1
    r_m = (1 + inp.tasso_sconto_annuo) ** (1 / 12) - 1
    van = sum(cf / (1 + r_m) ** t for t, cf in enumerate(flussi))
    cumulato, payback, cum = [], None, 0.0
    for t, cf in enumerate(flussi):
        cum += cf
        cumulato.append(cum)
        if payback is None and t > 0 and cum >= 0 and t >= passo * (n_contratti - 1):
            payback = t
    incassato = rata_tot + riscatti + recuperi

    anni = (orizzonte + 11) // 12
    annuali = [round(sum(flussi[a * 12:(a + 1) * 12]), 2) for a in range(anni)]

    punto_ac = rata_per_rendimento(c.capex_ac_eur, c.durata_mesi, inp.quota_investitore_pct,
                                   0.0, c.riscatto_eur)
    punto_ac_10 = rata_per_rendimento(c.capex_ac_eur, c.durata_mesi, inp.quota_investitore_pct,
                                      0.10, c.riscatto_eur)
    punto_dc = rata_per_rendimento(c.capex_dc_eur, c.durata_mesi, inp.quota_investitore_pct,
                                   0.0, c.riscatto_eur)
    punto_dc_10 = rata_per_rendimento(c.capex_dc_eur, c.durata_mesi, inp.quota_investitore_pct,
                                      0.10, c.riscatto_eur)

    return {
        "n_contratti": n_contratti,
        "punti_ac": n_contratti * c.punti_ac,
        "punti_dc": n_contratti * c.punti_dc,
        "capitale_investito_eur": round(investito, 2),
        "budget_non_investito_eur": round(inp.budget_eur - investito, 2),
        "incassato_eur": round(incassato, 2),
        "utile_eur": round(incassato - investito, 2),
        "moic": round(incassato / investito, 3),
        "irr_annuo": None if irr_annuo is None else round(irr_annuo, 4),
        "van_eur": round(van, 2),
        "payback_mesi": payback,
        "flussi_mensili_eur": [round(x, 2) for x in flussi],
        "cumulato_mensile_eur": [round(x, 2) for x in cumulato],
        "flussi_annuali_eur": annuali,
        "rata_pareggio_mese_eur": {"ac": round(punto_ac, 2), "dc": round(punto_dc, 2)},
        "rata_rendimento_10pct_mese_eur": {"ac": round(punto_ac_10, 2), "dc": round(punto_dc_10, 2)},
        "ipotesi": {
            "quota_investitore_pct": inp.quota_investitore_pct,
            "durata_mesi": c.durata_mesi,
            "rata_ac_mese_eur": c.rata_ac_mese_eur,
            "rata_dc_mese_eur": c.rata_dc_mese_eur,
            "riscatto_eur": c.riscatto_eur,
            "insolvenza_annua_pct": inp.insolvenza_annua_pct,
            "manutenzione": "a carico di EV Field Service",
            "imposte_incentivi": "esclusi",
        },
    }
