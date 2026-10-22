"""Databricks side of the parser: Volume upload, SQL execution, ``ai_parse_document``.

Pipeline for one run:

    local PDFs --Files API--> <volume>/pdf_parser/<run_id>/batch_<n>/   (``--batch-size`` files per folder)
    SELECT path, to_json(ai_parse_document(content, map('version', ...))) FROM READ_FILES(<batch folder>)
    -> {volume_path: parsed_json_dict}

Batching keeps every statement result under the INLINE result-size limit of the Statement Execution API
(one parsed PDF is typically 50-300 KB of JSON).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import Disposition, Format, StatementState

DEFAULT_BATCH_SIZE = 4


# ------------------------------------------------------------------ naming
def safe_name(name: str) -> str:
    """'STRAWBERRY HALVED- PO# 20260089938.pdf' -> 'STRAWBERRY_HALVED_PO_20260089938.pdf'"""
    stem, ext = os.path.splitext(name)
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9.]+", "_", stem)).strip("_") + ext.lower()


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:6]


def batched(items: list[Path], size: int) -> Iterable[list[Path]]:
    size = max(1, size)
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ------------------------------------------------------------------ 1. upload
def upload_pdfs(w: WorkspaceClient, pdfs: list[Path], volume_dir: str, quiet: bool = False) -> dict[str, Path]:
    """Upload each PDF to ``volume_dir`` (created if missing). Returns {volume_path: local_path}.

    Two local files that sanitise to the same name get a numeric suffix so neither is overwritten.
    """
    w.files.create_directory(volume_dir)
    mapping: dict[str, Path] = {}
    taken: set[str] = set()
    for pdf in pdfs:
        name = safe_name(pdf.name)
        stem, ext = os.path.splitext(name)
        n = 1
        while name in taken:
            n += 1
            name = f"{stem}_{n}{ext}"
        taken.add(name)
        target = f"{volume_dir}/{name}"
        with pdf.open("rb") as fh:
            w.files.upload(target, fh, overwrite=True)
        mapping[target] = pdf
        if not quiet:
            print(f"  uploaded  {pdf.name} -> {target}")
    return mapping


def delete_uploads(w: WorkspaceClient, volume_paths: Iterable[str], folders: Iterable[str] = ()) -> None:
    """Best-effort removal of the files (and then the empty run folders) this run created."""
    for p in volume_paths:
        try:
            w.files.delete(p)
        except Exception as exc:  # noqa: BLE001
            print(f"  could not delete {p}: {exc}")
    for d in sorted(set(folders), key=len, reverse=True):  # deepest first
        try:
            w.files.delete_directory(d)
        except Exception as exc:  # noqa: BLE001
            print(f"  could not delete folder {d}: {exc}")


# ------------------------------------------------------- 2. ai_parse_document
def run_sql(w: WorkspaceClient, warehouse_id: str, sql: str, timeout_s: int = 1800) -> list[list[Any]]:
    """Execute one statement on a SQL warehouse and return every row (all chunks) as a list of lists."""
    resp = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=sql,
        wait_timeout="50s",
        disposition=Disposition.INLINE,
        format=Format.JSON_ARRAY,
    )
    deadline = time.time() + timeout_s
    while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        if time.time() > deadline:
            w.statement_execution.cancel_execution(resp.statement_id)
            raise TimeoutError(f"SQL statement {resp.statement_id} exceeded {timeout_s}s")
        time.sleep(5)
        resp = w.statement_execution.get_statement(resp.statement_id)
    if resp.status.state != StatementState.SUCCEEDED:
        err = resp.status.error
        raise RuntimeError(f"SQL failed ({resp.status.state}): {err.message if err else 'unknown error'}")

    rows = list(resp.result.data_array or []) if resp.result else []
    chunk = resp.result.next_chunk_index if resp.result else None
    while chunk is not None:
        part = w.statement_execution.get_statement_result_chunk_n(resp.statement_id, chunk)
        rows.extend(part.data_array or [])
        chunk = part.next_chunk_index
    return rows


def parse_sql(volume_dir: str, version: str, image_output_path: Optional[str] = None) -> str:
    """The ``ai_parse_document`` statement for one folder of PDFs (also usable as-is in a notebook)."""
    folder = volume_dir.replace("'", "''")
    opts = [f"'version', '{version}'"]
    if image_output_path:
        opts.append(f"'imageOutputPath', '{image_output_path.replace(chr(39), chr(39) * 2)}'")
    return f"""
        SELECT path,
               to_json(ai_parse_document(content, map({', '.join(opts)}))) AS parsed
        FROM READ_FILES('{folder}', format => 'binaryFile', fileNamePattern => '*.pdf')
    """


def ai_parse_documents(
    w: WorkspaceClient,
    warehouse_id: str,
    volume_dir: str,
    version: str = "2.0",
    image_output_path: Optional[str] = None,
    timeout_s: int = 1800,
) -> dict[str, dict]:
    """Run ``ai_parse_document`` over every PDF in ``volume_dir``. Returns {volume_path: parsed_dict}."""
    out: dict[str, dict] = {}
    for path, parsed in run_sql(w, warehouse_id, parse_sql(volume_dir, version, image_output_path), timeout_s):
        out[path.removeprefix("dbfs:")] = json.loads(parsed) if parsed else {}
    return out


def upload_and_parse(
    w: WorkspaceClient,
    pdfs: list[Path],
    volume_dir: str,
    warehouse_id: str,
    version: str = "2.0",
    batch_size: int = DEFAULT_BATCH_SIZE,
    image_output_path: Optional[str] = None,
    cleanup: bool = False,
    quiet: bool = False,
) -> tuple[dict[str, Path], dict[str, dict], str]:
    """Upload in batches, parse each batch, optionally delete the uploads.

    Returns (mapping {volume_path: local_path}, parsed {volume_path: dict}, run folder on the volume).
    """
    run_dir = f"{volume_dir}/{new_run_id()}"
    mapping: dict[str, Path] = {}
    parsed: dict[str, dict] = {}
    folders: list[str] = [run_dir]
    batches = list(batched(pdfs, batch_size))
    try:
        for i, batch in enumerate(batches, 1):
            batch_dir = f"{run_dir}/batch_{i:03d}"
            folders.append(batch_dir)
            if not quiet:
                print(f"[upload {i}/{len(batches)}] {len(batch)} PDF(s) -> {batch_dir}")
            mapping.update(upload_pdfs(w, batch, batch_dir, quiet=quiet))
            if not quiet:
                print(f"[parse  {i}/{len(batches)}] ai_parse_document v{version} on warehouse {warehouse_id}")
            t0 = time.time()
            parsed.update(ai_parse_documents(w, warehouse_id, batch_dir, version, image_output_path))
            if not quiet:
                print(f"           done in {time.time() - t0:.0f}s")
    finally:
        if cleanup and mapping:
            if not quiet:
                print(f"[cleanup] removing {len(mapping)} uploaded file(s) from {run_dir}")
            delete_uploads(w, mapping.keys(), folders)
    return mapping, parsed, run_dir
