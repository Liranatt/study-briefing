"""Downloads every lecture recording from Moodle and keeps its audio for transcription.

    python -X utf8 recordings_pull.py                 all courses, the current semester first
    python -X utf8 recordings_pull.py --course 68512  only this Moodle course (can repeat)
    python -X utf8 recordings_pull.py --resolve-only  only list the recordings, download nothing

For every recording:
  <year>\\<semester>\\<course>\\הקלטות\\<NNN> <title>.m4a        audio (mono, 16 kHz, AAC 32k), always
  <year>\\<semester>\\<course>\\הקלטות\\וידאו\\<NNN> <title>.mp4   the video, only for CURRENT_YEAR
Older videos are downloaded to a temporary folder outside OneDrive, turned into audio, and deleted.
transcribe.py turns the audio into text next to it, in הקלטות\\תמלולים\\.

Where recordings are:
  - videostream modules and the video block: pages that need a web session; the player page has an mp4 on cloudfront.
    The web session comes from the mobile app's autologin key, so no password is sent to a web page.
  - media files uploaded as course files: pluginfile URLs with the token.
  - Zoom cloud links and YouTube links are listed in catalog\\recordings.jsonl but not downloaded yet.

Login: the Moodle token and private token are kept in %LOCALAPPDATA%\\study-briefing\\moodle.json (outside OneDrive),
so a second run does not ask again. The password itself is never written anywhere.
"""
import argparse
import collections
import concurrent.futures
import getpass
import hashlib
import html
import http.cookiejar
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

import moodle_pull as mp

HERE = pathlib.Path(__file__).resolve().parent
CATALOG = HERE / "catalog"
ROOT = HERE.parent
LOCAL = pathlib.Path(os.environ.get("LOCALAPPDATA", str(pathlib.Path.home()))) / "study-briefing"
TOKEN_FILE = LOCAL / "moodle.json"
TMP = LOCAL / "tmp"
INDEX = CATALOG / "recordings.jsonl"
RUNNING = CATALOG / "recordings_pull.running"
DEBUG = CATALOG / "recordings_debug"
CURRENT_YEAR = "2026-27"
SKIP = {51696, 51776, 60163}
SEM_DIR = {"א": "סמסטר א", "ב": "סמסטר ב", "קיץ": "קיץ"}
AUDIO_ARGS = ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k"]
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

lock = threading.Lock()
log = []


def pin_moodle_address():
    """Send every request for moodle.bgu.ac.il to one Akamai edge address, so Moodle sees one client IP."""
    import socket
    host = urllib.parse.urlparse(mp.ORIGIN).hostname
    real = socket.getaddrinfo
    first = [a for a in real(host, 443, socket.AF_INET, socket.SOCK_STREAM)][:1]

    def pinned(name, port, *args, **kwargs):
        if name == host and first:
            return [(f, t, p, c, (first[0][4][0], port)) for f, t, p, c, _ in first]
        return real(name, port, *args, **kwargs)
    socket.getaddrinfo = pinned


pin_moodle_address()
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
opener.addheaders = [("User-Agent", mp.APP_UA)]


def say(s=""):
    s = mp.mask(s)
    with lock:
        log.append(s)
        print(s, flush=True)


def ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# ---------- login and web session ----------

def ask_password():
    username = input("Username (English keyboard, without @post.bgu.ac.il): ").strip()
    password = getpass.getpass("Password (hidden while typing): ")
    if password:
        mp.secrets.append(password)
    return username, password


def login(creds=None):
    """(token, private token, user id). Reuses the saved token while Moodle still accepts it."""
    if TOKEN_FILE.exists():
        saved = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        mp.secrets.extend(s for s in (saved.get("token"), saved.get("privatetoken")) if s)
        try:
            info = mp.ws(saved["token"], "core_webservice_get_site_info")
            say(f"Logged in with the saved token as {info.get('fullname')}")
            return saved["token"], saved.get("privatetoken"), info["userid"]
        except Exception as exc:
            say(f"The saved token no longer works ({exc}); logging in again.")
    for attempt in range(3):
        username, password = creds or ask_password()
        creds = None
        got = mp.post_json(mp.BASE + "/login/token.php", {"username": username, "password": password, "service": mp.SERVICE})
        if "token" in got:
            break
        say(f"login FAILED: {got.get('error')}. Try again ({2 - attempt} left).")
    else:
        raise SystemExit("no login")
    mp.secrets.extend(s for s in (got["token"], got.get("privatetoken")) if s)
    info = mp.ws(got["token"], "core_webservice_get_site_info")
    os.makedirs(LOCAL, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"token": got["token"], "privatetoken": got.get("privatetoken"),
                                      "userid": info["userid"], "saved": time.strftime("%Y-%m-%d %H:%M")}), encoding="utf-8")
    say(f"Logged in as {info.get('fullname')}; token saved outside OneDrive for the next runs.")
    return got["token"], got.get("privatetoken"), info["userid"]


