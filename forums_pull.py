"""Downloads every Moodle forum (announcements, Q&A) of every course into the study tree.

    python forums_pull.py                 all courses
    python forums_pull.py --course 61012  only this Moodle course (can repeat)
    python forums_pull.py --saved-token   no password: the token moodle_pull.py --saved-token uses
                                          (%LOCALAPPDATA%\\study-briefing\\moodle.json, or the file in STUDY_MOODLE_TOKEN)

Writes <year>\\<semester>\\<course>\\הודעות ופורומים\\<forum>.md (every discussion and post, oldest first,
with date and author) and the attachments under הודעות ופורומים\\קבצים מצורפים\\. Login as in moodle_pull.py;
the password and token are never written. Every attachment is also recorded in catalog\\filed.jsonl.
"""
import argparse
import getpass
import json
import os
import pathlib
import sys
import time

import moodle_pull as mp
import paths

HERE = pathlib.Path(__file__).resolve().parent
CATALOG = HERE / "catalog"
ROOT = HERE.parent
SKIP = {51696, 51776, 60163}
SEM_DIR = {"א": "סמסטר א", "ב": "סמסטר ב", "קיץ": "קיץ"}


def course_dirs():
    out = {}
    for c in json.load(open(CATALOG / "courses.json", encoding="utf-8")):
        for o in c["offerings"]:
            if o.get("moodle_id"):
                out[o["moodle_id"]] = ROOT / o["year"] / SEM_DIR[o["semester"]] / mp.safe(c["name_he"], 60)
    return out


def posts_of(token, names, discussion):
    if "mod_forum_get_discussion_posts" in names:
        try:
            data = mp.ws(token, "mod_forum_get_discussion_posts", discussionid=discussion["discussion"], sortby="created", sortdirection="ASC")
            return [{"subject": p.get("subject"), "author": (p.get("author") or {}).get("fullname"), "time": p.get("timecreated"),
                     "message": p.get("message"), "attachments": [{"filename": a.get("filename"), "fileurl": a.get("url") or a.get("fileurl"),
                                                                   "filesize": a.get("filesize"), "timemodified": a.get("timemodified"),
                                                                   "mimetype": a.get("mimetype")} for a in (p.get("attachments") or [])]}
                    for p in data.get("posts", [])]
        except Exception:
            pass
    return [{"subject": discussion.get("subject") or discussion.get("name"), "author": discussion.get("userfullname"),
             "time": discussion.get("created"), "message": discussion.get("message"),
             "attachments": [{"filename": a.get("filename"), "fileurl": a.get("fileurl"), "filesize": a.get("filesize"),
                              "timemodified": a.get("timemodified"), "mimetype": a.get("mimetype")}
                             for a in (discussion.get("attachments") or []) if isinstance(a, dict)]}]


