"""One organized study tree from Moodle, Downloads, the local study folder and Google Drive.

    python organize.py plan  [--home "G:\\My Drive\\לימודים"]   write catalog\\organize_plan.jsonl and organize_summary.md; change nothing
    python organize.py apply [--home ...]                       carry out the plan, resumable

Every copy of the same content (same sha256) is one item. The best copy goes into the tree, and the
other copies go to the Recycle Bin (Drive trash for files on G:), only after the kept copy is verified
in place. catalog\\filed.jsonl records where every item went, including the Moodle URLs and staging
paths it stands for, so moodle_pull.py does not download it again.

Tree: <home>\\<year> <semester>\\<course>\\<type>\\...   for example  2025-26 סמסטר ב\\מודלים חישוביים\\שיעורים\\
"""
import argparse
import collections
import ctypes
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
import time
import zipfile

import paths as study_paths  # Linux support: stored Windows paths, trash instead of the Recycle Bin

HERE = pathlib.Path(__file__).resolve().parent
CATALOG = pathlib.Path(os.environ.get("ORGANIZE_CATALOG") or HERE / "catalog")
SOURCES = CATALOG / "sources"
LOCAL_ROOT = pathlib.Path(os.environ.get("ORGANIZE_LOCAL_ROOT") or r"C:\Users\Liran\OneDrive\שולחן העבודה\מדעי המחשב")
DEFAULT_HOME = str(LOCAL_ROOT)  # Liran: everything goes into the מדעי המחשב folder
EXCLUDE_COURSES = {"cs-puzzles"}  # enrolled in Moodle, never taken
SKIP_DIRS = {".venv", "venv", "__pycache__", "node_modules", ".ipynb_checkpoints"}  # .git and .idea move with their project
SEM_DIR = {"א": "סמסטר א", "ב": "סמסטר ב", "קיץ": "קיץ"}
TYPE_DIR = {
    "lecture": "שיעורים", "recitation": "תרגולים", "assignment": "עבודות", "my_submission": "עבודות",
    "feedback": "עבודות", "assignment_solution": "עבודות", "assign_summary": "עבודות",
    "exam": "מבחנים", "exam_solution": "מבחנים", "my_exam_booklet": "מבחנים", "quiz_review": "מבחנים",
    "my_notes": "סיכומים שלי", "summary_other": "סיכומים", "syllabus": "מידע על הקורס", "admin": "מידע על הקורס",
    "grades_records": "ציונים", "code_project": "קוד", "paper": "מאמרים", "book": "ספרים",
    "generated": "חומר שנוצר עם Claude", "other": "אחר",
}
ASSIGN_SUB = {"my_submission": "ההגשה שלי", "feedback": "משוב", "assignment_solution": "פתרון"}
MOODLE_KIND = {"assign_intro": "assignment", "my_submission": "my_submission", "feedback": "feedback",
               "assign_summary": "assign_summary", "quiz_review": "quiz_review", "grades": "grades_records"}
SOURCE_RANK = {"tree": -1, "moodle": 0, "drive": 1, "local": 2, "downloads": 3, "zip": 4}
ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------- helpers ----------

def safe(name, limit=80):
    name = ILLEGAL.sub(" ", str(name).replace('"', "״"))
    return re.sub(r"\s+", " ", name).strip(" .")[:limit] or "_"


def jsonl(path):
    path = pathlib.Path(path)
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()] if path.exists() else []


def write_jsonl(path, rows):
    tmp = pathlib.Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as w:
        for row in rows:
            w.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def longp(path):
    path = str(path)
    return "\\\\?\\" + path if os.name == "nt" and len(path) > 250 and not path.startswith("\\\\?\\") else path


def exists(path):
    return bool(path) and os.path.exists(longp(path))


def size_of(path):
    return os.path.getsize(longp(path))


