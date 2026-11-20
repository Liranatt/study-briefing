#!/usr/bin/env bash
# One-time setup of Liran's study system on the home server. Every secret is typed here, hidden (read -s),
# and stored under ~/study/secrets (folder 700, files 600). Nothing here uses sudo or prints a secret.
#
#   setup.sh bgu            BGU username, password, ID number -> secrets/bgu.env, and a fresh Moodle token
#   setup.sh mail           the BGU mailbox (Google Workspace): a Google app password for IMAP -> secrets/mail.env
#   setup.sh gemini         Gemini API key for generating daily podcasts -> secrets/gemini.env
#   setup.sh calendar       Google Calendar's secret iCal address -> secrets/calendar.env; prints the feed address
#   setup.sh site-password  the site's login -> secrets/web/site_auth (bcrypt, made by caddy hash-password)
#   setup.sh tunnel         a Cloudflare Zero Trust tunnel token -> secrets/cloudflared.env; starts study-tunnel
#   setup.sh cron           adds the nightly run (01:30 UTC) to the crontab; other lines stay as they are
#   setup.sh web            installs the Caddyfile and docker-compose.yml into ~/study/server and (re)starts study-web
#   setup.sh run            runs the nightly pipeline now (log: ~/study/logs/<date>.log)
#   setup.sh test X         tries one collector, writes nothing: portal | mail | calendar | feed | recordings | transcribe
#   setup.sh feed-url       prints the calendar feed address to subscribe to in Google Calendar
#   setup.sh status         what is set up, what ran last
set -euo pipefail

STUDY="$HOME/study"
P="$STUDY/cs/study-briefing"
PY="$STUDY/venv/bin/python"
SECRETS="$STUDY/secrets"
SRV="$STUDY/server"
COMPOSE=(docker compose -p study -f "$SRV/docker-compose.yml")
CRON_LINE="30 1 * * * $P/server/nightly.sh"
export STUDY_SECRETS="$SECRETS" STUDY_MOODLE_TOKEN="$SECRETS/moodle.json" PYTHONIOENCODING=utf-8 TZ=Asia/Jerusalem
umask 077
mkdir -p "$SECRETS" "$SECRETS/web"
chmod 700 "$SECRETS" "$SECRETS/web"

say() { printf '%s\n' "$*"; }
# write_secret <file>: stdin -> file, mode 600, replaced in one step
write_secret() { local f="$1"; cat > "$f.new"; chmod 600 "$f.new"; mv -f "$f.new" "$f"; }

cmd_bgu() {
    say "BGU login (the same as Moodle and the portal). Typing is hidden for the password and ID."
    read -r -p "Username (English, without @post.bgu.ac.il): " user
    read -r -s -p "Password: " pass; echo
    read -r -s -p "ID number (9 digits): " idn; echo
    idn="${idn//[^0-9]/}"
    [ -n "$user" ] && [ -n "$pass" ] || { say "username and password are needed"; exit 1; }
    [ "${#idn}" -eq 9 ] || { say "the ID number must have 9 digits (with the check digit)"; exit 1; }
    printf 'BGU_USER=%s\nBGU_PASS=%s\nBGU_ID=%s\n' "$user" "$pass" "$idn" | write_secret "$SECRETS/bgu.env"
    unset pass idn
    say "saved: $SECRETS/bgu.env"
    say "getting a fresh Moodle token (login/token.php)..."
    cd "$P"
    "$PY" -X utf8 - <<'EOF'
import json, os, time
import moodle_pull as mp, study_secrets
c = study_secrets.load("bgu.env")
mp.secrets.append(c["BGU_PASS"])
got = mp.post_json(mp.BASE + "/login/token.php", {"username": c["BGU_USER"].strip(), "password": c["BGU_PASS"], "service": mp.SERVICE})
if "token" not in got:
    raise SystemExit(f"Moodle login failed: {got.get('errorcode')} - {got.get('error')}")
info = mp.ws(got["token"], "core_webservice_get_site_info")
path = study_secrets.path("moodle.json")
tmp = path.with_suffix(".new")
tmp.write_text(json.dumps({"token": got["token"], "privatetoken": got.get("privatetoken"), "userid": info["userid"],
                           "saved": time.strftime("%Y-%m-%d %H:%M")}), encoding="utf-8")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print(f"Moodle token saved for {info.get('fullname')} -> {path}")
EOF
    say "test the portal now:      $0 test portal"
    say "test the recordings now:  $0 test recordings"
}

