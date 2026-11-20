"""Builds the study catalog from the organized tree in מדעי המחשב. No model involved.

    python catalog.py            catalog every file and extract text (only new files get extracted)

Writes, under catalog\\:
    files.jsonl      one row per file: id, path, course, offering, kind, assignment number, pages, text pages, sha256
    text\\<id>.txt   the file's text, page by page ("=== page N ==="), for PDF, Office, HTML, notebooks, code
    by_course\\<key>.json   the file list of one course, the input of its course agent

The id is "f_" + the first 8 hex digits of the sha256 of the content, so it survives moves and renames.
Scanned pages without text are counted (pages - text_pages); their handwriting is decoded in a later step.
"""
import hashlib
import html
import json
import os
import pathlib
import re
import sys
import zipfile

import paths

HERE = pathlib.Path(__file__).resolve().parent
CATALOG = HERE / "catalog"
TEXT = CATALOG / "text"
BY_COURSE = CATALOG / "by_course"
ROOT = HERE.parent
SKIP_TOP = {"study-briefing", "אישורים לארנונה"}
SEM = {"סמסטר א": "א", "סמסטר ב": "ב", "קיץ": "קיץ"}
FOLDER_KIND = {"שיעורים": "lecture", "תרגולים": "recitation", "סיכומים שלי": "my_notes", "סיכומים": "summary_other",
               "מידע על הקורס": "syllabus", "ציונים": "grades_records", "קוד": "code_project", "מאמרים": "paper",
               "ספרים": "book", "חומר שנוצר עם Claude": "generated", "אחר": "other"}
CODE_EXT = {".py", ".c", ".h", ".cpp", ".java", ".js", ".ts", ".s", ".asm", ".m", ".r", ".sql", ".sh", ".scala", ".rkt", ".hs"}
PLAIN_EXT = {".txt", ".md", ".csv", ".tex", ".json", ".yaml", ".yml"} | CODE_EXT
MAX_PAGES = 400
MAX_CHARS = 400_000


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def safe(name, limit=60):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(name).replace('"', "״"))
    return re.sub(r"\s+", " ", name).strip(" .")[:limit] or "_"


def strip_html(text):
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return "\n".join(l.strip() for l in text.splitlines() if l.strip())


def office_text(path):
    """Text of a .docx / .pptx / .xlsx, from its XML, one block per slide or document part."""
    parts = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        slides = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)), key=lambda n: int(re.findall(r"\d+", n)[-1]))
        docs = [n for n in names if n in ("word/document.xml",)] or [n for n in names if re.match(r"xl/sharedStrings\.xml$", n)]
        for i, n in enumerate(slides or docs, 1):
            xml = z.read(n).decode("utf-8", "replace")
            xml = re.sub(r"</a:p>|</w:p>|</si>", "\n", xml)
            parts.append((i, html.unescape(re.sub(r"<[^>]+>", "", xml))))
    return parts


def extract(path, ext):
    """[(page number, text)], pages, text_pages"""
    if ext == ".pdf":
        import fitz
        doc = fitz.open(path)
        pages = [(i + 1, doc[i].get_text()) for i in range(min(doc.page_count, MAX_PAGES))]
        return pages, doc.page_count
    if ext in (".docx", ".pptx", ".xlsx"):
        parts = office_text(path)
        return parts, len(parts)
    if ext in (".html", ".htm"):
        return [(1, strip_html(open(path, encoding="utf-8", errors="replace").read()))], 1
    if ext == ".ipynb":
        nb = json.load(open(path, encoding="utf-8"))
        cells = ["".join(c.get("source", [])) for c in nb.get("cells", [])]
        return [(i + 1, c) for i, c in enumerate(cells)], len(cells)
    if ext in PLAIN_EXT:
        return [(1, open(path, encoding="utf-8", errors="replace").read(MAX_CHARS))], 1
    if ext == ".zip":
        with zipfile.ZipFile(path) as z:
            return [(1, "ZIP members:\n" + "\n".join(f"{i.file_size:>10}  {i.filename}" for i in z.infolist()))], 1
    return [], 0


