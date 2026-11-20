"""Downloads all of Liran's course material from BGU Moodle through the official mobile-app API.

    python moodle_pull.py --plan            list what is there and how big it is; downloads nothing
    python moodle_pull.py                   download everything
    python moodle_pull.py --course 68512    only this course (can repeat)
    python moodle_pull.py --dest "G:\\My Drive\\לימודים"   put קורסים\\ there instead of next to study-briefing

The username and password are typed at the keyboard and stay in memory, as in moodle_check.py.
No token is written to disk or shown on screen.

Files land in <study folder>\\קורסים\\<year>\\<semester>\\<moodle id> <course name>\\, under the
course's own Moodle sections. Assignments go to עבודות\\ (instructions, my submission, feedback,
and a סיכום.md with the due date and grade), quiz reviews to בחנים\\, the grade report to ציונים\\.
Each course also gets _course.md (the course page as text) and _moodle.json (what the API returned).
Video and audio are not downloaded; they are listed in _moodle.json for the transcription step.
Every file is also listed in catalog\\moodle_files.jsonl.

Re-running skips files already on disk with the same size and modification time, so the script
can be stopped (Ctrl+C) and started again.
"""
import argparse
import collections
import concurrent.futures
import getpass
import hashlib
import html
import json
import os
import pathlib
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import socket

import paths

socket.setdefaulttimeout(90)  # nothing may wait on the network forever

ORIGIN = "https://moodle.bgu.ac.il"
BASE = ORIGIN + "/moodle"
SERVICE = "moodle_mobile_app"
APP_UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/130.0 Mobile Safari/537.36 MoodleMobile 4.5.0 (45000)")

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
COURSES = ROOT / "קורסים"
CATALOG = HERE / "catalog"
MANIFEST = CATALOG / "moodle_files.jsonl"
# STUDY_MOODLE_TOKEN: the server keeps the token in ~/study/secrets/moodle.json
SAVED_TOKEN = pathlib.Path(os.environ.get("STUDY_MOODLE_TOKEN") or
                           pathlib.Path(os.environ.get("LOCALAPPDATA", str(pathlib.Path.home()))) / "study-briefing" / "moodle.json")

SKIP = {51776: "הקלטות של שבוע ההתארגנות", 60163: "הדרכת קלסטר HPC", 51696: "לא נלמד"}
FILED_URL, FILED_STAGING = {}, {}  # from catalogiled.jsonl: where organize.py put each Moodle file
WORKERS = 4
MAX_FILE = 300 * 2**20
MAX_PATH = 240
MEDIA_EXT = re.compile(r"\.(mp4|m4v|mov|mkv|avi|webm|wmv|mp3|m4a|wav|aac|ogg)$", re.I)
VIDEO_URL = re.compile(r"zoom\.us/rec|youtu\.?be|vimeo|blocks/video|viewvideo|panopto|kaltura|\.mp4", re.I)
RECORDING_MODS = {"videostream", "zoom", "kalvidres", "panoptosubmission"}
ITEMID = re.compile(r"/pluginfile\.php/\d+/[a-z_]+/[a-z_]+/(\d+)/")
SEM_DIR = {"א": "סמסטר א", "ב": "סמסטר ב", "קיץ": "קיץ"}
SEM_OF = {"א": "א", "ב": "ב", "ג": "קיץ", "1": "א", "2": "ב", "3": "קיץ", "a": "א", "b": "ב"}
SEM_PATTERNS = [
    (r"סמסטר\s*([אבג])", lambda g: SEM_OF[g]),
    (r"סמ\s*([123])(?!\d)", lambda g: SEM_OF[g]),
    (r"\bsemester\s*([ab])\b", lambda g: SEM_OF[g.lower()]),
    (r"\bS([12])\b", lambda g: SEM_OF[g]),
    (r"\bfall\b", lambda g: "א"),
    (r"\bspring\b", lambda g: "ב"),
    (r"\bsummer\b", lambda g: "קיץ"),
]
TERM_TOKENS = re.compile(
    r"סמסטר\s*[אבג]['׳]?|סמ\s*[123](?!\d)|\bSemester\s*[AB]\b|\bS[12]\b|תשפ[\"״]?[א-ת]['׳]?"
    r"|(?<!\d)20\d\d[123](?!\d)|\(\s*(?:Fall|Spring|Summer)?[^()]*\d{2,4}\s*\)", re.I)
ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f‎‏‪-‮]')
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}

lines = []
secrets = []


# ---------- small helpers ----------

def mask(value):
    value = str(value)
    for secret in secrets:
        if secret:  # an empty password must not hide every character
            value = value.replace(secret, "<hidden>")
    return re.sub(r"\b((?:ws)?token|key|sesskey)=[^&\"'\s<>]+", r"\1=<hidden>", value)


