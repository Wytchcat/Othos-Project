"""Read-only access to one Amazon S3 bucket, to copy PDFs into the library ("From AWS" in the Add documents window).

Outbound only: this PC calls AWS; nothing calls this PC and no port is opened. It can only list and read,
because the key you create for it is allowed to do only that (see IAM_HELP). Written with the standard
library and httpx so no AWS package has to be installed.

Settings come from .env (never printed, never logged):
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY      the key you made for Othos (IAM_HELP)
    AWS_REGION                                      like us-east-1  (AWS_DEFAULT_REGION also works)
    OTHOS_S3_BUCKET                                 the bucket holding your PDFs
    OTHOS_S3_PREFIX                                 optional: only this folder inside the bucket
    AWS_SESSION_TOKEN                               optional: only for temporary keys
"""
import hashlib
import hmac
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

HERE = Path(__file__).resolve().parent
MAX_PDF_BYTES = 200 * 1024 * 1024
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
_BUCKET_RX = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")
_REGION_RX = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")

IAM_HELP = """\
Give Othos its own AWS key that can only READ one bucket. Then, even if the key leaked, nobody could change or delete anything.

1. Sign in to the AWS console. Open IAM, then Users, then Create user.
2. Name it othos-library-reader. Leave "Provide user access to the AWS Management Console" OFF (it needs no password).
3. Choose "Attach policies directly", then "Create policy", then the JSON tab, and paste the policy below
   (change YOUR-BUCKET to your bucket's name). Name it othos-library-read-only and create it, then attach it to the user.
4. Open the user, Security credentials, Create access key. Choose "Application running outside AWS".
   Copy the Access key ID and the Secret access key. The secret is shown only once.
5. Add these lines to the file named .env in the Othos-agent folder, then save it:
       AWS_ACCESS_KEY_ID=...
       AWS_SECRET_ACCESS_KEY=...
       AWS_REGION=us-east-1
       OTHOS_S3_BUCKET=YOUR-BUCKET
   Never paste the secret into a chat, an email or a screenshot. To stop access later, deactivate the key in IAM.
"""


def iam_policy(bucket: str = "YOUR-BUCKET") -> str:
    return ('{\n  "Version": "2012-10-17",\n  "Statement": [\n'
            '    {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::%s"},\n'
            '    {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::%s/*"}\n  ]\n}' % (bucket, bucket))


