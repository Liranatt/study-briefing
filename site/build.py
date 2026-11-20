"""Liran's study site: gathers the study tree into one JSON object and renders a single-page app.

    python -X utf8 build.py --today 2026-11-17 [--root R] [--out site\\out] [--files-base /] [--inline-concepts]

Reads (never writes) the study root R:
    R\\<year>\\<semester>\\<course>\\מידע על הקורס\\_moodle_<id>.json   the Moodle course page of each offering
    R\\<year>\\<semester>\\<course>\\הודעות ופורומים\\*.md              the forums (may not exist yet)
    R\\study-briefing\\catalog\\{courses.json, files.jsonl}
    R\\study-briefing\\kb\\{grades,grading,exams,map,concepts}.json and kb\\courses\\<key>.json (+ exam guides)
Writes out\\index.html (template.html with the data inline, so it opens from file:// too), out\\data\\site.json and
the knowledge map as out\\data\\concepts.js (loaded on demand, from file:// too) and out\\data\\concepts.json
(inline in index.html only with --inline-concepts; --external-concepts is still accepted). File links in the
knowledge map are --files-base + the file's path under R, percent-encoded, + '#page=N' for a PDF; the server is
expected to serve R under that prefix.

The data-gathering functions only take paths and return plain dicts, so the server can run the same code
on another root. Rendering is the last step (render_html). Python 3 standard library only.

Rule: facts about the current semester come only from that offering's own sources (its Moodle id, its grading
entry, its exams). Earlier offerings of the same course appear only as labelled history ("past").
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from difflib import SequenceMatcher
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template.html"
DATA_SLOT = '<script type="application/json" id="data">null</script>'
CONCEPTS_SLOT = '<script type="application/json" id="concepts">null</script>'
SEM_DIR = {"א": "סמסטר א", "ב": "סמסטר ב", "קיץ": "קיץ"}
WINDOW_DAYS = 14            # "recent" on the home tab: news, new files, grades
MOODLE_BASE = "https://moodle.bgu.ac.il/moodle"

# Current courses Liran keeps the material of without being registered. The grade sheet agrees: it has no
# 2026-27 row for these.
NOT_ENROLLED = {"dl-theory": "לא רשום, חומר בלבד"}
# Exam-guide sections that are built on earlier offerings (shown with a "past attempts" badge).
GUIDE_PAST_SECTIONS = ("נקודות התורפה",)
# Moodle file kinds that are course material (not my submissions, grade dumps or generated summaries).
MATERIAL_KINDS = {"course_file", "assign_intro"}

try:  # Israel time for Moodle's unix timestamps; tzdata may be missing on a bare server
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Jerusalem")
except Exception:  # noqa: BLE001
    TZ = None


# ---------- small helpers ----------

def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _last_sunday(y: int, m: int) -> date:
    d = (date(y, m + 1, 1) if m < 12 else date(y + 1, 1, 1)) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def local_dt(ts) -> datetime | None:
    """A Moodle unix timestamp as a naive Israel-time datetime."""
    if not ts:
        return None
    utc = datetime.fromtimestamp(int(ts), timezone.utc)
    if TZ is not None:
        return utc.astimezone(TZ).replace(tzinfo=None)
    # Israel's rule since 2013: summer time from the Friday before the last Sunday of March, 02:00,
    # to the last Sunday of October, 02:00.
    naive = utc.replace(tzinfo=None)
    start = datetime.combine(_last_sunday(naive.year, 3) - timedelta(days=2), time(0, 0))
    end = datetime.combine(_last_sunday(naive.year, 10) - timedelta(days=1), time(23, 0))
    return naive + timedelta(hours=3 if start <= naive < end else 2)


def iso_min(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M") if dt else None


def days_until(dt: datetime | date | None, today: date) -> int | None:
    if dt is None:
        return None
    return ((dt.date() if isinstance(dt, datetime) else dt) - today).days


def digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def norm_name(s) -> str:
    """For matching names across sources: no quotes, geresh, punctuation or spaces."""
    return re.sub(r"[\s'\"׳״`.,:()\-–]", "", str(s or ""))


def semester_of(today: date) -> tuple[str, str]:
    """BGU's semester on a date: א from October to February, ב from March to July, קיץ in August-September."""
    y = today.year
    if today.month >= 10:
        return f"{y}-{(y + 1) % 100:02d}", "א"
    if today.month <= 2:
        return f"{y - 1}-{y % 100:02d}", "א"
    if today.month <= 7:
        return f"{y - 1}-{y % 100:02d}", "ב"
    return f"{y - 1}-{y % 100:02d}", "קיץ"


def module_base(page: dict) -> str:
    for s in page.get("sections") or []:
        for m in s.get("modules") or []:
            hit = re.match(r"(https?://[^/]+/moodle)/", m.get("url") or "")
            if hit:
                return hit.group(1)
    return MOODLE_BASE


def browser_url(url: str | None, module_url: str | None = None) -> str | None:
    """A link that opens in a logged-in browser: the module page, or pluginfile without the webservice token path.
    A link whose token was masked during the pull falls back to the module page."""
    if module_url:
        return module_url
    if not url or "<hidden>" in url:
        return None
    return url.replace("/webservice/pluginfile.php/", "/pluginfile.php/").replace("?forcedownload=1", "")


# ---------- catalog ----------

def load_catalog(project: Path) -> dict:
    courses = read_json(project / "catalog" / "courses.json", [])
    files, by_name = {}, {}
    fl = project / "catalog" / "files.jsonl"
    if fl.exists():
        with fl.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                files[r.get("id")] = r
                by_name.setdefault(r.get("name"), []).append(r)
    concepts = {c["key"]: c for c in read_json(project / "kb" / "concepts.json", []) if c.get("key")}
    # a course summary may still use a key the central agent merged into another one: both lead to the merged concept
    gkey = {k: k for k in concepts}
    for k, c in concepts.items():
        for mk in c.get("merged_keys") or []:
            gkey.setdefault(mk, k)
    return {"courses": courses, "files": files, "by_name": by_name, "concepts": concepts, "gkey": gkey,
            "grades": read_json(project / "kb" / "grades.json", []),
            "grading": read_json(project / "kb" / "grading.json", []),
            "exams": read_json(project / "kb" / "exams.json", []),
            "map": read_json(project / "kb" / "map.json", {}) or {}}


def moodle_page_path(root: Path, cat: dict, moodle_id) -> Path | None:
    if not moodle_id:
        return None
    for r in cat["by_name"].get(f"_moodle_{moodle_id}.json", []):
        p = root / r["path"]
        if p.exists():
            return p
    return None


def offering_dir(root: Path, cat: dict, course: dict, off: dict) -> Path | None:
    """The offering's folder in the study tree: next to its Moodle page, else where its catalogued files are."""
    p = moodle_page_path(root, cat, off.get("moodle_id"))
    if p:
        return p.parent.parent
    for r in cat["files"].values():
        if r.get("course") == course["key"] and r.get("year") == off.get("year") and r.get("semester") == off.get("semester"):
            parts = Path(r["path"]).parts
            if len(parts) >= 3:
                return root / parts[0] / parts[1] / parts[2]
    guess = root / off.get("year", "") / SEM_DIR.get(off.get("semester"), "") / course["name_he"]
    return guess if guess.exists() else None


def file_semester(cat: dict, file_id) -> str | None:
    r = cat["files"].get(file_id)
    return f"{r['year']} {r['semester']}" if r and r.get("year") and r.get("semester") else None


# ---------- forums ----------

POST_HEAD = re.compile(r"^\*\*(.+?)\*\*, (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}|-)\s*$")
ATTACH = re.compile(r"^- קובץ מצורף: `(.+)`\s*$")
FORUM_TYPE = re.compile(r"^פורום מסוג (\S+)")
ANNOUNCE_NAME = re.compile(r"לוח הודעות|^הודעות|announcement|^news", re.I)


def parse_forum(path: Path) -> dict:
    """One forum markdown (forums_pull.py): '# name', 'פורום מסוג <type>', then '## discussion' and
    '**author**, dd.mm.yyyy hh:mm' post headers, each followed by its text and attachment lines."""
    forum = {"name": path.stem, "type": None, "discussions": []}
    disc = post = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if disc is None:
            if line.startswith("# "):
                forum["name"] = line[2:].strip()
                continue
            m = FORUM_TYPE.match(line)
            if m and forum["type"] is None:
                forum["type"] = m.group(1)
                continue
        if line.startswith("## "):
            disc = {"title": line[3:].strip(), "posts": []}
            forum["discussions"].append(disc)
            post = None
            continue
        if disc is None:
            continue  # the forum's own intro
        m = POST_HEAD.match(line)
        if m:
            when = None if m.group(2) == "-" else datetime.strptime(m.group(2), "%d.%m.%Y %H:%M")
            post = {"author": m.group(1).strip(), "time": when, "lines": [], "attachments": []}
            disc["posts"].append(post)
            continue
        if post is None:
            continue
        m = ATTACH.match(line)
        if m:
            post["attachments"].append(m.group(1))
        else:
            post["lines"].append(line)
    for d in forum["discussions"]:
        for p in d["posts"]:
            p["text"] = "\n".join(p.pop("lines")).strip()
            if p["text"] == "(בלי טקסט)":
                p["text"] = ""
    return forum


def read_forums(course_dir: Path | None) -> list[dict] | None:
    """Every forum of an offering, or None when its forum folder does not exist (not downloaded yet)."""
    d = course_dir / "הודעות ופורומים" if course_dir else None
    if not d or not d.is_dir():
        return None
    return [parse_forum(p) for p in sorted(d.glob("*.md"))]


def is_announcements(forum: dict) -> bool:
    return forum.get("type") == "news" or bool(ANNOUNCE_NAME.search(forum.get("name") or ""))


TITLES = re.compile(r"^(פרופ|פרופסור|אמריטוס|ד\"?ר|דר|מר|גב|prof|dr)\.?$", re.I)


def lecturer_surnames(lecturer: str | None) -> set[str]:
    """'ד"ר ס. בן דניאל' from the grade sheet -> {'בןדניאל'}: what is left after the title and the initial."""
    toks = [t for t in re.split(r"\s+", lecturer or "")
            if t and not TITLES.match(t.replace("'", "").replace("׳", "")) and not re.fullmatch(r"\S{1,2}\.", t)]
    n = norm_name("".join(toks))
    return {n} if len(n) >= 3 else set()


def staff_of(forums: list[dict], lecturer: str | None) -> tuple[set[str], set[str]]:
    """Who is staff: whoever opens a thread in an announcements forum (students cannot), plus the lecturer named
    on the grade sheet (matched by surname)."""
    staff = set()
    for f in forums:
        if is_announcements(f):
            for d in f["discussions"]:
                if d["posts"]:
                    staff.add(norm_name(d["posts"][0]["author"]))
    return staff, lecturer_surnames(lecturer)


