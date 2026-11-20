"""Extract the official prerequisite table ("קורסי קדם") from the department yearbook PDF (ShnatonCS2026.pdf).

    python -X utf8 official_prereqs.py <pdf> [out json]

Rows are recovered from word positions: each course row starts with a number like 232-12011 in the right column; the
prerequisite numbers sit in the left column in the same row band. Output kb/official_prereqs.json:
  {"courses": {number: {"name", "credits", "prereqs":[numbers], "pages":[..]}}}
Names are matched to catalog course keys later by name. Check the output against the PDF: this is layout parsing.
"""
import json
import re
import sys
import fitz

NUM = re.compile(r"^\d{3}-\d{5}$")


def rows_of(page):
    W = page.rect.width
    words = page.get_text("words")  # x0,y0,x1,y1,word
    nums = [(w[1], w[0], w[4]) for w in words if NUM.match(w[4])]
    right = sorted([n for n in nums if n[1] > W * 0.62])  # course-number column
    left = [n for n in nums if n[1] < W * 0.30]  # prerequisite column
    out = []
    for i, (y, x, num) in enumerate(right):
        y_end = right[i + 1][0] - 2 if i + 1 < len(right) else y + 60
        band = [w for w in words if y - 4 <= w[1] < y_end + 1]
        name = " ".join(w[4] for w in sorted(band, key=lambda w: -w[0]) if W * 0.28 < w[0] < W * 0.62 and not re.match(r"^[\d.]+$", w[4]))
        credits = [w[4] for w in band if re.match(r"^\d\.\d$", w[4]) and W * 0.18 < w[0] < W * 0.28]
        prereq = [p[2] for p in left if y - 4 <= p[0] < y_end + 1]
        out.append({"number": num, "name": name.strip(), "credits": credits[0] if credits else None, "prereqs": prereq})
    return out


def main(pdf, out="kb/official_prereqs.json"):
    doc = fitz.open(pdf)
    courses = {}
    for pn, page in enumerate(doc, 1):
        for r in rows_of(page):
            c = courses.setdefault(r["number"], {"name": r["name"], "credits": r["credits"], "prereqs": [], "pages": []})
            if len(r["name"]) > len(c["name"]):
                c["name"] = r["name"]
            for p in r["prereqs"]:
                if p not in c["prereqs"]:
                    c["prereqs"].append(p)
            c["pages"].append(pn)
    json.dump({"source": pdf, "courses": courses}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(len(courses), "courses ->", out)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1], *(sys.argv[2:3]))
