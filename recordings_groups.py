"""Picks the recordings to keep in courses whose video list mixes the recordings of every group.

In big courses the video block ("מדיה של הקורס") lists every lecture group (01, 02, ...) and every recitation
group (11, 12, 23, ...). Those courses are marked video_block_needs_group in catalog\\recordings.jsonl.
For each one this keeps:
  - Liran's own groups, when Moodle still says which they are;
  - otherwise the lecture group and the recitation group with the most recorded hours;
  - and every recording that belongs to no group (office hours, files the staff uploaded).
The kept recordings become ordinary "stream" entries with a file name; the rest are marked
video_block_other_groups, like the other courses' recordings of other groups.

    python -X utf8 recordings_groups.py            print the choice only
    python -X utf8 recordings_groups.py --apply    write it to catalog\\recordings.jsonl
    python -X utf8 recordings_groups.py --redo --course 54409 --apply
                                                   choose again in a course already split (recordings not yet
                                                   downloaded go back to waiting; uses the group list saved last time)
    python -X utf8 recordings_pull.py --from-index then downloads the kept recordings, and transcribe.py picks them up

Needs the saved Moodle token (recordings_pull.py makes it); no password is asked.
"""
import argparse
import collections
import json
import re
import sys

import moodle_pull as mp
import recordings_pull as rp

NEEDS = "video_block_needs_group"
KINDS = ("lecture", "recitation")


def video_id(url):
    m = re.search(r"[?&]id=(\d+)", url or "")
    return int(m.group(1)) if m else None


def numbers(group):
    return {int(n) for n in re.findall(r"\d+", group or "")}


def kind_of(number):
    """BGU numbers lecture groups 01-09 and recitation groups 11 and up."""
    return "lecture" if number < 10 else "recitation"


def seconds(length):
    total = 0
    for part in re.findall(r"\d+", length or "")[-3:]:
        total = total * 60 + int(part)
    return total


def choose(listed, mine):
    """{kind: (chosen group numbers, why)} and per-group stats [(kind, number, recordings, hours)]."""
    hours, count = collections.Counter(), collections.Counter()
    for v in listed:
        for n in numbers(v["group"]):
            hours[n] += seconds(v["length"]) / 3600
            count[n] += 1
    chosen = {}
    for kind in KINDS:
        groups = [n for n in hours if kind_of(n) == kind]
        if not groups:
            continue
        own = {n for n in groups if n in mine}
        # a student sits in one lecture group and one recitation group; more than one match is not a real signal
        chosen[kind] = (own, "my group") if len(own) == 1 else ({max(groups, key=lambda n: (hours[n], count[n]))}, "most hours")
    stats = sorted((kind_of(n), n, count[n], hours[n]) for n in hours)
    return chosen, stats


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the choice to recordings.jsonl")
    ap.add_argument("--course", type=int, action="append", default=[])
    ap.add_argument("--redo", action="store_true", help="choose again in the --course courses, from the saved group list")
    args = ap.parse_args()
    if args.redo and not args.course:
        raise SystemExit("--redo needs --course")

    records = {r["key"]: r for r in map(json.loads, open(rp.INDEX, encoding="utf-8"))}
    if args.redo:
        for r in records.values():
            if r["cid"] in args.course and "group" in r and r.get("status") != "done" and \
                    r["kind"] in ("stream", "video_block_other_groups"):
                r["kind"] = NEEDS
                r.pop("audio", None)
                r.pop("video", None)
    waiting = collections.defaultdict(list)
    for r in records.values():
        if r["kind"] == NEEDS and (not args.course or r["cid"] in args.course):
            waiting[r["cid"]].append(r)
    if not waiting:
        print("No course waits for a group.")
        return

    web = None
    dirs = rp.course_dirs()
    total_kept = total_hours = 0
    for cid, recs in sorted(waiting.items(), key=lambda kv: dirs[kv[0]]["year"]):
        info = dirs[cid]
        if all("length" in r for r in recs):  # listed by an earlier run: no need to open Moodle again
            listed = [{"url": r["page"], "title": r.get("list_title", r["title"]), "group": r["group"],
                       "length": r["length"], "owner": r["owner"], "created": r["created"]} for r in recs]
            mine = set()
        else:
            if web is None:
                token, private, userid = rp.login()
                web = rp.Web(token, private, userid)
                if not web.connect():
                    raise SystemExit("No Moodle web session, so the video lists cannot be read.")
            listed = rp.video_list(web, cid)
            mine = rp.my_groups(web, cid)
        by_id = {video_id(v["url"]): v for v in listed}
        chosen, stats = choose(listed, mine)
        keep_numbers = set().union(*(c[0] for c in chosen.values())) if chosen else set()
        rp.say(f"\n{info['year']} {info['name']} ({cid}): {len(listed)} in the video list, {len(recs)} waiting;"
               f" Moodle says my groups are {sorted(mine) or 'unknown'}")
        for kind, number, n, h in stats:
            mark = "  <- keep" if number in keep_numbers else ""
            rp.say(f"   {kind:10} {number:02d}: {n:3d} recordings, {h:5.1f} h{mark}")
        for kind, (nums, why) in chosen.items():
            rp.say(f"   {kind}: group {', '.join(f'{n:02d}' for n in sorted(nums))} ({why})")

        used = {r["audio"] for r in records.values() if r.get("cid") == cid and r.get("audio")}
        number = 1 + len(used)
        kept = other = unmatched = 0
        kept_hours = 0.0
        for r in sorted(recs, key=lambda r: video_id(r.get("page")) or 0):
            v = by_id.get(video_id(r.get("page")))
            if v is None:
                unmatched += 1
                continue
            nums = numbers(v["group"])
            extra = {"group": v["group"], "owner": v["owner"], "length": v["length"], "created": v["created"],
                     "list_title": v["title"]}
            if nums and not nums & keep_numbers:
                r.update(kind="video_block_other_groups", **extra)
                other += 1
                continue
            title = v["title"].strip() or r["title"].strip() or "הקלטה"
            while True:
                name = mp.safe(f"{number:03d} {title}", 90)
                audio = info["dir"] / "הקלטות" / f"{name}.m4a"
                number += 1
                if str(audio) not in used:
                    break
            used.add(str(audio))
            r.update(kind="stream", title=title, status="listed", audio=str(audio),
                     video=str(info["dir"] / "הקלטות" / "וידאו" / f"{name}.mp4"), **extra)
            kept += 1
            kept_hours += seconds(v["length"]) / 3600
        rp.say(f"   keep {kept} ({kept_hours:.0f} h), other groups {other}, not in the list any more {unmatched}")
        total_kept += kept
        total_hours += kept_hours

    rp.say(f"\nTOTAL: keep {total_kept} recordings, {total_hours:.0f} h of audio")
    if args.apply:
        rp.save_index(records)
        rp.say("Written to catalog\\recordings.jsonl. Next: python -X utf8 recordings_pull.py --from-index")
    else:
        rp.say("Dry run: nothing written. Add --apply to keep this choice.")


if __name__ == "__main__":
    main()