class Web:
    """A Moodle web session through the mobile app's autologin key."""

    def __init__(self, token, private, userid, creds=None):
        self.token, self.private, self.userid = token, private, userid
        self.creds = creds  # username and password typed in this window; kept in memory only
        self.last_key = 0.0
        self.ok = False

    def form_login(self):
        """The website's own login form: no IP check and no 6-minute wait, but needs the password."""
        page = self.raw(mp.BASE + "/login/index.php")
        token = re.search(r'name="logintoken" value="([^"]+)"', page)
        data = urllib.parse.urlencode({"anchor": "", "logintoken": token.group(1) if token else "",
                                       "username": self.creds[0], "password": self.creds[1]}).encode()
        with opener.open(urllib.request.Request(mp.BASE + "/login/index.php", data=data), timeout=60) as r:
            home = r.read().decode("utf-8", "replace")
        self.ok = "logout.php" in home
        say(f"Website login: {'OK' if self.ok else 'FAILED (wrong password?)'}")
        return self.ok

    def connect(self):
        return self.form_login() if self.creds else self.start()

    def key(self):
        """A fresh autologin key. Moodle hands out one every 6 minutes, so wait for it when needed."""
        for attempt in range(4):
            wait = 370 - (time.time() - self.last_key)
            if self.last_key and wait > 0:
                say(f"   waiting {int(wait)}s for a new web session key")
                time.sleep(wait)
            try:
                key = mp.ws(self.token, "tool_mobile_get_autologin_key", privatetoken=self.private)
                self.last_key = time.time()
                mp.secrets.append(key["key"])
                return key
            except Exception as exc:  # usually: a key was handed out less than 6 minutes ago (an earlier run)
                say(f"   no web session key yet ({exc})")
                self.last_key = time.time()
        raise RuntimeError("Moodle gave no web session key")

    def start(self, tries=3):
        """Moodle accepts the key only from the client IP that asked for it. Moodle sits behind Akamai, and
        every request is pinned to one Akamai address (see pin_moodle_address) and sent as POST, which keeps
        that IP the same; if it still differs, try again with a new key."""
        if not self.private:
            say("No private token, so no web session: videostream recordings cannot be resolved.")
            return False
        for attempt in range(tries):
            key = self.key()
            query = urllib.parse.urlencode({"userid": self.userid, "key": key["key"], "urltogo": mp.BASE + "/my/"}).encode()
            try:
                with opener.open(urllib.request.Request(key["autologinurl"], data=query), timeout=60) as r:
                    home = r.read().decode("utf-8", "replace")
                self.ok = "logout.php" in home
                why = "" if self.ok else "no logout link on the page"
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                why = f"HTTP {exc.code}: " + " | ".join(mp.text(m)[:120] for m in re.findall(
                    r"(?is)<div[^>]*(?:errormessage|alert)[^>]*>(.*?)</div>", body)[:2])
                self.ok = False
            say(f"Web session: {'OK' if self.ok else 'FAILED (' + why + ')'}")
            if self.ok:
                return True
        return False

    def raw(self, url):
        with opener.open(url, timeout=60) as r:
            return r.read().decode("utf-8", "replace")

    def get(self, url):
        page = self.raw(url)
        if "logout.php" not in page and re.search(r'id="login|loginform|login/index\.php', page):
            if self.connect():
                page = self.raw(url)
        return page


# ---------- finding the recordings ----------

def course_dirs():
    out = {}
    for c in json.load(open(CATALOG / "courses.json", encoding="utf-8")):
        if c.get("not_a_course"):
            continue
        for o in c["offerings"]:
            if o.get("moodle_id"):
                out[o["moodle_id"]] = {"key": c["key"], "name": c["name_he"], "year": o["year"], "semester": o["semester"],
                                       "dir": ROOT / o["year"] / SEM_DIR[o["semester"]] / mp.safe(c["name_he"], 60)}
    return out


