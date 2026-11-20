"""Writes Liran's study calendar as an .ics feed that Google Calendar can subscribe to.

    python -X utf8 calendar_feed.py                    write ~/study/site/cal/<secret>.ics
    python -X utf8 calendar_feed.py --out DIR          somewhere else
    python -X utf8 calendar_feed.py --test             print what would go in (counts and the first few), write nothing

What goes in, for the current semester only (BGU: א October-February, ב March-July):
  - the weekly timetable with rooms, from catalog\\portal\\schedule.json (portal_pull.py), repeating every week
    until the semester ends;
  - Moodle deadlines: assignment due dates and quiz closing times, from each current course's _moodle_<id>.json;
  - exams: kb\\exams.json, and the exams and quizzes in catalog\\portal\\exams_tasks.json;
  - the academic calendar from the same file: holidays and breaks, semester end, exam periods (all-day);
  - events announced in staff mail (mail_events.py: catalog\\mail): moved, cancelled or makeup lectures, exam rooms,
    meetings, deadlines; not the ones Moodle or the portal already gave.
The lessons repeat until the portal's "סיום סמסטר" date (the fixed guess in semester_end() only without it).

The file name is a secret (~/study/secrets/cal_token, made here on the first run), so the feed can be served without
the site password at https://<site>/cal/<secret>.ics. Older .ics files in the folder are removed, so a new token
retires the old address.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import secrets as pysecrets
import sys

import study_secrets

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CATALOG = HERE / "catalog"
SEM_DIR = {"א": "סמסטר א", "ב": "סמסטר ב", "קיץ": "קיץ"}
DAYS = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"]
VTIMEZONE = """BEGIN:VTIMEZONE
TZID:Asia/Jerusalem
BEGIN:DAYLIGHT
TZOFFSETFROM:+0200
TZOFFSETTO:+0300
TZNAME:IDT
DTSTART:19700327T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=23,24,25,26,27,28,29;BYDAY=FR
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0300
TZOFFSETTO:+0200
TZNAME:IST
DTSTART:19701025T020000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE"""


def semester_of(today):
    y = today.year
    if today.month >= 10:
        return f"{y}-{(y + 1) % 100:02d}", "א"
    if today.month <= 2:
        return f"{y - 1}-{y % 100:02d}", "א"
    if today.month <= 7:
        return f"{y - 1}-{y % 100:02d}", "ב"
    return f"{y - 1}-{y % 100:02d}", "קיץ"


def semester_end(year, sem):
    first = int(year[:4])
    return {"א": dt.date(first + 1, 2, 1), "ב": dt.date(first + 1, 7, 20)}.get(sem, dt.date(first + 1, 9, 30))


def esc(text):
    return str(text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def fold(line):
    """RFC 5545: lines of at most 75 octets, continued with a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def local_stamp(when):
    return when.strftime("%Y%m%dT%H%M%S")


def event(uid_key, title, start, end=None, location="", description="", rrule=None, all_day=False):
    uid = hashlib.sha1(uid_key.encode("utf-8")).hexdigest()[:20] + "@study-briefing"
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
             f"SUMMARY:{esc(title)}"]
    if all_day:
        lines += [f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
                  f"DTEND;VALUE=DATE:{(start + dt.timedelta(days=1)).strftime('%Y%m%d')}"]
    else:
        lines += [f"DTSTART;TZID=Asia/Jerusalem:{local_stamp(start)}",
                  f"DTEND;TZID=Asia/Jerusalem:{local_stamp(end or start + dt.timedelta(minutes=15))}"]
    if rrule:
        lines.append(f"RRULE:{rrule}")
    if location:
        lines.append(f"LOCATION:{esc(location)}")
    if description:
        lines.append(f"DESCRIPTION:{esc(description)}")
    lines.append("END:VEVENT")
    return lines


def read_json(path, default=None):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def current_offerings(year, sem):
    out = []
    for c in read_json(CATALOG / "courses.json", []):
        for o in c.get("offerings", []):
            if o.get("year") == year and o.get("semester") == sem:
                out.append((c, o))
    return out


def moodle_page(year, sem, course, moodle_id):
    base = ROOT / year / SEM_DIR[sem]
    for p in [base / course["name_he"] / "מידע על הקורס" / f"_moodle_{moodle_id}.json", *base.glob(f"*/*/_moodle_{moodle_id}.json"),
              *base.glob(f"*/_moodle_{moodle_id}.json")]:
        if p.exists():
            return read_json(p, {})
    return {}