def _is_staff(author: str, staff: set[str], surnames: set[str]) -> bool:
    n = norm_name(author)
    # the surname must end the author's name, so a student whose first name is the lecturer's surname is not staff
    return n in staff or any(n.endswith(s) and len(n) > len(s) for s in surnames)


# The filter. Each rule is a regex with what it is for; keep_post applies them in order.
THANKS = re.compile(  # thank-you notes, "+1", "me too", "good luck": nothing to act on
    r"^\W*(תודה( רבה)?( לכולם)?|תודה רבה רבה|thanks?( you)?( (a lot|so much))?|thx|\+1|גם אני|גם לי|אותה שאלה|"
    r"מצטרפ(ת)?( לשאלה)?|בהצלחה( לכולם)?|אחלה|מעולה|סבבה|אוקיי|ok)\W*$", re.I)
PERSONAL = re.compile(  # a reply about one student's own case: their submission, grade, an email or a private talk
    r"ההגשה שלך|העבודה שלך|הציון שלך|המייל שלך|במייל|בדוא\"?ל|בפרטי|באופן אישי|אישית|שלחתי לך|תשלח(י)? לי|"
    r"שלח(י)? לי|פנה אלי|פני אלי|בדקתי את ההגשה|your submission|your grade|e-?mail me|sent you|privately", re.I)
EXAM_WORD = re.compile(r"מבחן|בחינה|בחינות|בוחן|בחנים|מועד [אבג]|מועד מיוחד|exam|quiz|midterm", re.I)
EXAM_INFO = re.compile(  # with an exam word: what the exam covers, when and where, what is allowed, grades out
    r"יכלול|ייכלל|יכלל|כולל|לא יהיה|לא ייכלל|החומר|חומר ל|דף נוסחאות|דף הנוסחאות|חומר עזר|פתוח|סגור|תאריך|בשעה|"
    r"משך|חדר|אולם|פורסמ|ציונים|ערעור|מגן|will (include|cover|be)|material|formula sheet|date|room", re.I)
KEEP_RULES = [
    # a deadline was set, moved or extended
    ("מועד הגשה", re.compile(r"הארכ|הוארכ|מועד (ה)?הגשה|תאריך (ה)?הגשה|להגיש עד|ההגשה (נדחת|תידח)|דדליין|deadline|"
                             r"extension|extended|due date", re.I)),
    # something in an assignment, lecture or recitation was wrong or unclear, and was fixed or clarified for all
    # ("שלום לכולם" at a reply's start means the staff member is writing to the whole class)
    ("תיקון או הבהרה", re.compile(r"תיקון|תוקנ|תיקנ|נפלה טעות|טעות (בשאלה|בניסוח|בעבודה|במטלה|בתרגיל|בשקף|במצגת|"
                                  r"בהרצאה|בתרגול)|טעות קטנה|עודכנ|עדכון|הבהרה|להבהיר|הובהר|שימו לב|לתשומת לב|"
                                  r"^(שלום|היי|הי|בוקר טוב|ערב טוב) (לכולם|כולם|לכולכם)|erratum|typo|correct(ed|ion)|"
                                  r"clarif|please note|note that|updated", re.I)),
    # grading rules: weights, bonuses, appeals, points per question
    ("ציון", re.compile(r"ציון|ציונים|ניקוד|בונוס|ערעור|\d+\s*נק(ודות|')|משקל (ה)?(עבוד|מטל|תרגיל|בוחן|בחינ|מבחן|רכיב)|"
                        r"grading|bonus|appeal", re.I)),
    # a room, building or time change
    ("חדר או שעה", re.compile(r"החדר|בחדר|לחדר|אולם|בניין|שינוי (שעה|בשעה|מיקום|חדר)|יתקיים ב|תתקיים ב|יתקיימו ב|"
                              r"הועבר(ה)? ל|room|hall|building|moved to|reschedul", re.I)),
    # a class was cancelled
    ("ביטול", re.compile(r"בוטל|יבוטל|ביטול|לא (י|ת)תקיי|cancel", re.I)),
    # a new file, solution or recording was posted
    ("קובץ חדש", re.compile(r"פורסמ|פורסם|פרסמנו|נפרסם|הועל(ה|ו|תה)|העלינו|על(ה|ו) ל?מודל|מצ\"ב|מצ״ב|מצורפ|uploaded|"
                            r"posted|published|attached", re.I)),
]
SHORT = 40  # characters of new text below which a Q&A reply is chit-chat


def keep_post(post: dict, forum: dict, prior_lines: set[str], staff: set[str], surnames: set[str]) -> tuple[bool, str, list[str]]:
    """Is this forum post worth showing? Returns (keep, reason, tags)."""
    # 1. Students: their questions, thanks and "+1"s. Only staff posts can change anything.
    if not _is_staff(post["author"], staff, surnames):
        return False, "סטודנט", []
    text = post["text"]
    # 2. A thank-you or "good luck" with nothing else in it.
    if len(text) < 80 and THANKS.match(text):
        return False, "תודה או +1", []
    # 3. Announcements forum, by staff: always relevant.
    if is_announcements(forum):
        return True, "לוח הודעות", [t for t, rx in KEEP_RULES if rx.search(text)]
    # From here on: staff in a Q&A forum. Only the text the reply adds, not the question it quotes.
    new = "\n".join(l for l in text.splitlines() if l.strip() and l.strip() not in prior_lines).strip()
    # 4. A reply that only quotes the question.
    if len(new) < 15 and not post["attachments"]:
        return False, "ציטוט בלי תוספת", []
    # 5. An answer about one student's own case.
    if PERSONAL.search(new):
        return False, "מקרה אישי", []
    # 6. It changes or clarifies something for everyone: a deadline, a correction, exam info, grading, a room or
    #    time, a cancelled class, a new file (an attachment counts as a new file).
    tags = [t for t, rx in KEEP_RULES if rx.search(new)]
    if EXAM_WORD.search(new) and EXAM_INFO.search(new):
        tags.append("מבחן או בוחן")
    if post["attachments"] and "קובץ חדש" not in tags:
        tags.append("קובץ חדש")
    if tags:
        return True, "עדכון בפורום", tags
    # 7. Everything else: short chit-chat, or an ordinary answer to one question.
    return False, ("קצר" if len(new) < SHORT else "תשובה לשאלה"), []


def filter_forums(forums: list[dict] | None, lecturer: str | None) -> dict:
    """The posts worth showing, plus counts of what came in and what was dropped and why."""
    if forums is None:
        return {"folder": False, "items": [], "total": 0, "kept": 0, "hidden": 0, "by_reason": {}, "forums": 0}
    staff, surnames = staff_of(forums, lecturer)
    items, reasons, total = [], Counter(), 0
    for f in forums:
        for d in f["discussions"]:
            prior: set[str] = set()
            for i, p in enumerate(d["posts"]):
                total += 1
                keep, reason, tags = keep_post(p, f, prior, staff, surnames)
                reasons[reason] += 1
                if keep:
                    items.append({"forum": f["name"], "announcements": is_announcements(f), "discussion": d["title"],
                                  "author": p["author"], "time": iso_min(p["time"]), "reply": i > 0,
                                  "text": p["text"][:4000], "attachments": p["attachments"], "tags": tags})
                prior |= {l.strip() for l in p["text"].splitlines() if l.strip()}
    items.sort(key=lambda x: x["time"] or "", reverse=True)
    return {"folder": True, "forums": len(forums), "items": items, "total": total, "kept": len(items),
            "hidden": total - len(items), "by_reason": dict(reasons)}


# ---------- one offering's Moodle page ----------

def moodle_offering(page: dict, today: date) -> dict:
    """Assignments, quizzes, grades, sections, recordings and files of one Moodle offering."""
    base = module_base(page)
    mod_url, mod_by_name = {}, {}
    for s in page.get("sections") or []:
        for m in s.get("modules") or []:
            if not m.get("url") and m.get("id") and m.get("modname") not in (None, "label"):
                m = {**m, "url": f"{base}/mod/{m['modname']}/view.php?id={m['id']}"}  # folders come without a link
            mod_url[m.get("id")] = m.get("url")
            mod_by_name[(s.get("name"), m.get("name"))] = m
    grade_by_cm = {g.get("cmid"): g for g in page.get("grades") or [] if g.get("cmid")}

    assignments = []
    for a in page.get("assignments") or []:
        due, cut = local_dt(a.get("duedate")), local_dt(a.get("cutoffdate"))
        sub = a.get("submission") or {}
        g = grade_by_cm.get(a.get("cmid")) or {}
        assignments.append({
            "name": a.get("name"), "due": iso_min(due), "cutoff": iso_min(cut), "days_left": days_until(due, today),
            "opens": iso_min(local_dt(a.get("allowsubmissionsfromdate"))),
            "status": sub.get("status"), "submitted": sub.get("status") == "submitted", "team": bool(a.get("teamsubmission")),
            "grade": g.get("raw") if g.get("raw") is not None else a.get("grade"), "max": g.get("max"),
            "graded_at": iso_min(local_dt(g.get("graded_at") or a.get("graded_at"))), "feedback": a.get("feedback") or g.get("feedback") or "",
            "url": mod_url.get(a.get("cmid")) or (f"{base}/mod/assign/view.php?id={a['cmid']}" if a.get("cmid") else None)})
    assignments.sort(key=lambda x: x["due"] or "9999")

    quizzes = []
    for q in page.get("quizzes") or []:
        close = local_dt(q.get("timeclose"))
        atts = [x for x in q.get("attempts") or [] if x.get("state") == "finished"]
        g = grade_by_cm.get(q.get("cmid")) or {}
        best = g.get("raw")
        if best is None and atts and q.get("sumgrades"):
            best = round(max(x.get("sumgrades") or 0 for x in atts) / q["sumgrades"] * (q.get("grade") or q["sumgrades"]), 2)
        quizzes.append({
            "name": q.get("name"), "opens": iso_min(local_dt(q.get("timeopen"))), "closes": iso_min(close),
            "days_left": days_until(close, today), "attempts": len(atts), "best": best, "max": q.get("grade"),
            "intro": (q.get("intro") or "")[:400],
            "url": mod_url.get(q.get("cmid")) or (f"{base}/mod/quiz/view.php?id={q['cmid']}" if q.get("cmid") else None)})
    quizzes.sort(key=lambda x: x["closes"] or "9999")

    grades = [{"item": g.get("item"), "kind": g.get("module") or g.get("type"), "raw": g.get("raw"), "grade": g.get("grade"),
               "max": g.get("max"), "percent": g.get("percent"), "feedback": g.get("feedback") or "",
               "graded_at": iso_min(local_dt(g.get("graded_at")))}
              for g in page.get("grades") or [] if g.get("type") in ("mod", "manual")]
    course_total = next((g for g in page.get("grades") or [] if g.get("type") == "course"), None)

    sections = []
    for s in page.get("sections") or []:
        mods = []
        for m in s.get("modules") or []:
            mods.append({"name": m.get("name"), "type": m.get("modname"), "url": mod_url.get(m.get("id")) or None,
                         "visible": m.get("visible", True),
                         "text": (m.get("description") or "")[:1500] if m.get("modname") == "label" else (m.get("description") or "")[:300],
                         "links": [u for u in m.get("links") or [] if "<hidden>" not in u][:3]})
        sections.append({"n": s.get("section"), "name": s.get("name"), "summary": (s.get("summary") or "")[:800], "modules": mods})

    recordings, zoom = [], None
    for r in page.get("recordings") or []:
        mod = mod_by_name.get((r.get("section"), r.get("name"))) or {}
        if r.get("modname") == "zoom":  # the live-class join link, not a recording
            zoom = mod.get("url") or r.get("url")
            continue
        url = r.get("url")
        if not url or "<hidden>" in url:
            url = mod.get("url")
        recordings.append({"name": r.get("name"), "section": r.get("section"), "type": r.get("modname"), "url": url,
                           "transcribed": False})

    files = []
    for f in page.get("files") or []:
        if f.get("kind") not in MATERIAL_KINDS:
            continue
        mt = local_dt(f.get("mtime"))
        files.append({"name": f.get("module") or f.get("filename"), "filename": f.get("filename"), "section": f.get("section"),
                      "mtime": iso_min(mt), "size": f.get("size"),
                      "url": browser_url(f.get("url"), mod_url.get(f.get("module_id")))})
    files.sort(key=lambda x: x["mtime"] or "", reverse=True)

    c = page.get("course") or {}
    return {"moodle_id": c.get("id"), "fullname": c.get("fullname"), "url": f"{base}/course/view.php?id={c.get('id')}",
            "pulled_at": iso_min(local_dt(page.get("pulled_at"))), "final_grade": (page.get("final_grade") or {}).get("grade"),
            "course_total": course_total and {"raw": course_total.get("raw"), "max": course_total.get("max")},
            "zoom": zoom, "assignments": assignments, "quizzes": quizzes, "grades": grades, "sections": sections,
            "recordings": recordings, "files": files, "errors": page.get("errors") or []}