def say(value=""):
    value = mask(value)
    lines.append(value)
    print(value, flush=True)


def day(ts):
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else "-"


def text(value):
    """HTML from Moodle to plain text."""
    if not value:
        return ""
    value = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", str(value))
    value = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>", "\n", value)
    value = re.sub(r"(?i)<li[^>]*>", "• ", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value).replace("\xa0", " ")
    rows = (re.sub(r"[ \t]+", " ", row).strip() for row in value.splitlines())
    return "\n".join(row for row in rows if row)


def shorten(name, limit, keep_ext=False):
    if len(name) <= limit:
        return name
    if keep_ext:
        stem, dot, ext = name.rpartition(".")
        if dot and stem and 0 < len(ext) <= 8:
            return stem[:max(1, limit - len(ext) - 1)].rstrip(" .") + "." + ext
    return name[:limit].rstrip(" .")


def safe(name, limit=60, keep_ext=False):
    """A Windows-safe file or folder name."""
    name = html.unescape(str(name or "")).replace('"', "״")
    name = ILLEGAL.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .") or "_"
    if name.split(".")[0].upper() in RESERVED:
        name = "_" + name
    return shorten(name, limit, keep_ext)


def fit(base, parts):
    """base/parts, with the file name trimmed until the path is short enough for Windows.

    Only the file name is trimmed, so every file of a section or folder lands in the same folder.
    A path that is still too long is written through longp().
    """
    parts = list(parts)
    over = len(paths.portable(base.joinpath(*parts))) - MAX_PATH  # measured as on Windows: same names on the server
    if over > 0:
        parts[-1] = shorten(parts[-1], max(16, len(parts[-1]) - over), keep_ext=True)
    return base.joinpath(*parts)


def longp(path):
    path = str(path)
    if os.name == "nt" and len(path) > 250 and not path.startswith("\\\\?\\"):
        return "\\\\?\\" + path
    return path


def mine(url, own_id):
    """True when an assignment file belongs to my own submission or grade.

    The item id in the file URL is the submission id (my files) or the grade id (feedback).
    A file of another item is someone else's, and is not downloaded.
    """
    hit = ITEMID.search(url or "")
    return hit is None or (own_id is not None and int(hit.group(1)) == int(own_id))


def shown(path, base):
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def flatten(prefix, value, out):
    if isinstance(value, dict):
        for key, item in value.items():
            flatten(f"{prefix}[{key}]", item, out)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            flatten(f"{prefix}[{index}]", item, out)
    else:
        out[prefix] = value


def post_json(url, data):
    request = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), headers={"User-Agent": APP_UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def ws(token, function, **params):
    data = {"wstoken": token, "wsfunction": function, "moodlewsrestformat": "json"}
    for key, value in params.items():
        flatten(key, value, data)
    result = post_json(BASE + "/webservice/rest/server.php", data)
    if isinstance(result, dict) and result.get("exception"):
        raise RuntimeError(f"{result.get('errorcode')}: {result.get('message')}")
    return result


# ---------- where a course goes ----------

def term(course):
    """(academic year like '2025-26', semester 'א' / 'ב' / 'קיץ') from the course name, else its start date."""
    name = text(course.get("fullname"))
    when = time.localtime(course.get("startdate") or time.time())
    year, month = when.tm_year, when.tm_mon
    code = re.search(r"(?<!\d)(20\d\d)([123])(?!\d)", name)  # BGU code: 20262 = year ending 2026, semester 2
    if code:
        end = int(code.group(1))
        return f"{end - 1}-{end % 100:02d}", SEM_OF[code.group(2)]
    sem = None
    for pattern, pick in SEM_PATTERNS:
        hit = re.search(pattern, name, re.I)
        if hit:
            sem = pick(hit.group(1) if hit.groups() else "")
            break
    if sem is None:
        sem = "א" if month >= 8 else ("ב" if month <= 5 else "קיץ")
    first = year if (month >= 7 if sem == "א" else month >= 9) else year - 1
    return f"{first}-{(first + 1) % 100:02d}", sem


def course_dir_name(course):
    name = TERM_TOKENS.sub(" ", text(course.get("fullname")) or course.get("shortname") or "")
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"[\s\-–.,]+$", "", name).strip()
    return safe(f"{course['id']} {name}", 50)


# ---------- downloading one file ----------

