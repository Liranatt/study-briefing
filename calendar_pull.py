"""Reads Liran's Google Calendar through its secret iCal address and keeps the upcoming events.

    python -X utf8 calendar_pull.py            the next 60 days -> catalog\\calendar\\upcoming.json
    python -X utf8 calendar_pull.py --days 120
    python -X utf8 calendar_pull.py --test     download and count; write nothing

The address ("Secret address in iCal format" in Google Calendar's settings) is in ~/study/secrets/calendar.env as
CALENDAR_ICS_URL; `setup.sh calendar` writes it. Read-only: nothing is ever sent to the calendar.
Recurring events are expanded (icalendar + recurring-ical-events). Times are kept in Israel time.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import urllib.request
import zoneinfo

import study_secrets

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "catalog" / "calendar"
TZ = zoneinfo.ZoneInfo("Asia/Jerusalem")


def as_time(value):
    if isinstance(value, dt.datetime):
        return (value if value.tzinfo else value.replace(tzinfo=TZ)).astimezone(TZ).isoformat(timespec="minutes"), False
    return value.isoformat(), True  # a date: an all-day event


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    # several calendars: the addresses are separated by spaces (setup.sh calendar asks one per line)
    urls = [u for u in study_secrets.load("calendar.env").get("CALENDAR_ICS_URL", "").split() if u.startswith("https://")]
    if not urls:
        print(f"no calendar address in {study_secrets.path('calendar.env')}: run setup.sh calendar")
        return 2
    import icalendar
    import recurring_ical_events
    start = dt.datetime.now(TZ) - dt.timedelta(days=1)
    end = start + dt.timedelta(days=args.days + 1)
    events, seen, failed = [], set(), 0
    for n, url in enumerate(urls, 1):
        req = urllib.request.Request(url, headers={"User-Agent": "study-briefing calendar_pull"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                cal = icalendar.Calendar.from_ical(r.read())
        except Exception as exc:  # the address itself is a secret: never print it
            print(f"calendar {n}: download failed: {type(exc).__name__} {getattr(exc, 'code', '')}")
            failed += 1
            continue
        name = str(cal.get("X-WR-CALNAME") or f"יומן {n}")
        count = 0
        for ev in recurring_ical_events.of(cal).between(start, end):
            s, all_day = as_time(ev.get("DTSTART").dt)
            key = (str(ev.get("UID") or ""), s)
            if key in seen:  # the same event shared into two of the calendars
                continue
            seen.add(key)
            e = ev.get("DTEND")
            events.append({"title": str(ev.get("SUMMARY") or ""), "start": s, "end": as_time(e.dt)[0] if e else None,
                           "all_day": all_day, "location": str(ev.get("LOCATION") or ""),
                           "description": str(ev.get("DESCRIPTION") or "")[:500], "uid": key[0], "calendar": name})
            count += 1
        print(f"calendar {n}: {count} events in the next {args.days} days")
    if failed == len(urls):
        return 1
    events.sort(key=lambda e: e["start"])
    print(f"{len(events)} events in the next {args.days} days from {len(urls) - failed} calendar(s)" +
          (f"; the first on {events[0]['start'][:10]}" if events else ""))
    if args.test:
        print("test done. Nothing was written.")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = OUT / "upcoming.json.tmp"
    tmp.write_text(json.dumps({"pulled": dt.datetime.now(TZ).isoformat(timespec="minutes"), "days": args.days,
                               "events": events}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, OUT / "upcoming.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
