#!/usr/bin/env python
"""Everything one morning episode of ONE course needs, as one JSON. Standard library only, deterministic, no LLM.

    python -X utf8 podcast_context.py <course-key> <YYYY-MM-DD>            writes the context file
    python -X utf8 podcast_context.py --list-active                         the course keys that get an episode

The file lands in  <study tree>\\פודקאסט\\context\\<course-key>\\<date>.json  (override with --out).

Where each part comes from (all of it is data, nothing is guessed):
  schedule      site\\out\\data\\site.json  home.schedule (the portal timetable), dates computed from the weekday
  materials     catalog\\moodle_files.jsonl (current offering only; the copied-over old-year files are dropped)
                joined to catalog\\files.jsonl and catalog\\text\\<id>.txt (slide text)
  syllabus      the current offering's syllabus text: week plan, dated plan, staff and groups
  assignments   site.json courses[].moodle.assignments / quizzes / grades, home.exams
  announcements the forum markdown files of the current offering (staff only; students are dropped)
  submissions   catalog my_submission files + the Moodle submission status
  prereqs       kb\\courses\\<key>.prereqs.json ranked against the newest slide text, kb\\concepts.json for names

site.json is rebuilt with site\\build.py when it is older than --max-age-hours (default 18), unless --refresh never.
"""
import argparse
import collections
import datetime as dt
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent                 # study-briefing
TREE = HERE.parent                                     # the study tree root
WIN_ROOT = r"C:\Users\Liran\OneDrive\שולחן העבודה\מדעי המחשב"
SITE_JSON = HERE / "site" / "out" / "data" / "site.json"
CATALOG = HERE / "catalog"
PODCAST = TREE / "פודקאסט"

# A syllabus that is known to be better than what the generic rule picks (the updated one, 18.11.2026).
SYLLABUS_OVERRIDE = {"geometric-learning-theory": "f_ccfb645d"}

DAYS = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת"]
BIDI = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
PAGE_MARK = re.compile(r"^=== page (\d+) ===\s*$", re.M)
JUNK_LINES = re.compile(r"^(\[תמלול כתב יד\]|\[עמוד ריק\]|•|הצהרת פרטיותהצהרת נגישות.*)$")


# ------------------------------------------------------------------ small helpers

def day_index(d):
    """Hebrew week index: 0 = Sunday ... 4 = Thursday, 6 = Saturday."""
    return (d.weekday() + 1) % 7


def he_date(d):
    return f"יום {DAYS[day_index(d)]} {d.day}.{d.month}.{d.year}"


def parse_iso(s):
    if not s:
        return None
    s = s.strip()
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        try:
            return dt.datetime.combine(dt.date.fromisoformat(s[:10]), dt.time())
        except ValueError:
            return None


def hhmm(s):
    return (s or "")[:5]


def clean(text):
    return BIDI.sub("", text or "")


def read_text(fid):
    p = CATALOG / "text" / f"{fid}.txt"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def pages_of(raw):
    """[(page number, cleaned page text)] from a catalog text file."""
    parts = PAGE_MARK.split(raw)
    out = []
    for i in range(1, len(parts), 2):
        out.append((int(parts[i]), clean(parts[i + 1])))
    return out or [(1, clean(raw))]


def squash(s):
    return re.sub(r"\s+", " ", s).strip()


def norm_name(s):
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"[\"'׳״`.]", "", s)
    return squash(s)


def rel_of(abs_or_rel):
    """A stored path (absolute Windows path or tree-relative) as a tree-relative path with backslashes."""
    p = (abs_or_rel or "").replace("/", "\\")
    if p.lower().startswith(WIN_ROOT.lower() + "\\"):
        p = p[len(WIN_ROOT) + 1:]
    return p


# ------------------------------------------------------------------ site.json

def site_age_hours(site):
    g = parse_iso(site.get("generated"))
    if not g:
        return 1e9
    return (dt.datetime.now() - g).total_seconds() / 3600


