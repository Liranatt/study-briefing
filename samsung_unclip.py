"""Find Samsung note pages whose drawing extends beyond the page rectangle (sdocx-cli clips it) and re-render them whole.

    python -X utf8 samsung_unclip.py scan <worker> <workers>   check every inked page; re-render the clipped ones
    python -X utf8 samsung_unclip.py one <note dir> <page>      check and re-render a single page (1-based)

For each page: export SVG with sdocx-cli, measure the extents of every line/path/image, and if anything lies outside
the page rectangle, remove the clip, widen the viewBox to the content and rasterise with PyMuPDF to
  pages\\<dir>\\pNNN.png  (about 1400 px on the long side of the page width) and  hi\\<dir>\\pNNN.png (about 2x).
A page that is not clipped is left untouched. Results are appended to catalog\\samsung\\unclipped.jsonl.
"""
import json
import os
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
LOCAL = pathlib.Path(os.environ["LOCALAPPDATA"]) / "study-briefing" / "samsung"
STATE = pathlib.Path(os.environ["LOCALAPPDATA"]) / "Packages" / "SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy" / "LocalState"
CLI = pathlib.Path.home() / ".cargo" / "bin" / "sdocx-cli.exe"
OUT = HERE / "catalog" / "samsung" / "unclipped.jsonl"
MATERIAL = 0.04  # re-render only when content lies more than 4% of the page size outside it
TOL = 3.0  # svg units of slack before a page counts as clipped


def export_svg(dirname, number):
    tmp = LOCAL / "svg_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    svg = tmp / f"{dirname}_{number}_{os.getpid()}.svg"
    run = subprocess.run([str(CLI), str(LOCAL / "sdocx" / f"{dirname}.sdocx"), "-o", str(svg), "--pages", str(number)],
                         capture_output=True, text=True, encoding="utf-8")
    if run.returncode != 0 or not svg.exists():
        raise RuntimeError(run.stderr[-200:])
    text = svg.read_text(encoding="utf-8")
    svg.unlink()
    return text


def extents(svg):
    xs, ys = [], []
    for m in re.finditer(r"<line\b[^>]*>", svg):
        t = m.group(0)
        for k in ("x1", "x2"):
            v = re.search(rf'\b{k}="([-\d.eE+]+)"', t)
            if v:
                xs.append(float(v.group(1)))
        for k in ("y1", "y2"):
            v = re.search(rf'\b{k}="([-\d.eE+]+)"', t)
            if v:
                ys.append(float(v.group(1)))
    for m in re.finditer(r'<(?:path|polyline|polygon)\b[^>]*?\b(?:d|points)="([^"]+)"', svg):
        nums = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", m.group(1))]
        xs += nums[0::2]
        ys += nums[1::2]
    for m in re.finditer(r"<image\b[^>]*>", svg):
        t = m.group(0)
        g = {k: re.search(rf'\b{k}="([-\d.eE+]+)"', t) for k in ("x", "y", "width", "height")}
        if all(g.values()):
            x, y, w, h = (float(g[k].group(1)) for k in ("x", "y", "width", "height"))
            xs += [x, x + w]
            ys += [y, y + h]
    return xs, ys


def check_page(dirname, number):
    svg = export_svg(dirname, number)
    vb = re.search(r'viewBox="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"', svg)
    W, H = float(vb.group(3)), float(vb.group(4))
    xs, ys = extents(svg)
    if not xs or not ys:
        return None
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    if minx >= -TOL and miny >= -TOL and maxx <= W + TOL and maxy <= H + TOL:
        return {"clipped": False, "page": [W, H]}
    pad = 12
    x0, y0 = min(0.0, minx) - pad, min(0.0, miny) - pad
    x1, y1 = max(W, maxx) + pad, max(H, maxy) + pad
    return {"clipped": True, "page": [W, H], "box": [x0, y0, x1, y1], "svg": svg}


def rasterise(svg, box, long_side):
    import fitz
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    svg = re.sub(r'clip-path="url\(#[^)]*\)"', "", svg)
    svg = re.sub(r'<svg\b[^>]*>', f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="{x0} {y0} {w} {h}">', svg, count=1)
    doc = fitz.open(stream=svg.encode("utf-8"), filetype="svg")
    page = doc[0]
    zoom = long_side / max(page.rect.width, page.rect.height)
    pm = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    doc.close()
    return pm


def fix_page(dirname, number):
    r = check_page(dirname, number)
    if not r or not r["clipped"]:
        return r
    W, H = r["page"]
    x0, y0, x1, y1 = r["box"]
    over = max(-(x0 + 12) / W, (x1 - 12 - W) / W, -(y0 + 12) / H, (y1 - 12 - H) / H)
    r["over"] = round(over, 3)
    if over <= MATERIAL:  # a few strokes poke out by a hair: the page as drawn is complete enough
        r["clipped"] = "minor"
        r.pop("svg")
        return r
    scale_up = max((x1 - x0) / W, (y1 - y0) / H)
    base = 1400 * max(1.0, scale_up if (x1 - x0) / W >= (y1 - y0) / H else scale_up)
    (LOCAL / "pages" / dirname).mkdir(parents=True, exist_ok=True)
    (LOCAL / "hi" / dirname).mkdir(parents=True, exist_ok=True)
    rasterise(r["svg"], r["box"], min(base, 2400)).save(LOCAL / "pages" / dirname / f"p{number:03d}.png")
    rasterise(r["svg"], r["box"], min(base * 1.8, 3800)).save(LOCAL / "hi" / dirname / f"p{number:03d}.png")
    r.pop("svg")
    return r


def scan(worker, workers):
    rows = [json.loads(l) for l in open(HERE / "catalog" / "samsung" / "pages.jsonl", encoding="utf-8")]
    maps = {json.loads(l)["uuid"]: json.loads(l) for l in open(HERE / "catalog" / "samsung" / "map.jsonl", encoding="utf-8")}
    done = set()
    if OUT.exists():
        for l in open(OUT, encoding="utf-8"):
            j = json.loads(l)
            done.add((j["dir"], j["page"]))
    todo = [(maps[r["uuid"]]["dir"], int(f[1:4])) for r in rows for f in r["pages"]]
    n = 0
    for i, (d, p) in enumerate(todo):
        if i % workers != worker or (d, p) in done:
            continue
        if any((STATE / "wdoc" / d / "media").glob("*.pdf")):  # annotated lecture: the page is the slide, leave it
            continue
        try:
            res = fix_page(d, p)
            rec = {"dir": d, "page": p, "clipped": (res or {}).get("clipped", False), "over": (res or {}).get("over"), "box": res.get("box") if res else None, "page_size": res.get("page") if res else None}
        except Exception as e:
            rec = {"dir": d, "page": p, "error": str(e)[:120]}
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n += 1
        if n % 100 == 0:
            print(worker, n, flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "scan":
        scan(int(sys.argv[2]), int(sys.argv[3]))
    elif cmd == "one":
        r = fix_page(sys.argv[2], int(sys.argv[3]))
        print({k: v for k, v in (r or {}).items() if k != "svg"})
    else:
        print(__doc__)
