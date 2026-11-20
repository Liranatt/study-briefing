"""Samsung Notes transcripts: which course each note belongs to, reading batches for the agents, and filing the result.

    python -X utf8 samsung_ingest.py map               write catalog\\samsung\\map.jsonl (note -> course key, offering, folder) and print a summary
    python -X utf8 samsung_ingest.py batches [units]   write catalog\\samsung\\batches.json (default 40 pages per batch; photos count 1)
    python -X utf8 samsung_ingest.py status            how many page transcripts exist, per batch group
    python -X utf8 samsung_ingest.py file              turn the per-page transcripts into one .md per note inside the study tree
                                                         (<course>\\סיכומים שלי\\Samsung Notes\\...), so catalog.py picks them up

An agent reads the page images of a batch and writes one file per image to catalog\\samsung\\md\\<note dir>\\<image name>.md
(p012.png -> p012.md, photo_003.jpg -> photo_003.md). Skipped on purpose: bureaucracy, credit-card, telepad and job-interview folders.
"""
import json
import os
import pathlib
import re
import sys
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CAT = HERE / "catalog"
SN = CAT / "samsung"
MD = SN / "md"
LOCAL = pathlib.Path(os.environ["LOCALAPPDATA"]) / "study-briefing" / "samsung" / "pages"

# folder name (the course level of the Samsung tree) -> course key in catalog\courses.json
COURSE = {
    "למידה חישובית": "intro-ml", "למידה על גרפים": "graph-learning", "מודלים": "computational-models", "מערכות הפעלה": "operating-systems",
    "אלגברה לינארית": "algebra-1-cs", "לוגיקה": "logic-set-theory", "מבוא למדעי המחשב": "intro-cs", "אלגברה 2": "algebra-2-cs",
    "מבנה נתונים": "data-structures", "קומבינטוריקה": "discrete-structures", "חדוא 2": "calculus-2-cs", "מבוא להסתברות": "probability-cs",
    "תכנון אלגוריתמים": "algorithm-design", "תכנות מערכות": "systems-programming", "אופטימזציה": "optimization-ds",
    "הסתברות 2.0": "prob-stat-cs", "הסתברות וסטטיסטיקה": "prob-stat-cs", "עקרונות שפות תכנות": "programming-languages",
    "DL": "deep-learning-intro", "ML": "intro-ml", "אנליזה נומרית": "numerical-analysis", "יסודות המימון": "finance-foundations",
    "מעבדה בניתוח נתונים פיננסים": "financial-data-lab", "מודלים חישוביים": "computational-models", "עיבוד שפה טבעית": "nlp",
    "שיטות קומבינטוריות וגיאומטריות": "geometric-learning-theory",
    # the neuroscience and math years
    "ההפילוסופיה של המדע": "philosophy-logic-science", "מבוא לבלשנות": "linguistics-intro", "מבוא לכימיה": "chemistry-cog",
    "ביולוגיה של התא": "cell-biology", "חדוא א": "calculus-1-eng", "סמינר": "cogsci-seminar", "סמנטיקה": "semantics-nl",
    "פיזיקה 1": "physics-1c", "פייתון": "python-intro", "פילוסופיה של הנפש": "philosophy-of-mind", "תחביר": "syntax-nl",
    "כימיה": "chemistry-cog", "פיזיקה 2": "physics-2c", "מבוא לרשתות נוירונים": "neural-networks-intro",
    "אינפי": "calculus-1", "מעבדה": "espl", "בדידה": "discrete-math", "לינארית": "linear-algebra-1",
}
SKIP = {"בירוקרטיה", "אשראי", "טלפד"}  # personal admin, not study material (interview prep is NOT skipped: it is career-related)
UNSORTED = ROOT / "לא ממוין" / "Samsung Notes"
SEMDIR = {"א": "סמסטר א", "ב": "סמסטר ב", "קיץ": "קיץ"}


def norm(s):
    return re.sub(r"[\"'׳״”“’‘`\s]+", "", s or "").replace("חדו״א", "חדוא").lower()


def acad(created):
    """'2024-11-03 10:00' -> ('2024-25', 'א')"""
    d = datetime.strptime(created[:10], "%Y-%m-%d")
    y, m = d.year, d.month
    if m >= 10:
        return f"{y}-{(y + 1) % 100:02d}", "א"
    if m <= 2:
        return f"{y - 1}-{y % 100:02d}", "א"
    if m <= 6:
        return f"{y - 1}-{y % 100:02d}", "ב"
    return f"{y - 1}-{y % 100:02d}", "קיץ"


def course_level(path):
    """The Samsung folder path -> (course folder name or None, extra sub-folders)."""
    p = list(path)
    if p[:2] == ["Folders", "Archive"] or p[:1] == ["Archive"]:
        p = p[p.index("Archive") + 1:]
        # Archive/<degree>/<semester>/<course>/<sub...>   or   Archive/<degree>/<course>/<sub...> (math: one level less)
        for i, name in enumerate(p):
            if name in COURSE:
                return name, p[i + 1:]
        return (p[0] if p else None), p[1:]
    if p[:1] == ["Folders"]:
        p = p[1:]
    if p[:1] in (["מדעי המחשב"], ["מדעי המחשב 2"]):
        for i, name in enumerate(p[1:], 1):
            if name in COURSE:
                return name, p[i + 1:]
        return (p[1] if len(p) > 1 else None), p[2:]
    return (p[0] if p else None), p[1:]