def fetch(job, token):
    path, rec = longp(job["path"]), job["rec"]
    if os.path.exists(path):
        stat = os.stat(path)
        if stat.st_size == job["size"] and (not job["mtime"] or int(stat.st_mtime) == job["mtime"]):
            rec["status"] = "exists"
            return rec
    url = job["url"] + ("&" if "?" in job["url"] else "?") + urllib.parse.urlencode({"token": token})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part = path + ".part"
    error = ""
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": APP_UA})
            with urllib.request.urlopen(request, timeout=120) as response:
                kind = response.headers.get("Content-Type", "")
                expected = job["mime"] or ""
                head = b""
                if ("json" in kind or "text/html" in kind) and not any(t in expected for t in ("json", "html")):
                    # Book chapters and pages are real HTML; only an error or login page is a failure.
                    head = response.read(4096)
                    sample = head.decode("utf-8", "replace")
                    if any(sign in sample for sign in ('"errorcode"', 'id="login', "loginform", 'class="errormessage')):
                        raise RuntimeError("Moodle answered with an error page: " + sample[:300])
                digest = hashlib.sha256(head)
                started = time.time()
                with open(part, "wb") as out:
                    out.write(head)
                    for block in iter(lambda: response.read(1 << 20), b""):
                        out.write(block)
                        digest.update(block)
                        if time.time() - started > 600:  # a connection that trickles forever
                            raise TimeoutError("download took more than 10 minutes")
            os.replace(part, path)
            rec["sha256"] = digest.hexdigest()
            if job["mtime"]:
                os.utime(path, (job["mtime"], job["mtime"]))
            got = os.path.getsize(path)
            rec["status"] = "downloaded" if not job["size"] or got == job["size"] else f"downloaded, size {got} not {job['size']}"
            return rec
        except Exception as exc:
            error = mask(f"{type(exc).__name__}: {exc}")
            if isinstance(exc, RuntimeError) or (isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 404)):
                break
            time.sleep(2 * (attempt + 1))
    try:
        os.remove(part)
    except OSError:
        pass
    rec["status"] = "error: " + error
    return rec


# ---------- one course ----------

