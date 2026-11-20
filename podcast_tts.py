#!/usr/bin/env python
"""Hebrew script (markdown with speaker labels) -> one mp3, chapters, episodes.json.

    python -X utf8 podcast_tts.py <study tree>\\פודקאסט\\<course-key>\\<YYYY-MM-DD>.md

What it does
  1. reads the script: '# title', '## chapter', and lines 'הילה: ...' / 'אבי: ...' (a speaker label, a colon, the spoken text)
  2. makes the text safe for the voices (see PRONOUNCE and numbers below), warns about what it could not fix
  3. synthesizes it with edge-tts (he-IL-HilaNeural for הילה, he-IL-AvriNeural for אבי), sentence chunks, retries, a cache
  4. joins the chunks with short pauses (ffmpeg), writes <date>.mp3 next to the script
  5. measures the duration and the chapter starts from the chunk lengths and updates <course>\\episodes.json (newest first)

edge-tts sends the script text to Microsoft's online voices (Liran approved this). If it cannot be reached, this says so plainly,
leaves the script as it is, writes no audio and exits with code 2.

edge-tts lives in its own venv, %LOCALAPPDATA%\\study-briefing\\podcast_venv; this file starts itself inside it when needed.
ffmpeg: PATH, $STUDY_FFMPEG, or the binary of the imageio-ffmpeg package that the recording tools already use.
"""
import argparse
import asyncio
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TREE = HERE.parent
VENV_PY = Path(os.environ.get("LOCALAPPDATA", "")) / "study-briefing" / "podcast_venv" / "Scripts" / "python.exe"

VOICES = {"הילה": "he-IL-HilaNeural", "אבי": "he-IL-AvriNeural"}
WORDS_PER_MIN = 105                       # measured with -12% speaking rate and 0.4s / 1.2s pauses
PAUSE_CHAPTER_MS = 1200                   # between ## chapters (1.2s)
PAUSE_TURN_MS = 400                       # between speaker turns (0.4s)
PAUSE_SENT_MS = 180                       # between chunks of the same turn
CHUNK_CHARS = 420
DEFAULT_RATE = "-12%"                     # 10-15% slower per podcast_rules.md

# Fallback Latin terms & single letters when missing from podcast_pronounce.json.
PRONOUNCE = {
    "DFA": "די אף איי", "NFA": "אן אף איי", "VC": "וי סי", "PAC": "פאק", "RNN": "אר אן אן", "NLP": "אן אל פי",
    "CKY": "סי קיי וואי", "BIO": "בי איי או", "PTB": "פי טי בי", "HMM": "אייץ' אם אם", "CRF": "סי אר אף",
    "SGD": "אס ג'י די", "CFG": "סי אף ג'י", "CNF": "סי אן אף", "UD": "יו די", "LM": "אל אם", "NER": "אן אי אר",
    "POS": "פי או אס", "MLP": "אם אל פי", "FFN": "אף אף אן", "LSTM": "אל אס טי אם", "ReLU": "ריילו", "PyTorch": "פייטורץ'",
    "CoNLL-U": "קונל יו", "RAM": "ראם", "SAT": "סאט", "NP": "אן פי", "API": "איי פי איי", "LaTeX": "לטקס",
    "n-gram": "אן גרם", "n-grams": "אן גרמים", "ngram": "אן גרם", "ngrams": "אן גרמים", "bigram": "ביגרם", "bigrams": "ביגרמים",
    "unigram": "יוניגרם", "trigram": "טריגרם", "Rank": "ראנק", "START": "סטארט", "END": "אנד",
    "GRU": "ג'י אר יו", "BPE": "בי פי אי", "TF-IDF": "טי אף איי די אף", "XOR": "קסור", "SVM": "אס וי אם",
}
DEFAULT_LETTERS = {
    "a": "אֵיי", "b": "בִּי", "c": "סִי", "d": "דִּי", "e": "אִי", "f": "אֶף", "g": "גִּ'י",
    "h": "אֵייץ'", "i": "אַיי", "j": "גֵּ'יי", "k": "קֵיי", "l": "אֶל", "m": "אֶם", "n": "אֶן",
    "o": "אוֹ", "p": "פִּי", "q": "קְיוּ", "r": "אָר", "s": "אֶס", "t": "טִי", "u": "יוּ",
    "v": "וִי", "w": "דַּאבְּל יוּ", "x": "אִיקְס", "y": "וָואי", "z": "זֶד",
}
SYMBOLS = {"%": " אחוז", "&": " וגם ", "+": " פלוס ", "=": " שווה ", "≤": " קטן או שווה ", "≥": " גדול או שווה ",
           "≠": " שונה מ ", "∈": " שייך ל ", "∞": " אינסוף ", "→": " ", "⇔": " אם ורק אם ", "≡": " שקול ", "·": " "}