def load_site(refresh="auto", max_age=18.0):
    rebuilt = False
    need = refresh == "always" or not SITE_JSON.exists()
    if not need and refresh == "auto":
        try:
            site = json.loads(SITE_JSON.read_text(encoding="utf-8"))
            need = site_age_hours(site) > max_age
        except Exception:
            need = True
    if need and refresh != "never":
        run = subprocess.run([sys.executable, "-X", "utf8", str(HERE / "site" / "build.py")],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        if run.returncode != 0:
            print("site\\build.py failed:\n" + (run.stderr or run.stdout)[-1500:], file=sys.stderr)
        rebuilt = run.returncode == 0
    site = json.loads(SITE_JSON.read_text(encoding="utf-8"))
    return site, rebuilt


def active_courses(site):
    sem = site.get("semester") or {}
    out = []
    for c in site.get("courses", []):
        if c.get("is_research") or not c.get("enrolled"):
            continue
        if c.get("year") == sem.get("year") and c.get("semester") == sem.get("semester"):
            out.append(c["key"])
    return out


# ------------------------------------------------------------------ catalog

class Catalog:
    def __init__(self):
        self.files = {}
        self.by_path = {}
        self.by_name_size = collections.defaultdict(list)
        with open(CATALOG / "files.jsonl", encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                self.files.setdefault(r["id"], []).append(r)
                self.by_path[rel_of(r["path"]).lower()] = r
                self.by_name_size[(r["name"], r["size"])].append(r)
        self.moodle = [json.loads(l) for l in open(CATALOG / "moodle_files.jsonl", encoding="utf-8")]
        self.courses = {c["key"]: c for c in json.loads((CATALOG / "courses.json").read_text(encoding="utf-8"))}

    def join(self, mrow):
        """The catalog record of a Moodle row, or None. sha256 first, then the filed path, then name+size."""
        sha = mrow.get("sha256")
        if sha and ("f_" + sha[:8]) in self.files:
            return self.files["f_" + sha[:8]][0]
        filed = mrow.get("filed")
        if filed:
            r = self.by_path.get(rel_of(filed).lower())
            if r:
                return r
        cands = self.by_name_size.get((mrow.get("filename"), mrow.get("size")))
        if cands:
            return cands[0]
        return None

    def course_files(self, key, year):
        out = []
        for rs in self.files.values():
            for r in rs:
                if r.get("course") == key and rel_of(r["path"]).startswith(year + "\\"):
                    out.append(r)
        return out

    def course_name(self, key):
        c = self.courses.get(key)
        return c["name_he"] if c else key


# ------------------------------------------------------------------ syllabus

def split_sections(text):
    """{heading: body} for '## heading' sections of a cleaned syllabus text."""
    secs, cur, buf = {}, "_head", []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.*)$", line)
        if m:
            secs[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    secs[cur] = "\n".join(buf).strip()
    return secs


def clean_syllabus(raw):
    lines = []
    for l in clean(raw).splitlines():
        if PAGE_MARK.match(l) or JUNK_LINES.match(l.strip()):
            continue
        lines.append(l.rstrip())
    return "\n".join(lines)


NAME_RE = re.compile(r"((?:ד\"ר|פרופ'|מר|גב'|גברת)\s+[\u0590-\u05FF'\"\- .]{2,40}?)(?=\s*(?:—|,|;|:|$)|\s+[A-Za-z]|\s+יום\b|\s+שעות\b)")
SLOT_RE = re.compile(r"יום\s+(\S+)\s+(\d\d:\d\d)-(\d\d:\d\d)")


def parse_staff(sec_text, default_group=None):
    """Slots of the course staff: [{group, type, name, day, start, end, room, building}]."""
    slots, group = [], default_group
    for line in sec_text.splitlines():
        line = line.strip()
        gm = re.match(r"^-?\s*קבוצה\s+(\d+)", line)
        if gm:
            group = gm.group(1)
        elif re.match(r"^קבוצה\s+\d+\s*:?\s*$", line):
            continue
        if not line.startswith("-") and not gm:
            continue
        typ = "תרגיל" if re.search(r"תרגיל", line[:40]) else ("שעור" if re.search(r"שעור", line[:40]) else "")
        nm = NAME_RE.search(line)
        name = squash(nm.group(1)) if nm else ""
        pieces = re.split(r";\s*(?=יום)", line)
        for piece in pieces:
            for sm in SLOT_RE.finditer(piece):
                room = re.search(r"חדר\s+(\d+)", piece[sm.end():])
                bld = re.search(r"\[(\d+)\]|בניין\s+(\d+)|בנין\s+(\d+)", piece[sm.end():])
                slots.append({
                    "group": group, "type": typ, "name": name, "day": sm.group(1),
                    "start": sm.group(2), "end": sm.group(3),
                    "room": room.group(1) if room else None,
                    "building": next((g for g in (bld.groups() if bld else []) if g), None),
                })
    # a '(המשך)' line without its own name inherits it from the same group and type
    for s in slots:
        if not s["name"]:
            s["name"] = next((o["name"] for o in slots if o["group"] == s["group"] and o["type"] == s["type"] and o["name"]), "")
    return slots


def parse_syllabus(fid, year_label):
    raw = read_text(fid)
    text = clean_syllabus(raw)
    secs = split_sections(text)
    weeks_key = next((k for k in secs if "נושאי הקורס" in k), None)
    staff_key = next((k for k in secs if k.startswith("צוות הקורס")), None)
    desc_key = next((k for k in secs if k.startswith("תיאור הקורס")), None)
    staff_body = secs.get(staff_key, "") if staff_key else ""
    if not staff_body:                       # some syllabi have 'צוות הקורס:' as a plain line, not a heading
        lines, grab, buf = text.splitlines(), False, []
        for l in lines:
            if not grab and re.match(r"^צוות הקורס\s*:?\s*$", l.strip()):
                grab = True
                continue
            if grab and l.startswith("## "):
                break
            if grab:
                buf.append(l)
        staff_body = "\n".join(buf)
    weeks_body = secs.get(weeks_key, "") if weeks_key else ""
    dated, numbered, plain = [], [], []
    for line in weeks_body.splitlines():
        line = line.strip()
        tm = re.match(r"^\|\s*(\d+)\s*\|\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|?$", line)
        if tm:
            n, d, mth, y, topic, ch = tm.groups()
            dated.append({"n": int(n), "date": f"{int(y):04d}-{int(mth):02d}-{int(d):02d}",
                          "topic": topic.strip(), "chapters": ch.strip() or None})
            continue
        nm = re.match(r"^(\d+)\.\s+(.*)$", line)
        if nm:
            numbered.append({"n": int(nm.group(1)), "topic": nm.group(2).strip()})
        elif line and not line.startswith("|") and not line.startswith("-"):
            plain.append(line)
    return {
        "file_id": fid,
        "title_line": next((l for l in text.splitlines() if l.startswith("# ")), "").lstrip("# ").strip(),
        "weeks_heading_found": bool(weeks_key),
        "dated_plan": dated,
        "numbered_plan": numbered,
        "plan_notes": plain,
        "course_description": (secs.get(desc_key) or "")[:4500] if desc_key else None,
        "staff_section": staff_body,
        "staff_slots": parse_staff(staff_body, (re.search(r"קבוצה\s+(\d+)", staff_key or "") or [None, None])[1]),
        "grading_section": (secs.get("אופן הערכה") or "")[:1800],
    }


def pick_syllabus(cat, key, year):
    if key in SYLLABUS_OVERRIDE and SYLLABUS_OVERRIDE[key] in cat.files:
        return SYLLABUS_OVERRIDE[key]
    best = None
    for r in cat.course_files(key, year):
        if r["kind"] != "syllabus":
            continue
        raw = read_text(r["id"])
        if not raw or "נושאי הקורס בחלוקה" not in raw:
            continue
        if best is None or (r["mtime"], r["id"]) > (best["mtime"], best["id"]):
            best = r
    return best["id"] if best else None


# ------------------------------------------------------------------ schedule

def slot_view(lesson, d, syl_slots):
    typ = "תרגול" if lesson["type"] == "תרגיל" else "הרצאה"
    bm = re.match(r"\[(\d+)\]", lesson.get("location") or "")
    bname = re.sub(r"\s*\(\d+\)\s*$", "", lesson.get("building") or "").strip()
    staff = None
    for s in syl_slots:
        if s["day"] == lesson["day_name"] and s["start"] == hhmm(lesson["start"]) and s["room"] == lesson.get("room"):
            staff = {"name": s["name"], "group": s["group"]}
            break
    return {
        "date": d.isoformat(), "date_he": he_date(d), "day": lesson["day_name"],
        "start": hhmm(lesson["start"]), "end": hhmm(lesson["end"]), "type": typ, "type_portal": lesson["type"],
        "building_number": bm.group(1) if bm else None, "building_name": bname, "room": lesson.get("room"),
        "mode": lesson.get("mode"), "location_raw": lesson.get("location"),
        "staff": staff, "overlaps_another_course": bool(lesson.get("clash")),
    }


def build_schedule(site, key, d, syl_slots):
    sch = site["home"]["schedule"]
    mine = [l for l in sch["lessons"] if l.get("course") == key]
    breaks = {a["date"] for a in sch.get("academic", []) if a.get("break")}

    def on(day):
        return [l for l in mine if l["day"] == day_index(day)]

    today = [slot_view(l, d, syl_slots) for l in sorted(on(d), key=lambda x: x["start"])]
    previous, nxt = [], []
    for k in range(1, 15):
        dd = d - dt.timedelta(days=k)
        for l in sorted(on(dd), key=lambda x: x["start"], reverse=True):
            if len(previous) < 4:
                previous.append(slot_view(l, dd, syl_slots))
    for k in range(1, 22):
        dd = d + dt.timedelta(days=k)
        if dd.isoformat() in breaks:
            continue
        for l in sorted(on(dd), key=lambda x: x["start"]):
            if len(nxt) < 5:
                nxt.append(slot_view(l, dd, syl_slots))
    horizon = d + dt.timedelta(days=21)
    academic = [a for a in sch.get("academic", []) if d.isoformat() <= a["date"] <= horizon.isoformat()]
    types_today = []
    for s in today:
        if s["type"] not in types_today:
            types_today.append(s["type"])
    today_summary = {
        "has_class_today": bool(today),
        "types_today": types_today,
        "spoken_summary_he": ("יש היום " + " ו".join(types_today) + " בקורס") if types_today else "אין היום שיעור בקורס",
        "spoken_rule": "Say ONLY whether there is a lecture/recitation today or not (and any cancellation/change from staff announcements). Do NOT read out room numbers, building numbers, hours, or future timetable dates.",
    }
    return {
        "source": "site.json home.schedule (portal timetable)", "pulled": sch.get("pulled"), "label": sch.get("label"),
        "today_summary": today_summary,
        "weekly_pattern": [{"day": l["day_name"], "start": hhmm(l["start"]), "end": hhmm(l["end"]), "type": l["type"],
                            "room": l.get("room"), "building": l.get("building")} for l in sorted(mine, key=lambda x: (x["day"], x["start"]))],
        "today_slots": today, "previous_slots": previous, "next_slots": nxt,
        "academic_calendar_next_3_weeks": academic,
        "note": "the weekly pattern is the portal's; cancellations and moves appear only in the staff announcements below. In the podcast, state only if there is class today or not.",
    }


def my_groups(site, key, syl_slots):
    sch = site["home"]["schedule"]
    groups, names = set(), set()
    for l in sch["lessons"]:
        if l.get("course") != key:
            continue
        for s in syl_slots:
            if s["day"] == l["day_name"] and s["start"] == hhmm(l["start"]) and s["room"] == l.get("room"):
                groups.add(s["group"])
    for s in syl_slots:
        if s["group"] in groups and s["name"]:
            names.add(s["name"])
    return sorted(g for g in groups if g), sorted(names)


# ------------------------------------------------------------------ forums / announcements

POST_RE = re.compile(r"^\*\*(.+?)\*\*,\s*(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})\s*$")


def parse_forum(raw):
    text = "\n".join(l for l in clean(raw).splitlines() if not PAGE_MARK.match(l) and not JUNK_LINES.match(l.strip()))
    title = (re.search(r"^#\s+(.+)$", text, re.M) or [None, ""])[1].strip()
    typ = (re.search(r"פורום מסוג\s+(\S+)", text) or [None, ""])[1]
    posts, disc, cur = [], "", None
    for line in text.splitlines():
        if line.startswith("## "):
            disc, cur = line[3:].strip(), None
            continue
        m = POST_RE.match(line.strip())
        if m:
            a, dd, mm, yy, hh, mi = m.groups()
            cur = {"discussion": disc, "author": a.strip(), "time": f"{yy}-{mm}-{dd}T{hh}:{mi}", "lines": []}
            posts.append(cur)
        elif cur is not None:
            cur["lines"].append(line)
    for p in posts:
        p["text"] = "\n".join(l for l in p.pop("lines") if l.strip()).strip()
    return title, typ, posts


def tag_post(text, title):
    t = f"{title} {text}"
    tags = []
    if re.search(r"מבוטל|בוטל|ביטול|לא יתקיים|לא תתקיים|מתבטל", t):
        tags.append("ביטול")
    if re.search(r"חדר|כיתה|בניין|בנין|בשעה|שעות|\d{1,2}:\d{2}|\d{1,2}-\d{1,2}", t):
        tags.append("חדר או שעה")
    if re.search(r"תיקון|נפלה טעות|טעות|עדכון|שינוי|במקום", t):
        tags.append("תיקון או שינוי")
    if re.search(r"עבודה|תרגיל בית|הגשה|בוחן|מבחן|בחינה", t):
        tags.append("מטלה או בחינה")
    if re.search(r"הקלטה|זום|zoom", t, re.I):
        tags.append("הקלטה או זום")
    return tags


def surname_of(staff_name):
    n = norm_name(staff_name)
    n = re.sub(r"^(דר|פרופ|מר|גב|גברת)\s+", "", n)
    parts = n.split()
    if len(parts) >= 2 and len(parts[0]) <= 2:
        parts = parts[1:]                       # drop the initial: "ע כלמטץ" -> "כלמטץ"
    return " ".join(parts)


def build_announcements(cat, key, year, syl_slots, mygroups, d, window_days):
    forum_rows = [r for r in cat.course_files(key, year) if "הודעות ופורומים" in r["path"] and r["ext"] == ".md"]
    boards, staff_authors = [], set()
    parsed = []
    for r in forum_rows:
        raw = read_text(r["id"])
        if not raw:
            continue
        title, typ, posts = parse_forum(raw)
        parsed.append((r, title, typ, posts))
        if typ == "news":
            staff_authors |= {p["author"] for p in posts}
            boards.append(title)
    surnames = {}
    for s in syl_slots:
        if s["name"]:
            surnames.setdefault(surname_of(s["name"]), set()).add(s["group"])
    staff_surnames = set(surnames)

    def is_staff(author):
        a = norm_name(author)
        return author in staff_authors or any(sn and sn in a for sn in staff_surnames)

    def author_group(author):
        a = norm_name(author)
        hit = [g for sn, gs in surnames.items() if sn and sn in a for g in gs]
        if not hit:
            return "unknown"
        return "my" if set(hit) & set(mygroups) else "other"

    since = dt.datetime.combine(d - dt.timedelta(days=window_days), dt.time())
    until = dt.datetime.combine(d, dt.time(23, 59))
    items, dropped_students = [], 0
    for r, title, typ, posts in parsed:
        for p in posts:
            t = parse_iso(p["time"])
            if not t or t < since or t > until:
                continue
            if not is_staff(p["author"]):
                dropped_students += 1
                continue
            text = p["text"]
            text = re.sub(r"https?://\S+", "[קישור]", text)
            items.append({
                "forum": title, "forum_type": typ, "kind": "announcement" if typ == "news" else "staff_reply_in_forum",
                "discussion": p["discussion"], "author": p["author"], "author_group": author_group(p["author"]),
                "time": p["time"], "text": text[:1400], "tags": tag_post(text, p["discussion"]),
            })
    items.sort(key=lambda x: x["time"], reverse=True)
    for i in items:
        i["age_days"] = (d - dt.date.fromisoformat(i["time"][:10])).days
    changes = [i for i in items if i["author_group"] != "other"
               and (set(i["tags"]) & {"ביטול", "תיקון או שינוי"} or ("חדר או שעה" in i["tags"] and i["kind"] == "announcement"))]
    return {
        "source": "forum markdown files of the current offering (catalog text), staff posts only",
        "window_days": window_days, "students_posts_dropped": dropped_students,
        "my_groups": mygroups,
        "note": "author_group: 'my' = staff of your group, 'other' = staff of another group (their lesson/room changes are not yours; "
                "course-wide items such as assignment publications still apply), 'unknown' = not matched to a group",
        "announcements": items, "possible_changes": changes[:8],
    }


# ------------------------------------------------------------------ materials

LECT_RE = re.compile(r"שיעור|שיעורים|מצגת|מצגות|lecture|lesson|סיכום", re.I)
SKIP_EXT = {".png", ".jpg", ".jpeg", ".txt", ".zip", ".json", ".html", ".md"}


def lesson_date_hint(module, year_start):
    m = re.search(r"(\d{1,2})(?:-(\d{1,2}))?/(\d{1,2})", module or "")
    if not m:
        return None
    day, day2, mon = int(m.group(1)), m.group(2), int(m.group(3))
    y = year_start if mon >= 8 else year_start + 1
    try:
        return dt.date(y, mon, day).isoformat()
    except ValueError:
        return None


def number_hint(filename, module):
    for s in (filename or "", module or ""):
        m = re.search(r"lecture_(\d+)", s) or re.search(r"שיעור(?:ים)?\s*(\d+)", s) or re.search(r"תרגול\s*(?:מספר\s*)?(\d+)", s)
        if m:
            return int(m.group(1))
    return None


def outline_of(raw, limit=60):
    out = []
    for pn, body in pages_of(raw)[:limit]:
        lines = [l.strip() for l in body.splitlines() if l.strip()]
        lines = [l for l in lines if not JUNK_LINES.match(l)]
        if not lines:
            continue
        head = lines[:2] if re.fullmatch(r"[\d\s·]+", lines[0]) is None else lines[1:3]
        out.append({"p": pn, "t": squash(" | ".join(head))[:130]})
    return out


HL_RE = re.compile(r"(משפט|הגדרה|למה|טענה|מסקנה|הוכחה|דוגמה|שאלה|Theorem|Lemma|Definition)")


def highlights_of(raw, n=24):
    out = []
    for pn, body in pages_of(raw):
        for l in body.splitlines():
            l = squash(l)
            if 8 <= len(l) <= 220 and HL_RE.search(l[:28]):
                out.append(f"p{pn}: {l}")
                if len(out) >= n:
                    return out
    return out


def build_materials(cat, key, moodle_id, year, d, text_chars):
    year_start = int(year[:4])
    rows = [r for r in cat.moodle if r["course"] == moodle_id and r.get("modname") in ("resource", "folder")]
    minutes = collections.Counter((r.get("mtime") or 0) // 60 for r in rows)
    legacy_minutes = {m for m, c in minutes.items() if c >= 12}        # the course-copy batch of old files
    legacy = [r for r in rows if (r.get("mtime") or 0) // 60 in legacy_minutes]
    rows = [r for r in rows if r not in legacy]
    modules = collections.OrderedDict()
    for r in rows:
        modules.setdefault(r["module_id"], []).append(r)
    items = []
    for mid, rs in modules.items():
        first = rs[0]
        module, section = first.get("module") or "", first.get("section") or ""
        ext_ok = [r for r in rs if Path(r.get("filename") or "").suffix.lower() not in SKIP_EXT]
        if not ext_ok:
            continue
        is_rec = "תרגול" in section or "תרגול" in module
        is_lec = not is_rec and (LECT_RE.search(module) or re.search(r"lecture|W\d|Lesson|שיעור", first.get("filename") or "", re.I))
        if re.search(r"סילבוס|לו.?ז|ספר", module) or section in ("עבודות",) or "מבחנים" in section or "בחנים" in section:
            continue
        if not (is_rec or is_lec):
            continue
        # one representative file per module: pdf, then pptx, then others; prefer one the catalog has text for
        def rank(r):
            c = cat.join(r)
            has_text = bool(c and read_text(c["id"]))
            ext = Path(r["filename"]).suffix.lower()
            return (0 if has_text else 1, {".pdf": 0, ".pptx": 1, ".ppsx": 2, ".docx": 3}.get(ext, 9))
        rep = sorted(ext_ok, key=rank)[0]
        c = cat.join(rep)
        raw = read_text(c["id"]) if c else None
        mt = dt.datetime.fromtimestamp(rep["mtime"]) if rep.get("mtime") else None
        ld = lesson_date_hint(module, year_start)
        item = {
            "kind": "recitation" if is_rec else "lecture", "title": module, "filename": rep["filename"], "section": section,
            "number": number_hint(rep["filename"], module), "lesson_date_in_title": ld,
            "moodle_modified": mt.isoformat(timespec="minutes") if mt else None,
            "catalog_id": c["id"] if c else None, "pages": c.get("pages") if c else None,
            "text_available": bool(raw), "url": f"https://moodle.bgu.ac.il/moodle/mod/{rep.get('modname')}/view.php?id={mid}",
            "_raw": raw,
        }
        items.append(item)

    lectures = [i for i in items if i["kind"] == "lecture"]
    recs = [i for i in items if i["kind"] == "recitation"]

    def key_fn(i):
        return (i["lesson_date_in_title"] or "", i["number"] or 0, i["moodle_modified"] or "")

    lectures.sort(key=key_fn)
    recs.sort(key=lambda i: (i["number"] or 0, i["moodle_modified"] or ""))
    today = d.isoformat()
    dated = [i for i in lectures if i["lesson_date_in_title"]]
    if dated:                                         # date-keyed course (slides named by lesson date)
        past = [i for i in dated if i["lesson_date_in_title"] < today]
        future = [i for i in dated if i["lesson_date_in_title"] >= today]
        newest, upcoming = past[-2:][::-1], future[:2]
    else:                                             # numbered course
        published = [i for i in lectures if (i["moodle_modified"] or "") <= today + "T23:59"]
        newest, upcoming = published[-2:][::-1], []

    def full(i, chars):
        raw = i["_raw"]
        v = {k: i[k] for k in i if not k.startswith("_")}
        if raw:
            body = "\n".join(t for _, t in pages_of(raw))
            v["outline"] = outline_of(raw)
            v["highlights"] = highlights_of(raw, n=32)
            if chars > 0:
                v["text_head"] = squash(body)[:chars]
            v["text_file"] = f"study-briefing\\catalog\\text\\{i['catalog_id']}.txt"
        return v

    newest_v = [full(i, text_chars if n == 0 else text_chars // 2) for n, i in enumerate(newest)]
    upcoming_v = [full(i, text_chars // 3) for i in upcoming]
    rec_published = [i for i in recs if (i["moodle_modified"] or "") <= today + "T23:59"]
    newest_rec_v = [full(i, text_chars // 2) for i in rec_published[-1:]]

    def spine_entry(i, recent_chars=0):
        raw = i["_raw"]
        v = {k: i[k] for k in ("kind", "title", "filename", "section", "number", "lesson_date_in_title", "moodle_modified", "catalog_id", "pages", "text_available", "url")}
        if raw:
            v["outline"] = outline_of(raw, limit=40)
            v["highlights"] = highlights_of(raw, n=28)
            v["text_file"] = f"study-briefing\\catalog\\text\\{i['catalog_id']}.txt"
            if recent_chars > 0:
                body = "\n".join(t for _, t in pages_of(raw))
                v["text_head"] = squash(body)[:recent_chars]
        return v

    recent_lec_ids = {i.get("catalog_id") for i in (past[-4:] if dated else published[-4:]) if i.get("catalog_id")}
    all_lectures_v = [spine_entry(i, text_chars // 3 if i.get("catalog_id") in recent_lec_ids else 0) for i in lectures]
    all_recs_v = [spine_entry(i, text_chars // 4 if i in rec_published[-2:] else 0) for i in recs]
    course_spine = []
    for idx, lec in enumerate(all_lectures_v, 1):
        course_spine.append({
            "order": idx,
            "number": lec.get("number") or idx,
            "title": lec.get("title"),
            "date": lec.get("lesson_date_in_title") or (lec.get("moodle_modified") or "")[:10],
            "key_headings": [o["t"] for o in (lec.get("outline") or [])[:8]],
            "highlights": (lec.get("highlights") or [])[:12],
            "text_file": lec.get("text_file"),
        })
    return {
        "source": "catalog moodle_files + files + text (current offering only; full cumulative course history included)",
        "usage_rule": "Look across ALL lectures in course_spine and all_lectures (not only newest_lectures), because concepts build on top of each other and connect to current/upcoming homework. Filter and synthesize only what is relevant to the current progress and homework so the episode stays focused and under 12 minutes.",
        "legacy_files_dropped": len(legacy),
        "legacy_note": "the Moodle page of this offering also holds files copied from earlier years (same upload minute); they are not used" if legacy else None,
        "course_spine": course_spine,
        "newest_lectures": newest_v, "upcoming_lectures_already_published": upcoming_v, "newest_recitation": newest_rec_v,
        "all_lectures": all_lectures_v, "recitations": all_recs_v,
        "recitation_text_note": "a recitation file with text_available false is known by its title only" if any(not i["text_available"] for i in recs) else None,
    }


# ------------------------------------------------------------------ assignments, quizzes, grades

ROMAN_NUM = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}


def parse_hw_number(name):
    if not name:
        return None
    m = re.search(r"(?:עבודה|תרגיל(?:\s+בית)?|homework|hw|exercise)\s*(?:מספר\s*)?(\d+)", name, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(VI|IV|V|III|II|I)\b", name)
    if m:
        return ROMAN_NUM.get(m.group(1))
    m = re.search(r"(\d+)", name)
    return int(m.group(1)) if m else None


def match_lectures_to_hw(hw_text, all_lectures):
    if not hw_text or not all_lectures:
        return []
    hw_norm = norm_name(hw_text).lower()
    hw_tokens = {t.lower() for t in re.findall(r"[\u0590-\u05FFA-Za-z]{4,}", hw_norm)}
    scored = []
    for lec in all_lectures:
        blob = " ".join([lec.get("title") or ""] + [o["t"] for o in (lec.get("outline") or [])] + (lec.get("highlights") or []) + [lec.get("text_head") or ""])
        if not blob.strip() and lec.get("catalog_id"):
            raw = read_text(lec["catalog_id"])
            if raw:
                blob = squash(" ".join(t for _, t in pages_of(raw)))[:5000]
        lec_tokens = {t.lower() for t in re.findall(r"[\u0590-\u05FFA-Za-z]{4,}", norm_name(blob))}
        common = sorted(hw_tokens & lec_tokens)
        if len(common) >= 2:
            scored.append({
                "number": lec.get("number"),
                "title": lec.get("title"),
                "text_file": lec.get("text_file"),
                "shared_terms_count": len(common),
                "sample_shared_terms": common[:12],
            })
    scored.sort(key=lambda x: (-x["shared_terms_count"], x.get("number") or 0))
    return scored[:5]


def due_text(due):
    t = parse_iso(due)
    if not t:
        return None
    if (t.hour, t.minute) == (0, 0):
        prev = t - dt.timedelta(days=1)
        return f"{he_date(t.date())} בשעה 00:00 (כלומר בחצות שבין {DAYS[day_index(prev)]} {prev.day}.{prev.month} ל{DAYS[day_index(t)]})"
    return f"{he_date(t.date())} בשעה {t.strftime('%H:%M')}"


def build_work(site, cat, key, year, d, all_lectures=None):
    c = next(x for x in site["courses"] if x["key"] == key)
    mo = c.get("moodle") or {}
    now = dt.datetime.combine(d, dt.time(7, 0))      # the morning of the episode
    cfiles = cat.course_files(key, year)
    hw_by_num = {}
    sum_by_num = {}
    for r in cfiles:
        raw_num = r.get("number")
        try:
            num = int(raw_num) if raw_num is not None else None
        except (TypeError, ValueError):
            num = parse_hw_number(str(raw_num))
        if not num:
            num = parse_hw_number(r.get("name")) or parse_hw_number(r.get("path"))
        if r["kind"] == "assignment" and num:
            if num not in hw_by_num or (r.get("mtime") or 0) > (hw_by_num[num].get("mtime") or 0):
                hw_by_num[num] = r
        elif r["kind"] == "assign_summary" and num:
            sum_by_num[num] = r
    asg = []
    for a in mo.get("assignments", []):
        due = parse_iso(a.get("due"))
        if a.get("submitted"):
            state = "graded" if a.get("grade") is not None else "submitted_waiting_for_grade"
        elif due and due < now:
            state = "closed_not_submitted"
        else:
            state = "open_not_submitted"
        num = parse_hw_number(a.get("name"))
        hw_r = hw_by_num.get(num)
        sum_r = sum_by_num.get(num)
        hw_raw = read_text(hw_r["id"]) if hw_r else None
        sum_raw = read_text(sum_r["id"]) if sum_r else None
        hw_body = squash("\n".join(t for _, t in pages_of(hw_raw))) if hw_raw else None
        char_cap = 6500 if state == "open_not_submitted" else 3000
        entry = {
            "name": a["name"], "number": num, "due": a.get("due"), "due_text": due_text(a.get("due")),
            "days_from_episode_date": (due.date() - d).days if due else None,
            "status_moodle": a.get("status"), "submitted": bool(a.get("submitted")), "state": state,
            "team_submission": a.get("team"), "counts_toward_grade": a.get("counts"),
            "grade": a.get("grade"), "max": a.get("max"), "opens": a.get("opens"), "url": a.get("url"),
        }
        if hw_r:
            entry["catalog_id"] = hw_r["id"]
            entry["filename"] = hw_r["name"]
            entry["text_file"] = f"study-briefing\\catalog\\text\\{hw_r['id']}.txt"
            if hw_body:
                entry["questions_text"] = hw_body[:char_cap]
                entry["related_lectures"] = match_lectures_to_hw(hw_body + " " + (sum_raw or ""), all_lectures or [])
        if sum_raw:
            entry["summary_md"] = clean_syllabus(sum_raw)[:2500]
        asg.append(entry)
    quizzes = []
    for q in mo.get("quizzes", []) or []:
        op, cl = parse_iso(q.get("opens")), parse_iso(q.get("closes"))
        state = "not_open_yet" if op and op > now else ("closed" if cl and cl < now else "open")
        quizzes.append({
            "name": q["name"], "opens": q.get("opens"), "closes": q.get("closes"), "closes_text": due_text(q.get("closes")),
            "state": state, "attempts_done": q.get("attempts"), "best": q.get("best"), "max": q.get("max"),
            "not_counted_note": "the quiz intro says it is not counted" if re.search(r"אינו נספר|ללא ציון", q.get("intro") or "") else None,
        })
    upcoming = []
    for a in asg:
        if a["due"] and parse_iso(a["due"]) >= now:
            upcoming.append({"type": "assignment", "name": a["name"], "due": a["due"], "due_text": a["due_text"],
                             "done": a["submitted"], "counts_toward_grade": a["counts_toward_grade"]})
    for q in quizzes:
        if q["closes"] and parse_iso(q["closes"]) >= now:
            upcoming.append({"type": "moodle_quiz", "name": q["name"], "due": q["closes"], "due_text": q["closes_text"],
                             "done": bool(q["attempts_done"]), "opens": q["opens"], "state": q["state"]})
    upcoming.sort(key=lambda x: x["due"])
    exams = [e for e in site["home"]["exams"] if e["course"] == key and e["date"] >= d.isoformat()]
    exams = [{"type": e["type_official"] or e["type"], "date": e["date"], "date_he": he_date(dt.date.fromisoformat(e["date"])),
              "time": e.get("time"), "days_from_episode_date": (dt.date.fromisoformat(e["date"]) - d).days,
              "room_in_data": None} for e in exams]
    grades = [g for g in site["home"]["grades"] if g["course"] == key]
    # my_submission files with a timestamp in the name (assignment export) or the file's own date
    subs = []
    for r in cat.course_files(key, year):
        if r["kind"] != "my_submission":
            continue
        m = re.search(r"_(\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", r["name"])
        when = dt.datetime(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))) if m else dt.datetime.fromtimestamp(r["mtime"])
        subs.append({"file": r["name"], "assignment_number": r.get("number"), "when": when.isoformat(timespec="minutes"),
                     "when_from": "file name stamp" if m else "file modified time", "pages": r.get("pages")})
    subs.sort(key=lambda x: x["when"], reverse=True)
    gr = c.get("grading") or {}
    return {
        "source": "site.json courses[].moodle + home.exams/grades; catalog my_submission files",
        "moodle_pulled_at": mo.get("pulled_at"),
        "assignments": asg, "moodle_quizzes": quizzes, "upcoming_in_due_order": upcoming,
        "exams_and_course_quizzes": exams, "grades_recorded": grades,
        "my_submission_files": subs[:10],
        "grading_formula": gr.get("formula_line"),
        "grading_components": gr.get("components"),
        "homework_counts_toward_grade": c.get("homework_counts"),
    }


# ------------------------------------------------------------------ prerequisite pointers

def build_prereqs(cat, key, material_text):
    p = HERE / "kb" / "courses" / f"{key}.prereqs.json"
    if not p.exists():
        return {"candidates": [], "note": "no prereqs file"}
    data = json.loads(p.read_text(encoding="utf-8"))
    concepts = {}
    for c in json.loads((HERE / "kb" / "concepts.json").read_text(encoding="utf-8")):
        concepts[c["key"]] = c
    text = norm_name(material_text).lower()
    cands, seen = [], set()
    for it in data["items"]:
        li = it.get("learned_in")
        if not li or not li.get("course") or li["course"] == key:
            continue
        c = concepts.get(it["concept"], {})
        phrases = [x for x in [c.get("name_he"), c.get("name_en")] + list(c.get("aliases") or []) if x]
        hits = [ph for ph in phrases if len(norm_name(ph)) >= 4 and norm_name(ph).lower() in text]
        note_tokens = {t for t in re.findall(r"[\u0590-\u05FFA-Za-z]{4,}", it.get("note") or "")}
        tok_hits = sum(1 for t in note_tokens if t.lower() in text)
        score = 3 * len(set(hits)) + min(tok_hits, 4) * 0.5 + (0.5 if it.get("where") in ("lecture", "recitation") else 0)
        k = (it["concept"], li["course"])
        if k in seen:
            continue
        seen.add(k)
        cands.append({
            "concept": it["concept"], "name_he": c.get("name_he"), "name_en": c.get("name_en"),
            "learned_in_course_key": li["course"], "learned_in_course": cat.course_name(li["course"]),
            "learned_in_year": li.get("year"), "learned_in_semester": li.get("semester"),
            "how_it_is_used_here": it.get("note"), "used_in": it.get("where"), "phrase_hits_in_slides": sorted(set(hits)),
            "score": round(score, 2),
        })
    cands.sort(key=lambda x: -x["score"])
    return {
        "source": f"kb\\courses\\{key}.prereqs.json + kb\\concepts.json, ranked by overlap with the newest slide text",
        "how_to_use": "Use for silent awareness AND for at least one brief backward link (per podcast_rules.md R10) connecting a concept used here to an earlier course where it was learned.",
        "candidates": cands[:15],
    }


# ------------------------------------------------------------------ previous episode

def previous_episode(key, d):
    p = PODCAST / key / "episodes.json"
    if not p.exists():
        return None
    try:
        eps = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    older = [e for e in eps if e.get("date") and e["date"] < d.isoformat()]
    if not older:
        return None
    e = max(older, key=lambda x: x["date"])
    return {"date": e["date"], "title": e.get("title"), "script": e.get("script"),
            "script_path": str(TREE / Path(e["script"])) if e.get("script") else None}


def recent_covered(key, d, limit=5):
    """Up to 5 <!-- covered: ... --> summaries from episodes older than d."""
    folder = PODCAST / key
    if not folder.is_dir():
        return []
    out = []
    files = sorted((p for p in folder.glob("*.md") if p.stem < d.isoformat()), reverse=True)
    for p in files[:limit]:
        txt = p.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"<!--\s*covered:\s*(.*?)\s*-->", txt, flags=re.S)
        out.append({"date": p.stem, "covered": squash(m.group(1)) if m else None})
    return out


# ------------------------------------------------------------------ plan position

def plan_position(syl, d):
    dated = syl["dated_plan"]
    if not dated:
        return None
    t = d.isoformat()
    return {
        "previous_rows": [r for r in dated if r["date"] < t][-3:],
        "today_row": next((r for r in dated if r["date"] == t), None),
        "next_rows": [r for r in dated if r["date"] > t][:4],
    }


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("course", nargs="?")
    ap.add_argument("date", nargs="?")
    ap.add_argument("--list-active", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--refresh", choices=["auto", "always", "never"], default="auto")
    ap.add_argument("--max-age-hours", type=float, default=18.0)
    ap.add_argument("--window-days", type=int, default=21, help="how far back announcements are kept")
    ap.add_argument("--text-chars", type=int, default=7000, help="slide text kept for the newest lecture")
    ap.add_argument("--print", action="store_true", help="print the JSON as well")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    site, rebuilt = load_site(a.refresh, a.max_age_hours)
    if a.list_active:
        print("\n".join(active_courses(site)))
        return
    if not a.course or not a.date:
        ap.error("course key and date are required")
    d = dt.date.fromisoformat(a.date)
    c = next((x for x in site["courses"] if x["key"] == a.course), None)
    if not c or not c.get("moodle_id"):
        sys.exit(f"unknown or non-Moodle course: {a.course} (active: {', '.join(active_courses(site))})")
    sem = site["semester"]
    year = sem["year"]
    cat = Catalog()

    syl_id = pick_syllabus(cat, a.course, year)
    syl = parse_syllabus(syl_id, year) if syl_id else None
    syl_slots = syl["staff_slots"] if syl else []
    groups, my_staff = my_groups(site, a.course, syl_slots)

    sched = build_schedule(site, a.course, d, syl_slots)
    mats = build_materials(cat, a.course, c["moodle_id"], year, d, a.text_chars)
    ann = build_announcements(cat, a.course, year, syl_slots, groups, d, a.window_days)
    work = build_work(site, cat, a.course, year, d, all_lectures=mats["all_lectures"])
    newest_text = " ".join(v.get("text_head", "") for v in mats["newest_lectures"] + mats["upcoming_lectures_already_published"])
    full_newest = ""
    for v in mats["newest_lectures"]:
        raw = read_text(v["catalog_id"]) if v.get("catalog_id") else None
        if raw:
            full_newest += " " + " ".join(t for _, t in pages_of(raw))
    prereqs = build_prereqs(cat, a.course, full_newest or newest_text)

    warnings = []
    if site_age_hours(site) > a.max_age_hours:
        warnings.append(f"site.json is {site_age_hours(site):.0f} hours old")
    if not syl:
        warnings.append("no syllabus with a week plan found in the catalog for this offering")
    elif not syl["dated_plan"] and not syl["numbered_plan"]:
        warnings.append("the syllabus has no dated or numbered week plan; topics are only given in order (see course_description)")
    if not mats["newest_lectures"]:
        warnings.append("no lecture slides of the current offering found")
    if any(not r["text_available"] for r in mats["recitations"]):
        warnings.append("some recitation files have no extracted text (titles only)")
    if not sched["today_slots"]:
        warnings.append(f"no lesson of this course on {he_date(d)} according to the timetable")

    out = {
        "meta": {
            "course_key": a.course, "course_name": c["name"], "course_name_en": c.get("name_en"),
            "lecturer": c.get("lecturer"), "moodle_id": c["moodle_id"], "course_number": c.get("number"),
            "offering": f"{year} סמסטר {sem['semester']}",
            "episode_date": d.isoformat(), "episode_date_he": he_date(d), "weekday": DAYS[day_index(d)],
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "site_json_generated": site.get("generated"), "site_json_rebuilt_now": rebuilt,
            "timetable_pulled": site["home"]["schedule"].get("pulled"), "moodle_pulled": (c.get("moodle") or {}).get("pulled_at"),
            "my_groups": groups, "my_staff": my_staff,
            "episodes_folder": str(PODCAST / a.course),
            "rules": [
                "dates, hours, rooms, homework status: only from this file",
                "current-offering facts only from this offering's own sources (this file)",
                "say that you do not know when a field is empty",
            ],
        },
        "warnings": warnings,
        "schedule": sched,
        "syllabus": None if not syl else {
            "file_id": syl["file_id"], "title_line": syl["title_line"], "plan_position": plan_position(syl, d),
            "dated_plan": syl["dated_plan"], "numbered_plan": syl["numbered_plan"], "plan_notes": syl["plan_notes"],
            "course_description": syl["course_description"], "staff_section": syl["staff_section"],
            "staff_slots": syl["staff_slots"], "grading_section": syl["grading_section"],
        },
        "materials": mats,
        "work": work,
        "announcements": ann,
        "prerequisite_pointers": prereqs,
        "previous_episode": previous_episode(a.course, d),
        "recent_covered": recent_covered(a.course, d),
        "site_hint": "the course's multiple-choice questions are on that course's page on the study site",
    }
    out_path = Path(a.out) if a.out else PODCAST / "context" / a.course / f"{d.isoformat()}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out_path}  ({out_path.stat().st_size // 1024} KB)  warnings: {len(warnings)}")
    for w in warnings:
        print("  warning:", w)
    if a.print:
        print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