def israel(ts):
    import zoneinfo
    return dt.datetime.fromtimestamp(ts, zoneinfo.ZoneInfo("Asia/Jerusalem")).replace(tzinfo=None)


def moodle_events(year, sem):
    out = []
    for course, off in current_offerings(year, sem):
        if not off.get("moodle_id"):
            continue
        page = moodle_page(year, sem, course, off["moodle_id"])
        for a in page.get("assignments") or []:
            if a.get("duedate"):
                when = israel(a["duedate"])
                out += event(f"assign {a.get('id')}", f"הגשה: {a.get('name')} · {course['name_he']}", when,
                             description=f"מודל: https://moodle.bgu.ac.il/moodle/mod/assign/view.php?id={a.get('cmid')}")
        for q in page.get("quizzes") or []:
            if q.get("timeclose"):
                when = israel(q["timeclose"])
                out += event(f"quiz {q.get('id')}", f"בוחן נסגר: {q.get('name')} · {course['name_he']}", when,
                             description=f"מודל: https://moodle.bgu.ac.il/moodle/mod/quiz/view.php?id={q.get('cmid')}")
    return out


def exam_events(year, sem):
    """The portal's exams and quizzes first (they have the hour); kb\\exams.json adds the "מועד א/ב" label and any
    exam the portal does not list (as an all-day event)."""
    out, kb = [], {}
    for e in read_json(HERE / "kb" / "exams.json", []) or []:
        if e.get("semester") != f"{year} {sem}" or not re.match(r"\d{4}-\d{2}-\d{2}$", str(e.get("date") or "")):
            continue
        kind = (e.get("type") or "").strip()
        label = f"מבחן {kind}" if kind.startswith("מועד") or not kind else kind  # "מבחן מועד א", "בוחן"
        kb[(dt.date.fromisoformat(e["date"]), e.get("course_name"))] = (label, e)
    seen = set()
    for item in portal_items():
        kind = item.get("calenderDataType") or item.get("dataType")  # 2 quiz, 3 exam (the portal's own spelling)
        if kind not in (2, 3):
            continue
        when = parse_when(item.get("date"))
        name = item.get("courseName") or item.get("name") or ""
        if not when or (when.date(), name) in seen:
            continue
        seen.add((when.date(), name))
        label = kb.get((when.date(), name), ("מבחן" if kind == 3 else "בוחן",))[0]
        timed = when.hour or when.minute
        out += event(f"portal {kind} {name} {when.isoformat()}", f"{label}: {name}", when if timed else when.date(),
                     end=when + dt.timedelta(hours=3) if timed else None, location=item.get("location") or "",
                     description=(item.get("description") or "").strip(), all_day=not timed)
    for (day, name), (label, e) in kb.items():
        if (day, name) not in seen:
            out += event(f"exam {e.get('course')} {e.get('type')} {e['date']}", f"{label}: {name}", day, all_day=True)
    return out


def known_dates(year, sem):
    """(course key, date, 'deadline'|'exam') the feed already has from Moodle, kb\\exams.json and the portal, so the
    same thing announced in a mail is not added twice."""
    out = set()
    names = {}
    for course, off in current_offerings(year, sem):
        names[norm(course.get("name_he"))] = course["key"]
        if not off.get("moodle_id"):
            continue
        page = moodle_page(year, sem, course, off["moodle_id"])
        for a in page.get("assignments") or []:
            if a.get("duedate"):
                out.add((course["key"], israel(a["duedate"]).date(), "deadline"))
        for q in page.get("quizzes") or []:
            if q.get("timeclose"):
                out.add((course["key"], israel(q["timeclose"]).date(), "deadline"))
    for e in read_json(HERE / "kb" / "exams.json", []) or []:
        if re.match(r"\d{4}-\d{2}-\d{2}$", str(e.get("date") or "")):
            out.add((e.get("course"), dt.date.fromisoformat(e["date"]), "exam"))
    for item in portal_items():
        when = parse_when(item.get("date"))
        key = names.get(norm(item.get("courseName")))
        if when and key and item.get("calenderDataType") in (2, 3):
            out.add((key, when.date(), "exam"))
    return out


def norm(s):
    return re.sub(r"[\s'\"׳״`.,:()\-–]", "", str(s or "")).replace("יא", "א")


