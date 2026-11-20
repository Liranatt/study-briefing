"""Transcribes every lecture recording with ivrit-ai's Hebrew Whisper, on this computer's GPU.

    python -X utf8 transcribe.py            keep going while recordings_pull.py is still downloading
    python -X utf8 transcribe.py --once     transcribe what is there now, then stop
    python -X utf8 transcribe.py --file X   one audio file (for a quick look)

Reads  <year>\\<semester>\\<course>\\הקלטות\\<name>.m4a
Writes <year>\\<semester>\\<course>\\הקלטות\\תמלולים\\<name>.txt   text with a [hh:mm:ss] mark per paragraph
       <year>\\<semester>\\<course>\\הקלטות\\תמלולים\\<name>.json  every segment with its start and end time
The current semester goes first, then the newest years. Nothing leaves this computer.
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CATALOG = HERE / "catalog"
RUNNING = CATALOG / "recordings_pull.running"
LOG = CATALOG / "transcribe_log.txt"
MODEL = "ivrit-ai/whisper-large-v3-turbo"
CURRENT_YEAR = "2026-27"
RATE = 16000
PARAGRAPH_SECONDS = 60


def say(s):
    line = f"{time.strftime('%H:%M:%S')} {s}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def longp(p):
    p = str(p)
    return "\\\\?\\" + p if os.name == "nt" and len(p) > 250 and not p.startswith("\\\\?\\") else p


def out_paths(audio):
    folder = audio.parent / "תמלולים"
    return folder / (audio.stem + ".txt"), folder / (audio.stem + ".json")


SKIP_FILE = CATALOG / "transcribe_skip.txt"  # one file name per line: recordings that hang the model (look at them later)
SKIP = set(l.strip() for l in open(SKIP_FILE, encoding="utf-8")) if SKIP_FILE.exists() else set()


def todo():
    """Audio files without a transcript, current semester first, then the newest year."""
    items = []
    for year_dir in ROOT.iterdir():
        if not re.fullmatch(r"20\d\d-\d\d", year_dir.name):
            continue
        for audio in year_dir.glob("*/*/הקלטות/*.m4a"):
            if audio.name in SKIP:
                continue
            if not os.path.exists(longp(out_paths(audio)[0])):
                items.append(audio)
    items.sort(key=lambda a: (a.parts[len(ROOT.parts)] != CURRENT_YEAR, [-ord(c) for c in a.parts[len(ROOT.parts)]], str(a)))
    return items


def load_audio(path):
    import imageio_ffmpeg
    import numpy as np
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-i", longp(path),
           "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def stamp(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def speech_windows(wave, vad, max_seconds=28.0, gap_seconds=1.5):
    """(start, end) sample ranges of speech, each at most max_seconds, found with the Silero voice detector.

    Whisper invents text on silence and noise (on a noise test it produced a Knesset speech), so breaks and
    silent stretches are never sent to it.
    """
    import torch
    from silero_vad import get_speech_timestamps
    regions = get_speech_timestamps(torch.from_numpy(wave), vad, sampling_rate=RATE,
                                    min_silence_duration_ms=400, speech_pad_ms=200)
    limit, gap = int(max_seconds * RATE), int(gap_seconds * RATE)
    windows = []
    for r in regions:
        s, e = r["start"], r["end"]
        while e - s > limit:  # one long stretch of speech: cut it into pieces
            windows.append([s, s + limit])
            s += limit
        if windows and s - windows[-1][1] <= gap and e - windows[-1][0] <= limit:
            windows[-1][1] = e
        else:
            windows.append([s, e])
    return windows


def squash(text):
    """'נכבדה, נכבדה, נכבדה, נכבדה' -> 'נכבדה,': a phrase of up to 6 words repeated 3+ times in a row is kept once."""
    return re.sub(r"((?:\S+[\s,.]+){1,6}?)\1{2,}", r"\1", text.strip() + " ").strip()


def transcribe(asr, vad, wave, batch):
    windows = speech_windows(wave, vad)
    if not windows:
        return []
    inputs = [{"raw": wave[s:e], "sampling_rate": RATE} for s, e in windows]
    results = asr(inputs, batch_size=batch, generate_kwargs={"language": "he", "task": "transcribe"})
    segments, last = [], None
    for (s, e), r in zip(windows, results):
        text = squash(r.get("text") or "")
        if not text or text == last:
            continue
        last = text
        segments.append({"start": round(s / RATE, 1), "end": round(e / RATE, 1), "text": text})
    return segments


def write(audio, segments, seconds, took):
    txt, js = out_paths(audio)
    os.makedirs(longp(txt.parent), exist_ok=True)
    rel = audio.relative_to(ROOT)
    lines = [f"# תמלול: {audio.stem}", f"קורס: {rel.parts[2]} · {rel.parts[0]} {rel.parts[1]}",
             f"אורך: {stamp(seconds)} · תומלל ב-{time.strftime('%d.%m.%Y')} עם {MODEL} · תמלול אוטומטי, ייתכנו טעויות", ""]
    para, para_start = [], None
    for s in segments:
        if para_start is None:
            para_start = s["start"] or 0
        para.append(s["text"])
        if (s["end"] or s["start"] or 0) - para_start >= PARAGRAPH_SECONDS:
            lines += [f"[{stamp(para_start)}] " + " ".join(para), ""]
            para, para_start = [], None
    if para:
        lines += [f"[{stamp(para_start)}] " + " ".join(para), ""]
    with open(longp(js), "w", encoding="utf-8") as f:
        json.dump({"audio": str(rel), "model": MODEL, "seconds": seconds, "took": took, "segments": segments}, f, ensure_ascii=False)
    with open(longp(txt), "w", encoding="utf-8") as f:  # the .txt last: its presence marks the file as done
        f.write("\n".join(lines))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--batch", type=int, default=16)
    args = ap.parse_args()
    import torch
    from transformers import pipeline
    from silero_vad import load_silero_vad
    asr = pipeline("automatic-speech-recognition", model=MODEL, dtype=torch.float16, device="cuda:0")
    vad = load_silero_vad()
    say(f"model ready on {torch.cuda.get_device_name(0)}")
    idle_since = None
    done = 0
    while True:
        queue = [pathlib.Path(args.file)] if args.file else todo()
        if not queue:
            if args.once or args.file or not RUNNING.exists() or (idle_since and time.time() - idle_since > 3 * 3600):
                break
            idle_since = idle_since or time.time()
            time.sleep(60)
            continue
        idle_since = None
        audio = queue[0]
        try:
            started = time.time()
            wave = load_audio(audio)
            seconds = len(wave) / RATE
            segments = transcribe(asr, vad, wave, args.batch)
            took = time.time() - started
            write(audio, segments, round(seconds), round(took))
            done += 1
            say(f"done {audio.relative_to(ROOT)} | {stamp(seconds)} audio in {took:.0f}s | {len(queue) - 1} waiting")
        except Exception as exc:
            say(f"FAILED {audio}: {type(exc).__name__}: {exc}")
            bad = out_paths(audio)[0].with_suffix(".failed")
            os.makedirs(longp(bad.parent), exist_ok=True)
            pathlib.Path(longp(bad)).write_text(str(exc), encoding="utf-8")
            if args.file:
                break
            os.replace(longp(audio), longp(audio.with_suffix(".m4a.failed")))  # do not retry it forever
        if args.file:
            break
    say(f"finished: {done} transcribed")


if __name__ == "__main__":
    main()