def course_meta(info, cid):
    path = info["dir"] / "מידע על הקורס" / f"_moodle_{cid}.json"
    if not os.path.exists(mp.longp(path)):
        return {}
    return json.load(open(mp.longp(path), encoding="utf-8"))


def links_in(page):
    """(url, anchor text) of every link on a page."""
    out = []
    for href, inner in re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
        out.append((html.unescape(href), mp.text(inner)))
    return out


def media_in(page):
    found = re.findall(r'<source[^>]+src="([^"]+)"', page, re.I)
    found += re.findall(r'https?://[^"\'\s<>]+?\.(?:mp4|m3u8|m4a|mp3)(?:\?[^"\'\s<>]*)?', page, re.I)
    return list(dict.fromkeys(html.unescape(u) for u in found))


def save_debug(name, page):
    os.makedirs(DEBUG, exist_ok=True)
    (DEBUG / name).write_text(mp.mask(page), encoding="utf-8")


def resolve_player(web, url, debug_name=None):
    """mp4 (or m3u8) URLs behind a player page, following viewvideo links and iframes one level deep."""
    page = web.get(url)
    media = media_in(page)
    if not media:
        nested = [u for u, _ in links_in(page) if "viewvideo.php" in u]
        nested += [html.unescape(u) for u in re.findall(r'<iframe[^>]+src="([^"]+)"', page, re.I)]
        for inner in nested[:3]:
            inner = urllib.parse.urljoin(url, inner)
            media = media_in(web.get(inner))
            if media:
                break
    if not media and debug_name:
        save_debug(debug_name, page)
    return media


def key_of(url):
    clean = re.sub(r"([?&])(token|key|sesskey)=[^&]*", r"\1", url)
    return hashlib.sha1(clean.encode()).hexdigest()[:16]


def find_recordings(web, cid, info, meta):
    """Every recording of one course, in course order: dicts with title, kind, url."""
    found, seen = [], set()

    def add(title, kind, url, page=""):
        k = key_of(url)
        if k in seen:
            return
        seen.add(k)
        found.append({"title": title, "kind": kind, "url": url, "page": page, "key": k})

    pages_done = set()
    for rec in meta.get("recordings", []):
        url, name, mod = rec.get("url") or "", rec.get("name") or "הקלטה", rec.get("modname")
        if mod is None and "pluginfile.php" in url:
            add(name, "file", url)
        elif "zoom.us" in url:
            add(name, "zoom_link", url)
        elif re.search(r"youtu\.?be", url):
            add(name, "youtube_link", url)
        elif "dropbox.com" in url:
            add(name, "direct", re.sub(r"([?&])dl=0", r"\1dl=1", url))
        elif mod == "zoom":
            continue  # the Zoom meeting entry page; its cloud recordings, if listed, are Zoom links
        elif "videostream" in url or "viewvideo.php" in url or "blocks/video" in url:
            if not web.ok:
                add(name, "unresolved", url, url)
                continue
            pages_done.add(url)
            try:
                media = [KNOWN_MEDIA[url]] if url in KNOWN_MEDIA else \
                    resolve_player(web, url, f"{cid}_{len(found):03d}.html" if len(found) < 2 else None)
            except Exception as exc:
                say(f"   {name}: page failed: {exc}")
                continue
            for n, m in enumerate(media[:4]):
                add(name if n == 0 else f"{name} ({n + 1})", "stream", m, url)
            if not media:
                add(name, "unresolved", url, url)
        else:
            add(name, "other_link", url)
    if web.ok:  # the video block ("מדיה של הקורס") lists recordings that no module links to
        try:
            mine = my_groups(web, cid)
            listed = video_list(web, cid)
            kept = [v for v in listed if in_my_group(v["group"], mine)]
            say(f"   video list: {len(listed)} recordings, {len(kept)} in my groups {sorted(mine) or '(no groups: all kept)'}")
            for v in kept:
                if v["url"] in pages_done:
                    continue
                media = [KNOWN_MEDIA[v["url"]]] if v["url"] in KNOWN_MEDIA else resolve_player(web, v["url"])[:1]
                for m in media:
                    add(v["title"], "stream", m, v["url"])
                    found[-1].update(group=v["group"], owner=v["owner"], length=v["length"], created=v["created"])
        except Exception as exc:
            say(f"   video list failed: {exc}")
    return found


