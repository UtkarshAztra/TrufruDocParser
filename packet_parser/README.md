# packet_parser: `ai_parse_document` JSON -> final receiving-packet JSON (rules only, no LLM)

Second stage after `pdf_parser`. `pdf_parser` turns a PDF into `<stem>.ai_parse.json` (the raw Databricks
`ai_parse_document` output); `packet_parser` reads that file and writes `<stem>.final.json`: the three
documents of an inbound receiving packet (inspection report, purchase order, bill of lading) as structured
fields, one merged traceability row, per-field provenance and cross-document checks.

Ported from `TruFru-exp/experiments/v1/regex_parser/` (same extraction code; output verified identical on the
17 `bol_iqf` test packets). Nothing is sent to a language model and no Databricks call is made.

```
<stem>.ai_parse.json --load--> pages of elements (flat searchable text per page, tables as rows)
                     --classify--> IMIR | VDF_PO | BOL_FreezPak | BOL_ColdStore | BOL_other | COA | other
                     --extract--> inspection_report / purchase_order / bill_of_lading
                     --merge--> traceability row (printed sources before handwritten ones)
                     --validate--> cross_checks + numeric_confirm + master_match --> <stem>.final.json
```

## Input: `.ai_parse.json`, not `.elements.json`

The parser reads the **raw** service output. `<stem>.elements.json` is a flattened view derived from the same
file (empty table rows dropped, 1-based pages, no struck-out `<s>` or checkbox markers); the extractors need
the raw table HTML to tell a ticked box from a crossed-out one, so they load `.ai_parse.json` directly.

## PDF -> final JSON in one command

The full pipeline has two stages. `pdf_parser --final` runs both in one go.

```
                      stage 1: pdf_parser                      stage 2: packet_parser
                      (Databricks ai_parse_document)           (rules only, local, no LLM)

  PDF  ---------->  <stem>.ai_parse.json  ---------->  <stem>.final.json
                    <stem>.elements.json                  inspection_report
                                                          purchase_order
                                                          bill_of_lading
                                                          traceability + cross_checks
```

1. **pdf_parser** uploads the PDF to the Unity Catalog Volume, runs `ai_parse_document` on the SQL warehouse,
   and saves the raw result as `<stem>.ai_parse.json` (plus the flattened `<stem>.elements.json`).
2. **packet_parser** reads `<stem>.ai_parse.json`, classifies the pages, extracts the three documents, merges
   them into the traceability row and writes `<stem>.final.json` next to it.

Stage 1 needs the project-root `.env` (Databricks host, warehouse, volume; see `pdf_parser/README.md`).
Stage 2 needs nothing.

**One PDF:**

```powershell
.venv\Scripts\python -m pdf_parser "docs\official-docs\BOL IQF Strawberries\STRAWBERRY HALVED- PO# 20260089938.pdf" --out output\packets --json-only --final
```

**Whole folder:**

```powershell
.venv\Scripts\python -m pdf_parser "docs\official-docs\BOL IQF Strawberries" --out output\packets --json-only --final
```

**Where the output lands** (one sub-folder per PDF):

```
docs\official-docs\BOL IQF Strawberries\STRAWBERRY HALVED- PO# 20260089938.pdf
        |
        |  stage 1: pdf_parser (Databricks ai_parse_document)
        v
output\packets\STRAWBERRY_HALVED_PO_20260089938\STRAWBERRY_HALVED_PO_20260089938.ai_parse.json
output\packets\STRAWBERRY_HALVED_PO_20260089938\STRAWBERRY_HALVED_PO_20260089938.elements.json
        |
        |  stage 2: packet_parser (rules)
        v
output\packets\STRAWBERRY_HALVED_PO_20260089938\STRAWBERRY_HALVED_PO_20260089938.final.json   <-- final output

output\packets\manifest.json   one row per PDF, with the final_json path
```

