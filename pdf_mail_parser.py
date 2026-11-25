"""Turn an email and its PDF attachments into one scanner input."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sys
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pymupdf

MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PAGES = 200
MAX_OUTPUT_CHARS = 2_000_000


class HTMLText(HTMLParser):
    """Keep comments and hidden HTML text in the scan input."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"[HTML comment] {data} [/HTML comment]")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def text(self) -> str:
        return "".join(self.parts).strip()


def html_to_scan_text(value: str) -> str:
    parser = HTMLText()
    parser.feed(value)
    return parser.text()


@dataclass
class Attachment:
    filename: str
    mime_type: str
    data: bytes | None = None
    attachment_id: str | None = None


@dataclass
class ParsedMail:
    body: str
    attachments: list[Attachment] = field(default_factory=list)


def _b64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _gmail_parts(part: dict, result: ParsedMail) -> None:
    children = part.get("parts") or []
    if children:
        for child in children:
            _gmail_parts(child, result)
        return
    mime = (part.get("mimeType") or "").lower()
    filename = part.get("filename") or ""
    data = part.get("body", {}).get("data")
    attachment_id = part.get("body", {}).get("attachmentId")
    if filename or attachment_id:
        result.attachments.append(Attachment(filename, mime, _b64url(data) if data else None, attachment_id))
    elif data and mime in {"text/plain", "text/html"}:
        decoded = _b64url(data).decode("utf-8", errors="replace")
        text = html_to_scan_text(decoded) if mime == "text/html" else decoded
        # Include both alternatives: an attacker may hide content in either.
        if text.strip():
            result.body += ("\n" if result.body else "") + text


def parse_gmail_message(message: dict) -> ParsedMail:
    result = ParsedMail("")
    _gmail_parts(message.get("payload") or {}, result)
    return result


def parse_eml(data: bytes) -> ParsedMail:
    message = BytesParser(policy=policy.default).parsebytes(data)
    result = ParsedMail("")
    for part in message.walk():
        if part.is_multipart():
            continue
        mime = part.get_content_type().lower()
        filename = part.get_filename() or ""
        payload = part.get_payload(decode=True) or b""
        if filename or part.get_content_disposition() == "attachment":
            result.attachments.append(Attachment(filename, mime, payload))
        elif mime in {"text/plain", "text/html"}:
            decoded = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            text = html_to_scan_text(decoded) if mime == "text/html" else decoded
            if text.strip():
                result.body += ("\n" if result.body else "") + text
    return result


def is_pdf(attachment: Attachment) -> bool:
    indicated = attachment.mime_type == "application/pdf" or attachment.filename.lower().endswith(".pdf")
    # Include disguised PDFs too, but do not feed non-PDF data to the parser.
    return indicated or bool(attachment.data and attachment.data.lstrip().startswith(b"%PDF-"))


def _trace_text(span: dict) -> str:
    chars = []
    for char in span.get("chars", []):
        code = char[0]
        if isinstance(code, int) and code > 0:
            chars.append(chr(code))
    return "".join(chars).strip()


def extract_pdf_text(data: bytes) -> str:
    if len(data) > MAX_PDF_BYTES:
        raise ValueError(f"PDF exceeds {MAX_PDF_BYTES} byte limit")
    if not data.lstrip().startswith(b"%PDF-"):
        raise ValueError("attachment is not a PDF (missing PDF header)")
    chunks: list[str] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ValueError("encrypted PDF requires a password")
        if len(doc) > MAX_PAGES:
            raise ValueError(f"PDF exceeds {MAX_PAGES} page limit")
        metadata = doc.metadata or {}
        for key in ("title", "subject", "keywords", "author"):
            if metadata.get(key):
                chunks.append(f"[PDF metadata {key}] {metadata[key]}")
        for page_number, page in enumerate(doc, 1):
            chunks.append(f"[Page {page_number}]")
            # get_text includes text even when rendered white, tiny, or outside the page.
            # INFINITE avoids clipping text outside the normal crop rectangle.
            page_text = page.get_text("text", clip=pymupdf.INFINITE_RECT()).strip()
            if page_text:
                chunks.append(page_text)
            for span in page.get_texttrace():
                if span.get("type") == 3 or span.get("opacity", 1) == 0:
                    hidden = _trace_text(span)
                    if hidden and hidden not in page_text:
                        chunks.append(f"[Hidden PDF text] {hidden}")
            for annot in page.annots() or []:
                info = annot.info or {}
                for key in ("content", "title", "subject"):
                    if info.get(key):
                        chunks.append(f"[PDF annotation {key}] {info[key]}")
    output = "\n".join(chunks)
    if len(output) > MAX_OUTPUT_CHARS:
        raise ValueError(f"extracted text exceeds {MAX_OUTPUT_CHARS} character limit")
    return output


