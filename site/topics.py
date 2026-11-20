"""Topic titles for the concept browser: concepts that belong together across courses.

A concept's topic is decided in this order:
  1. a keyword rule (on the concept key / English name) for the topics that really cross courses (linear algebra, how a
     computer works, probability, calculus, optimization): e.g. "eigenvalue" learned in a neural-networks course still
     sits under linear algebra;
  2. the topic of the course where the concept was first learned (COURSE_TOPIC);
  3. the course itself (a topic named after it).
Inside a topic the concepts are grouped by the course where they were first learned, so "linear algebra" shows
"algebra 1", "algebra 2" and also the linear algebra that neural networks or machine learning needed.
Edit the two dicts freely: nothing else depends on the exact titles.
"""
import re

COURSE_TOPIC = {
    "linear-algebra-1": "אלגברה לינארית", "algebra-1-cs": "אלגברה לינארית", "algebra-2-cs": "אלגברה לינארית",
    "calculus-1": "חשבון ואנליזה", "calculus-1-eng": "חשבון ואנליזה", "calculus-2-cs": "חשבון ואנליזה", "ode": "חשבון ואנליזה",
    "numerical-analysis": "חשבון ואנליזה", "math-proficiency": "חשבון ואנליזה",
    "probability-1-stat": "הסתברות וסטטיסטיקה", "probability-cs": "הסתברות וסטטיסטיקה", "prob-stat-cs": "הסתברות וסטטיסטיקה",
    "algorithms-probability-basics": "הסתברות וסטטיסטיקה",
    "discrete-math": "לוגיקה ומתמטיקה בדידה", "discrete-structures": "לוגיקה ומתמטיקה בדידה", "logic-set-theory": "לוגיקה ומתמטיקה בדידה",
    "math-applications-cs": "לוגיקה ומתמטיקה בדידה", "philosophy-logic-science": "לוגיקה ומתמטיקה בדידה",
    "data-structures": "מבני נתונים ואלגוריתמים", "algorithm-design": "מבני נתונים ואלגוריתמים", "intro-cs": "מבני נתונים ואלגוריתמים",
    "python-intro": "מבני נתונים ואלגוריתמים", "cs-puzzles": "מבני נתונים ואלגוריתמים",
    "computational-models": "תיאוריית החישוב",
    "systems-programming": "כיצד מחשב עובד", "operating-systems": "כיצד מחשב עובד", "espl": "כיצד מחשב עובד",
    "programming-languages": "שפות תכנות",
    "intro-ml": "למידת מכונה ולמידה עמוקה", "deep-learning-intro": "למידת מכונה ולמידה עמוקה", "neural-networks-intro": "למידת מכונה ולמידה עמוקה",
    "dl-theory": "למידת מכונה ולמידה עמוקה",
    "optimization-ds": "אופטימיזציה",
    "graph-learning": "למידה על גרפים",
    "geometric-learning-theory": "תורת הלמידה", "research": "מחקר",
    "linguistics-intro": "שפה, בלשנות ומחשבה", "semantics-nl": "שפה, בלשנות ומחשבה", "syntax-nl": "שפה, בלשנות ומחשבה", "nlp": "שפה, בלשנות ומחשבה",
    "philosophy-of-mind": "שפה, בלשנות ומחשבה", "cogsci-seminar": "שפה, בלשנות ומחשבה",
    "finance-foundations": "מסחר ופיננסים", "financial-data-lab": "מסחר ופיננסים", "algo-trading": "מסחר ופיננסים",
    "chemistry-cog": "מדעי הטבע", "cell-biology": "מדעי הטבע", "physics-1c": "מדעי הטבע", "physics-2c": "מדעי הטבע",
}

# keyword rules: topic -> regex over "<key> <english name>" (lower case). Only for topics that cross courses.
KEYWORDS = [
    ("אלגברה לינארית", r"eigen|singular-value|svd|matrix|matrices|vector-space|determinant|orthogonal|linear-map|linear-transform|"
                      r"\brank\b|null-?space|column-space|subspace|projection|gram-schmidt|spectral|positive-definite|diagonaliz|inner-product|dot-product|basis"),
    ("כיצד מחשב עובד", r"register|x86|assembly|stack-frame|call-stack|syscall|system-call|interrupt|instruction-set|opcode|machine-code|"
                       r"\bcache\b|virtual-memory|paging|page-table|kernel|process|thread|scheduler|file-system|inode|\belf\b|linker|loader|"
                       r"calling-convention|endian|memory-hierarchy|\bcpu\b|\bram\b|pointer|segmentation|mutex|semaphore|deadlock"),
    ("הסתברות וסטטיסטיקה", r"probability|expectation|expected-value|variance|distribution|bayes|hypothesis|p-value|confidence-interval|"
                           r"random-variable|central-limit|law-of-large|likelihood|estimator|covariance|correlation|markov-chain"),
    ("חשבון ואנליזה", r"derivative|gradient|jacobian|hessian|integral|taylor|limit|chain-rule|partial-derivative|series-convergence|continuity"),
    ("אופטימיזציה", r"convex|lagrang|kkt|gradient-descent|optimization|stochastic-gradient|duality|newton-method|line-search"),
]
KEYWORDS = [(t, re.compile(p)) for t, p in KEYWORDS]


def topic_of(concept: dict, first_course: str | None) -> str:
    text = f"{concept.get('k', '')} {concept.get('en', '')}".lower()
    own = COURSE_TOPIC.get(first_course or "")
    for title, rx in KEYWORDS:
        if title != own and rx.search(text):
            return title
    return own or ""


def build_topics(concepts: list[dict], courses: dict) -> list[dict]:
    """[{title, groups: [{course, name, ks: [concept keys]}]}], topics and groups sorted by size. courses: key -> [name, ...]."""
    topics: dict[str, dict[str, list]] = {}
    for c in concepts:
        fl = c.get("fl")
        place = c["d"][fl] if isinstance(fl, int) and fl < len(c["d"]) else (c["d"][0] if c["d"] else (c["u"][0] if c["u"] else None))
        course = place[0] if place else None
        title = topic_of(c, course) or ((courses.get(course) or [course or "ללא קורס"])[0])
        topics.setdefault(title, {}).setdefault(course or "", []).append(c["k"])
    out = []
    for title, groups in topics.items():
        gl = [{"course": k, "name": (courses.get(k) or ["ללא קורס"])[0], "ks": sorted(v)} for k, v in groups.items()]
        gl.sort(key=lambda g: -len(g["ks"]))
        out.append({"title": title, "n": sum(len(g["ks"]) for g in gl), "groups": gl})
    out.sort(key=lambda t: -t["n"])
    return out
