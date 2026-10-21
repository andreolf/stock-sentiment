#!/usr/bin/env bash
# Publish the dashboard to GitHub Pages (docs/ on main).
#
# Usage: ./publish_dashboard.sh [--skip-render] [--force]
#   --skip-render  don't run dashboard.py first (the collector calls this right after rendering)
#   --force        ignore the rate limits below
#
# Steps: render dashboard (optional) -> copy dashboard/index.html (+ latest.png) into docs/ ->
#        safety check (no secret values / key-like strings) -> commit ONLY docs/ and push, only if changed.
# Rate limits (seconds, env-overridable): HTML at most every PUBLISH_MIN_INTERVAL (900 = 15 min),
# the ~1 MB PNG at most every PUBLISH_PNG_MIN_INTERVAL (21600 = 6 h) to keep the repo small.
# Fail-safe: never prints secrets, holds a lock so runs never overlap, and always exits 0 unless the
# safety check refuses to publish (exit 3). Pushing uses the box's gh credential helper.
set -u
cd "$(dirname "$0")" || exit 0
PY=.venv/bin/python
STATE=data/publish
mkdir -p "$STATE" docs
MIN_HTML=${PUBLISH_MIN_INTERVAL:-900}
MIN_PNG=${PUBLISH_PNG_MIN_INTERVAL:-21600}
SKIP_RENDER=0; FORCE=0
for a in "$@"; do
  case "$a" in --skip-render) SKIP_RENDER=1 ;; --force) FORCE=1 ;; esac
done
log() { echo "publish: $*"; }

exec 9>"$STATE/lock"
if ! flock -n 9; then log "another publish is running; skip"; exit 0; fi

now=$(date +%s)
last_html=$(cat "$STATE/last_html" 2>/dev/null || echo 0)
last_png=$(cat "$STATE/last_png" 2>/dev/null || echo 0)
if [ "$FORCE" = 0 ] && [ $((now - last_html)) -lt "$MIN_HTML" ]; then
  log "last publish $((now - last_html))s ago (< ${MIN_HTML}s); skip"; exit 0
fi

if [ "$SKIP_RENDER" = 0 ]; then
  timeout 180 "$PY" dashboard.py >/dev/null 2>&1 || log "[warn] dashboard.py failed; publishing the last rendered copy"
fi
[ -s dashboard/index.html ] || { log "[warn] no dashboard/index.html; skip"; exit 0; }

# --- safety check on what would go public (values are never printed) ---
check_file() {
  local f="$1"
  for v in ALPACA_PAPER_KEY_ID ALPACA_PAPER_SECRET_KEY REDDIT_CLIENT_ID REDDIT_CLIENT_SECRET; do
    val="${!v:-}"
    if [ -n "$val" ] && [ "${#val}" -ge 8 ] && grep -qF -- "$val" "$f"; then
      log "REFUSING: $f contains the value of \$$v"; return 1
    fi
  done
  if grep -qE 'gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY|sk-[A-Za-z0-9]{20,}' "$f"; then
    log "REFUSING: $f contains a key-like string"; return 1
  fi
  return 0
}
check_file dashboard/index.html || exit 3

cp dashboard/index.html docs/index.html
touch docs/.nojekyll
paths=(docs/index.html docs/.nojekyll)
if [ -s dashboard/latest.png ] && { [ "$FORCE" = 1 ] || [ $((now - last_png)) -ge "$MIN_PNG" ] || [ ! -f docs/latest.png ]; }; then
  cp dashboard/latest.png docs/latest.png
  png_updated=1
fi
[ -f docs/latest.png ] && paths+=(docs/latest.png)

git add -- "${paths[@]}" >/dev/null 2>&1
stamp=$(TZ=Europe/Zurich date '+%Y-%m-%d %H:%M')
if git diff --cached --quiet -- docs; then
  log "no change"
else
  if ! git commit -q -m "dashboard: update $stamp Zurich" -- docs >/dev/null 2>&1; then
    log "[warn] commit failed"; exit 0
  fi
  [ "${png_updated:-0}" = 1 ] && echo "$now" > "$STATE/last_png"
fi
echo "$now" > "$STATE/last_html"
# push whenever local main is ahead (also retries a commit whose push failed earlier)
ahead=$(git rev-list --count origin/main..main 2>/dev/null || echo 1)
if [ "$ahead" != 0 ]; then
  if timeout 90 git push -q origin main >/dev/null 2>&1; then
    log "pushed $(git rev-parse --short HEAD) ($stamp Zurich)"
  else
    log "[warn] push failed; will retry on the next publish"
  fi
fi
exit 0
