"""Build a local .eml test message with one or more PDF attachments."""

import argparse
from email.message import EmailMessage
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body-file", type=Path, default=Path("sample_body.txt"))
    parser.add_argument("--pdf", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, default=Path("test_mail.eml"))
    args = parser.parse_args()

    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "tester@example.com"
    message["Subject"] = "PDF parser local test"
    message.set_content(args.body_file.read_text(encoding="utf-8"))
    for path in args.pdf:
        message.add_attachment(
            path.read_bytes(), maintype="application", subtype="pdf", filename=path.name
        )
    args.output.write_bytes(message.as_bytes())
    print(args.output)


if __name__ == "__main__":
    main()