# ---------- grading, exams, grade sheet ----------

HOMEWORK = re.compile(r"תרגיל|עבוד|מטל|homework|assignment", re.I)


def grading_entry(cat: dict, key: str, off: dict) -> dict | None:
    for g in cat["grading"]:
        if g.get("course") == key and g.get("year") == off.get("year") and g.get("semester") == off.get("semester") \
                and (not off.get("moodle_id") or g.get("moodle_id") in (None, off.get("moodle_id"))):
            return g
    return None


def homework_counts(g: dict | None) -> bool | None:
    """Do assignments count in this offering's grade? None when the formula is unknown."""
    if not g or not g.get("formula_found"):
        return None
    return any((c.get("weight") or 0) > 0 for c in g.get("formula") or [] if HOMEWORK.search(c.get("component") or ""))


def grading_view(g: dict | None) -> dict | None:
    if not g:
        return None
    rec = g.get("reconcile") or {}
    comps = rec.get("components") or {}
    table_key = next((k for k in comps if str(k).startswith("טבלת מבחן נדרש")), None)
    calc = None
    weighted = [c for c in g.get("formula") or [] if (c.get("weight") or 0) > 0]
    quiz = [c for c in weighted if "בוחן" in (c.get("component") or "")]
    exam = [c for c in weighted if re.search(r"בחינה|מבחן", c.get("component") or "")]
    m = re.search(r"עובר (\d+)", g.get("formula_line") or "") or re.search(r"ציון עובר (\d+)", g.get("pass_rule") or "")
    if len(weighted) == 2 and len(quiz) == 1 and len(exam) == 1 and m:
        # final = wq·quiz + we·exam, and the exam itself must pass: the exam needed for a target is
        # max(pass, (target − wq·quiz) / we)
        calc = {"quiz_weight": quiz[0]["weight"], "exam_weight": exam[0]["weight"], "exam_min": int(m.group(1))}
    return {"formula_found": g.get("formula_found"), "formula_line": g.get("formula_line"),
            "components": [{"name": c.get("component"), "weight": c.get("weight"), "rule": c.get("rule") or ""} for c in g.get("formula") or []],
            "pass_rule": g.get("pass_rule"), "magen": g.get("magen"), "reserve_duty": g.get("reserve_duty"),
            "factors": g.get("factors"), "status": g.get("status"), "note": rec.get("note"),
            "computed_vs_official": g.get("computed_vs_official"),
            "src": [{"file": s.get("file"), "page": s.get("page"), "quote": s.get("quote")} for s in g.get("src") or []],
            "required_table": {"title": table_key, "rows": comps[table_key]} if table_key and isinstance(comps[table_key], dict) else None,
            "calc": calc}


def exams_of(cat: dict, key: str, label: str, today: date) -> list[dict]:
    out = []
    for e in cat["exams"]:
        if e.get("course") == key and e.get("semester") == label:
            d = date.fromisoformat(e["date"]) if e.get("date") else None
            out.append({"type": e.get("type"), "type_official": e.get("type_official"), "date": e.get("date"),
                        "days_left": days_until(d, today), "status": e.get("status"), "exam_grade": e.get("exam_grade"),
                        "sheet_grade": e.get("sheet_grade"), "final_grade": e.get("final_grade"), "notes": e.get("notes") or "",
                        "written_exam": e.get("written_exam")})
    out.sort(key=lambda x: x["date"] or "")
    return out


def moodle_number(root: Path, cat: dict, moodle_id) -> str | None:
    """The course number in the Moodle page's idnumber (its first 8 digits, e.g. 13210442 for 132.1.0442).
    Only the head of the file is read: 'course' is its first key."""
    p = moodle_page_path(root, cat, moodle_id)
    if not p:
        return None
    try:
        with p.open(encoding="utf-8") as f:
            head = f.read(2000)
    except OSError:
        return None
    m = re.search(r'"idnumber":\s*"(\d{8})', head)
    return m.group(1) if m else None


def sheet_rows(cat: dict, course: dict, off: dict, nums: set, used: set | None = None) -> list[dict]:
    """The grade-sheet rows of an offering: same semester, and the same course number, or a name that starts the
    same way ("פיסיקה 1ג" and "פיסיקה 1ג למדעי הבריאות"). A course can have two rows (the second one marked *,
    counted in another degree); the main row comes first."""
    label = f"{off.get('year')} {off.get('semester')}"
    nums = (set(nums) | {digits(off.get("number")), digits(course.get("number"))}) - {""}
    mine = norm_name(course.get("name_he"))
    out = []
    for i, r in enumerate(cat["grades"]):
        if r.get("semester_label") != label or (used is not None and i in used):
            continue
        theirs = norm_name(r.get("course_name"))
        if digits(r.get("course_number")) in nums or (mine and theirs and (theirs.startswith(mine) or mine.startswith(theirs))):
            out.append(r)
            if used is not None:
                used.add(i)
    out.sort(key=lambda r: "*" in str(r.get("final_grade") or ""))
    return out


def sheet_row(cat: dict, course: dict, off: dict, nums: set = frozenset()) -> dict | None:
    rows = sheet_rows(cat, course, off, nums)
    return rows[0] if rows else None


def fnum(v):
    """12.0 -> 12, 4.33333 -> 4.33; anything else as it is."""
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() else round(v, 2)
    return v


# ---------- the knowledge base ----------

def kb_view(cat: dict, kb: dict | None, course: dict, current_ids: set, current_label: str, multi: bool) -> dict | None:
    """The kb summary for the current offering: its units, the concepts they touch, and strengths and weaknesses
    split by which offering their sources come from."""
    if not kb:
        return None
    units = kb.get("units") or []
    if multi:  # a course taken more than once: only this offering's units
        units = [u for u in units if u.get("offering") in current_ids]
    keys = []
    for u in units:
        for k in (u.get("concepts_defined") or []) + (u.get("concepts_used") or []):
            if k not in keys:
                keys.append(k)
    by_key = {c["key"]: c for c in kb.get("concepts") or []}
    if not multi:
        keys += [k for k in by_key if k not in keys]
    concepts = []
    for k in keys:
        c = by_key.get(k) or cat["concepts"].get(k)
        if not c:
            continue
        concepts.append({"key": k, "g": cat["gkey"].get(k), "name": c.get("name_he") or k, "en": c.get("name_en") or "",
                         "kind": c.get("kind"), "level": c.get("my_level") or "unknown", "basis": c.get("level_basis")})

    def chip(k):  # a concept as {name, global key (or None when the knowledge map has no such concept)}
        c = by_key.get(k) or cat["concepts"].get(cat["gkey"].get(k, k)) or {}
        return {"n": c.get("name_he") or k, "g": cat["gkey"].get(k)}

    def scope(item):
        sems = {file_semester(cat, s.get("file")) for s in item.get("src") or []} - {None}
        if not sems:
            return "unknown"
        if sems == {current_label}:
            return "current"
        return "past" if current_label not in sems else "mixed"

    def judged(items):
        return [{"text": x.get("text"), "basis": x.get("basis"), "scope": scope(x),
                 "concepts": [chip(k) for k in x.get("concepts") or []][:6]}
                for x in items or []]

    ov = kb.get("overview") or {}
    return {"summary": ov.get("summary") or "", "goals": [g.get("text") for g in ov.get("goals") or []],
            "prerequisites": [g.get("text") for g in ov.get("prerequisites") or []],
            "units": [{"order": u.get("order"), "kind": u.get("kind"), "title": u.get("title"), "summary": u.get("summary") or "",
                       "concepts": [{"k": k, **chip(k)} for k in (u.get("concepts_defined") or [])[:20]]}
                      for u in sorted(units, key=lambda u: u.get("order") or 0)],
            "concepts": concepts, "strengths": judged(kb.get("strengths")), "weaknesses": judged(kb.get("weaknesses")),
            "levels_note": "הרמה בכל מושג נקבעה לפי כל הניסיונות בקורס, כולל הופעות קודמות." if multi else None,
            "gaps": [g.get("text") for g in kb.get("gaps") or []][:12]}


def related(cat: dict, key: str, names: dict) -> dict:
    """Course edges of the knowledge map: the courses this one builds on, and the courses that continue it."""
    edges = (cat["map"] or {}).get("course_edges") or []
    cchip = lambda k: {"n": (cat["concepts"].get(cat["gkey"].get(k, k)) or {}).get("name_he") or k, "g": cat["gkey"].get(k)}  # noqa: E731
    pick = lambda e, other: {"key": other, "name": names.get(other, other), "count": e.get("count"),  # noqa: E731
                             "examples": [cchip(x) for x in (e.get("examples") or [])[:8]]}
    return {"builds_on": sorted((pick(e, e["from"]) for e in edges if e.get("to") == key), key=lambda x: -(x["count"] or 0)),
            "used_by": sorted((pick(e, e["to"]) for e in edges if e.get("from") == key), key=lambda x: -(x["count"] or 0))}