def load_niqqud_lexicon():
    p = HERE / "podcast_pronounce.json"
    if not p.exists():
        return {}, {}, dict(PRONOUNCE), dict(DEFAULT_LETTERS), {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        words = data.get("words") or {}
        phrases = data.get("phrases") or {}
        latin = dict(PRONOUNCE)
        latin.update(data.get("latin") or {})
        letters = dict(DEFAULT_LETTERS)
        letters.update(data.get("letters") or {})
        pending = dict(data.get("pending") or {})
        return words, phrases, latin, letters, pending
    except Exception:
        return {}, {}, dict(PRONOUNCE), dict(DEFAULT_LETTERS), {}


NIQQUD_WORDS, NIQQUD_PHRASES, LATIN_LEXICON, LETTER_LEXICON, PENDING_LEXICON = load_niqqud_lexicon()
NIQQUD_RE = re.compile(r"[\u0591-\u05C7]")
NIQQUD_PATTERNS = [
    (re.compile(r"(?<![\u0590-\u05FF])(וכש|ובה|ולה|ומה|וה|וש|וב|ול|ום|ומ|וכ|שה|שב|של|שמ|שכ|מה|בה|לה|כש|[והבלמכש])?" + re.escape(w) + r"(?![\u0590-\u05FF])"), v)
    for w, v in sorted(NIQQUD_WORDS.items(), key=lambda kv: len(kv[0]), reverse=True)
]
PFX_VOC = {
    "ו": "וְ", "ה": "הַ", "ב": "בְּ", "ל": "לְ", "מ": "מִ", "כ": "כְּ", "ש": "שֶׁ",
    "וה": "וְהַ", "וש": "וְשֶׁ", "וב": "וּבְ", "ול": "וּלְ", "ומ": "וּמִ", "וכ": "וּכְ",
    "שה": "שֶׁהַ", "שב": "שֶׁבְּ", "של": "שֶׁלְּ", "שמ": "שֶׁמִּ", "שכ": "שֶׁכְּ",
    "מה": "מֵהַ", "בה": "בַּ", "לה": "לַ", "כש": "כְּשֶׁ",
    "וכש": "וּכְשֶׁ", "ובה": "וּבַ", "ולה": "וּלַ", "ומה": "וּמֵהַ",
}


def _phrase_pattern(plain):
    parts = [re.escape(ch) + r"[\u0591-\u05C7]*" if "\u05D0" <= ch <= "\u05EA" else re.escape(ch) for ch in plain]
    return re.compile(r"(?<![\u0590-\u05FF])" + "".join(parts) + r"(?![\u0590-\u05FF])")


PHRASE_PATTERNS = [
    (_phrase_pattern(k), v)
    for k, v in sorted(NIQQUD_PHRASES.items(), key=lambda kv: len(kv[0]), reverse=True)
]
NAKDAN_CACHE_PATH = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "study-briefing" / "tmp" / "podcast" / "nakdan_cache.json"
_NAKDAN_CACHE = None


def strip_niqqud(s):
    return NIQQUD_RE.sub("", s)


def get_nakdan_cache():
    global _NAKDAN_CACHE
    if _NAKDAN_CACHE is None:
        try:
            if NAKDAN_CACHE_PATH.exists():
                _NAKDAN_CACHE = json.loads(NAKDAN_CACHE_PATH.read_text(encoding="utf-8"))
            else:
                _NAKDAN_CACHE = {}
        except Exception:
            _NAKDAN_CACHE = {}
    return _NAKDAN_CACHE


def save_nakdan_cache():
    if _NAKDAN_CACHE is None:
        return
    try:
        NAKDAN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        NAKDAN_CACHE_PATH.write_text(json.dumps(_NAKDAN_CACHE, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def pfx_vocalized(pfx, stem):
    if pfx == "מ" and stem and stem[0] in "אהחער":
        return "מֵ"
    if pfx == "ומ" and stem and stem[0] in "אהחער":
        return "וּמֵ"
    return PFX_VOC[pfx]


def apply_lexicon_token(raw_w, fallback_voc):
    if raw_w in NIQQUD_WORDS:
        return NIQQUD_WORDS[raw_w]
    for pfx_len in (3, 2, 1):
        if len(raw_w) > pfx_len + 2 and raw_w[:pfx_len] in PFX_VOC:
            stem = raw_w[pfx_len:]
            if stem in NIQQUD_WORDS:
                return pfx_vocalized(raw_w[:pfx_len], stem) + NIQQUD_WORDS[stem]
    return fallback_voc


def apply_phrases(text):
    for pat, rep in PHRASE_PATTERNS:
        text = pat.sub(rep, text)
    return text


def apply_niqqud(text, use_nakdan=True):
    clean_t = strip_niqqud(text).strip()
    if not clean_t:
        return text
    if use_nakdan:
        cache = get_nakdan_cache()
        key = hashlib.sha1(clean_t.encode("utf-8")).hexdigest()[:20]
        if key in cache:
            res_text = cache[key]
        else:
            try:
                req = urllib.request.Request(
                    "https://nakdan-u1-0.loadbalancer.dicta.org.il/api",
                    data=json.dumps({
                        "task": "nakdan", "genre": "modern", "data": clean_t,
                        "addmorph": False, "keepqq": False, "keepmetagim": False,
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=20) as r:
                    tokens = json.loads(r.read().decode("utf-8"))
                out = []
                for tok in tokens:
                    if tok.get("sep"):
                        out.append(tok.get("word", ""))
                        continue
                    raw_w = strip_niqqud(tok.get("word", ""))
                    opts = tok.get("options") or []
                    voc_w = (opts[0][0] if isinstance(opts[0], list) else opts[0]).replace("|", "") if opts else raw_w
                    out.append(apply_lexicon_token(raw_w, voc_w))
                res_text = "".join(out)
                cache[key] = res_text
            except Exception:
                res_text = text
                for pat, vocalized in NIQQUD_PATTERNS:
                    res_text = pat.sub(lambda m, voc=vocalized: (pfx_vocalized(m.group(1), strip_niqqud(voc)) if m.group(1) else "") + voc, res_text)
    else:
        res_text = text
        for pat, vocalized in NIQQUD_PATTERNS:
            res_text = pat.sub(lambda m, voc=vocalized: (pfx_vocalized(m.group(1), strip_niqqud(voc)) if m.group(1) else "") + voc, res_text)
    # Enforce single-word lexicon overrides (including on cached Dicta output)
    tokens_out = re.split(r"([\s,.:;!?()\[\]\"'״׳\u05BE\-–—]+)", res_text)
    fixed = []
    for part in tokens_out:
        raw_w = strip_niqqud(part)
        if raw_w and re.fullmatch(r"[\u05D0-\u05EA]+", raw_w):
            fixed.append(apply_lexicon_token(raw_w, part))
        else:
            fixed.append(part)
    res_text = apply_phrases("".join(fixed))
    return res_text


def vocalize_script_file(script_path):
    """Vocalize every speaker turn inside <date>.md with full Hebrew Niqqud and write it back."""
    raw = script_path.read_text(encoding="utf-8")
    label_re = re.compile(r"^(\**\s*(?:" + "|".join(map(re.escape, VOICES)) + r")\s*\**\s*:\s*\**\s*)(.*)$")
    lines = raw.splitlines()
    out_lines = []
    in_fence = False
    in_turn = False
    changed = False
    for line in lines:
        s = line.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            in_turn = False
            out_lines.append(line)
            continue
        if in_fence or not s or s.startswith("#") or s.startswith("<!--") or s.startswith(">") or s.startswith("---"):
            in_turn = False
            out_lines.append(line)
            continue
        m = label_re.match(line)
        if m:
            in_turn = True
            prefix, body = m.group(1), m.group(2)
            voc_body = apply_niqqud(body, use_nakdan=True)
            if voc_body != body:
                changed = True
            out_lines.append(prefix + voc_body)
        elif in_turn:
            voc_line = apply_niqqud(line, use_nakdan=True)
            if voc_line != line:
                changed = True
            out_lines.append(voc_line)
        else:
            out_lines.append(line)
    save_nakdan_cache()
    if changed:
        script_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return changed

ONES = ["אפס", "אחת", "שתיים", "שלוש", "ארבע", "חמש", "שש", "שבע", "שמונה", "תשע", "עשר", "אחת עשרה", "שתים עשרה",
        "שלוש עשרה", "ארבע עשרה", "חמש עשרה", "שש עשרה", "שבע עשרה", "שמונה עשרה", "תשע עשרה"]
TENS = {20: "עשרים", 30: "שלושים", 40: "ארבעים", 50: "חמישים", 60: "שישים", 70: "שבעים", 80: "שמונים", 90: "תשעים"}
HUNDREDS = {1: "מאה", 2: "מאתיים", 3: "שלוש מאות", 4: "ארבע מאות", 5: "חמש מאות", 6: "שש מאות", 7: "שבע מאות", 8: "שמונה מאות", 9: "תשע מאות"}


def num_he(n):
    """A cardinal number in Hebrew words (feminine, the form used for counting and room numbers). 0 <= n < 100000."""
    if n < 20:
        return ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return TENS[t * 10] + (" ו" + ONES[o] if o else "")
    if n < 1000:
        h, r = divmod(n, 100)
        return HUNDREDS[h] + (" ו" + num_he(r) if r else "")
    th, r = divmod(n, 1000)
    head = {1: "אלף", 2: "אלפיים"}.get(th) or (num_he(th) + " אלף")
    return head + (" ו" + num_he(r) if r else "")


# ------------------------------------------------------------------ ffmpeg

def find_ffmpeg():
    for cand in (os.environ.get("STUDY_FFMPEG"), shutil.which("ffmpeg")):
        if cand and Path(cand).exists():
            return cand
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    for pat in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python3*\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg*.exe"),
                os.path.expandvars(r"%APPDATA%\Python\Python3*\site-packages\imageio_ffmpeg\binaries\ffmpeg*.exe")):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    sys.exit("ffmpeg not found (put it on PATH, set STUDY_FFMPEG, or pip install imageio-ffmpeg)")


def run_ff(ff, *args):
    return subprocess.run([ff, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def seconds_of(ff, path):
    """Exact duration: ffprobe when there is one beside ffmpeg, else a decode pass with ffmpeg (time= of the last progress line)."""
    probe = Path(ff).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if probe.exists():
        r = subprocess.run([str(probe), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True)
        try:
            return float(r.stdout.strip())
        except ValueError:
            pass
    r = subprocess.run([ff, "-hide_banner", "-nostdin", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    if not times:
        raise RuntimeError(f"could not read the duration of {path}: {r.stderr[-200:]}")
    h, m, s = times[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


# ------------------------------------------------------------------ script

def parse_visual_block(kind, raw_block, default_title):
    body = "\n".join(raw_block).strip()
    if not body:
        return []
    if kind == "visual":
        try:
            obj = json.loads(body)
            if isinstance(obj, list):
                return [x for x in obj if isinstance(x, dict)]
            if isinstance(obj, dict):
                return [obj]
        except Exception as e:
            raise ValueError(f"invalid JSON in ```visual block: {e}")
    if "<svg" in body:
        return [{"title": default_title, "svg": body}]
    return []


def parse_script(text):
    title, chapters = "", []          # chapters: [{'title', 'turns': [(speaker, text)], 'visuals': [...]}]
    cur = {"title": "פתיחה", "turns": [], "visuals": []}
    chapters.append(cur)
    last = None
    label_re = re.compile(r"^\**\s*(" + "|".join(map(re.escape, VOICES)) + r")\s*\**\s*:\s*\**\s*(.*)$")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    lines = text.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        fm = re.match(r"^```(visual|svg)\s*$", line, re.I)
        if fm:
            kind = fm.group(1).lower()
            idx += 1
            buf = []
            while idx < len(lines) and not lines[idx].strip().startswith("```"):
                buf.append(lines[idx])
                idx += 1
            idx += 1
            cur["visuals"].extend(parse_visual_block(kind, buf, cur["title"]))
            last = None
            continue
        idx += 1
        if not line or line.startswith(">") or line.startswith("---"):
            continue
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            if not cur["turns"] and not cur["visuals"] and len(chapters) == 1:
                chapters.pop()
            cur = {"title": line[3:].strip(), "turns": [], "visuals": []}
            chapters.append(cur)
            last = None
            continue
        m = label_re.match(line)
        if m:
            last = [m.group(1), m.group(2).strip()]
            cur["turns"].append(last)
        elif last is not None:
            last[1] += " " + line
        else:
            raise ValueError(f"line before any speaker label: {line[:60]}")
    chapters = [c for c in chapters if c["turns"] or c["visuals"]]
    return title, chapters


def merge_sidecar_visuals(script_path, chapters):
    sidecar = script_path.with_suffix(".visuals.json")
    if not sidecar.exists() or not chapters:
        return
    try:
        items = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(items, list):
        return
    for v in items:
        if not isinstance(v, dict):
            continue
        ci = v.get("chapter_index")
        if isinstance(ci, int) and 0 <= ci < len(chapters):
            target = chapters[ci]
        elif v.get("chapter_title"):
            ct = v["chapter_title"]
            target = next((c for c in chapters if ct in c["title"] or c["title"] in ct), chapters[0])
        else:
            target = chapters[0]
        if not any(existing.get("title") == v.get("title") and existing.get("svg") == v.get("svg") for existing in target["visuals"]):
            target["visuals"].append({k: val for k, val in v.items() if k not in ("chapter_index", "chapter_title")})


def attach_episode_visuals(starts, chapters):
    all_visuals = []
    for ci, st in enumerate(starts):
        vis = chapters[ci].get("visuals") if ci < len(chapters) else None
        if vis:
            st["visuals"] = vis
            for v in vis:
                all_visuals.append({
                    "chapter_index": ci,
                    "start_sec": st["start_sec"],
                    "chapter_title": st["title"],
                    **v,
                })
        else:
            st.pop("visuals", None)
    return all_visuals


PFX_ALT = "וכש|ובה|ולה|ומה|וה|וש|וב|ול|ום|ומ|וכ|שה|שב|של|שמ|שכ|מה|בה|לה|כש|[והבלמכש]"


def parse_terms_header(raw_text):
    """Extract <!-- terms: key=pron; ... --> entries from the script header."""
    out = {}
    for m in re.finditer(r"<!--\s*terms:(.*?)-->", raw_text, flags=re.S):
        for pair in m.group(1).split(";"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    out[k] = v
    return out


def clean_term_pron(val):
    """Strip syllable-break hyphens inside Hebrew pronunciations ('סופט-מקס' -> 'סופטמקס')."""
    return re.sub(r"(?<=[\u0590-\u05FF'׳])-(?=[\u0590-\u05FF])", "", strip_niqqud(val)).strip()


def register_pending_terms(terms_hdr):
    """Add newly declared terms (not in approved 'latin') to podcast_pronounce.json['pending'] for Liran's approval."""
    approved_lower = {k.lower() for k in LATIN_LEXICON} | {k.lower() for k in LETTER_LEXICON}
    added = []
    for k, v in terms_hdr.items():
        if k.lower() in approved_lower:
            continue
        existing_lower = {ek.lower() for ek in PENDING_LEXICON}
        if k.lower() not in existing_lower:
            PENDING_LEXICON[k] = v
            added.append((k, v))
    if added:
        p = HERE / "podcast_pronounce.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            pend = data.setdefault("pending", {})
            for k, v in added:
                pend[k] = v
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print("  pending terms added for one-time approval:", ", ".join(f"{k}={v}" for k, v in added))


def replace_latin_terms(text, terms_hdr=None):
    """Replace Latin words/phrases (latin + pending + script terms) and single letters using LETTER_LEXICON."""
    combined = {}
    for src in (LATIN_LEXICON, PENDING_LEXICON):
        for k, v in src.items():
            combined[k.lower()] = clean_term_pron(v)
    for k, v in (terms_hdr or {}).items():
        combined[k.lower()] = clean_term_pron(v)
    for k, v in LETTER_LEXICON.items():
        combined.setdefault(k.lower(), v)
    if not combined:
        return text
    keys = sorted(combined.keys(), key=lambda x: (-len(x), x))
    alt = "|".join(re.escape(k) for k in keys)
    pat = re.compile(r"(?:(?<![\u0590-\u05FFA-Za-z0-9])(" + PFX_ALT + r")-|(?<![A-Za-z0-9]))(" + alt + r")(?![A-Za-z0-9])", re.I)

    def repl(m):
        pfx, tok = m.group(1), m.group(2)
        rep = combined[tok.lower()]
        if not pfx:
            return rep
        first_word = strip_niqqud(rep.split()[0]) if rep else ""
        if first_word in NIQQUD_WORDS:
            return pfx + strip_niqqud(rep)
        return pfx_vocalized(pfx, strip_niqqud(rep)) + rep

    return pat.sub(repl, text)


def speakable(text, issues, terms_hdr=None, use_nakdan=False):
    t = strip_niqqud(text)
    t = re.sub(r"[*_`]", "", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    for sym, rep in SYMBOLS.items():
        if sym in t:
            t = t.replace(sym, rep)
    t = replace_latin_terms(t, terms_hdr)
    leftovers = sorted(set(re.findall(r"[A-Za-z][A-Za-z0-9\-]*", t)))
    if leftovers:
        issues.append("Latin text left (the voice may misread it): " + ", ".join(leftovers[:12]))

    def number(m):
        n = int(m.group(0).replace(",", ""))
        if n >= 100000:
            issues.append(f"large number left as digits: {n}")
            return m.group(0)
        issues.append(f"digits turned into words by the tool, better write the number in words: {m.group(0)}")
        return num_he(n)

    t = re.sub(r"\d[\d,]*", number, t)
    if re.search(r"[\\^_{}$<>|~]", t):
        issues.append("symbols left that the voice cannot read: " + "".join(sorted(set(re.findall(r"[\\^_{}$<>|~]", t)))))
    t = apply_niqqud(t, use_nakdan=use_nakdan)
    return re.sub(r"\s+", " ", t).strip()


def chunks_of(text):
    """Sentence-aligned pieces of at most CHUNK_CHARS characters."""
    sents = [s.strip() for s in re.split(r"(?<=[.!?…:;])\s+", text) if s.strip()]
    out, cur = [], ""
    for s in sents:
        if cur and len(cur) + 1 + len(s) > CHUNK_CHARS:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def word_count(text):
    return len(re.findall(r"[\u0590-\u05FF\w']+", strip_niqqud(text)))


# ------------------------------------------------------------------ synthesis

class Unreachable(RuntimeError):
    pass


async def synth_one(edge_tts, text, voice, rate, retries=4):
    last = None
    for attempt in range(retries):
        try:
            buf = bytearray()
            async for ch in edge_tts.Communicate(text, voice, rate=rate).stream():
                if ch["type"] == "audio":
                    buf += ch["data"]
            if len(buf) < 800:
                raise RuntimeError("edge returned no audio")
            return bytes(buf)
        except Exception as e:                       # network, handshake, empty audio
            last = e
            await asyncio.sleep(2 * (attempt + 1))
    raise Unreachable(f"{type(last).__name__}: {last}")


async def synth_all(jobs, cache, rate, concurrency):
    import edge_tts
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def one(i, job):
        nonlocal done
        key = hashlib.sha1(f"{job['voice']}|{rate}|{job['text']}".encode("utf-8")).hexdigest()[:20]
        path = cache / f"{key}.mp3"
        if not path.exists() or path.stat().st_size < 800:
            async with sem:
                data = await synth_one(edge_tts, job["text"], job["voice"], rate)
            path.write_bytes(data)
        done += 1
        if done % 10 == 0 or done == len(jobs):
            print(f"  synthesized {done}/{len(jobs)}", flush=True)
        job["file"] = path

    await asyncio.gather(*(one(i, j) for i, j in enumerate(jobs)))


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("script")
    ap.add_argument("--out", help="mp3 path (default: next to the script)")
    ap.add_argument("--rate", default=DEFAULT_RATE, help=f"edge-tts speaking rate, e.g. -12%% (default {DEFAULT_RATE.replace('%', '%%')})")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--check", action="store_true", help="only lint the script: words, estimated minutes, problems (no network)")
    ap.add_argument("--nakdan", action="store_true", help="legacy Dicta Nakdan pass (off by default per podcast_rules.md)")
    ap.add_argument("--vocalize", action="store_true", help="legacy in-place script vocalization (not used in normal runs per R2)")
    ap.add_argument("--sync-metadata", action="store_true", help="update episodes.json with chapters and 2D visuals from the script without re-synthesizing audio")
    ap.add_argument("--no-episodes", action="store_true", help="do not touch episodes.json")
    ap.add_argument("--keep-chunks", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    script = Path(a.script).resolve()
    if a.vocalize:
        vocalize_script_file(script)
    raw_script = script.read_text(encoding="utf-8")
    terms_hdr = parse_terms_header(raw_script)
    title, chapters = parse_script(raw_script)
    merge_sidecar_visuals(script, chapters)
    issues, total_words = [], 0
    jobs = []
    use_nakdan = bool(a.nakdan) and not a.check and not a.sync_metadata
    for ci, ch in enumerate(chapters):
        for speaker, text in ch["turns"]:
            sp_text = speakable(text, issues, terms_hdr=terms_hdr, use_nakdan=use_nakdan)
            total_words += word_count(sp_text)
            for k, piece in enumerate(chunks_of(sp_text)):
                jobs.append({"chapter": ci, "speaker": speaker, "voice": VOICES[speaker], "text": piece, "first_of_turn": k == 0})
    if use_nakdan:
        save_nakdan_cache()
    est = total_words / WORDS_PER_MIN
    vis_count = sum(len(ch.get("visuals") or []) for ch in chapters)
    print(f"{script.name}: {len(chapters)} chapters, {vis_count} visuals, {len(jobs)} chunks, {total_words} words, about {est:.1f} minutes at {WORDS_PER_MIN} words/min")
    for i in sorted(set(issues)):
        print("  note:", i)
    if a.check or a.vocalize:
        return

    out = Path(a.out).resolve() if a.out else script.with_suffix(".mp3")
    if a.sync_metadata:
        eps_path = script.parent / "episodes.json"
        eps = json.loads(eps_path.read_text(encoding="utf-8")) if eps_path.exists() else []
        date = script.stem
        prev_ep = next((e for e in eps if e.get("date") == date), None)
        if not prev_ep and not out.exists():
            sys.exit(f"--sync-metadata requires an existing entry in {eps_path} or {out}")
        ff = find_ffmpeg() if out.exists() else None
        duration = prev_ep["duration_sec"] if prev_ep and prev_ep.get("duration_sec") else (int(round(seconds_of(ff, out))) if ff and out.exists() else int(round(est * 60)))
        prev_chs = (prev_ep or {}).get("chapters") or []
        starts = []
        for ci, ch in enumerate(chapters):
            st_sec = prev_chs[ci]["start_sec"] if ci < len(prev_chs) else (prev_chs[-1]["start_sec"] + 30 if prev_chs else ci * 60)
            starts.append({"start_sec": st_sec, "title": ch["title"]})
        all_visuals = attach_episode_visuals(starts, chapters)
        rel = lambda p: p.relative_to(TREE).as_posix()
        entry = {"date": date, "title": title or script.stem, "audio": rel(out), "duration_sec": duration, "script": rel(script), "chapters": starts}
        if all_visuals:
            entry["visuals"] = all_visuals
        eps = [e for e in eps if e.get("date") != date] + [entry]
        eps.sort(key=lambda e: e["date"], reverse=True)
        eps_path.write_text(json.dumps(eps, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"episodes.json synced metadata: {eps_path} ({len(eps)} episodes, {len(all_visuals)} visuals)")
        return

    if "edge_tts" not in sys.modules:
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            if VENV_PY.exists() and Path(sys.executable).resolve() != VENV_PY.resolve():
                sys.exit(subprocess.call([str(VENV_PY), "-X", "utf8", str(Path(__file__).resolve()), *sys.argv[1:]]))
            sys.exit("edge-tts is not installed (python -m venv %LOCALAPPDATA%\\study-briefing\\podcast_venv; pip install edge-tts)")

    ff = find_ffmpeg()
    out = Path(a.out).resolve() if a.out else script.with_suffix(".mp3")
    work = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "study-briefing" / "tmp" / "podcast" / f"{script.parent.name}_{script.stem}"
    work.mkdir(parents=True, exist_ok=True)

    try:
        asyncio.run(synth_all(jobs, work, a.rate, a.concurrency))
    except Unreachable as e:
        print(f"\nedge-tts could not be reached or kept failing ({e}).\n"
              f"No audio was made; the script is left as it is: {script}\n"
              f"Run this again later; the chunks that did succeed are cached in {work}.")
        sys.exit(2)

    if terms_hdr:
        register_pending_terms(terms_hdr)

    # trim the silence the voice leaves around every chunk, work in wav, then pauses and one final mp3 encode
    import wave

    def wav_seconds(path):
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()

    trim = ("silenceremove=start_periods=1:start_silence=0.04:start_threshold=-50dB:detection=peak,areverse,"
            "silenceremove=start_periods=1:start_silence=0.12:start_threshold=-50dB:detection=peak,areverse")
    for j in jobs:
        dst = Path(j["file"]).with_suffix(".wav")
        if not dst.exists():
            r = run_ff(ff, "-i", str(j["file"]), "-af", trim, "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(dst))
            if r.returncode or not dst.exists() or wav_seconds(dst) < 0.2:
                sys.exit("ffmpeg could not trim a chunk: " + r.stderr[-300:])
        j["wav"] = dst
    for ms in (PAUSE_CHAPTER_MS, PAUSE_TURN_MS, PAUSE_SENT_MS):
        r = run_ff(ff, "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", f"{ms / 1000}", "-c:a", "pcm_s16le", str(work / f"pause{ms}.wav"))
        if r.returncode:
            sys.exit("ffmpeg could not make the pause: " + r.stderr[-300:])
    pause_len = {ms: wav_seconds(work / f"pause{ms}.wav") for ms in (PAUSE_CHAPTER_MS, PAUSE_TURN_MS, PAUSE_SENT_MS)}

    listing, t, starts, seen = [], 0.0, [], set()
    prev = None
    for j in jobs:
        if prev is not None:
            ms = PAUSE_CHAPTER_MS if j["chapter"] != prev["chapter"] else (PAUSE_TURN_MS if j["first_of_turn"] else PAUSE_SENT_MS)
            listing.append(f"file '{(work / f'pause{ms}.wav').as_posix()}'")
            t += pause_len[ms]
        if j["chapter"] not in seen:
            seen.add(j["chapter"])
            starts.append({"start_sec": int(t), "title": chapters[j["chapter"]]["title"]})
        listing.append(f"file '{Path(j['wav']).as_posix()}'")
        t += wav_seconds(j["wav"])
        prev = j
    (work / "list.txt").write_text("\n".join(listing), encoding="utf-8")
    r = run_ff(ff, "-f", "concat", "-safe", "0", "-i", str(work / "list.txt"), "-ac", "1", "-ar", "24000", "-c:a", "libmp3lame", "-b:a", "64k",
               "-metadata", f"title={title or script.stem}", "-metadata", "artist=Study briefing", str(out))
    if r.returncode:
        sys.exit("ffmpeg could not join the chunks: " + r.stderr[-400:])
    duration = int(round(seconds_of(ff, out)))
    print(f"audio: {out}  {duration // 60}:{duration % 60:02d}  ({out.stat().st_size // 1024} KB)")

    if not a.no_episodes:
        eps_path = script.parent / "episodes.json"
        eps = json.loads(eps_path.read_text(encoding="utf-8")) if eps_path.exists() else []
        date = script.stem
        try:
            rel = lambda p: p.relative_to(TREE).as_posix()
            all_visuals = attach_episode_visuals(starts, chapters)
            entry = {"date": date, "title": title or script.stem, "audio": rel(out), "duration_sec": duration, "script": rel(script), "chapters": starts}
            if all_visuals:
                entry["visuals"] = all_visuals
        except ValueError:
            print("note: the script is not inside the study tree, episodes.json was not touched")
        else:
            eps = [e for e in eps if e.get("date") != date] + [entry]
            eps.sort(key=lambda e: e["date"], reverse=True)
            eps_path.write_text(json.dumps(eps, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"episodes.json updated: {eps_path} ({len(eps)} episodes, {len(all_visuals)} visuals)")
    if not a.keep_chunks:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