def sha_file(path):
    h = hashlib.sha256()
    with open(longp(path), "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def name_key(name, size):
    """Content key from file name and size, for Moodle files that were not hashed (reading them back from Drive is slow)."""
    stem, dot, ext = str(name).lower().strip().rpartition(".")
    stem = re.sub(r"\s*\(\d+\)$", "", stem if dot else ext)
    return f"ns:{stem}.{ext if dot else ''}|{size}"


def moodle_root():
    p = CATALOG / "moodle_root.txt"
    return pathlib.Path(study_paths.local(p.read_text(encoding="utf-8").strip())) if p.exists() else None


# ---------- classification of Moodle files that no agent saw ----------

def guess_kind(rec):
    name = f"{rec.get('module') or ''} {rec.get('filename') or ''}".lower()
    section = (rec.get("section") or "").lower()
    solution = re.search(r"פתרון|פתרונות|solution|\bsol\b|_sol|sol\.", name)
    for text in (name, section):
        if re.search(r"מבחן|מבחנים|בחינה|exam|moed|מועד|midterm|בוחן|בחני|quiz", text):
            return "exam_solution" if solution else "exam"
        if re.search(r"תרגול|recitation|tutorial|\bps\s?\d|practice session|תגבור", text):
            return "recitation"
        if re.search(r"עבודה|עבודות|תרגיל בית|תרגילי בית|homework|\bhw\s?\d|assignment|מטלה|מטלות", text):
            return "assignment_solution" if solution else "assignment"
        if re.search(r"הרצא|שיעור|lecture|lesson|slides|שקפים|מצגת", text):
            return "lecture"
        if re.search(r"סילבוס|syllabus|נהלי|לוח זמנים|schedule|מידע כללי", text):
            return "syllabus"
        if re.search(r"סיכום|summary|notes|דף נוסחאות|formula", text):
            return "summary_other"
    return "other"


def number_of(text):
    hit = re.search(r"(?:עבודה|תרגיל|מטלה|hw|homework|assignment|ex|targil|lesson|lecture|שיעור|תרגול|ps)\s*[_-]?\s*(\d{1,2})",
                    text or "", re.I)
    return hit.group(1) if hit else None


# ---------- plan ----------

def load_agent():
    agent = {}
    files = sorted(SOURCES.glob("classified_*.jsonl"))
    for f in [f for f in files if "resolved" not in f.name] + [f for f in files if "resolved" in f.name]:
        override = "resolved" in f.name
        for row in jsonl(f):
            (agent.__setitem__ if override else agent.setdefault)(row["id"][:12] if not row["id"].startswith(("codedir:", "folder-")) else row["id"], row)
            for m in row.get("members") or []:
                if m.get("id"):
                    agent.setdefault(("member", row["id"], m.get("name")), m)
                    agent.setdefault(m["id"][:12], m)
    return agent


def build(home):
    courses = json.load(open(CATALOG / "courses.json", encoding="utf-8"))
    by_moodle = {o["moodle_id"]: (c, o) for c in courses for o in c["offerings"] if o.get("moodle_id")}
    by_key = {c["key"]: c for c in courses}
    agent = load_agent()
    by_path_file = SOURCES / "classified_by_path.json"
    by_path = json.load(open(by_path_file, encoding="utf-8")) if by_path_file.exists() else {}
    copies = collections.defaultdict(list)
    zips, code_units = {}, {}

    # already in the tree
    for row in jsonl(CATALOG / "filed.jsonl"):
        if row.get("sha") and exists(row["dest"]):
            copies[row["sha"]].append({"source": "tree", "path": row["dest"], "size": row.get("size", 0), "filed": row})

    # Moodle staging
    root = moodle_root()
    by_name = {}  # name+size key -> content key, so a Downloads copy finds its Moodle twin without hashing Drive
    cache_path = CATALOG / "moodle_hashes.json"
    cache = json.load(open(cache_path, encoding="utf-8")) if cache_path.exists() else {}
    for rec in jsonl(CATALOG / "moodle_files.jsonl") if root else []:
        if rec.get("course") in {o["moodle_id"] for k in EXCLUDE_COURSES for o in by_key.get(k, {}).get("offerings", [])}:
            continue
        if not rec.get("path") or rec["status"].split(":")[0].split(",")[0] not in ("downloaded", "exists", "written"):
            continue
        path = root / rec["path"]
        if not exists(path):
            continue
        st = os.stat(longp(path))
        key = f"{rec['path']}|{st.st_size}|{int(st.st_mtime)}"
        if key not in cache and rec.get("sha256") and st.st_size == rec.get("size"):
            cache[key] = rec["sha256"]
        content = cache.get(key) or name_key(rec.get("filename") or path.name, st.st_size)
        if not content.startswith("ns:"):
            by_name.setdefault(name_key(rec.get("filename") or path.name, st.st_size), content)
        copies[content].append({"source": "moodle", "path": str(path), "size": st.st_size, "moodle": rec})
    json.dump(cache, open(cache_path, "w", encoding="utf-8"))

    # Downloads, local, Drive
    for name in ("downloads", "local"):  # files that were already in Google Drive stay where they are
        for row in jsonl(SOURCES / f"inventory_{name}.jsonl"):
            if not exists(row["path"]):
                continue
            cls = agent.get(row["sha256"][:12]) or by_path.get(row["path"])  # a file still downloading when classified
            if name == "local":
                unit = next((k[len("codedir:"):] for k in agent if isinstance(k, str) and k.startswith("codedir:")
                             and row["rel"].startswith(k[len("codedir:"):] + "\\")), None)
                if unit and row["ext"] not in (".pdf", ".docx", ".pptx"):
                    code_units.setdefault(unit, agent["codedir:" + unit])
                    continue
            if row["ext"] == ".zip" and row.get("members") and cls and not cls.get("personal"):
                whole = cls.get("kind") in ("code_project", "my_submission") and cls.get("members_same", True)
                if not whole:
                    zips[row["path"]] = {"row": row, "cls": cls}
                    for m in row["members"]:
                        if m.get("sha256") and not m["name"].startswith("__MACOSX"):
                            mcls = agent.get(("member", cls["id"], m["name"])) or agent.get(m["sha256"][:12]) or (cls if cls.get("members_same") else None)
                            copies[m["sha256"]].append({"source": "zip", "zip": row["path"], "member": m["name"],
                                                        "size": m["size"], "agent": mcls})
                    continue  # the zip itself is not filed; it goes once all its members are in the tree
            nk = name_key(pathlib.PureWindowsPath(row["path"]).name, row["size"])
            content = row["sha256"] if row["sha256"] in copies else (by_name.get(nk) or (nk if nk in copies else row["sha256"]))
            copies[content].append({"source": name, "path": row["path"], "size": row["size"], "agent": cls, "rel": row["rel"]})

    plan, review, taken = [], [], {}
    for sha, group in copies.items():
        group.sort(key=lambda c: SOURCE_RANK[c["source"]])
        info = resolve(group, by_moodle, by_key)
        if info is None:
            continue
        best = group[0]
        if info.get("unsure"):
            review.append({"sha": sha, **{k: info.get(k) for k in ("course", "kind", "title")},
                           "copies": [c.get("path") or f"{c['zip']} :: {c['member']}" for c in group]})
        if best["source"] == "tree":
            dest = pathlib.Path(best["path"])
        else:
            dest = destination(home, info, best, by_key)
            base, n = dest, 1
            while str(dest).lower() in taken and taken[str(dest).lower()] != sha:
                n += 1
                dest = base.with_name(f"{base.stem} ({n}){base.suffix}")
        taken[str(dest).lower()] = sha
        previous = best.get("filed") or {}
        urls = sorted({*previous.get("urls", []), *[c["moodle"]["url"] for c in group if c["source"] == "moodle" and c["moodle"].get("url")]})
        staging = sorted({*previous.get("staging", []), *[c["moodle"]["path"] for c in group if c["source"] == "moodle"]})
        act = "extract" if best["source"] == "zip" else ("keep" if best.get("path") == str(dest) else "move")
        plan.append({"action": act, "sha": sha, "src": best.get("path"), "zip": best.get("zip"), "member": best.get("member"),
                     "dest": str(dest), "course": info["course"], "kind": info["kind"], "size": best["size"],
                     "urls": urls, "staging": staging})
        for other in group[1:]:
            if other["source"] == "zip" or other.get("path") == str(dest):
                continue
            plan.append({"action": "recycle", "sha": sha, "src": other["path"], "dest": None, "duplicate_of": str(dest),
                         "kept": best.get("path"), "kept_size": best["size"],
                         "course": info["course"], "kind": info["kind"], "size": other["size"]})
    placed = {p["sha"] for p in plan if p["action"] in ("move", "keep", "extract")}
    for path, z in zips.items():
        shas = [m["sha256"] for m in z["row"]["members"] if m.get("sha256") and not m["name"].startswith("__MACOSX")]
        if shas and all(s in placed for s in shas):
            plan.append({"action": "recycle", "sha": z["row"]["sha256"], "src": path, "dest": None, "members": shas,
                         "duplicate_of": "every file inside is filed", "course": z["cls"].get("course"), "kind": "zip",
                         "size": z["row"]["size"]})
    for unit, cls in code_units.items():
        if not cls.get("study") or cls.get("course") in EXCLUDE_COURSES:
            continue
        info = {k: cls.get(k) for k in ("course", "year", "semester", "number", "exam", "title")}
        info["kind"] = "code_project"
        src = LOCAL_ROOT / unit
        dest = destination(home, info, {"path": str(src)}, by_key)
        plan.append({"action": "move_folder", "sha": None, "src": str(src), "dest": str(dest), "course": info["course"],
                     "kind": "code_project", "size": 0})
    return plan, review


def resolve(group, by_moodle, by_key):
    """Course, offering, kind and number for one content. None = leave every copy alone."""
    tree = next((c["filed"] for c in group if c["source"] == "tree"), None)
    moodle = next((c["moodle"] for c in group if c["source"] == "moodle"), None)
    agents = [c["agent"] for c in group if c.get("agent")]
    if any(a.get("personal") for a in agents):
        return None
    cls = next((a for a in agents if a.get("study")), None)
    if moodle is None and cls is None and tree is None:
        return None
    info = {"course": None, "year": None, "semester": None, "kind": None, "number": None, "exam": None, "title": None}
    if tree:
        info.update(course=tree.get("course"), kind=tree.get("kind"))
    if cls:
        info.update({k: cls.get(k) for k in ("course", "year", "semester", "kind", "number", "exam", "title")})
    if moodle:
        found = by_moodle.get(moodle["course"])
        if found:
            course, offering = found
            info.update(course=course["key"], year=offering["year"], semester=offering["semester"])
        kind = moodle.get("kind")
        if kind in MOODLE_KIND:
            info["kind"] = MOODLE_KIND[kind]
        elif not info["kind"]:
            info["kind"] = guess_kind(moodle)
        info["moodle"] = moodle
        info["number"] = info["number"] or number_of(f"{moodle.get('module')} {moodle.get('filename')}")
    info["kind"] = info["kind"] or "other"
    if info["course"] in EXCLUDE_COURSES:
        return None
    if info["course"] and info["course"] not in by_key:
        info["course"] = None
    sure_without_course = info["kind"] in ("grades_records", "admin", "syllabus", "book", "paper")
    info["unsure"] = bool(cls and cls.get("confidence") == "low") or (info["course"] is None and not sure_without_course)
    return info


def destination(home, info, best, by_key):
    home = pathlib.Path(home)
    name = pathlib.PurePosixPath(best["member"]).name if best.get("member") else pathlib.PureWindowsPath(best["path"]).name
    kind, course_key = info["kind"], info["course"]
    moodle = info.get("moodle") or {}
    if name.lower() == "index.html" and moodle:  # a Moodle page or book chapter: name it after the page
        chapter = pathlib.PureWindowsPath(moodle.get("path") or "").parent.name
        name = f"{moodle.get('module') or 'דף'}{' - ' + chapter if chapter.isdigit() else ''}.html"
    if course_key == "research":
        return home / "מחקר" / TYPE_DIR.get(kind, "אחר") / safe(name)
    if not course_key:
        if kind in ("grades_records", "admin"):
            return home / "ציונים ומסמכים" / safe(name)
        if kind == "syllabus":
            return home / "ציונים ומסמכים" / "סילבוסים של קורסים אחרים" / safe(name)
        if kind == "book":
            return home / "ספרים" / safe(name)
        if kind == "paper":
            return home / "מחקר" / "מאמרים" / safe(name)
        return home / "לא ממוין" / safe(name)
    course = by_key[course_key]
    year = info.get("year") or course["offerings"][0]["year"]
    sem = info.get("semester") or course["offerings"][0]["semester"]
    base = home / year / SEM_DIR.get(sem, sem) / safe(course["name_he"], 60)  # <year>\<semester>\<course>
    folder = base / TYPE_DIR.get(kind, "אחר")
    moodle = info.get("moodle") or {}
    if TYPE_DIR.get(kind) == "עבודות":
        parts = pathlib.PureWindowsPath(moodle.get("path") or "").parts
        if len(parts) > 4 and parts[3] == "עבודות":
            title = re.sub(r"^\d+\s+", "", parts[4])
        else:
            title = f"עבודה {info['number']}" if info.get("number") else "ללא מספר"
        folder = folder / safe(title, 45)
        if kind in ASSIGN_SUB:
            folder = folder / ASSIGN_SUB[kind]
    elif kind in ("exam", "exam_solution"):
        exam = info.get("exam") or {}
        if exam.get("year"):
            folder = folder / safe(f"{exam['year']}" + (f" מועד {exam['moed']}" if exam.get("moed") else ""))
    elif kind == "my_exam_booklet":
        folder = folder / "המחברות שלי"
    elif kind == "quiz_review":
        folder = folder / "בחנים במודל"
    if kind == "code_project" and not best.get("member") and pathlib.Path(best["path"]).is_dir():
        return folder / safe(name)
    return folder / safe(name)


def summary(plan, review, home):
    acts = collections.Counter(p["action"] for p in plan)
    size = collections.Counter()
    for p in plan:
        size[p["action"]] += p.get("size") or 0
    names = {"move": "מועבר לעץ", "keep": "כבר במקום", "extract": "מחולץ מ-ZIP", "recycle": "כפילות, לסל המיחזור",
             "move_folder": "תיקיית קוד שעוברת"}
    out = ["# הצעת סידור", "", f"היעד: `{home}`", "", "| פעולה | קבצים | נפח |", "|---|---|---|"]
    out += [f"| {names[a]} | {acts[a]} | {size[a] / 2**20:,.0f} MB |" for a in names if acts[a]]
    per = collections.defaultdict(collections.Counter)
    for p in plan:
        if p["action"] in ("move", "keep", "extract", "move_folder"):
            rel = pathlib.PureWindowsPath(p["dest"]).relative_to(home).parts
            per[" / ".join(rel[:2]) if len(rel) > 2 else rel[0]][rel[2] if len(rel) > 3 else "(שורש)"] += 1
    out += ["", "## לפי קורס", ""] + [f"- **{c}**: " + ", ".join(f"{k} {v}" for k, v in per[c].most_common()) for c in sorted(per)]
    out += ["", f"## לבדיקה ({len(review)})", ""] + [f"- {r.get('title') or ''} ({r['kind']}, {r['course'] or '?'}): {r['copies'][0]}" for r in review]
    return "\n".join(out) + "\n"


# ---------- apply ----------

class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int), ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p)]