Drop `--json-only` to also get the `.md` / `.parsed.md` / `.parsed.html` views. If stage 2 fails for a packet,
the stage 1 files are kept and a warning is printed, so the Databricks work is never lost; re-run
`packet_parser` alone (next section) to regenerate `.final.json` without calling Databricks.

## Run packet_parser alone (ai_parse.json -> final.json)

Run from the project root in PowerShell. Input is always a `<stem>.ai_parse.json` written by `pdf_parser`
(or a folder containing them); output is one `<stem>.final.json` per packet plus a `summary.json`.

### A whole run folder, outputs collected in one place

```powershell
.venv\Scripts\python -m packet_parser docs\my_docs\data --out output\final
```

```
docs\my_docs\data\**\<stem>.ai_parse.json  ------->  output\final\<stem>.final.json
                                                     output\final\summary.json
```

Every `*.ai_parse.json` under `docs\my_docs\data` (searched recursively) becomes one file in `output\final\`.

### One document folder, output next to the input

```powershell
.venv\Scripts\python -m packet_parser docs\my_docs\data\many\IQF_STRAWBERRY_PO_20260089932
```

```
docs\my_docs\data\many\IQF_STRAWBERRY_PO_20260089932\
├── IQF_STRAWBERRY_PO_20260089932.ai_parse.json   (input, from pdf_parser)
├── IQF_STRAWBERRY_PO_20260089932.elements.json
├── IQF_STRAWBERRY_PO_20260089932.final.json      <-- written here
└── summary.json                                  <-- written here
```

Without `--out`, each `.final.json` lands next to its `.ai_parse.json` and `summary.json` goes into the folder
given as input.

### Other variations

```powershell
# one file
.venv\Scripts\python -m packet_parser some\dir\<stem>.ai_parse.json --out output\final

