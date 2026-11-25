"""Parse local PDFs concurrently and save one result directory per PDF.

The functions in this module can also be called from a web application's upload
handler. No Gmail credentials or network access are needed.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Iterable

import pymupdf

from pdf_mail_parser import extract_pdf_text


KST = timezone(timedelta(hours=9))


def parse_pdf_bytes(data: bytes, filename: str) -> dict[str, str]:
    """Return a JSON-ready result for a PDF upload without writing to disk."""
    try:
        return {"filename": filename, "status": "success", "text": extract_pdf_text(data)}
    except (ValueError, RuntimeError, pymupdf.FileDataError) as error:
        return {"filename": filename, "status": "error", "error": str(error), "text": ""}


def _parse_path(path: Path) -> dict[str, str]:
    try:
        return parse_pdf_bytes(path.read_bytes(), path.name)
    except OSError as error:
        return {"filename": path.name, "status": "error", "error": str(error), "text": ""}


def _safe_stem(stem: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")[:100]
    return name or "pdf"


def _new_result_dir(root: Path, stem: str, timestamp: str) -> Path:
    base = f"{_safe_stem(stem)}_{timestamp}"
    for number in range(10000):
        candidate = root / (base if number == 0 else f"{base}_{number + 1}")
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise FileExistsError(f"too many results for {base}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _claim(db: sqlite3.Connection, digest: str) -> tuple[bool, str]:
    """Atomically reserve a PDF hash across CLI runs and web requests."""
    now = datetime.now(KST)
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT status, result_dir, updated_at FROM processed WHERE sha256 = ?", (digest,)
        ).fetchone()
        if row:
            status, result_dir, updated_at = row
            complete = result_dir and (Path(result_dir) / "output.json").is_file()
            in_progress = status == "processing" and now - datetime.fromisoformat(updated_at) < timedelta(hours=1)
            if (status == "success" and complete) or in_progress:
                return False, result_dir or ""
            db.execute(
                "UPDATE processed SET status = 'processing', result_dir = '', updated_at = ? WHERE sha256 = ?",
                (now.isoformat(), digest),
            )
        else:
            db.execute(
                "INSERT INTO processed (sha256, status, result_dir, updated_at) VALUES (?, 'processing', '', ?)",
                (digest, now.isoformat()),
            )
    return True, ""


def parse_pdf_files(
    paths: Iterable[str | Path],
    output_dir: str | Path = "results",
    max_workers: int | None = None,
) -> list[dict[str, str]]:
    """Parse PDFs in separate processes and write text/JSON per PDF.

    Results follow input order. A content hash identifies PDFs already parsed
    successfully, including renamed copies and concurrent requests.
    """
    pdf_paths = [Path(path).resolve() for path in paths]
    if not pdf_paths:
        return []
    if max_workers is not None and max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(KST).strftime("%Y-%m-%d_%H-%M-%S")
    with closing(sqlite3.connect(root / "index.sqlite3", timeout=30)) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS processed ("
            "sha256 TEXT PRIMARY KEY, status TEXT NOT NULL, result_dir TEXT, updated_at TEXT NOT NULL)"
        )
        pending: list[tuple[int, Path, str]] = []
        results: list[dict[str, str] | None] = [None] * len(pdf_paths)
        for index, path in enumerate(pdf_paths):
            try:
                digest = _sha256_file(path)
            except OSError as error:
                results[index] = {"filename": path.name, "status": "error", "error": str(error), "text": "", "source_path": str(path), "result_dir": ""}
                continue
            claimed, existing_dir = _claim(db, digest)
            if claimed:
                pending.append((index, path, digest))
            else:
                results[index] = {"filename": path.name, "status": "duplicate", "text": "", "source_path": str(path), "result_dir": existing_dir}

        if pending:
            workers = min(max_workers or min(4, os.cpu_count() or 1), len(pending))
            with ProcessPoolExecutor(max_workers=workers) as executor:
                parsed = executor.map(_parse_path, [item[1] for item in pending])
                for (index, path, digest), result in zip(pending, parsed):
                    folder = _new_result_dir(root, path.stem, timestamp)
                    result["sha256"] = digest
                    (folder / "output.txt").write_text(result["text"], encoding="utf-8")
                    (folder / "output.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    with db:
                        db.execute(
                            "UPDATE processed SET status = ?, result_dir = ?, updated_at = ? WHERE sha256 = ?",
                            (result["status"], str(folder), datetime.now(KST).isoformat(), digest),
                        )
                    results[index] = {**result, "source_path": str(path), "result_dir": str(folder)}
    return [result for result in results if result is not None]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse local PDFs into new text and JSON folders")
    parser.add_argument("pdfs", nargs="*", type=Path, help="PDF files; default: *.pdf in --input-dir")
    parser.add_argument("--input-dir", type=Path, default=Path("pdfs"), help="directory to scan when no PDF files are given (default: pdfs)")
    parser.add_argument("--output-dir", type=Path, default=Path("results"), help="parent directory for new results")
    parser.add_argument("--workers", type=int, default=None, help="parallel processes (default: up to 4)")
    args = parser.parse_args(argv)
    paths = args.pdfs or sorted(args.input_dir.glob("*.pdf"))
    if not paths:
        parser.error("no PDF files found")
    try:
        results = parse_pdf_files(paths, args.output_dir, args.workers)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"error: {error}\n")
    for result in results:
        print(f"{result['status']}: {result['filename']} -> {result['result_dir']}")
    return 1 if any(result["status"] == "error" for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
