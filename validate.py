"""Checks a course agent's output: the schema, every file id, every page number.

    python -X utf8 validate.py kb\\courses\\<key>.json

Prints "OK" and exits 0, or lists the problems and exits 1.
"""
import json
import pathlib
import sys

import jsonschema

HERE = pathlib.Path(__file__).resolve().parent


def refs(node):
    """Every {"file": ...} reference inside the output."""
    if isinstance(node, dict):
        if "file" in node and isinstance(node["file"], str) and (node.get("page") or node.get("time")):
            yield node
        for value in node.values():
            yield from refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from refs(value)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    path = pathlib.Path(sys.argv[1])
    problems = []
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception as exc:
        print(f"not valid JSON: {exc}")
        sys.exit(1)
    schema = json.load(open(HERE / "templates" / "course_summary.schema.json", encoding="utf-8"))
    for error in sorted(jsonschema.Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))[:40]:
        problems.append(f"schema at {'/'.join(map(str, error.path)) or '(top)'}: {error.message[:200]}")
    files = {}
    for row in map(json.loads, open(HERE / "catalog" / "files.jsonl", encoding="utf-8")):
        files.setdefault(row["id"], row)
    key = (data.get("course") or {}).get("key")
    for ref in refs(data):
        row = files.get(ref["file"])
        if row is None:
            problems.append(f"unknown file id {ref['file']}")
            continue
        if key and row.get("course") not in (key, None):
            problems.append(f"{ref['file']} belongs to course {row.get('course')}, not {key}")
        pages = row.get("pages") or 0
        for p in (ref.get("page"), ref.get("page_end")):
            if p and pages and p > pages:
                problems.append(f"{ref['file']} has {pages} pages; page {p} does not exist")
    seen = set()
    for c in data.get("concepts", []):
        if c.get("key") in seen:
            problems.append(f"concept key {c['key']} appears twice")
        seen.add(c.get("key"))
    if problems:
        print(f"{len(problems)} problems:")
        for p in dict.fromkeys(problems):
            print(" -", p)
        sys.exit(1)
    print(f"OK: {len(data.get('units', []))} units, {len(data.get('concepts', []))} concepts, "
          f"{len(data.get('assignments', []))} assignments, {len(data.get('exams', []))} exams, {len(list(refs(data)))} references")


if __name__ == "__main__":
    main()
