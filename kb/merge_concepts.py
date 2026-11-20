"""Merges the course agents' concepts into one knowledge map (see templates\\central_agent.md).

    python -X utf8 kb\\merge_concepts.py snapshot                 # once per run, before the first build
    python -X utf8 kb\\merge_concepts.py candidates [out.json]   # step 2: proposes keys that may be one concept
    python -X utf8 kb\\merge_concepts.py build                   # steps 1, 3, 4, 5: writes the outputs

Reads (never changes):  kb\\courses\\<key>.json (course summaries), kb\\courses\\<key>.prereqs.json (what each course
                        stands on: items checked by validate_prereqs.py), catalog\\files.jsonl, catalog\\filed.jsonl
                        (where each copy came from), catalog\\courses.json, catalog\\by_course\\*.json,
                        catalog\\text\\<id>.txt (only forum files, to tell announcement forums apart)
Merge decisions:        kb\\concept_aliases.json   {old key: canonical key}, written by the central agent
Cycle decisions:        kb\\concept_cycle_breaks.json   which depends_on edge of each cycle is dropped, and why
Writes:                 kb\\concepts.json, kb\\map.json, kb\\central_report.md,
                        kb\\scratch\\central_course_stats.json (per-course numbers, for the next run's comparison)

Prerequisite lists (kb\\courses\\<key>.prereqs.json):
- every item becomes a used_in entry with its source ("from": "prereqs", where, used_how, weight, learned_in, note);
  used_in entries from the summaries carry "from": "summary" and their first appears_at source;
- an item with a key that no summary has becomes a concept of its own (no definition, so first_learned is null);
- in map.json every course pair has one edge: count and examples come from the summaries as before, weight is the sum
  of the prereq item weights and prereq_items lists them. The edge starts at the item's learned_in course when that
  course was learned no later than the use; otherwise (no claim, or a claim later than the use) at the earliest course
  that defines the concept before the use, marked "basis": "map";
- map.json course_paths is the tree of courses each course stands on (breadth first: depth 1 = direct edges), and
  every concept gets roots: up to 5 chains of keys down through depends_on to concepts with no depends_on.
Previous run:           `snapshot` copies the current outputs to kb\\scratch\\central_prev\\; `build` compares against
                        that copy and writes the section "מה השתנה מהריצה הקודמת" of the report.

Source rules (from Liran):
- Offerings of the same course are never mixed. An occurrence gets the year and semester of its own source file;
  a file that sits in two offerings of the course (the same file id in both folders) counts for the earliest one.
- Forum content counts only when it is a staff announcement (a Moodle "news" forum, where only staff post) or a
  staff clarification that the central agent checked by hand (STAFF_CLARIFICATIONS). Any other forum reference is
  left out: a definition that rests on it is treated as a use, and evidence that rests only on it is not shown.
"""
import collections
import datetime
import difflib
import glob
import itertools
import json
import math
import pathlib
import re
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
KB = HERE / "kb"
PREV_DIR = KB / "scratch" / "central_prev"
STATS_PATH = KB / "scratch" / "central_course_stats.json"
SEM_ORDER = {"א": 0, "ב": 1, "קיץ": 2}
FORUM_DIR = "הודעות ופורומים"
EXTERNAL_DIR = "חומר מאתרים חיצוניים"

# Courses whose summary did not pass validate.py in this run (given to the central agent).
# Run of 19.11.2026 (incremental Samsung Notes pass + learned_in/ML/via_chain fixes):
# all 48 summaries in kb\courses validated, so the list is empty.
NOT_VALIDATED = []

# Courses whose prerequisite list failed or did not pass validate_prereqs.py in this run (given to the central agent).
# Run of 19.11.2026: none. Courses with no prereqs file at all are found by the code.
PREREQ_NOT_VALIDATED = []

# What happened between the previous run and this one (the first paragraph of "מה השתנה מהריצה הקודמת").
RUN_NOTE = ("בריצה של 19.11.2026 שולבו בהדרגה מאות סיכומי Samsung Notes מתומללים לתוך `kb\\courses\\*.prereqs.json` "
            "בלי למחוק פריטים קיימים; תוקנו שיוכי `learned_in` (בעיקר העברת קדם ML מ-`deep-learning-intro` ל-"
            "`intro-ml` עבור `graph-learning`, תיקון חריגות סמסטרים ב-`calculus-1-eng` ומפתחות ב-`neural-networks-intro`, "
            "החלפת שיוכים שטחיים ממדעי הקוגניציה/פילוסופיה/בלשנות ו-`algorithms-probability-basics` לקורסי המתמטיקה, "
            "התכנות והמדמ\"ח שבהם הנושא נלמד לעומק, והשארת בסיס הבלשנות/תחביר/סמנטיקה ב-`nlp`), והורחבו נתיבי "
            "`via` בדוח כך שיציגו את השרשרת חוליה-אחר-חוליה במסלולים בעומק ≥ 2.")

CYCLE_BREAKS_PATH = KB / "concept_cycle_breaks.json"
ROOTS_MAX_DEPTH = 8
ROOTS_PER_CONCEPT = 5

# Courses whose deep paths the report shows: the current semester first, then the courses Liran asked to check.
FOCUS_COURSES = ["computational-models", "nlp", "geometric-learning-theory", "research",
                 "graph-learning", "algo-trading", "deep-learning-intro", "intro-ml"]
# Ancestors each course should have (kb\next_map_pass.md); the report checks them.
EXPECTED_ANCESTORS = {
    "graph-learning": [("אלגברה", ["algebra-1-cs", "algebra-2-cs"]),
                       ("חדו\"א", ["calculus-1", "calculus-1-eng", "calculus-2-cs"]),
                       ("הסתברות וסטטיסטיקה", ["probability-1-stat", "probability-cs", "prob-stat-cs"]),
                       ("אופטימיזציה", ["optimization-ds"]),
                       ("למידת מכונה", ["intro-ml"]),
                       ("למידה עמוקה", ["deep-learning-intro"])],
    "nlp": [("בלשנות", ["linguistics-intro"]), ("תחביר", ["syntax-nl"]), ("סמנטיקה", ["semantics-nl"]),
            ("למידת מכונה", ["intro-ml"]), ("למידה עמוקה", ["deep-learning-intro"])],
}
# Linear algebra and calculus basics whose first_learned the report shows (keys may be merged keys).
BASICS = ["linear-system", "gaussian-elimination", "matrix-multiplication", "invertible-matrix", "determinant",
          "vector-space", "subspace", "span", "linear-independence", "basis", "dimension", "rank",
          "linear-transformation", "kernel", "matrix-of-linear-map", "change-of-basis-matrix", "rank-nullity-theorem",
          "supremum", "sequence-limit", "cauchy-sequence", "limit-of-function", "function-continuity",
          "intermediate-value-theorem", "uniform-continuity", "derivative", "chain-rule", "mean-value-theorem",
          "exponential-function", "logarithm"]
WHERE_HE = {"submission": "הגשה", "project": "פרויקט", "exam": "מבחן", "quiz": "בוחן", "lecture": "הרצאה",
            "recitation": "תרגול"}
HOW_HE = {"proof": "הוכחה", "code": "קוד", "computation": "חישוב", "analysis": "ניתוח", "modeling": "מידול",
          "explanation": "הסבר"}

# Posts in discussion forums (not announcement forums) that the central agent read and found to be a staff
# clarification, by forum file id. Only these discussion-forum files count as a source.
STAFF_CLARIFICATIONS = {
    # filled by the central agent; see the report section on sources
}


# ---------------------------------------------------------------- step 1: collect

