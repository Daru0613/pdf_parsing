import base64
from email.message import EmailMessage

import pymupdf

from pdf_mail_parser import (
    extract_pdf_text,
    main,
    parse_eml,
    parse_gmail_message,
    prepare_scan_text,
)


def make_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Visible instruction")
    page.insert_text((72, 90), "White hidden instruction", color=(1, 1, 1))
    page.insert_text((72, 110), "<!-- hidden comment instruction -->")
    page.insert_text((72, 130), "Invisible render instruction", render_mode=3)
    page.insert_text((72, 150), "Transparent instruction", fill_opacity=0)
    page.add_text_annot((120, 120), "Annotation instruction")
    data = doc.tobytes()
    doc.close()
    return data


def test_eml_body_before_pdf_and_hidden_text():
    message = EmailMessage()
    message.set_content("Mail body")
    message.add_attachment(make_pdf(), maintype="application", subtype="pdf", filename="test.pdf")
    output = prepare_scan_text(parse_eml(message.as_bytes()))
    assert output.index("Mail body") < output.index("[PDF attachment: test.pdf]")
    for phrase in ("Visible instruction", "White hidden instruction", "hidden comment instruction", "Invisible render instruction", "Transparent instruction", "Annotation instruction"):
        assert phrase in output


def test_gmail_inline_pdf_and_html_comment():
    pdf = make_pdf()
    encode = lambda data: base64.urlsafe_b64encode(data).decode().rstrip("=")
    message = {"payload": {"parts": [
        {"mimeType": "text/html", "body": {"data": encode(b"<div>Mail text</div><!-- hidden email text -->")}},
        {"mimeType": "application/pdf", "filename": "a.pdf", "body": {"data": encode(pdf)}},
    ]}}
    output = prepare_scan_text(parse_gmail_message(message))
    assert "hidden email text" in output
    assert "White hidden instruction" in output


def test_gmail_external_attachment_fetch():
    pdf = make_pdf()
    message = {"payload": {"parts": [
        {"mimeType": "text/plain", "body": {"data": "SGVsbG8"}},
        {"mimeType": "application/pdf", "filename": "a.pdf", "body": {"attachmentId": "att1"}},
    ]}}
    output = prepare_scan_text(parse_gmail_message(message), lambda attachment_id: pdf if attachment_id == "att1" else b"")
    assert output.startswith("Hello")
    assert "Visible instruction" in output


def test_invalid_pdf_reports_error():
    message = EmailMessage()
    message.set_content("Body")
    message.add_attachment(b"not a pdf", maintype="application", subtype="pdf", filename="bad.pdf")
    assert "[PDF parse error:" in prepare_scan_text(parse_eml(message.as_bytes()))


def test_direct_extraction():
    assert "White hidden instruction" in extract_pdf_text(make_pdf())


def test_cli_eml_output(tmp_path):
    message = EmailMessage()
    message.set_content("CLI mail body")
    message.add_attachment(make_pdf(), maintype="application", subtype="pdf", filename="cli.pdf")
    eml = tmp_path / "mail.eml"
    output = tmp_path / "scan.txt"
    eml.write_bytes(message.as_bytes())
    assert main(["--eml", str(eml), "--output", str(output)]) == 0
    text = output.read_text(encoding="utf-8")
    assert text.index("CLI mail body") < text.index("White hidden instruction")


def test_cli_separate_eml_pdf_includes_all_three_hidden_methods(tmp_path):
    message = EmailMessage()
    message.set_content("Visible mail body")
    message.add_alternative(
        '<p>Visible HTML</p><!-- HTML comment injection -->'
        '<div style="display:none">CSS display none injection</div>',
        subtype="html",
    )
    eml = tmp_path / "mail.eml"
    pdf = tmp_path / "separate.pdf"
    output = tmp_path / "scan.txt"
    eml.write_bytes(message.as_bytes())
    pdf.write_bytes(make_pdf())

    assert main(["--eml", str(eml), "--pdf", str(pdf), "--output", str(output)]) == 0
    text = output.read_text(encoding="utf-8")
    assert "HTML comment injection" in text
    assert "CSS display none injection" in text
    assert "White hidden instruction" in text
    assert text.index("Visible mail body") < text.index("[PDF attachment: separate.pdf]")