# only some packets of a run
.venv\Scripts\python -m packet_parser docs\my_docs\data --doc STRAWBERRY_HALVED_PO_20260089938 --doc STRAWBERRY_HALVED_PO_20260089945
```

`uv run parse-packet ...` works the same.

| Flag | Meaning |
|---|---|
| `input` | a `<stem>.ai_parse.json`, a document folder, or a whole run folder (searched recursively) |
| `--out DIR` | write every `<stem>.final.json` into DIR (default: next to its `.ai_parse.json`) |
| `--doc STEM` | only these packets (repeatable) |
| `--summary PATH` | where `summary.json` goes (default: `<out>/summary.json`, else `<input>/summary.json`) |
| `--master-dir DIR` | folder with `master_keywords.csv`, `master_products.csv`, `master_lots.csv` (default `docs/official-docs`, or `$TRUFRU_MASTER_DIR`) |
| `--no-master` | skip the fuzzy matcher / master-data check (`master_match` = `null`) |
| `--no-numeric` | skip the numeric confirmation (`numeric_confirm` = `null`) |
| `--quiet` | no per-packet console lines |

Console line per packet: page kinds in order (`IMIR VDF_ BOL_`), plus `FALLBACK:` when a BOL layout is unknown,
`SCHEMA:` when the packet failed validation, `FAILED CHECKS:` when a cross-check failed, `NUMERIC:` when a number
is in conflict, `MASTER:` with the matcher / master-data flags.

## Three check layers (flags only, nothing is changed)

After extraction the packet goes through three independent, deterministic checks. None of them edits a value;
each only reports what agrees and what needs a human look.

| Layer | Module | Compares |
|---|---|---|
| `cross_checks` | `checks.py` | the same value on the IMIR, PO and BOL (PO number, item, lot, seal, counts, weights, temperature spec) |
| `numeric_confirm` | `numeric_confirm.py` + `text_reader.py` | **every number** against (A) the printed value found in the document text and (B) the math rules R1-R15 |
| `master_match` | `fuzzy_matcher/` + `master_check.py` | the extracted item, origin, organic tick-box, lots and cut against the master CSVs in `docs/official-docs` |

Both new layers were ported from the v1 experiments (`TruFru-exp/experiments/v1/fuzzy_matcher` and
`interdependent_numeric_data`; output verified identical on the 78 test3 packets). They read the same
`===== PAGE n =====` / `[type] content` text that `pdf_parser` writes as `<stem>.parsed.md`, but `packet_parser`
renders that text itself from the loaded `.ai_parse.json` (`elements.parsed_text`), so no extra input file is needed.

**`numeric_confirm`** (interdependent numeric data). For `quantity_cases x case_weight_lbs = net_weight_lbs` all three
numbers are checked, each by every rule it takes part in and by the printed text. Rules: R1 cases x case weight = net
· R2 IMIR lot math · R3 BOL line math · R4/R5 BOL line sums = totals · R6 gross >= net · R7 cases agree across
documents · R8 net agrees · R9 gross agrees · R10 net = PO ordered (soft) · R11 case weight agrees · R12 pallets (soft)
· R13 temperature traceability = IMIR · R14 temperature in spec (report only) · R15 damaged <= cases (report only).
Each number gets a status: `CONFIRMED_TEXT` (equals the printed value) · `CONFIRMED_MATH` (a strict rule holds) ·
`CONFLICT` (the text or a strict rule disagrees and this number is the likely culprit; `should_be` gives the value the
rule implies) · `WARN` (only a soft rule disagrees) · `WEAK` (soft support only) · `UNVERIFIED` · `MISSING`.

**`master_match`** (fuzzy matcher). `rapidfuzz` finds item codes, lots, descriptions, origins and attributes from
the master CSVs in the OCR text, with OCR repairs (`CO1547. AD` -> `C01547.AD`, `25O919B` -> `250919B`) and a
1-edit near-match rule for long identifiers. The verdict is `CONFIRMED` / `LIKELY` / `AMBIGUOUS` / `NEW_VARIANT` /
`CONFLICT` / `NO_MATCH` with flags such as `origin_mismatch`, `unknown_item_code`, `near_match`. The checks
`master_item_code`, `master_origin`, `master_organic`, `master_lots`, `master_cut` then compare the extraction with
that verdict. The verdict also feeds the traceability merge (IMIR item number repaired via master data; origin fallback
when the IMIR value is unreadable). The CSVs are loaded once per process.

If the master CSVs are missing, `master_match` is written with a `not computed: FileNotFoundError ...` flag and
`needs_review: true`; the rest of the packet is unaffected.

## Output: `<stem>.final.json`

| Key | What it is |
|---|---|
| `source_file` | the stem (`IQF_STRAWBERRY_PO_20260089932`) |
| `parsed_at` | UTC timestamp |
| `parser` | `engine`, `ai_parse_document_version`, `input_path`, `page_kinds` (one per page), `templates_detected`, `fallback_reason`, `schema_error`, `master_dir` |
| `po_number_normalized` | PO digits only (`20260089932`); falls back to the 11 digits in the file name |
| `lot_identifier_type` | always `supplier_lot` (C items are traced by supplier lot) |
| `packet.documents_present` | `inspection_report`, `purchase_order`, `bill_of_lading`, `coa`, `other` |
| `packet.inspection_report` | IMIR form: header fields, every Yes/No/N/A tick as `yes` / `no` / `na` / `blank`, lot rows, temperature, disposition, signature *presence* and dates |
| `packet.purchase_order` | PO number, vendor, order date, line items, handwritten lot / date / notes |
| `packet.bill_of_lading` | parties, key/value header, line items, totals, TRUCK ARRIVED / DEPARTED stamps (initials never copied) |
| `packet.traceability` | the one-row join record: PO, item, lots, cases, weights, pallets, dates, times, carrier, seal, temperature, accepted |
| `packet.low_confidence_fields` | dotted paths with the reason (`ocr confidence 0.40`, `handwriting`, `from IMIR`, ...) |
| `sources` | dotted path -> `{element, page, type, confidence}` of the element the value was read from |
| `cross_checks` | `po_number_consistent`, `item_number_consistent`, `lot_consistent`, `seal_consistent`, `bol_weight_math`, `imir_weight_math`, `case_count_consistent`, `net_weight_consistent`, `temperature_in_spec`; status `pass` / `warn` / `fail` / `unknown` |
| `numeric_confirm` | `verdict`, `counts` per status, `confirmed` / `checked`, `rules` (id, name, PASS / FAIL / WARN / SKIP, formula, suspects, should_be), `numbers` (one per field: value, status, should_be, text_match, rules_agree, rules_disagree), `needs_review` |
| `master_match` | `matcher_status`, `matcher_item`, `matcher_candidates`, `matcher_flags`, `observed` (origins / cuts / organic tick-box seen in the text), `lots_found`, `checks` (the five `master_*` checks), `flags`, `needs_review` |

Field definitions live in `schema.py` (pydantic). Dates are ISO `YYYY-MM-DD`, times `HH:MM` 24h.
`summary.json` carries per packet `numeric_conflicts`, `numeric_verdict`, `master_item`, `master_flags` and `needs_review`.

PII rule: signatures and initials are reported as booleans only; initials seen on the stamps are also scrubbed
from every free-text field.

Packets that are not receiving packets (for example a COA PDF) still produce a file: `documents_present`
says what was found, the three document blocks are `null`, and `fallback_reason` explains why.

## Library use

```python
from packet_parser import build_final, write_final

