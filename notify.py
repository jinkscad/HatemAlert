#!/usr/bin/env python3
"""
Fetches internships from RSS + Greenhouse + Lever, filters CS interns/co-ops
in Canada (onsite, hybrid, or remote if Canada is indicated), emails new ones,
persists seen IDs under state/seen.json.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state" / "seen.json"
CONFIG_PATH = ROOT / "feeds.yaml"

INTERN_RE = re.compile(
    r"\b(intern(ship)?|co-?op|placement|student\s+(role|job)|"
    r"undergraduate\s+intern)\b",
    re.IGNORECASE,
)
CS_RE = re.compile(
    r"\b(software|swe|developer|engineer|backend|frontend|full[\s-]?stack|"
    r"computer\s+science|cs\b|machine\s+learning|ml\b|data\s+engineer|"
    r"platform|infrastructure|devops|security\s+engineer|android|ios)\b",
    re.IGNORECASE,
)

# Cities, provinces, or explicit "Canada" / Canadian territory names.
CANADA_HINT = re.compile(
    r"(\bCanada\b|\bCanadian\b|"
    r"(?:eligible|authorized|legally\s+permitted)\s+to\s+work\s+in\s+Canada\b|"
    r"(?:based|located|residing)\s+in\s+Canada\b|"
    r"\bOntario\b|\bQuébec\b|\bQuebec\b|\bBritish\s+Columbia\b|\bAlberta\b|\bManitoba\b|"
    r"\bSaskatchewan\b|\bNova\s+Scotia\b|\bNew\s+Brunswick\b|\bNewfoundland\b|\bLabrador\b|"
    r"\bPrince\s+Edward\b|\bP\.?E\.?I\.?\b|\bNunavut\b|\bYukon\b|"
    r"\bNorthwest\s+Territories\b|"
    r"\bToronto\b|\bMississauga\b|\bVancouver\b|\bMontreal\b|\bMontréal\b|\bOttawa\b|"
    r"\bCalgary\b|\bEdmonton\b|\bWinnipeg\b|\bWaterloo\b|\bKitchener\b|\bHamilton\b|"
    r"\bHalifax\b|\bGTA\b|\bBurnaby\b|\bSurrey\b|\bKanata\b|"
    r"\bLondon\s*,\s*ON\b|"
    r"(?:,\s*)(?:ON|QC|BC|AB|MB|SK|NS|NB|NL)(?:\s*$|\s|,))",
    re.IGNORECASE,
)
# Strong US-only signals: skip if none of CANADA_HINT matched (checked separately).
US_ONLY_RE = re.compile(
    r"\b(US\s+only|U\.S\.?\s+only|USA\s+only|United\s+States\s+only|"
    r"Must\s+be\s+(?:legally\s+)?authorized\s+to\s+work\s+in\s+the\s+US)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Job:
    job_id: str
    title: str
    url: str
    source: str
    location_text: str


def stable_id(url: str, title: str) -> str:
    h = hashlib.sha256(f"{url.strip().lower()}|{title.strip().lower()}".encode()).hexdigest()[:32]
    return h


def load_seen() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return set(data.get("ids", []))


def save_seen(ids: set[str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"ids": sorted(ids)}, indent=2) + "\n", encoding="utf-8")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {"rss": [], "greenhouse_boards": [], "lever": []}
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}


def strip_html(raw: str) -> str:
    return re.sub(r"<[^>]+>", " ", raw or "")


def normalize_location_blob(*parts: str) -> str:
    return re.sub(r"\s+", " ", strip_html(" ".join(p for p in parts if p))).strip()


def matches_intern_cs(title: str) -> bool:
    t = title or ""
    if not INTERN_RE.search(t):
        return False
    return bool(CS_RE.search(t))


def is_canada_location(location_blob: str) -> bool:
    t = normalize_location_blob(location_blob)
    if not t:
        return False
    if US_ONLY_RE.search(t) and not CANADA_HINT.search(t):
        return False
    return bool(CANADA_HINT.search(t))


def fetch_rss(url: str) -> list[Job]:
    out: list[Job] = []
    parsed = feedparser.parse(
        url,
        agent="HatemAlert/1.0 (+https://github.com/)",
    )
    for e in parsed.entries:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or e.get("id") or "").strip()
        summary = (e.get("summary") or e.get("description") or "").strip()
        if not title or not link:
            continue
        jid = stable_id(link, title)
        host = urlparse(url).netloc or "rss"
        loc = normalize_location_blob(title, summary)
        out.append(Job(job_id=jid, title=title, url=link, source=f"rss:{host}", location_text=loc))
    return out


def fetch_greenhouse(board_token: str) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
    r = requests.get(url, timeout=30, headers={"User-Agent": "HatemAlert/1.0"})
    r.raise_for_status()
    data = r.json()
    out: list[Job] = []
    for j in data.get("jobs") or []:
        title = (j.get("title") or "").strip()
        link = (j.get("absolute_url") or "").strip()
        if not title or not link:
            continue
        jid = stable_id(link, title)
        loc_obj = j.get("location")
        loc_name = ""
        if isinstance(loc_obj, dict):
            loc_name = (loc_obj.get("name") or "").strip()
        elif isinstance(loc_obj, str):
            loc_name = loc_obj.strip()
        loc = normalize_location_blob(loc_name, title)
        out.append(
            Job(job_id=jid, title=title, url=link, source=f"greenhouse:{board_token}", location_text=loc)
        )
    return out


def fetch_lever(company: str) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{company}"
    r = requests.get(url, timeout=30, headers={"User-Agent": "HatemAlert/1.0"})
    r.raise_for_status()
    postings = r.json()
    if not isinstance(postings, list):
        postings = postings.get("data") or []
    out: list[Job] = []
    for p in postings:
        title = (p.get("text") or "").strip()
        host = (p.get("hostedUrl") or "").strip()
        if not title or not host:
            continue
        jid = stable_id(host, title)
        cats = p.get("categories") or {}
        loc_part = ""
        if isinstance(cats, dict):
            raw_loc = (cats.get("location") or "") or ""
            wt = (cats.get("workplaceType") or "") or ""
            commitment = (cats.get("commitment") or "") or ""
            loc_part = normalize_location_blob(str(raw_loc), str(wt), str(commitment))
        desc = (p.get("description") or "")[:4000]
        loc = normalize_location_blob(loc_part, title, strip_html(desc))
        out.append(Job(job_id=jid, title=title, url=host, source=f"lever:{company}", location_text=loc))
    return out


def collect_jobs(cfg: dict[str, Any]) -> list[Job]:
    jobs: list[Job] = []
    for item in cfg.get("rss") or []:
        u = item if isinstance(item, str) else item.get("url")
        if u:
            jobs.extend(fetch_rss(u))
    for b in cfg.get("greenhouse_boards") or []:
        if b:
            try:
                jobs.extend(fetch_greenhouse(str(b).strip()))
            except Exception:
                continue
    for c in cfg.get("lever") or []:
        if c:
            try:
                jobs.extend(fetch_lever(str(c).strip()))
            except Exception:
                continue
    return jobs


def parse_recipients(raw: str) -> list[str]:
    """Comma- or semicolon-separated To addresses."""
    out: list[str] = []
    for part in re.split(r"[,;]", raw):
        a = part.strip()
        if a:
            out.append(a)
    return out


def send_email(to_addrs: list[str], from_addr: str, subject: str, html: str, smtp_user: str, smtp_pass: str) -> None:
    if not to_addrs:
        raise ValueError("send_email: no recipients")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText(html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(smtp_user, smtp_pass)
            s.sendmail(from_addr, to_addrs, msg.as_string())
    except smtplib.SMTPAuthenticationError as e:
        raise SystemExit(
            "SMTP login failed. Use a Gmail App Password for SMTP_PASSWORD (not your normal password), "
            "and set SMTP_USER + ALERT_EMAIL_FROM to that same Gmail address. "
            f"Underlying error: {e!s}"
        ) from e


def main() -> None:
    to_addrs = parse_recipients(os.environ.get("ALERT_EMAIL_TO", ""))
    from_addr = os.environ.get("ALERT_EMAIL_FROM", "").strip()
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASSWORD", "").strip()

    missing = []
    if not to_addrs:
        missing.append("ALERT_EMAIL_TO")
    if not from_addr:
        missing.append("ALERT_EMAIL_FROM")
    if not smtp_user:
        missing.append("SMTP_USER")
    if not smtp_pass:
        missing.append("SMTP_PASSWORD")
    if missing:
        raise SystemExit(
            "Missing environment variables: "
            + ", ".join(missing)
            + ". Add them under Settings → Secrets and variables → Actions "
            "(repository secrets; names must match exactly)."
        )

    cfg = load_config()
    all_jobs = collect_jobs(cfg)
    filtered = [
        j for j in all_jobs if matches_intern_cs(j.title) and is_canada_location(j.location_text)
    ]

    seen = load_seen()
    # Empty state = first run: record current listings only (no email blast).
    bootstrap = len(seen) == 0
    new_jobs = [j for j in filtered if j.job_id not in seen]

    if bootstrap:
        for j in filtered:
            seen.add(j.job_id)
        save_seen(seen)
        print(f"Bootstrap: tracked {len(filtered)} Canada intern+CS listings, no email sent.")
        return

    if not new_jobs:
        print("No new Canada intern+CS listings.")
        return

    for j in new_jobs:
        seen.add(j.job_id)
    save_seen(seen)

    lines = []
    for j in new_jobs:
        loc_snip = (j.location_text or "")[:220] + ("…" if len(j.location_text or "") > 220 else "")
        lines.append(
            "<li>"
            f'<a href="{html.escape(j.url)}">{html.escape(j.title)}</a>'
            f"<br><small>{html.escape(loc_snip)}</small> "
            f"<small>({html.escape(j.source)})</small>"
            "</li>"
        )
    body_html = f"""\
<html><body>
<p>New <strong>Canada</strong> intern/co-op listings (CS-related):</p>
<ul>{"".join(lines)}</ul>
</body></html>"""
    send_email(
        to_addrs,
        from_addr,
        subject=f"[HatemAlert] {len(new_jobs)} new Canada intern listing(s)",
        html=body_html,
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
    )
    print(f"Emailed {len(new_jobs)} new listing(s) to {', '.join(to_addrs)}.")


if __name__ == "__main__":
    main()
