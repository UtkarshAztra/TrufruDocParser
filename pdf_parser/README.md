# pdf_parser: Databricks `ai_parse_document` only, no LLM

Parses PDFs into layout-aware text, tables and figure captions using the Databricks
[`ai_parse_document`](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_parse_document)
SQL function. Nothing is sent to a language model; the output is exactly what the parsing service returns,
re-rendered into files people and other code can use.

Lifted from the v1 experiment in `TruFru-exp/experiments/v1/Databricks_parser/` (steps 1 and 2 of that
pipeline, without the LLM extraction, cross-checks and master-data match).

```
local PDFs --Files API--> /Volumes/<catalog>/<schema>/<volume>/pdf_parser/<run_id>/batch_<n>/
                 --SQL warehouse--> SELECT path, to_json(ai_parse_document(content, map('version','2.0')))
                                    FROM READ_FILES('<batch folder>', format => 'binaryFile')
                 --local--> output/pdf_parser/<stem>/<stem>.ai_parse.json | .elements.json | .parsed.md | .md | .parsed.html
```

**Next step:** `packet_parser` turns each `<stem>.ai_parse.json` written here into `<stem>.final.json`
(structured inspection report / purchase order / bill of lading + traceability row), see `packet_parser/README.md`.

```
  PDF  ---------->  <stem>.ai_parse.json  ---------->  <stem>.final.json
       pdf_parser                         packet_parser
       (Databricks ai_parse_document)     (rules only, no LLM)
```

Add `--final` to any `pdf_parser` command to run both stages at once:

```powershell
# one PDF
.venv\Scripts\python -m pdf_parser "docs\official-docs\BOL IQF Strawberries\STRAWBERRY HALVED- PO# 20260089938.pdf" --out output\packets --json-only --final

# whole folder
.venv\Scripts\python -m pdf_parser "docs\official-docs\BOL IQF Strawberries" --out output\packets --json-only --final
```

## Setup

The project venv already has every dependency (`databricks-sdk`, `python-dotenv`, `pymupdf`).
`pyproject.toml` lists them and registers the `parse-pdf` script.

Project-root `.env`:

```dotenv
DATABRICKS_HOST=adb-xxxx.azuredatabricks.net
DATABRICKS_TOKEN=dapi...                        # PAT; if missing/invalid the CLI profile below is used
DATABRICKS_CONFIG_PROFILE=DEFAULT               # databricks auth login --profile DEFAULT
DATABRICKS_WAREHOUSE_ID=...                     # Pro or Serverless warehouse with AI Functions
TRUFRU_PARSER_VOLUME=/Volumes/<catalog>/<schema>/<volume>
TRUFRU_PARSER_VERSION=2.0                       # optional ai_parse_document version
```

Permissions: `READ VOLUME` + `WRITE VOLUME` on the volume and `CAN USE` on the warehouse.

## Run

Run from the project root in PowerShell. First make sure you are logged in (`databricks auth login --profile DEFAULT`).

### Parse one PDF

```powershell
.venv\Scripts\python -m pdf_parser "docs\official-docs\COA's\IQF STRAWBERRIES HALVED- PO# 20260079358.pdf" --out docs\my_docs\data --json-only
```

What it does:

1. Uploads the PDF to the Databricks Volume (`TRUFRU_PARSER_VOLUME/pdf_parser/<run_id>/batch_001/`).
2. Runs `ai_parse_document` on the SQL warehouse to read the text, tables and figures on every page.
3. Creates a folder named after the file under `docs\my_docs\data` and saves two JSON files in it:
   - `IQF_STRAWBERRIES_HALVED_PO_20260079358.ai_parse.json`: raw Databricks output.
   - `IQF_STRAWBERRIES_HALVED_PO_20260079358.elements.json`: the main data file, one record per element with page, type, text and table rows.
4. Writes `docs\my_docs\data\manifest.json` with a status line for the run.

### Parse every PDF in a folder

```powershell
.venv\Scripts\python -m pdf_parser "docs\official-docs\COA's" --out docs\my_docs\data --json-only
```

Same steps for every `*.pdf` in the folder (4 files per upload batch). Each PDF gets its own sub-folder under
`docs\my_docs\data` with its two JSON files, and `manifest.json` lists them all.

```
docs/my_docs/data/
├── manifest.json
├── IQF_STRAWBERRIES_HALVED_PO_20260079358/
│   ├── IQF_STRAWBERRIES_HALVED_PO_20260079358.ai_parse.json
│   └── IQF_STRAWBERRIES_HALVED_PO_20260079358.elements.json
└── IQF_STRAWBERRIES_HALVED_PO_20260079359/
    └── ...
```

### Useful variations

