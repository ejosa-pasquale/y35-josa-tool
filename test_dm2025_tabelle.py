"""Minimi DM 28/10/2025 sul testo ufficiale (Allegato 1, Cap. 6, Tabelle 4 e 5, GU n. 283 del 5/12/2025)."""
from datetime import date

from josa_core.compliance_dm2025 import BuildingProfile, compute_dm2025, compare_with_hardware_config

OGGI = date(2026, 11, 11)

def dm(posti, tipo, pubblico=False):
    r = compute_dm2025(BuildingProfile(residenziale=False, posti_auto=posti, tipo_intervento=tipo,
                                       accesso_pubblico=pubblico), OGGI)
    return (r.punti_tipologia_a_minimi_a_regime, r.punti_tipologia_b_minimi_a_regime,
            r.punti_tipologia_a_applicabili_oggi, r.punti_tipologia_b_applicabili_oggi)


# --- Tabella 5 (accesso privato) ---
assert dm(10, "nuova_costruzione")[:2] == (0, 0)
assert dm(15, "nuova_costruzione")[:2] == (3, 0)            # 11-20: 3
assert dm(15, "ristrutturazione_importante")[:2] == (2, 0)  # 11-20: 2
assert dm(15, "esistente")[:2] == (0, 0)                    # esistenti da 21 posti
assert dm(40, "nuova_costruzione")[:2] == (6, 0)            # 3 ogni 20
assert dm(55, "esistente") == (9, 0, 3, 0)                  # 3 ogni 20; oggi 50% dei valori: 1 ogni 20
assert dm(100, "esistente")[:2] == (15, 0)
assert dm(101, "esistente")[:2] == (9, 1)                   # 3 ogni 50 + 1 B
assert dm(501, "nuova_costruzione")[:2] == (33, 2)
assert dm(1200, "ristrutturazione_importante")[:2] == (48, 2)
# --- Tabella 4 (accesso pubblico) ---
assert dm(55, "nuova_costruzione", True)[:2] == (6, 0)      # 2 ogni 20
assert dm(200, "nuova_costruzione", True)[:2] == (8, 1)
assert dm(300, "ristrutturazione_importante", True)[:2] == (6, 1)
assert dm(1200, "esistente", True)[:2] == (48, 4)

# 100 -> 101 posti: in equivalente A (1 B = 10 A) il minimo non scende
a100, b100 = dm(100, "esistente")[:2]
a101, b101 = dm(101, "esistente")[:2]
assert a101 + 10 * b101 >= a100 + 10 * b100

# DC sotto 50 kW non e' Tipologia B; sotto 7,4 kW non conta
r = compute_dm2025(BuildingProfile(residenziale=False, posti_auto=40, tipo_intervento="nuova_costruzione"), OGGI)
c = compare_with_hardware_config(r, {"AC 3.7kW": 4, "AC 22kW": 2, "DC 30kW": 1, "DC 60kW": 1},
                                 {"AC 3.7kW": {"p": 3.7}, "AC 22kW": {"p": 22}, "DC 30kW": {"p": 30}, "DC 60kW": {"p": 60}})

print("OK: minimi DM 28/10/2025 (Tabelle 4 e 5), fase transitoria, Tipologia A/B.")
