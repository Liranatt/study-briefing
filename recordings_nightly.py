"""New lecture recordings on the home server, every night: list them on Moodle and download them as audio
(and the video, for the current year), with recordings_pull.py's own functions. recordings_pull.py is not changed.

    python -X utf8 recordings_nightly.py              the current semester's courses
    python -X utf8 recordings_nightly.py --all        every course
    python -X utf8 recordings_nightly.py --course 68512
    python -X utf8 recordings_nightly.py --test       log in to the website and list the recordings; download nothing

What recordings_pull.py would ask at the keyboard comes from the server's secrets instead:
  - username and password: ~/study/secrets/bgu.env (BGU_USER, BGU_PASS), for the website's own login form
    (recordings_pull.Web.form_login: no 6-minute autologin wait);
  - the Moodle token: ~/study/secrets/moodle.json (or STUDY_MOODLE_TOKEN), as recordings_pull.login() reads it.
The index catalog\\recordings.jsonl keeps its Windows paths (paths.portable), so the PC can still read it.
Temporary video of older years goes to ~/study/tmp/recordings and is deleted after the audio is made.
"""
import argparse
import collections
import datetime as dt
import json
import os
import pathlib
import sys

import paths
import study_secrets

TOKEN_FILE = pathlib.Path(os.environ.get("STUDY_MOODLE_TOKEN") or study_secrets.path("moodle.json"))


def semester_of(today):
    y = today.year
    if today.month >= 10:
        return f"{y}-{(y + 1) % 100:02d}", "א"
    if today.month <= 2:
        return f"{y - 1}-{y % 100:02d}", "א"
    if today.month <= 7:
        return f"{y - 1}-{y % 100:02d}", "ב"
    return f"{y - 1}-{y % 100:02d}", "קיץ"


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--course", type=int, action="append", default=[])
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    creds = study_secrets.load("bgu.env")
    if not creds.get("BGU_USER") or not creds.get("BGU_PASS"):
        print(f"no BGU login in {study_secrets.path('bgu.env')}: run setup.sh bgu")
        return 2

    import moodle_pull as mp
    import recordings_pull as rp
    rp.LOCAL = TOKEN_FILE.parent          # login() makes this folder before it saves the token
    rp.TOKEN_FILE = TOKEN_FILE
    rp.TMP = pathlib.Path.home() / "study" / "tmp" / "recordings"
    mp.secrets.append(creds["BGU_PASS"])

    def to_local(rec):
        rec = dict(rec)
        for k in ("audio", "video"):
            if rec.get(k):
                rec[k] = paths.local(rec[k])
        return rec

    def save_index(records):  # replaces rp.save_index: the file keeps the Windows form
        tmp = rp.INDEX.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in records.values():
                r = dict(r)
                for k in ("audio", "video"):
                    if r.get(k):
                        r[k] = paths.portable(r[k])
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, rp.INDEX)

    rp.save_index = save_index
    old_umask = os.umask(0o077)  # a token saved by login() is readable only by Liran
    try:
        creds_pair = (creds["BGU_USER"].strip(), creds["BGU_PASS"])
        token, private, userid = rp.login(creds_pair)
    finally:
        os.umask(old_umask)
    web = rp.Web(token, private, userid, creds_pair)
    if not web.connect():
        rp.say("website login failed; only the recordings already listed can be downloaded")

    dirs = rp.course_dirs()
    if args.course:
        ids = [i for i in args.course if i in dirs]
    elif args.all:
        ids = list(dirs)
    else:
        year, sem = semester_of(dt.date.today())
        ids = [i for i, d in dirs.items() if d["year"] == year and d["semester"] == sem]
    ids = [i for i in ids if i not in rp.SKIP]
    records = {}
    if rp.INDEX.exists():
        old = [json.loads(line) for line in open(rp.INDEX, encoding="utf-8") if line.strip()]
        rp.KNOWN_MEDIA.update({r["page"]: r["url"] for r in old if r.get("kind") == "stream" and r.get("page")})
        records = {r["key"]: to_local(r) for r in old}  # every course's rows are kept, not only this run's
    jobs, kinds = [], collections.Counter()
    for n, cid in enumerate(ids, 1):
        info = dirs[cid]
        try:
            found = rp.find_recordings(web, cid, info, rp.course_meta(info, cid))
        except Exception as exc:
            rp.say(f"[{n}/{len(ids)}] {cid} {info['name']}: FAILED {exc}")
            continue
        used = {r["audio"] for r in records.values() if r.get("cid") == cid and r.get("audio")}
        number = 1 + sum(1 for r in records.values() if r.get("cid") == cid and r.get("audio"))
        for item in found:
            kinds[item["kind"]] += 1
            rec = records.get(item["key"]) or {
                "key": item["key"], "cid": cid, "course": info["key"], "course_he": info["name"], "year": info["year"],
                "semester": info["semester"], "title": item["title"], "kind": item["kind"], "url": mp.mask(item["url"]),
                "page": mp.mask(item.get("page", "")), "status": "listed"}
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
            if rec.get("audio") and rec.get("status") != "done" and not os.path.exists(rec["audio"]):
                jobs.append({"url": item["url"], "rec": rec})
            elif rec.get("audio") and os.path.exists(rec["audio"]):
                rec["status"] = "done"
        counts = collections.Counter(i["kind"] for i in found)
        rp.say(f"[{n}/{len(ids)}] {cid} {info['year']} {info['name']}: " + (", ".join(f"{k} {v}" for k, v in counts.items()) or "none"))
    rp.say(f"Listed: {dict(kinds)}; new to download: {len(jobs)}")
    if args.test:
        print("test done. Nothing was downloaded or written.")
        return 0
    save_index(records)
    if jobs:
        rp.download_all(jobs, records, token, args.workers)
    (rp.CATALOG / "recordings_nightly_log.txt").write_text("\n".join(rp.log) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
