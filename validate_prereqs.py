"""Checks a course's prerequisite list: kb\\courses\\<key>.prereqs.json.

    python -X utf8 validate_prereqs.py kb\\courses\\<key>.prereqs.json

The file lists the tools, techniques and theorems a course actually used, where (file and page),
how, and in which earlier course Liran learned each one. Prints OK or the problems, one per line.
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
WHERE = {"submission", "exam", "quiz", "lecture", "recitation", "project"}
HOW = {"proof", "code", "computation", "analysis", "modeling", "explanation"}


def main(path):
    path = pathlib.Path(path)
    files = {r["id"]: r for r in map(json.loads, open(HERE / "catalog" / "files.jsonl", encoding="utf-8"))}
    concepts = json.load(open(HERE / "kb" / "concepts.json", encoding="utf-8"))
    known = {c["key"] for c in concepts} | {k for c in concepts for k in c.get("merged_keys", [])}
    courses = {c["key"] for c in json.load(open(HERE / "catalog" / "courses.json", encoding="utf-8"))}
    data = json.load(open(path, encoding="utf-8"))
    errors = []
    key = path.name.removesuffix(".prereqs.json")
    if data.get("course") != key:
        errors.append(f"course is {data.get('course')!r}, file name says {key!r}")
    items = data.get("items")
    if not isinstance(items, list) or not items:
        errors.append("items must be a non-empty list")
        items = []
    for n, it in enumerate(items):
        at = f"items[{n}] {it.get('concept')!r}"
        if not it.get("concept"):
            errors.append(f"{at}: no concept key")
        elif not it.get("new_key") and it["concept"] not in known:
            errors.append(f"{at}: concept key not in kb/concepts.json (set new_key: true and name_he if it is new)")
        if it.get("new_key") and not it.get("name_he"):
            errors.append(f"{at}: a new key needs name_he")
        if it.get("where") not in WHERE:
            errors.append(f"{at}: where must be one of {sorted(WHERE)}")
        if it.get("used_how") not in HOW:
            errors.append(f"{at}: used_how must be one of {sorted(HOW)}")
        if it.get("weight") not in (1, 2, 3):
            errors.append(f"{at}: weight must be 1, 2 or 3")
        src = it.get("src") or {}
        f = files.get(src.get("file"))
        if not f:
            errors.append(f"{at}: src.file {src.get('file')!r} is not in catalog/files.jsonl")
        else:
            page, pages = src.get("page"), f.get("pages")
            if page is not None and (not isinstance(page, int) or page < 1 or (pages and page > pages)):
                errors.append(f"{at}: page {page} out of range 1..{pages} for {src['file']}")
        learned = it.get("learned_in")
        if learned is not None and learned.get("course") not in courses:
            errors.append(f"{at}: learned_in.course {learned.get('course')!r} is not a course key")
    print("OK" if not errors else "\n".join(errors))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1]))