def local_files(cat: dict, key: str, off: dict | None) -> list[dict]:
    """Catalogued files of a course without Moodle (the folder is the group)."""
    out = []
    for r in cat["files"].values():
        if r.get("course") != key:
            continue
        if off and r.get("year") and (r.get("year"), r.get("semester")) != (off.get("year"), off.get("semester")):
            continue
        parts = Path(r["path"]).parts
        folder = "\\".join(parts[3:-1]) if r.get("year") else "\\".join(parts[1:-1])
        out.append({"name": r.get("name"), "folder": folder or "", "kind": r.get("kind"), "pages": r.get("pages")})
    out.sort(key=lambda x: (x["folder"], x["name"] or ""))
    return out[:400]


def split_guide(md: str) -> list[dict]:
    """The exam guide by its '## ' sections, each marked when it is built on earlier offerings."""
    parts, cur = [], {"title": "", "md": []}
    for line in md.splitlines():
        if line.startswith("## "):
            parts.append(cur)
            cur = {"title": line[3:].strip(), "md": []}
        else:
            cur["md"].append(line)
    parts.append(cur)
    out = []
    for p in parts:
        body = "\n".join(p["md"]).strip().strip("-").strip()
        if not body and not p["title"]:
            continue
        out.append({"title": p["title"], "md": body, "past": any(s in p["title"] for s in GUIDE_PAST_SECTIONS)})
    return out


# ---------- the knowledge map ----------

def file_url_path(path: str) -> str:
    """'2022-23\\סמסטר א\\a b.pdf' -> '2022-23/%D7%A1...%20%D7%90/a%20b.pdf': each segment percent-encoded as UTF-8."""
    return "/".join(quote(seg, safe="") for seg in re.split(r"[\\/]+", path or "") if seg)


def _trim(a: list) -> list:
    while a and a[-1] is None:
        a.pop()
    return a


SEM_RANK = {"א": 1, "ב": 2, "קיץ": 3}


def _course_key(v) -> str | None:
    """learned_in is a course key in concepts.json and {course, year, semester} in the prereqs files."""
    return v.get("course") if isinstance(v, dict) else v


def gather_concepts(root: Path, project: Path, cat: dict, files_base: str) -> tuple[dict, dict]:
    """The concept index for the knowledge map and the course trees, compact, and counts for the build log.

    A place (where a concept was defined) is [course, year, semester, file id, page, page_end]. A use adds
    [where, used_how, weight, learned_in, note] (from the prereqs pass: where Liran used it, and in which earlier
    course he had learned it). Trailing empties are left out. Each file is listed once: [url path, file name,
    extension]; the page builds the link as files_base + url path (+ '#page=N' for a PDF). Roots are chains of
    concept indices (the concept itself left out). course_paths: {course: [[ancestor, depth, weight, via, path]]}."""
    files: dict[str, list] = {}
    missing_ids: set[str] = set()
    missing_disk: set[str] = set()
    idx = {k: i for i, k in enumerate(cat["concepts"])}

    def place(e: dict) -> list:
        src = e.get("src") or {}
        fid = src.get("file")
        if fid and fid not in files:
            r = cat["files"].get(fid)
            if r is None:
                missing_ids.add(fid)
            else:
                p = r.get("path") or ""
                if not (root / p).exists():
                    missing_disk.add(fid)
                files[fid] = [file_url_path(p), r.get("name") or Path(p).name, (r.get("ext") or Path(p).suffix).lower().lstrip(".")]
        return _trim([e.get("course"), e.get("year"), e.get("semester"), fid, src.get("page"), src.get("page_end")])

    def use(e: dict) -> list:
        base = place(e)
        extra = [e.get("where"), e.get("used_how"), e.get("weight"), _course_key(e.get("learned_in")), e.get("note")]
        return _trim((base + [None] * 6)[:6] + extra) if any(x is not None for x in extra) else base

    def first_label(c: dict) -> str | None:
        offs = sorted((o.get("year") or "", SEM_RANK.get(o.get("semester"), 9), o.get("semester")) for o in c["offerings"])
        return f"{offs[0][0]} {offs[0][2]}" if offs else None

    concepts = []
    for k, c in cat["concepts"].items():
        d = [place(e) for e in c.get("defined_in") or []]
        fl = place(c["first_learned"]) if c.get("first_learned") else None
        concepts.append({"k": k, "he": c.get("name_he") or k, "en": c.get("name_en") or "",
                         "al": (c.get("aliases") or []) + [m for m in c.get("merged_keys") or [] if m != k],
                         "kd": c.get("kind"), "st": c.get("statement") or "",
                         "d": d, "u": [use(e) for e in c.get("used_in") or []],
                         "fl": d.index(fl) if fl in d else fl,  # usually one of the defined places: its index
                         "dep": [x for x in c.get("depends_on") or [] if x in cat["concepts"]],
                         # only known levels; the page says "לא ידוע" for the other courses
                         "lv": [_trim([x.get("course"), x.get("level"), x.get("basis")]) for x in c.get("levels") or []
                                if x.get("level") and x.get("level") != "unknown"],
                         # chains down to the roots, as indices (the first element, the concept itself, left out)
                         "rt": [[idx[x] for x in chain[1:] if x in idx] for chain in c.get("roots") or [] if len(chain) > 1],
                         "pw": c.get("prereq_weight") or 0})
    mp = cat["map"] or {}
    edges = [{"f": e.get("from"), "t": e.get("to"), "n": e.get("count"), "w": e.get("weight"), "pc": e.get("prereq_count"),
              "ex": [x for x in e.get("examples") or [] if x in cat["concepts"]]}
             for e in mp.get("course_edges") or []]
    paths = {course: [[p.get("ancestor"), p.get("depth"), p.get("weight"), p.get("via") or [], p.get("path") or []] for p in v]
             for course, v in (mp.get("course_paths") or {}).items()}
    # each course's prereqs pass: its summary, and how much of its grade the assignments are
    pre, pre_unknown = {}, []
    for f in sorted((project / "kb" / "courses").glob("*.prereqs.json")):
        j = read_json(f) or {}
        if j.get("course"):
            pre[j["course"]] = _trim([j.get("summary_he") or "", j.get("assignment_share"), j.get("assignment_share_note")])
            pre_unknown += [(j["course"], x.get("concept")) for x in j.get("items") or [] if x.get("concept") not in cat["concepts"]]
    index = {"files_base": files_base, "files": files, "concepts": concepts, "course_edges": edges, "paths": paths, "pre": pre,
             "courses": {c["key"]: [c["name_he"], 1 if c.get("not_a_course") else 0, first_label(c)] for c in cat["courses"]}}

    def when(_course, year, sem):
        return (year or "", SEM_RANK.get(sem, 9))
    first_of = {c["key"]: min(when(c["key"], o.get("year"), o.get("semester")) for o in c["offerings"])
                for c in cat["courses"] if c["offerings"]}
    uses = [(c["k"], u) for c in concepts for u in c["u"] if len(u) > 6]
    stats = {"concepts": len(concepts), "concept_edges": sum(len(c["dep"]) for c in concepts), "course_edges": len(edges),
             "with_file_link": sum(1 for c in concepts if any(len(p) > 3 and p[3] for p in c["d"] + c["u"])),
             "files": len(files), "missing_file_ids": sorted(missing_ids), "missing_on_disk": sorted(missing_disk),
             "no_first_learned": sum(1 for c in concepts if c["fl"] is None),
             "dangling_deps": sum(1 for c in cat["concepts"].values() for d in c.get("depends_on") or [] if d not in cat["concepts"]),
             "only_not_a_course": sum(1 for c in concepts if c["d"] + c["u"]
                                     and all((index["courses"].get(p[0]) or ["", 0])[1] for p in c["d"] + c["u"])),
             "uses": len(uses), "uses_weight3": sum(1 for _, u in uses if len(u) > 8 and u[8] == 3),
             "uses_without_learned_in": sum(1 for _, u in uses if len(u) <= 9 or not u[9]),
             "learned_after_use": [f"{k}: {u[0]} {u[1]} {u[2]} < {u[9]}" for k, u in uses
                                   if len(u) > 9 and u[9] in first_of and first_of[u[9]] > when(u[0], u[1], u[2])],
             "prereq_items_unknown_concept": [f"{c}: {k}" for c, k in pre_unknown],
             "roots_chains": sum(len(c["rt"]) for c in concepts), "with_roots": sum(1 for c in concepts if c["rt"]),
             "course_paths": sum(len(v) for v in paths.values()),
             "deepest_path": max((p[1] for v in paths.values() for p in v), default=0),
             "path_courses_unknown": sorted({x for v in paths.values() for p in v for x in p[4] if x not in index["courses"]})}
    try:
        from topics import build_topics
        index["topics"] = build_topics(index["concepts"], index["courses"])
    except Exception as exc:  # the concept browser falls back to one group per course
        print(f"  topics skipped: {type(exc).__name__}: {exc}")
        index["topics"] = []
    return index, stats


# ---------- a current course ----------