cmd_mail() {
    cat <<'EOF'
The BGU mailbox is Google Workspace (its MX is Google), read here over IMAP with a Google "app password".
Create one, once:
  1. Open https://myaccount.google.com and sign in with the BGU address (...@post.bgu.ac.il).
  2. Security -> "2-Step Verification": turn it on (an app password needs it).
  3. Open https://myaccount.google.com/apppasswords , name it "study-server", press Create.
     Google shows 16 letters: that is the app password (it is not your BGU password).
  4. If Gmail's settings show "Forwarding and POP/IMAP", make sure IMAP is enabled.
If step 2 or 3 is missing, the university blocked app passwords for students; then mail stays off.
The server only reads: the mailbox is opened read-only, nothing is marked, moved or deleted.
EOF
    local def=""
    [ -s "$SECRETS/bgu.env" ] && def="$(sed -n 's/^BGU_USER=//p' "$SECRETS/bgu.env")@post.bgu.ac.il"
    read -r -p "Mail address [${def}]: " addr
    addr="${addr:-$def}"
    [[ "$addr" == *@* ]] || { say "a full address is needed"; exit 1; }
    read -r -s -p "App password (16 letters, hidden): " app; echo
    app="${app// /}"
    [ "${#app}" -ge 12 ] || { say "that does not look like an app password"; exit 1; }
    printf 'MAIL_USER=%s\nMAIL_PASS=%s\nMAIL_HOST=imap.gmail.com\n' "$addr" "$app" | write_secret "$SECRETS/mail.env"
    unset app
    say "saved: $SECRETS/mail.env. Testing (counts only):"
    cd "$P" && "$PY" -X utf8 mail_pull.py --test || say "the test failed: check the app password, then run: $0 mail"
}

cmd_gemini() {
    say "Gemini API key for generating daily morning podcasts on the server (from https://aistudio.google.com/apikey)."
    read -r -s -p "Paste GEMINI_API_KEY (hidden): " gkey; echo
    gkey="$(printf '%s' "$gkey" | tr -d '[:space:]')"
    [ "${#gkey}" -ge 20 ] || { say "that does not look like a Gemini API key"; exit 1; }
    printf 'GEMINI_API_KEY=%s\n' "$gkey" | write_secret "$SECRETS/gemini.env"
    unset gkey
    say "saved: $SECRETS/gemini.env (mode 600)."
}

