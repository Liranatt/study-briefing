"""Reads Liran's BGU student portal (portal.bgu.ac.il) through its JSON API: timetable, exams and tasks, grades,
messages and notifications.

    python -X utf8 portal_pull.py          write catalog\\portal\\*.json
    python -X utf8 portal_pull.py --test   log in and call every endpoint once; print only what came back
                                           (status, keys, counts), write nothing

Login: username, password and the 9-digit ID number from ~/study/secrets/bgu.env (BGU_USER, BGU_PASS, BGU_ID;
`setup.sh bgu` writes it). POST /Portal/Account/StandardLogin {userName, password, id, cultureCode,
isKeepConnected} returns a JWT, sent as "Authorization: Bearer" on every later call, as the portal's own page does.
The request bodies are the ones in the portal's Angular bundle (main.*.js, read on 17.11.2026).

Writes catalog\\portal\\:
    schedule.json      the semester timetable (scheduleByDays: days, hours, rooms) of every semester the filter offers
    courses.json       my courses as the portal lists them (with the semester filter)
    exams_tasks.json   exams, quizzes, assignments, academic events and lessons from 2 weeks ago to 8 months ahead
    grades.json        the detailed grade list (and its filters)
    widgets.json       the home page widgets: grades, messages, events, assignments
    recent.json        "recent information" (the notifications list)
    pages.json         the raw data of the portal pages read (for fixing this script when the portal changes)
    summary.json       when it ran, what worked and what failed
The password and the token are never written or printed.
"""
import argparse
import datetime as dt
import http.cookiejar
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

import study_secrets

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "catalog" / "portal"
BASE = "https://portal.bgu.ac.il"
CULTURE = "he-IL"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
DATA_TYPES = [1, 2, 3, 4, 5, 6]  # Assignment, Quiz, Exam, CourseGrade, Event, Lesson (the bundle's enum)
MAX_PAGES = 20

secrets = []