def gather_course(root: Path, project: Path, cat: dict, course: dict, cur: tuple[str, str], today: date) -> dict:
    year, sem = cur
    label = f"{year} {sem}"
    key = course["key"]
    offs = [o for o in course["offerings"] if o.get("year") == year and o.get("semester") == sem]
    main = next((o for o in offs if o.get("kind") != "reinforcement"), offs[0])
    current_ids = {o.get("moodle_id") for o in offs}
    names = {c["key"]: c["name_he"] for c in cat["courses"]}
    kb = read_json(project / "kb" / "courses" / f"{key}.json")
    cdir = offering_dir(root, cat, course, main)
    page_path = moodle_page_path(root, cat, main.get("moodle_id"))
    page = read_json(page_path) if page_path else None
    moodle = moodle_offering(page, today) if page else None
    g = grading_entry(cat, key, main)
    sheet = sheet_row(cat, course, main, {moodle_number(root, cat, main.get("moodle_id"))} - {None})
    hw = homework_counts(g)
    exams = exams_of(cat, key, label, today)
    no_exam = any(e.get("written_exam") is False for e in exams)
    exams = [e for e in exams if e.get("written_exam") is not False]
    forums = filter_forums(read_forums(cdir), (sheet or {}).get("lecturer"))
    if moodle:
        for a in moodle["assignments"]:
            a["counts"] = hw
    enrolled = key not in NOT_ENROLLED

    # what is done and what is left, from this offering only
    done, left = [], []
    if moodle:
        for a in moodle["assignments"]:
            optional = a["counts"] is False
            if a["submitted"]:
                done.append({"text": f"הגשת את {a['name']}", "when": a["due"], "note": "ממתין לציון" if a["grade"] is None else f"ציון {fnum(a['grade'])}"})
            elif a["days_left"] is None or a["days_left"] >= 0:
                left.append({"text": f"{a['name']}", "when": a["due"], "days_left": a["days_left"],
                             "note": "רשות, לא נספר בציון" if optional else "עוד לא הוגש"})
            else:
                (done if optional else left).append({"text": f"{a['name']}: לא הוגש", "when": a["due"],
                                                     "note": "רשות, לא נספר בציון" if optional else "המועד עבר"})
        for q in moodle["quizzes"]:
            if q["attempts"]:
                done.append({"text": f"פתרת את {q['name']}", "when": q["closes"],
                             "note": f"ציון {fnum(q['best'])}/{fnum(q['max'])}" if q["best"] is not None else "בלי ציון עדיין"})
            elif q["days_left"] is None or q["days_left"] >= 0:
                left.append({"text": q["name"], "when": q["closes"], "days_left": q["days_left"], "note": "עוד לא נפתר"})
            else:
                left.append({"text": f"{q['name']}: לא נפתר", "when": q["closes"], "note": "נסגר"})
    for e in exams:
        if e["status"] == "עבר":
            done.append({"text": e["type_official"] or e["type"], "when": e["date"], "note": f"ציון {fnum(e['exam_grade'])}" if e["exam_grade"] is not None else ""})
        else:
            left.append({"text": e["type_official"] or e["type"], "when": e["date"], "days_left": e["days_left"], "note": "בחינה"})
    left.sort(key=lambda x: x.get("when") or "9999")

    multi = len([o for o in course["offerings"] if o.get("kind") != "reinforcement"]) > 1
    view = {
        "key": key, "name": course["name_he"], "name_en": ((kb or {}).get("course") or {}).get("name_en"),
        "short": short_name(course["name_he"]), "enrolled": enrolled, "enrollment": NOT_ENROLLED.get(key),
        "year": year, "semester": sem, "moodle_id": main.get("moodle_id"), "number": main.get("number") or course.get("number"),
        "lecturer": (sheet or {}).get("lecturer"), "credits": (sheet or {}).get("credits"),
        "sheet_grade": (sheet or {}).get("final_grade"), "moodle": moodle,
        "grading": grading_view(g), "homework_counts": hw, "exams": exams, "no_exam": no_exam, "done": done, "left": left,
        "forum": forums, "kb": kb_view(cat, kb, course, current_ids, label, multi),
        "related": related(cat, key, names),
        "local_files": [] if moodle else local_files(cat, key, main if key != "research" else None),
        "is_research": key == "research",
        "missing": [],
    }
    if not page:
        view["missing"].append("אין דף מודל לקורס הזה" if not main.get("moodle_id") else "דף המודל לא נמצא בקטלוג")
    if not forums["folder"] and main.get("moodle_id"):
        view["missing"].append("הפורומים של הקורס עוד לא ירדו")
    if not g:
        view["missing"].append("אין נוסחת ציון להופעה הזו")
    guide = project / "kb" / "courses" / f"{key}_exam_guide.md"
    if guide.exists():
        view["exam_guide"] = split_guide(guide.read_text(encoding="utf-8"))
    if multi:
        view["past"] = past_offerings(root, cat, course, kb, cur, today)
    return view


def short_name(name: str) -> str:
    if len(name) <= 20:
        return name
    words, out = name.split(), ""
    for w in words:
        if len(out) + len(w) + 1 > 18:
            break
        out = f"{out} {w}".strip()
    return out + "…"


def past_offerings(root: Path, cat: dict, course: dict, kb: dict | None, cur: tuple[str, str], today: date) -> dict:
    """Earlier offerings of a course taken again: history only, never applied to the current semester."""
    key = course["key"]
    rows, seen = [], set()
    for o in course["offerings"]:
        if (o.get("year"), o.get("semester")) == cur:
            continue
        lbl = f"{o['year']} {o['semester']}"
        if o.get("kind") == "reinforcement":
            continue
        if lbl in seen:
            continue
        seen.add(lbl)
        sheet = sheet_row(cat, course, o, {moodle_number(root, cat, o.get("moodle_id"))} - {None})
        g = grading_entry(cat, key, o)
        ids ={x.get("moodle_id") for x in course["offerings"] if (x.get("year"), x.get("semester")) == (o["year"], o["semester"])}
        rows.append({
            "label": lbl, "moodle_ids": sorted(i for i in ids if i), "lecturer": (sheet or {}).get("lecturer"),
            "final_grade": (sheet or {}).get("final_grade"), "moed_grades": (sheet or {}).get("moed_grades"), "note": (sheet or {}).get("note"),
            "formula_line": (g or {}).get("formula_line"), "status": (g or {}).get("status"),
            "computed_vs_official": (g or {}).get("computed_vs_official"),
            "exams": exams_of(cat, key, lbl, today),
            "timeline": [{"date": t.get("date"), "event": t.get("event"), "score": t.get("score"), "max": t.get("max"), "note": t.get("note") or ""}
                         for t in ((kb or {}).get("progression") or {}).get("timeline") or [] if t.get("offering") in ids],
        })
    rows.sort(key=lambda r: r["label"])
    return {"offerings": rows, "summary": ((kb or {}).get("progression") or {}).get("summary") or ""}


# ---------- the archive ----------

def kb_highlights(cat: dict, kb: dict | None) -> dict | None:
    if not kb:
        return None
    ov = kb.get("overview") or {}
    lv = Counter(c.get("my_level") for c in kb.get("concepts") or [])
    weak = [{"n": c.get("name_he") or c.get("key"), "g": cat["gkey"].get(c.get("key"))}
            for c in kb.get("concepts") or [] if c.get("my_level") == "weak"]
    return {"summary": ov.get("summary") or "", "weak_concepts": weak[:14],
            "strengths": [x.get("text") for x in kb.get("strengths") or []][:3],
            "weaknesses": [x.get("text") for x in kb.get("weaknesses") or []][:3],
            "progression": ((kb.get("progression") or {}).get("summary") or ""),
            "units": len(kb.get("units") or []), "concepts": len(kb.get("concepts") or []),
            "levels": {k: lv.get(k, 0) for k in ("strong", "ok", "weak")}}


def gather_archive(root: Path, project: Path, cat: dict, cur: tuple[str, str], today: date) -> dict:
    """Every past offering by year and semester, with the grade sheet, the exams and the kb highlights."""
    used: set[int] = set()
    groups: dict[str, list] = {}
    kbs: dict[str, dict | None] = {}
    rel: dict[str, dict] = {}
    names = {c["key"]: c["name_he"] for c in cat["courses"]}
    for c in cat["courses"]:
        if c.get("not_a_course"):
            continue
        offs = [o for o in c["offerings"] if (o.get("year"), o.get("semester")) != cur]
        seen = set()
        for o in offs:
            lbl = f"{o['year']} {o['semester']}"
            if o.get("kind") == "reinforcement" or lbl in seen:
                continue
            seen.add(lbl)
            nums = {moodle_number(root, cat, x.get("moodle_id")) for x in c["offerings"]
                    if (x.get("year"), x.get("semester")) == (o["year"], o["semester"])} - {None}
            rows = sheet_rows(cat, c, o, nums, used)
            sheet = rows[0] if rows else None
            g = grading_entry(cat, c["key"], o)
            if c["key"] not in kbs:
                kbs[c["key"]] = kb_highlights(cat, read_json(project / "kb" / "courses" / f"{c['key']}.json"))
                rel[c["key"]] = related(cat, c["key"], names)
            extra = [x.get("moodle_id") for x in c["offerings"] if (x.get("year"), x.get("semester")) == (o["year"], o["semester"])
                     and x.get("kind") == "reinforcement"]
            groups.setdefault(lbl, []).append({
                "key": c["key"], "name": c["name_he"], "moodle_id": o.get("moodle_id"), "reinforcement": [x for x in extra if x],
                "final_grade": (sheet or {}).get("final_grade"), "moed_grades": (sheet or {}).get("moed_grades"),
                "note": (sheet or {}).get("note"), "credits": (sheet or {}).get("credits"), "lecturer": (sheet or {}).get("lecturer"),
                "in_sheet": sheet is not None, "sheet_name": (sheet or {}).get("course_name"),
                "other_rows": [{"final_grade": r.get("final_grade"), "note": r.get("note"), "name": r.get("course_name")} for r in rows[1:]],
                "formula_line": (g or {}).get("formula_line"),
                "computed_vs_official": (g or {}).get("computed_vs_official"),
                "exams": exams_of(cat, c["key"], lbl, today), "taken": len([x for x in c["offerings"] if x.get("kind") != "reinforcement"])})
    cur_label = f"{cur[0]} {cur[1]}"
    sheet_only = [{"name": r.get("course_name"), "label": r.get("semester_label"), "final_grade": r.get("final_grade"),
                   "note": r.get("note"), "credits": r.get("credits"), "number": r.get("course_number")}
                  for i, r in enumerate(cat["grades"]) if i not in used and r.get("semester_label") != cur_label]
    order = sorted(groups, key=lambda s: (s.split()[0], {"א": 1, "ב": 2, "קיץ": 3}.get(s.split()[-1], 9)), reverse=True)
    return {"semesters": [{"label": s, "courses": sorted(groups[s], key=lambda x: x["name"])} for s in order],
            "sheet_only": sheet_only, "kb": kbs, "related": rel}


def exam_files_of(cat: dict, files_base: str) -> dict:
    """course key -> exam booklets (his own), exam forms and solutions, each with the link the file server serves."""
    out: dict = {}
    names = {"my_exam_booklet": "המחברת שלי", "exam": "טופס המבחן", "exam_solution": "פתרון המבחן"}
    for r in cat["files"].values():
        if r.get("kind") not in names or not r.get("course"):
            continue
        out.setdefault(r["course"], []).append({
            "label": names[r["kind"]], "kind": r["kind"], "year": r.get("year"), "semester": r.get("semester"), "exam": r.get("exam"),
            "name": r.get("name"), "pages": r.get("pages"), "url": files_base + file_url_path(r["path"])})
    for v in out.values():
        v.sort(key=lambda x: (str(x["year"] or ""), x["kind"] != "my_exam_booklet", x["name"] or ""), reverse=True)
    return out


FIXTURES = False  # --fixtures: use site/fixtures/* for the questions and the podcast when the real files do not exist yet