result = build_final("docs/my_docs/data/many/IQF_STRAWBERRY_PO_20260089932/IQF_STRAWBERRY_PO_20260089932.ai_parse.json")
print(result["packet"]["traceability"]["vdf_lot_number"])

path, result = write_final(ai_parse_path, out_dir="output/final")
result = build_final(ai_parse_path, master=False)                 # skip the matcher; numeric=False skips the numbers
result = build_final(ai_parse_path, master_dir="path/to/csvs")    # other master data

# the two layers on their own
from packet_parser.fuzzy_matcher import get_matcher, match_text
verdict = match_text("Item#: CO1547. AD Strawberry Halved Peru lot LC25207", get_matcher())

from packet_parser.numeric_confirm import numeric_confirm
from packet_parser.text_reader import read_printed_text
block = numeric_confirm(result["packet"], read_printed_text(open("<stem>.parsed.md", encoding="utf-8").read()))
```

Tests: `python -m pytest -q tests` (matcher behaviour, numeric rules, pipeline wiring).

## Files

| File | Purpose |
|---|---|
| `cli.py` | `python -m packet_parser` entry point |
| `parse.py` | `parse_doc` (classify + extract + merge), validation, `build_final` / `write_final` |
| `elements.py` | loads `.ai_parse.json` into pages; every character of the page text maps back to its element |
| `classify.py` | page kind from printed titles and labels |
| `imir.py` | Incoming Material Inspection Report |
| `vdf_po.py` | Van Drunen Farms purchase order (printed lines + handwriting) |
| `bol_freezpak.py` / `bol_coldstore.py` / `bol_generic.py` | the three BOL layouts (generic = unknown layout, flagged for review) |
| `merge.py` | traceability row with per-field source priority |
| `normalize.py` | dates, times, numbers, tick marks |
| `common.py` | `Ctx` (sources + flags + initials to scrub) and the label -> value rule |
| `checks.py` | cross-document checks |
| `numeric_confirm.py` | rules R1-R15, witnesses per number, status and likely culprit (`numeric_confirm` block) |
| `text_reader.py` | printed numbers from the document text: 3 BOL table layouts, PO `NN,NNN.NN LB`, pallet notes |
| `fuzzy_matcher/` | master CSV index (`master_data.py`), keyword finder (`matcher.py`), verdict (`resolver.py`), OCR normalisation (`normalize.py`), thresholds and paths (`config.py`) |
| `master_check.py` | the five `master_*` checks of the extraction against the matcher verdict (`master_match` block) |
| `schema.py` | pydantic models = the contract of the final JSON |
