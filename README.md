# 메일 PDF 파서

메일 본문 뒤에 PDF 첨부파일의 텍스트를 붙여 정적 검사기 또는 AI 검사기에 전달할 입력을 만듭니다. PDF의 흰색 글씨, 투명/숨김 렌더링 글씨, 페이지 바깥 글씨, 주석과 일부 메타데이터를 추출합니다. PDF 안에 문자 그대로 들어 있는 `<!-- ... -->`도 텍스트로 유지합니다. HTML 메일의 주석과 CSS로 숨겨진 본문 텍스트도 유지합니다.

## 설치

Python 3.10 이상에서:

```powershell
python -m pip install -r requirements.txt
```

## PDF만 일괄 처리 (Gmail 연결 없음)

PDF 파일을 `pdfs` 폴더에 모아두고 다음 명령을 실행합니다. 여러 PDF는 최대 4개 프로세스에서 병렬로 파싱합니다.

```powershell
New-Item -ItemType Directory -Force .\pdfs
python pdf_batch.py
```

다른 폴더 또는 특정 파일을 지정할 수도 있습니다.

```powershell
python pdf_batch.py --input-dir "C:\PDF 파일" --output-dir .\results --workers 4
python pdf_batch.py .\first.pdf .\second.pdf --output-dir .\results
```

처음 처리하는 PDF는 `results\PDF파일명_YYYY-MM-DD_HH-MM-SS\` 폴더에 `output.txt`와 `output.json`으로 저장됩니다. `results\index.sqlite3`에 파일 내용의 SHA-256 해시를 기록합니다. 다시 실행하면 이미 성공한 PDF는 `duplicate`로 표시하고 건너뜁니다. 파일명을 바꾼 복사본도 같은 내용이면 건너뜁니다. 오류가 난 PDF는 다음 실행에서 다시 시도합니다. 같은 초에 서로 다른 파일명이 겹치면 폴더 뒤에 번호를 붙입니다. JSON에는 `filename`, `status`, `text`, `sha256`이 들어가며 실패 시 `error`도 들어갑니다. 한 파일이 실패해도 다른 파일의 결과는 저장됩니다. 실패가 있으면 명령은 종료 코드 1을 반환합니다.

웹사이트의 업로드 처리 코드에서는 디스크에 저장하기 전에도 바이트를 직접 파싱할 수 있습니다.

```python
from pdf_batch import parse_pdf_bytes, parse_pdf_files

result = parse_pdf_bytes(uploaded_pdf_bytes, "uploaded.pdf")  # JSON으로 직렬화 가능한 dict
results = parse_pdf_files(["first.pdf", "second.pdf"], output_dir="results", max_workers=4)
```

`parse_pdf_files`는 입력 순서대로 결과를 반환합니다. 파일별 결과에는 `source_path`와 `result_dir`도 포함됩니다. 여러 사용자 요청이 동시에 같은 PDF를 제출해도 해시 예약을 통해 중복 파싱을 피합니다.

## 로컬 시험

1. `sample_body.txt`를 수정합니다.
2. 검사할 PDF를 준비하고 메일 파일을 만듭니다.

```powershell
python make_test_mail.py --body-file sample_body.txt --pdf .\example.pdf --output test_mail.eml
python pdf_mail_parser.py --eml test_mail.eml --output scan_input.txt
```

`--pdf`를 여러 번 지정하면 여러 첨부파일을 시험할 수 있습니다. `scan_input.txt`에는 원래 메일 본문이 먼저 나오고, PDF별 파일명과 페이지별 추출 텍스트가 이어집니다. `--output`을 생략하면 표준 출력으로 보냅니다.

## Gmail 연동

이미 저장한 Gmail `users.messages.get(format=full)` JSON은 다음처럼 처리합니다. JSON의 PDF 첨부가 `body.data`가 아닌 `attachmentId`로만 제공되면 OAuth 토큰도 필요합니다.

```powershell
python pdf_mail_parser.py --gmail-json message.json --output scan_input.txt
```

Gmail API에서 직접 메일과 첨부파일을 읽으려면 `gmail.readonly` 범위가 있는 **유효한 OAuth access token**을 환경 변수에 넣습니다. 토큰 발급과 갱신은 현재 Gmail 연동 코드가 담당합니다.

```powershell
$env:GMAIL_ACCESS_TOKEN = "<access token>"
python pdf_mail_parser.py --gmail-message-id "<message id>" --output scan_input.txt
```

기존 검사 코드에서는 `parse_gmail_message(message)`로 Gmail 응답을 변환하고, `prepare_scan_text(mail, fetch_attachment)`의 반환 문자열을 **정적 검사 전에** 전달하면 됩니다. `fetch_attachment`는 attachment ID를 받아 PDF 바이트를 반환하는 함수입니다. CLI는 동일한 작업을 Gmail API로 수행합니다.

PDF당 25 MiB, 200페이지, 추출 결과 200만 글자 제한이 있습니다. 암호화되거나 손상된 PDF와 첨부파일 누락은 출력에 `[PDF parse error: ...]`로 남깁니다. 이미지로만 된 스캔 PDF는 OCR이 없으므로 텍스트가 추출되지 않습니다. 이 경우 `[No extractable PDF text]`가 출력됩니다.

## EML 본문과 PDF가 별도 파일일 때

두 파일이 같은 폴더에 있어도 자동으로 연결되지는 않습니다. `--eml`에 메일 파일을, `--pdf`에 PDF 파일 경로를 각각 지정합니다. `--pdf`는 여러 번 사용할 수 있습니다.

```powershell
python pdf_mail_parser.py --eml .\examples\sample_mail.eml --pdf .\examples\sample_report.pdf --output .\examples\scan_input.txt
Get-Content -Encoding UTF8 .\examples\scan_input.txt
```

`--output`을 생략하면 추출 텍스트가 콘솔에 출력됩니다. 실제 파일 경로에 공백이 있으면 따옴표로 감싸세요.

```powershell
python pdf_mail_parser.py --eml "C:\mail samples\message.eml" --pdf "C:\mail samples\report.pdf" --output scan_input.txt
```

샘플 EML과 PDF는 `python make_separate_samples.py`로 다시 만들 수 있습니다. EML의 HTML 주석과 `display:none` 요소, PDF의 흰색 글씨에 인젝션 문구가 들어 있습니다. 파서는 이 문구들을 추출 텍스트에 포함합니다. 검사기에 전달할 때에는 이 텍스트를 **검사 대상 데이터**로 취급하세요.
