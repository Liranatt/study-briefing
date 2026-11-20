"""Files what moodle_pull.py just downloaded into the study tree, without duplicates.

    python -X utf8 file_new.py            do it
    python -X utf8 file_new.py --dry-run  only show what would happen

For every downloaded file in catalog\\moodle_files.jsonl that is still in the staging folder (קורסים\\):
  - same content already in the tree (sha256): the staging copy goes to the Recycle Bin, and its Moodle URL is
    added to that tree file's row in catalog\\filed.jsonl, so it is not downloaded again;
  - new content: moved to <year>\\<semester>\\<course>\\<type>\\..., by the same rules as organize.py.
This is the small daily version of organize.py, for new Moodle files only.
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import sys

import organize as org
import paths

HERE = pathlib.Path(__file__).resolve().parent
CATALOG = HERE / "catalog"
ROOT = HERE.parent
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}
KIND = {"assign_intro": "assignment", "my_submission": "my_submission", "feedback": "feedback",
        "quiz_review": "quiz_review", "assign_summary": "assign_summary"}
NAMED_BY_MODULE = {"quiz_review", "assign_summary"}  # "ניסיון 1.html", "סיכום.md": the module name goes first


def number(text):
    n = org.number_of(text)
    if n:
        return n
    hit = re.search(r"(?:עבודה|תרגיל(?: בית)?|מטלה|homework|hw)\s+([IVX]{1,4})\b", text or "", re.I)
    return str(ROMAN[hit.group(1).upper()]) if hit and hit.group(1).upper() in ROMAN else None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    staging = org.moodle_root()
    courses = json.load(open(CATALOG / "courses.json", encoding="utf-8"))
    base_of = {o["moodle_id"]: ROOT / o["year"] / org.SEM_DIR[o["semester"]] / org.safe(c["name_he"], 60)
               for c in courses for o in c["offerings"] if o.get("moodle_id")}
    key_of = {o["moodle_id"]: c["key"] for c in courses for o in c["offerings"] if o.get("moodle_id")}
    filed = {paths.local(r["dest"]): r for r in org.jsonl(CATALOG / "filed.jsonl")}  # keyed by the path on this machine
    tree = {}
    for r in org.jsonl(CATALOG / "files.jsonl"):
        if r.get("sha256"):
            tree.setdefault(r["sha256"], str(ROOT / paths.rel(r["path"])))
    for dest, r in filed.items():
        if r.get("sha") and org.exists(dest):
            tree.setdefault(r["sha"], dest)
    moved = dup = 0
    trash = []
    for rec in org.jsonl(CATALOG / "moodle_files.jsonl"):
        if not str(rec.get("status", "")).startswith(("downloaded", "written")) or not rec.get("path") or not staging:
            continue
        src = staging / paths.rel(rec["path"])
        if not org.exists(src):
            continue
        sha = rec.get("sha256") or org.sha_file(src)
        url = rec.get("url")
        if sha in tree:
            dest = tree[sha]
            row = filed.setdefault(dest, {"sha": sha, "dest": paths.portable(dest), "course": key_of.get(rec.get("course")), "kind": rec.get("kind"),
                                          "size": rec.get("size"), "urls": [], "staging": []})
            if url and url not in row.setdefault("urls", []):
                row["urls"].append(url)
            if rec["path"] not in row.setdefault("staging", []):
                row["staging"].append(rec["path"])
            trash.append(str(src))
            dup += 1
            continue
        base = base_of.get(rec.get("course"))
        if not base:
            print(f"   no course folder for Moodle course {rec.get('course')}: {rec['path']}")
            continue
        if not rec.get("module"):  # pages moodle_pull writes itself carry the module only in their folder: "02 תרגיל בית II"
            rec["module"] = re.sub(r"^\d+\s+", "", pathlib.PureWindowsPath(rec["path"]).parent.name)
        text = f"{rec.get('module') or ''} {rec.get('filename') or ''}"
        kind = KIND.get(rec.get("kind")) or org.guess_kind(rec)
        name = rec.get("filename") or src.name
        if kind in NAMED_BY_MODULE and rec.get("module"):
            name = f"{rec['module']} - {name}"
        dest = org.type_folder(base, kind, number(text)) / org.safe(name, 120)
        n = 1
        while org.exists(dest):
            n += 1
            dest = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        print(f"   new: {rec['path']}  ->  {dest.relative_to(ROOT)}")
        if not args.dry_run:
            os.makedirs(org.longp(dest.parent), exist_ok=True)
            shutil.move(org.longp(src), org.longp(dest))
            filed[str(dest)] = {"sha": sha, "dest": paths.portable(dest), "course": key_of.get(rec.get("course")), "kind": kind,
                                "size": rec.get("size"), "urls": [url] if url else [], "staging": [rec["path"]]}
            tree[sha] = str(dest)
        moved += 1
    gone = [] if args.dry_run else org.recycle(trash)
    if not args.dry_run:
        org.write_jsonl(CATALOG / "filed.jsonl", list(filed.values()))
        org.remove_empty(staging, keep_root=True)
    print(f"{moved} new files filed, {dup} duplicates ({len(gone)} staging copies sent to the Recycle Bin)")


if __name__ == "__main__":
    main()
