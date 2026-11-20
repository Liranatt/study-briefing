"""Dated events in staff mail: an exam, a deadline, a lecture moved or cancelled, a meeting.

    python -X utf8 mail_events.py            print what calendar_feed.py would add from catalog\\mail
    python -X utf8 mail_events.py --all      also events already in the past

Used by calendar_feed.py (and the site). Rules, not a language model, so it stays free and runs every night:
  - which mail: course announcements (a Moodle forum copy from a "לוח הודעות" forum, or a course mail whose
    subject starts with the course name or its Moodle code) and personal mail to Liran from a bgu.ac.il address.
    Not: Moodle's own reminders (calendar_feed takes the deadlines from Moodle itself), mailing lists ([...]),
    university announcements.
  - which sentence: one with a date (26.5.2026, 3.6, 23/6, "May 26th", "June 16th", "September 1st",
    "26 במאי", tomorrow / מחר, today / היום, next Tuesday / ביום שלישי) and a word that makes it an event
    (exam, quiz, midterm, deadline, due, submit, lecture, class, cancelled, meeting, zoom, מבחן, בוחן, הגשה,
    הרצאה, שיעור, תרגול, מבוטל, פגישה ...).
  - the hour: in the same sentence ("at 9:10", "from 13:15 to 16:00", "end of day" / "midnight" = 23:59), else
    the first hour in the mail when the mail names only one date.
  - the room: "building 96 ... room 003" / "בניין 90 כיתה 323" anywhere in the mail.
A year is added when missing: the one that puts the date closest after the mail (up to 60 days before it).
"""
import argparse
import datetime as dt
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
CATALOG = HERE / "catalog"