cmd_calendar() {
    cat <<'EOF'
Google Calendar's private feed, read-only:
  1. Open https://calendar.google.com on a computer -> the gear -> Settings.
  2. On the left, under "Settings for my calendars", pick your calendar -> "Integrate calendar".
  3. Scroll to the bottom: copy "Secret address in iCal format" (the eye icon shows it; it has "private-" in it
     and ends with basic.ics). Not "Public address in iCal format" above it: that one answers 404.
  Several calendars: paste each address on its own line (several lines in one paste work too), then press
  Enter on an empty line. This replaces the addresses saved before.
EOF
    local urls=() url n=0
    while :; do
        n=$((n + 1))
        read -r -s -p "Secret address $n (hidden; empty line = done): " url; echo
        url="$(printf '%s' "$url" | tr -d '[:space:]')"
        [ -n "$url" ] || break
        local one
        for one in $(printf '%s' "$url" | sed 's#https://#\nhttps://#g'); do  # two addresses glued on one line
            [[ "$one" == https://calendar.google.com/* && "$one" == *.ics ]] || { say "  address $n is not a Google Calendar iCal address; skipped"; continue; }
            [[ "$one" == */private-* ]] || { say "  address $n is a public address (/public/); copy the SECRET one; skipped"; continue; }
            urls+=("$one"); say "  address $n taken"
        done
    done
    unset url one
    [ "${#urls[@]}" -gt 0 ] || { say "no address saved; the old one (if any) is kept"; exit 1; }
    printf 'CALENDAR_ICS_URL=%s\n' "${urls[*]}" | write_secret "$SECRETS/calendar.env"
    say "saved ${#urls[@]} calendar(s)."
    unset urls
    say "saved: $SECRETS/calendar.env. Testing:"
    cd "$P" && "$PY" -X utf8 calendar_pull.py --test || true
    cmd_feed_url
}

cmd_feed_url() {
    cd "$P" && "$PY" -X utf8 calendar_feed.py --out "$STUDY/site/cal" >/dev/null
    local host="${STUDY_HOST:-study.example.com}"
    [ -s "$SECRETS/site_host" ] && host="$(cat "$SECRETS/site_host")"
    say
    say "The study calendar (timetable, Moodle deadlines, exams) for Google Calendar -> Other calendars -> + -> From URL:"
    say "  https://$host/cal/$(cat "$SECRETS/cal_token").ics"
    say "(it works once the tunnel is up; keep the address private, it needs no password)"
}

cmd_site_password() {
    read -r -p "Site username [liran]: " user
    user="${user:-liran}"
    [[ "$user" =~ ^[A-Za-z0-9._-]+$ ]] || { say "letters, digits, . _ - only"; exit 1; }
    read -r -s -p "Site password (hidden): " pw; echo
    read -r -s -p "Again: " pw2; echo
    [ "$pw" = "$pw2" ] || { say "the two passwords differ"; exit 1; }
    [ "${#pw}" -ge 10 ] || { say "use at least 10 characters"; exit 1; }
    local hash
    hash="$(printf '%s\n' "$pw" | docker run --rm -i caddy:2.10.0-alpine caddy hash-password)"
    unset pw pw2
    [[ "$hash" == \$2* ]] || { say "caddy hash-password failed"; exit 1; }
    printf 'basic_auth {\n\t%s %s\n}\n' "$user" "$hash" | write_secret "$SECRETS/web/site_auth"
    ln -sfn web/site_auth "$SECRETS/site_auth"
    say "saved: $SECRETS/web/site_auth (a bcrypt hash; the password itself is not stored)"
    "${COMPOSE[@]}" up -d --force-recreate study-web >/dev/null
    sleep 2
    say "check: without a password the site answers $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8097/) (401 expected)"
}

cmd_tunnel() {
    [ -s "$SECRETS/web/site_auth" ] || { say "set the site password first: $0 site-password (the tunnel makes the site public)"; exit 1; }
    cat <<'EOF'
A Cloudflare Tunnel of its own for the study site (the finance tunnel is not touched):
  1. https://one.dash.cloudflare.com -> Networks -> Tunnels -> "Create a tunnel" -> Cloudflared.
  2. Name it "study". Cloudflare shows an install command with a long token (starts with eyJ).
     Copy the token (or the whole command; the token is taken out of it).
EOF
    read -r -s -p "Paste the tunnel token (hidden): " tok; echo
    case "$tok" in *'*'*) say "what was pasted is the masked text from the page (it has * in it). Use the page's copy button, in a normal browser"; exit 1 ;; esac
    tok="$(printf '%s' "$tok" | grep -oE 'eyJ[A-Za-z0-9_=-]+' | head -1 || true)"
    [ "${#tok}" -ge 100 ] || { say "no full token found in what was pasted (a real one is about 180 characters)"; exit 1; }
    printf 'TUNNEL_TOKEN=%s\n' "$tok" | write_secret "$SECRETS/cloudflared.env"
    unset tok
    read -r -p "Hostname you will give it [${STUDY_HOST:-study.example.com}]: " host
    host="${host:-${STUDY_HOST:-study.example.com}}"
    printf '%s\n' "$host" | write_secret "$SECRETS/site_host"
    "${COMPOSE[@]}" --profile tunnel up -d --force-recreate study-tunnel
    sleep 5
    docker logs --tail 5 study-tunnel 2>&1 | grep -iE "registered|error|failed" | sed 's/token=[^ ]*/token=<hidden>/' || true
    cat <<EOF

  3. Back in Cloudflare, in the tunnel's "Public Hostname" tab: Add a public hostname
       Subdomain: ${host%%.*}   Domain: ${host#*.}   Service: HTTP   URL: study-web:80
  4. Open https://$host : it asks for the site password.
EOF
}

cmd_cron() {
    if crontab -l 2>/dev/null | grep -qF "$P/server/nightly.sh"; then
        say "already in the crontab:"; crontab -l | grep -F "$P/server/nightly.sh"; return
    fi
    chmod +x "$P/server/nightly.sh"
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    say "added: $CRON_LINE   (01:30 UTC = 03:30 Israel time, before the finance runs at 04:00 UTC)"
}

cmd_web() {
    mkdir -p "$SRV" "$STUDY/site"
    cp "$P/server/docker-compose.yml" "$P/server/Caddyfile" "$SRV/"
    "${COMPOSE[@]}" up -d --force-recreate study-web
    sleep 2
    say "study-web on 127.0.0.1:8097 answers $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8097/)"
}

cmd_test() {
    cd "$P"
    case "${1:-}" in
        portal) "$PY" -X utf8 portal_pull.py --test ;;
        mail) "$PY" -X utf8 mail_pull.py --test ;;
        calendar) "$PY" -X utf8 calendar_pull.py --test ;;
        feed) "$PY" -X utf8 calendar_feed.py --test ;;
        recordings) "$PY" -X utf8 recordings_nightly.py --test ;;
        transcribe) "$PY" -X utf8 transcribe_cpu.py --once --current-only --max-files 1 ;;
        *) say "test what? portal | mail | calendar | feed | recordings | transcribe"; exit 1 ;;
    esac
}