KNOWN_MEDIA = {}  # player page -> mp4, from an earlier run's recordings.jsonl, so pages are not opened twice


def my_groups(web, cid):
    """Liran's group numbers in a course (lecture group, recitation group), from the web service.

    The video list shows the recordings of every group: other lecturers' sections and other TAs' recitations.
    """
    try:
        got = mp.ws(web.token, "core_group_get_course_user_groups", courseid=cid, userid=web.userid)
    except Exception:
        return set()
    out = set()
    for g in got.get("groups", []):
        name = g.get("name") or ""
        # many courses also put students in submission groups ("Assignment_1_48", "הגשה_שניה 113"); their numbers are not sections
        if re.search(r"(?i)assign|submi|project|הגשה|מטלה|עבודה|פרויקט|תרגיל\s*בית", name):
            continue
        for n in re.findall(r"\d+", name):
            out.add(int(n))
    return out


def in_my_group(group, mine):
    """True for recordings of my groups, recordings without a group, and every recording when my groups are unknown."""
    numbers = {int(n) for n in re.findall(r"\d+", group or "")}
    return not mine or not numbers or bool(numbers & mine)


def video_list(web, cid, max_pages=60):
    """(player url, title) of every recording in the course's video block, following the list's pages.

    The list shows 10 recordings a page, and its links all say "הקלטה", so the title is the text of the
    recording's table row (date, time, name). Oldest first, by the recording's id.
    """
    first = f"{mp.BASE}/blocks/video/videoslist.php?courseid={cid}"
    queue, seen_pages, players = [first], set(), {}
    while queue and len(seen_pages) < max_pages:
        page_url = queue.pop(0)
        if page_url in seen_pages:
            continue
        seen_pages.add(page_url)
        listing = web.get(page_url)
        if page_url == first:
            save_debug(f"{cid}_videoslist.html", listing)
        rows = re.findall(r"(?is)<tr\b.*?</tr>", listing) or [listing]
        for row in rows:
            for href, anchor in links_in(row):
                if "viewvideo.php" not in href:
                    continue
                url = urllib.parse.urljoin(mp.BASE + "/", href)
                # columns: thumbnail | name | group | length | owner | created ("17-11-2026 12:07:01")
                cells = [re.sub(r"\s+", " ", mp.text(c)).strip() for c in re.findall(r"(?is)<td\b[^>]*>(.*?)</td>", row)]
                if cells and not cells[0]:
                    cells = cells[1:]
                cells += [""] * (5 - len(cells))
                title = cells[0] or anchor or "הקלטה"
                created = re.search(r"\d{1,2}-\d{1,2}-\d{4}", cells[4])
                if created and created.group(0) not in title:
                    title = f"{title} ({created.group(0)})"
                players.setdefault(url, {"url": url, "title": title[:90], "group": cells[1], "length": cells[2],
                                         "owner": cells[3], "created": cells[4]})
        for href, _ in links_in(listing):
            # only the list's own page links; not the language links (?lang=ar would switch the page language)
            # and not the column sorting links, which show the same recordings again
            nxt = urllib.parse.urljoin(page_url, href)
            if ("videoslist.php" in nxt and f"courseid={cid}" in nxt and re.search(r"[?&](?:page|tpage|spage)=\d", nxt)
                    and not re.search(r"[?&](?:lang|tsort|tdir)=", nxt) and nxt not in seen_pages):
                queue.append(nxt)
    vid = lambda u: int((re.search(r"[?&]id=(\d+)", u) or [0, 0])[1])  # noqa: E731
    return sorted(players.values(), key=lambda p: vid(p["url"]))


# ---------- downloading ----------

