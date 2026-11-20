#!/usr/bin/env python3
"""Checks a podcast script (.md) against the rules in podcast_rules.md.
Standard library only. Exit code 0 = clean, 1 = violations.

  python podcast_lint.py 2026-11-19.md --dict podcast_pronounce.json
  python podcast_lint.py 2026-11-19.md --json        # machine-readable, for a cheap repair call

Each violation names the rule (R2, R11, ...) and the line, so the writer model can be
asked to fix only those lines instead of regenerating the episode.
"""
import argparse, json, re, sys

NIKUD = re.compile(r"[֑-ׇ]")
LATIN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)*")
BAD_CHARS = re.compile(r"[()\[\]{}$\\^_=+<>|*/%#~–—←-⇿∀-⋿⁰-₟²³¹]")
SENT_END = re.compile(r"(?<=[.?!])\s+")

# Hebrew spellings of Latin letter names. STRONG ones are flagged on their own,
# the others only when two or more letter names stand in a row ("אל אס טי אם").
STRONG = {"קיי", "איקס", "אקס", "וואי", "זד", "אן", "אר", "קיו", "ג'יי", "אייץ'", "דאבל"}
WEAK = {"איי", "בי", "סי", "די", "אי", "אף", "ג'י", "אל", "אם", "או", "פי", "אס", "טי", "יו", "וי", "זי"}
D_AS_LETTER = re.compile(r"(?:\b(?:מ?די|לדי)\s+(?:ועוד|פלוס|פחות|נקודות|קבוצות|קודקודים)|(?:ממד|מימד)\s+די\b)")


def strip_prefix(word):
    """ל-LSTM -> LSTM ; keeps the Latin token only."""
    return LATIN.findall(word)


def load_dict(path):
    """Load known term keys from either a flat dict/list or sectioned podcast_pronounce.json."""
    if not path:
        return set()
    data = json.load(open(path, encoding="utf-8"))
    if isinstance(data, dict):
        keys = []
        for k, v in data.items():
            if k.startswith("_") or k == "letters":
                continue
            if isinstance(v, dict):
                keys.extend(v.keys())
            else:
                keys.append(k)
    else:
        keys = [d if isinstance(d, str) else next(iter(d.values())) for d in data]
    out = set()
    for k in keys:
        if isinstance(k, str):
            out.add(k.lower())
            out.update(tok.lower() for tok in LATIN.findall(k))
    return out