def saved_login(say):
    """{"token": ...} from the saved token file, if Moodle still accepts it; else {}."""
    if not mp.SAVED_TOKEN.exists():
        say(f"No saved token at {mp.SAVED_TOKEN}")
        return {}
    saved = json.loads(mp.SAVED_TOKEN.read_text(encoding="utf-8"))
    mp.secrets.extend(s for s in (saved.get("token"), saved.get("privatetoken")) if s)
    try:
        mp.ws(saved["token"], "core_webservice_get_site_info")
    except Exception as exc:
        say(f"The saved token no longer works ({exc})")
        return {}
    say("Logged in with the saved token")
    return saved


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--course", type=int, action="append", default=[])
    parser.add_argument("--saved-token", action="store_true", help="use the saved Moodle token; never asks for a password")
    args = parser.parse_args()
    log = []
    say = lambda s="": (log.append(mp.mask(s)), print(mp.mask(s), flush=True))
    try:
        login = saved_login(say) if args.saved_token else {}
        if args.saved_token and not login:
            return  # the nightly run has no keyboard: stop instead of asking for a password
        for attempt in range(0 if login else 3):
            username = input("Username (English keyboard, without @post.bgu.ac.il): ").strip()
            password = getpass.getpass("Password (hidden while typing): ")
            if password:
                mp.secrets.append(password)
            login = mp.post_json(mp.BASE + "/login/token.php", {"username": username, "password": password, "service": mp.SERVICE})
            if "token" in login:
                break
            say(f"login FAILED: {login.get('error')}. Try again ({2 - attempt} left).")
        if "token" not in login:
            return
        token = login["token"]
        mp.secrets.append(token)
        info = mp.ws(token, "core_webservice_get_site_info")
        names = {f["name"] for f in info.get("functions", [])}
        dirs = course_dirs()
        ids = [i for i in (args.course or sorted(dirs)) if i not in SKIP and i in dirs]
        filed_path = CATALOG / "filed.jsonl"
        filed = {paths.local(r["dest"]): r for r in map(json.loads, open(filed_path, encoding="utf-8"))} if filed_path.exists() else {}
        total_posts = total_files = 0
        for n, cid in enumerate(ids, 1):
            try:
                forums = mp.ws(token, "mod_forum_get_forums_by_courses", courseids=[cid])
            except Exception as exc:
                say(f"[{n}/{len(ids)}] {cid}: forums failed: {exc}")
                continue
            out_dir = dirs[cid] / "הודעות ופורומים"
            course_posts = course_files = 0
            for forum in forums:
                discussions, page = [], 0
                while True:
                    try:
                        data = mp.ws(token, "mod_forum_get_forum_discussions", forumid=forum["id"], page=page, perpage=100)
                    except Exception:
                        break
                    batch = data.get("discussions", [])
                    discussions += batch
                    if len(batch) < 100:
                        break
                    page += 1
                if not discussions:
                    continue
                lines = [f"# {mp.text(forum.get('name'))}", "", f"פורום מסוג {forum.get('type')} · {len(discussions)} דיונים · הורד {mp.day(time.time())}", ""]
                if mp.text(forum.get("intro")):
                    lines += [mp.text(forum.get("intro")), ""]
                for d in sorted(discussions, key=lambda d: d.get("created") or 0):
                    posts = posts_of(token, names, d)
                    lines += [f"## {mp.text(d.get('name') or d.get('subject'))}", ""]
                    for p in posts:
                        course_posts += 1
                        lines += [f"**{p.get('author') or '?'}**, {mp.day(p.get('time'))}", "", mp.text(p.get("message")) or "(בלי טקסט)"]
                        for a in p["attachments"]:
                            if not a.get("fileurl"):
                                continue
                            dest = out_dir / "קבצים מצורפים" / mp.safe(f"{mp.text(forum.get('name'))[:30]} - {a['filename']}", 100, True)
                            job = {"url": a["fileurl"], "path": dest, "size": int(a.get("filesize") or 0), "mtime": int(a.get("timemodified") or 0),
                                   "mime": a.get("mimetype") or "", "rec": {}}
                            status = mp.fetch(job, token)
                            if str(status.get("status", "")).startswith(("downloaded", "exists")):
                                course_files += 1
                                filed[str(dest)] = {"sha": status.get("sha256"), "dest": paths.portable(dest), "course": None, "kind": "admin",
                                                    "size": os.path.getsize(mp.longp(dest)), "urls": [mp.mask(a["fileurl"])]}
                            lines.append(f"- קובץ מצורף: `{dest.name}`")
                        lines.append("")
                os.makedirs(mp.longp(out_dir), exist_ok=True)
                with open(mp.longp(out_dir / (mp.safe(mp.text(forum.get("name")), 60) + ".md")), "w", encoding="utf-8") as f:
                    f.write(mp.mask("\n".join(lines)) + "\n")
            total_posts += course_posts
            total_files += course_files
            say(f"[{n}/{len(ids)}] {cid} {dirs[cid].name}: {len(forums)} forums, {course_posts} posts, {course_files} attachments")
        with open(filed_path, "w", encoding="utf-8") as f:
            for r in filed.values():
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        say(f"TOTAL: {total_posts} posts, {total_files} attachments")
    except Exception as exc:
        say(f"STOPPED: {type(exc).__name__}: {exc}")
    finally:
        (CATALOG / "forums_pull_log.txt").write_text("\n".join(log) + "\n", encoding="utf-8")
        try:
            input("Press Enter to close this window")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