cmd_status() {
    say "secrets in $SECRETS (only whether they exist):"
    for f in moodle.json bgu.env mail.env gemini.env calendar.env cal_token cloudflared.env web/site_auth; do
        if [ -s "$SECRETS/$f" ]; then say "  [x] $f"; else say "  [ ] $f"; fi
    done
    say "cron:"; crontab -l 2>/dev/null | grep -F "$P/server/nightly.sh" | sed 's/^/  /' || say "  not set: $0 cron"
    say "containers:"; docker ps -a --filter name=study- --format '  {{.Names}}  {{.Status}}  {{.Ports}}'
    say "site: http://127.0.0.1:8097/ -> $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8097/ || echo down)"
    say "last run: $(cat "$STUDY/logs/last_run.txt" 2>/dev/null || echo never)"
    say "logs: $STUDY/logs/ (newest: $(ls -1t "$STUDY"/logs/20*.log 2>/dev/null | head -1))"
    say "disk: $(du -sh "$STUDY/cs" 2>/dev/null | cut -f1) tree, $(df -h "$HOME" | awk 'NR==2{print $4}') free"
}

case "${1:-}" in
    bgu) cmd_bgu ;;
    mail) cmd_mail ;;
    gemini) cmd_gemini ;;
    calendar) cmd_calendar ;;
    feed-url) cmd_feed_url ;;
    site-password) cmd_site_password ;;
    tunnel) cmd_tunnel ;;
    cron) cmd_cron ;;
    web) cmd_web ;;
    run) "$P/server/nightly.sh"; tail -3 "$STUDY/logs/$(TZ=Asia/Jerusalem date +%F).log" ;;
    test) shift; cmd_test "$@" ;;
    status) cmd_status ;;
    *) sed -n '2,15p' "$0"; exit 1 ;;
esac