def gather_extras(root: Path, project: Path, out: Path | None, files_base: str, courses: list[dict]) -> tuple[dict, dict]:
    """Per active course: the daily questions (kb/questions/<course>.json, copied to data/questions/) and the podcast episodes
    (<root>/פודקאסט/<course>/episodes.json, embedded with the links the file server serves). Real files win; fixtures only
    with --fixtures, so test data never reaches the live site."""
    questions: dict = {}
    podcast: dict = {}
    fx = HERE / "fixtures"
    for c in courses:
        if not c.get("enrolled") or c.get("is_research"):
            continue
        key = c["key"]
        src, kind = project / "kb" / "questions" / f"{key}.json", "real"
        if not src.exists() and FIXTURES and (fx / "questions" / f"{key}.json").exists():
            src, kind = fx / "questions" / f"{key}.json", "fixture"
        q = read_json(src) if src.exists() else None
        if isinstance(q, dict) and out:
            _write(out / "data" / "questions" / f"{key}.json", json.dumps(q, ensure_ascii=False, separators=(",", ":")))
            questions[key] = {"source": kind, "sets": len(q.get("sets") or []), "bank": len(q.get("bank") or [])}
        src, kind = root / "פודקאסט" / key / "episodes.json", "real"
        if not src.exists() and FIXTURES and (fx / "podcast" / key / "episodes.json").exists():
            src, kind = fx / "podcast" / key / "episodes.json", "fixture"
        eps = read_json(src) if src.exists() else None
        if isinstance(eps, list) and eps:
            def url(path):
                if kind == "fixture":
                    return f"fixtures/podcast/{key}/" + quote(Path(str(path)).name, safe="")
                return files_base + file_url_path(path)
            podcast[key] = {"source": kind, "episodes": [{**e, "audio_url": url(e.get("audio") or ""), "script_url": url(e["script"]) if e.get("script") else None}
                                                         for e in eps if isinstance(e, dict) and e.get("audio")]}
    if out and FIXTURES and (fx / "podcast").is_dir():
        shutil.copytree(fx / "podcast", out / "fixtures" / "podcast", dirs_exist_ok=True)
    return questions, podcast


# ---------- the degree table (the archive tab) ----------

# The official totals printed on "גיליון ציונים 17.11.2026.pdf" (תקציר רשומה אקדמי, תואר ראשון במדעי המחשב). The build
# recomputes both from kb/grades.json and says whether they match; the page shows the computed ones.
OFFICIAL = {"gpa": 83.02, "credits": 122.5, "degree": "תואר ראשון במדעי המחשב", "source": "גיליון ציונים 17.11.2026"}
CS_DEPT = "232"
DEPT_ORIGIN = {"19": "קוגניציה", "201": "מתמטיקה"}


def _gnum(v) -> int | None:
    sv = str(v or "")
    if sv.startswith(("400", "600", "800", "801", "==")):
        return None
    s = sv.replace("*", "").replace("F", "").strip()
    return int(s) if s.isdigit() and 0 < int(s) <= 100 else None


def _sem_key(label) -> tuple:
    y, _, sm = str(label or "").partition(" ")
    if not re.match(r"\d{4}-\d{2}$", y):
        return ("0000", 0)  # exemptions without a semester go last
    return (y, {"א": 1, "ב": 2, "קיץ": 3}.get(sm, 0))


def _dept_code(r) -> str:
    return str(r.get("department") or "").split()[0] if r.get("department") else ""


def gather_degree(cat: dict, archive: dict, cur: tuple[str, str]) -> dict:
    """One row per course that stands, with its final grade. Rules (from Liran, 18.11.2026): no moed columns, no earlier
    failed attempts that were retaken, no duplicates; rows marked "הוכר בתואר/רכיב אחר" were recognised in this degree and
    count. Average: credit-weighted over the degree department's rows that carry a number and are not marked "לא לשקלול"
    (a failing number counts until the retake ends); credits: every passed row, including exemptions and pass-only."""
    rows = cat["grades"]
    by_num = {digits(c.get("number")): c["key"] for c in cat["courses"] if digits(c.get("number"))}
    by_name = {norm_name(c["name_he"]): c["key"] for c in cat["courses"] if not c.get("not_a_course")}
    keyof = {}
    for sem in archive["semesters"]:
        for c in sem["courses"]:
            for nm in (c["name"], c.get("sheet_name")):
                if nm:
                    keyof[(sem["label"], norm_name(nm))] = c["key"]
    other_dept: dict = {}  # course number -> the rows of another department (where a recognised course was originally taken)
    for r in rows:
        if _dept_code(r) != CS_DEPT:
            other_dept.setdefault(r.get("course_number"), []).append(r)
    recognised_nums = {r["course_number"] for r in rows if _dept_code(r) == CS_DEPT and str(r.get("final_grade")).endswith("*")}
    cur_label = f"{cur[0]} {cur[1]}"

    def key_of(r):
        return (by_num.get(digits(r.get("course_number"))) or keyof.get((r.get("semester_label"), norm_name(r.get("course_name"))))
                or by_name.get(norm_name(r.get("course_name"))))

    def origin_of(r, starred):
        org = other_dept.get(r.get("course_number")) if starred else None
        return DEPT_ORIGIN.get(_dept_code(org[0])) if org else None

    def row_of(r, kind, **extra):
        o = {"name": r.get("course_name"), "number": r.get("course_number"), "credits": r.get("credits"), "label": r.get("semester_label"),
             "kind": kind, "grade": _gnum(r.get("final_grade")), "key": key_of(r), "note": None, "recognised": False, "origin": None}
        o.update(extra)
        return o

    degree_rows, pts, graded_cr, earned = [], 0.0, 0.0, 0.0
    for r in rows:
        if _dept_code(r) != CS_DEPT:
            continue
        fg, note, cr = str(r.get("final_grade") or ""), r.get("note") or "", r.get("credits") or 0
        n = _gnum(fg)
        starred = fg.endswith("*")
        if fg.startswith("=="):
            degree_rows.append(row_of(r, "progress"))
        elif fg.startswith("800") or "לא לשקלול" in note:
            continue  # did not finish, or an old failing attempt that a later course replaced
        elif fg.startswith("600"):
            degree_rows.append(row_of(r, "exempt", note="פטור")); earned += cr
        elif fg.startswith("400"):
            degree_rows.append(row_of(r, "pass", recognised=starred, origin=origin_of(r, starred),
                                      note="פטור" if "פטור" in (r.get("course_name") or "") else None)); earned += cr
        elif n is not None:
            failing = fg.endswith("F")
            degree_rows.append(row_of(r, "fail" if failing else "graded", recognised=starred, origin=origin_of(r, starred)))
            if cr:
                pts += cr * n; graded_cr += cr
            if not failing:
                earned += cr
    progress_names = {norm_name(x["name"]) for x in degree_rows if x["kind"] == "progress"}
    for x in degree_rows:
        if x["kind"] == "fail":
            x["note"] = ("עדיין בממוצע הרשמי עד שהקורס החוזר יסתיים" if norm_name(x["name"]) in progress_names else "ציון נכשל שנשאר בממוצע")
    gpa = pts / graded_cr if graded_cr else None
    computed = {"gpa": round(gpa, 4) if gpa is not None else None, "credits_earned": earned, "credits_graded": graded_cr, "grade_points": pts}
    recon = {"official": OFFICIAL, "computed": computed,
             "gpa_match": gpa is not None and round(gpa, 2) == OFFICIAL["gpa"], "credits_match": abs(earned - OFFICIAL["credits"]) < 1e-9,
             "rule": "ממוצע משוקלל בנק״ז על שורות המחלקה למדעי המחשב עם ציון מספרי שאינן מסומנות 'לא לשקלול' (גם ציון נכשל שעדיין עומד), "
                     "בלי עובר ובלי פטורים. נק״ז מצטבר: כל שורה שעברת, כולל עובר ופטורים."}
    # earlier studies that were not recognised: one row per course, its last passed attempt
    prior: dict = {}
    for r in rows:
        if _dept_code(r) == CS_DEPT or r.get("course_number") in recognised_nums or r.get("course_number") is None:
            continue
        fg = str(r.get("final_grade") or "")
        if fg.startswith(("800", "600")):
            continue
        if _gnum(fg) is None and not (fg.startswith("400") and (r.get("credits") or 0)):
            continue
        old = prior.get(r["course_number"])
        if not old or _sem_key(r.get("semester_label")) > _sem_key(old.get("semester_label")):
            prior[r["course_number"]] = r
    prior_rows = [row_of(r, "prior", origin=DEPT_ORIGIN.get(_dept_code(r)), failed=str(r.get("final_grade") or "").endswith("F")) for r in prior.values()]
    prior_rows.sort(key=lambda x: _sem_key(x["label"]), reverse=True)
    groups: dict = {}
    for x in degree_rows:
        groups.setdefault(x["label"] or "ללא סמסטר", []).append(x)
    sems = [{"label": s, "rows": sorted(groups[s], key=lambda x: (x["kind"] != "progress", x["name"] or ""))}
            for s in sorted(groups, key=_sem_key, reverse=True)]
    by_key = {}
    for x in degree_rows + prior_rows:
        if x["key"] and x["kind"] != "fail":
            by_key[x["key"]] = {"grade": x["grade"], "kind": x["kind"], "recognised": x["recognised"], "origin": x["origin"],
                                "credits": x["credits"], "label": x["label"]}
    return {"semesters": sems, "prior": prior_rows, "recon": recon, "by_key": by_key, "current_label": cur_label}


# ---------- the home tab ----------

def gather_home(courses: list[dict], today: date) -> dict:
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    deadlines, news, files, grades, exams = [], [], [], [], []
    for c in courses:
        if not c["enrolled"]:
            continue
        m = c.get("moodle")
        ref = {"course": c["key"], "course_name": c["name"]}
        if m:
            for a in m["assignments"]:
                if a["days_left"] is not None and a["days_left"] >= 0:
                    deadlines.append({**ref, "kind": "עבודה", "name": a["name"], "due": a["due"], "days_left": a["days_left"],
                                      "status": "הוגש" if a["submitted"] else "לא הוגש", "done": a["submitted"],
                                      "counts": a["counts"], "url": a["url"]})
            for q in m["quizzes"]:
                if q["days_left"] is not None and q["days_left"] >= 0:
                    deadlines.append({**ref, "kind": "בוחן במודל", "name": q["name"], "due": q["closes"], "days_left": q["days_left"],
                                      "status": f"נפתר ({q['attempts']} ניסיונות)" if q["attempts"] else "לא נפתר",
                                      "done": bool(q["attempts"]), "counts": None, "url": q["url"]})
            for f in m["files"]:
                if (f["mtime"] or "") >= since:
                    files.append({**ref, **f})
            for g in m["grades"]:
                if g["raw"] is not None and (g["graded_at"] or "") >= since:
                    grades.append({**ref, **g})
        for x in c["forum"]["items"]:
            if (x["time"] or "") >= since:
                news.append({**ref, **x})
        for e in c["exams"]:
            if e["status"] != "עבר" and e["days_left"] is not None and e["days_left"] >= 0:
                exams.append({**ref, **e})
    deadlines.sort(key=lambda x: x["due"] or "")
    news.sort(key=lambda x: x["time"] or "", reverse=True)
    files.sort(key=lambda x: x["mtime"] or "", reverse=True)
    grades.sort(key=lambda x: x["graded_at"] or "", reverse=True)
    exams.sort(key=lambda x: x["date"] or "")
    return {"window_days": WINDOW_DAYS, "deadlines": deadlines, "news": news, "files": files, "grades": grades, "exams": exams,
            "today": [d for d in deadlines if d["days_left"] == 0]}