def lint(text, known, a):
    out = []
    add = lambda line, rule, msg: out.append({"line": line, "rule": rule, "msg": msg})
    lines = text.split("\n")
    if not lines or not lines[0].startswith("# "):
        add(1, "R1", "first line must be '# <course>: <topic>'")

    declared = set()
    for m in re.finditer(r"<!--\s*terms:(.*?)-->", text, flags=re.S):
        for pair in m.group(1).split(";"):
            if "=" in pair:
                k = pair.split("=", 1)[0].strip()
                declared.add(k.lower())
                declared.update(tok.lower() for tok in LATIN.findall(k))
    known = known | declared

    in_block, block, block_start = False, [], 0
    visuals, chapters, total_words = 0, 0, 0
    last_speaker, letters_in_chapter, chapter_line = None, set(), 1
    short_turns = turns = 0

    def close_chapter():
        if len(letters_in_chapter) > a.max_letters:
            add(chapter_line, "R16", f"{len(letters_in_chapter)} single Latin letters in one chapter "
                f"({', '.join(sorted(letters_in_chapter))}); max {a.max_letters}")

    for n, line in enumerate(lines, 1):
        s = line.strip()
        if in_block:
            if s == "```":
                in_block = False
                visuals += 1
                try:
                    v = json.loads("\n".join(block))
                    if "svg" in v and "sim" in v and not a.allow_svg:
                        add(block_start, "R3", "visual has both 'sim' and 'svg'; drop the svg")
                    if len(v.get("caption", "").split()) > a.max_caption:
                        add(block_start, "R3", f"caption longer than {a.max_caption} words")
                    if len(v.get("formulas", [])) > 2:
                        add(block_start, "R3", "more than 2 formulas")
                except Exception as e:
                    add(block_start, "R3", f"visual block is not valid JSON: {e}")
            else:
                block.append(line)
            continue
        if s.startswith("```"):
            if s != "```visual":
                add(n, "R1", "only ```visual blocks are allowed")
            in_block, block, block_start = True, [], n
            continue
        if not s or s.startswith("<!--") or (n == 1 and s.startswith("# ")):
            continue
        if s.startswith("## "):
            close_chapter()
            chapters += 1
            letters_in_chapter, chapter_line, last_speaker = set(), n, None
            continue
        m = re.match(r"^(\S+): (.+)$", s)
        if not m or m.group(1) not in a.speakers:
            add(n, "R1", "line is not a heading, a comment, a visual block or '<speaker>: text'")
            continue
        speaker, said = m.groups()
        if speaker == last_speaker:
            add(n, "R12", f"{speaker} speaks twice in a row; alternate or merge")
        last_speaker = speaker

        if NIKUD.search(said):
            add(n, "R2", f"{len(NIKUD.findall(said))} nikud marks; write plain unvocalised Hebrew")
            said = NIKUD.sub("", said)
        words = said.split()
        total_words += len(words)
        turns += 1
        short_turns += len(words) <= 8
        if len(words) > a.max_turn:
            add(n, "R12", f"turn has {len(words)} words; max {a.max_turn}")
        sentences = [x for x in SENT_END.split(said) if x.strip()]
        if len(sentences) > a.max_turn_sentences:
            add(n, "R12", f"turn has {len(sentences)} sentences; max {a.max_turn_sentences}")
        for sent in sentences:
            k = len(sent.split())
            if k > a.max_sentence:
                add(n, "R11", f"sentence has {k} words; max {a.max_sentence}: {sent[:60]}...")
        bad = sorted(set(BAD_CHARS.findall(said)))
        if bad:
            add(n, "R11", f"characters not allowed in spoken text: {' '.join(bad)}")
        no_latin = LATIN.sub("", said)
        if re.search(r"\d", no_latin):
            add(n, "R14", "digits in spoken text; write numbers in words")

        for tok in LATIN.findall(said):
            if len(tok) == 1:  # single letters come from the fixed letter table; only their count is limited
                letters_in_chapter.add(tok)
                continue
            if tok.lower() not in known:
                add(n, "R25", f"'{tok}' is not in the dictionary and not declared in <!-- terms: ... -->")

        heb = [w.strip(".,:;?!\"'״") for w in LATIN.sub(" ", said).split()]
        run = []
        for w in heb + [""]:
            if w in STRONG or w in WEAK:
                run.append(w)
                continue
            if run and (len(run) >= 3 or (len(run) >= 2 and any(x in STRONG for x in run))
                        or (len(run) == 1 and run[0] in STRONG)):
                add(n, "R17", f"Latin letter name spelled in Hebrew: '{' '.join(run)}'")
            run = []
        if D_AS_LETTER.search(said):
            add(n, "R17", "'די' used as the letter d; say the meaning (הממד) or a concrete case")

    close_chapter()
    if in_block:
        add(block_start, "R1", "visual block is not closed")
    if visuals > a.max_visuals:
        add(1, "R3", f"{visuals} visuals; max {a.max_visuals}")
    if not a.min_chapters <= chapters <= a.max_chapters:
        add(1, "R7", f"{chapters} chapters; expected {a.min_chapters}-{a.max_chapters}")
    if not a.min_words <= total_words <= a.max_words:
        add(1, "R4", f"{total_words} spoken words; expected {a.min_words}-{a.max_words}")
    if turns and short_turns * 3 < turns:
        add(1, "R12", f"only {short_turns} of {turns} turns are short (8 words or fewer); need a third")
    return out, {"words": total_words, "turns": turns, "chapters": chapters, "visuals": visuals}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("script")
    p.add_argument("--dict", help="pronunciation dictionary (JSON object or list); its keys count as known terms")
    p.add_argument("--speakers", nargs="+", default=["הילה", "אבי"])
    p.add_argument("--min-words", type=int, default=850)
    p.add_argument("--max-words", type=int, default=1150)
    p.add_argument("--max-sentence", type=int, default=16)
    p.add_argument("--max-turn", type=int, default=40)
    p.add_argument("--max-turn-sentences", type=int, default=4)
    p.add_argument("--min-chapters", type=int, default=3)
    p.add_argument("--max-chapters", type=int, default=5)
    p.add_argument("--max-visuals", type=int, default=2)
    p.add_argument("--max-caption", type=int, default=30)
    p.add_argument("--max-letters", type=int, default=2)
    p.add_argument("--allow-svg", action="store_true")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    text = open(a.script, encoding="utf-8").read()
    problems, stats = lint(text, load_dict(a.dict), a)
    if a.json:
        print(json.dumps({"ok": not problems, "stats": stats, "violations": problems}, ensure_ascii=False, indent=1))
    else:
        for v in sorted(problems, key=lambda v: (v["line"], v["rule"])):
            print(f"line {v['line']:>3}  {v['rule']:<4} {v['msg']}")
        print(f"{'OK' if not problems else 'FAIL'}: {len(problems)} violations | {stats}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