def recycle(paths):
    """Send files to the Recycle Bin (Drive trash on G:). Returns the paths that are gone afterwards.

    On Linux (the home server) there is no Recycle Bin: the files move to ~/study/trash/<date>/ instead.
    """
    if os.name != "nt":
        return study_paths.recycle(paths)
    gone = []
    short = [p for p in paths if len(p) < 259]
    for start in range(0, len(short), 100):
        chunk = short[start:start + 100]
        buf = ctypes.create_unicode_buffer("\0".join(chunk) + "\0\0", len("\0".join(chunk)) + 2)
        op = SHFILEOPSTRUCTW(None, 3, ctypes.cast(buf, ctypes.c_wchar_p), None, 0x40 | 0x10 | 0x04 | 0x400, 0, None, None)
        ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        gone += [p for p in chunk if not os.path.exists(p)]
    return gone


def apply(home, only=None):
    """only: a folder; then only files from that folder are moved, extracted or recycled."""
    plan = jsonl(CATALOG / "organize_plan.jsonl")
    if only:
        scopes = [str(o).lower().rstrip("\\") + "\\" for o in only]
        plan = [r for r in plan if any((r.get("src") or r.get("zip") or "").lower().startswith(sc) for sc in scopes)]
    log_path = CATALOG / "organize_done.jsonl"
    done = {r["key"] for r in jsonl(log_path)}
    filed = {r["dest"]: r for r in jsonl(CATALOG / "filed.jsonl")}
    log = open(log_path, "a", encoding="utf-8")
    counts, errors = collections.Counter(), []

    def mark(row, status):
        key = f"{row['action']}|{row.get('src')}|{row.get('member')}|{row.get('dest')}"
        log.write(json.dumps({"key": key, "status": status, "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False) + "\n")
        log.flush()
        counts[status] += 1

    def keyof(row):
        return f"{row['action']}|{row.get('src')}|{row.get('member')}|{row.get('dest')}"

    for row in plan:
        if row["action"] not in ("move", "keep", "extract", "move_folder"):
            continue
        if row["action"] in ("keep", "move", "extract") and exists(row["dest"]) and row["dest"] in filed:
            # already in the tree: still record every Moodle URL and staging path that stands for it,
            # so moodle_pull.py never downloads a copy again
            old = filed[row["dest"]]
            old["urls"] = sorted({*old.get("urls", []), *row.get("urls", [])})
            old["staging"] = sorted({*old.get("staging", []), *row.get("staging", [])})
        if keyof(row) in done:
            continue
        dest = row["dest"]
        try:
            if row["action"] == "move_folder":
                moved = 0
                for dirpath, dirnames, filenames in os.walk(row["src"]):
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                    for name in filenames:
                        src = os.path.join(dirpath, name)
                        target = os.path.join(dest, os.path.relpath(src, row["src"]))
                        os.makedirs(longp(os.path.dirname(target)), exist_ok=True)
                        shutil.move(longp(src), longp(target))
                        moved += 1
                remove_empty(row["src"])
                filed[dest] = {"sha": None, "dest": dest, "course": row["course"], "kind": row["kind"], "size": 0,
                               "folder": True, "from": [row["src"]]}
                mark(row, f"moved folder ({moved} files)")
                continue
            if row["action"] == "keep":
                pass
            elif exists(dest) and size_of(dest) == row["size"]:
                pass  # already there from an earlier run
            elif row["action"] == "extract":
                os.makedirs(longp(os.path.dirname(dest)), exist_ok=True)
                with zipfile.ZipFile(row["zip"]) as z:
                    info = next(i for i in z.infolist() if member_name(i) == row["member"])
                    data = z.read(info)
                if hashlib.sha256(data).hexdigest() != row["sha"]:
                    raise RuntimeError("extracted content does not match")
                with open(longp(dest), "wb") as out:
                    out.write(data)
            else:
                os.makedirs(longp(os.path.dirname(dest)), exist_ok=True)
                shutil.move(longp(row["src"]), longp(dest))
            if not exists(dest) or size_of(dest) != row["size"]:
                raise RuntimeError("file not in place after the move")
            old = filed.get(dest, {})
            filed[dest] = {"sha": row["sha"], "dest": dest, "course": row["course"], "kind": row["kind"], "size": row["size"],
                           "urls": sorted({*old.get("urls", []), *row.get("urls", [])}),
                           "staging": sorted({*old.get("staging", []), *row.get("staging", [])})}
            mark(row, row["action"])
        except Exception as exc:
            errors.append(f"{row['action']} {row.get('src') or row.get('zip')}: {type(exc).__name__}: {exc}")
    write_jsonl(CATALOG / "filed.jsonl", filed.values())

    # duplicates, only once a kept copy is verified: in the tree, or still at its place (a staging, Drive or local copy)
    in_tree = {r["sha"] for r in filed.values() if r.get("sha") and exists(r["dest"])}
    in_tree |= {r["sha"] for r in jsonl(CATALOG / "organize_plan.jsonl")
                if r["action"] in ("move", "keep") and r.get("src") and exists(r["src"]) and size_of(r["src"]) == r["size"]}
    batch = []
    for row in plan:
        if row["action"] != "recycle" or not exists(row["src"]):
            continue  # a file that was recycled and later downloaded again is handled again
        if row.get("members"):
            if not all(s in in_tree for s in row["members"]):
                continue
        elif row["sha"] not in in_tree:
            continue
        batch.append(row)
    gone = set(recycle([r["src"] for r in batch]))
    for row in batch:
        if row["src"] in gone:
            mark(row, "recycled")
        else:
            errors.append(f"recycle {row['src']}: still there")

    # course pages from the Moodle staging folder, and empty staging folders
    root = moodle_root()
    if root and root.exists() and not only:
        place_course_pages(root, home, filed)
        write_jsonl(CATALOG / "filed.jsonl", filed.values())
        remove_empty(str(root), keep_root=True)
    for old in ("סמסטר ו", "תואר שני", "סילבוסים"):
        if (LOCAL_ROOT / old).exists():
            remove_empty(str(LOCAL_ROOT / old))
    log.close()
    return counts, errors


def member_name(info):
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.filename.encode("cp437", "replace")
    for enc in ("utf-8", "cp1255"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return info.filename


def place_course_pages(root, home, filed):
    courses = json.load(open(CATALOG / "courses.json", encoding="utf-8"))
    by_moodle = {o["moodle_id"]: (c, o) for c in courses for o in c["offerings"] if o.get("moodle_id")}
    for page in list(root.glob("*/*/*/_course.md")) + list(root.glob("*/*/*/_moodle.json")):
        meta_id = int(page.parent.name.split(" ")[0]) if page.parent.name.split(" ")[0].isdigit() else None
        found = by_moodle.get(meta_id)
        if not found or found[0]["key"] in EXCLUDE_COURSES:
            continue
        course, offering = found
        target_dir = pathlib.Path(home) / offering["year"] / SEM_DIR[offering["semester"]] / safe(course["name_he"], 60) / "מידע על הקורס"
        name = f"דף הקורס במודל {meta_id}.md" if page.name == "_course.md" else f"_moodle_{meta_id}.json"
        dest = target_dir / name
        os.makedirs(longp(target_dir), exist_ok=True)
        shutil.move(longp(page), longp(dest))
        filed[str(dest)] = {"sha": None, "dest": str(dest), "course": course["key"], "kind": "course_page",
                            "size": size_of(dest), "staging": [str(page.relative_to(root))]}


def type_folder(base, kind, number=None, exam=None):
    """Folder for a kind of file inside a course folder (base)."""
    folder = pathlib.Path(base) / TYPE_DIR.get(kind, "אחר")
    if TYPE_DIR.get(kind) == "עבודות":
        folder = folder / (f"עבודה {number}" if number else "ללא מספר")
        if kind in ASSIGN_SUB:
            folder = folder / ASSIGN_SUB[kind]
    elif kind in ("exam", "exam_solution") and (exam or {}).get("year"):
        folder = folder / safe(f"{exam['year']}" + (f" מועד {exam['moed']}" if exam.get("moed") else ""))
    elif kind == "my_exam_booklet":
        folder = folder / "המחברות שלי"
    elif kind == "quiz_review":
        folder = folder / "בחנים במודל"
    return folder


def refile(changes):
    """Move files already in the tree to the folder of their (new) kind, keeping catalog\\filed.jsonl in step.

    changes: [{"path": absolute path in the tree, "kind", "number", "exam"}]. The course folder is the parent of "אחר".
    """
    filed = {r["dest"]: r for r in jsonl(CATALOG / "filed.jsonl")}
    moved, errors = 0, []
    for ch in changes:
        src = pathlib.Path(ch["path"])
        if ch.get("kind") in (None, "other") or not exists(src) or "אחר" not in src.parts:
            continue
        base = pathlib.Path(*src.parts[:src.parts.index("אחר")])
        dest = type_folder(base, ch["kind"], ch.get("number"), ch.get("exam")) / src.name
        n = 1
        while exists(dest):
            n += 1
            dest = dest.with_name(f"{src.stem} ({n}){src.suffix}")
        try:
            os.makedirs(longp(dest.parent), exist_ok=True)
            shutil.move(longp(src), longp(dest))
        except OSError as exc:
            errors.append(f"{src}: {exc}")
            continue
        row = filed.pop(str(src), None) or {"sha": None, "size": size_of(dest)}
        row.update(dest=str(dest), kind=ch["kind"])
        filed[str(dest)] = row
        moved += 1
    write_jsonl(CATALOG / "filed.jsonl", filed.values())
    for course_dir in {pathlib.Path(*pathlib.Path(c["path"]).parts[:pathlib.Path(c["path"]).parts.index("אחר") + 1])
                       for c in changes if "אחר" in pathlib.Path(c["path"]).parts}:
        if course_dir.exists():
            remove_empty(str(course_dir))
    return moved, errors


def remove_empty(top, keep_root=False):
    for dirpath, dirnames, filenames in os.walk(top, topdown=False):
        if keep_root and os.path.samefile(dirpath, top):
            continue
        try:
            if not os.listdir(longp(dirpath)):
                os.rmdir(longp(dirpath))
        except OSError:
            pass


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["plan", "apply", "refile"])
    parser.add_argument("changes", nargs="?", help="refile: JSONL of {path, kind, number, exam}")
    parser.add_argument("--home", default=DEFAULT_HOME)
    parser.add_argument("--only", action="append", help="apply: touch only files that come from this folder (can repeat)")
    args = parser.parse_args()
    if args.command == "plan":
        plan, review = build(args.home)
        write_jsonl(CATALOG / "organize_plan.jsonl", plan)
        (CATALOG / "organize_summary.md").write_text(summary(plan, review, args.home), encoding="utf-8")
        write_jsonl(CATALOG / "organize_review.jsonl", review)
        print(collections.Counter(p["action"] for p in plan), "review:", len(review))
    elif args.command == "refile":
        moved, errors = refile(jsonl(args.changes))
        print(f"moved: {moved}, errors: {len(errors)}")
        for e in errors[:20]:
            print("  ", e)
    else:
        counts, errors = apply(args.home, args.only)
        print(dict(counts))
        print(f"errors: {len(errors)}")
        for e in errors[:30]:
            print("  ", e)
        (CATALOG / "organize_errors.txt").write_text("\n".join(errors) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