class Pull:
    """Everything gathered for one course: download jobs, a record per file, and the course page as text."""

    def __init__(self, course, plan):
        self.course, self.plan = course, plan
        self.year, self.sem = term(course)
        self.dir = COURSES / self.year / SEM_DIR[self.sem] / course_dir_name(course)
        self.jobs, self.records, self.md = [], [], []
        self.taken, self.by_url = {}, {}
        self.not_mine = 0  # assignment files of other students that the API listed; never downloaded
        self.meta = {
            "course": {k: course.get(k) for k in ("id", "fullname", "shortname", "idnumber", "startdate", "enddate")},
            "term": {"year": self.year, "semester": self.sem},
            "pulled_at": int(time.time()),
            "final_grade": None, "sections": [], "recordings": [], "links": [],
            "assignments": [], "quizzes": [], "grades": None, "errors": [],
        }

    def record(self, **fields):
        rec = {"course": self.course["id"], "year": self.year, "semester": self.sem, "section": "", "module_id": None,
               "modname": None, "module": None, "kind": None, "filename": None, "url": None, "size": 0, "mtime": 0,
               "mimetype": "", "path": None, "status": "planned"}
        rec.update(fields)
        self.records.append(rec)
        return rec

    def rel(self, path):
        """Path inside קורסים\\. Where קורסים\\ itself is is written to catalog\\moodle_root.txt."""
        return paths.portable_rel(path.relative_to(COURSES))  # the catalog keeps '\\' paths on Linux too

    def add(self, parts, f, kind, section="", module=None):
        """Queue one Moodle file for download. Returns its local path, or None when it is not downloaded."""
        url = f.get("fileurl")
        if not url:
            return None
        module = module or {}
        name = f.get("filename") or ""
        size, mime = int(f.get("filesize") or 0), f.get("mimetype") or ""
        rec = self.record(section=section, module_id=module.get("id"), modname=module.get("modname"),
                          module=text(module.get("name")), kind=kind, filename=name, url=mask(url), size=size,
                          mtime=int(f.get("timemodified") or 0), mimetype=mime)
        if mime.startswith(("video/", "audio/")) or MEDIA_EXT.search(name):
            rec["status"] = "skipped_media"
            self.meta["recordings"].append({"section": section, "name": rec["module"] or name, "file": name,
                                            "size": size, "url": rec["url"]})
            return None
        if size > MAX_FILE:
            rec["status"] = "skipped_large"
            return None
        if url in FILED_URL and os.path.exists(longp(FILED_URL[url])):  # already sorted into the study tree
            rec["status"], rec["filed"] = "filed", paths.portable(FILED_URL[url])
            return pathlib.Path(FILED_URL[url])
        if url in self.by_url:  # the same file reached twice, e.g. personal and team submission
            rec["status"], rec["path"] = "duplicate", self.rel(self.by_url[url])
            return self.by_url[url]
        path = base = fit(self.dir, parts)
        number = 1
        while str(path).lower() in self.taken:
            number += 1
            path = base.with_name(f"{base.stem} ({number}){base.suffix}")
        self.taken[str(path).lower()] = url
        self.by_url[url] = path
        rec["path"] = self.rel(path)
        self.jobs.append({"url": url, "path": path, "size": size, "mtime": rec["mtime"], "mime": mime, "rec": rec})
        return path

    def write(self, parts, content, kind):
        """A small text file made here (summary, quiz review, grades), not downloaded."""
        path = fit(self.dir, parts)
        content = mask(content)
        rel = self.rel(path)
        rec = self.record(kind=kind, filename=path.name, size=len(content.encode("utf-8")), mtime=int(time.time()),
                          mimetype="text/html" if path.suffix == ".html" else "text/markdown", path=rel,
                          status="planned" if self.plan else "written")
        if rel in FILED_STAGING:  # sorted into the study tree earlier: keep that copy current instead
            path = pathlib.Path(FILED_STAGING[rel])
            rec["filed"] = paths.portable(path)
        if not self.plan:
            os.makedirs(longp(path.parent), exist_ok=True)
            with open(longp(path), "w", encoding="utf-8") as out:
                out.write(content)

    # --- course page ---

    def contents(self, token):
        for section in ws(token, "core_course_get_contents", courseid=self.course["id"]):
            number = int(section.get("section") or 0)
            name = text(section.get("name")) or f"יחידה {number}"
            summary = text(section.get("summary"))
            entry = {"section": number, "name": name, "summary": summary, "modules": []}
            self.meta["sections"].append(entry)
            self.md += ["", f"## {name}"] + ([summary] if summary else [])
            folder = safe(f"{number:02d} {name}", 40)
            for module in section.get("modules") or []:
                self.module(module, folder, name, entry)

    def module(self, module, folder, section, entry):
        mod, name = module.get("modname", ""), text(module.get("name"))
        description = text(module.get("description"))
        contents = module.get("contents") or []
        files = [c for c in contents if c.get("type") == "file"]
        links = [mask(c.get("fileurl", "")) for c in contents if c.get("type") == "url"]
        entry["modules"].append({"id": module.get("id"), "modname": mod, "name": name, "url": module.get("url"),
                                 "visible": module.get("uservisible", True), "description": description,
                                 "links": links, "dates": module.get("dates") or []})
        line = f"- [{mod}] {name}"
        if mod in RECORDING_MODS or any(VIDEO_URL.search(link) for link in links):
            self.meta["recordings"].append({"section": section, "name": name, "modname": mod,
                                            "url": links[0] if links else module.get("url", "")})
            line += " (הקלטה)"
        if mod == "url":
            self.meta["links"] += [{"section": section, "name": name, "url": link} for link in links]
            line += "".join(f" → {link}" for link in links)
        elif mod not in ("assign", "quiz") and files:
            if len(files) == 1:
                one = files[0]
                filename = one.get("filename") or name
                if filename.lower() == "index.html":
                    filename = name + ".html"
                path = self.add([folder, safe(filename, 80, True)], one, "course_file", section, module)
                line += f" → `{shown(path, self.dir)}`" if path else " (וידאו או קובץ גדול, לא הורד)"
            else:
                subfolder = safe(name, 40)
                for one in files:
                    sub = [safe(p, 30) for p in (one.get("filepath") or "/").strip("/").split("/") if p]
                    self.add([folder, subfolder, *sub, safe(one.get("filename"), 80, True)], one, "course_file", section, module)
                line += f" → `{folder}\\{subfolder}\\` ({len(files)} קבצים)"
        self.md.append(line)
        self.md += ["  " + row for row in description.splitlines()]

    # --- assignments ---

    def assignments(self, token, names):
        data = ws(token, "mod_assign_get_assignments", courseids=[self.course["id"]])
        found = data["courses"][0].get("assignments", []) if data.get("courses") else []
        for number, assign in enumerate(found, 1):
            title = text(assign.get("name")) or f"מטלה {number}"
            folder = ["עבודות", safe(f"{number:02d} {title}", 45)]
            module = {"id": assign.get("cmid"), "modname": "assign", "name": title}
            for one in assign.get("introattachments") or []:
                self.add(folder + ["הנחיות", safe(one.get("filename"), 80, True)], one, "assign_intro", "עבודות", module)
            info = {"id": assign.get("id"), "cmid": assign.get("cmid"), "name": title, "intro": text(assign.get("intro")),
                    "duedate": assign.get("duedate"), "cutoffdate": assign.get("cutoffdate"),
                    "allowsubmissionsfromdate": assign.get("allowsubmissionsfromdate"),
                    "gradingduedate": assign.get("gradingduedate"), "teamsubmission": assign.get("teamsubmission"),
                    "submission": None, "submission_text": "", "grade": None, "graded_at": None, "feedback": ""}
            if "mod_assign_get_submission_status" in names:
                try:
                    self.submission(ws(token, "mod_assign_get_submission_status", assignid=assign["id"]), folder, module, info)
                except Exception as exc:
                    info["status_error"] = mask(exc)
            self.meta["assignments"].append(info)
            self.write(folder + ["סיכום.md"], assignment_md(info), "assign_summary")

    def submission(self, status, folder, module, info):
        last = status.get("lastattempt") or {}
        texts = []
        not_mine = 0
        for key in ("submission", "teamsubmission"):
            sub = last.get(key) or {}
            if not sub:
                continue
            current = info["submission"] or {}
            if not current or (sub.get("status") == "submitted" and current.get("status") != "submitted"):
                info["submission"] = {"status": sub.get("status"), "timemodified": sub.get("timemodified"), "team": key == "teamsubmission"}
            for plugin in sub.get("plugins") or []:
                for area in plugin.get("fileareas") or []:
                    for one in area.get("files") or []:
                        if not mine(one.get("fileurl"), sub.get("id")):
                            not_mine += 1
                            continue
                        sub_dirs = [safe(p, 30) for p in (one.get("filepath") or "/").strip("/").split("/") if p]
                        self.add(folder + ["ההגשה שלי", *sub_dirs, safe(one.get("filename"), 80, True)],
                                 one, "my_submission", "עבודות", module)
                texts += [text(f.get("text")) for f in plugin.get("editorfields") or [] if text(f.get("text"))]
        info["submission_text"] = "\n\n".join(dict.fromkeys(texts))  # personal and team copies are often the same
        feedback = status.get("feedback") or {}
        own_grade = (feedback.get("grade") or {}).get("id")
        comments = []
        for plugin in feedback.get("plugins") or []:
            for area in plugin.get("fileareas") or []:
                for one in area.get("files") or []:
                    if not mine(one.get("fileurl"), own_grade):
                        not_mine += 1
                        continue
                    self.add(folder + ["משוב", safe(one.get("filename"), 80, True)], one, "feedback", "עבודות", module)
            comments += [text(f.get("text")) for f in plugin.get("editorfields") or [] if text(f.get("text"))]
        info["grade"] = text(feedback.get("gradefordisplay")) or None
        info["graded_at"] = feedback.get("gradeddate") or (feedback.get("grade") or {}).get("timemodified")
        info["feedback"] = "\n\n".join(comments)
        info["files_not_mine"] = not_mine
        self.not_mine += not_mine

    # --- quizzes ---

    def quizzes(self, token, names):
        found = ws(token, "mod_quiz_get_quizzes_by_courses", courseids=[self.course["id"]]).get("quizzes", [])
        for number, quiz in enumerate(found, 1):
            title = text(quiz.get("name")) or f"בוחן {number}"
            info = {"id": quiz.get("id"), "cmid": quiz.get("coursemodule"), "name": title, "intro": text(quiz.get("intro")),
                    "timeopen": quiz.get("timeopen"), "timeclose": quiz.get("timeclose"), "grade": quiz.get("grade"),
                    "sumgrades": quiz.get("sumgrades"), "attempts": []}
            self.meta["quizzes"].append(info)
            if "mod_quiz_get_user_attempts" not in names:
                continue
            try:
                attempts = ws(token, "mod_quiz_get_user_attempts", quizid=quiz["id"], status="all").get("attempts", [])
            except Exception as exc:
                info["error"] = mask(exc)
                continue
            for attempt in attempts:
                row = {k: attempt.get(k) for k in ("id", "attempt", "state", "timestart", "timefinish", "sumgrades")}
                info["attempts"].append(row)
                if self.plan or attempt.get("state") != "finished" or "mod_quiz_get_attempt_review" not in names:
                    continue
                try:
                    review = ws(token, "mod_quiz_get_attempt_review", attemptid=attempt["id"], page=-1)
                except Exception as exc:
                    row["review_error"] = mask(exc)
                    continue
                self.write(["בחנים", safe(f"{number:02d} {title}", 45), f"ניסיון {attempt.get('attempt')}.html"],
                           review_html(title, attempt, quiz, review), "quiz_review")

    # --- grades ---

    def grades(self, token, userid):
        data = ws(token, "gradereport_user_get_grade_items", courseid=self.course["id"], userid=userid)
        items = (data.get("usergrades") or [{}])[0].get("gradeitems") or []
        rows = [{"item": text(i.get("itemname")) or ("סיכום הקורס" if i.get("itemtype") == "course" else i.get("itemtype")),
                 "type": i.get("itemtype"), "module": i.get("itemmodule"), "cmid": i.get("cmid"),
                 "grade": text(i.get("gradeformatted")), "raw": i.get("graderaw"),
                 "min": i.get("grademin"), "max": i.get("grademax"), "percent": text(i.get("percentageformatted")),
                 "feedback": text(i.get("feedback")), "graded_at": i.get("gradedategraded")} for i in items]
        self.meta["grades"] = rows
        if rows:
            self.write(["ציונים", "ציונים.md"], grades_md(text(self.course.get("fullname")), rows), "grades")

    # --- the end ---

    def finish(self):
        if self.plan:
            return
        self.meta["files"] = self.records
        for name, content in (("_moodle.json", json.dumps(self.meta, ensure_ascii=False, indent=1)), ("_course.md", self.course_md())):
            target = self.dir / name
            target = pathlib.Path(FILED_STAGING.get(self.rel(target), target))
            os.makedirs(longp(target.parent), exist_ok=True)
            with open(longp(target), "w", encoding="utf-8") as out:
                out.write(mask(content))

    def course_md(self):
        course, meta = self.course, self.meta
        out = [f"# {text(course.get('fullname'))}", "",
               f"מספר במודל {course['id']} · {self.year}, {SEM_DIR[self.sem]} · {BASE}/course/view.php?id={course['id']}"]
        if meta["final_grade"]:
            out.append(f"ציון סופי במודל: {meta['final_grade'].get('grade')}")
        out += self.md
        if meta["assignments"]:
            out += ["", "## עבודות"]
            out += [f"- {a['name']}: הגשה עד {day(a.get('duedate'))}, ציון {a.get('grade') or '-'}" for a in meta["assignments"]]
        if meta["quizzes"]:
            out += ["", "## בחנים"]
            out += [f"- {q['name']}: {len(q['attempts'])} ניסיונות" for q in meta["quizzes"]]
        if meta["recordings"]:
            out += ["", "## הקלטות", "מתוך דף הקורס בלבד. ייתכן שיש עוד ברשימת ההקלטות של הקורס במודל."]
            out += [f"- {r['section']}: {r['name']}" for r in meta["recordings"]]
        if meta["errors"]:
            out += ["", "## מה נכשל"] + [f"- {e}" for e in meta["errors"]]
        return "\n".join(out) + "\n"


