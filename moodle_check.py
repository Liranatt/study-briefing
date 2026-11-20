"""Checks what the BGU Moodle mobile API gives one student account.

The username and password are typed at the keyboard. They and the token stay in memory
only. The report, with no secrets in it, is written to moodle_check_result.txt next to
this file.
"""
import collections
import getpass
import http.cookiejar
import pathlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request

ORIGIN = "https://moodle.bgu.ac.il"
BASE = ORIGIN + "/moodle"
SERVICE = "moodle_mobile_app"
# Moodle hands out an autologin key only to a client that identifies as its mobile app.
APP_UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/130.0 Mobile Safari/537.36 MoodleMobile 4.5.0 (45000)")
OUT = pathlib.Path(__file__).with_name("moodle_check_result.txt")
RECENT_COURSES = 8
WANTED = [
    "core_enrol_get_users_courses", "core_course_get_contents", "core_block_get_course_blocks",
    "core_course_get_updates_since", "mod_assign_get_assignments", "mod_assign_get_submission_status",
    "mod_quiz_get_quizzes_by_courses", "core_calendar_get_action_events_by_timesort",
    "mod_forum_get_forums_by_courses", "mod_forum_get_forum_discussions",
    "message_popup_get_popup_notifications", "gradereport_user_get_grade_items",
    "gradereport_overview_get_course_grades", "tool_mobile_get_autologin_key", "tool_mobile_get_content",
]
VIDEO_HINT = re.compile(r"viewvideo|video|media|zoom|cloudfront|\.mp4|הקלט|וידאו|סרטון", re.I)

lines = []
secrets = []
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
opener.addheaders = [("User-Agent", APP_UA)]


def say(text=""):
    for secret in secrets:
        text = text.replace(secret, "<hidden>")
    lines.append(text)
    print(text)


def mask(url):
    url = re.sub(r"(token|key|wstoken|sesskey)=[^&\"']+", r"\1=<hidden>", url)
    return re.sub(r"[0-9a-fA-F]{16,}", "<hash>", url)


def day(ts):
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else "-"


def flatten(prefix, value, out):
    if isinstance(value, dict):
        for key, item in value.items():
            flatten(f"{prefix}[{key}]", item, out)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            flatten(f"{prefix}[{index}]", item, out)
    else:
        out[prefix] = value


def post_json(url, data):
    body = urllib.parse.urlencode(data).encode()
    with opener.open(urllib.request.Request(url, data=body), timeout=40) as response:
        return json.loads(response.read().decode("utf-8"))


def get_html(url):
    with opener.open(url, timeout=40) as response:
        return response.read().decode("utf-8", "replace")


def ws(token, function, **params):
    data = {"wstoken": token, "wsfunction": function, "moodlewsrestformat": "json"}
    for key, value in params.items():
        flatten(key, value, data)
    result = post_json(BASE + "/webservice/rest/server.php", data)
    if isinstance(result, dict) and result.get("exception"):
        raise RuntimeError(f"{result.get('errorcode')}: {result.get('message')}")
    return result


def step(title, action):
    say()
    say(f"== {title}")
    try:
        return action()
    except Exception as error:  # one failed step must not hide the others
        say(f"   FAILED: {type(error).__name__}: {error}")
        return None


def video_links(html):
    hrefs = re.findall(r'href="([^"]+)"', html)
    return sorted({h.replace("&amp;", "&") for h in hrefs if h.startswith(BASE) and re.search(r"video|media", h, re.I)})


def probe_file(url):
    for label, headers in (("with Referer", {"Referer": ORIGIN + "/"}), ("without Referer", {})):
        request = urllib.request.Request(url, headers={"Range": "bytes=0-1023", "User-Agent": "Mozilla/5.0", **headers})
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                say(f"   file {label}: HTTP {response.status}, type {response.headers.get('Content-Type')}, "
                    f"range {response.headers.get('Content-Range')}")
        except Exception as error:
            say(f"   file {label}: {type(error).__name__}: {error}")


