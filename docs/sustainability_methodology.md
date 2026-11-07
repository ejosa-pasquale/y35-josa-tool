# Sustainability Score Y35 — metodologia SS-1.0

> Indice interno Y35, non è un rating ESG certificato. I KPI sono utilizzabili come input per la rendicontazione (ESRS E1 / VSME).

Lo score (0-100) misura l'impatto ambientale del progetto di elettrificazione della flotta, accanto alla valutazione economica (TCO, ROI). Usa solo grandezze già calcolate dal motore: Carbon Accounting (metodologia allineata alla ISO 14064-2, verificabile da terza parte), simulazione a 15 minuti, FV in sito, picco di ricarica. Non modifica nessun altro risultato: è un blocco aggiuntivo `kpi.sustainability`.

## Pilastri e KPI

| Pilastro | Peso | KPI | Normalizzazione |
|---|---|---|---|
| Decarbonizzazione | 25 | Riduzione netta CO2e vs flotta termica (%) | lineare 0% → 0, 100% → pieno |
| | 15 | Intensità emissiva (g CO2e/km) | 0 g/km → pieno, ≥200 g/km → 0 |
| Elettrificazione | 20 | Quota km percorsi in elettrico (PHEV: solo entro l'autonomia elettrica) | lineare |
| Energia rinnovabile | 15 | Quota kWh di ricarica da FV in sito o garanzie d'origine | lineare |
| Ricarica in sede | 10 | Energia in sede / (sede + colonnine pubbliche) | lineare; la ricarica domestica pianificata non penalizza |
| Impatto sulla rete | 15 | Picco di ricarica / potenza di rete disponibile | ≤50% → pieno, ≥100% → 0 |

Classi: A ≥ 80, B ≥ 65, C ≥ 50, D ≥ 35, E < 35.

**KPI non disponibile** (es. potenza di rete non indicata): non vale zero. Il suo peso viene ridistribuito in proporzione sugli altri KPI e il risultato lo segnala (`kpi_non_disponibili`, `peso_ridistribuito`).

**Affidabilità del dato**: `stimato` (simulazione ex-ante), `parzialmente_misurato`, `misurato` (sessioni OCPP / curva POD). È riportata accanto allo score ma non entra nel calcolo.

## Fonti dei dati

| KPI | Fonte nel motore |
|---|---|
| Riduzione %, intensità g/km | `josa_core/carbon/quantify.py` (fattori ISPRA/MASE, versione in `versione_fattori`) |
| Km elettrici | `carbon.activities_from_groups` (stesse regole del generatore eventi) |
| Energia rinnovabile | `energia_kwh.deposito_fv` + `deposito_go` (FV in sito e quota GO del Carbon Accounting) |
| Ricarica in sede | `kpi.e_int`, `kpi.e_ext` della simulazione |
| Impatto sulla rete | massimo di `timeline_p_kw` e `policy.p_rete_kw` |

## Confronto nel tempo

- Le soglie sono **assolute**, non relative ad altri clienti: lo stesso valore vale lo stesso punteggio ogni anno.
- Ogni risultato riporta `versione_metodologia` (SS-1.0) e `versione_fattori`.
- Se la metodologia cambia (pesi o soglie), si pubblica una nuova versione e l'anno precedente si ricalcola con la nuova, come per l'anno base negli inventari GHG.
- Il codice è in `josa_core/sustainability.py`; i test in `test_sustainability.py`.
