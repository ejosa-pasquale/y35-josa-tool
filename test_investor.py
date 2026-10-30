"""Test del simulatore investitore (josa_core.investor).

Uso: python3 test_investor.py
"""

from josa_core.investor import (ContrattoTipo, InvestitoreInputs, rata_per_rendimento,
                                simula_investitore)


def test_rata_al_rendimento_obiettivo_restituisce_quel_rendimento():
    rata = rata_per_rendimento(2500, 48, 50, 0.10, riscatto_eur=1.0)
    c = ContrattoTipo(punti_ac=10, rata_ac_mese_eur=rata)
    r = simula_investitore(InvestitoreInputs(contratto=c, insolvenza_annua_pct=0, mesi_tra_progetti=0))
    assert abs(r["irr_annuo"] - 0.10) < 1e-3, r["irr_annuo"]


def test_budget_e_numero_contratti():
    r = simula_investitore(InvestitoreInputs(budget_eur=1_000_000, contratto=ContrattoTipo(punti_ac=10)))
    assert r["n_contratti"] == 40
    assert r["capitale_investito_eur"] == 1_000_000


def test_insolvenze_riducono_il_rendimento():
    c = ContrattoTipo(rata_ac_mese_eur=130)
    sano = simula_investitore(InvestitoreInputs(contratto=c, insolvenza_annua_pct=0))
    rischio = simula_investitore(InvestitoreInputs(contratto=c, insolvenza_annua_pct=3))
    assert rischio["irr_annuo"] < sano["irr_annuo"]


def test_budget_oltre_il_massimo_rifiutato():
    try:
        simula_investitore(InvestitoreInputs(budget_eur=1_500_000))
    except ValueError:
        return
    raise AssertionError("budget oltre 1M accettato")


if __name__ == "__main__":
    for nome, f in list(globals().items()):
        if nome.startswith("test_"):
            f()
            print("OK", nome)