class S3Error(RuntimeError):
    pass


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def load_env() -> None:
    """Read .env into the environment of this process (values are never shown)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env", override=False)
    except ImportError:
        pass


def settings() -> dict:
    return {"access": _env("AWS_ACCESS_KEY_ID"), "secret": _env("AWS_SECRET_ACCESS_KEY"), "token": _env("AWS_SESSION_TOKEN"),
            "region": _env("AWS_REGION") or _env("AWS_DEFAULT_REGION"), "bucket": _env("OTHOS_S3_BUCKET"),
            "prefix": _env("OTHOS_S3_PREFIX").lstrip("/")}


def missing() -> list[str]:
    """Names of the settings that are not filled in (empty = ready)."""
    s = settings()
    names = {"access": "AWS_ACCESS_KEY_ID", "secret": "AWS_SECRET_ACCESS_KEY", "region": "AWS_REGION", "bucket": "OTHOS_S3_BUCKET"}
    return [n for k, n in names.items() if not s[k]]


# ---------------------------------------------------------------- signing (AWS Signature Version 4)
def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _q(s: str) -> str:
    return quote(s, safe="-_.~")


def sign(method: str, host: str, path: str, query: dict, headers: dict, payload_hash: str, access: str, secret: str,
         region: str, service: str, amzdate: str) -> str:
    """The Authorization header value for one request. `headers` are the ones to sign (lower-case names, incl. host and x-amz-date)."""
    date = amzdate[:8]
    cq = "&".join(f"{_q(k)}={_q(str(v))}" for k, v in sorted(query.items()))
    names = sorted(headers)
    ch = "".join(f"{n}:{' '.join(str(headers[n]).split())}\n" for n in names)
    signed = ";".join(names)
    canonical = "\n".join([method, path, cq, ch, signed, payload_hash])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret).encode(), date), region), service), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed}, Signature={sig}"


def _request(key: str = "", query: dict | None = None, stream: bool = False, client: httpx.Client | None = None):
    s = settings()
    if missing():
        raise S3Error("These settings are missing in .env: " + ", ".join(missing()))
    if not _BUCKET_RX.match(s["bucket"]) or not _REGION_RX.match(s["region"]):
        raise S3Error("The bucket name or the region in .env doesn't look right (region example: us-east-1).")
    dotted = "." in s["bucket"]               # a dotted bucket name can't use the https certificate: path style
    host = f"s3.{s['region']}.amazonaws.com" if dotted else f"{s['bucket']}.s3.{s['region']}.amazonaws.com"
    path = "/" + (s["bucket"] + "/" if dotted else "") + quote(key, safe="/-_.~")
    amzdate = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = {"host": host, "x-amz-content-sha256": EMPTY_SHA, "x-amz-date": amzdate}
    if s["token"]:
        headers["x-amz-security-token"] = s["token"]
    auth = sign("GET", host, path, query or {}, headers, EMPTY_SHA, s["access"], s["secret"], s["region"], "s3", amzdate)
    url = f"https://{host}{path}" + ("?" + "&".join(f"{_q(k)}={_q(str(v))}" for k, v in sorted((query or {}).items())) if query else "")
    send = {k: v for k, v in headers.items() if k != "host"} | {"Authorization": auth}
    return url, send


def _fail(r: httpx.Response) -> S3Error:
    code = msg = ""
    try:
        root = ET.fromstring(r.text)
        code = (root.findtext("Code") or "").strip()
        msg = (root.findtext("Message") or "").strip()
    except ET.ParseError:
        pass
    hint = {"AccessDenied": " The key may not be allowed to read this bucket (check the policy).",
            "InvalidAccessKeyId": " The access key ID isn't one AWS knows.",
            "SignatureDoesNotMatch": " The secret key doesn't match the access key ID.",
            "NoSuchBucket": " That bucket doesn't exist (check OTHOS_S3_BUCKET).",
            "PermanentRedirect": " The bucket is in another region (check AWS_REGION).",
            "AuthorizationHeaderMalformed": " The region in .env doesn't match the bucket's region."}.get(code, "")
    return S3Error(f"AWS said no ({r.status_code} {code or 'error'}): {msg[:160]}{hint}".strip())


# ---------------------------------------------------------------- list and download
def list_pdfs(limit: int = 1000, client: httpx.Client | None = None) -> list[dict]:
    """[{key, size, modified}] for PDFs under OTHOS_S3_PREFIX, newest first."""
    out, token = [], ""
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        while len(out) < limit:
            q = {"list-type": "2", "max-keys": "1000"}
            if settings()["prefix"]:
                q["prefix"] = settings()["prefix"]
            if token:
                q["continuation-token"] = token
            url, headers = _request("", q)
            r = client.get(url, headers=headers)
            if r.status_code != 200:
                raise _fail(r)
            root = ET.fromstring(re.sub(r'\sxmlns="[^"]+"', "", r.text, count=1))
            for c in root.findall("Contents"):
                key = c.findtext("Key") or ""
                if key.lower().endswith(".pdf"):
                    out.append({"key": key, "size": int(c.findtext("Size") or 0), "modified": (c.findtext("LastModified") or "")[:10]})
            token = root.findtext("NextContinuationToken") or ""
            if root.findtext("IsTruncated") != "true" or not token:
                break
    except httpx.HTTPError as e:
        raise S3Error(f"Couldn't reach AWS: {type(e).__name__}") from None
    finally:
        if own:
            client.close()
    out.sort(key=lambda d: d["modified"], reverse=True)
    return out[:limit]


def download(key: str, dest: Path, client: httpx.Client | None = None) -> Path:
    """Copy one PDF to dest (a file path). Refuses anything over MAX_PDF_BYTES."""
    if not key.lower().endswith(".pdf"):
        raise S3Error("only PDF files are copied")
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30, read=120))
    try:
        url, headers = _request(key)
        with client.stream("GET", url, headers=headers) as r:
            if r.status_code != 200:
                r.read()
                raise _fail(r)
            total = 0
            with open(dest, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    total += len(chunk)
                    if total > MAX_PDF_BYTES:
                        fh.close()
                        dest.unlink(missing_ok=True)
                        raise S3Error(f"{key} is bigger than {MAX_PDF_BYTES // 1048576} MB, so it was skipped.")
                    fh.write(chunk)
    except httpx.HTTPError as e:
        dest.unlink(missing_ok=True)
        raise S3Error(f"Couldn't reach AWS: {type(e).__name__}") from None
    finally:
        if own:
            client.close()
    return dest
