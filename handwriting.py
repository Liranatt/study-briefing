"""Handwriting step: which pages need decoding, page images for the agents, and the merged result.

    python -X utf8 handwriting.py batches <group> [pages_per_batch]   write catalog\\handwriting\\batches_<group>.json
    python -X utf8 handwriting.py render <file id> <page> [<page> ...]  write PNGs to catalog\\handwriting\\png\\<id>\\, print paths
    python -X utf8 handwriting.py merge                                  join page files into catalog\\handwriting\\<id>.md

Groups: "first" = Computational Models (Liran's own files) + every exam booklet; "own" = all of Liran's own
submissions, feedback and notes; "staff" = scanned lectures, recitations, exams and solutions.
An agent writes one file per page: catalog\\handwriting\\pages\\<id>\\p<NNN>.md
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
CAT = HERE / "catalog"
HW = CAT / "handwriting"
ROOT = HERE.parent
OWN = {"my_exam_booklet", "my_submission", "feedback", "my_notes"}
STAFF = {"lecture", "recitation", "exam", "exam_solution", "assignment", "assignment_solution", "quiz_review", "syllabus", "summary_other"}


def files():
    return {r["id"]: r for r in map(json.loads, open(CAT / "files.jsonl", encoding="utf-8"))}


def scanned_pages(row):
    """Page numbers with (almost) no text layer."""
    if row["ext"] in (".png", ".jpg", ".jpeg"):
        return [1]
    text = CAT / "text" / f"{row['id']}.txt"
    if row["ext"] != ".pdf" or not row.get("pages"):
        return []
    pages = {}
    if text.exists():
        current = None
        for line in open(text, encoding="utf-8"):
            if line.startswith("=== page "):
                current = int(line.split()[2])
                pages[current] = 0
            elif current:
                pages[current] += len(line.strip())
    return [p for p in range(1, min(row["pages"], 400) + 1) if pages.get(p, 0) < 40]


def done_pages(fid):
    d = HW / "pages" / fid
    return {int(p.stem[1:]) for p in d.glob("p*.md")} if d.exists() else set()


def batches(group, size):
    rows = files().values()
    if group == "first":
        chosen = [r for r in rows if r["kind"] == "my_exam_booklet" or (r["course"] == "computational-models" and r["kind"] in OWN)]
    elif group == "own":  # everything of Liran's that the "first" group does not already cover
        chosen = [r for r in rows if r["kind"] in OWN and r["kind"] != "my_exam_booklet"
                  and not (r["course"] == "computational-models")]
    else:
        chosen = [r for r in rows if r["kind"] in STAFF]
    work = []
    for r in sorted(chosen, key=lambda r: (r.get("course") or "", r["path"])):
        todo = [p for p in scanned_pages(r) if p not in done_pages(r["id"])]
        if todo:
            work.append((r, todo))
    out, current, count = [], [], 0
    for r, todo in work:
        for start in range(0, len(todo), size):
            chunk = todo[start:start + size]
            if count + len(chunk) > size and current:
                out.append(current)
                current, count = [], 0
            current.append({"id": r["id"], "course": r.get("course"), "kind": r["kind"], "path": r["path"], "pages": chunk})
            count += len(chunk)
    if current:
        out.append(current)
    HW.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(HW / f"batches_{group}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{group}: {sum(len(t) for _, t in work)} pages in {len(work)} files -> {len(out)} batches")


def render(fid, pages):
    import fitz
    row = files()[fid]
    src = ROOT / row["path"]
    out = HW / "png" / fid
    out.mkdir(parents=True, exist_ok=True)
    if row["ext"] in (".png", ".jpg", ".jpeg"):
        print(src)
        return
    doc = fitz.open(src)
    for p in pages:
        target = out / f"p{p:03d}.png"
        if not target.exists():
            doc[p - 1].get_pixmap(dpi=120).save(target)
        print(target)


def merge():
    n = 0
    for d in sorted((HW / "pages").glob("*")):
        parts = sorted(d.glob("p*.md"), key=lambda p: int(p.stem[1:]))
        with open(HW / f"{d.name}.md", "w", encoding="utf-8") as out:
            for p in parts:
                out.write(f"=== page {int(p.stem[1:])} ===\n{p.read_text(encoding='utf-8').strip()}\n")
        n += 1
    print(f"merged {n} files")


def inject():
    """Put each transcribed page into the catalog text file, in place of its empty scanned page."""
    import re
    n = 0
    for md in sorted(HW.glob("f_*.md")):
        fid = md.stem
        pages = dict(re.findall(r"=== page (\d+) ===\n(.*?)(?=\n=== page |\Z)", md.read_text(encoding="utf-8"), re.S))
        if not pages:
            continue
        text = CAT / "text" / f"{fid}.txt"
        current = {}
        if text.exists():
            current = dict(re.findall(r"=== page (\d+) ===\n(.*?)(?=\n=== page |\Z)", text.read_text(encoding="utf-8"), re.S))
        for num, body in pages.items():
            if len(current.get(num, "").strip()) < 40:
                current[num] = "[תמלול כתב יד]\n" + body.strip()
        with open(text, "w", encoding="utf-8") as out:
            for num in sorted(current, key=int):
                out.write(f"=== page {num} ===\n{current[num].strip()}\n")
        n += 1
    print(f"transcripts injected into {n} text files")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1]
    if cmd == "batches":
        batches(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 25)
    elif cmd == "render":
        render(sys.argv[2], [int(p) for p in sys.argv[3:]])
    elif cmd == "merge":
        merge()
    elif cmd == "inject":
        inject()
    else:
        print(__doc__)
