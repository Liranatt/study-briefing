"""Downloads course material that Moodle only links to: course websites on github.io, GitHub repositories,
and public Google Docs / Drive files. No login; only public pages are fetched.

    python -X utf8 external_pull.py [--max-per-site 300]

For every link in the courses' _moodle_<id>.json, writes into <course>\\חומר מאתרים חיצוניים\\<host>\\...
Pages that need a login are skipped and listed in catalog\\external_pull_log.txt. Every file is recorded in
catalog\\filed.jsonl with its URL.
"""
import argparse
import collections
import hashlib
import html
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
CATALOG = HERE / "catalog"
ROOT = HERE.parent
UA = "Mozilla/5.0 (study-archive; personal use)"
DOC_EXT = re.compile(r"\.(pdf|pptx?|docx?|xlsx?|zip|ipynb|py|ts|js|rkt|scm|c|h|java|txt|md|csv|json|tex)$", re.I)
SEM_DIR = {"א": "סמסטר א", "ב": "סמסטר ב", "קיץ": "קיץ"}
log = []


def say(s):
    log.append(s)
    print(s, flush=True)


def safe(name, limit=80):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", urllib.parse.unquote(name))
    return re.sub(r"\s+", " ", name).strip(" .")[:limit] or "_"


def get(url, limit=60 * 2**20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(limit + 1)
        if len(data) > limit:
            raise ValueError("too large")
        return data, r.headers.get("Content-Type", ""), r.geturl()


def save(path, data):
    p = str(path)
    if len(p) > 250:
        p = "\\\\?\\" + p
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def crawl_site(start, base_dir, max_files):
    """Same-host pages under the start path, and the documents they link to."""
    u = urllib.parse.urlparse(start)
    prefix = u.path.rsplit("/", 1)[0] + "/" if not u.path.endswith("/") else u.path
    queue, seen, files = [start], set(), []
    while queue and len(files) < max_files:
        url = queue.pop(0).split("#")[0]
        if url in seen:
            continue
        seen.add(url)
        try:
            data, ctype, final = get(url)
        except Exception as exc:
            say(f"   skip {url}: {exc}")
            continue
        path = urllib.parse.urlparse(final).path
        name = path.strip("/").replace("/", " ⁄ ") or "index"
        if "html" in ctype:
            files.append((base_dir / (safe(name, 100) + ("" if name.endswith(".html") else ".html")), data, final))
            for href in re.findall(r'href="([^"#]+)"', data.decode("utf-8", "replace")):
                nxt = urllib.parse.urljoin(final, html.unescape(href))
                nu = urllib.parse.urlparse(nxt)
                if nu.netloc == u.netloc and nu.path.startswith(prefix) and nxt not in seen:
                    queue.append(nxt)
        else:
            files.append((base_dir / safe(name, 100), data, final))
    return files


def github_repo(url):
    m = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)(?:/(tree|blob)/([^/]+)(/.*)?)?", url)
    if not m:
        return []
    owner, repo, kind, branch, rest = m.groups()
    repo = repo.removesuffix(".git")
    if kind == "blob" and branch and rest:
        raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}{rest}"
        data, _, _ = get(raw)
        return [(pathlib.Path(safe(f"{owner} {repo}")) / safe(rest.strip("/").replace("/", " ⁄ "), 100), data, raw)]
    for br in ([branch] if branch else ["main", "master"]):
        try:
            data, _, final = get(f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{br}")
            return [(pathlib.Path(safe(f"{owner} {repo} ({br}).zip")), data, url)]
        except Exception:
            continue
    return []


def google(url):
    m = re.search(r"docs\.google\.com/(document|spreadsheets|presentation)/d/([\w-]+)", url)
    if m:
        kind, did = m.groups()
        fmt = {"document": "pdf", "spreadsheets": "xlsx", "presentation": "pdf"}[kind]
        exp = f"https://docs.google.com/{kind}/d/{did}/export?format={fmt}" if kind != "presentation" else f"https://docs.google.com/presentation/d/{did}/export/pdf"
        data, ctype, _ = get(exp)
        if "html" in ctype:
            raise PermissionError("needs a Google login")
        return [(pathlib.Path(f"{did}.{fmt}"), data, url)]
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?id=)([\w-]+)", url)
    if m:
        data, ctype, _ = get(f"https://drive.google.com/uc?export=download&id={m.group(1)}")
        if "html" in ctype:
            raise PermissionError("needs a Google login, or is too large to fetch directly")
        return [(pathlib.Path(m.group(1)), data, url)]
    raise PermissionError("Google Drive folders need a login")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-per-site", type=int, default=300)
    args = ap.parse_args()
    courses = json.load(open(CATALOG / "courses.json", encoding="utf-8"))
    sys.path.insert(0, str(HERE))
    from organize import safe as course_name
    dirs = {o["moodle_id"]: ROOT / o["year"] / SEM_DIR[o["semester"]] / course_name(c["name_he"], 60)
            for c in courses for o in c["offerings"] if o.get("moodle_id")}
    files = [json.loads(l) for l in open(CATALOG / "files.jsonl", encoding="utf-8")]
    links = collections.OrderedDict()
    for p in (r for r in files if r["name"].startswith("_moodle_") and r["name"].endswith(".json")):
        meta = json.load(open(ROOT / p["path"], encoding="utf-8"))
        cid = meta["course"]["id"]
        if cid not in dirs:
            continue
        for l in meta.get("links", []):
            u = l["url"]
            if re.search(r"github\.io|github\.com|docs\.google\.com|drive\.google\.com", u) and "<hidden>" not in u:
                links.setdefault(u, (cid, l["name"]))
    filed_path = CATALOG / "filed.jsonl"
    filed = {r["dest"]: r for r in map(json.loads, open(filed_path, encoding="utf-8"))}
    done_urls = {u for r in filed.values() for u in r.get("urls", [])}
    sites_done, saved = set(), 0
    for url, (cid, title) in links.items():
        host = urllib.parse.urlparse(url).netloc
        base = dirs[cid] / "חומר מאתרים חיצוניים" / safe(host, 40)
        try:
            if host.endswith("github.io"):
                key = (host, cid)
                if key in sites_done:
                    continue  # one crawl per site and course; it follows the site's own links
                sites_done.add(key)
                got = crawl_site(url, base, args.max_per_site)
            elif host == "github.com":
                got = [(base / p, d, u) for p, d, u in github_repo(url)]
            else:
                got = [(base / p, d, u) for p, d, u in google(url)]
        except Exception as exc:
            say(f"SKIP {dirs[cid].name} | {title} | {url} | {type(exc).__name__}: {exc}")
            continue
        for path, data, src in got:
            if src in done_urls:
                continue
            save(path, data)
            done_urls.add(src)
            saved += 1
            filed[str(path)] = {"sha": hashlib.sha256(data).hexdigest(), "dest": str(path), "course": None, "kind": "other",
                                "size": len(data), "urls": [src], "external": True}
        say(f"{dirs[cid].name} | {title} | {len(got)} files")
        time.sleep(0.5)
    with open(filed_path, "w", encoding="utf-8") as f:
        for r in filed.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    say(f"TOTAL saved: {saved} files from {len(links)} links")
    (CATALOG / "external_pull_log.txt").write_text("\n".join(log) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
