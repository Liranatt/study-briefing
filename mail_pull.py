"""Reads the BGU mailbox (Google Workspace, imap.gmail.com) read-only and keeps the mail that matters for studies.

    python -X utf8 mail_pull.py            the last 14 days -> catalog\\mail\\<date>.json
    python -X utf8 mail_pull.py --days 30
    python -X utf8 mail_pull.py --test     log in, count the messages and how many would be kept; write nothing

Login: ~/study/secrets/mail.env (MAIL_USER = the full address, MAIL_PASS = a Google app password, optional
MAIL_HOST, default imap.gmail.com); `setup.sh mail` writes it. The mailbox is opened read-only and messages are
fetched with BODY.PEEK, so nothing is marked as read, moved or deleted.

Kept: mail from an address at bgu.ac.il (and its subdomains: post, moodle, ...), mail that mentions Moodle in the
sender or subject, and mail from a sender listed in catalog\\mail\\staff.txt (one address per line, optional).
For each: subject, from, date, message id and the first 1500 characters of the text. No attachments.
"""
import argparse
import datetime as dt
import email
import email.header
import email.policy
import email.utils
import hashlib
import html
import imaplib
import json
import os
import pathlib
import re
import sys

import study_secrets

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "catalog" / "mail"
TEXT_LIMIT = 5000  # a whole forum post with its Moodle footer (the course id is in the footer link)
MAX_FETCH_BYTES = 3 * 2**20  # bigger messages (attachments): headers only


HEBREW_ALIASES = re.compile(r"(?i)iso-8859-8-[ie]")  # Outlook's Hebrew (logical/explicit order); Python knows only iso-8859-8


def decode(value):
    try:
        return str(email.header.make_header(email.header.decode_header(HEBREW_ALIASES.sub("iso-8859-8", value or ""))))
    except Exception:
        return value or ""


def charset_of(part):
    cs = (part.get_content_charset() or "utf-8").lower()
    return "iso-8859-8" if HEBREW_ALIASES.fullmatch(cs) else cs


def raw_header(raw, name):
    """One header straight from the bytes. Some BGU mail has raw 8-bit Hebrew in Subject (not =?..?= encoded), which
    the email parser turns into '�'; such bytes are UTF-8 or Windows-1255."""
    hit = re.search(rb"(?im)^" + name.encode() + rb":[ \t]*(.*(?:\r?\n[ \t].*)*)", raw)
    if not hit:
        return ""
    value = re.sub(rb"\r?\n[ \t]+", b" ", hit.group(1)).strip()
    for charset in ("ascii", "utf-8", "cp1255"):
        try:
            return decode(value.decode(charset))
        except UnicodeDecodeError:
            continue
    return decode(value.decode("utf-8", "replace"))


def plain(msg):
    """The message's text: text/plain, else text/html without tags. Attachments are skipped."""
    best = None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                if HEBREW_ALIASES.fullmatch(part.get_content_charset() or ""):
                    raise LookupError("decoded below")
                body = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                try:
                    body = payload.decode(charset_of(part))
                except (LookupError, UnicodeDecodeError):
                    body = payload.decode("utf-8", "replace")
            if part.get_content_type() == "text/html":
                body = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", body)
                body = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", body)))
            if best is None or part.get_content_type() == "text/plain":
                best = body
            if part.get_content_type() == "text/plain":
                break
    text = re.sub(r"[ \t\xa0]+", " ", best or "")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def wanted(sender, subject, staff):
    addr = email.utils.parseaddr(sender)[1].lower()
    domain = addr.rpartition("@")[2]
    return (domain == "bgu.ac.il" or domain.endswith(".bgu.ac.il") or addr in staff
            or "moodle" in addr or "moodle" in (subject or "").lower() or "מודל" in (subject or ""))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    cfg = study_secrets.load("mail.env")
    if not cfg.get("MAIL_USER") or not cfg.get("MAIL_PASS"):
        print(f"no mail login in {study_secrets.path('mail.env')}: run setup.sh mail")
        return 2
    staff_file = OUT / "staff.txt"
    staff = {l.strip().lower() for l in staff_file.read_text(encoding="utf-8").splitlines() if "@" in l} if staff_file.exists() else set()
    box = imaplib.IMAP4_SSL(cfg.get("MAIL_HOST") or "imap.gmail.com", 993, timeout=60)
    try:
        box.login(cfg["MAIL_USER"].strip(), cfg["MAIL_PASS"].replace(" ", ""))  # Google shows app passwords in groups of 4
    except imaplib.IMAP4.error as exc:
        print(f"login failed: {exc}. Check the app password (setup.sh mail).")
        return 1
    try:
        typ, _ = box.select("INBOX", readonly=True)
        if typ != "OK":
            print("cannot open INBOX")
            return 1
        since = (dt.date.today() - dt.timedelta(days=args.days)).strftime("%d-%b-%Y")
        typ, data = box.uid("SEARCH", None, "SINCE", since)
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        kept = {}
        for uid in uids:
            typ, meta = box.uid("FETCH", uid, "(RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
            if typ != "OK" or not meta or not isinstance(meta[0], tuple):
                continue
            size = int((re.search(rb"RFC822\.SIZE (\d+)", meta[0][0]) or [0, b"0"])[1])
            head = email.message_from_bytes(meta[0][1], policy=email.policy.default)
            sender, subject = decode(head.get("From")), decode(head.get("Subject"))
            if "�" in sender + subject:
                sender, subject = raw_header(meta[0][1], "From") or sender, raw_header(meta[0][1], "Subject") or subject
            if not wanted(sender, subject, staff):
                continue
            text = ""
            if not args.test and size <= MAX_FETCH_BYTES:
                typ, full = box.uid("FETCH", uid, "(BODY.PEEK[])")
                if typ == "OK" and full and isinstance(full[0], tuple):
                    text = plain(email.message_from_bytes(full[0][1], policy=email.policy.default))[:TEXT_LIMIT]
            try:
                when = email.utils.parsedate_to_datetime(head.get("Date")).astimezone()
            except Exception:
                when = dt.datetime.now().astimezone()
            mid = (head.get("Message-ID") or "").strip() or hashlib.sha1((sender + subject + str(when)).encode()).hexdigest()
            kept.setdefault(when.date().isoformat(), []).append({
                "id": mid, "date": when.isoformat(timespec="minutes"), "from": sender, "subject": subject,
                "text": text, "size": size, "attachments_skipped": size > MAX_FETCH_BYTES})
        print(f"{len(uids)} messages in the last {args.days} days, {sum(map(len, kept.values()))} kept (BGU / Moodle / staff)")
        if args.test:
            print("test done. Nothing was written.")
            return 0
        OUT.mkdir(parents=True, exist_ok=True)
        for day, items in kept.items():
            path = OUT / f"{day}.json"
            old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            merged = {m["id"]: m for m in old}
            merged.update({m["id"]: m for m in items})
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(merged.values(), key=lambda m: m["date"]), ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        print(f"written to {OUT}")
        return 0
    finally:
        try:
            box.logout()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
