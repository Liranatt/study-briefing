"""Transcribes lecture recordings on the home server's CPU, with faster-whisper and ivrit-ai's Hebrew Whisper.

    python -X utf8 transcribe_cpu.py --once               every recording without a transcript, then stop
    python -X utf8 transcribe_cpu.py --once --until 03:30 do not start (or finish) a file after 03:30 UTC
    python -X utf8 transcribe_cpu.py --file X.m4a [--out-dir D]   one file (to try it)
    python -X utf8 transcribe_cpu.py --current-only       only the current year's recordings

The CPU twin of transcribe.py (which runs on the PC's GPU), with the same output:
    <year>\\<semester>\\<course>\\הקלטות\\תמלולים\\<name>.txt   text with a [hh:mm:ss] mark per paragraph (~60 s)
    <year>\\<semester>\\<course>\\הקלטות\\תמלולים\\<name>.json  every segment with its start and end time
Model: ivrit-ai/whisper-large-v3-turbo-ct2 (CTranslate2), int8, 8 threads, with the built-in Silero VAD so silence
and breaks are never sent to Whisper. A file that fails gets <name>.failed next to its transcript and is not retried;
the audio itself is left in place. A file cut off by --until is simply done on the next night.
"""
import argparse
import json
import os
import pathlib
import re
import sys
import time

import paths

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
LOG = HERE / "catalog" / "transcribe_cpu_log.txt"
MODEL = "ivrit-ai/whisper-large-v3-turbo-ct2"
CURRENT_YEAR = "2026-27"
PARAGRAPH_SECONDS = 60
os.environ.setdefault("HF_HOME", str(pathlib.Path.home() / "study" / "models" / "hf"))


class Deadline(Exception):
    pass


def say(s):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {s}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def out_paths(audio, out_dir=None):
    folder = pathlib.Path(out_dir) if out_dir else audio.parent / "תמלולים"
    return folder / (audio.stem + ".txt"), folder / (audio.stem + ".json")


def todo(current_only):
    """Audio files without a transcript (or a .failed mark), current year first, then the newest years."""
    items = []
    for year_dir in ROOT.iterdir():
        if not re.fullmatch(r"20\d\d-\d\d", year_dir.name) or (current_only and year_dir.name != CURRENT_YEAR):
            continue
        for audio in year_dir.glob("*/*/הקלטות/*.m4a"):
            txt = out_paths(audio)[0]
            if not txt.exists() and not txt.with_suffix(".failed").exists():
                items.append(audio)
    items.sort(key=lambda a: (a.parts[len(ROOT.parts)] != CURRENT_YEAR, [-ord(c) for c in a.parts[len(ROOT.parts)]], str(a)))
    return items


def stamp(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def squash(text):
    """A phrase of up to 6 words repeated 3+ times in a row is kept once (as in transcribe.py)."""
    return re.sub(r"((?:\S+[\s,.]+){1,6}?)\1{2,}", r"\1", text.strip() + " ").strip()


def load_audio(path):
    """16 kHz mono float32, decoded by ffmpeg (imageio-ffmpeg), as transcribe.py does; no PyAV needed."""
    import subprocess

    import imageio_ffmpeg
    import numpy as np
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-i", str(path),
           "-f", "s16le", "-ac", "1", "-ar", "16000", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def transcribe(model, audio, deadline, beam):
    pieces, info = model.transcribe(load_audio(audio), language="he", task="transcribe", beam_size=beam, vad_filter=True,
                                    vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 200},
                                    condition_on_previous_text=False)
    segments, last = [], None
    for seg in pieces:
        if deadline and time.time() > deadline:
            raise Deadline()
        text = squash(seg.text or "")
        if not text or text == last:
            continue
        last = text
        segments.append({"start": round(seg.start, 1), "end": round(seg.end, 1), "text": text})
    return segments, info.duration


def write(audio, segments, seconds, took, out_dir=None):
    txt, js = out_paths(audio, out_dir)
    txt.parent.mkdir(parents=True, exist_ok=True)
    try:
        rel = audio.resolve().relative_to(ROOT)
    except ValueError:
        rel = pathlib.Path(audio.name)
    parts = list(rel.parts) + ["", "", ""]
    lines = [f"# תמלול: {audio.stem}", f"קורס: {parts[2]} · {parts[0]} {parts[1]}",
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
    with open(js, "w", encoding="utf-8") as f:
        json.dump({"audio": paths.portable_rel(rel), "model": MODEL, "seconds": seconds, "took": took, "device": "cpu",
                   "segments": segments}, f, ensure_ascii=False)
    tmp = txt.with_suffix(".txt.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, txt)  # the .txt last: its presence marks the file as done


def deadline_of(until):
    """Seconds since the epoch of the next HH:MM UTC."""
    if not until:
        return None
    h, m = map(int, until.split(":"))
    now = time.time()
    day = now - now % 86400
    t = day + h * 3600 + m * 60
    return t if t > now else t + 86400


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="transcribe what is there now, then stop (the default)")
    ap.add_argument("--file")
    ap.add_argument("--out-dir", help="with --file: write the transcript here instead of next to the audio")
    ap.add_argument("--until", help="HH:MM UTC: stop before this time (the finance jobs start at 04:00 UTC)")
    ap.add_argument("--current-only", action="store_true")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--beam", type=int, default=5)
    ap.add_argument("--max-files", type=int, default=0)
    args = ap.parse_args()
    deadline = deadline_of(args.until)
    queue = [pathlib.Path(args.file)] if args.file else todo(args.current_only)
    if not queue:
        say("nothing to transcribe")
        return 0
    from faster_whisper import WhisperModel
    t0 = time.time()
    model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=args.threads)
    say(f"model ready in {time.time() - t0:.0f}s; {len(queue)} recordings waiting"
        + (f"; stopping at {args.until} UTC" if args.until else ""))
    done = 0
    for audio in queue:
        if deadline and time.time() > deadline - 600:
            say("stopping: the time limit is near")
            break
        try:
            started = time.time()
            segments, seconds = transcribe(model, audio, deadline, args.beam)
            took = time.time() - started
            write(audio, segments, round(seconds), round(took), args.out_dir if args.file else None)
            done += 1
            say(f"done {audio.name} | {stamp(seconds)} audio in {took / 60:.1f} min ({seconds / max(took, 1):.1f}x)")
        except Deadline:
            say(f"stopped in the middle of {audio.name} (time limit); it is done next time")
            break
        except Exception as exc:
            say(f"FAILED {audio}: {type(exc).__name__}: {exc}")
            if not args.file:
                bad = out_paths(audio)[0].with_suffix(".failed")
                bad.parent.mkdir(parents=True, exist_ok=True)
                bad.write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        if args.max_files and done >= args.max_files:
            break
    say(f"finished: {done} transcribed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
