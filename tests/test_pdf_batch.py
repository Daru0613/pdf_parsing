import json
from pathlib import Path

import pymupdf

from pdf_batch import parse_pdf_bytes, parse_pdf_files


def make_pdf(text: str) -> bytes:
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), text)
        return doc.tobytes()


def test_batch_creates_results_and_skips_prior_runs(tmp_path):
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(make_pdf("First document"))
    second.write_bytes(make_pdf("Second document"))

    results = parse_pdf_files([first, second], tmp_path / "results", max_workers=2)
    repeat = parse_pdf_files([first], tmp_path / "results", max_workers=2)

    assert [item["status"] for item in results] == ["success", "success"]
    assert repeat[0]["status"] == "duplicate"
    assert repeat[0]["result_dir"] == results[0]["result_dir"]
    assert len(list((tmp_path / "results").glob("*/output.json"))) == 2
    for result, expected in zip(results, ["First document", "Second document"]):
        folder = Path(result["result_dir"])
        assert folder.name.startswith(result["filename"].removesuffix(".pdf") + "_")
        assert expected in (folder / "output.txt").read_text(encoding="utf-8")
        stored = json.loads((folder / "output.json").read_text(encoding="utf-8"))
        assert stored["filename"] == result["filename"]
        assert expected in stored["text"]
        assert len(stored["sha256"]) == 64


def test_renamed_copy_and_same_batch_duplicate_are_skipped(tmp_path):
    first = tmp_path / "first.pdf"
    renamed = tmp_path / "renamed.pdf"
    data = make_pdf("Same bytes")
    first.write_bytes(data)
    renamed.write_bytes(data)

    results = parse_pdf_files([first, renamed], tmp_path / "results", max_workers=2)

    assert [item["status"] for item in results] == ["success", "duplicate"]
    assert len(list((tmp_path / "results").glob("*/output.json"))) == 1


def test_bad_pdf_does_not_stop_other_files(tmp_path):
    bad = tmp_path / "bad.pdf"
    good = tmp_path / "good.pdf"
    bad.write_bytes(b"not a pdf")
    good.write_bytes(make_pdf("Good document"))

    results = parse_pdf_files([bad, good], tmp_path / "results", max_workers=2)

    assert [item["status"] for item in results] == ["error", "success"]
    assert "Good document" in results[1]["text"]
    error_file = Path(results[0]["result_dir"]) / "output.json"
    assert json.loads(error_file.read_text(encoding="utf-8"))["error"]


def test_upload_api_needs_no_filesystem():
    result = parse_pdf_bytes(make_pdf("Uploaded document"), "upload.pdf")
    assert result == {"filename": "upload.pdf", "status": "success", "text": "[Page 1]\nUploaded document"}