def assignment_md(info):
    sub = info.get("submission") or {}
    out = [f"# {info['name']}", "",
           f"- מועד הגשה: {day(info.get('duedate'))}",
           f"- סטטוס ההגשה: {sub.get('status') or '-'}{' (הגשה קבוצתית)' if sub.get('team') else ''}",
           f"- הוגש: {day(sub.get('timemodified'))}",
           f"- ציון: {info.get('grade') or '-'}",
           f"- נבדק: {day(info.get('graded_at'))}"]
    if info.get("status_error"):
        out.append(f"- לא הצלחתי לקרוא את ההגשה: {info['status_error']}")
    for title, key in (("ההנחיות", "intro"), ("מה שהגשתי כטקסט", "submission_text"), ("משוב", "feedback")):
        if info.get(key):
            out += ["", f"## {title}", info[key]]
    return "\n".join(out) + "\n"


def review_html(title, attempt, quiz, review):
    out = [f"<!doctype html><html lang='he' dir='rtl'><meta charset='utf-8'><title>{html.escape(title)}</title><body>",
           f"<h1>{html.escape(title)}, ניסיון {attempt.get('attempt')}</h1>",
           f"<p>ציון: {review.get('grade')} מתוך {quiz.get('grade')}. הוגש: {day(attempt.get('timefinish'))}</p>"]
    for question in review.get("questions") or []:
        out.append(f"<section><h2>שאלה {question.get('slot')}: {question.get('mark') or '-'} מתוך "
                   f"{question.get('maxmark') or '-'} ({html.escape(str(question.get('status') or ''))})</h2>"
                   f"{question.get('html') or ''}</section>")
    return "\n".join(out) + "\n</body></html>\n"