def mask(value):
    value = str(value)
    for s in secrets:
        if s:
            value = value.replace(s, "<hidden>")
    return re.sub(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", "<jwt>", value)


class Portal:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.token = None

    def post(self, path, body, timeout=60):
        headers = {"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json, text/plain, */*",
                   "Origin": BASE, "Referer": BASE + "/private/home"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        try:
            with self.opener.open(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code} {mask(exc.read(300).decode('utf-8', 'replace'))}") from None
        data = json.loads(raw) if raw.strip() else None
        for _ in range(2):  # some endpoints return JSON inside a JSON string
            if isinstance(data, str) and data[:1] in "[{":
                data = json.loads(data)
        return data

    def login(self, user, password, id_number):
        got =self.post("/Portal/Account/StandardLogin", {"userName": user, "password": password, "id": id_number,
                                                          "cultureCode": CULTURE, "isKeepConnected": False,
                                                          "returnUrl": "/private/home"})
        if not isinstance(got, dict) or not got.get("succeed") or not got.get("token"):
            why = {k: v for k, v in (got or {}).items() if k not in ("token",)} if isinstance(got, dict) else got
            raise RuntimeError(f"login failed: {mask(json.dumps(why, ensure_ascii=False))[:300]}")
        self.token = got["token"]
        secrets.append(self.token)

    def page(self, url):
        return self.post("/Portal/Api/Home/Index", {"pageUrl": url, "cultureCode": CULTURE})


def walk(node, found, key_names):
    """Collect every value under one of key_names, anywhere in node."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in key_names:
                found.setdefault(k, []).append(v)
            walk(v, found, key_names)
    elif isinstance(node, list):
        for v in node:
            walk(v, found, key_names)


def private_links(node, out):
    """'/private/...' page addresses mentioned anywhere in a page's data."""
    if isinstance(node, dict):
        for v in node.values():
            private_links(v, out)
    elif isinstance(node, list):
        for v in node:
            private_links(v, out)
    elif isinstance(node, str):
        for m in re.findall(r"(?:https://portal\.bgu\.ac\.il)?(/private/[\w\-/%]+)", node):
            out.add(m.rstrip("/"))


def iso(day):
    return day.strftime("%Y-%m-%dT00:00:00.000Z")


def shape(value):
    """A short description of what came back, without the content."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {shape(v) if isinstance(v, (list, dict)) else type(v).__name__}"
                               for k, v in list(value.items())[:12]) + "}"
    if isinstance(value, list):
        return f"[{len(value)} items]"
    return type(value).__name__


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="call each endpoint, print shapes only, write nothing")
    args = ap.parse_args()
    creds = study_secrets.load("bgu.env")
    if not all(creds.get(k) for k in ("BGU_USER", "BGU_PASS", "BGU_ID")):
        print(f"no BGU login in {study_secrets.path('bgu.env')}: run setup.sh bgu")
        return 2
    secrets.extend([creds["BGU_PASS"], creds["BGU_ID"]])
    portal = Portal()
    results, errors = {}, {}

    def call(name, fn):
        try:
            results[name] = fn()
            print(f"OK    {name}: {shape(results[name])}", flush=True)
        except Exception as exc:
            errors[name] = mask(f"{type(exc).__name__}: {exc}")[:400]
            print(f"FAIL  {name}: {errors[name]}", flush=True)

    try:
        portal.login(creds["BGU_USER"].strip(), creds["BGU_PASS"], creds["BGU_ID"].strip())
        print("OK    login (token received)")
    except Exception as exc:
        print(f"FAIL  login: {mask(exc)}")
        return 1

    # pages: the home page, then the /private/ pages it links to; the blocks on them carry their filters
    pages, queue, seen = {}, ["/private/home"], set()
    while queue and len(pages) < MAX_PAGES:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            pages[url] = portal.page(url)
        except Exception as exc:
            errors[f"page {url}"] = mask(exc)[:200]
            continue
        links = set()
        private_links(pages[url], links)
        queue += sorted(links - seen)
    print(f"OK    pages read: {len(pages)}")
    found = {}
    walk(pages, found, {"semesterListFilter", "scheduleByDays", "datePeriodSelectOptions", "timeRange"})

    today = dt.date.today()
    # my courses (and the semester filter's values, which the timetable search takes too)
    call("courses", lambda: portal.post("/Portal/StudentAllCoursesBlockList/Index", {
        "cultureCode": CULTURE, "semesterFilterValue": None, "departmentFilterValue": None, "searchValue": None,
        "sectionSize": 100}))
    course_filter = (results.get("courses") or {}).get("semesterFilter") if isinstance(results.get("courses"), dict) else None

    # timetable: the block's own data, then every semester its filter offers
    schedule = {"from_page": found.get("scheduleByDays", []), "semesters": {}}
    options = [o for flt in found.get("semesterListFilter", []) if isinstance(flt, list) for o in flt if isinstance(o, dict)]
    if not options and isinstance(course_filter, list):  # same "year and semester" values, by the look of the bundle
        options = [o for o in course_filter if isinstance(o, dict)][:3]
    for opt in {str(o.get("value")): o for o in options if o.get("value") not in (None, "", "-1")}.values():
        call(f"schedule {opt.get('value')}", lambda o=opt: portal.post(
            "/Portal/StudentCoursesAndScheduleBlockList/SearchSemesterSchedule",
            {"cultureCode": CULTURE, "yearAndSemester": o["value"]}))
        if f"schedule {opt.get('value')}" in results:
            schedule["semesters"][str(opt["value"])] = {"label": opt.get("label"), "checked": opt.get("checked"),
                                                       "data": results.pop(f"schedule {opt.get('value')}")}
    print(f"{'OK  ' if schedule['from_page'] or schedule['semesters'] else 'NONE'}  timetable: "
          f"{len(schedule['from_page'])} from the page, {len(schedule['semesters'])} semesters from the filter")

    # the calendar block's search works; the list-view block's one answers HTTP 400 "request data is null"
    # (checked on the server 18.11.2026), so it is only the fallback
    call("exams_tasks", lambda: portal.post("/Portal/ExamsAndTasksCalendarBlockList/SearchExamsAndTasks", {
        "cultureCode": CULTURE, "dataTypes": DATA_TYPES, "fromDate": iso(today - dt.timedelta(days=14)),
        "toDate": iso(today + dt.timedelta(days=240)), "searchOneDay": False, "term": None}))
    if "exams_tasks" in errors:
        first = errors.pop("exams_tasks")
        call("exams_tasks", lambda: portal.post("/Portal/StudentExamsAndTasksListViewBlockList/SearchExamsAndTasks", {
            "cultureCode": CULTURE, "dataTypes": DATA_TYPES, "fromDate": iso(today - dt.timedelta(days=14)),
            "toDate": iso(today + dt.timedelta(days=240)), "searchOneDay": False, "term": None,
            "showWarningBubblePeriod": None}))
        if "exams_tasks" in errors:
            errors["exams_tasks"] = f"calendar block: {first} | list block: {errors['exams_tasks']}"
    grade_body = {"cultureCode": CULTURE, "activityStatusKey": None, "studentDegree": None, "yearAndSemester": None,
                  "term": None, "sortBy": None, "courseGradeAverageText": None}
    call("grades_index", lambda: portal.post("/Portal/StudentAllGradesDetailedListViewBlockList/Index", grade_body))
    call("grades", lambda: portal.post("/Portal/StudentAllGradesDetailedListViewBlockList/SearchGrades", grade_body))
    call("w_grades", lambda: portal.post("/Portal/Api/Widgets/GetGradesForWidget", {"cultureCode": CULTURE, "maxDisplayItems": 50}))
    call("w_messages", lambda: portal.post("/Portal/Api/Widgets/GetMessagesForWidget",
                                           {"cultureCode": CULTURE, "maxDisplayItems": 50, "pinnedMessages": None}))
    call("w_events", lambda: portal.post("/Portal/Api/Widgets/GetEventsForWidget", {"cultureCode": CULTURE, "maxDisplayItems": 50}))
    call("w_assignments", lambda: portal.post("/Portal/Api/Widgets/GetAssignmentsForWidget", {
        "cultureCode": CULTURE, "maxDisplayItems": 50, "postExpirationDisplayDuration": 14, "maxDisplaySubmitDayCount": 60}))
    call("recent", lambda: portal.post("/Portal/StudentRecentInformationListViewBlockList/SearchStudentRecentInformationListView",
                                       {"cultureCode": CULTURE, "pageSize": 100, "currentPage": 1}))
    if args.test:
        print(f"test done: {len(results)} calls worked, {len(errors)} failed. Nothing was written.")
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    files = {
        "schedule.json": schedule if schedule["from_page"] or schedule["semesters"] else None,
        "courses.json": results.get("courses"),
        "exams_tasks.json": results.get("exams_tasks"),
        "grades.json": {"index": results.get("grades_index"), "search": results.get("grades")},
        "widgets.json": {k[2:]: results.get(k) for k in ("w_grades", "w_messages", "w_events", "w_assignments")},
        "recent.json": results.get("recent"),
        "pages.json": pages,
    }
    for name, data in files.items():
        if data is None or data == {} or (isinstance(data, dict) and all(v is None for v in data.values())):
            continue  # keep the last good copy when this call failed
        tmp = OUT / (name + ".tmp")
        tmp.write_text(mask(json.dumps({"pulled": stamp, "data": data}, ensure_ascii=False, indent=1)), encoding="utf-8")
        os.replace(tmp, OUT / name)
    (OUT / "summary.json").write_text(json.dumps({"pulled": stamp, "worked": sorted(results) + ["pages"] * bool(pages),
                                                  "failed": errors}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"portal: {len(results)} calls worked, {len(errors)} failed -> {OUT}")
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
