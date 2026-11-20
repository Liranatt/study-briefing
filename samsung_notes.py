"""Samsung Notes (Windows app) -> a manifest and page images, for the handwriting step.

    python -X utf8 samsung_notes.py list                    write catalog\\samsung\\notes.jsonl (one row per live note)
    python -X utf8 samsung_notes.py pack <uuid> [<uuid>..]  zip each note folder to an .sdocx in the work folder
    python -X utf8 samsung_notes.py render <uuid> [<uuid>..]  pack, then sdocx-cli -> PNG pages in the work folder
    python -X utf8 samsung_notes.py pick                    pick 3 test notes (strokes only, PDF-annotated, photos)

The app keeps every note unpacked under
    %LOCALAPPDATA%\\Packages\\SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy\\LocalState\\wdoc\\<uuid>\\
and the titles, folder tree and dates in LocalState\\Storage.sqlite. This script only READS them (the database is
copied first). The app's own handwriting-recognition field (HWTextContent) is empty, so the text has to come from
reading the rendered pages.

Page images go to %LOCALAPPDATA%\\study-briefing\\samsung\\png\\<uuid>\\ (outside OneDrive: they are big). The manifest
goes to catalog\\samsung\\. sdocx-cli is the open-source reverse-engineered converter (cargo install sdocx-cli).
"""
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
CAT = HERE / "catalog" / "samsung"
LOCAL = pathlib.Path(os.environ["LOCALAPPDATA"])
STATE = LOCAL / "Packages" / "SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy" / "LocalState"
WORK = LOCAL / "study-briefing" / "samsung"
CLI = pathlib.Path.home() / ".cargo" / "bin" / "sdocx-cli.exe"


def snapshot_db():
    WORK.mkdir(parents=True, exist_ok=True)
    dst = WORK / "Storage_copy.sqlite"
    shutil.copy2(STATE / "Storage.sqlite", dst)
    c = sqlite3.connect(f"file:{dst.as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def ts(v):
    if not v:
        return None
    v = v / 1000 if v > 1e11 else v
    return datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M")


def folder_paths(c):
    tree = {r["UUID"]: (r["ParentUUID"], r["DisplayName"]) for r in c.execute("select UUID,ParentUUID,DisplayName from CategoryTreeDB")}

    def path(u):
        out = []
        while u and u in tree:
            p, n = tree[u]
            out.append(n)
            u = p
        return [x for x in reversed(out) if x]
    return path


def list_notes():
    c = snapshot_db()
    path = folder_paths(c)
    extra = {r["UUID"]: dict(r) for r in c.execute("select * from document_extra")}
    CAT.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(CAT / "notes.jsonl", "w", encoding="utf-8") as out:
        for r in c.execute("select UUID,Title,CategoryUUID,CreatedAt,LastModifiedAt,FilePath from NoteDB "
                           "where DeletedStatus=0 or DeletedStatus is null"):
            e = extra.get(r["UUID"], {})
            folder = pathlib.Path(r["FilePath"] or "").name
            d = STATE / "wdoc" / folder
            media = d / "media"
            row = {"uuid": r["UUID"], "dir": folder, "title": (r["Title"] or "").strip(), "path": path(r["CategoryUUID"]),
                   "created": ts(r["CreatedAt"]), "modified": ts(r["LastModifiedAt"]),
                   "has_stroke": bool(e.get("hasStroke")), "has_image": bool(e.get("hasImage")), "has_pdf": bool(e.get("hasPdf")),
                   "spi": len(list(media.glob("*.spi"))) if media.exists() else 0,
                   "jpg": len(list(media.glob("*.jpg"))) if media.exists() else 0,
                   "bytes": sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.exists() else 0}
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    print(f"{n} notes -> {CAT / 'notes.jsonl'}")


def rows():
    return {r["uuid"]: r for r in map(json.loads, open(CAT / "notes.jsonl", encoding="utf-8"))}


def pack(uuid):
    row = rows()[uuid]
    src = STATE / "wdoc" / row["dir"]
    (WORK / "sdocx").mkdir(parents=True, exist_ok=True)
    dst = WORK / "sdocx" / f"{row['dir']}.sdocx"
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(src).as_posix())
    return dst


def render(uuid):
    sd = pack(uuid)
    out = WORK / "png" / rows()[uuid]["dir"]
    out.mkdir(parents=True, exist_ok=True)
    target = out / "note.png"
    run = subprocess.run([str(CLI), str(sd), "-o", str(target)], capture_output=True, text=True, encoding="utf-8")
    print(run.returncode, run.stdout[-300:], run.stderr[-500:])
    return sorted(out.glob("*.png"))