EN_MONTHS = {m: i for i, ms in enumerate([("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
                                          ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
                                          ("september", "sep", "sept"), ("october", "oct"), ("november", "nov"),
                                          ("december", "dec")], 1) for m in ms}
HE_MONTHS = {"ינואר": 1, "פברואר": 2, "מרץ": 3, "מרס": 3, "אפריל": 4, "מאי": 5, "יוני": 6, "יולי": 7, "אוגוסט": 8,
             "ספטמבר": 9, "אוקטובר": 10, "נובמבר": 11, "דצמבר": 12}
EN_DAYS = {"sunday": 6, "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5}
HE_DAYS = {"ראשון": 6, "שני": 0, "שלישי": 1, "רביעי": 2, "חמישי": 3, "שישי": 4, "שבת": 5}

EN_MONTH_RE = "|".join(sorted(EN_MONTHS, key=len, reverse=True))
HE_MONTH_RE = "|".join(HE_MONTHS)
DATE_PATTERNS = [
    # 26.5.2026, 3.6, 23/6 (not 1.5 hours, not a version or a score)
    ("num", re.compile(r"(?<![\d.:/])(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?![\d/]|\.\d)"
                       r"(?!\s*(?:hours?|hrs?|h\b|שעות|שעה|points?|pts|נק|%|gb|mb|minutes|דקות))", re.I)),
    ("en_dm", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({EN_MONTH_RE})\b\.?(?:,?\s*(\d{{4}}))?", re.I)),
    ("en_md", re.compile(rf"\b({EN_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s*(\d{{4}}))?", re.I)),
    ("he", re.compile(rf"(?<!\d)(\d{{1,2}})\s+ב?({HE_MONTH_RE})(?:\s+(\d{{4}}))?")),
    ("tomorrow", re.compile(r"\btomorrow\b|מחר(?!תיים)", re.I)),
    ("today", re.compile(r"\btoday\b|\btonight\b|(?<![א-ת])היום(?![א-ת])|הערב(?![א-ת])", re.I)),
    ("day_after", re.compile(r"מחרתיים|day after tomorrow", re.I)),
    ("en_wd", re.compile(r"\b(?:(next|this)\s+)?(sunday|monday|tuesday|wednesday|thursday|friday|saturday)\b", re.I)),
    ("he_wd", re.compile(r"(?:ב)?יום\s+(ראשון|שני|שלישי|רביעי|חמישי|שישי)(?:\s+(הבא|הקרוב))?")),
]
EVENT = re.compile(r"exam|midterm|quiz|test\b|deadline|due\b|submi|lecture|class\b|lesson|recitation|tutorial|"
                   r"cancel|postpon|resched|meeting|zoom|guest|presentation|office hours|"
                   r"מבחן|בחינה|בוחן|הגש|להגיש|מועד|הרצאה|שיעור|תרגול|תרגיל|מבוטל|בוטל|ביטול|נדחה|דחייה|"
                   r"לא תתקיים|לא יתקיים|פגישה|נפגש|זום|הצג|שעות קבלה|השלמה", re.I)
# a date in these sentences is not something to attend: grades coming out, how far the material goes
NOT_EVENT = re.compile(r"grade|ציון|ציונים|material|cover|up to and including|חומר|עד וכולל", re.I)
CANCEL = re.compile(r"cancel|מבוטל|בוטל|ביטול|לא תתקיים|לא יתקיים|לא יהיה שיעור|לא תהיה הרצאה|לא יהיה תרגול", re.I)
STARTS = re.compile(r"start|begin|held|will be at|\bat\s+\d|מתחיל|נתחיל|יתחיל|תתחיל|יתקיים|תתקיים|בשעה", re.I)
OFFICE_HOURS = re.compile(r"office hours|שעות( ה)?קבלה", re.I)
KIND = [("office_hours", OFFICE_HOURS),  # "office hours before the quiz" is office hours, not the quiz
        ("exam", re.compile(r"exam|midterm|quiz|מבחן|בחינה|בוחן", re.I)),
        ("cancel", CANCEL),
        ("deadline", re.compile(r"deadline|due\b|submi|הגש|להגיש", re.I)),
        ("lecture", re.compile(r"lecture|class\b|lesson|recitation|tutorial|הרצאה|שיעור|תרגול|השלמה", re.I)),
        ("meeting", re.compile(r"meeting|פגישה|נפגש|office hours|שעות קבלה", re.I))]
KIND_HE = {"exam": "מבחן/בוחן", "cancel": "ביטול", "deadline": "הגשה", "lecture": "שיעור", "meeting": "פגישה",
           "office_hours": "שעות קבלה"}
TIME = re.compile(r"(?<![\d:.])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
RANGE = re.compile(r"(?:(?:from|between|מ-?|בין)\s*)?(?<![\d:.])([01]?\d|2[0-3]):([0-5]\d)\s*(?:to|until|and|עד|ל-?|-|–)\s*([01]?\d|2[0-3]):([0-5]\d)", re.I)
HOURS = re.compile(r"(?<![\d.:/])(1?\d|2[0-2])\s*[-–]\s*(1?\d|2[0-3])(?![\d.:/])")  # "ביום חמישי, 10-12"
END_OF_DAY = re.compile(r"end of (?:the )?day|midnight|eod|23:59|בסוף היום|עד חצות|חצות", re.I)
ROOM = re.compile(r"(?:building|בניין|בנין)\s*(\d{1,3})\b[^.\n]{0,40}?(?:room|class(?:room)?|חדר|כיתה)\s*([A-Za-z]?\d[\w-]{0,5})", re.I)
MOODLE_COURSE_ID = re.compile(r"moodle/(?:mod/forum/index|course/view)\.php\?id=(\d+)")
COURSE_PREFIX = re.compile(r"^\s*(?P<course>.+?)\s+סמ\s*\d+\s*:\s*(?P<rest>.*)$")
CODE_PREFIX = re.compile(r"^\s*(?P<code>\d{10,})\s*:\s*(?P<rest>.*)$")
FORUM_COPY = "זהו העתק של הודעה שפורסמה"


def read_json(path, default=None):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def digits(s):
    return re.sub(r"\D", "", str(s or ""))


def norm(s):
    return re.sub(r"[\s'\"׳״`.,:()\-–]", "", str(s or "")).replace("יא", "א")


def body_of(text):
    """What the sender wrote: a Moodle forum copy keeps only the post (between its dashed lines)."""
    text = (text or "").replace("\r\n", "\n")
    parts = re.split(r"\n?-{20,}\n?", text)
    body = parts[1] if is_forum_copy(text) and len(parts) >= 2 else text
    body = re.sub(r"\n\s*Links:\s*\n-+\n.*", "", body, flags=re.S)
    return re.split(r"\n?\s*_{10,}|\n\s*-----\s*Original Message|\n\s*From:\s.*\n\s*Sent:", body)[0].strip()


def sentences(text):
    """Sentences: lines joined inside a paragraph, split after . ! ? (a date's own dots stay)."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        para = re.sub(r"\s*\n\s*", " ", para).strip()
        out += [s.strip() for s in re.split(r"(?<=[!?])\s+|(?<=[^\d\s][.])\s+|(?<=\d\.)\s+(?=[A-Zא-ת])", para) if s.strip()]
    return out


def with_year(day, month, year, sent):
    if year:
        year = int(year)
        year += 2000 if year < 100 else 0
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None
    for y in (sent.year - 1, sent.year, sent.year + 1):
        try:
            d = dt.date(y, month, day)
        except ValueError:
            continue
        if sent - dt.timedelta(days=60) <= d <= sent + dt.timedelta(days=300):
            return d
    return None


def next_weekday(after, weekday):
    d = after + dt.timedelta(days=1)
    while d.weekday() != weekday:
        d += dt.timedelta(days=1)
    return d


def dates_in(sentence, sent):
    found = []
    for kind, rx in DATE_PATTERNS:
        for m in rx.finditer(sentence):
            d = None
            if kind == "num":
                day, month = int(m.group(1)), int(m.group(2))
                if 1 <= day <= 31 and 1 <= month <= 12:
                    d = with_year(day, month, m.group(3), sent)
            elif kind == "en_dm":
                d = with_year(int(m.group(1)), EN_MONTHS[m.group(2).lower()], m.group(3), sent)
            elif kind == "en_md":
                d = with_year(int(m.group(2)), EN_MONTHS[m.group(1).lower()], m.group(3), sent)
            elif kind == "he":
                d = with_year(int(m.group(1)), HE_MONTHS[m.group(2)], m.group(3), sent)
            elif kind == "tomorrow":
                d = sent + dt.timedelta(days=1)
            elif kind == "today":
                d = sent
            elif kind == "day_after":
                d = sent + dt.timedelta(days=2)
            elif kind == "en_wd":
                d = next_weekday(sent, EN_DAYS[m.group(2).lower()])
            elif kind == "he_wd":
                d = next_weekday(sent, HE_DAYS[m.group(1)])
            if d:
                found.append((m.start(), d, kind))
    # "tomorrow" next to an explicit date is the same day; keep explicit dates first
    explicit = [f for f in found if f[2] in ("num", "en_dm", "en_md", "he")]
    return sorted(explicit or found)


MIXED_RANGE = re.compile(r"(?<![\d:./])([01]?\d|2[0-3])(?::([0-5]\d))?\s*[-–]\s*([01]?\d|2[0-3]):([0-5]\d)")  # "8-10:30"


def time_in(sentence):
    r = RANGE.search(sentence)
    if r:
        return f"{int(r.group(1)):02d}:{r.group(2)}", f"{int(r.group(3)):02d}:{r.group(4)}"
    r = MIXED_RANGE.search(sentence)
    if r and int(r.group(1)) < int(r.group(3)):
        return f"{int(r.group(1)):02d}:{r.group(2) or '00'}", f"{int(r.group(3)):02d}:{r.group(4)}"
    t = TIME.search(sentence)
    if t:
        return f"{int(t.group(1)):02d}:{t.group(2)}", None
    h = HOURS.search(sentence)
    if h and 7 <= int(h.group(1)) < int(h.group(2)) <= 23:
        return f"{int(h.group(1)):02d}:00", f"{int(h.group(2)):02d}:00"
    if END_OF_DAY.search(sentence):
        return "23:59", None
    return None, None


def load_courses():
    out = []
    for c in read_json(CATALOG / "courses.json", []) or []:
        nums = {digits(c.get("number"))} | {digits(o.get("number")) for o in c.get("offerings", [])}
        out.append({"key": c["key"], "name": c.get("name_he") or c["key"], "nums": {n for n in nums if len(n) >= 6},
                    "moodle_ids": {str(o["moodle_id"]) for o in c.get("offerings", []) if o.get("moodle_id")}})
    return out


def course_of(subject, text, courses):
    """(course, subject without its course prefix) from 'X סמ 1: ...' or '232252320120262: ...'; the Moodle course
    id in the mail's forum link settles it when the name or number does not."""
    by_id = MOODLE_COURSE_ID.search(text or "")
    by_id = next((c for c in courses if by_id and by_id.group(1) in c["moodle_ids"]), None)
    m = COURSE_PREFIX.match(subject)
    if m:
        n = norm(m.group("course"))
        c = next((c for c in courses if norm(c["name"]) and (norm(c["name"]) in n or n in norm(c["name"]))), None)
        return c or by_id or {"key": None, "name": m.group("course").strip()}, m.group("rest")
    m = CODE_PREFIX.match(subject) or re.match(r"\s*(?P<code>\d{10,})\s*->", text or "")
    if m:
        code = m.group("code")
        c = next((c for c in courses if any(code.startswith(n) for n in c["nums"])), None)
        rest = m.group("rest") if "rest" in m.groupdict() and m.group("rest") else subject
        return c or by_id or {"key": None, "name": ""}, rest
    return by_id, subject


def address(m):
    sender = (m.get("from") or "").lower()
    return (re.search(r"<([^>]+)>", sender) or [None, sender])[1].strip()


def is_forum_copy(text):
    """Moodle's mail copy of a forum post: 'X סמ 1 -> פורומים -> ...' on top, 'זהו העתק...' at the bottom (which a
    shortened text may have lost)."""
    return FORUM_COPY in (text or "") or bool(re.search(r"->\s*פורומים\s*->", (text or "")[:300]))


def is_announcement(m):
    """A staff post in a course's announcements forum, as Moodle mails it ('לוח הודעות' may be split by a newline)."""
    text = m.get("text") or ""
    return is_forum_copy(text) and bool(re.search(r"לוח\s+הודעות", text[:400]))


def eligible(m, staff):
    """Course announcements, and mail to Liran from course staff (anyone who posted a course announcement, or is
    listed in catalog\\mail\\staff.txt) or replies from a BGU address. Not Moodle's reminders (calendar_feed takes
    deadlines from Moodle itself), mailing lists ([...]) or university announcements."""
    addr, subject, text = address(m), m.get("subject") or "", m.get("text") or ""
    if "[" in subject or not (re.search(r"@([\w-]+\.)*bgu\.ac\.il$", addr) or addr in staff):
        return False
    if is_forum_copy(text):
        return is_announcement(m)  # not students' posts in Q&A forums
    if re.match(r"(bguannouncements|noreply|no-reply|do-not-reply)@", addr):
        return False
    return bool(addr in staff or COURSE_PREFIX.match(subject) or CODE_PREFIX.match(subject)
                or re.match(r"\s*(re|fw|fwd|תשובה)\s*:", subject, re.I))


def events_from(m, courses):
    sent_at = dt.datetime.fromisoformat(m["date"]).replace(tzinfo=None)
    sent = sent_at.date()
    course, subject = course_of(m.get("subject") or "", m.get("text") or "", courses)
    body = body_of(m.get("text") or "")
    room_of = lambda text: (lambda r: f"בניין {r.group(1)}, חדר {r.group(2)}" if r else "")(ROOM.search(text or ""))  # noqa: E731
    sents = [subject] + sentences(body)
    dated = [(s, dates_in(s, sent)) for s in sents if not NOT_EVENT.search(s)]
    all_dates = {d for _, ds in dated for _, d, _ in ds}
    # an hour in another sentence counts only when that sentence says the event starts or is held then
    first_time = next((time_in(s) for s in sents if STARTS.search(s) and not re.search(r"break|הפסקה", s, re.I)
                       and time_in(s)[0] and time_in(s)[0] != "23:59"), (None, None))
    out, seen = [], set()
    for s, ds in dated:
        if not ds:
            continue
        if not (EVENT.search(s) or (s is not subject and EVENT.search(subject) and len(all_dates) == 1)):
            continue
        start, end = time_in(s)
        if not start and len(all_dates) == 1:
            start, end = first_time
        kind = next((k for k, rx in KIND if rx.search(s)), None) or next((k for k, rx in KIND if rx.search(subject)), "event")
        # the room: in this sentence; else in the mail when it names one date only (not for a cancellation)
        room = room_of(s) or ("" if kind == "cancel" or len(all_dates) > 1 else room_of(body))
        for _, d, how in ds[:2]:
            if d < sent:  # "the lecture on 11/11", written a week later: a reference, not an announcement
                continue
            if how == "today" and not start:  # "today" with no hour is usually not an event ("the confusion today about ...")
                continue
            if (d, kind) in seen:
                continue
            seen.add((d, kind))
            out.append({"date": d.isoformat(), "start": start, "end": end, "kind": kind, "kind_he": KIND_HE.get(kind, "אירוע"),
                        "title": subject.strip() or (KIND_HE.get(kind) or "אירוע"), "course": (course or {}).get("key"),
                        "course_name": (course or {}).get("name") or "", "sentence": s[:300], "room": room,
                        "sender": re.sub(r"\s*<[^>]*>", "", m.get("from") or "").strip().strip('"'),
                        "mail_date": m["date"], "mail_id": m.get("id"), "how": how})
    return out


def events(today=None, include_past=False, mails=None, since=None):
    """Every event in the kept mail (catalog\\mail\\*.json), soonest first: from `since` (default today) on, or all
    of them with include_past."""
    today = since or today or dt.date.today()
    courses = load_courses()
    if mails is None:
        mails, ids = [], set()
        for path in sorted((CATALOG / "mail").glob("20*.json")):
            for m in read_json(path, []) or []:
                if m.get("id") not in ids:
                    ids.add(m.get("id"))
                    mails.append(m)
    staff_file = CATALOG / "mail" / "staff.txt"
    staff = {address(m) for m in mails if is_announcement(m) and not re.match(r"(noreply|no-reply)@", address(m))}
    if staff_file.exists():
        staff |= {l.strip().lower() for l in staff_file.read_text(encoding="utf-8").splitlines() if "@" in l}
    out = []
    for m in mails:
        if m.get("date") and eligible(m, staff):
            out += [e for e in events_from(m, courses) if include_past or e["date"] >= today.isoformat()]
    # one event per (day, course, kind): the newest mail wins (a later mail corrects an earlier one)
    best = {}
    for e in sorted(out, key=lambda e: e["mail_date"]):
        best[(e["date"], e["course"] or e["course_name"], e["kind"])] = e
    return sorted(best.values(), key=lambda e: (e["date"], e["start"] or ""))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="also past events")
    ap.add_argument("--file", help="a JSON list of mails (as mail_pull writes) instead of catalog\\mail")
    args = ap.parse_args()
    mails = read_json(args.file, []) if args.file else None
    found = events(include_past=args.all, mails=mails)
    for e in found:
        print(f"{e['date']} {e['start'] or '--:--'}{'-' + e['end'] if e['end'] else ''} | {e['kind_he']} | {e['course_name'][:25]} | "
              f"{e['title'][:50]} | {e['room']} | « {e['sentence'][:110]} »")
    print(f"{len(found)} events")


if __name__ == "__main__":
    main()