```powershell
# also write readable .md, .parsed.md and .parsed.html next to the JSON (drop --json-only)
.venv\Scripts\python -m pdf_parser "docs\official-docs\COA's" --out docs\my_docs\data

# tables as CSV (tables\) and page PNGs (images\) inside each document folder
.venv\Scripts\python -m pdf_parser "docs\official-docs\COA's" --out docs\my_docs\data --csv --images

# include PDFs in sub-folders
.venv\Scripts\python -m pdf_parser docs\official-docs --out docs\my_docs\data --recursive

# delete the uploaded copies from the Volume once parsed
.venv\Scripts\python -m pdf_parser "docs\official-docs\COA's" --out docs\my_docs\data --cleanup

# re-render an earlier run offline (no Databricks calls), e.g. to add .md/.csv later
.venv\Scripts\python -m pdf_parser "docs\official-docs\COA's" --from-parse docs\my_docs\data --out docs\my_docs\data --csv

# PDF -> final receiving-packet JSON in one go (adds <stem>.final.json, see packet_parser/README.md)
.venv\Scripts\python -m pdf_parser "docs\official-docs\BOL IQF Strawberries" --out output\packets --json-only --final

# show the SQL you could paste into a notebook
.venv\Scripts\python -m pdf_parser any.pdf --print-sql
```

`uv run parse-pdf ...` works the same once `uv sync` succeeds in your environment.

| Flag | Meaning |
|---|---|
| `--out DIR` | output folder (default `output/pdf_parser/`); each document gets its own sub-folder `<stem>/` |
| `--flat` | write `<stem>.*` files directly into `--out` instead of one folder per document |
| `--json-only` | write only `<stem>.ai_parse.json` and `<stem>.elements.json` |
| `--final` | also run `packet_parser` and write `<stem>.final.json` next to `<stem>.ai_parse.json` (see `packet_parser/README.md`) |
| `--batch-size N` | PDFs per upload folder and per SQL statement (default 4; keeps results inside the inline result limit) |
| `--csv` | write each table as `<stem>/tables/table_p<page>_<n>.csv` |
| `--images` | render `<stem>/images/page_<n>.png` locally with PyMuPDF |
| `--cleanup` | delete the uploaded PDFs (and run folders) from the Volume after parsing |
| `--profile P` | use a Databricks CLI profile instead of the `.env` PAT |
| `--volume`, `--warehouse`, `--parse-version` | override the `.env` values |
| `--image-output-path /Volumes/...` | pass `imageOutputPath` so the service writes its own page images to a Volume |
| `--from-parse DIR` | reuse `<stem>/<stem>.ai_parse.json` (or flat `<stem>.ai_parse.json`) from DIR; no upload, no SQL |

## Outputs (per PDF, `<stem>` = file name with spaces/punctuation replaced by `_`)

Default layout: one folder per document.

```
<out>/
├── manifest.json
└── <stem>/
    ├── <stem>.ai_parse.json      raw service output
    ├── <stem>.elements.json      flattened elements + table rows + summary  (main data file)
    ├── <stem>.parsed.md / .md / .parsed.html
    ├── tables/table_p<page>_<n>.csv   (--csv)
    └── images/page_<n>.png            (--images)
```

| File | What it is |
|---|---|
| `<stem>.ai_parse.json` | raw service output; feed it back with `--from-parse` |
| `<stem>.parsed.md` | `===== PAGE n =====` + one `[type] content` line per element, tables kept as HTML. Byte-identical to the v1 experiment's `.parsed.md`; `packet_parser` renders the same text itself for its fuzzy matcher and numeric confirmation, so this file is only for reading |
| `<stem>.md` | readable Markdown: headings, paragraphs, Markdown tables, figure/signature captions |
| `<stem>.parsed.html` | browser view per page; elements with confidence < 0.50 are highlighted |
| `<stem>.elements.json` | flat element list (`type, page, confidence, bbox, text, rows`) plus a summary block |
| `tables/*.csv` | with `--csv` |
| `images/*.png` | with `--images` |
| `manifest.json` | in `<out>/`: one row per PDF with status, output folder, page/element/table counts, Volume path, run folder |

Element types the service returns on these packets: `text`, `section_header`, `title`, `table` (content is HTML),
`figure` (content empty, `description` holds the caption), `signature`, `caption`, `footnote`, `page_header`,
`page_footer`, `page_number`. Empty form rows inside tables are dropped in `.md`, `.elements.json` and CSV
(the raw HTML in `.ai_parse.json` and `.parsed.md` keeps them).

## Library use

```python
from pathlib import Path
from pdf_parser import Settings, get_client, parsed_to_markdown
from pdf_parser.databricks_io import upload_and_parse
from pdf_parser.render import ParsedDocument

s = Settings.from_env()
w = get_client(s.profile)
mapping, parsed, run_dir = upload_and_parse(w, [Path("some.pdf")], s.volume_dir, s.warehouse_id, s.parse_version)
for vol_path, local in mapping.items():
    doc = ParsedDocument.from_raw(parsed[vol_path], local.name)
    print(doc.summary())
    for t in doc.tables():
        print(t["page"], t["rows"][:3])
    print(parsed_to_markdown(doc))
```

## Files

| File | Purpose |
|---|---|
| `cli.py` | `python -m pdf_parser` entry point |
| `config.py` | `.env` loading, `Settings`, `WorkspaceClient` (PAT first, CLI profile fallback) |
| `databricks_io.py` | Volume upload, Statement Execution polling, `ai_parse_document` SQL, batching, cleanup |
| `render.py` | `ParsedDocument` model and the text / Markdown / HTML / JSON renderers |
| `tables.py` | HTML table to rows / CSV / Markdown (standard library only) |
| `images.py` | optional local page PNGs via PyMuPDF |