INK_MIN = 0.02  # percent of dark pixels below which a page counts as blank


def ink_percent(png):
    import numpy as np
    from PIL import Image
    return float((np.array(Image.open(png).convert("L")) < 200).mean() * 100)


def render_note(uuid):
    """The page images worth reading: pages with ink only. For a note with an embedded PDF (an annotated lecture)
    the CLI draws only the handwriting, so the ink is laid over the matching PDF page. Photos are copied at full size."""
    import numpy as np
    from PIL import Image
    row = rows()[uuid]
    src = STATE / "wdoc" / row["dir"]
    out = WORK / "pages" / row["dir"]
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    tmp = WORK / "png" / row["dir"]
    shutil.rmtree(tmp, ignore_errors=True)
    for p in render(uuid):
        pass
    pdfs = sorted((src / "media").glob("*.pdf"))
    doc = None
    if pdfs:
        import fitz
        doc = fitz.open(pdfs[0])
    kept = []
    for png in sorted(tmp.glob("note_page*.png"), key=lambda p: int(p.stem.replace("note_page", ""))):
        n = int(png.stem.replace("note_page", ""))
        if ink_percent(png) < INK_MIN:
            continue
        target = out / f"p{n + 1:03d}.png"
        if doc is not None and n < len(doc):
            ink = Image.open(png).convert("RGB")
            pm = doc[n].get_pixmap(dpi=96)
            bg = Image.frombytes("RGB", (pm.width, pm.height), pm.samples).resize(ink.size)
            Image.fromarray(np.minimum(np.array(bg), np.array(ink))).save(target)
        else:
            shutil.copy2(png, target)
        kept.append(target.name)
    photos = []
    for j in sorted((src / "media").glob("*.jpg")):
        dst = out / f"photo_{j.name.split('@')[0].zfill(3)}.jpg"
        shutil.copy2(j, dst)
        photos.append(dst.name)
    meta = {"uuid": uuid, "title": row["title"], "path": row["path"], "pages": kept, "photos": photos, "pdf_slides": len(doc) if doc else 0}
    (out / "pages.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    return meta


def pick():
    rs = list(rows().values())
    cs = [r for r in rs if r["path"][:2] in (["Folders", "מדעי המחשב"], ["Folders", "מדעי המחשב 2"])]
    cases = {
        "strokes only": [r for r in cs if r["has_stroke"] and not r["has_image"] and not r["has_pdf"] and 3 <= r["spi"] <= 8],
        "PDF annotated": [r for r in cs if r["has_pdf"]],
        "with photos": [r for r in cs if r["has_image"] and not r["has_pdf"]],
    }
    for k, v in cases.items():
        v.sort(key=lambda r: r["bytes"])
        r = v[len(v) // 2] if v else None
        print(k, len(v), "->", (r["uuid"], "/".join(r["path"][-3:]), r["title"], r["spi"], r["jpg"]) if r else None)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "list":
        list_notes()
    elif cmd == "pack":
        for u in sys.argv[2:]:
            print(pack(u))
    elif cmd == "render":
        for u in sys.argv[2:]:
            for p in render(u):
                print(p)
    elif cmd == "pages":
        for u in sys.argv[2:]:
            m = render_note(u)
            print(u, m["title"], len(m["pages"]), "inked pages,", len(m["photos"]), "photos,", m["pdf_slides"], "slides")
    elif cmd == "all":  # every note, current degree and master's first; resumable (skips notes already in pages.jsonl)
        done_file = CAT / "pages.jsonl"
        done = {json.loads(l)["uuid"] for l in open(done_file, encoding="utf-8")} if done_file.exists() else set()
        order = sorted(rows().values(), key=lambda r: (0 if r["path"][:2] in (["Folders", "מדעי המחשב"], ["Folders", "מדעי המחשב 2"]) else 1, r["path"]))
        for i, r in enumerate(order, 1):
            if r["uuid"] in done:
                continue
            try:
                m = render_note(r["uuid"])
            except Exception as e:  # keep going: one broken note must not stop 1,100 others
                m = {"uuid": r["uuid"], "title": r["title"], "path": r["path"], "pages": [], "photos": [], "pdf_slides": 0, "error": str(e)[:300]}
            with open(done_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
            if i % 25 == 0:
                print(f"{i}/{len(order)}", flush=True)
    elif cmd == "pick":
        pick()
    else:
        print(__doc__)