def mail_feed_events(year, sem, today):
    """Events announced in staff mail (mail_events.py): a moved or cancelled lecture, a makeup class, an exam room,
    a deadline. From 30 days back, so they stay in the calendar's history; not what Moodle or the portal already gave."""
    try:
        import mail_events
    except ImportError:
        return []
    known = known_dates(year, sem)
    out = []
    for e in mail_events.events(since=today - dt.timedelta(days=30)):
        day = dt.date.fromisoformat(e["date"])
        kind = {"exam": "exam", "deadline": "deadline"}.get(e["kind"])
        if kind and any((e["course"], day + dt.timedelta(days=k), kind) in known for k in (-1, 0, 1)):
            continue
        short = e["course_name"].split(" ")[0:3]
        title = f"{e['kind_he']}: {e['title']}" + (f" · {' '.join(short)}" if e["course_name"] else "")
        note = f"«{e['sentence']}»\nמתוך מייל של {e['sender']} ({e['mail_date'][:10]})"
        if e["start"]:
            start = dt.datetime.combine(day, dt.time.fromisoformat(e["start"]))
            minutes = {"exam": 120, "lecture": 120, "office_hours": 120, "meeting": 60}.get(e["kind"], 30)
            end = dt.datetime.combine(day, dt.time.fromisoformat(e["end"])) if e["end"] else start + dt.timedelta(minutes=minutes)
            out += event(f"mail {e['mail_id']} {e['date']} {e['kind']}", title, start, end, location=e["room"], description=note)
        else:
            out += event(f"mail {e['mail_id']} {e['date']} {e['kind']}", title, day, location=e["room"], description=note, all_day=True)
    return out


def portal_items():
    data = (read_json(CATALOG / "portal" / "exams_tasks.json", {}) or {}).get("data")
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else list(walk_dicts(data))


def academic_events(year, sem):
    """The portal's academic calendar (calenderDataType 5): holidays and breaks, semester and exam-period dates."""
    out = []
    for item in portal_items():
        if item.get("calenderDataType") != 5:
            continue
        when = parse_when(item.get("date"))
        name = (item.get("name") or item.get("courseName") or "").strip()
        if not when or not name:
            continue
        note = (item.get("description") or "").strip()
        out += event(f"academic {name} {when.date()}", name, when.date(), all_day=True,
                     description=note if note != name else "")
    return out


def semester_end_from_portal(sem):
    """'סיום סמסטר סתיו ...' (א) or 'סיום סמסטר אביב ...' (ב) in the portal's academic calendar."""
    word = {"א": "סתיו", "ב": "אביב"}.get(sem)
    for item in portal_items():
        name = item.get("name") or ""
        if word and item.get("calenderDataType") == 5 and name.startswith("סיום סמסטר") and word in name:
            when = parse_when(item.get("date"))
            if when:
                return when.date()
    return None


