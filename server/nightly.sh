#!/usr/bin/env bash
# Liran's study pipeline on the home server. cron runs it at 01:30 UTC (setup.sh cron), before the finance
# jobs at 04:00-05:00 UTC. Every step runs even if an earlier one failed. Log: ~/study/logs/<date>.log
#
#   1 moodle    moodle_pull.py --saved-token, the current semester's Moodle courses
#   2 forums    forums_pull.py --saved-token, the same courses
#   3 file      file_new.py: new Moodle files into the study tree
#   4 catalog   catalog.py, then handwriting.py inject
#   5 portal    portal_pull.py            (only when secrets/bgu.env exists)
#   6 mail      mail_pull.py              (only when secrets/mail.env exists)
#   7 calendar  calendar_pull.py          (only when secrets/calendar.env exists)
#   8 recordings recordings_nightly.py + transcribe_cpu.py until 03:30 UTC (only when secrets/bgu.env exists)
#   9 site      calendar_feed.py, then site/build.py -> ~/study/site
set -u

STUDY="$HOME/study"
P="$STUDY/cs/study-briefing"
PY="$STUDY/venv/bin/python"
SECRETS="$STUDY/secrets"
export TZ=Asia/Jerusalem                     # file dates as on the PC; cron itself runs in UTC
export PYTHONIOENCODING=utf-8 PYTHONUNBUFFERED=1
export STUDY_MOODLE_TOKEN="$SECRETS/moodle.json" STUDY_SECRETS="$SECRETS" HF_HOME="$STUDY/models/hf"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
[ -s "$HOME/.nvm/nvm.sh" ] && . "$HOME/.nvm/nvm.sh"
TODAY="$(date +%F)"
LOG="$STUDY/logs/$TODAY.log"
mkdir -p "$STUDY/logs" "$STUDY/site"

exec 9>/tmp/study-briefing.lock
if ! flock -n 9; then
    echo "$(date '+%F %T') another nightly run holds /tmp/study-briefing.lock; not starting" >> "$LOG"
    exit 0
fi
exec >>"$LOG" 2>&1 </dev/null
cd "$P" || { echo "!! no project folder $P"; exit 1; }

FAILED=()
step() {
    local name="$1"; shift
    echo
    echo "== $(date '+%F %T') $name"
    local t0=$SECONDS
    "$@" || { echo "!! step failed: $name (exit $?)"; FAILED+=("$name"); }
    echo "-- $name: $((SECONDS - t0))s"
}
has() { [ -s "$SECRETS/$1" ]; }

echo "######## nightly run $(date '+%F %T %Z') ########"

# the current semester's Moodle course ids, from catalog/courses.json
mapfile -t COURSES < <("$PY" - <<'EOF'
import datetime, json
t = datetime.date.today()
y = t.year
year, sem = ((f"{y}-{(y+1)%100:02d}", "א") if t.month >= 10 else (f"{y-1}-{y%100:02d}", "א") if t.month <= 2
             else (f"{y-1}-{y%100:02d}", "ב") if t.month <= 7 else (f"{y-1}-{y%100:02d}", "קיץ"))
for c in json.load(open("catalog/courses.json", encoding="utf-8")):
    for o in c.get("offerings", []):
        if o.get("year") == year and o.get("semester") == sem and o.get("moodle_id"):
            print(o["moodle_id"])
EOF
)
COURSE_ARGS=()
for id in "${COURSES[@]}"; do COURSE_ARGS+=(--course "$id"); done
echo "current Moodle courses: ${COURSES[*]:-(none)}"

if [ ${#COURSES[@]} -gt 0 ]; then
    step "1 moodle" "$PY" -X utf8 moodle_pull.py --saved-token "${COURSE_ARGS[@]}"
    step "2 forums" "$PY" -X utf8 forums_pull.py --saved-token "${COURSE_ARGS[@]}"
else
    echo "!! no current courses found in catalog/courses.json"; FAILED+=("courses")
fi
step "3 file_new" "$PY" -X utf8 file_new.py
step "4 catalog" "$PY" -X utf8 catalog.py
step "4 handwriting inject" "$PY" -X utf8 handwriting.py inject

if has bgu.env; then step "5 portal" "$PY" -X utf8 portal_pull.py; else echo "-- 5 portal: skipped (no secrets/bgu.env; setup.sh bgu)"; fi
if has mail.env; then step "6 mail" "$PY" -X utf8 mail_pull.py; else echo "-- 6 mail: skipped (no secrets/mail.env; setup.sh mail)"; fi
if has calendar.env; then step "7 calendar" "$PY" -X utf8 calendar_pull.py; else echo "-- 7 calendar: skipped (no secrets/calendar.env; setup.sh calendar)"; fi

if command -v claude >/dev/null 2>&1; then
    step "8.5 podcast" bash -c "cd '$STUDY/cs' && claude -p --model sonnet --effort medium --dangerously-skip-permissions 'Run study-briefing/podcast_daily_prompt.md for today ($TODAY) using $PY -X utf8 for all python commands. Follow study-briefing/podcast_rules.md (~10 min, 950-1150 words on new material or 850-1150 otherwise, with rich backward connections), lint with podcast_lint.py until 0 violations, and synthesize with podcast_tts.py.'"
elif has gemini.env; then
    step "8.5 podcast" "$PY" -X utf8 podcast_generate.py --all-active --today "$TODAY"
else
    echo "-- 8.5 podcast: skipped (no claude CLI or secrets/gemini.env)"
fi

step "9 calendar feed" "$PY" -X utf8 calendar_feed.py --out "$STUDY/site/cal"
step "9 site" "$PY" -X utf8 site/build.py --root "$STUDY/cs" --out "$STUDY/site" --files-base /files/ --today "$TODAY"

# recordings last: downloading and transcribing can take hours, and the site is already fresh
if has bgu.env; then
    step "8 recordings" "$PY" -X utf8 recordings_nightly.py
    step "8 transcribe" "$PY" -X utf8 transcribe_cpu.py --once --current-only --until 03:30
else
    echo "-- 8 recordings: skipped (no secrets/bgu.env; setup.sh bgu)"
fi

find "$STUDY/logs" -maxdepth 1 -name '20*.log' -mtime +60 -delete 2>/dev/null
echo
echo "######## done $(date '+%F %T'); failed: ${FAILED[*]:-none} ########"
printf '%s\tfailed: %s\n' "$(date '+%F %T %Z')" "${FAILED[*]:-none}" > "$STUDY/logs/last_run.txt"
