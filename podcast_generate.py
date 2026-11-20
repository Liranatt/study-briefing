#!/usr/bin/env python3
"""Generate daily ~10-minute Hebrew podcast episodes on the server using the Gemini API.

    python -X utf8 podcast_generate.py <course-key> <YYYY-MM-DD>
    python -X utf8 podcast_generate.py --all-active [--today YYYY-MM-DD] [--force]

Reads GEMINI_API_KEY from environment or ~/study/secrets/gemini.env (via study_secrets).
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TREE = HERE.parent
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def load_gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    try:
        import study_secrets
        c = study_secrets.load("gemini.env")
        key = (c.get("GEMINI_API_KEY") or "").strip()
        if key:
            return key
    except Exception:
        pass
    raise SystemExit("GEMINI_API_KEY not found in env or secrets/gemini.env (run: setup.sh gemini)")


def call_gemini(api_key: str, model: str, prompt: str, system: str = "", temperature: float = 0.7) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": 8192},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Gemini API HTTP {e.code}: {err[:500]}")
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if text.startswith("```markdown"):
        text = text[len("```markdown"):].strip()
    elif text.startswith("```"):
        text = text[3:].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


def extract_rules_for_course(course_name_he: str) -> str:
    rules_text = (HERE / "podcast_rules.md").read_text(encoding="utf-8")
    # Drop part ו (pipeline-only) per podcast_rules.md
    if "\n## ו." in rules_text:
        rules_text = rules_text.split("\n## ו.", 1)[0]
    # Keep parts א-ד and only the matching course section from part ה
    if "\n## ה." in rules_text:
        head, part_e = rules_text.split("\n## ה.", 1)
        sections = re.split(r"\n(?=### )", part_e)
        matched = [s for s in sections if course_name_he and course_name_he.split(":")[0].strip() in s.splitlines()[0]]
        if matched:
            rules_text = head + "\n## ה. דף קורס\n\n" + matched[0].strip()
        else:
            rules_text = head
    return rules_text.strip()


def load_lexicon_keys() -> dict:
    p = HERE / "podcast_pronounce.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {
        "latin": sorted((data.get("latin") or {}).keys()),
        "pending": sorted((data.get("pending") or {}).keys()),
        "words_sample_count": len(data.get("words") or {}),
    }


def run_lint(md_path: Path, has_new_material: bool) -> tuple[bool, dict]:
    min_w = 950 if has_new_material else 850
    cmd = [
        sys.executable, "-X", "utf8", str(HERE / "podcast_lint.py"),
        str(md_path), "--dict", str(HERE / "podcast_pronounce.json"),
        "--min-words", str(min_w), "--max-words", "1150", "--json",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    try:
        info = json.loads(res.stdout)
    except Exception:
        info = {"ok": res.returncode == 0, "violations": [], "raw": res.stdout + res.stderr}
    return res.returncode == 0, info


def repair_with_gemini(api_key: str, model: str, md_path: Path, violations: list[dict], stats: dict) -> None:
    text = md_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    numbered = "\n".join(f"{i}: {ln}" for i, ln in enumerate(lines, 1))
    v_text = "\n".join(f"- line {v['line']} ({v['rule']}): {v['msg']}" for v in violations)
    prompt = (
        f"תקן את תסריט הפודקאסט הבא כך שיעמוד בכל החוקים בלי הפרות.\n"
        f"סטטיסטיקה נוכחית: {json.dumps(stats, ensure_ascii=False)}\n"
        f"הפרות שדווחו:\n{v_text}\n\n"
        f"החזר את קובץ ה-Markdown המלא והמתוקן בלבד, בלי הסברים לפני או אחרי.\n\n"
        f"התסריט הנוכחי (עם מספרי שורות):\n{numbered}"
    )
    fixed = call_gemini(api_key, model, prompt, temperature=0.3)
    if fixed.startswith("# "):
        md_path.write_text(fixed + "\n", encoding="utf-8")


def generate_one(api_key: str, model: str, course: str, date_str: str, force: bool = False) -> bool:
    out_dir = TREE / "פודקאסט" / course
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{date_str}.md"
    mp3_path = out_dir / f"{date_str}.mp3"
    if mp3_path.exists() and not force:
        print(f"skip {course} {date_str}: {mp3_path.name} already exists")
        return True

    # 1. Build context
    subprocess.run(
        [sys.executable, "-X", "utf8", str(HERE / "podcast_context.py"), course, date_str],
        check=True,
    )
    ctx_path = TREE / "פודקאסט" / "context" / course / f"{date_str}.json"
    ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
    course_name = (ctx.get("course") or {}).get("name") or course
    has_new = bool((ctx.get("materials") or {}).get("newest_lectures"))

    # 2. Build prompt
    rules_text = extract_rules_for_course(course_name)
    daily_prompt = (HERE / "podcast_daily_prompt.md").read_text(encoding="utf-8")
    lex_keys = load_lexicon_keys()

    system = (
        "אתה כותב תסריט פודקאסט לימודי בעברית עבור סטודנט למדעי המחשב.\n"
        "החזר אך ורק קובץ Markdown תקין לפי R1 (בלי הקדמה ובלי סיום מחוץ לקובץ).\n\n"
        + rules_text
    )
    user_prompt = (
        f"כתוב את פרק הבוקר של הקורס '{course_name}' ({course}) לתאריך {date_str}.\n"
        f"יעד אורך: כ-10 דקות האזנה (~1000-1080 מילים מדוברות, 5 פרקים כולל פתיחה וסיום, עד 2 בלוקי visual עם sim בלבד).\n"
        f"דגש חשוב: חבר במפורש בין הרעיון המרכזי לשיעורים קודמים בקורס (course_spine) ולמושגי רקע מקורסים קודמים (prerequisite_pointers).\n\n"
        f"מפתחות מילון הגייה קיימים (אין צורך להצהיר עליהם ב-terms):\n{json.dumps(lex_keys, ensure_ascii=False)}\n\n"
        f"הנחיות והסכמות של POD_SIM:\n{daily_prompt}\n\n"
        f"קובץ ההקשר המלא ({date_str}):\n{json.dumps(ctx, ensure_ascii=False)}"
    )

    print(f"generating {course} {date_str} with {model} ...")
    script = call_gemini(api_key, model, user_prompt, system=system, temperature=0.7)
    md_path.write_text(script + "\n", encoding="utf-8")

    # 3. Lint + up to 2 repair rounds
    for attempt in range(3):
        ok, info = run_lint(md_path, has_new)
        if ok:
            print(f"  lint OK: {info.get('stats')}")
            break
        print(f"  lint round {attempt + 1}: {len(info.get('violations', []))} violations")
        if attempt < 2 and info.get("violations"):
            repair_with_gemini(api_key, model, md_path, info["violations"], info.get("stats", {}))
    else:
        print(f"  warning: lint still has violations for {course}, proceeding to TTS")

    # 4. Synthesize MP3 + update episodes.json
    subprocess.run([sys.executable, "-X", "utf8", str(HERE / "podcast_tts.py"), str(md_path)], check=True)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("course", nargs="?")
    ap.add_argument("date", nargs="?")
    ap.add_argument("--all-active", action="store_true")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    api_key = load_gemini_key()
    date_str = args.today or args.date or datetime.date.today().isoformat()

    if args.all_active:
        res = subprocess.run(
            [sys.executable, "-X", "utf8", str(HERE / "podcast_context.py"), "--list-active"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        courses = [ln.strip().split()[0] for ln in res.stdout.splitlines() if ln.strip()]
    elif args.course:
        courses = [args.course]
    else:
        ap.error("provide <course> <date> or --all-active")

    failed = 0
    for c in courses:
        try:
            generate_one(api_key, args.model, c, date_str, force=args.force)
        except Exception as exc:
            print(f"ERROR {c}: {exc}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
