"""High-resolution versions of Samsung note pages, for the careful second reading.

    python -X utf8 samsung_hires.py page <note dir> <page number>     e.g.  1e13e6d9-ef7c-a745-0000-018d23bd0af2 2   (p002)
    python -X utf8 samsung_hires.py sample <sample json>              add a `hi` path to every item of an audit sample file

The page is exported as a vector PDF by sdocx-cli and rasterised at 200 dpi (about 2500 px wide), so the strokes are
sharp when an agent crops and zooms into them. Photos (photo_NNN.jpg) are already full size.
"""
import json
import os
import pathlib
import subprocess
import sys

LOCAL = pathlib.Path(os.environ["LOCALAPPDATA"]) / "study-briefing" / "samsung"
STATE = pathlib.Path(os.environ["LOCALAPPDATA"]) / "Packages" / "SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy" / "LocalState"
CLI = pathlib.Path.home() / ".cargo" / "bin" / "sdocx-cli.exe"


def hires_page(dirname, number, dpi=200):
    import fitz
    out = LOCAL / "hi" / dirname
    out.mkdir(parents=True, exist_ok=True)
    png = out / f"p{number:03d}.png"
    if png.exists():
        return png
    pdf = out / f"p{number:03d}.pdf"
    run = subprocess.run([str(CLI), str(LOCAL / "sdocx" / f"{dirname}.sdocx"), "-o", str(pdf), "--pages", str(number)],
                         capture_output=True, text=True, encoding="utf-8")
    if run.returncode != 0 or not pdf.exists():
        raise RuntimeError(run.stderr[-300:])
    doc = fitz.open(pdf)
    pm = doc[0].get_pixmap(dpi=dpi)
    slides = sorted((STATE / "wdoc" / dirname / "media").glob("*.pdf"))
    if slides:  # an annotated lecture: lay the ink over the matching slide
        import numpy as np
        from PIL import Image
        sl = fitz.open(slides[0])
        if number - 1 < len(sl):
            ink = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
            sp = sl[number - 1].get_pixmap(matrix=fitz.Matrix(pm.width / sl[number - 1].rect.width, pm.height / sl[number - 1].rect.height))
            bg = Image.frombytes("RGB", (sp.width, sp.height), sp.samples).resize(ink.size)
            Image.fromarray(np.minimum(np.array(bg), np.array(ink))).save(png)
            sl.close()
            doc.close()
            try:
                pdf.unlink()
            except OSError:
                pass
            return png
        sl.close()
    pm.save(png)
    doc.close()
    try:
        pdf.unlink()
    except OSError:
        pass
    return png


def hi_for(img):
    p = pathlib.Path(img)
    if p.suffix.lower() == ".jpg":
        return p
    return hires_page(p.parent.name, int(p.stem[1:]))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "page":
        print(hires_page(sys.argv[2], int(sys.argv[3])))
    elif cmd == "sample":
        f = pathlib.Path(sys.argv[2])
        items = json.load(open(f, encoding="utf-8"))
        for it in items:
            it["hi"] = str(hi_for(it["img"]))
        json.dump(items, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(len(items), "items updated")
    elif cmd == "all":  # python samsung_hires.py all <batches json> [start step]: make every page's hi-res, in batch order
        batches = json.load(open(sys.argv[2], encoding="utf-8"))
        start, step = (int(sys.argv[3]), int(sys.argv[4])) if len(sys.argv) > 4 else (0, 1)
        n = bad = 0
        for bi, b in enumerate(batches):
            if bi % step != start:
                continue
            for it in b:
                try:
                    hi_for(it["image"])
                    n += 1
                except Exception as e:
                    bad += 1
                    print("fail", it["image"][-60:], str(e)[:80], flush=True)
            print("batch", bi, "done; images", n, "failed", bad, flush=True)
    else:
        print(__doc__)
