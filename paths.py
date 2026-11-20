"""Paths stored in the catalog, on Windows (this PC) and on Linux (the home server).

catalog\\filed.jsonl, moodle_files.jsonl, recordings.jsonl and moodle_root.txt hold absolute Windows paths
(C:\\Users\\Liran\\OneDrive\\שולחן העבודה\\מדעי המחשב\\...) and relative paths with backslashes. The server keeps
the same tree under ~/study/cs. These helpers turn a stored path into one that works here, and back.

    local(p)          stored absolute path -> a path on this machine (str). Either root prefix is accepted.
    rel(p)            stored relative path ('2026-27\\סמסטר א\\x.pdf') -> this machine's separators
    portable(p)       absolute path on this machine -> the Windows form, for writing into the catalog
    portable_rel(p)   relative path on this machine -> the Windows form ('\\' separators)
    recycle(paths)    Linux: move files to ~/study/trash/<date>/ (the Windows Recycle Bin stays in organize.py)

On Windows every function returns its input unchanged (as str), except that a server path is mapped to R.
"""
import os
import pathlib
import shutil
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent  # R: the study tree on this machine
WIN_ROOT = r"C:\Users\Liran\OneDrive\שולחן העבודה\מדעי המחשב"
SERVER_ROOT = os.environ.get("STUDY_SERVER_ROOT", os.path.expanduser("~/study/cs"))
TRASH = pathlib.Path(os.environ.get("STUDY_TRASH") or pathlib.Path.home() / "study" / "trash")
WINDOWS = os.name == "nt"


def _under(path, prefix, sep):
    """The rest of path after prefix (case-insensitive for Windows), or None."""
    if WINDOWS or sep == "\\":
        if path.lower() == prefix.lower():
            return ""
        if path.lower().startswith(prefix.lower() + sep):
            return path[len(prefix) + 1:]
    else:
        if path == prefix:
            return ""
        if path.startswith(prefix + sep):
            return path[len(prefix) + 1:]
    return None


def local(p):
    """A stored absolute path, as a path on this machine. None stays None."""
    if p is None:
        return None
    p = str(p)
    if WINDOWS:
        rest = _under(p.replace("\\", "/"), SERVER_ROOT, "/") if p.startswith("/") else None
        return str(ROOT.joinpath(*rest.split("/"))) if rest else (str(ROOT) if rest == "" else p)
    rest = _under(p, WIN_ROOT, "\\")
    if rest is None and p.startswith("\\\\?\\"):
        rest = _under(p[4:], WIN_ROOT, "\\")
    if rest is None:
        return p
    return str(ROOT.joinpath(*[part for part in rest.split("\\") if part])) if rest else str(ROOT)


def rel(p):
    """A stored relative path, with this machine's separators."""
    if p is None:
        return None
    p = str(p)
    return p.replace("/", "\\") if WINDOWS else p.replace("\\", "/")


def portable(p):
    """An absolute path on this machine, in the Windows form the catalog uses."""
    if p is None:
        return None
    p = str(p)
    if WINDOWS:
        return p
    rest = _under(p, str(ROOT), "/")
    if rest is None:
        return p
    return WIN_ROOT + ("\\" + rest.replace("/", "\\") if rest else "")


def portable_rel(p):
    """A relative path on this machine, in the Windows form ('\\' separators)."""
    if p is None:
        return None
    p = str(p)
    return p if WINDOWS else p.replace("/", "\\")


def recycle(paths):
    """Linux stand-in for the Recycle Bin: move each file to TRASH/<date>/<its path under R>. Returns those moved."""
    gone = []
    day = TRASH / time.strftime("%Y-%m-%d")
    for p in paths:
        src = pathlib.Path(local(p))
        if not src.exists():
            continue
        try:
            inner = src.resolve().relative_to(ROOT)
        except ValueError:
            inner = pathlib.Path(src.name)
        dest = day / inner
        n = 1
        while dest.exists():
            n += 1
            dest = dest.with_name(f"{inner.stem} ({n}){inner.suffix}")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            gone.append(p)
        except OSError:
            pass
    return gone