def download(url, dest, headers, expect_media=True):
    """url -> dest, resuming a partial download. Raises on failure."""
    part = str(dest) + ".part"
    os.makedirs(os.path.dirname(part), exist_ok=True)
    error = None
    for attempt in range(6):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        h = {"User-Agent": BROWSER_UA, **headers}
        if have:
            h["Range"] = f"bytes={have}-"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=120) as r:
                ctype = r.headers.get("Content-Type", "")
                if expect_media and ("html" in ctype or "json" in ctype):
                    raise RuntimeError(f"got {ctype} instead of a recording: " + r.read(300).decode("utf-8", "replace"))
                total = None
                rng = r.headers.get("Content-Range")
                if rng and "/" in rng and rng.split("/")[-1].isdigit():
                    total = int(rng.split("/")[-1])
                elif r.headers.get("Content-Length"):
                    total = int(r.headers["Content-Length"]) + (have if r.status == 206 else 0)
                mode = "ab" if have and r.status == 206 else "wb"
                with open(part, mode) as f:
                    for block in iter(lambda: r.read(1 << 20), b""):
                        f.write(block)
            got = os.path.getsize(part)
            if total and got < total:
                raise IOError(f"stopped at {got} of {total} bytes")
            os.replace(part, dest)
            return
        except RuntimeError:
            raise
        except Exception as exc:
            error = exc
            time.sleep(min(60, 5 * (attempt + 1)))
    raise IOError(f"download failed after retries: {error}")


def to_audio(src, dest):
    tmp = str(dest) + ".part"
    cmd = [ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), *AUDIO_ARGS, "-f", "mp4", tmp]
    run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if run.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 10_000:
        raise RuntimeError("ffmpeg: " + (run.stderr or "no audio")[-300:])
    os.replace(tmp, dest)


def process(job, token):
    """Download one recording, keep its audio (and the video for the current year)."""
    rec = job["rec"]
    audio = pathlib.Path(rec["audio"])
    if os.path.exists(mp.longp(audio)):
        rec["status"] = "done"
        return rec
    url = job["url"]
    headers = {"Referer": mp.ORIGIN + "/"}
    if rec["kind"] == "file":
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode({"token": token})
    keep_video = rec["year"] == CURRENT_YEAR
    video = pathlib.Path(rec["video"]) if keep_video else TMP / f"{rec['key']}.video"
    started = time.time()
    os.makedirs(mp.longp(audio.parent), exist_ok=True)  # older years download to TMP, so nothing made this folder
    try:
        if url.split("?")[0].lower().endswith(".m3u8"):
            os.makedirs(os.path.dirname(mp.longp(audio)), exist_ok=True)
            if keep_video:
                os.makedirs(os.path.dirname(mp.longp(video)), exist_ok=True)
                subprocess.run([ffmpeg(), "-nostdin", "-loglevel", "error", "-y", "-i", url, "-c", "copy", mp.longp(video)], check=True)
                to_audio(mp.longp(video), mp.longp(audio))
            else:
                to_audio(url, mp.longp(audio))
        else:
            if not (keep_video and os.path.exists(mp.longp(video))):
                download(url, mp.longp(video), headers)
            to_audio(mp.longp(video), mp.longp(audio))
            if not keep_video:
                os.remove(mp.longp(video))
        rec["status"] = "done"
        rec["audio_bytes"] = os.path.getsize(mp.longp(audio))
        rec["seconds"] = int(time.time() - started)
    except Exception as exc:
        rec["status"] = "error: " + mp.mask(f"{type(exc).__name__}: {exc}")[:400]
        if not keep_video and os.path.exists(mp.longp(video)):
            os.remove(mp.longp(video))
    return rec


# ---------- main ----------

def download_all(jobs, records, token, workers):
    os.makedirs(TMP, exist_ok=True)
    done = errors = 0
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(process, job, token): job for job in jobs}
        for fut in concurrent.futures.as_completed(futures):
            rec = fut.result()
            if rec["status"] == "done":
                done += 1
            else:
                errors += 1
                say(f"   ERROR {rec['course_he']} | {rec['title']} | {rec['status']}")
            if (done + errors) % 5 == 0 or done + errors == len(jobs):
                say(f"{time.strftime('%H:%M')} recordings: {done} done, {errors} errors, {len(jobs) - done - errors} left")
                with lock:
                    save_index(records)
                    (CATALOG / "recordings_pull_log.txt").write_text("\n".join(log) + "\n", encoding="utf-8")
    save_index(records)
    say(f"TOTAL: {done} recordings saved as audio, {errors} errors")


