# -*- coding: utf-8 -*-
"""בדיקת קבצי השאלות היומיות (kb/questions/<course>.json).

הרצה:  python -X utf8 -I questions_validate.py [קובץ ...]
בלי ארגומנטים בודק את כל הקבצים ב-kb/questions.
קוד יציאה 0 אם אין שגיאות, 1 אם יש. אזהרות לא משנות את קוד היציאה.
"""
import json, os, re, sys, glob, collections

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(HERE, "catalog")
QDIR = os.path.join(HERE, "kb", "questions")
PREFIX = {"computational-models": "cm", "nlp": "nlp", "geometric-learning-theory": "glt"}
CURRENT_YEAR = "2026-27"


def load_catalog():
    files = {}
    with open(os.path.join(CATALOG, "files.jsonl"), encoding="utf8") as f:
        for line in f:
            r = json.loads(line)
            files[r["id"]] = r
    return files


_pages_cache = {}


def pages_of(fid):
    if fid in _pages_cache:
        return _pages_cache[fid]
    p = os.path.join(CATALOG, "text", fid + ".txt")
    nums = None
    if os.path.exists(p):
        with open(p, encoding="utf8") as f:
            nums = {int(m) for m in re.findall(r"^=== page (\d+) ===", f.read(), re.M)}
    _pages_cache[fid] = nums
    return nums


def check_question(q, course, files, errs, warns, where):
    def E(msg): errs.append(f"{where} {q.get('id')}: {msg}")
    def W(msg): warns.append(f"{where} {q.get('id')}: {msg}")
    for k in ("id", "q", "choices", "answer", "explain", "src", "topic"):
        if k not in q:
            E(f"חסר שדה {k}")
            return
    if not re.fullmatch(PREFIX[course] + r"-\d{4}", q["id"]):
        E("מזהה לא בפורמט <קורס>-<4 ספרות>")
    ch = q["choices"]
    if not (isinstance(ch, list) and len(ch) == 4 and all(isinstance(c, str) and c.strip() for c in ch)):
        E("חייבות להיות בדיוק ארבע תשובות לא ריקות")
    elif len(set(c.strip() for c in ch)) != 4:
        E("שתי תשובות זהות")
    if not (isinstance(q["answer"], int) and 0 <= q["answer"] <= 3):
        E("answer מחוץ לטווח 0-3")
    for k in ("q", "explain", "topic"):
        if not isinstance(q[k], str) or not q[k].strip():
            E(f"{k} ריק")
    texts = [q["q"], q["explain"]] + list(ch)
    for t in texts:
        if t.count("$") % 2:
            E("מספר סימני $ אי-זוגי (נוסחה לא סגורה): " + t[:50])
        if re.search(r"<[a-zA-Z/]", re.sub(r"\$[^$]*\$", "", t)):
            W("נראה כמו HTML: " + t[:50])
    if re.search(r"\b(תשובה|סעיף)\s*[א-דA-D]\b", q["explain"]):
        W("ההסבר מפנה לאות של תשובה")
    n_sent = len([s for s in re.split(r"(?<=[.!?])\s+", q["explain"].strip()) if s])
    if n_sent > 4:
        W(f"ההסבר ארוך ({n_sent} משפטים)")
    s = q["src"]
    if not (isinstance(s, dict) and isinstance(s.get("file"), str) and isinstance(s.get("page"), int)):
        E("src חייב להכיל file (מחרוזת) ו-page (מספר שלם)")
        return
    fid, pg = s["file"], s["page"]
    r = files.get(fid)
    if r is None:
        E(f"קובץ {fid} אינו בקטלוג")
        return
    if r.get("course") != course:
        E(f"הקובץ {fid} שייך לקורס {r.get('course')}, לא ל-{course}")
    if r.get("year") != CURRENT_YEAR:
        W(f"הקובץ {fid} מהופעה {r.get('year')} ולא {CURRENT_YEAR} (עובדות על הסמסטר הנוכחי רק ממקורות שלו)")
    nums = pages_of(fid)
    if nums is None:
        E(f"אין קובץ טקסט לקובץ {fid}")
    elif pg not in nums:
        E(f"עמוד {pg} לא קיים בקובץ {fid} (יש {min(nums)}-{max(nums)})")


def check_file(path, files):
    errs, warns = [], []
    try:
        with open(path, encoding="utf8") as f:
            d = json.load(f)
    except Exception as ex:
        return [f"{path}: JSON לא תקין: {ex}"], [], {}
    name = os.path.basename(path)
    course = d.get("course")
    if course not in PREFIX:
        errs.append(f"{name}: course לא מוכר: {course}")
        return errs, warns, {}
    if os.path.splitext(name)[0] != course:
        errs.append(f"{name}: שם הקובץ לא תואם ל-course")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d.get("generated", ""))):
        errs.append(f"{name}: generated חסר או לא בפורמט YYYY-MM-DD")
    sets, bank = d.get("sets"), d.get("bank")
    if not isinstance(sets, list) or not isinstance(bank, list):
        errs.append(f"{name}: חייבים sets ו-bank כרשימות")
        return errs, warns, {}
    allq, dates = [], []
    for i, st in enumerate(sets):
        where = f"{name} set[{i}]"
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(st.get("date", ""))):
            errs.append(f"{where}: date לא תקין")
        dates.append(st.get("date"))
        if not str(st.get("title", "")).strip():
            errs.append(f"{where}: title ריק")
        qs = st.get("questions", [])
        if len(qs) != 8:
            errs.append(f"{where}: {len(qs)} שאלות במקום 8")
        for q in qs:
            check_question(q, course, files, errs, warns, where)
            allq.append(q)
    if len(set(dates)) != len(dates):
        errs.append(f"{name}: תאריכים כפולים בין הסטים")
    for q in bank:
        check_question(q, course, files, errs, warns, f"{name} bank")
        allq.append(q)
    ids = [q.get("id") for q in allq]
    dup = [k for k, v in collections.Counter(ids).items() if v > 1]
    if dup:
        errs.append(f"{name}: מזהים כפולים: {dup}")
    qtexts = [re.sub(r"\s+", " ", q.get("q", "")).strip() for q in allq]
    duptxt = [k[:50] for k, v in collections.Counter(qtexts).items() if v > 1]
    if duptxt:
        errs.append(f"{name}: נוסח שאלה כפול: {duptxt}")
    pos = collections.Counter(q.get("answer") for q in allq if isinstance(q.get("answer"), int))
    stats = {"sets": sum(len(s.get("questions", [])) for s in sets), "bank": len(bank), "answer_positions": dict(sorted(pos.items()))}
    if allq and max(pos.values()) > 0.4 * len(allq):
        warns.append(f"{name}: התפלגות מיקום התשובה הנכונה לא מאוזנת: {dict(pos)}")
    return errs, warns, stats


def main(argv):
    files = load_catalog()
    paths = argv or sorted(glob.glob(os.path.join(QDIR, "*.json")))
    paths = [p for p in paths if os.path.basename(p) != "README.json"]
    bad = False
    for p in paths:
        errs, warns, stats = check_file(p, files)
        print(f"== {os.path.basename(p)}: {stats}")
        for w in warns:
            print("  אזהרה:", w)
        for e in errs:
            print("  שגיאה:", e)
        bad = bad or bool(errs)
    print("נכשל" if bad else "תקין")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