def grades_md(course_name, rows):
    def cell(value):
        return str(value if value not in (None, "") else "-").replace("|", "/").replace("\n", " ")
    out = [f"# ציונים: {course_name}", "", f"מתוך המודל, {day(time.time())}.", "",
           "| פריט | ציון | טווח | אחוז | משוב |", "|---|---|---|---|---|"]
    out += [f"| {cell(r['item'])} | {cell(r['grade'])} | {cell(r['min'])}–{cell(r['max'])} | {cell(r['percent'])} | {cell(r['feedback'])} |"
            for r in rows]
    return "\n".join(out) + "\n"


def save_manifest(course_id, records):
    rows = collections.OrderedDict()
    if MANIFEST.exists():
        for row in MANIFEST.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(row)
            except ValueError:
                continue
            rows.setdefault(item.get("course"), []).append(item)
    known = {(r.get("path"), r.get("size")): r["sha256"] for r in rows.get(course_id, []) if r.get("sha256")}
    for rec in records:  # a file skipped as already there keeps the hash from the run that downloaded it
        if not rec.get("sha256") and (rec.get("path"), rec.get("size")) in known:
            rec["sha256"] = known[(rec["path"], rec["size"])]
    rows[course_id] = records
    CATALOG.mkdir(parents=True, exist_ok=True)
    temp = MANIFEST.with_suffix(".tmp")
    with open(temp, "w", encoding="utf-8") as out:
        for items in rows.values():
            for item in items:
                out.write(mask(json.dumps(item, ensure_ascii=False)) + "\n")
    os.replace(temp, MANIFEST)