def prepare_scan_text(
    mail: ParsedMail,
    fetch_attachment: Callable[[str], bytes] | None = None,
) -> str:
    """Call this before static or AI inspection; returns body followed by PDF text."""
    pieces = [mail.body.strip()]
    for index, attachment in enumerate(mail.attachments, 1):
        if not is_pdf(attachment):
            continue
        label = attachment.filename or f"attachment-{index}.pdf"
        pieces.append(f"[PDF attachment: {label}]")
        try:
            data = attachment.data
            if data is None and attachment.attachment_id and fetch_attachment:
                data = fetch_attachment(attachment.attachment_id)
            if data is None:
                raise ValueError("attachment bytes unavailable; use Gmail API mode or provide inline data")
            pieces.append(extract_pdf_text(data) or "[No extractable PDF text]")
        except (ValueError, RuntimeError, pymupdf.FileDataError) as error:
            pieces.append(f"[PDF parse error: {error}]")
    return "\n\n".join(piece for piece in pieces if piece)


def _gmail_get(path: str, token: str) -> dict:
    request = Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/" + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise ValueError(f"Gmail API HTTP {error.code}: {error.reason}") from error
    except URLError as error:
        raise ValueError(f"Gmail API connection failed: {error.reason}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append PDF attachment text to email scan input")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--eml", type=Path, help="RFC 822 .eml file")
    source.add_argument("--gmail-json", type=Path, help="saved Gmail messages.get JSON response")
    source.add_argument("--gmail-message-id", help="message ID to fetch from Gmail API")
    parser.add_argument(
        "--pdf", type=Path, action="append", default=[],
        help="separate PDF file to append (repeat for multiple files)",
    )
    parser.add_argument("--token-env", default="GMAIL_ACCESS_TOKEN", help="environment variable holding Gmail OAuth access token")
    parser.add_argument("--output", type=Path, help="write combined text to this UTF-8 file (default: stdout)")
    args = parser.parse_args(argv)
    try:
        token = os.environ.get(args.token_env)
        if args.gmail_message_id:
            if not token:
                raise ValueError(f"{args.token_env} is required for Gmail API mode")
            message = _gmail_get(f"messages/{quote(args.gmail_message_id, safe='')}?format=full", token)
            mail = parse_gmail_message(message)
            message_id = message.get("id", args.gmail_message_id)
        elif args.gmail_json:
            message = json.loads(args.gmail_json.read_text(encoding="utf-8"))
            mail = parse_gmail_message(message)
            message_id = message.get("id", "")
        else:
            mail = parse_eml(args.eml.read_bytes())
            message_id = ""

        for path in args.pdf:
            mail.attachments.append(Attachment(path.name, "application/pdf", path.read_bytes()))

        def fetch(attachment_id: str) -> bytes:
            if not token or not message_id:
                raise ValueError("external Gmail attachment needs message ID and OAuth token")
            response = _gmail_get(
                f"messages/{quote(message_id, safe='')}/attachments/{quote(attachment_id, safe='')}", token
            )
            return _b64url(response["data"])

        output = prepare_scan_text(mail, fetch)
        if args.output:
            args.output.write_text(output + "\n", encoding="utf-8")
        else:
            print(output)
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