# ---------- portal, mail and calendar (collected on the server by portal_pull, mail_pull, calendar_pull) ----------

DAY_NAMES = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת"]
BREAK = re.compile(r"פגרת|חופשת|אין לימודים")


def _local(v) -> datetime | None:
    """Portal and mail times: '...Z' is UTC, anything else is already Israel time. Naive Israel time back."""
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo:
        d = d.astimezone(TZ) if TZ else d
    return d.replace(tzinfo=None)


def _hhmm(hours) -> str:
    h = float(hours)
    return f"{int(h):02d}:{round((h - int(h)) * 60):02d}"


def _course_of(name: str, courses: list[dict]) -> dict | None:
    """The current course a portal or mail name belongs to ('גיאומטריות' and 'גאומטריות' are the same word)."""
    n = norm_name(name).replace("יא", "א")
    best, score = None, 0.0
    for c in courses:
        cn = norm_name(c["name"]).replace("יא", "א")
        if not cn or not n:
            continue
        s = 1.0 if cn in n or (len(n) >= 8 and n in cn) else SequenceMatcher(None, n, cn).ratio()
        if s > score:
            best, score = c, s
    return best if score >= 0.75 else None


def _portal(project: Path, name: str):
    return (read_json(project / "catalog" / "portal" / name, {}) or {})


def gather_schedule(project: Path, courses: list[dict], today: date) -> dict | None:
    """The weekly timetable with rooms (portal schedule.json), today's lessons, breaks and the academic calendar."""
    sched = _portal(project, "schedule.json")
    data = sched.get("data") or {}
    sems = [s for s in (data.get("semesters") or {}).values() if isinstance(s, dict) and isinstance(s.get("data"), dict)]
    items = _portal(project, "exams_tasks.json").get("data")
    items = [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
    if not sems and not items:
        return None
    pick = next((s for s in sems if s.get("checked")), sems[0]) if sems else {"label": "", "data": {}}
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("dayOfWeek"), int) and node.get("timeStart") is not None and node.get("name"):
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(pick["data"].get("scheduleByDays"))
    dated = [(x, _local(x.get("date"))) for x in items if x.get("calenderDataType") == 6]
    lessons = []
    for l in found:
        day = (l["dayOfWeek"] - 1) % 7
        start, end = _hhmm(l["timeStart"]), _hhmm(l.get("timeEnd") or float(l["timeStart"]) + 1)
        mode = next(((x.get("description") or "").strip() for x, d in dated if d and x.get("courseName") == l["name"]
                     and (d.weekday() + 1) % 7 == day and d.strftime("%H:%M") == start), "")
        m = re.match(r"\s*\[(\d+)\]\s*(.*?),\s*(.+?)\s*$", l.get("location") or "")
        c = _course_of(l["name"], courses)
        lessons.append({"day": day, "day_name": DAY_NAMES[day], "start": start, "end": end, "name": l["name"],
                        "type": (l.get("type") or "").strip(), "location": l.get("location") or "",
                        "building": (m.group(2) if m.group(1) in m.group(2) else f"{m.group(2)} ({m.group(1)})") if m else "",
                        "room": m.group(3) if m else "",
                        "mode": mode, "course": c["key"] if c else None, "course_name": c["name"] if c else l["name"]})
    lessons.sort(key=lambda x: (x["day"], x["start"]))
    for a in lessons:  # two lessons at the same time: he can only sit in one of them
        a["clash"] = [b["course_name"] for b in lessons if b is not a and b["day"] == a["day"]
                      and b["start"] < a["end"] and a["start"] < b["end"]]
    academic, breaks, sem_end = [], {}, None
    for x in items:
        if x.get("calenderDataType") != 5:
            continue
        d = _local(x.get("date"))
        name = (x.get("name") or x.get("courseName") or "").strip()
        if not d or not name:
            continue
        note = (x.get("description") or "").strip()
        if BREAK.search(name + " " + note):
            breaks[d.date().isoformat()] = name
        if name.startswith("סיום סמסטר") and not sem_end and d.date() >= today - timedelta(days=200):
            sem_end = d.date().isoformat()
        if today <= d.date() <= today + timedelta(days=60):
            academic.append({"date": d.date().isoformat(), "name": name, "note": note if note != name else "",
                             "days_left": (d.date() - today).days, "break": bool(BREAK.search(name + " " + note))})
    academic.sort(key=lambda x: x["date"])

    def day_lessons(d: date) -> list[dict]:
        if d.isoformat() in breaks or (sem_end and d.isoformat() > sem_end):
            return []
        return [x for x in lessons if x["day"] == (d.weekday() + 1) % 7]
    nxt = None
    for i in range(1, 15):
        d = today + timedelta(days=i)
        if day_lessons(d):
            nxt = {"date": d.isoformat(), "days_left": i, "lessons": day_lessons(d)}
            break
    return {"label": pick.get("label") or "", "pulled": sched.get("pulled"), "lessons": lessons,
            "today": day_lessons(today), "today_break": breaks.get(today.isoformat()), "next": nxt,
            "academic": academic, "semester_end": sem_end}


def portal_exam_times(project: Path, courses: list[dict], exams: list[dict], today: date) -> list[dict]:
    """The portal's exams and quizzes have the hour; add it to the known exams, and add any exam they do not list."""
    items = _portal(project, "exams_tasks.json").get("data")
    out = list(exams)
    for x in items if isinstance(items, list) else []:
        if not isinstance(x, dict) or x.get("calenderDataType") not in (2, 3):
            continue
        d = _local(x.get("date"))
        if not d or d.date() < today:
            continue
        c = _course_of(x.get("courseName") or "", courses)
        if c and c.get("no_exam"):
            continue  # no written exam in this course: the portal row is the final assignment's slot
        hour = d.strftime("%H:%M") if d.hour or d.minute else None
        match = next((e for e in out if e.get("date") == d.date().isoformat()
                      and (e.get("course") == (c or {}).get("key") or norm_name(e.get("course_name")) == norm_name(x.get("courseName")))), None)
        if match:
            match["time"] = match.get("time") or hour
        else:
            out.append({"course": c["key"] if c else None, "course_name": c["name"] if c else x.get("courseName"),
                        "type": "מבחן" if x["calenderDataType"] == 3 else "בוחן", "date": d.date().isoformat(), "time": hour,
                        "days_left": (d.date() - today).days, "status": "בקרוב", "source": "פורטל"})
    out.sort(key=lambda e: (e.get("date") or "", e.get("time") or ""))
    return out


MAIL_COURSE = re.compile(r"^(?P<course>.+?)\s+סמ\s*\d+\s*:\s*(?P<rest>.*)$")
MOODLE_MAIL_KEEP = re.compile(r"באיחור|אישור הגשת|נבדק|ציון|grade", re.I)  # deadline reminders repeat the deadlines card
REPLY = re.compile(r"^\s*(re|תשובה|השב)\s*:", re.I)


FORUM_COPY = "זהו העתק של הודעה שפורסמה"