def load_files():
    """Every catalog row, by file id. The same file (same id) can sit in several folders, for example an exam that
    both offerings put on Moodle, so one id may have several rows. Each row gets `prov` from catalog\\filed.jsonl:
    'moodle' (downloaded from that offering's Moodle), 'url' (another download), 'local' (copied into the folder
    from Liran's own Downloads, so not a source of that offering by itself) or None (not recorded)."""
    prov = {}
    filed = HERE / "catalog" / "filed.jsonl"
    if filed.exists():
        with open(filed, encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                rel = r.get("dest", "").split("מדעי המחשב\\", 1)[-1]
                urls = r.get("urls") or []
                prov[rel] = ("moodle" if any("moodle" in u for u in urls) else "url" if urls
                             else "local" if r.get("from") else None)
    files = collections.defaultdict(list)
    with open(HERE / "catalog" / "files.jsonl", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            row["prov"] = prov.get(row.get("path", ""))
            files[row["id"]].append(row)
    return files


def load_courses():
    courses = {}
    for path in sorted(glob.glob(str(KB / "courses" / "*.json"))):
        if path.endswith(".prereqs.json"):
            continue
        data = json.load(open(path, encoding="utf-8"))
        courses[data["course"]["key"]] = data
    return courses


def tkey(year, semester):
    return (year, SEM_ORDER[semester])


_forum_kind = {}


def _forum_type(fid):
    if fid not in _forum_kind:
        try:
            head = open(HERE / "catalog" / "text" / f"{fid}.txt", encoding="utf-8").read(600)
        except OSError:
            head = ""
        m = re.search(r"פורום מסוג (\w+)", head)
        _forum_kind[fid] = m.group(1) if m else None
    return _forum_kind[fid]


_source_kind = {}


def source_kind(fid, files):
    """'news_forum': a staff announcement forum (or an attachment posted there).
    'staff_clarification': a discussion forum checked by hand (STAFF_CLARIFICATIONS).
    'forum': any other forum file or attachment; it does not count as a source.
    'external': a course website or public document linked from Moodle, fetched on the day of the pull.
    'course': everything else."""
    if fid not in _source_kind:
        _source_kind[fid] = _source_kind_uncached(fid, files)
    return _source_kind[fid]


def _source_kind_uncached(fid, files):
    rows = files.get(fid) or []
    path = rows[0].get("path", "") if rows else ""
    if FORUM_DIR in path:
        if fid in STAFF_CLARIFICATIONS:
            return "staff_clarification"
        if "קבצים מצורפים" in path:
            folder = path.split("קבצים מצורפים")[0]
            name = path.rsplit("\\", 1)[-1]
            for other in _forum_index(files).get(folder, []):
                if other in STAFF_CLARIFICATIONS or _forum_type(other) == "news":
                    try:
                        text = open(HERE / "catalog" / "text" / f"{other}.txt", encoding="utf-8").read()
                    except OSError:
                        continue
                    if name in text:
                        return "news_forum" if _forum_type(other) == "news" else "staff_clarification"
            return "forum"
        return "news_forum" if _forum_type(fid) == "news" else "forum"
    if EXTERNAL_DIR in path:
        return "external"
    return "course"


_forum_idx = {}


def _forum_index(files):
    """Forum .md files by their folder."""
    if not _forum_idx:
        for fid, rows in files.items():
            p = rows[0].get("path", "")
            if FORUM_DIR in p and p.endswith(".md") and "קבצים מצורפים" not in p:
                _forum_idx.setdefault(p.rsplit("\\", 1)[0] + "\\", []).append(fid)
        _forum_idx.setdefault("", [])
    return _forum_idx


def counts_as_source(ref, files):
    return source_kind(ref["file"], files) != "forum"


def occurrence_period(ck, course, concept, files):
    """Year and semester of one concept occurrence, from the course offerings.

    One offering: that one. Several: the offering of the defined_at file (catalog\\files.jsonl); for a used
    concept (no defined_at) the first appears_at file that matches an offering. A file that sits in several
    offerings of this course counts for the offerings whose copy came from Moodle (a copy filed from Liran's
    Downloads does not make it that offering's source), and among them for the earliest. Otherwise a file of the
    same year, and only then the earliest offering. Forum references that do not count as a source are skipped.
    """
    offs = sorted({(o["year"], o["semester"]) for o in course["offerings"]}, key=lambda p: tkey(*p))
    if len(offs) == 1:
        return offs[0][0], offs[0][1], "single_offering"
    refs = []
    if concept.get("defined_at"):
        refs.append(("defined_at", concept["defined_at"]))
    refs += [("appears_at", r) for r in concept.get("appears_at", [])]
    refs = [(how, r) for how, r in refs if counts_as_source(r, files)]

    def own_rows(ref):
        return [r for r in files.get(ref["file"], []) if r.get("course") in (ck, None)]

    for how, ref in refs:
        rows = [r for r in own_rows(ref) if (r.get("year"), r.get("semester")) in offs]
        sourced = [r for r in rows if r.get("prov") != "local"] or rows
        ps = sorted({(r.get("year"), r.get("semester")) for r in sourced}, key=lambda p: tkey(*p))
        if ps:
            note = ""
            if len({(r.get("year"), r.get("semester")) for r in rows}) > len(ps):
                note = "_local_copy_skipped"
            elif len(ps) > 1:
                note = "_shared"
            return ps[0][0], ps[0][1], how + note
    for how, ref in refs:
        years = {r.get("year") for r in own_rows(ref)}
        same_year = [o for o in offs if o[0] in years]
        if same_year:
            return same_year[0][0], same_year[0][1], how + "_year"
    return offs[0][0], offs[0][1], "earliest_offering"


def collect(courses, files):
    """Every concept occurrence in every course. A definition whose page is a forum post that does not count
    (see the module doc) becomes a use."""
    occs = []
    for ck, data in courses.items():
        for order, c in enumerate(data["concepts"]):
            year, semester, how = occurrence_period(ck, data, c, files)
            role, defined_at, statement, demoted = c["role"], c["defined_at"], c["statement"], False
            if role == "defined" and defined_at and not counts_as_source(defined_at, files):
                role, defined_at, statement, demoted = "used", None, "", True
            occs.append({
                "key": c["key"], "course": ck, "order": order, "role": role, "kind": c["kind"],
                "year": year, "semester": semester, "period_from": how, "demoted": demoted,
                "defined_at": defined_at, "statement": statement,
                "appears_at": [r for r in c.get("appears_at", []) if counts_as_source(r, files)],
                "src_kind": source_kind(defined_at["file"], files) if defined_at else None,
                "name_he": c["name_he"], "name_en": c["name_en"], "aliases": c["aliases"],
                "depends_on": c["depends_on"], "my_level": c["my_level"], "level_basis": c["level_basis"],
            })
    return occs


def load_prereqs():
    """kb\\courses\\<key>.prereqs.json, by course key."""
    out = {}
    for path in sorted(glob.glob(str(KB / "courses" / "*.prereqs.json"))):
        data = json.load(open(path, encoding="utf-8"))
        out[data["course"]] = data
    return out


def collect_prereqs(prereqs, courses, files, mapping):
    """Every prerequisite item as a use with a source. The year and semester are those of the item's source file, as
    for a summary occurrence. Keys resolve through the plain alias entries only: the prereq agents chose their keys
    from kb\\concepts.json, where a key that two courses used for two concepts was already split (for example
    `derivative` there is the calculus derivative, and the finance one is `financial-derivative`), so a course-scoped
    entry of the summaries must not move them."""
    uses = []
    for ck, d in sorted(prereqs.items()):
        if ck not in courses:
            print("warning: prereqs for a course with no summary:", ck)
            continue
        for order, it in enumerate(d["items"]):
            src = dict(it["src"])
            year, semester, how = occurrence_period(ck, courses[ck], {"defined_at": None, "appears_at": [src]}, files)
            li = it.get("learned_in") or None
            uses.append({
                "key": canon(mapping, it["concept"]), "orig_key": it["concept"], "course": ck, "order": order,
                "year": year, "semester": semester, "period_from": how, "src": src,
                "src_kind": source_kind(src["file"], files),
                "where": it["where"], "used_how": it["used_how"], "weight": it["weight"],
                "learned_in": ({"course": li["course"], "year": li["year"], "semester": li["semester"]} if li else None),
                "note": it.get("note", ""), "new_key": bool(it.get("new_key")),
                "name_he": it.get("name_he", ""), "name_en": it.get("name_en", ""),
            })
    return uses


def prereq_pseudo_occs(puses, occs):
    """Prereq keys that no summary has, shaped like occurrences (for `candidates`)."""
    have = {o["key"] for o in occs}
    out, seen = [], set()
    for u in puses:
        if u["orig_key"] in have or u["orig_key"] in seen:
            continue
        seen.add(u["orig_key"])
        out.append({"key": u["orig_key"], "course": u["course"], "role": "used", "kind": "other",
                    "name_he": u["name_he"], "name_en": u["name_en"], "aliases": [], "statement": u["note"]})
    return out


# ---------------------------------------------------------------- step 2: candidates

def norm(text):
    text = text.lower().replace("’", "'").replace("״", '"')
    text = re.sub(r"[^0-9a-z֐-׿]+", " ", text)
    return " ".join(text.split())


def stem(token):
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


GENERIC = {"theorem", "rule", "law", "lemma", "principle", "method", "algorithm", "formula", "property",
           "definition", "problem", "of", "the", "a", "an", "and", "in", "for", "on"}


def candidates(occs):
    by_key = collections.defaultdict(list)
    for o in occs:
        by_key[o["key"]].append(o)
    names = {}
    for k, os_ in by_key.items():
        s = {norm(k.replace("-", " "))}
        for o in os_:
            s.add(norm(o["name_en"]))
            s.add(norm(o["name_he"]))
            s.update(norm(a) for a in o["aliases"])
        names[k] = {x for x in s if len(x.replace(" ", "")) >= 2}
    keys = sorted(by_key)
    index = collections.defaultdict(set)
    for k in keys:
        for n in names[k]:
            index[n].add(k)
    pairs = collections.defaultdict(set)
    for n, ks in index.items():
        for a, b in itertools.combinations(sorted(ks), 2):
            pairs[(a, b)].add("name:" + n)
    toks = {k: [stem(t) for t in k.split("-")] for k in keys}
    core = {k: frozenset(t for t in toks[k] if t not in GENERIC) for k in keys}
    initials = {k: "".join(t[0] for t in k.split("-") if t not in {"of", "the", "a", "an", "and"}) for k in keys}
    for a, b in itertools.combinations(keys, 2):
        why = []
        if set(toks[a]) == set(toks[b]):
            why.append("same_tokens")
        elif core[a] and core[a] == core[b]:
            why.append("same_core")
        elif (core[a] and core[b] and (core[a] < core[b] or core[b] < core[a])
              and abs(len(core[a]) - len(core[b])) == 1 and min(len(core[a]), len(core[b])) >= 2):
            why.append("core_subset")
        if "-" not in a and 2 <= len(a) <= 6 and a == initials[b] and "-" in b:
            why.append("acronym")
        if "-" not in b and 2 <= len(b) <= 6 and b == initials[a] and "-" in a:
            why.append("acronym")
        sm = difflib.SequenceMatcher(None, a, b)
        if sm.real_quick_ratio() >= 0.85 and sm.quick_ratio() >= 0.85 and sm.ratio() >= 0.85:
            why.append(f"similar:{sm.ratio():.2f}")
        if why:
            pairs[(a, b)].update(why)
    out = []
    for (a, b), why in sorted(pairs.items()):
        def info(k):
            os_ = by_key[k]
            o = os_[0]
            return {"key": k, "courses": sorted({x["course"] + ":" + x["role"][0] for x in os_}),
                    "name_en": o["name_en"], "name_he": o["name_he"], "kind": o["kind"],
                    "aliases": sorted({a_ for x in os_ for a_ in x["aliases"]})[:8],
                    "statement": (next((x["statement"] for x in os_ if x["statement"]), ""))[:160]}
        out.append({"why": sorted(why), "a": info(a), "b": info(b)})
    return out


# ---------------------------------------------------------------- step 3: build

def load_aliases(occs, extra_keys=()):
    """kb\\concept_aliases.json: {old key: canonical key}. A "course:key" entry applies to that course only.
    `extra_keys`: keys that the prerequisite lists use."""
    path = KB / "concept_aliases.json"
    raw = json.load(open(path, encoding="utf-8")) if path.exists() else {}
    mapping = {k: v for k, v in raw.items() if not k.startswith("_")}
    known = {o["key"] for o in occs} | set(extra_keys)
    known_scoped = {f"{o['course']}:{o['key']}" for o in occs}
    bad = [k for k in mapping if (k not in known_scoped if ":" in k else k not in known)]
    if bad:
        print("warning: aliases for keys that no course has:", bad)
    return mapping


def canon(mapping, key, course=None):
    """Canonical key of `key` as used in `course` (course-scoped entries first, then chains of plain entries)."""
    if course is not None and f"{course}:{key}" in mapping:
        key = mapping[f"{course}:{key}"]
    seen = set()
    while key in mapping and key not in seen:
        seen.add(key)
        key = mapping[key]
    return key


def src_of(defined_at):
    if not defined_at:
        return None
    return dict(defined_at)


def summary_used(c):
    """used_in entries that come from the course summaries (the meaning used_in had before the prerequisite lists:
    a course that uses the concept without defining it). Entries of an older run have no "from"."""
    return [u for u in c["used_in"] if u.get("from", "summary") == "summary"]


def prereq_used(c):
    return [u for u in c["used_in"] if u.get("from") == "prereqs"]


def build_concepts(occs, mapping, course_rank, puses=()):
    groups = collections.defaultdict(list)
    for o in occs:
        groups[canon(mapping, o["key"], o["course"])].append(o)
    pgroups = collections.defaultdict(list)
    for u in puses:
        pgroups[u["key"]].append(u)

    def okey(o):
        return (tkey(o["year"], o["semester"]), course_rank.get(o["course"], 999), o["order"])

    def puse_entry(u):
        return {"course": u["course"], "year": u["year"], "semester": u["semester"], "src": u["src"],
                "from": "prereqs", "where": u["where"], "used_how": u["used_how"], "weight": u["weight"],
                "learned_in": u["learned_in"]["course"] if u["learned_in"] else None, "note": u["note"]}

    def prereq_fields(ck):
        ps = sorted(pgroups.get(ck, []), key=okey)
        claims = collections.Counter()
        for u in ps:
            if u["learned_in"]:
                li = u["learned_in"]
                claims[(li["course"], li["year"], li["semester"])] += 1
        return ps, {"prereq_weight": sum(u["weight"] for u in ps),
                    "learned_in_claims": [{"course": c, "year": y, "semester": s, "n": n}
                                          for (c, y, s), n in sorted(claims.items(),
                                                                     key=lambda t: (tkey(t[0][1], t[0][2]), t[0][0]))]}

    def used_order(u):
        return (tkey(u["year"], u["semester"]), course_rank.get(u["course"], 999),
                0 if u["from"] == "summary" else 1, -u.get("weight", 0))

    concepts = []
    # a key that only a prerequisite list has: a concept with uses and no definition
    for ck in sorted(set(pgroups) - set(groups)):
        ps, extra = prereq_fields(ck)
        head = next((u for u in ps if u["orig_key"] == ck and u["name_he"]), None) or \
            next((u for u in ps if u["name_he"]), ps[0])
        aliases = []
        for u in ps:
            for a in (u["name_he"], u["name_en"]):
                if a and a not in (head["name_he"], head["name_en"]) and a not in aliases:
                    aliases.append(a)
        concepts.append({
            "key": ck, "name_he": head["name_he"] or ck, "name_en": head["name_en"], "aliases": aliases,
            "merged_keys": sorted({u["orig_key"] for u in ps} - {ck}), "kind": "other",
            "defined_in": [], "used_in": sorted((puse_entry(u) for u in ps), key=used_order),
            "first_learned": None, "statement": "", "depends_on": [], "levels": [], **extra,
        })
    for ck, os_ in groups.items():
        os_.sort(key=okey)
        defined = [o for o in os_ if o["role"] == "defined"]
        defined_courses = []
        defined_in = []
        for o in defined:
            if o["course"] in defined_courses:
                continue
            defined_courses.append(o["course"])
            defined_in.append({"course": o["course"], "year": o["year"], "semester": o["semester"],
                               "src": src_of(o["defined_at"])})
        used_in = []
        used_courses = []
        for o in os_:
            if o["role"] == "used" and o["course"] not in defined_courses and o["course"] not in used_courses:
                used_courses.append(o["course"])
                srcs = o["appears_at"]  # only references that count as a source (see collect)
                used_in.append({"course": o["course"], "year": o["year"], "semester": o["semester"],
                                "src": dict(srcs[0]) if srcs else None, "from": "summary"})
        ps, extra = prereq_fields(ck)
        used_in = sorted(used_in + [puse_entry(u) for u in ps], key=used_order)
        first = defined[0] if defined else None
        head = first or next((o for o in os_ if o["key"] == ck), os_[0])
        name_he, name_en = head["name_he"], head["name_en"]
        if first and first["key"] != ck:
            same = next((o for o in os_ if o["key"] == ck), None)
            if same:
                name_he, name_en = same["name_he"], same["name_en"]
        aliases = []
        for o in os_:
            for a in [o["name_he"], o["name_en"]] + list(o["aliases"]):
                if a and a not in (name_he, name_en) and a not in aliases:
                    aliases.append(a)
        if first:
            statement = first["statement"]
        else:
            statement = next((o["statement"] for o in os_ if o["statement"]), "")
        deps = []
        for o in os_:
            for d in o["depends_on"]:
                d = canon(mapping, d, o["course"])
                if d != ck and d not in deps:
                    deps.append(d)
        levels = []
        for o in os_:
            lv = {"course": o["course"], "level": o["my_level"], "basis": o["level_basis"]}
            if lv not in levels:
                levels.append(lv)
        merged_keys = sorted({o["key"] for o in os_ if o["key"] != ck} | {u["orig_key"] for u in ps if u["orig_key"] != ck})
        concepts.append({
            "key": ck, "name_he": name_he, "name_en": name_en, "aliases": aliases, "merged_keys": merged_keys,
            "kind": head["kind"],
            "defined_in": defined_in, "used_in": used_in,
            "first_learned": ({"course": first["course"], "year": first["year"], "semester": first["semester"],
                               "src": src_of(first["defined_at"])} if first else None),
            "statement": statement, "depends_on": deps, "levels": levels, **extra,
        })
    concepts.sort(key=lambda c: c["key"])
    return concepts


def merge_stats(occs, mapping):
    """Keys merged into another key (plain entries), the groups they form, and course-scoped splits."""
    keys = {o["key"] for o in occs}
    groups = collections.defaultdict(list)
    for k in sorted(mapping):
        if ":" not in k and k in keys and canon(mapping, k) != k:
            groups[canon(mapping, k)].append(k)
    merged = sum(len(v) for v in groups.values())
    groups = {k: [k] + v for k, v in groups.items()}
    splits = sorted((k, v) for k, v in mapping.items() if ":" in k)
    return merged, groups, splits


# ---------------------------------------------------------------- step 3b: cycles in depends_on, and roots

def load_cycle_breaks():
    """kb\\concept_cycle_breaks.json: {"breaks": [{"concept": a, "drop": b, "why": ...}]} means that `a` no longer
    depends on `b` (the reverse edge, b depends on a, stays). Written by the central agent."""
    if not CYCLE_BREAKS_PATH.exists():
        return []
    return json.load(open(CYCLE_BREAKS_PATH, encoding="utf-8")).get("breaks", [])


def find_cycles(concepts):
    """Strongly connected components of depends_on with more than one concept (Tarjan, iterative)."""
    dep = {c["key"]: [d for d in c["depends_on"]] for c in concepts}
    index, low, on, stack, comps = {}, {}, set(), [], []
    counter = 0
    for root in sorted(dep):
        if root in index:
            continue
        work = [(root, iter(dep[root]))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in dep:
                    continue
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(dep[w])))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    comps.append(sorted(comp))
    return sorted(comps)


def apply_cycle_breaks(concepts, breaks):
    """Drops the depends_on edges named in kb\\concept_cycle_breaks.json. Returns what was applied, entries that no
    longer match an edge, the two-concept cycles found before the breaks and the cycles that are left."""
    cidx = {c["key"]: c for c in concepts}
    dep = {c["key"]: set(c["depends_on"]) for c in concepts}
    before_pairs = sorted({tuple(sorted((a, b))) for a in dep for b in dep[a] if b in dep and a in dep[b]})
    before_sccs = find_cycles(concepts)
    applied, stale = [], []
    for b in breaks:
        c = cidx.get(b["concept"])
        if c is None or b["drop"] not in c["depends_on"]:
            stale.append(b)
            continue
        c["depends_on"] = [d for d in c["depends_on"] if d != b["drop"]]
        other = cidx.get(b["drop"])
        applied.append({"concept": b["concept"], "dropped": b["drop"],
                        "kept_reverse": bool(other and b["concept"] in other["depends_on"]),
                        "two_cycle": tuple(sorted((b["concept"], b["drop"]))) in before_pairs, "why": b["why"]})
    return {"applied": applied, "stale": stale, "two_cycles_before": before_pairs,
            "sccs_before": before_sccs, "left": find_cycles(concepts)}


def concept_time(c):
    fl = c and c["first_learned"]
    return tkey(fl["year"], fl["semester"]) if fl else (str(9999), 9)


def add_roots(concepts):
    """roots: up to ROOTS_PER_CONCEPT chains [concept, dependency, ..., root] down through depends_on, at most
    ROOTS_MAX_DEPTH steps. A root is a concept with no depends_on (or a key that no course defines or uses).

    Ranking: a chain that reaches a root within the limit beats one cut by the limit; then the heavier chain, where
    every concept after the first adds 1 + log(1 + prereq_weight), so concepts Liran used a lot in his own work and
    deeper chains win; then the chain whose root was learned earlier. The chosen chains start with different direct
    dependencies where possible (the branches the concept stands on), then end at different roots. A cycle left in
    depends_on is cut where the walk meets it."""
    cidx = {c["key"]: c for c in concepts}
    wt = {k: 1 + math.log1p(c.get("prereq_weight", 0)) for k, c in cidx.items()}
    memo = {}
    keep = 12

    def rank(x):
        s, done, rt, ch = x
        return (not done, -s, rt, ch)

    def best(n, left, path):
        """Best chains from n down: (score of the concepts after n, reaches a root, root time, chain)."""
        if (n, left) in memo and not (memo[(n, left)][1] & path):
            return memo[(n, left)][0]
        c = cidx.get(n)
        deps = [d for d in (c["depends_on"] if c else []) if d not in path]
        if not deps:
            res = [(0.0, True, concept_time(c), (n,))]
        elif left == 0:
            res = [(0.0, False, concept_time(c), (n,))]
        else:
            cands = []
            for d in deps:
                for s, done, rt, ch in best(d, left - 1, path | {n}):
                    cands.append((s + wt.get(d, 1.0), done, rt, (n,) + ch))
            cands.sort(key=rank)
            res = cands[:keep]
        memo[(n, left)] = (res, {x for r in res for x in r[3][1:]})
        return res

    for c in concepts:
        if not c["depends_on"]:
            c["roots"] = []
            continue
        cands = sorted(((s + wt.get(d, 1.0), done, rt, (c["key"],) + ch)
                        for d in c["depends_on"] for s, done, rt, ch in best(d, ROOTS_MAX_DEPTH - 1, frozenset({c["key"]}))),
                       key=rank)
        chosen = []
        for pick in (lambda ch: ch[1] not in {q[1] for q in chosen},      # a new branch
                     lambda ch: ch[-1] not in {q[-1] for q in chosen},    # a new root
                     lambda ch: ch not in chosen):
            for x in cands:
                if len(chosen) == ROOTS_PER_CONCEPT:
                    break
                if pick(x[3]):
                    chosen.append(x[3])
        c["roots"] = [list(ch) for ch in chosen]


# ---------------------------------------------------------------- step 4: map

def prereq_edge_from(u, c):
    """Where a prerequisite item's edge starts: (course, basis). The learned_in course when it was learned no later
    than the use (the same semester counts: Liran took the two courses together); otherwise the earliest course that
    defines the concept before the use. (None, reason) when there is no earlier course."""
    tu = tkey(u["year"], u["semester"])
    li = u["learned_in"]
    if li and li["course"] == u["course"]:
        return None, "self"
    if li and tkey(li["year"], li["semester"]) <= tu:
        return li["course"], "learned_in"
    for d in sorted((c or {}).get("defined_in", []), key=lambda d: tkey(d["year"], d["semester"])):
        if d["course"] != u["course"] and tkey(d["year"], d["semester"]) < tu:
            return d["course"], ("map_late_claim" if li else "map")
    return None, ("late_claim" if li else "none")


def build_map(concepts, occs, mapping, puses=()):
    known = {c["key"] for c in concepts}
    asserted = collections.defaultdict(set)
    for o in occs:
        for d in o["depends_on"]:
            asserted[(canon(mapping, o["key"], o["course"]), canon(mapping, d, o["course"]))].add(o["course"])
    concept_edges = []
    for c in concepts:
        for d in c["depends_on"]:
            concept_edges.append({"concept": c["key"], "depends_on": d, "resolved": d in known,
                                  "courses": sorted(asserted.get((c["key"], d), []))})
    # count and examples: from the summaries only, as before the prerequisite lists
    ncourses = {c["key"]: len({x["course"] for x in c["defined_in"]} | {x["course"] for x in summary_used(c)})
                for c in concepts}
    edges = collections.defaultdict(set)
    for c in concepts:
        for u in summary_used(c):
            tu = tkey(u["year"], u["semester"])
            for d in c["defined_in"]:
                if tkey(d["year"], d["semester"]) < tu:
                    edges[(d["course"], u["course"])].add(c["key"])
    # weight and prereq_items: from the prerequisite lists
    cidx = {c["key"]: c for c in concepts}
    pedges = collections.defaultdict(list)
    for u in puses:
        u["edge_from"], u["edge_basis"] = prereq_edge_from(u, cidx.get(u["key"]))
        if u["edge_from"]:
            pedges[(u["edge_from"], u["course"])].append(u)
    course_edges = []
    for (a, b) in set(edges) | set(pedges):
        ks = edges.get((a, b), set())
        ex = sorted(ks, key=lambda k: (-ncourses[k], k))[:8]
        items = sorted(pedges.get((a, b), []),
                       key=lambda u: (-u["weight"], u["key"], u["src"]["file"], u["src"].get("page") or 0))
        pitems = []
        for u in items:
            it = {"concept": u["key"], "where": u["where"], "used_how": u["used_how"], "weight": u["weight"],
                  "src": u["src"]}
            if u["edge_basis"] != "learned_in":
                it["basis"] = "map"
            pitems.append(it)
        course_edges.append({"from": a, "to": b, "count": len(ks), "examples": ex,
                             "weight": sum(u["weight"] for u in items),
                             "prereq_count": len({u["key"] for u in items}), "prereq_items": pitems})
    course_edges.sort(key=lambda e: (-e["weight"], -e["count"], e["from"], e["to"]))
    return {"concept_edges": concept_edges, "course_edges": course_edges}


def edge_via(e, n=6):
    """The concepts that carry a course edge: prereq concepts by total weight, then the summary examples."""
    tot = collections.Counter()
    for it in e.get("prereq_items", []):
        tot[it["concept"]] += it["weight"]
    out = [k for k, _ in sorted(tot.items(), key=lambda t: (-t[1], t[0]))]
    out += [k for k in e["examples"] if k not in tot]
    return out[:n]


def build_course_paths(course_edges, courses):
    """{course: [{ancestor, depth, via, weight, count, path}]}: the tree of courses a course stands on, breadth first
    over the course edges (depth 1: the courses it uses directly). An ancestor that several courses of the level below
    use hangs under the one with the heaviest edge; path is ancestor -> ... -> course, via the concepts on the
    ancestor's own edge, and weight and count are that edge's."""
    inc = collections.defaultdict(dict)
    for e in course_edges:
        inc[e["to"]][e["from"]] = e

    def strength(e):
        return (e["weight"], e["count"])

    out = {}
    for c in sorted(courses):
        tree, seen, frontier, depth = {}, {c}, [c], 0
        while frontier:
            depth += 1
            cand = {}
            for child in frontier:
                for a, e in inc.get(child, {}).items():
                    if a in seen:
                        continue
                    if a not in cand or strength(e) > strength(cand[a][1]):
                        cand[a] = (child, e)
            for a, (child, e) in cand.items():
                tree[a] = (depth, child, e)
            seen |= set(cand)
            frontier = sorted(cand)
        entries = []
        for a, (d, parent, e) in tree.items():
            path, x = [a], parent
            while x != c:
                path.append(x)
                x = tree[x][1]
            path.append(c)
            via_chain = [{"from": u, "to": v, "concepts": edge_via(inc[v][u], 4)} for u, v in zip(path, path[1:])]
            entries.append({"ancestor": a, "depth": d, "via": edge_via(e), "via_chain": via_chain,
                            "weight": e["weight"], "count": e["count"], "path": path})
        entries.sort(key=lambda t: (t["depth"], -t["weight"], -t["count"], t["ancestor"]))
        out[c] = entries
    return out


# ---------------------------------------------------------------- views shared by the report and the comparison

def ncourses_of(c):
    return len({x["course"] for x in c["defined_in"] + c["used_in"]})


def top30(concepts):
    return sorted(concepts, key=lambda c: (-ncourses_of(c), -len(c["defined_in"]), c["key"]))[:30]


def weak_multi(concepts):
    out = {}
    for c in concepts:
        wc = []
        for lv in c["levels"]:
            if lv["level"] == "weak" and lv["course"] not in wc:
                wc.append(lv["course"])
        if len(wc) > 1:
            out[c["key"]] = wc
    return out


def early_uses(concepts, prereqs=False):
    """Concepts used (in a summary, or with prereqs=True in a prerequisite list) before their first definition."""
    out = {}
    for c in concepts:
        fl = c["first_learned"]
        if not fl:
            continue
        before = [u for u in (prereq_used(c) if prereqs else summary_used(c))
                  if tkey(u["year"], u["semester"]) < tkey(fl["year"], fl["semester"])]
        if before:
            out[c["key"]] = before
    return out


def per_course_concepts(concepts):
    """{course: (canonical concepts touching it, of them defined there)} from a concepts list (summaries only)."""
    total, defined = collections.Counter(), collections.Counter()
    for c in concepts:
        for x in c["defined_in"]:
            defined[x["course"]] += 1
            total[x["course"]] += 1
        for x in summary_used(c):
            total[x["course"]] += 1
    return total, defined


THIN_HEAD = ("סף: פחות מ-5 יחידות, או פחות מ-35 מושגים, או פחות מ-20 קבצים שנקראו, או שאין בכלל עבודות ומבחנים, "
             "או שכל המושגים בלי רמה (`unknown`), או שפחות מ-40% מהמושגים מסומנים `defined` "
             "(הקורס הוא המקום שבו המושגים נלמדו, אבל סוכן הקורס לא מצא את ההגדרות בחומר).")


def course_stats(courses, files):
    """Per-course numbers for the thin-output table and for the next run's comparison."""
    out = {}
    for k, d in courses.items():
        nu, nc, na, ne, nr = len(d["units"]), len(d["concepts"]), len(d["assignments"]), len(d["exams"]), len(d["files_read"])
        unk = sum(1 for c in d["concepts"] if c["my_level"] == "unknown")
        nd = sum(1 for c in d["concepts"] if c["role"] == "defined")
        weak = sum(1 for c in d["concepts"] if c["my_level"] == "weak")
        why = []
        if nu < 5:
            why.append("מעט יחידות")
        if nc < 35:
            why.append("מעט מושגים")
        if nr < 20:
            why.append("מעט קבצים נקראו")
        if na == 0 and ne == 0:
            why.append("אין עבודות ומבחנים")
        if unk == nc:
            why.append("אין רמות")
        if nc and nd < 0.4 * nc:
            why.append("מעט הגדרות")
        kinds = collections.Counter()
        for ref in refs_in(d):
            kinds[source_kind(ref["file"], files)] += 1
        out[k] = {"units": nu, "concepts": nc, "defined": nd, "assignments": na, "exams": ne, "files_read": nr,
                  "gaps": len(d["gaps"]), "unknown_level": unk, "weak": weak, "thin": why,
                  "refs_news_forum": kinds["news_forum"], "refs_forum_other": kinds["forum"],
                  "refs_staff_clarification": kinds["staff_clarification"], "refs_external": kinds["external"]}
    return out


def refs_in(node):
    if isinstance(node, dict):
        if "file" in node and isinstance(node["file"], str):
            yield node
        for v in node.values():
            yield from refs_in(v)
    elif isinstance(node, list):
        for v in node:
            yield from refs_in(v)


# ---------------------------------------------------------------- step 5: report

def period(x):
    return f"{x['year']} {x['semester']}"


def load_prev():
    """The previous run's outputs, as copied by `snapshot`."""
    if not (PREV_DIR / "concepts.json").exists() or not (PREV_DIR / "map.json").exists():
        return None
    prev = {"concepts": json.load(open(PREV_DIR / "concepts.json", encoding="utf-8")),
            "map": json.load(open(PREV_DIR / "map.json", encoding="utf-8"))}
    rp = PREV_DIR / "central_report.md"
    prev["report"] = rp.read_text(encoding="utf-8") if rp.exists() else ""
    ap = PREV_DIR / "concept_aliases.json"
    prev["aliases"] = ({k: v for k, v in json.load(open(ap, encoding="utf-8")).items() if not k.startswith("_")}
                       if ap.exists() else {})
    sp = PREV_DIR / "central_course_stats.json"
    prev["stats"] = json.load(open(sp, encoding="utf-8")) if sp.exists() else None
    prev["when"] = datetime.datetime.fromtimestamp((PREV_DIR / "concepts.json").stat().st_mtime)
    return prev


def prev_thin_rows(report, name_to_key):
    """{course: reasons} from the thin-output table of an earlier report."""
    out = {}
    part = report.split("### סיכומים תקינים אבל דלים", 1)
    if len(part) < 2:
        return out
    for line in part[1].splitlines():
        cells = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(cells) == 10 and cells[1].isdigit():
            out[name_to_key.get(cells[0], cells[0])] = {
                "units": int(cells[1]), "concepts": int(cells[2]), "defined": int(cells[3]),
                "assignments": int(cells[4]), "exams": int(cells[5]), "files_read": int(cells[6]),
                "gaps": int(cells[7]), "unknown_level": int(cells[8]), "thin": [x.strip() for x in cells[9].split(",")]}
    return out


def prev_number(report, pattern):
    m = re.search(pattern, report)
    return int(m.group(1)) if m else None


def changes_section(prev, concepts, cmap, courses, occs, mapping, stats, cname, files):
    lines = []
    w = lines.append
    w("## מה השתנה מהריצה הקודמת")
    w("")
    if prev is None:
        w("אין עותק של הריצה הקודמת ב-`kb\\scratch\\central_prev`, ולכן אין השוואה.")
        w("")
        return lines
    cn = lambda k: cname.get(k, k)  # noqa: E731
    pc = {c["key"]: c for c in prev["concepts"]}
    cc = {c["key"]: c for c in concepts}
    pmap = prev["map"]
    rep = prev["report"]
    w(f"ההשוואה היא מול הפלט של הריצה הקודמת ({prev['when']:%d.%m.%Y %H:%M}), שהועתק ל-`kb\\scratch\\central_prev`. "
      + RUN_NOTE)
    w("")
    updated, kept = [], []
    for k in sorted(courses):
        p = KB / "courses" / f"{k}.json"
        (updated if datetime.datetime.fromtimestamp(p.stat().st_mtime) > prev["when"] else kept).append(k)
    w(f"**סיכומי קורסים:** {len(updated)} עודכנו מאז הריצה הקודמת"
      + (f"; {len(kept)} לא עודכנו ונכנסו עם הסיכום הקיים: " + ", ".join(cn(k) for k in kept) if kept else "")
      + ". " + ("אין קורס שהעדכון שלו נכשל או לא עבר את `validate.py`." if not NOT_VALIDATED else
                "העדכון נכשל או לא עבר את `validate.py`, ולכן נכנס הסיכום הקיים: " + ", ".join(cn(k) for k in NOT_VALIDATED) + "."))
    w("")

    def delta(a, b):
        if a is None:
            return f"{b}"
        d = b - a
        return f"{a} ← {b} ({'+' if d >= 0 else ''}{d})"

    merged_now = merge_stats(occs, mapping)[0]
    prev_entries = prev_number(rep, r"רשומות מושג בסיכומי הקורסים: (\d+)")
    prev_merged = prev_number(rep, r"מפתחות שאוחדו לתוך מפתח אחר: \*\*(\d+)")
    w("**מספרים (קודם ← עכשיו):** "
      f"רשומות מושג {delta(prev_entries, len(occs))}; "
      f"מושגים {delta(len(pc), len(cc))}; "
      f"מפתחות שאוחדו {delta(prev_merged, merged_now)}; "
      f"קשתות בין מושגים {delta(len(pmap['concept_edges']), len(cmap['concept_edges']))}; "
      f"קשתות בין קורסים {delta(len(pmap['course_edges']), len(cmap['course_edges']))}; "
      f"קשתות בין קורסים עם משקל קדם {delta(sum(1 for e in pmap['course_edges'] if e.get('weight')), sum(1 for e in cmap['course_edges'] if e.get('weight')))}; "
      f"שימושים עם מקור מרשימות הקדם {delta(sum(len(prereq_used(c)) for c in pc.values()), sum(len(prereq_used(c)) for c in cc.values()))}; "
      f"מושגים בלי הגדרה {delta(sum(1 for c in pc.values() if c['first_learned'] is None), sum(1 for c in cc.values() if c['first_learned'] is None))}; "
      f"מושגים שמשמשים בסיכום לפני ההגדרה הראשונה {delta(len(early_uses(list(pc.values()))), len(early_uses(concepts)))}.")
    w("")

    # concepts
    new = sorted(set(cc) - set(pc))
    gone = sorted(set(pc) - set(cc))
    if new:
        byc = collections.defaultdict(list)
        for k in new:
            c = cc[k]
            for x in c["defined_in"]:
                if f"`{k}`" not in byc[x["course"]]:
                    byc[x["course"]].append(f"`{k}`")
            for x in c["used_in"]:
                tag = f"`{k}` (שימוש)" if x.get("from", "summary") == "summary" else f"`{k}` (רשימת קדם)"
                if f"`{k}`" not in byc[x["course"]] and tag not in byc[x["course"]]:
                    byc[x["course"]].append(tag)
        w(f"**מושגים חדשים ({len(new)}):** " + "; ".join(f"{cn(k)}: " + ", ".join(v) for k, v in sorted(byc.items())) + ".")
        w("")
    if gone:
        w(f"**מושגים שירדו מהמפה ({len(gone)}):** " + ", ".join(f"`{k}`" for k in gone) + ".")
        w("")
    alias_new = sorted(set(mapping.items()) - set(prev["aliases"].items()))
    alias_gone = sorted(set(prev["aliases"].items()) - set(mapping.items()))
    if alias_new or alias_gone:
        w("**החלטות איחוד:** " + "; ".join(
            [f"חדש: `{a}` ← `{b}`" for b, a in alias_new] + [f"בוטל: `{a}` ← `{b}`" for b, a in alias_gone]) + ".")
        w("")

    def places(lst):
        return {x["course"]: period(x) for x in lst}

    changed = []
    small = []
    for k in sorted(set(cc) & set(pc)):
        a, b = pc[k], cc[k]
        parts = []
        fa, fb = a["first_learned"], b["first_learned"]
        if (fa and (fa["course"], period(fa))) != (fb and (fb["course"], period(fb))):
            was = f"{cn(fa['course'])} {period(fa)}" if fa else "בלי הגדרה"
            now = f"{cn(fb['course'])} {period(fb)}" if fb else "בלי הגדרה"
            parts.append(f"נלמד לראשונה: {now} (קודם: {was})")
        da, db = places(a["defined_in"]), places(b["defined_in"])
        ua, ub = places(summary_used(a)), places(summary_used(b))  # the prereq uses are summarized below
        for k2 in sorted(set(db) - set(da)):
            parts.append(f"הגדרה חדשה ב{cn(k2)} {db[k2]}" + (" (קודם שימוש)" if k2 in ua else ""))
        for k2 in sorted(set(da) - set(db)):
            parts.append(f"ההגדרה ב{cn(k2)} ירדה" + (" (עכשיו שימוש)" if k2 in ub else ""))
        for k2 in sorted(set(ub) - set(ua) - set(da)):
            parts.append(f"שימוש חדש ב{cn(k2)} {ub[k2]}")
        for k2 in sorted(set(ua) - set(ub) - set(db)):
            parts.append(f"השימוש ב{cn(k2)} ירד")
        for src, dst, label in ((da, db, "הגדרה"), (ua, ub, "שימוש")):
            for k2 in sorted(set(src) & set(dst)):
                if src[k2] != dst[k2]:
                    parts.append(f"{label} ב{cn(k2)} עברה מ-{src[k2]} ל-{dst[k2]}")
        if parts:
            changed.append(f"`{k}`: " + "; ".join(parts))
        elif any(a.get(f) != b.get(f) for f in ("statement", "depends_on", "levels", "aliases", "name_he", "merged_keys")):
            small.append(k)
    if changed:
        w(f"**מושגים קיימים שהשתנו במקום הלימוד ({len(changed)}):**")
        w("")
        for s in changed:
            w(f"- {s}")
        w("")
    if small:
        w(f"מושגים קיימים שהשתנו רק בניסוח, בשמות, ברמות או ב-`depends_on` ({len(small)}): "
          + ", ".join(f"`{k}`" for k in small[:40]) + ("..." if len(small) > 40 else "") + ".")
        w("")

    # edges
    pe = {(e["concept"], e["depends_on"]) for e in pmap["concept_edges"]}
    ce = {(e["concept"], e["depends_on"]) for e in cmap["concept_edges"]}
    add, rem = sorted(ce - pe), sorted(pe - ce)
    if add or rem:
        w(f"**קשתות בין מושגים:** {len(add)} חדשות, {len(rem)} ירדו. "
          + ("חדשות: " + ", ".join(f"`{b}` ← `{a}`" for a, b in add[:40]) + ("..." if len(add) > 40 else "") + ". " if add else "")
          + ("ירדו: " + ", ".join(f"`{b}` ← `{a}`" for a, b in rem[:20]) + ("..." if len(rem) > 20 else "") + "." if rem else ""))
        w("(בכל זוג: המושג שנשענים עליו ← המושג שנשען עליו, כמו בשרשראות שבהמשך.)")
        w("")
    # course edges from the summaries (count): as before
    pce = {(e["from"], e["to"]): e for e in pmap["course_edges"] if e["count"]}
    cce = {(e["from"], e["to"]): e for e in cmap["course_edges"] if e["count"]}
    items = []
    for key in sorted(set(cce) - set(pce), key=lambda k: (-cce[k]["count"], k)):
        e = cce[key]
        items.append(f"חדשה: {cn(e['from'])} ← {cn(e['to'])} ({e['count']}: " + ", ".join(f"`{x}`" for x in e["examples"][:4]) + ")")
    for key in sorted(set(pce) - set(cce)):
        e = pce[key]
        items.append(f"ירדה: {cn(e['from'])} ← {cn(e['to'])} (היו {e['count']}: " + ", ".join(f"`{x}`" for x in e["examples"][:4]) + ")")
    for key in sorted(set(pce) & set(cce)):
        a, b = pce[key], cce[key]
        if a["count"] != b["count"]:
            items.append(f"השתנתה: {cn(key[0])} ← {cn(key[1])} ({a['count']} ← {b['count']})")
    if items:
        w(f"**קשתות בין קורסים לפי הסיכומים ({len(items)} שינויים בספירת המושגים; הכיוון: הקורס שמגדיר ← הקורס שמשתמש):**")
        w("")
        for s in items:
            w(f"- {s}")
        w("")
    else:
        w("**קשתות בין קורסים לפי הסיכומים:** אין שינוי בספירת המושגים.")
        w("")
    # course edges from the prerequisite lists (weight)
    pw_ = {(e["from"], e["to"]): e.get("weight", 0) for e in pmap["course_edges"]}
    inc = collections.defaultdict(list)
    for e in cmap["course_edges"]:
        if e.get("weight") and e["weight"] != pw_.get((e["from"], e["to"]), 0):
            inc[e["to"]].append(e)
    if inc:
        allp = {(e["from"], e["to"]) for e in pmap["course_edges"]}
        n_new = sum(1 for es in inc.values() for e in es if (e["from"], e["to"]) not in allp)
        w(f"**קשתות קדם מרשימות הקדם:** {sum(len(v) for v in inc.values())} קשתות קיבלו משקל, "
          f"מהן {n_new} זוגות קורסים שלא היה ביניהם קשר במפה הקודמת (מסומנים ב-*). "
          "לכל קורס: הקורסים שהוא נשען עליהם, עם סכום המשקלים והמושגים הכבדים (עד 8 קורסים):")
        w("")
        first = {k: min(tkey(o["year"], o["semester"]) for o in d["offerings"]) for k, d in courses.items()}
        for k in sorted(inc, key=lambda k: (first.get(k, ("9999", 9)), k)):
            es = sorted(inc[k], key=lambda e: (-e["weight"], e["from"]))
            parts = [f"{cn(e['from'])}{'*' if (e['from'], e['to']) not in allp else ''} ({e['weight']}: "
                     + ", ".join(f"`{x}`" for x in edge_via(e, 3)) + ")" for e in es[:8]]
            more = f"; ועוד {len(es) - 8}" if len(es) > 8 else ""
            w(f"- **{cn(k)}** נשען על: " + "; ".join(parts) + more + ".")
        w("")

    # course findings
    w("**ממצאים לפי קורס:**")
    w("")
    pt, pd = per_course_concepts(prev["concepts"])
    ct, cd = per_course_concepts(concepts)
    diffs = [(k, pt[k], ct[k], pd[k], cd[k]) for k in sorted(set(pt) | set(ct)) if (pt[k], pd[k]) != (ct[k], cd[k])]
    if diffs:
        w("- מושגים במפה לפי קורס (מוגדרים בסוגריים), קודם ← עכשיו: " + "; ".join(
            f"{cn(k)} {a} ({c}) ← {b} ({d})" for k, a, b, c, d in diffs) + ".")
    pw, cw = weak_multi(prev["concepts"]), weak_multi(concepts)
    if pw != cw:
        ins = sorted(set(cw) - set(pw))
        outs = sorted(set(pw) - set(cw))
        mod = sorted(k for k in set(pw) & set(cw) if pw[k] != cw[k])
        w("- חולשות שחוזרות: " + "; ".join(
            ([("נוספו: " + ", ".join(f"`{k}`" for k in ins))] if ins else [])
            + ([("ירדו: " + ", ".join(f"`{k}`" for k in outs))] if outs else [])
            + ([("השתנו הקורסים: " + ", ".join(f"`{k}`" for k in mod))] if mod else [])) + ".")
    else:
        w(f"- חולשות שחוזרות (`weak` ביותר מקורס אחד): אין שינוי, אותם {len(cw)} מושגים.")

    def weak_elsewhere(cs):
        out = {}
        for c in cs:
            weak = {lv["course"] for lv in c["levels"] if lv["level"] == "weak"}
            others = {x["course"] for x in c["defined_in"] + c["used_in"]} - weak
            if len(weak) == 1 and others:
                out[c["key"]] = sorted(others)
        return out

    pr, cr = weak_elsewhere(prev["concepts"]), weak_elsewhere(concepts)
    rchg = [f"`{k}` (מופיע עכשיו גם ב" + ", ".join(cn(x) for x in cr[k] if x not in pr.get(k, [])) + ")"
            for k in sorted(cr) if set(cr[k]) - set(pr.get(k, []))]
    rgone = [f"`{k}`" for k in sorted(set(pr) - set(cr))]
    if rchg or rgone:
        w(f"- מושגים שסומנו `weak` בקורס אחד ומופיעים בקורסים אחרים (כולל שימוש ברשימת קדם): {len(pr)} ← {len(cr)}; "
          f"{len(rchg)} מופיעים עכשיו בקורסים נוספים"
          + (" (מוצגים 30): " if len(rchg) > 30 else ": ") + "; ".join(rchg[:30])
          + (("; ירדו: " + ", ".join(rgone)) if rgone else "") + ".")
    p30, c30 = [c["key"] for c in top30(prev["concepts"])], [c["key"] for c in top30(concepts)]
    if set(p30) != set(c30):
        w("- 30 המושגים החוזרים: נכנסו " + ", ".join(f"`{k}`" for k in c30 if k not in p30)
          + "; יצאו " + ", ".join(f"`{k}`" for k in p30 if k not in c30) + ".")
    else:
        w("- 30 המושגים החוזרים: אותה רשימה" + (" (סדר שונה)" if p30 != c30 else "") + ".")
    pn = {c["key"] for c in prev["concepts"] if c["first_learned"] is None}
    cnd = {c["key"] for c in concepts if c["first_learned"] is None}
    if pn != cnd:
        w(f"- מושגים בלי הגדרה: קיבלו הגדרה " + (", ".join(f"`{k}`" for k in sorted(pn - cnd)) or "אף אחד")
          + "; חדשים בלי הגדרה " + (", ".join(f"`{k}`" for k in sorted(cnd - pn)) or "אף אחד") + ".")
    pe_, ce_ = early_uses(prev["concepts"]), early_uses(concepts)
    if set(pe_) != set(ce_):
        w("- מושגים שמשמשים לפני ההגדרה הראשונה במפה: נוספו " + (", ".join(f"`{k}`" for k in sorted(set(ce_) - set(pe_))) or "אף אחד")
          + "; ירדו " + (", ".join(f"`{k}`" for k in sorted(set(pe_) - set(ce_))) or "אף אחד") + ".")
    name_to_key = {v: k for k, v in cname.items()}
    pstats = prev["stats"] or {}
    pthin = {k: v for k, v in pstats.items() if v.get("thin")} if pstats else prev_thin_rows(rep, name_to_key)
    cthin = {k: v for k, v in stats.items() if v["thin"]}
    thin_items = []
    for k in sorted(set(cthin) - set(pthin)):
        thin_items.append(f"{cn(k)} נכנס לטבלה ({', '.join(cthin[k]['thin'])})")
    for k in sorted(set(pthin) - set(cthin)):
        thin_items.append(f"{cn(k)} יצא מהטבלה")
    for k in sorted(set(pthin) & set(cthin)):
        a, b = pthin[k], cthin[k]
        nums = [f"{lab} {a[f]} ← {b[f]}" for f, lab in (("units", "יחידות"), ("concepts", "מושגים"), ("assignments", "עבודות"),
                                                         ("exams", "מבחנים"), ("files_read", "קבצים שנקראו"), ("gaps", "פערים"))
                if f in a and a[f] != b[f]]
        if nums or set(a["thin"]) != set(b["thin"]):
            thin_items.append(f"{cn(k)}: " + ", ".join(nums) + (f" (סיבות: {', '.join(b['thin'])})" if set(a["thin"]) != set(b["thin"]) else ""))
    w("- סיכומים דלים: " + ("; ".join(thin_items) if thin_items else "אין שינוי") + ".")
    if pstats:
        moved = []
        for k in sorted(stats):
            a, b = pstats.get(k), stats[k]
            if not a:
                continue
            nums = [f"{lab} {a[f]} ← {b[f]}" for f, lab in (("units", "יחידות"), ("assignments", "עבודות"), ("exams", "מבחנים"),
                                                             ("files_read", "קבצים שנקראו"), ("gaps", "פערים"), ("weak", "מושגים חלשים"))
                    if a.get(f) != b.get(f)]
            if nums:
                moved.append(f"{cn(k)}: " + ", ".join(nums))
        if moved:
            w("- מספרים לפי קורס שהשתנו: " + "; ".join(moved) + ".")
    else:
        w("- בריצה הקודמת לא נשמרו מספרים לכל קורס מעבר לטבלת הסיכומים הדלים, ולכן ההשוואה של יחידות, עבודות ומבחנים "
          "היא רק לקורסים שבטבלה. מהריצה הזו הם נשמרים ב-`kb\\scratch\\central_course_stats.json`.")
    w("")
    return lines


def sources_section(occs, courses, stats, cname, files, dropped_evidence):
    """Forum and external-site sources, and how the map treats them."""
    lines = []
    w = lines.append
    cn = lambda k: cname.get(k, k)  # noqa: E731
    w("### מקורות מהפורומים ומאתרים חיצוניים")
    w("")
    nf = {k: v["refs_news_forum"] + v["refs_forum_other"] + v["refs_staff_clarification"] for k, v in stats.items()}
    ne = {k: v["refs_external"] for k, v in stats.items()}
    w(f"הפניות לפורומים בסיכומים: {sum(nf.values())} ב-{sum(1 for v in nf.values() if v)} קורסים "
      f"({sum(v['refs_news_forum'] for v in stats.values())} ללוחות הודעות של הסגל, "
      f"{sum(v['refs_forum_other'] for v in stats.values())} לפורומי שאלות ודיון). "
      f"הפניות לאתרים חיצוניים: {sum(ne.values())} ב-{sum(1 for v in ne.values() if v)} קורסים "
      "(" + ", ".join(f"{cn(k)} {v}" for k, v in sorted(ne.items(), key=lambda t: -t[1]) if v) + ").")
    w("")
    w("הכלל: תוכן מהפורום נחשב רק כשהוא הודעה או הבהרה של הסגל. לוח הודעות (פורום מסוג news) הוא פורום שרק הסגל כותב בו, "
      "ולכן הוא נחשב. פורום שאלות ודיון נחשב רק אם הסוכן המרכזי בדק שההודעה שמצוטטת היא של הסגל. "
      "במפה הכלל חל על שלושה מקומות: הגדרה של מושג, השנה והסמסטר של הופעה, והראיות לחולשות.")
    w("")
    forum_def = [o for o in occs if o["src_kind"] in ("news_forum", "staff_clarification")]
    demoted = [o for o in occs if o["demoted"]]
    if forum_def:
        w("- מושגים שההגדרה שלהם במפה באה מהודעת סגל בפורום: " + "; ".join(
            f"`{o['key']}` ב{cn(o['course'])} {o['year']} {o['semester']} (`{o['defined_at']['file']}`)" for o in forum_def) + ".")
    if demoted:
        w("- הגדרות שנשענו על פורום שאינו של הסגל, ולכן נספרו במפה כשימוש בלבד: " + "; ".join(
            f"`{o['key']}` ב{cn(o['course'])}" for o in demoted) + ".")
    else:
        w("- אין הגדרה של מושג שנשענת על הודעה בפורום שאינה של הסגל.")
    other_forum = {k: v["refs_forum_other"] for k, v in stats.items() if v["refs_forum_other"]}
    if other_forum:
        w("- הפניות בסיכומי הקורסים לפורומי שאלות ודיון, שהמפה לא משתמשת בהן (בעיקר בעבודות, בדפוסי מבחנים ובפערים). "
          "דף קורס שנבנה מהן צריך לבדוק שההודעה המצוטטת היא של הסגל: "
          + ", ".join(f"{cn(k)} {v}" for k, v in sorted(other_forum.items(), key=lambda t: -t[1])) + ".")
    if dropped_evidence:
        w(f"- ראיות לחולשה שנשענו רק על פורום שאינו של הסגל ולא הוצגו בדוח: {len(dropped_evidence)} ("
          + "; ".join(f"`{k}` ב{cn(c)}" for k, c in dropped_evidence[:10]) + ").")
    else:
        w("- אין ראיה לחולשה בדוח שנשענת רק על פורום שאינו של הסגל.")
    ext = [o for o in occs if o["src_kind"] == "external"]
    if ext:
        w(f"- מושגים שההגדרה שלהם באה מאתר קורס חיצוני ({len(ext)}): " + "; ".join(
            f"`{o['key']}` ב{cn(o['course'])}" for o in ext) + ". "
          "האתרים הורדו ב-17.11.2026 מהקישורים שבמודל של אותה הופעה, אבל הדפים הם הגרסה החיה שלהם היום, "
          "ולא בהכרח הגרסה מהסמסטר שבו לירן למד. לכן השנה והסמסטר של ההגדרות האלה הם של ההופעה שקישרה אליהן, "
          "ואין בהם טענה על מה בדיוק נלמד באותו סמסטר.")
    shared = collections.Counter(o["course"] for o in occs if o["period_from"].endswith("_shared"))
    skipped = collections.Counter(o["course"] for o in occs if o["period_from"].endswith("_local_copy_skipped"))
    fallback = collections.Counter(o["course"] for o in occs if o["period_from"] in ("earliest_offering",)
                                   and len({(x["year"], x["semester"]) for x in courses[o["course"]]["offerings"]}) > 1)
    if shared or skipped:
        w("- קובץ שנמצא בשתי הופעות של אותו קורס (אותו קובץ בשתי התיקיות): עד עכשיו ההופעה נקבעה לפי סדר השורות "
          "ב-`catalog\\files.jsonl`, ולכן יכלה להתחלף בין ריצות. עכשיו הקובץ נחשב מקור רק של ההופעות שהעתק שלהן ירד "
          "מהמודל של אותה הופעה (עותק שהועתק מתיקיית ההורדות של לירן לא הופך אותו למקור של ההופעה), ומביניהן של המוקדמת. "
          + (("הופעות מושג שנקבעו לפי קובץ ששתי ההופעות העלו למודל: " + ", ".join(f"{cn(k)} {v}" for k, v in shared.most_common()) + ". ")
             if shared else "")
          + (("הופעות מושג שבהן עותק מקומי לא נספר: " + ", ".join(f"{cn(k)} {v}" for k, v in skipped.most_common()) + ".")
             if skipped else ""))
    local_copies = []
    for ck, d in courses.items():
        offs = {(o["year"], o["semester"]) for o in d["offerings"]}
        if len(offs) < 2:
            continue
        cited = collections.Counter(r["file"] for r in refs_in({k: v for k, v in d.items() if k != "files_read"}))
        for fid in cited:
            rows = [r for r in files.get(fid, []) if r.get("course") in (ck, None)]
            local = {(r["year"], r["semester"]) for r in rows if r.get("prov") == "local"}
            other = {(r["year"], r["semester"]) for r in rows if r.get("prov") != "local"}
            if local - other and other:
                local_copies.append((ck, fid, sorted(local - other), sorted(other), cited[fid]))
    if local_copies:
        w("- קבצים שבהופעה אחת הם רק עותק מתיקיית ההורדות, ובהופעה אחרת ירדו מהמודל. סוכן הקורס עלול להתייחס אליהם "
          "כמקור של שתי ההופעות, וכדאי לבדוק טענות על ההופעה שבה הם רק עותק: " + "; ".join(
              f"`{fid}` ב{cn(ck)}: עותק מקומי ב-{', '.join(y + ' ' + s for y, s in lo)}, מהמודל ב-{', '.join(y + ' ' + s for y, s in ot)} "
              f"({n} הפניות בסיכום)" for ck, fid, lo, ot, n in local_copies) + ".")
    if fallback:
        w("- הופעות מושג בקורס עם כמה הופעות, שאין להן אף קובץ מאחת ההופעות ולכן קיבלו את ההופעה הראשונה של הקורס: "
          + ", ".join(f"{cn(k)} {v}" for k, v in fallback.most_common()) + ".")
    w("")
    return lines


def nums_of(weight, count):
    """'משקל W, N מושגים' for a course edge."""
    parts = [f"משקל {weight}"] if weight else []
    if count:
        parts.append("מושג אחד" if count == 1 else f"{count} מושגים")
    return ", ".join(parts)


def deep_paths_section(courses, concepts, cmap, puses, prereqs, cname, cyc, prev):
    """## מסלולי ידע עמוקים: per focus course, the tree of courses it stands on, its heaviest prerequisite items
    (Liran's own work) and the longest chains from those concepts down to their roots; then the checks and the
    cycle decisions."""
    lines = []
    w = lines.append
    cn = lambda k: cname.get(k, k)  # noqa: E731
    cidx = {c["key"]: c for c in concepts}
    paths = cmap["course_paths"]
    edge = {(e["from"], e["to"]): e for e in cmap["course_edges"]}
    by_course = collections.defaultdict(list)
    for u in puses:
        by_course[u["course"]].append(u)

    def node(k):
        c = cidx.get(k)
        if c is None:
            return f"`{k}` (לא במפה)"
        fl = c["first_learned"]
        where = f"{cn(fl['course'])}, {period(fl)}" if fl else "בלי הגדרה"
        return f"{c['name_he']} ({where})"

    def ncourses_chain(ch):
        return len({cidx[x]["first_learned"]["course"] for x in ch if x in cidx and cidx[x]["first_learned"]})

    def b_(k):
        """The Hebrew prefix ב before a course name (with a hyphen before a name in Latin letters)."""
        name = cn(k)
        return ("ב-" if name[:1].isascii() else "ב") + name

    w("## מסלולי ידע עמוקים")
    w("")
    w("הפרק בנוי מרשימות הקדם (`kb\\courses\\<key>.prereqs.json`): בכל רשימה, כל כלי, משפט או טכניקה שהקורס באמת "
      "השתמש בהם, עם מקור (קובץ ועמוד), אופן השימוש, הקורס שבו לירן למד אותם ומשקל. המשקל נקבע לפי המקום: "
      "3 להגשה של לירן בקורס שהעבודות בו הן לפחות 50% מהציון, או לפרויקט; 2 להגשה בקורס אחר, למבחן ולבוחן; "
      "1 להרצאה ולתרגול. כך ההגשות שלו בקורסים שנשענים על עבודות שוקלות הכי הרבה.")
    w("")
    w("- **עץ הקורסים** (`course_paths` ב-`kb\\map.json`): עומק 1 הם הקורסים שהקורס נשען עליהם ישירות, בקשת מהסיכומים "
      "או מרשימת הקדם; עומק 2 הם קורסים שרק קורס מעומק 1 נשען עליהם, וכן הלאה. בסוגריים: משקל הקדם של הקשת, "
      "מספר המושגים מהסיכומים, והמושגים הכבדים.")
    w("- **הפריטים הכבדים:** הפריטים עם המשקל הגבוה ביותר ברשימת הקדם, מושג אחד לכל שורה, עם הקובץ והעמוד.")
    w("- **השרשראות עד השורשים** (`roots` ב-`kb\\concepts.json`): מהמושגים של רשימת הקדם ומהמושגים שהקורס מגדיר, "
      "יורדים דרך `depends_on` (אחרי שבירת המעגלים) עד מושג שלא נשען על אף מושג, עד 8 צעדים. מוצגות הארוכות, "
      "ומביניהן הכבדות: שעוברות במושגים שלירן השתמש בהם הכי הרבה ברשימות הקדם. הכיוון: השורש ← ... ← המושג, "
      "ובסוגריים הקורס והסמסטר שבהם כל מושג נלמד לראשונה. שרשרת שמתחילה ב-\"...\" נחתכה במגבלת העומק, "
      "והמושג הראשון בה עוד נשען על מושגים אחרים.")
    w("")
    for ck in FOCUS_COURSES:
        if ck not in courses:
            w(f"### {cn(ck)} (`{ck}`)")
            w("")
            w("אין סיכום תקין לקורס, ולכן הוא לא במפה.")
            w("")
            continue
        d = courses[ck]
        pr = prereqs.get(ck)
        offs = "; ".join(sorted({f"{o['year']} {o['semester']}" for o in d["offerings"]}))
        w(f"### {cn(ck)} (`{ck}`), {offs}")
        w("")
        items = by_course.get(ck, [])
        if pr is None:
            w("אין לקורס רשימת קדם, ולכן העץ שלו בנוי רק מהסיכומים.")
        else:
            share = pr.get("assignment_share")
            share_s = f"העבודות הן {round(share * 100)}% מהציון" if isinstance(share, (int, float)) else "חלק העבודות בציון לא ידוע"
            w(f"{share_s}. ברשימת הקדם {len(items)} פריטים, סכום משקלים {sum(u['weight'] for u in items)}, "
              f"מהם {sum(1 for u in items if u['weight'] == 3)} במשקל 3.")
        w("")
        tree = paths.get(ck, [])
        if tree:
            levels = collections.defaultdict(list)
            for t in tree:
                levels[t["depth"]].append(t)
            parts = []
            for dep in sorted(levels):
                ts = levels[dep]
                if dep == 1:
                    shown = ts[:12]
                    s = "; ".join(
                        f"{cn(t['ancestor'])} ({nums_of(t['weight'], t['count'])}: "
                        + ", ".join(f"`{x}`" for x in t["via"][:3]) + ")" for t in shown)
                    if len(ts) > len(shown):
                        s += "; ועוד " + ", ".join(cn(t["ancestor"]) for t in ts[len(shown):])
                else:
                    s = "; ".join(
                        f"{cn(t['ancestor'])} (" + "; ".join(
                            f"{cn(h['from'])}→{cn(h['to'])}: " + ", ".join(f"`{x}`" for x in h["concepts"][:2])
                            for h in (t.get("via_chain") or [])
                        ) + ")"
                        for t in ts
                    )
                parts.append(f"עומק {dep} ({len(ts)}): {s}")
            w(f"- **עץ הקורסים** ({len(tree)} קורסים): " + ". ".join(parts) + ".")
        else:
            w("- **עץ הקורסים:** הקורס לא נשען על אף קורס אחר במפה.")
        if items:
            top = sorted(items, key=lambda u: (-u["weight"], 0 if u["where"] in ("submission", "project") else 1,
                                               u["order"]))
            shown_items, seen_k = [], set()
            for u in top:
                if u["key"] in seen_k:
                    continue
                seen_k.add(u["key"])
                shown_items.append(u)
                if len(shown_items) >= 8:
                    break
            w(f"- **הפריטים הכבדים** (משקל {shown_items[0]['weight']} ומטה):")
            for u in shown_items:
                c = cidx.get(u["key"])
                li = u["learned_in"]
                learned = (f"נלמד {b_(li['course'])}" if li else "בלי קורס קודם ברשימה")
                if u.get("edge_basis", "learned_in") not in ("learned_in", None) and u.get("edge_from"):
                    learned += f"; במפה: {cn(u['edge_from'])}"
                note = u["note"] if len(u["note"]) <= 170 else u["note"][:167].rstrip() + "..."
                src = f"`{u['src']['file']}` עמ' {u['src'].get('page')}"
                if u["src_kind"] == "forum":
                    src += " (פורום שאינו של הסגל)"
                w(f"  - `{u['key']}` ({c['name_he'] if c else u['name_he']}): {note} "
                  f"[{WHERE_HE.get(u['where'], u['where'])}, {HOW_HE.get(u['used_how'], u['used_how'])}, משקל {u['weight']}; "
                  f"{src}; {learned}]")
        # chains to the roots
        cand_keys = []
        for u in sorted(items, key=lambda u: -u["weight"]):
            if u["key"] not in cand_keys:
                cand_keys.append(u["key"])
        for c in concepts:
            if any(x["course"] == ck for x in c["defined_in"]) and c["key"] not in cand_keys:
                cand_keys.append(c["key"])
        iw = collections.Counter()
        for u in items:
            iw[u["key"]] += u["weight"]
        chains = []
        for k in cand_keys:
            for ch in cidx[k].get("roots", []) if k in cidx else []:
                heavy = sum(1 + math.log1p(cidx[x].get("prereq_weight", 0)) for x in ch if x in cidx)
                chains.append((len(ch), heavy + iw[k], ncourses_chain(ch), ch))
        chains.sort(key=lambda t: (-t[0], -t[1], -t[2], t[3]))
        shown_ch = []
        for ln, _, nc, ch in chains:
            if ln < 3:
                continue
            if any(len(set(ch) & set(q)) >= 3 or ch[0] == q[0] for q in shown_ch):
                continue
            shown_ch.append(ch)
            if len(shown_ch) >= 4:
                break
        if shown_ch:
            w("- **השרשראות הארוכות עד השורשים:**")
            for ch in shown_ch:
                cut = "... ← " if cidx.get(ch[-1]) and cidx[ch[-1]]["depends_on"] else ""
                w("  - " + cut + " ← ".join(node(x) for x in reversed(ch)))
        w("")

    # checks
    w("### בדיקות")
    w("")
    for ck, groups in EXPECTED_ANCESTORS.items():
        tree = {t["ancestor"]: t for t in paths.get(ck, [])}
        parts, missing = [], []
        for label, keys in groups:
            got = []
            for k in keys:
                t = tree.get(k)
                if t is None:
                    got.append(f"{cn(k)}: חסר")
                    continue
                if t["depth"] == 1:
                    e = edge[(k, ck)]
                    got.append(f"{cn(k)}: ישיר ({nums_of(e['weight'], e['count'])}: "
                               + ", ".join(f"`{x}`" for x in t["via"][:3]) + ")")
                else:
                    got.append(f"{cn(k)}: עומק {t['depth']}, דרך " + " ← ".join(cn(x) for x in reversed(t["path"][1:-1])))
            if not any(k in tree for k in keys):
                missing.append((label, keys))
            parts.append(f"{label}: " + "; ".join(got))
        w(f"- **{cn(ck)}** אמור לעמוד על {', '.join(l for l, _ in groups)}. "
          + ("כולם בעץ. " if not missing else "חסרים: " + ", ".join(l for l, _ in missing) + ". ")
          + " | ".join(parts) + ".")
        # an expected course that is missing or only indirect: why
        for label, keys in groups:
            for k in keys:
                t = tree.get(k)
                if t is not None and t["depth"] == 1:
                    continue
                defined_here = {c["key"] for c in concepts if any(x["course"] == k for x in c["defined_in"])}
                used = [u for u in by_course.get(ck, []) if u["key"] in defined_here]
                state = "חסר בעץ" if t is None else f"מגיע רק בעומק {t['depth']}"
                if used:
                    went = collections.Counter(u.get("edge_from") or "-" for u in used)
                    w(f"  - {cn(k)} {state}: {len(used)} פריטים ברשימת הקדם של {cn(ck)} משתמשים במושגים שמוגדרים גם "
                      f"{b_(k)} (" + ", ".join(f"`{x}`" for x in sorted({u['key'] for u in used})[:6]) + "), "
                      "אבל סוכן הקדם ייחס כל אחד לקורס שבו לירן למד אותו קודם, והקשת יצאה משם: "
                      + ", ".join(f"{cn(x)} {n}" for x, n in went.most_common() if x != "-")
                      + (f" (ו-{went['-']} בלי קשת)" if went["-"] else "") + ".")
                else:
                    w(f"  - {cn(k)} {state}: אין ברשימת הקדם של {cn(ck)} פריט שמושג שלו מוגדר {b_(k)}, "
                      "ואין קשת מהסיכומים.")
    # first learned of linear algebra and calculus basics
    for ck, label in (("linear-algebra-1", "אלגברה לינארית 1"), ("calculus-1", "חדו\"א 1")):
        defined_here = [c for c in concepts if any(x["course"] == ck for x in c["defined_in"])]
        first_here = [c for c in defined_here if c["first_learned"] and c["first_learned"]["course"] == ck]
        other = [c for c in defined_here if c not in first_here]
        moved = []
        if prev:
            pc = {c["key"]: c for c in prev["concepts"]}
            for c in first_here:
                p = pc.get(c["key"])
                pf = p and p["first_learned"]
                if not p or not pf or pf["course"] != ck:
                    moved.append(f"`{c['key']}`" + (f" (קודם {cn(pf['course'])})" if pf else " (מושג חדש)" if not p else " (קודם בלי הגדרה)"))
        w(f"- **נלמד לראשונה ב{label}:** הקורס מגדיר {len(defined_here)} מושגים, ו-{len(first_here)} מהם נלמדו בו לראשונה. "
          + (f"עברו אליו בריצה הזו ({len(moved)}): " + ", ".join(moved) + ". " if moved else "")
          + (f"מושגים שהוא מגדיר ונלמדו לראשונה בקורס אחר באותו סמסטר או קודם ({len(other)}): "
             + ", ".join(f"`{c['key']}` ({cn(c['first_learned']['course'])}, {period(c['first_learned'])})" for c in other) + "."
             if other else "כל המושגים שהוא מגדיר נלמדו בו לראשונה."))
    alias_idx = {m: c["key"] for c in concepts for m in c["merged_keys"]}
    basics = []
    for k in BASICS:
        k = k if k in cidx else alias_idx.get(k, k)
        if k in cidx and k not in basics:
            basics.append(k)
    w("- **מושגי יסוד באלגברה לינארית ובחדו\"א, איפה נלמדו לראשונה:** " + "; ".join(
        f"`{k}`: " + (f"{cn(cidx[k]['first_learned']['course'])} {period(cidx[k]['first_learned'])}"
                      if cidx[k]["first_learned"] else "בלי הגדרה") for k in basics) + ".")
    ea, eap = early_uses(concepts), early_uses(concepts, prereqs=True)
    w(f"- **שימוש לפני ההגדרה הראשונה:** {len(ea)} מושגים משמשים בסיכום של קורס מוקדם יותר מההגדרה הראשונה במפה"
      + (f" (בריצה הקודמת {len(early_uses(prev['concepts']))})" if prev else "")
      + f"; לפי רשימות הקדם {len(eap)} מושגים.")
    basis = collections.Counter(u.get("edge_basis") for u in puses)
    w(f"- **מאיפה יצאו קשתות הקדם:** {basis['learned_in']} פריטים לפי הקורס שסוכן הקדם ציין; "
      f"{basis['map']} בלי קורס מצוין, לפי ההגדרה הראשונה במפה לפני השימוש; "
      f"{basis['map_late_claim'] + basis['late_claim']} שהקורס שצוין נלמד אחרי השימוש "
      f"({basis['map_late_claim']} עברו להגדרה במפה, {basis['late_claim']} בלי קשת); "
      f"{basis['none']} בלי קורס מצוין ובלי הגדרה מוקדמת במפה (ידע מלפני התואר או מקורס חסר), ולכן בלי קשת.")
    no_pr = sorted(set(courses) - set(prereqs))
    w("- **קורסים בלי רשימת קדם:** " + (", ".join(f"{cn(k)} (`{k}`)" for k in no_pr) if no_pr else "אין") + ". "
      + ("רשימות קדם שנכשלו או לא עברו את `validate_prereqs.py`: " + ", ".join(cn(k) for k in PREREQ_NOT_VALIDATED) + "."
         if PREREQ_NOT_VALIDATED else "כל רשימות הקדם עברו את `validate_prereqs.py`."))
    forum_src = [u for u in puses if u["src_kind"] == "forum"]
    if forum_src:
        w(f"- **פריטי קדם שהמקור שלהם בפורום שאינו של הסגל:** {len(forum_src)} ("
          + ", ".join(f"`{u['key']}` ב{cn(u['course'])}" for u in forum_src[:10]) + "). הם נשארו ב-`used_in` עם המקור.")
    w("")

    # cycle decisions
    w("### מעגלים ב-`depends_on` שנשברו")
    w("")
    w(f"לפני השבירה היו {len(cyc['two_cycles_before'])} זוגות מושגים שכל אחד מהם נשען על השני, "
      f"ו-{len(cyc['sccs_before'])} רכיבים מעגליים בסך הכול (חלקם של שלושה מושגים או יותר). "
      "בכל מעגל נשארה קשת אחת: המושג הכללי או זה שנלמד קודם הוא הבסיס, והמושג שמוגדר בעזרתו נשען עליו. "
      "ההחלטות רשומות ב-`kb\\concept_cycle_breaks.json`. בכל שורה: הקשת שנשארה (הבסיס ← המושג שנשען עליו), "
      "ואחריה הנימוק.")
    w("")
    for b in cyc["applied"]:
        a, dropped = b["concept"], b["dropped"]
        if b["kept_reverse"]:
            w(f"- `{a}` ← `{dropped}`: ירדה הקשת ש-`{a}` נשען על `{dropped}`. {b['why']}")
        else:
            w(f"- מעגל ארוך: ירדה הקשת ש-`{a}` נשען על `{dropped}`. {b['why']}")
    w("")
    if cyc["stale"]:
        w("החלטות שלא חלו כי הקשת כבר לא קיימת: " + ", ".join(f"`{b['concept']}` → `{b['drop']}`" for b in cyc["stale"]) + ".")
        w("")
    if cyc["left"]:
        w(f"**מעגלים שנשארו ({len(cyc['left'])}):** " + "; ".join(", ".join(f"`{k}`" for k in s) for s in cyc["left"])
          + ". בחישוב השורשים הם נחתכו במקום שבו ההליכה פוגשת אותם.")
    else:
        w("אחרי השבירה אין מעגלים ב-`depends_on`.")
    w("")
    return lines


def build_report(courses, concepts, occs, mapping, cmap, catalog_courses, by_course_counts, files, stats, prev,
                 puses=(), prereqs=None, cyc=None):
    prereqs = prereqs or {}
    cname = {c["key"]: c["name_he"] for c in catalog_courses}
    for k, d in courses.items():
        cname[k] = d["course"]["name_he"]
    cidx = {c["key"]: c for c in concepts}
    lines = []
    w = lines.append
    n_entries = len(occs)
    n_keys = len({o["key"] for o in occs})
    merged_keys, merge_groups, splits = merge_stats(occs, mapping)
    merged_groups = len(merge_groups)
    never_defined = [c for c in concepts if c["first_learned"] is None]
    missing_catalog = [c["key"] for c in catalog_courses if c["key"] not in courses]

    # weakness evidence (needed by the sources section too)
    evid = collections.defaultdict(list)
    dropped_evidence = []

    def add_evidence(key, ck, basis, text, src):
        kept = [r for r in src if counts_as_source(r, files)]
        if src and not kept:
            dropped_evidence.append((key, ck))
            return
        evid[(key, ck)].append((basis, text, kept))

    for ck, d in courses.items():
        for j in d["weaknesses"]:
            for k in j["concepts"]:
                add_evidence(canon(mapping, k, ck), ck, j["basis"], j["text"], j["src"])
        for a in d["assignments"]:
            for m in a["mistakes"]:
                if m["concept"]:
                    add_evidence(canon(mapping, m["concept"], ck), ck, m["basis"], f"{a['title']}: {m['text']}", m["src"])

    w("# מפת הידע של לירן: דוח הסוכן המרכזי")
    w("")
    w(f"ריצה של {datetime.date.today():%d.%m.%Y}. "
      "הדוח נבנה בקוד מ-`kb\\merge_concepts.py` מתוך סיכומי הקורסים שעברו את `validate.py`. "
      "ההחלטות על איחוד מושגים רשומות ב-`kb\\concept_aliases.json`, וההחלטות על שבירת מעגלים ב-`depends_on` "
      "ב-`kb\\concept_cycle_breaks.json`. רשימות הקדם (`kb\\courses\\<key>.prereqs.json`, אחרי `validate_prereqs.py`) "
      "נכנסו כשימושים עם מקור וכקשתות בין קורסים עם משקל. "
      "הקבצים המלאים: `kb\\concepts.json` (מושג לכל שורה, עם `used_in` עם מקורות ו-`roots`) ו-`kb\\map.json` "
      "(קשתות בין מושגים ובין קורסים, ו-`course_paths`: עץ הקורסים שכל קורס עומד עליו).")
    w("")
    lines += changes_section(prev, concepts, cmap, courses, occs, mapping, stats, cname, files)
    lines += sources_section(occs, courses, stats, cname, files, dropped_evidence)
    w("## מספרים")
    w("")
    w(f"- קורסים בקטלוג: {len(catalog_courses)}. קורסים עם סיכום תקין שנכנסו למפה: **{len(courses)}**. "
      f"קורסים בלי סיכום תקין: {len(missing_catalog)} (פירוט בסוף הדוח).")
    if NOT_VALIDATED:
        w(f"- קורסים שהעדכון שלהם נכשל או לא עבר את `validate.py` בסבב הזה, ולכן נכנסו עם הסיכום הקודם שלהם: "
          + ", ".join(cname.get(k, k) for k in NOT_VALIDATED) + ".")
    else:
        w("- קורסים שהעדכון שלהם נכשל או לא עבר את `validate.py` בסבב הזה: אין. כל הסיכומים עוברים את `validate.py`.")
    w(f"- רשומות מושג בסיכומי הקורסים: {n_entries}, במפתחות שונים: {n_keys}.")
    w(f"- מושגים במפה אחרי איחוד: **{len(concepts)}**.")
    w(f"- מפתחות שאוחדו לתוך מפתח אחר: **{merged_keys}**, ב-{merged_groups} מושגים קנוניים. "
      f"בנוסף, {len(splits)} רשומות בקורס מסוים הופרדו ממפתח שקורסים שונים נתנו לשני מושגים שונים "
      f"({sum(1 for _, t in splits if t in {o['key'] for o in occs})} מהן עברו למושג קיים, והשאר קיבלו מפתח חדש).")
    w(f"- מושגים שמוגדרים ביותר מקורס אחד: {sum(1 for c in concepts if len(c['defined_in']) > 1)}. "
      f"מושגים שמופיעים ביותר מקורס אחד (הגדרה או שימוש): "
      f"{sum(1 for c in concepts if len({x['course'] for x in c['defined_in'] + c['used_in']}) > 1)}.")
    w(f"- מושגים שאף קורס זמין לא מגדיר (רק שימוש): {len(never_defined)}"
      + (f", מהם {sum(1 for c in never_defined if not summary_used(c))} שמופיעים רק ברשימות הקדם" if puses else "") + ".")
    w(f"- רשימות קדם: {len(prereqs)} קורסים, {len(puses)} פריטים "
      f"({sum(1 for u in puses if u['weight'] == 3)} במשקל 3, {sum(1 for u in puses if u['weight'] == 2)} במשקל 2, "
      f"{sum(1 for u in puses if u['weight'] == 1)} במשקל 1), על {len({u['key'] for u in puses})} מושגים; "
      f"{len({u['key'] for u in puses} - {o_k for o_k in (canon(mapping, o['key'], o['course']) for o in occs)})} "
      "מהם מפתחות חדשים שאין בשום סיכום. כל פריט נכנס ל-`used_in` של המושג עם המקור שלו.")
    w(f"- קשתות `depends_on` בין מושגים: {len(cmap['concept_edges'])} (אחרי שבירת {len(cyc['applied']) if cyc else 0} קשתות במעגלים). "
      f"קשתות בין קורסים: **{len(cmap['course_edges'])}**, מהן {sum(1 for e in cmap['course_edges'] if e['weight'])} עם משקל קדם "
      f"ו-{sum(1 for e in cmap['course_edges'] if e['weight'] and not e['count'])} שבאות רק מרשימות הקדם.")
    w("")
    w("### החלטות האיחוד")
    w("")
    w("המפתח הקנוני ואחריו המפתחות שאוחדו לתוכו (המקור: `kb\\concept_aliases.json`). "
      "מושגים שרק דומים נשארו נפרדים, למשל `compact-set` ו-`compact-operator`, `newton-method` ו-`newton-raphson`, "
      "`dot-product` ו-`inner-product`, `double-counting` ו-`combinatorial-proof`, `l2-regularization` ו-`ridge-regression`, "
      "`deep-neural-network` ו-`neural-network`, `compositional-semantics` ו-`compositionality`, "
      "`quantifier` (בלשנות) ו-`quantifiers` (לוגיקה), `euler-circuit` ו-`eulerian-path`, "
      "`case` (יחסה מורפולוגית) ו-`structural-case` (יחסה מבנית), `async-await` ו-`promise`.")
    w("")
    w("- " + "; ".join(f"`{k}` ← " + ", ".join(f"`{x}`" for x in v if x != k)
                       for k, v in sorted(merge_groups.items())))
    w("")
    if splits:
        w("פיצולים (אותו מפתח, מושגים שונים בקורסים שונים): " +
          "; ".join(f"`{s.split(':')[1]}` ב{cname.get(s.split(':')[0], s.split(':')[0])} ← `{t}`" for s, t in splits) + ".")
        w("")

    # top 30
    w("## 30 המושגים שחוזרים בהכי הרבה קורסים")
    w("")
    w("\"קורסים\" סופר כל קורס שמגדיר את המושג או משתמש בו, בסיכום או ברשימת הקדם. "
      "\"נלמד לראשונה\" הוא ההגדרה המוקדמת ביותר לפי שנה וסמסטר, "
      "עם המקור (מזהה קובץ ועמוד) כפי שסוכן הקורס רשם אותו. \"(קדם)\": הקורס מופיע רק ברשימת הקדם שלו.")
    w("")
    w("| # | מושג | קורסים | נלמד לראשונה | חזר ב- |")
    w("|---|---|---|---|---|")

    for i, c in enumerate(top30(concepts), 1):
        fl = c["first_learned"]
        if fl:
            src = fl["src"]
            first = f"{cname.get(fl['course'], fl['course'])}, {period(fl)} (`{src['file']}` עמ' {src.get('page', src.get('time'))})"
        else:
            u = c["used_in"][0]
            first = f"לא מוגדר באף קורס זמין; שימוש ראשון: {cname.get(u['course'], u['course'])}, {period(u)}"
        later, seen_c = [], set()
        for x in c["defined_in"] + summary_used(c) + prereq_used(c):
            if (fl and x["course"] == fl["course"]) or x["course"] in seen_c:
                continue
            seen_c.add(x["course"])
            tag = "" if x in c["defined_in"] else (" (שימוש)" if x.get("from", "summary") == "summary" else " (קדם)")
            later.append((tkey(x["year"], x["semester"]), f"{cname.get(x['course'], x['course'])} {period(x)}{tag}"))
        later = "; ".join(s for _, s in sorted(later))
        w(f"| {i} | {c['name_he']} (`{c['key']}`) | {ncourses_of(c)} | {first} | {later} |")
    w("")

    # chains
    w("## שרשראות קדם: מה נשען על מה לאורך התואר")
    w("")
    w("### בין קורסים")
    w("")
    w("קשת מקורס א' לקורס ב' באה משני מקורות. מהסיכומים: קורס ב' משתמש (`role: used`) במושגים שהוגדרו בקורס א', "
      "שנלמד בסמסטר מוקדם יותר (\"מושגים\" בסוגריים). מרשימות הקדם: פריטים של קורס ב' שלירן למד בקורס א' "
      "(\"משקל\": סכום המשקלים של הפריטים). לכל קורס מופיעים הקורסים שהוא נשען עליהם הכי הרבה, לפי המשקל ואחריו "
      "מספר המושגים, עם המושגים הכבדים.")
    w("")
    incoming = collections.defaultdict(list)
    for e in cmap["course_edges"]:
        incoming[e["to"]].append(e)
    first_period = {}
    for k, d in courses.items():
        first_period[k] = min((tkey(o["year"], o["semester"]), f"{o['year']} {o['semester']}") for o in d["offerings"])
    for k in sorted(incoming, key=lambda k: (first_period[k][0], k)):
        es = sorted(incoming[k], key=lambda e: (-e["weight"], -e["count"], e["from"]))
        parts = []
        for e in es[:5]:
            ex = ", ".join(f"`{x}`" for x in edge_via(e, 4))
            parts.append(f"{cname.get(e['from'], e['from'])} ({nums_of(e['weight'], e['count'])}: {ex})")
        w(f"- **{cname.get(k, k)}** ({first_period[k][1]}) נשען על: " + "; ".join(parts)
          + (f"; ועוד {len(es) - 5} קורסים" if len(es) > 5 else "") + ".")
    w("")
    # longest course chain over strong edges that go forward in time (by each course's first offering)
    strong = [e for e in cmap["course_edges"] if (e["count"] >= 2 or e["weight"] >= 3)
              and e["from"] in first_period and e["to"] in first_period
              and first_period[e["from"]][0] < first_period[e["to"]][0]]
    chain_memo = {}

    def chain_to(k):
        if k in chain_memo:
            return chain_memo[k]
        preds = [e for e in strong if e["to"] == k]
        bestc = [k]
        for e in sorted(preds, key=lambda e: (-e["weight"], -e["count"], e["from"])):
            c = chain_to(e["from"]) + [k]
            if len(c) > len(bestc):
                bestc = c
        chain_memo[k] = bestc
        return bestc

    chains = sorted({tuple(chain_to(k)) for k in courses}, key=lambda c: -len(c))
    shown = []
    for ch in chains:
        if len(ch) < 3 or any(set(ch) <= set(s) for s in shown):
            continue
        shown.append(ch)
        if len(shown) >= 6:
            break
    if shown:
        w("השרשראות הארוכות ביותר בין קורסים (כל צעד הוא קשת עם שני מושגים לפחות מהסיכומים או משקל קדם 3 לפחות, "
          "לפי סדר הלמידה):")
        w("")
        for ch in shown:
            w("- " + " ← ".join(f"{cname.get(k, k)} ({first_period[k][1]})" for k in ch))
        w("")

    w("### בין מושגים")
    w("")
    w("שרשראות `depends_on` שבהן כל מושג נלמד לראשונה בסמסטר מאוחר יותר מהמושג שהוא נשען עליו, "
      "כך שהשרשרת עוברת בין כמה קורסים לאורך התואר. הסדר הוא סדר הלמידה.")
    w("")

    def ftime(c):
        fl = c["first_learned"]
        return tkey(fl["year"], fl["semester"]) if fl else None

    memo = {}

    def longest(k):
        if k in memo:
            return memo[k]
        c = cidx[k]
        t = ftime(c)
        bestp = [k]
        if t is not None:
            for d in c["depends_on"]:
                dc = cidx.get(d)
                if dc is None or ftime(dc) is None or not (ftime(dc) < t):
                    continue
                p = longest(d) + [k]
                if (len({cidx[x]["first_learned"]["course"] for x in p}), len(p)) > \
                        (len({cidx[x]["first_learned"]["course"] for x in bestp}), len(bestp)):
                    bestp = p
        memo[k] = bestp
        return bestp

    allp = []
    for c in concepts:
        if c["first_learned"]:
            p = longest(c["key"])
            ncs = len({cidx[x]["first_learned"]["course"] for x in p})
            if ncs >= 3:
                allp.append((ncs, len(p), p))
    allp.sort(key=lambda t: (-t[0], -t[1], t[2][-1]))
    used_ends = set()
    shown_p = []
    for ncs, ln, p in allp:
        if p[-1] in used_ends or any(len(set(p) & set(q)) >= 2 for q in shown_p):
            continue
        shown_p.append(p)
        used_ends.add(p[-1])
        if len(shown_p) >= 12:
            break
    for p in shown_p:
        w("- " + " ← ".join(
            f"{cidx[x]['name_he']} ({cname.get(cidx[x]['first_learned']['course'], '')}, {period(cidx[x]['first_learned'])})"
            for x in p))
    w("")

    lines += deep_paths_section(courses, concepts, cmap, puses, prereqs, cname, cyc, prev)

    # weaknesses
    w("## חולשות שחוזרות")
    w("")
    w("מושגים שבהם `my_level` הוא `weak` ביותר מקורס אחד. הראיות הן נקודות החולשה והטעויות שסוכני הקורסים רשמו "
      "על אותו מושג (אחרי איחוד המפתחות), עם המקור. הקורס הוא הקורס כולו: בקורס שנלמד כמה פעמים, "
      "השנה של כל ראיה היא של הקובץ שבמקור.")
    w("")
    wm = weak_multi(concepts)
    if not wm:
        w("אין מושג שסומן `weak` ביותר מקורס אחד.")
    for c, wcs in sorted(((cidx[k], v) for k, v in wm.items()), key=lambda t: (-len(t[1]), t[0]["key"])):
        w(f"### {c['name_he']} (`{c['key']}`): חלש ב-{len(wcs)} קורסים")
        w("")
        for ck in wcs:
            basis = next(lv["basis"] for lv in c["levels"] if lv["course"] == ck and lv["level"] == "weak")
            items = evid.get((c["key"], ck), [])
            w(f"- **{cname.get(ck, ck)}** (בסיס: `{basis}`)")
            for b, text, src in items[:3]:
                s = ", ".join(f"`{r['file']}` עמ' {r.get('page', r.get('time'))}" for r in src[:3])
                w(f"  - {text} [{b}; {s}]")
            if not items:
                w("  - סוכן הקורס סימן `weak` ברשומת המושג, בלי נקודת חולשה או טעות מפורטת שמפנה אליו.")
        w("")
    single = collections.Counter()
    for c in concepts:
        if sum(1 for lv in c["levels"] if lv["level"] == "weak") == 1:
            single[[lv["course"] for lv in c["levels"] if lv["level"] == "weak"][0]] += 1
    if single:
        w("מושגים שסומנו `weak` בקורס אחד בלבד, לפי קורס: " +
          ", ".join(f"{cname.get(k, k)} {v}" for k, v in single.most_common()) + ".")
        w("")
    recur = []
    for c in concepts:
        weak = [lv["course"] for lv in c["levels"] if lv["level"] == "weak"]
        others = {x["course"] for x in c["defined_in"] + c["used_in"]} - set(weak)
        if len(set(weak)) == 1 and others:
            recur.append((c, weak[0], sorted(others)))
    if recur:
        w(f"מושגים שסומנו `weak` בקורס אחד ומופיעים גם בקורסים אחרים ({len(recur)}; מוצגים עד 20). "
          "אלה המקומות שבהם חולשה ישנה עלולה לחזור. בסוגריים: הרמה שסוכן הקורס האחר רשם.")
        w("")

        def lvl(c, course):
            ls = [lv["level"] for lv in c["levels"] if lv["course"] == course]
            known = [x for x in ls if x != "unknown"]
            return known[0] if known else "unknown"

        for c, wc, others in sorted(recur, key=lambda t: (-len(t[2]), t[0]["key"]))[:20]:
            basis = next(lv["basis"] for lv in c["levels"] if lv["course"] == wc and lv["level"] == "weak")
            w(f"- {c['name_he']} (`{c['key']}`): חלש ב{cname.get(wc, wc)} (`{basis}`); מופיע גם ב: "
              + ", ".join(f"{cname.get(o, o)} (`{lvl(c, o)}`)" for o in others) + ".")
        w("")

    # missing / thin
    w("## קורסים שהפלט שלהם חסר או דל")
    w("")
    w("### בלי סיכום תקין (לא נכנסו למפה)")
    w("")
    w("| קורס | שנה וסמסטר | קבצים בקטלוג | עם טקסט | מצב |")
    w("|---|---|---|---|---|")
    for c in catalog_courses:
        if c["key"] in courses:
            continue
        offs = "; ".join(f"{o['year']} {o['semester']}" for o in c["offerings"])
        nf, nt = by_course_counts.get(c["key"], (0, 0))
        state = "הסיכום לא עבר את validate.py" if c["key"] in NOT_VALIDATED else "אין קבצים בקטלוג ואין סיכום"
        draft = KB / "scratch" / c["key"]
        if c["key"] in NOT_VALIDATED and draft.exists() and any(draft.iterdir()):
            state += "; יש טיוטה ב-`kb\\scratch` שלא נכנסה"
        w(f"| {c['name_he']} (`{c['key']}`) | {offs} | {nf} | {nt} | {state} |")
    w("")
    if missing_catalog:
        w("המשמעות למפה: מושגים שנלמדו לראשונה באחד הקורסים האלה מופיעים כאן כאילו נלמדו לראשונה בקורס מאוחר יותר, "
          "או בלי הגדרה בכלל. " +
          ("כל הקורסים החסרים הם בלי קבצים בקטלוג, ולכן אין להם חומר שאפשר היה לסכם." if all(
              by_course_counts.get(k, (0, 0))[0] == 0 for k in missing_catalog) else
           "`first_learned` של מושגים מהקורסים האלה מצביע על הקורס הזמין הבא שמגדיר אותם."))
    else:
        w("כל הקורסים בקטלוג נכנסו למפה.")
    w("")
    if never_defined:
        ex = sorted(never_defined, key=lambda c: (-len(c["used_in"]), c["key"]))[:15]
        w(f"מושגים שמשמשים בקורסים הזמינים ואף קורס זמין לא מגדיר ({len(never_defined)}), למשל: " +
          ", ".join(f"{c['name_he']} (`{c['key']}`, {len(c['used_in'])} קורסים)" for c in ex) + ".")
        w("")
    early = early_uses(concepts)
    if early:
        w(f"מושגים שמשמשים בקורס מוקדם יותר מההגדרה הראשונה שיש במפה ({len(early)}). "
          "כאן ההגדרה המקורית כנראה נמצאת בקורס חסר או בחומר שלא נקרא: " +
          "; ".join(f"`{k}` (שימוש ב{cname.get(b[0]['course'], b[0]['course'])} {period(b[0])}, "
                    f"הגדרה ראשונה ב{cname.get(cidx[k]['first_learned']['course'], '')} {period(cidx[k]['first_learned'])})"
                    for k, b in sorted(early.items())[:25]) + ".")
        w("")
    w("### סיכומים תקינים אבל דלים")
    w("")
    w(THIN_HEAD)
    w("")
    w("| קורס | יחידות | מושגים | מוגדרים | עבודות | מבחנים | קבצים שנקראו | פערים | מושגים בלי רמה | למה דל |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for k in sorted(courses, key=lambda k: (first_period[k][0], k)):
        s = stats[k]
        if s["thin"]:
            w(f"| {cname.get(k, k)} | {s['units']} | {s['concepts']} | {s['defined']} | {s['assignments']} | {s['exams']} | "
              f"{s['files_read']} | {s['gaps']} | {s['unknown_level']} | {', '.join(s['thin'])} |")
    w("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- main

def snapshot():
    """Copies the current outputs to kb\\scratch\\central_prev, so the next build can say what changed."""
    PREV_DIR.mkdir(parents=True, exist_ok=True)
    copied = []
    for src in (KB / "concepts.json", KB / "map.json", KB / "central_report.md", KB / "concept_aliases.json", STATS_PATH):
        if src.exists():
            shutil.copy2(src, PREV_DIR / src.name)
            copied.append(src.name)
        elif (PREV_DIR / src.name).exists():
            (PREV_DIR / src.name).unlink()
    print("snapshot ->", PREV_DIR, copied)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "snapshot":
        snapshot()
        return
    files = load_files()
    courses = load_courses()
    occs = collect(courses, files)
    prereqs = load_prereqs()
    pkeys = {it["concept"] for d in prereqs.values() for it in d["items"]}
    mapping = load_aliases(occs, pkeys)
    puses = collect_prereqs(prereqs, courses, files, mapping)
    if cmd == "candidates":
        out = candidates(occs + prereq_pseudo_occs(puses, occs))
        text = json.dumps(out, ensure_ascii=False, indent=1)
        if len(sys.argv) > 2:
            open(sys.argv[2], "w", encoding="utf-8").write(text)
            print(f"{len(out)} candidate pairs -> {sys.argv[2]}")
        else:
            print(text)
        return
    catalog_courses = json.load(open(HERE / "catalog" / "courses.json", encoding="utf-8"))
    course_rank = {c["key"]: i for i, c in enumerate(catalog_courses)}
    by_course_counts = {}
    for path in glob.glob(str(HERE / "catalog" / "by_course" / "*.json")):
        d = json.load(open(path, encoding="utf-8"))
        fl = d.get("files", [])
        k = pathlib.Path(path).stem
        by_course_counts[k] = (len(fl), sum(1 for x in fl if (x.get("text_pages") or 0) > 0))
    concepts = build_concepts(occs, mapping, course_rank, puses)
    cyc = apply_cycle_breaks(concepts, load_cycle_breaks())
    if cyc["stale"]:
        print("warning: cycle breaks that match no edge:", [(b["concept"], b["drop"]) for b in cyc["stale"]])
    if cyc["left"]:
        print("warning: cycles left in depends_on:", cyc["left"])
    add_roots(concepts)
    cmap = build_map(concepts, occs, mapping, puses)
    cmap["course_paths"] = build_course_paths(cmap["course_edges"], courses)
    cmap["broken_cycles"] = [{"concept": b["concept"], "dropped": b["dropped"], "why": b["why"]} for b in cyc["applied"]]
    stats = course_stats(courses, files)
    prev = load_prev()
    report = build_report(courses, concepts, occs, mapping, cmap, catalog_courses, by_course_counts, files, stats, prev,
                          puses, prereqs, cyc)

    def dump_list(name, rows):
        return f"\"{name}\": [\n" + ",\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n]"

    with open(KB / "concepts.json", "w", encoding="utf-8") as fh:
        fh.write("[\n" + ",\n".join(json.dumps(c, ensure_ascii=False) for c in concepts) + "\n]\n")
    with open(KB / "map.json", "w", encoding="utf-8") as fh:
        fh.write("{\n" + dump_list("concept_edges", cmap["concept_edges"]) + ",\n"
                 + dump_list("course_edges", cmap["course_edges"]) + ",\n"
                 + "\"course_paths\": {\n" + ",\n".join(json.dumps(k, ensure_ascii=False) + ": " + json.dumps(v, ensure_ascii=False)
                                                       for k, v in cmap["course_paths"].items()) + "\n},\n"
                 + dump_list("broken_cycles", cmap["broken_cycles"]) + "\n}\n")
    open(KB / "central_report.md", "w", encoding="utf-8").write(report)
    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    json.dump(stats, open(STATS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    merged = merge_stats(occs, mapping)[0]
    print(json.dumps({"courses": len(courses), "concept_entries": len(occs), "prereq_lists": len(prereqs),
                      "prereq_items": len(puses), "concepts": len(concepts),
                      "merged_keys": merged, "concept_edges": len(cmap["concept_edges"]),
                      "course_edges": len(cmap["course_edges"]),
                      "course_edges_with_weight": sum(1 for e in cmap["course_edges"] if e["weight"]),
                      "cycle_breaks": len(cyc["applied"]), "cycles_left": len(cyc["left"]),
                      "previous": None if prev is None else {"concepts": len(prev["concepts"]),
                                                             "concept_edges": len(prev["map"]["concept_edges"]),
                                                             "course_edges": len(prev["map"]["course_edges"])}}))


if __name__ == "__main__":
    main()