def walk_dicts(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk_dicts(v)


def parse_when(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def room_of(item):
    parts = [str(v) for k, v in item.items() if v and isinstance(v, (str, int))
             and re.search(r"room|location|place|class(?!ification)|building|hall|חדר|מיקום", k, re.I)]
    return ", ".join(dict.fromkeys(parts))


TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


def slot_times(slot):
    start = end = None
    for k, v in slot.items():
        if not isinstance(v, str):
            continue
        m = TIME.search(v)
        if not m:
            continue
        if re.search(r"start|from|begin", k, re.I) and not start:
            start = (int(m.group(1)), int(m.group(2)))
        elif re.search(r"end|to$|until|finish", k, re.I) and not end:
            end = (int(m.group(1)), int(m.group(2)))
    return start, end


def lesson_mode(name, day_no, start):
    """'כיתה' or 'היברידי': the dated lessons in exams_tasks.json (calenderDataType 6) carry it in their description."""
    for item in portal_items():
        when = parse_when(item.get("date"))
        if (item.get("calenderDataType") == 6 and item.get("courseName") == name and when
                and (when.weekday() + 1) % 7 == day_no and when.time() == start):
            return (item.get("description") or "").strip()
    return ""


def timetable_events(year, sem, today):
    """Weekly lessons from the portal's scheduleByDays (best effort: the exact field names were not seen yet)."""
    data = (read_json(CATALOG / "portal" / "schedule.json", {}) or {}).get("data") or {}
    days = None
    sems = [s for s in (data.get("semesters") or {}).values() if isinstance(s.get("data"), dict)]
    pick = next((s for s in sems if s.get("checked")), sems[0] if sems else None)  # the filter marks the current one
    if pick:
        days = pick["data"].get("scheduleByDays")
    if days is None and data.get("from_page"):
        days = data["from_page"][0]
    if not isinstance(days, list):
        return []
    out, end = [], semester_end_from_portal(sem) or semester_end(year, sem)
    sunday = today - dt.timedelta(days=(today.weekday() + 1) % 7)
    until = f"UNTIL={end.strftime('%Y%m%d')}T235959Z"
    lessons = [l for l in walk_dicts(days) if isinstance(l.get("dayOfWeek"), int) and l.get("timeStart") is not None and l.get("name")]
    if lessons:  # the portal's fields (seen 18.11.2026): dayOfWeek 1 = Sunday, timeStart/timeEnd in hours
        for l in lessons:
            day_no = (l["dayOfWeek"] - 1) % 7
            first = sunday + dt.timedelta(days=day_no)
            begin = dt.datetime.combine(first, dt.time()) + dt.timedelta(hours=float(l["timeStart"]))
            finish = dt.datetime.combine(first, dt.time()) + dt.timedelta(hours=float(l.get("timeEnd") or l["timeStart"] + 1))
            mode = lesson_mode(l["name"], day_no, begin.time())
            kind = (l.get("type") or "").strip()
            out += event(f"lesson {l['name']} {day_no} {l['timeStart']}", f"{l['name']}{' (' + kind + ')' if kind else ''}",
                         begin, finish, location=l.get("location") or "", description=mode,
                         rrule=f"FREQ=WEEKLY;BYDAY={DAYS[day_no]};{until}")
        return out
    for index, day in enumerate(days):
        day_no = index
        if isinstance(day, dict):
            for k in ("dayOfWeek", "day", "dayNumber", "dayIndex"):
                if isinstance(day.get(k), int):
                    day_no = day[k] % 7
                    break
        for slot in walk_dicts(day):
            if slot is day:
                continue
            start, stop = slot_times(slot)
            name = next((str(v) for k, v in slot.items() if isinstance(v, str) and v.strip()
                         and re.search(r"course|name|title|subject", k, re.I) and not TIME.fullmatch(v.strip())), "")
            if not start or not name:
                continue
            first = sunday + dt.timedelta(days=day_no)
            begin = dt.datetime.combine(first, dt.time(*start))
            finish = dt.datetime.combine(first, dt.time(*(stop or (start[0] + 1, start[1]))))
            kind = next((str(v) for k, v in slot.items() if isinstance(v, str) and re.search(r"type|kind", k, re.I)), "")
            out += event(f"lesson {name} {day_no} {start}", f"{name}{' (' + kind + ')' if kind else ''}", begin, finish,
                         location=room_of(slot), rrule=f"FREQ=WEEKLY;BYDAY={DAYS[day_no]};UNTIL={end.strftime('%Y%m%d')}T235959Z")
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path.home() / "study" / "site" / "cal")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    year, sem = semester_of(today)
    parts = {"timetable": timetable_events(year, sem, today), "moodle": moodle_events(year, sem), "exams": exam_events(year, sem),
             "academic": academic_events(year, sem), "mail": mail_feed_events(year, sem, today)}
    counts = {k: sum(1 for line in v if line == "BEGIN:VEVENT") for k, v in parts.items()}
    print(f"{year} {sem}: " + ", ".join(f"{k} {n}" for k, n in counts.items()))
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//study-briefing//calendar_feed//HE", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:לימודים", "X-WR-TIMEZONE:Asia/Jerusalem", *VTIMEZONE.splitlines(),
             *parts["timetable"], *parts["moodle"], *parts["exams"], *parts["academic"], *parts["mail"], "END:VCALENDAR"]
    body = "\r\n".join(fold(l) for l in lines) + "\r\n"
    if args.test:
        titles = [l[8:] for l in lines if l.startswith("SUMMARY:")][:8]
        print("first events: " + " | ".join(titles))
        print("test done. Nothing was written.")
        return 0
    token_file = study_secrets.path("cal_token")
    if not token_file.exists():
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(pysecrets.token_urlsafe(24) + "\n", encoding="utf-8")
        os.chmod(token_file, 0o600)
    token = token_file.read_text(encoding="utf-8").strip()
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / f"{token}.ics"
    for old in args.out.glob("*.ics"):
        if old != target:
            old.unlink()
    tmp = args.out / ".feed.tmp"
    tmp.write_text(body, encoding="utf-8", newline="")
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)
    print(f"feed written: {args.out}/<cal_token>.ics ({sum(counts.values())} events)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