# ---------- main ----------

def run(args):
    say(f"BGU Moodle pull{' (plan only, nothing is downloaded)' if args.plan else ''}, {day(time.time())}")
    login = {}
    if args.saved_token and SAVED_TOKEN.exists():  # saved by recordings_pull.py, outside OneDrive
        login = json.loads(SAVED_TOKEN.read_text(encoding="utf-8"))
        secrets.extend(s for s in (login.get("token"), login.get("privatetoken")) if s)
        try:
            ws(login["token"], "core_webservice_get_site_info")
            say("Logged in with the saved token")
        except Exception as exc:
            say(f"The saved token no longer works ({exc})")
            login = {}
    for attempt in range(0 if login else 3):  # a typo, or a Hebrew keyboard layout, should not end the run
        username = input("Username (English keyboard, without @post.bgu.ac.il): ").strip()
        password = getpass.getpass("Password (hidden while typing): ")
        if password:
            secrets.append(password)
        login = post_json(BASE + "/login/token.php", {"username": username, "password": password, "service": SERVICE})
        if "token" in login:
            break
        say(f"login FAILED: {login.get('errorcode')} - {login.get('error')}. Try again ({2 - attempt} left).")
    if "token" not in login:
        return
    token = login["token"]
    secrets.extend(s for s in (token, login.get("privatetoken")) if s)

    filed = CATALOG / "filed.jsonl"
    if filed.exists():
        for row in map(json.loads, filed.read_text(encoding="utf-8").splitlines()):
            dest = paths.local(row["dest"])  # stored as a Windows path; the server maps it to its own tree
            for url in row.get("urls", []):
                FILED_URL[url] = dest
            for rel in row.get("staging", []):
                FILED_STAGING[paths.portable_rel(paths.rel(rel))] = dest
        say(f"{len(FILED_URL)} Moodle files are already sorted into the study tree; they are not downloaded again")
    info = ws(token, "core_webservice_get_site_info")
    names = {f["name"] for f in info.get("functions", [])}
    userid = info["userid"]
    if not info.get("downloadfiles"):
        say("This Moodle does not allow file downloads through the app API. Stopping.")
        return

    courses = sorted(ws(token, "core_enrol_get_users_courses", userid=userid), key=lambda c: c.get("startdate") or 0)
    if args.course:
        missing = set(args.course) - {c["id"] for c in courses}
        if missing:
            say(f"not among my courses: {', '.join(map(str, sorted(missing)))}")
        courses = [c for c in courses if c["id"] in args.course]

    finals = {}
    if "gradereport_overview_get_course_grades" in names:
        try:
            finals = {g["courseid"]: g for g in ws(token, "gradereport_overview_get_course_grades").get("grades", [])}
        except Exception as exc:
            say(f"final grades: {exc}")
    say(f"{len(courses)} courses, {len(finals)} with a final grade in the overview. Files go to {COURSES}")
    say()

    totals = collections.Counter()
    for index, course in enumerate(courses, 1):
        label = f"[{index}/{len(courses)}] {course['id']} {text(course.get('fullname'))}"
        if course["id"] in SKIP:
            say(f"{label}: skipped ({SKIP[course['id']]})")
            continue
        pull = Pull(course, args.plan)
        pull.meta["final_grade"] = finals.get(course["id"])
        steps = [("contents", lambda: pull.contents(token))]
        if "mod_assign_get_assignments" in names:
            steps.append(("assignments", lambda: pull.assignments(token, names)))
        if "mod_quiz_get_quizzes_by_courses" in names:
            steps.append(("quizzes", lambda: pull.quizzes(token, names)))
        if "gradereport_user_get_grade_items" in names:
            steps.append(("grades", lambda: pull.grades(token, userid)))
        for name, action in steps:
            try:
                action()
            except Exception as exc:  # one failed step must not stop the course
                pull.meta["errors"].append(mask(f"{name}: {type(exc).__name__}: {exc}"))

        size = sum(j["size"] for j in pull.jobs)
        assigns = pull.meta["assignments"]
        mine = sum(r["kind"] == "my_submission" and r["status"] != "duplicate" for r in pull.records)
        feedback = sum(r["kind"] == "feedback" and r["status"] != "duplicate" for r in pull.records)
        attempts = sum(a.get("state") == "finished" for q in pull.meta["quizzes"] for a in q["attempts"])
        grades = pull.meta["grades"]
        graded = "error" if grades is None else sum(r["raw"] is not None for r in grades)
        place = f"{pull.year} {SEM_DIR[pull.sem]}"
        totals.update(courses=1, files=len(pull.jobs), bytes=size, recordings=len(pull.meta["recordings"]),
                      assignments=len(assigns), mine=mine, attempts=attempts, not_mine=pull.not_mine)
        others = f", {pull.not_mine} files of other students skipped" if pull.not_mine else ""

        if args.plan:
            there = sum(os.path.exists(longp(j["path"])) and os.path.getsize(longp(j["path"])) == j["size"] for j in pull.jobs)
            say(f"{label} ({place}): {len(pull.jobs)} files, {size / 2**20:.0f} MB ({there} already on disk), "
                f"{len(assigns)} assignments ({mine} files I submitted, {feedback} feedback files), "
                f"{len(pull.meta['quizzes'])} quizzes ({attempts} finished attempts), "
                f"{len(pull.meta['recordings'])} recordings, grade items with a grade: {graded}{others}")
        else:
            say(f"{label} ({place}): {len(pull.jobs)} files, {size / 2**20:.0f} MB{others}")
            with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
                for done, _ in enumerate(pool.map(lambda job: fetch(job, token), pull.jobs), 1):
                    if done % 20 == 0 or done == len(pull.jobs):
                        print(f"   {done}/{len(pull.jobs)}", end="\r", flush=True)
            pull.finish()
            save_manifest(course["id"], pull.records)
            status = collections.Counter(r["status"].split(":")[0].split(",")[0] for r in pull.records)
            totals.update(downloaded=status["downloaded"], errors=status["error"])
            say(f"   {status['downloaded']} new, {status['exists']} already there, {status['written']} pages written, "
                f"{status['filed']} already sorted, {status['skipped_media']} video/audio not downloaded, {status['skipped_large']} too large, "
                f"{status['error']} errors")
            for rec in [r for r in pull.records if r["status"].startswith("error")][:5]:
                say(f"   ERROR {rec['filename']}: {rec['status']}")
        for error in pull.meta["errors"]:
            say(f"   step failed: {error}")

    say()
    say(f"TOTAL: {totals['courses']} courses, {totals['files']} files, {totals['bytes'] / 2**30:.2f} GB, "
        f"{totals['assignments']} assignments ({totals['mine']} files I submitted), "
        f"{totals['attempts']} finished quiz attempts, {totals['recordings']} recordings (not downloaded), "
        f"{totals['not_mine']} assignment files of other students skipped")
    if not args.plan:
        say(f"downloaded now: {totals['downloaded']}, errors: {totals['errors']}. File list: {MANIFEST}")