def courses():
    return {c["key"]: c for c in json.load(open(CAT / "courses.json", encoding="utf-8"))}


def pick_offering(c, created):
    offs = c.get("offerings") or []
    if not offs:
        return None
    if len(offs) == 1:
        return offs[0]
    y, s = acad(created)
    for o in offs:
        if o["year"] == y and o["semester"] == s:
            return o
    same = [o for o in offs if o["semester"] == s]
    return (same or offs)[0] if y < offs[0]["year"] else (same or offs)[-1]


def course_dir(c, off):
    base = ROOT / off["year"] / SEMDIR[off["semester"]]
    want = norm(c["name_he"])
    if base.exists():
        for d in base.iterdir():
            if d.is_dir() and (norm(d.name) == want or norm(d.name).startswith(want[:12])):
                return d
    return None


def make_map():
    notes = [json.loads(l) for l in open(SN / "notes.jsonl", encoding="utf-8")]
    C = courses()
    out, stats = [], {}
    for n in notes:
        top = (n["path"][1] if n["path"][:1] == ["Folders"] and len(n["path"]) > 1 else (n["path"][0] if n["path"] else ""))
        name, sub = course_level(n["path"])
        row = {"uuid": n["uuid"], "dir": n["dir"], "title": n["title"], "samsung_path": "/".join(n["path"]), "created": n["created"], "skip": False}
        if name in SKIP or any(x in SKIP for x in n["path"]):
            row["skip"] = True
            row["why"] = "personal admin folder"
        key = COURSE.get(name)
        if name == "מעבדה":
            key = "espl"
        row["course"], row["sub"] = key, [s for s in sub if s]
        if key and not row["skip"]:
            off = pick_offering(C[key], n["created"] or "2024-01-01")
            cd = course_dir(C[key], off) if off else None
            if cd:
                row.update({"year": off["year"], "semester": off["semester"],
                            "dest": str((cd / "סיכומים שלי" / "Samsung Notes" / pathlib.Path(*row["sub"])).relative_to(ROOT)) if row["sub"]
                            else str((cd / "סיכומים שלי" / "Samsung Notes").relative_to(ROOT))})
            else:
                key = None
        if not row.get("dest") and not row["skip"]:
            row["course"] = None
            row["dest"] = str(pathlib.Path("לא ממוין") / "Samsung Notes" / "/".join(x for x in [name or "שורש"] + row["sub"] if x))
        stats[row["course"] or ("skip" if row["skip"] else "unsorted")] = stats.get(row["course"] or ("skip" if row["skip"] else "unsorted"), 0) + 1
        out.append(row)
    with open(SN / "map.jsonl", "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
        print(v, k)
    print(len(out), "notes ->", SN / "map.jsonl")


def priority(m):
    p = m["samsung_path"]
    if "Archive" in p:
        return 2
    if p.startswith("Folders/מדעי המחשב"):
        return 0
    return 1


def make_batches(size):
    """Append batches for the images not yet in batches.json, so batch numbers never change once an agent has one."""
    maps = {json.loads(l)["uuid"]: json.loads(l) for l in open(SN / "map.jsonl", encoding="utf-8")}
    rendered = [json.loads(l) for l in open(SN / "pages.jsonl", encoding="utf-8")]
    rendered.sort(key=lambda r: (priority(maps[r["uuid"]]), maps[r["uuid"]]["samsung_path"], maps[r["uuid"]]["created"] or ""))
    master = json.load(open(SN / "batches.json", encoding="utf-8")) if (SN / "batches.json").exists() else []
    assigned = {f for b in master for i in b for f in i["images"]}
    batches, cur, count = [], [], 0
    for r in rendered:
        m = maps[r["uuid"]]
        if m["skip"] or "error" in r:
            continue
        d = LOCAL / m["dir"]
        files = [str(d / f) for f in r["pages"]] + [str(d / f) for f in r["photos"]]
        files = [f for f in files if f not in assigned and not (MD / m["dir"] / (pathlib.Path(f).stem + ".md")).exists()]
        for i in range(0, len(files), size):
            chunk = files[i:i + size]
            if count + len(chunk) > size and cur:
                batches.append(cur)
                cur, count = [], 0
            cur.append({"uuid": r["uuid"], "dir": m["dir"], "title": m["title"], "folder": m["samsung_path"], "course": m["course"],
                        "created": m["created"], "images": chunk, "out": str(MD / m["dir"]), "group": priority(m)})
            count += len(chunk)
    if cur:
        batches.append(cur)
    start = len(master)
    json.dump(master + batches, open(SN / "batches.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"added batches {start}..{start + len(batches) - 1} ({len(batches)}); {sum(len(i['images']) for b in batches for i in b)} images; total {start + len(batches)}")


HI = LOCAL.parent / "hi"
MD2 = SN / "md2"


def make_batches2(kind, size):
    """Careful-pass batches: only pages (png) or only photos (jpg). Append-only master file batches_<kind>.json.
    Each item: image, hi (high-res twin), draft (first-pass transcript if any), out (md2 folder)."""
    maps = {json.loads(l)["uuid"]: json.loads(l) for l in open(SN / "map.jsonl", encoding="utf-8")}
    rendered = [json.loads(l) for l in open(SN / "pages.jsonl", encoding="utf-8")]
    rendered.sort(key=lambda r: (priority(maps[r["uuid"]]), maps[r["uuid"]]["samsung_path"], maps[r["uuid"]]["created"] or ""))
    mf = SN / f"batches_{kind}.json"
    master = json.load(open(mf, encoding="utf-8")) if mf.exists() else []
    assigned = {i["image"] for b in master for i in b}
    batches, cur = [], []
    for r in rendered:
        m = maps[r["uuid"]]
        if m["skip"] or "error" in r:
            continue
        d = LOCAL / m["dir"]
        names = r["pages"] if kind == "pages" else r["photos"]
        for f in names:
            img = str(d / f)
            if img in assigned or (MD2 / m["dir"] / (pathlib.Path(f).stem + ".md")).exists():
                continue
            hi = str(HI / m["dir"] / f) if kind == "pages" else img
            draft = MD / m["dir"] / (pathlib.Path(f).stem + ".md")
            cur.append({"uuid": r["uuid"], "dir": m["dir"], "title": m["title"], "folder": m["samsung_path"], "course": m["course"],
                        "image": img, "hi": hi, "draft": str(draft) if draft.exists() else None, "out": str(MD2 / m["dir"] / (pathlib.Path(f).stem + ".md"))})
            if len(cur) >= size:
                batches.append(cur)
                cur = []
    if cur:
        batches.append(cur)
    start = len(master)
    json.dump(master + batches, open(mf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{kind}: added batches {start}..{start + len(batches) - 1} ({len(batches)}); {sum(len(b) for b in batches)} images; total {start + len(batches)}")


def status():
    done = len(list(MD.rglob("*.md"))) if MD.exists() else 0
    b = json.load(open(SN / "batches.json", encoding="utf-8")) if (SN / "batches.json").exists() else []
    tot = sum(len(i["images"]) for x in b for i in x)
    print(f"page transcripts on disk: {done}; images in the current batch file: {tot}")


def safe(s):
    return re.sub(r'[\\/:*?"<>|\r\n]+', " ", s).strip(" .") or "ללא כותרת"


def file_notes():
    maps = [json.loads(l) for l in open(SN / "map.jsonl", encoding="utf-8")]
    rendered = {json.loads(l)["uuid"]: json.loads(l) for l in open(SN / "pages.jsonl", encoding="utf-8")}
    written = 0
    for m in maps:
        d = MD / m["dir"]
        d2 = MD2 / m["dir"]
        if m["skip"] or not (d.exists() or d2.exists()):
            continue
        byname = {p.name: p for p in d.glob("*.md")} if d.exists() else {}
        if d2.exists():
            byname.update({p.name: p for p in d2.glob("*.md")})  # careful pass wins over the quick pass
        parts = sorted(byname.values(), key=lambda p: (p.stem.startswith("photo"), p.stem))
        if not parts:
            continue
        r = rendered.get(m["uuid"], {})
        total = len(r.get("pages", [])) + len(r.get("photos", []))
        dest = ROOT.joinpath(*[part.rstrip(". ") or "_" for part in pathlib.PurePath(m["dest"]).parts])  # Windows drops trailing dots/spaces in folder names
        dest.mkdir(parents=True, exist_ok=True)
        name = f"{safe(m['title'])} ({(m['created'] or '')[:10]}) {m['uuid'][-4:]}.md"
        lines = [f"# {m['title']}", "",
                 f"מקור: Samsung Notes, תיקייה {m['samsung_path']}, נוצר {m['created']}. תמלול כתב יד של לירן (נקרא מתמונות העמודים).",
                 f"עמודים שתומללו: {len(parts)} מתוך {total}", ""]
        for p in parts:
            label = ("צילום " + p.stem.split("_")[-1].lstrip("0")) if p.stem.startswith("photo") else "עמוד " + p.stem[1:].lstrip("0")
            if p.parent.name == "md":
                label += " (קריאה ראשונית, פחות מדויקת)"
            lines += [f"## {label}", "", p.read_text(encoding="utf-8").strip(), ""]
        (dest / name).write_text("\n".join(lines), encoding="utf-8")
        written += 1
    print(f"{written} note files written into the study tree")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "map":
        make_map()
    elif cmd == "batches":
        make_batches(int(sys.argv[2]) if len(sys.argv) > 2 else 40)
    elif cmd == "batches2":
        make_batches2(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 30)
    elif cmd == "status":
        status()
    elif cmd == "file":
        file_notes()
    else:
        print(__doc__)