def kind_of(parts, name, filed_kind):
    """Kind from the folder layout (the filing step's kind wins when known)."""
    if filed_kind and filed_kind not in ("course_page",):
        return filed_kind
    if name.startswith("דף הקורס במודל") or name.startswith("_moodle_"):
        return "course_page"
    folder = parts[0] if parts else ""
    sub = parts[1:]
    if folder == "עבודות":
        if "ההגשה שלי" in sub:
            return "my_submission"
        if "משוב" in sub:
            return "feedback"
        if "פתרון" in sub:
            return "assignment_solution"
        return "assign_summary" if name == "סיכום.md" else "assignment"
    if folder == "מבחנים":
        if "המחברות שלי" in sub:
            return "my_exam_booklet"
        if "בחנים במודל" in sub:
            return "quiz_review"
        return "exam_solution" if re.search(r"sol|פתרון|solution|תשובות|answers", name, re.I) else "exam"
    return FOLDER_KIND.get(folder, "other")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    TEXT.mkdir(parents=True, exist_ok=True)
    BY_COURSE.mkdir(parents=True, exist_ok=True)
    courses = json.load(open(CATALOG / "courses.json", encoding="utf-8"))
    by_name = {safe(c["name_he"]): c for c in courses}
    filed = {}
    if (CATALOG / "filed.jsonl").exists():
        for row in map(json.loads, open(CATALOG / "filed.jsonl", encoding="utf-8")):
            filed[os.path.normcase(paths.local(row["dest"]))] = row  # stored as a Windows path
    manifest = {}
    if (CATALOG / "moodle_files.jsonl").exists():
        for row in map(json.loads, open(CATALOG / "moodle_files.jsonl", encoding="utf-8")):
            if row.get("path"):
                manifest[paths.portable_rel(paths.rel(row["path"]))] = row
    old, moved = {}, {}
    if (CATALOG / "files.jsonl").exists():
        for row in map(json.loads, open(CATALOG / "files.jsonl", encoding="utf-8")):
            old[(paths.rel(row["path"]), row["size"], row.get("mtime"))] = row  # '\' paths from Windows, on Linux
            moved[(row["name"], row["size"], row.get("mtime"))] = row

    rows, extracted = [], 0
    for top in sorted(p for p in ROOT.iterdir() if p.is_dir() and p.name not in SKIP_TOP):
        for path in sorted(top.rglob("*")):
            if not path.is_file() or any(part in (".git", ".idea", "__pycache__", ".venv") for part in path.parts):
                continue
            rel = path.relative_to(ROOT)
            st = path.stat()
            known = old.get((str(rel), st.st_size, int(st.st_mtime))) or moved.get((path.name, st.st_size, int(st.st_mtime)))
            f = filed.get(os.path.normcase(str(path)), {})
            sha = known["sha256"] if known else (f["sha"] if f.get("sha") and not f["sha"].startswith("ns:") and f.get("size") == st.st_size else sha_file(path))
            row = {"id": "f_" + sha[:8], "path": str(rel), "name": path.name, "ext": path.suffix.lower(), "size": st.st_size,
                   "mtime": int(st.st_mtime), "sha256": sha}
            parts = rel.parts
            m = re.match(r"(20\d\d-\d\d) (סמסטר א|סמסטר ב|קיץ)$", parts[0])  # old layout: "<year> <semester>\<course>"
            y = re.match(r"20\d\d-\d\d$", parts[0]) and len(parts) > 3 and parts[1] in SEM  # layout: "<year>\<semester>\<course>"
            if y:
                course = by_name.get(parts[2])
                row.update(course=course["key"] if course else None, year=parts[0], semester=SEM[parts[1]])
                inner = parts[3:-1]
            elif m and len(parts) > 2:
                course = by_name.get(parts[1])
                row.update(course=course["key"] if course else None, year=m.group(1), semester=SEM[m.group(2)])
                inner = parts[2:-1]
            elif parts[0] == "מחקר":
                row.update(course="research", year=None, semester=None)
                inner = parts[1:-1]
            else:
                row.update(course=None, year=None, semester=None)
                inner = parts[:-1]
            row["kind"] = kind_of(list(inner), path.name, f.get("kind"))
            hit = next((re.search(r"(\d+)", p) for p in inner if p.startswith(("עבודה", "תרגיל", "מטלה"))), None)
            row["number"] = hit.group(1) if hit else None
            exam = next((p for p in inner if re.match(r"(19|20)\d\d", p)), None) if inner and inner[0] == "מבחנים" else None
            row["exam"] = exam
            staging = [paths.portable_rel(paths.rel(s)) for s in f.get("staging", [])]
            moodle = next((manifest[s] for s in staging if s in manifest), None)
            if moodle:
                row["moodle"] = {"course": moodle.get("course"), "section": moodle.get("section"), "item": moodle.get("module")}
            if known and (TEXT / f"{row['id']}.txt").exists():
                row.update(pages=known.get("pages"), text_pages=known.get("text_pages"))
            else:
                try:
                    pages, total = extract(str(path), row["ext"])
                except Exception as exc:
                    pages, total = [], 0
                    row["extract_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
                text_pages = sum(1 for _, t in pages if len(t.strip()) > 40)
                row.update(pages=total, text_pages=text_pages)
                if pages:
                    with open(TEXT / f"{row['id']}.txt", "w", encoding="utf-8") as out:
                        size = 0
                        for n, t in pages:
                            block = f"=== page {n} ===\n{t.strip()}\n"
                            size += len(block)
                            if size > MAX_CHARS:
                                out.write(f"=== cut: text longer than {MAX_CHARS} characters ===\n")
                                break
                            out.write(block)
                    extracted += 1
            rows.append(row)
            if len(rows) % 500 == 0:
                print(f"  {len(rows)} files...", flush=True)

    with open(CATALOG / "files.jsonl", "w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    per = {}
    for row in rows:
        per.setdefault(row["course"] or "_none", []).append(row)
    for key, items in per.items():
        course = next((c for c in courses if c["key"] == key), {"key": key, "name_he": key, "offerings": []})
        json.dump({"course": course, "files": items}, open(BY_COURSE / f"{key}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    scans = sum(1 for r in rows if r.get("pages") and r.get("text_pages", 0) < r["pages"] / 2)
    print(f"{len(rows)} files, {len(per)} courses, {extracted} extracted now, {scans} mostly scanned (handwriting step later)")


if __name__ == "__main__":
    main()