def mail_body(text: str) -> str:
    """Only what the sender wrote: a Moodle forum copy keeps the part between its dashed lines (no breadcrumb,
    link, 'על ידי ...' line or footer); a trailing 'Links:' block goes too."""
    parts = re.split(r"\n?-{20,}\n?", text or "")
    body = parts[1] if FORUM_COPY in (text or "") and len(parts) >= 3 else (text or "")
    body = re.sub(r"\n\s*Links:\s*\n-+\n.*", "", body, flags=re.S)
    body = re.split(r"\n?\s*_{10,}|\n\s*-----\s*Original Message|\n\s*From:\s.*\n\s*Sent:", body)[0]  # a reply: not the quoted mail
    body = re.sub(r"(?m)^\s*-{10,}\s*$", "", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def gather_mail(project: Path, courses: list[dict], today: date, news: list[dict] | None = None) -> dict | None:
    """Mail from catalog/mail (mail_pull.py): course announcements, Moodle notices about his own work, and replies
    to him are shown; general university mail (lists, the dean's office, announcements) is only counted.
    A forum post's mail copy is skipped when the forum news on the home tab already has it."""
    folder = project / "catalog" / "mail"
    if not folder.is_dir():
        return None
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    in_news = {norm_name(x.get("discussion")) for x in news or []}
    shown, general, seen = [], Counter(), set()
    for path in sorted(folder.glob("20*.json")):
        if path.stem < since:
            continue
        for m in read_json(path, []) or []:
            if m.get("id") in seen:
                continue
            seen.add(m.get("id"))
            sender = m.get("from") or ""
            addr = (re.search(r"<([^>]+)>", sender) or [None, sender])[1].strip().lower()
            who = re.sub(r"\s*<[^>]*>\s*", "", sender).strip().strip('"') or addr
            subject = (m.get("subject") or "").strip()
            item = {"date": m.get("date"), "from": who, "subject": subject, "text": mail_body(m.get("text") or ""),
                    "course": None, "course_name": None, "kind": None}
            hit = MAIL_COURSE.match(subject)
            c = _course_of(hit.group("course"), courses) if hit else None
            if c:
                item.update(course=c["key"], course_name=c["name"], kind="הודעה בקורס", subject=hit.group("rest") or subject)
                if FORUM_COPY in (m.get("text") or "") and norm_name(item["subject"]) in in_news:
                    general["הודעות פורום שכבר בחדשות מהפורומים"] += 1
                    continue
            elif "moodle" in addr:
                if not MOODLE_MAIL_KEEP.search(subject):
                    general["תזכורות מהמודל"] += 1
                    continue
                item["kind"] = "מהמודל"
                # which course: "232252110120271 -> מטלה -> ..." starts with the course number; or "בקורס X סמ 1"
                code = re.match(r"\s*(\d{8,})", item["text"])
                named = re.search(r"בקורס\s+(.+?)\s+סמ\s*\d", item["text"])
                c = next((c for c in courses if code and digits(c.get("number")) and code.group(1).startswith(digits(c.get("number")))), None) \
                    or (_course_of(named.group(1), courses) if named else None)
                if c:
                    item.update(course=c["key"], course_name=c["name"])
                item["text"] = re.sub(r"^\s*\d{8,}\s*->.*\n", "", item["text"]).strip()
            elif REPLY.match(subject) and "[" not in subject:
                item["kind"] = "תשובה אליך"
                c = _course_of(REPLY.sub("", subject).strip(" .…"), courses)
                if c:
                    item.update(course=c["key"], course_name=c["name"])
            else:
                general["רשימות תפוצה" if "[" in subject else "הודעות כלליות של האוניברסיטה"] += 1
                continue
            shown.append(item)
    shown.sort(key=lambda x: x["date"] or "", reverse=True)
    return {"items": shown, "general": dict(general), "window_days": WINDOW_DAYS}


def gather_calendar(project: Path, today: date) -> dict | None:
    """His own Google Calendar's next 7 days (calendar_pull.py), without the lessons the timetable already shows."""
    cal = read_json(project / "catalog" / "calendar" / "upcoming.json", None)
    if not isinstance(cal, dict):
        return None
    end = (today + timedelta(days=7)).isoformat()
    events = [e for e in cal.get("events") or [] if today.isoformat() <= (e.get("start") or "")[:10] <= end]
    return {"pulled": cal.get("pulled"), "events": events[:30]}


def has_transcripts(root: Path, cur: tuple[str, str]) -> bool:
    base = root / cur[0] / SEM_DIR.get(cur[1], cur[1])
    return any(base.glob("*/**/תמלולים/*.txt"))  # transcribe.py writes <course>\הקלטות\תמלולים\


# ---------- everything ----------

def gather(root: Path, project: Path, today: date, cur: tuple[str, str], out: Path | None = None,
           files_base: str = "/") -> tuple[dict, dict, dict]:
    """Everything the page shows: (site data, concept index, concept counts)."""
    cat = load_catalog(project)
    concepts, cstats = gather_concepts(root, project, cat, files_base)
    # not_enrolled: material kept in the tree, but Liran does not take the course (dl-theory in 2026-27)
    current = [c for c in cat["courses"] if not c.get("not_a_course") and not c.get("not_enrolled")
               and any((o.get("year"), o.get("semester")) == cur for o in c["offerings"])]
    # enrolled first, then by whether there is a Moodle page, then research and material-only courses
    rank = lambda c: (c["key"] in NOT_ENROLLED, c["key"] == "research",  # noqa: E731
                      not any(o.get("moodle_id") for o in c["offerings"] if (o.get("year"), o.get("semester")) == cur))
    courses = [gather_course(root, project, cat, c, cur, today) for c in sorted(current, key=rank)]
    home = gather_home(courses, today)
    home["schedule"] = gather_schedule(project, courses, today)
    home["exams"] = portal_exam_times(project, courses, home["exams"], today)
    home["mail"] = gather_mail(project, courses, today, home["news"])
    home["calendar"] = gather_calendar(project, today)
    try:  # dated events in staff mail (the same ones the calendar feed gets)
        sys.path.insert(0, str(project))
        import mail_events
        # not a deadline or exam the home tab already lists (from Moodle or the portal), a day either way
        have = {(d["course"], (d.get("due") or "")[:10], "deadline") for d in home["deadlines"]} | \
               {(e.get("course"), e.get("date"), "exam") for e in home["exams"]}
        near = lambda e: {(e["course"], (date.fromisoformat(e["date"]) + timedelta(days=k)).isoformat(), e["kind"]) for k in (-1, 0, 1)}  # noqa: E731
        home["mail_events"] = [e for e in mail_events.events(today=today) if not (near(e) & have)][:20]
    except Exception as exc:  # the home tab must build without it
        print(f"  mail events skipped: {type(exc).__name__}: {exc}")
        home["mail_events"] = []
    episode = None
    if out and (out / "audio" / f"{today.isoformat()}.mp3").exists():
        episode = {"audio": f"audio/{today.isoformat()}.mp3", "date": today.isoformat()}
    missing = []
    for c in courses:
        missing += [f"{c['name']}: {m}" for m in c["missing"]]
    if not has_transcripts(root, cur):
        missing.append("אין עדיין תמלולים להקלטות של הסמסטר")
    if not (home["schedule"] or {}).get("lessons"):
        missing.append("אין עדיין מערכת שעות מהפורטל, ולכן אין רשימת הרצאות להיום")
    if home["mail"] is None:
        missing.append("המייל האוניברסיטאי עוד לא מחובר")
    archive = gather_archive(root, project, cat, cur, today)
    questions, podcast = gather_extras(root, project, out, files_base, courses)
    degree = gather_degree(cat, archive, cur)
    data = {
        "generated": datetime.now(TZ).strftime("%Y-%m-%dT%H:%M") if TZ else datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "today": today.isoformat(), "semester": {"year": cur[0], "semester": cur[1], "label": f"{cur[0]} סמסטר {cur[1]}"},
        "episode": episode, "home": home, "courses": courses,
        "archive": archive, "degree": degree, "exam_files": exam_files_of(cat, files_base), "questions": questions, "podcast": podcast, "missing": missing,
        "concept_counts": {k: v for k, v in cstats.items() if not isinstance(v, list)},
        "forum_stats": {c["key"]: {k: c["forum"][k] for k in ("folder", "forums", "total", "kept", "hidden", "by_reason")} for c in courses},
    }
    return data, concepts, cstats


# ---------- rendering ----------

def _blob(obj) -> str:
    """JSON for a <script type="application/json"> block: '<' is escaped so no string can close the tag."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def concepts_js(concepts: dict) -> str:
    """data/concepts.js: the concept index as a script, so the page can load it on demand from file:// as well."""
    return "window.__CONCEPTS__ = " + json.dumps(concepts, ensure_ascii=False, separators=(",", ":")) + ";\n"


def render_html(data: dict, template: str, concepts: dict | None = None) -> str:
    """The template with the site data inline. The concept index is inline only when given (--inline-concepts);
    otherwise the page loads data/concepts.js when the knowledge map or a course tree first needs it (works from
    file:// too), and falls back to fetching data/concepts.json."""
    if DATA_SLOT not in template or CONCEPTS_SLOT not in template:
        raise RuntimeError("template.html is missing a data slot")
    html = template.replace(DATA_SLOT, f'<script type="application/json" id="data">{_blob(data)}</script>', 1)
    if concepts is not None:
        html = html.replace(CONCEPTS_SLOT, f'<script type="application/json" id="concepts">{_blob(concepts)}</script>', 1)
    return html


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)  # the web server never sees a half-written file


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--today", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--root", type=Path, default=HERE.parent.parent, help="the study root (מדעי המחשב)")
    ap.add_argument("--project", type=Path, help="default: <root>\\study-briefing")
    ap.add_argument("--out", type=Path, default=HERE / "out")
    ap.add_argument("--semester", help="e.g. 2026-27:א (default: from --today)")
    ap.add_argument("--fixtures", action="store_true", help="use site/fixtures for the daily questions and the podcast when the real files do not exist")
    ap.add_argument("--audio", type=Path, help="today's episode mp3, copied to out\\audio\\<date>.mp3")
    ap.add_argument("--files-base", default="/", help="URL prefix under which the server serves the study root (default /)")
    ap.add_argument("--inline-concepts", action="store_true",
                    help="also put the concept index inside index.html (one self-contained file, about 1 MB heavier)")
    ap.add_argument("--external-concepts", action="store_true",
                    help="kept for older scripts: the concept index is outside index.html by default now")
    args = ap.parse_args()
    global FIXTURES
    FIXTURES = args.fixtures
    today = date.fromisoformat(args.today) if args.today else date.today()
    cur = tuple(args.semester.split(":", 1)) if args.semester else semester_of(today)
    root = args.root.resolve()
    project = (args.project or root / "study-briefing").resolve()
    out = args.out.resolve()
    if args.audio:
        (out / "audio").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.audio, out / "audio" / f"{today.isoformat()}.mp3")
    files_base = args.files_base if args.files_base.endswith("/") else args.files_base + "/"
    data, concepts, cstats = gather(root, project, today, cur, out, files_base)
    _write(out / "data" / "site.json", json.dumps(data, ensure_ascii=False, indent=1))
    _write(out / "data" / "concepts.json", json.dumps(concepts, ensure_ascii=False, separators=(",", ":")))
    _write(out / "data" / "concepts.js", concepts_js(concepts))
    if (HERE / "vendor").is_dir():  # local copies of third-party scripts (KaTeX), served from the page's own origin
        shutil.copytree(HERE / "vendor", out / "vendor", dirs_exist_ok=True)
    inline = args.inline_concepts and not args.external_concepts
    _write(out / "index.html", render_html(data, TEMPLATE.read_text(encoding="utf-8"), concepts if inline else None))
    h = data["home"]
    print(f"semester {cur[0]} {cur[1]}, today {today}: {len(data['courses'])} course tabs, {len(h['deadlines'])} deadlines, "
          f"{len(h['news'])} news, {len(h['files'])} new files, {len(h['grades'])} recent grades, {len(h['exams'])} exams ahead, "
          f"{sum(len(s['courses']) for s in data['archive']['semesters'])} past courses")
    for k, s in data["forum_stats"].items():
        print(f"  forums {k}: " + (f"{s['total']} posts, {s['kept']} kept, {dict(s['by_reason'])}" if s["folder"] else "no forum folder"))
    rc = data["degree"]["recon"]
    print(f"  degree: average {rc['computed']['gpa']} (official {rc['official']['gpa']}, {'match' if rc['gpa_match'] else 'NO MATCH'}), "
          f"credits {rc['computed']['credits_earned']} (official {rc['official']['credits']}, {'match' if rc['credits_match'] else 'NO MATCH'}), "
          f"{sum(len(x['rows']) for x in data['degree']['semesters'])} rows + {len(data['degree']['prior'])} earlier-studies rows")
    print(f"  knowledge map: {cstats['concepts']} concepts, {cstats['concept_edges']} concept edges, {cstats['course_edges']} course edges, "
          f"{cstats['with_file_link']} with a file link ({cstats['files']} files), {len(cstats['missing_file_ids'])} unknown file ids, "
          f"{len(cstats['missing_on_disk'])} files missing on disk, {cstats['no_first_learned']} without first_learned, "
          f"{cstats['only_not_a_course']} only from not-a-course material")
    print(f"  deep paths: {cstats['course_paths']} course paths (deepest {cstats['deepest_path']}), {cstats['with_roots']} concepts with roots "
          f"({cstats['roots_chains']} chains), {cstats['uses']} uses ({cstats['uses_weight3']} of weight 3, "
          f"{cstats['uses_without_learned_in']} without learned_in)")
    for what in ("missing_file_ids", "missing_on_disk", "learned_after_use", "prereq_items_unknown_concept", "path_courses_unknown"):
        if cstats[what]:
            print(f"    {what} ({len(cstats[what])}): {'; '.join(cstats[what][:8])}")
    print(f"wrote {out / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