def save_index(records):
    tmp = INDEX.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in records.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, INDEX)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", type=int, action="append", default=[])
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--resolve-only", action="store_true")
    ap.add_argument("--from-index", action="store_true", help="download what recordings.jsonl lists; no login, no listing")
    ap.add_argument("--no-password", action="store_true", help="use the saved token's autologin instead of the website login")
    args = ap.parse_args()
    RUNNING.write_text(time.strftime("%Y-%m-%d %H:%M"), encoding="utf-8")
    try:
        if args.from_index:  # download what catalog\recordings.jsonl already lists; no web session needed
            saved = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            mp.secrets.extend(s for s in (saved.get("token"), saved.get("privatetoken")) if s)
            records = {r["key"]: r for r in map(json.loads, open(INDEX, encoding="utf-8"))}
            order = lambda r: (r["year"] != CURRENT_YEAR, [-ord(ch) for ch in r["year"]], r["cid"], r["audio"])  # noqa: E731
            todo = sorted((r for r in records.values() if r.get("audio") and r.get("status") != "done"), key=order)
            jobs = [{"url": r["url"], "rec": r} for r in todo]
            say(f"From the list: {len(jobs)} recordings to download")
            download_all(jobs, records, saved["token"], args.workers)
            return
        creds = None
        if sys.stdin.isatty() and not args.no_password:
            say("The recording pages need a login to the Moodle website. The password stays in this window's memory only.")
            creds = ask_password()
        token, private, userid = login(creds)
        web = Web(token, private, userid, creds)
        web.connect()
        dirs = course_dirs()
        ids = [i for i in (args.course or dirs) if i in dirs and i not in SKIP]
        ids.sort(key=lambda i: (dirs[i]["year"] != CURRENT_YEAR, [-ord(ch) for ch in dirs[i]["year"]], i))
        records = {}
        if INDEX.exists():
            old = [json.loads(line) for line in open(INDEX, encoding="utf-8")]
            KNOWN_MEDIA.update({r["page"]: r["url"] for r in old if r.get("kind") == "stream" and r.get("page")})
            # recordings already saved keep their file; the rest are listed again (group filter, clean titles)
            records = {r["key"]: r for r in old if r.get("status") == "done"}
            say(f"{len(records)} recordings already saved; {len(KNOWN_MEDIA)} player pages known from the last run")
        jobs, kinds = [], collections.Counter()
        for n, cid in enumerate(ids, 1):
            info = dirs[cid]
            meta = course_meta(info, cid)
            try:
                found = find_recordings(web, cid, info, meta)
            except Exception as exc:
                say(f"[{n}/{len(ids)}] {cid} {info['name']}: FAILED {exc}")
                continue
            used = {r["audio"] for r in records.values() if r.get("cid") == cid}
            number = 1 + sum(1 for r in records.values() if r.get("cid") == cid and r.get("audio"))
            for item in found:
                kinds[item["kind"]] += 1
                old = records.get(item["key"])
                rec = old or {"key": item["key"], "cid": cid, "course": info["key"], "course_he": info["name"],
                              "year": info["year"], "semester": info["semester"], "title": item["title"], "kind": item["kind"],
                              "url": mp.mask(item["url"]), "page": mp.mask(item.get("page", "")), "status": "listed"}
                if item["kind"] in ("file", "stream", "direct") and not rec.get("audio"):
                    while True:
                        name = mp.safe(f"{number:03d} {item['title']}", 90)
                        audio = info["dir"] / "הקלטות" / f"{name}.m4a"
                        number += 1
                        if str(audio) not in used:
                            break
                    used.add(str(audio))
                    rec["audio"] = str(audio)
                    rec["video"] = str(info["dir"] / "הקלטות" / "וידאו" / f"{name}.mp4")
                records[item["key"]] = rec
                if rec.get("audio") and rec.get("status") != "done":
                    jobs.append({"url": item["url"], "rec": rec})
            counts = collections.Counter(i["kind"] for i in found)
            say(f"[{n}/{len(ids)}] {cid} {info['year']} {info['name']}: " + (", ".join(f"{k} {v}" for k, v in counts.items()) or "none"))
        save_index(records)
        say(f"Listed: {dict(kinds)}; to download now: {len(jobs)}")
        if args.resolve_only:
            return
        download_all(jobs, records, token, args.workers)
    except SystemExit as exc:
        say(f"STOPPED: {exc}")
    except Exception as exc:
        say(f"STOPPED: {type(exc).__name__}: {exc}")
    finally:
        try:
            RUNNING.unlink()
        except OSError:
            pass
        (CATALOG / "recordings_pull_log.txt").write_text("\n".join(log) + "\n", encoding="utf-8")
        try:
            input("Press Enter to close this window")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