def main():
    say(f"BGU Moodle check, {day(time.time())}")
    username = input("Username: ").strip()
    password = getpass.getpass("Password (hidden while typing): ")
    secrets.append(password)

    login = post_json(BASE + "/login/token.php", {"username": username, "password": password, "service": SERVICE})
    if "token" not in login:
        say(f"== 1. login FAILED: {login.get('errorcode')} - {login.get('error')}")
        return
    token, private = login["token"], login.get("privatetoken")
    secrets.extend(s for s in (token, private) if s)
    say(f"== 1. login OK. private token: {'yes' if private else 'no'}")

    def site_info():
        info = ws(token, "core_webservice_get_site_info")
        names = {f["name"] for f in info.get("functions", [])}
        say(f"   Moodle {info.get('release')}, {len(names)} functions, downloadfiles={info.get('downloadfiles')}")
        for name in WANTED:
            say(f"   {'yes' if name in names else 'NO '}  {name}")
        extra = sorted(n for n in names if re.search(r"video|zoom|media|kaltura|panopto", n, re.I))
        say(f"   video-related functions: {', '.join(extra) or 'none'}")
        return info, names
    result = step("2. site info and available functions", site_info)
    if not result:
        return
    info, names = result
    userid = info["userid"]

    def list_courses():
        courses = ws(token, "core_enrol_get_users_courses", userid=userid)
        courses.sort(key=lambda c: c.get("startdate") or 0, reverse=True)
        say(f"   {len(courses)} courses")
        for course in courses:
            say(f"   id {course['id']}  start {day(course.get('startdate'))[:10]}  {course.get('fullname')}")
        return courses
    courses = step("3. courses", list_courses) or []
    recent = courses[:RECENT_COURSES]

    def contents():
        for course in recent:
            sections = ws(token, "core_course_get_contents", courseid=course["id"])
            modules = [m for s in sections for m in s.get("modules", [])]
            kinds = collections.Counter(m.get("modname") for m in modules)
            say(f"   course {course['id']}: {len(sections)} sections, " + ", ".join(f"{k}={v}" for k, v in kinds.most_common()))
            hits = []
            for module in modules:
                files = module.get("contents") or []
                text = " ".join([module.get("modname", ""), module.get("name", ""), module.get("url", "")]
                                + [f.get("fileurl", "") + " " + (f.get("mimetype") or "") for f in files])
                if VIDEO_HINT.search(text):
                    hits.append(module)
            say(f"      looks like a recording: {len(hits)}")
            for module in hits[:5]:
                link = (module.get("contents") or [{}])[0].get("fileurl") or module.get("url", "")
                say(f"      [{module.get('modname')}] {module.get('name')} | {mask(link)}")
    step("4. course contents (module types, recordings)", contents)

    def blocks():
        if "core_block_get_course_blocks" not in names:
            say("   function not available")
            return
        for course in recent[:4]:
            found = ws(token, "core_block_get_course_blocks", courseid=course["id"], returncontents=1).get("blocks", [])
            say(f"   course {course['id']}: " + ", ".join(b.get("name", "?") for b in found))
            for block in found:
                body = json.dumps(block.get("contents") or {}, ensure_ascii=False)
                if VIDEO_HINT.search(block.get("name", "") + body):
                    for link in sorted(set(re.findall(r'href=\\"([^"\\]+)', body)))[:5]:
                        say(f"      [{block.get('name')}] {mask(link)}")
    step("5. course blocks", blocks)

    def tasks():
        now = int(time.time())
        data = ws(token, "mod_assign_get_assignments", courseids=[c["id"] for c in recent])
        for course in data.get("courses", []):
            assignments = course.get("assignments", [])
            upcoming = sorted((a for a in assignments if (a.get("duedate") or 0) > now), key=lambda a: a["duedate"])
            say(f"   course {course['id']}: {len(assignments)} assignments, {len(upcoming)} still open")
            for assignment in upcoming[:5]:
                say(f"      due {day(assignment['duedate'])}  {assignment.get('name')}")
        events = ws(token, "core_calendar_get_action_events_by_timesort", timesortfrom=now, limitnum=20).get("events", [])
        say(f"   calendar: {len(events)} upcoming events")
        for event in events[:10]:
            say(f"      {day(event.get('timesort'))}  [{event.get('modulename')}] {event.get('name')}")
    step("6. assignments and calendar", tasks)

    def web_session():
        if not private or "tool_mobile_get_autologin_key" not in names:
            say("   no private token or no autologin function, so no web session this way")
            return
        key = ws(token, "tool_mobile_get_autologin_key", privatetoken=private)
        secrets.append(key["key"])
        query = urllib.parse.urlencode({"userid": userid, "key": key["key"], "urltogo": BASE + "/my/"})
        home = get_html(key["autologinurl"] + "?" + query)
        logged_in = "logout.php" in home
        say(f"   web session through autologin: {'OK' if logged_in else 'NOT logged in'}")
        if not logged_in:
            return
        for course in recent[:4]:
            links = video_links(get_html(f"{BASE}/course/view.php?id={course['id']}"))
            say(f"   course {course['id']}: {len(links)} video/media links on the course page")
            for link in links[:6]:
                say(f"      {mask(link)}")
            players = [l for l in links if "viewvideo.php" in l]
            for page in [l for l in links if "viewvideo.php" not in l][:3]:
                if players:
                    break
                players = [l for l in video_links(get_html(page)) if "viewvideo.php" in l]
                say(f"      list page {mask(page)} -> {len(players)} recordings")
            if not players:
                continue
            player = get_html(players[0])
            sources = re.findall(r'<source[^>]+src="([^"]+)"', player) or re.findall(r'https?://[^"\'\s]+\.mp4[^"\'\s]*', player)
            say(f"      player page {mask(players[0])} -> {len(sources)} sources")
            if sources:
                source = sources[0].replace("&amp;", "&")
                say(f"      source: {mask(source)}")
                probe_file(source)
                return
    step("7. web session and a recording page", web_session)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        main()
    except Exception as error:
        say(f"STOPPED: {type(error).__name__}: {error}")
    finally:
        OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nReport saved to {OUT.name}")
        input("Press Enter to close this window")