def main():
    parser = argparse.ArgumentParser(description="Download BGU Moodle course material through the app API.")
    parser.add_argument("--plan", action="store_true", help="list what is there and how big it is; download nothing")
    parser.add_argument("--course", type=int, action="append", default=[], help="Moodle course id; can repeat")
    parser.add_argument("--dest", help="folder that will hold קורסים\\ (default: the study folder)")
    parser.add_argument("--saved-token", action="store_true", help="use the token recordings_pull.py saved; no password")
    args = parser.parse_args()
    report = CATALOG / ("moodle_plan.txt" if args.plan else "moodle_refresh_log.txt" if args.saved_token else "moodle_pull_log.txt")
    global COURSES
    if args.dest:
        dest = pathlib.Path(args.dest)
        if not dest.is_dir():
            print(f"The folder {dest} does not exist. Nothing was done.")
            input("Press Enter to close this window")
            return
        COURSES = dest / "קורסים"
    if not args.plan:
        CATALOG.mkdir(parents=True, exist_ok=True)
        (CATALOG / "moodle_root.txt").write_text(paths.portable(COURSES) + "\n", encoding="utf-8")
    try:
        run(args)
    except KeyboardInterrupt:
        say("STOPPED (Ctrl+C). Run again to continue; finished files are skipped.")
    except Exception as exc:
        say(f"STOPPED: {type(exc).__name__}: {exc}")
    finally:
        CATALOG.mkdir(parents=True, exist_ok=True)
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nReport saved to {report}")
        try:
            input("Press Enter to close this window")
        except EOFError:
            pass


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
