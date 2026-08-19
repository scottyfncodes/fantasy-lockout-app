#!/bin/sh
# Cache the season data before serving, then hand off to uvicorn.
#
# A fresh disk has no seasons in it, and a league cannot be created without one
# — the year is drawn at random from the cached eligible years. Building on
# first boot means a new deploy comes up playable instead of coming up broken.
# It is skipped once the data is there, so restarts and redeploys are fast.
set -e

: "${RETRO_REPLAY_DB:=/data/replay.sqlite3}"
# Cached before the port opens, so this is first-boot time you wait through
# once. Real seasons cost about 90 seconds each (download plus Chadwick);
# synthetic ones a couple of seconds. Widen or narrow to taste — roughly
# 11 MB of disk per season either way.
: "${RETRO_SEASONS:=2016-2019}"
: "${PORT:=8000}"
# The IL feed lives on a different site from the box scores and refuses some
# hosts. Left strict, one 403 marks every season unplayable and the app comes
# up unable to create a league at all — a confusing dead end for a first
# deploy. Lenient costs nothing when the feed does work: the season keeps its
# real injuries and the flag goes unused. Set to 0 to demand the feed.
: "${RETRO_ALLOW_MISSING_IL:=1}"
# Where the image put the IL export. Only consulted when the live feed fails.
: "${RETRO_IL_FILE:=/opt/injuries.csv}"
[ -f "$RETRO_IL_FILE" ] || RETRO_IL_FILE=""
[ -n "$RETRO_IL_FILE" ] && il_file="--il-file $RETRO_IL_FILE"
export RETRO_REPLAY_DB RETRO_IL_FILE RETRO_SEASONS

# What still needs building. A year qualifies if it is not cached at all, or
# if it is cached with no injuries while the export covers it — that second
# case is how a season ingested before the export existed heals itself instead
# of staying injury-free forever.
missing=$(python - <<'PLAN'
import os
from app import db
from app.pipeline.build import parse_years
from app.pipeline import injury_file

want = parse_years(os.environ.get("RETRO_SEASONS", ""))
covered = set()
path = os.environ.get("RETRO_IL_FILE", "")
if path:
    try:
        covered = injury_file.covered_years(injury_file.read_rows(path))
    except injury_file.InjuryFileUnusable:
        covered = set()

db.init_db()
with db.closing_conn() as conn:
    cached = {r["year"]: r["n"] for r in conn.execute(
        """SELECT s.year, (SELECT COUNT(*) FROM il_stints i WHERE i.season = s.year) n
             FROM seasons s""")}
todo = [y for y in want
        if y not in cached or (cached[y] == 0 and y in covered)]
print(",".join(str(y) for y in todo))
PLAN
)

: "${RETRO_WARMUP_MARKER:=/data/.warmup}"
export RETRO_WARMUP_MARKER
rm -f "$RETRO_WARMUP_MARKER"

if [ -n "$missing" ]; then
    echo "seasons to cache: ${missing} (${RETRO_SOURCE:-synthetic})"
    [ "$RETRO_ALLOW_MISSING_IL" = "0" ] || lenient="--allow-missing-il"
    # Caching seventeen seasons takes about twenty-five minutes, and blocking
    # the port for it means the only thing anyone sees is 502 with no way to
    # tell a slow first boot from a broken one. So the server comes up first
    # and the cache fills behind it: the app can then say how far along it is.
    # The marker is how it tells "still working" from "stopped early".
    printf '%s' "$missing" > "$RETRO_WARMUP_MARKER"
    # Deliberately not fatal. A failed ingest used to kill the container under
    # set -e, so the host restarted it, so it failed again — a crash loop whose
    # only symptom is 502 Bad Gateway, with the actual reason buried in a log
    # nobody thinks to open. Booting anyway means the app can be reached and
    # say what went wrong, which is worth more than refusing to start.
    (
        if ! python -m app.pipeline.build --years "$missing" \
                --source "${RETRO_SOURCE:-synthetic}" $lenient $il_file; then
            echo "WARNING: could not cache ${missing} from ${RETRO_SOURCE:-synthetic}."
            echo "WARNING: /api/health reports what is cached."
            echo "WARNING: set RETRO_SOURCE=synthetic to come up without the network."
        fi
        python - <<'PRUNE'
# Pruned against the *league config* range, never RETRO_SEASONS. Those two
# mean different things: RETRO_SEASONS is only which years to warm up front,
# while eligible_year_min/max is which years a league may draw. Pruning to the
# warm-up list meant every year cached on demand — that is, every year anyone
# actually played — was struck off the draw on the next restart, until only
# the warmed pair was left.
from app import db
from app.config import LeagueConfig
from app.pipeline.build import prune_to
with db.closing_conn() as conn:
    for year, reason in prune_to(conn, LeagueConfig.load().eligible_years()):
        print(f"[{year}] dropped from the draw — {reason}")
PRUNE
        rm -f "$RETRO_WARMUP_MARKER"
        echo "season cache complete"
    ) &
else
    echo "all requested seasons already cached in ${RETRO_REPLAY_DB}"
    python - <<'PRUNE'
# Pruned against the *league config* range, never RETRO_SEASONS. Those two
# mean different things: RETRO_SEASONS is only which years to warm up front,
# while eligible_year_min/max is which years a league may draw. Pruning to the
# warm-up list meant every year cached on demand — that is, every year anyone
# actually played — was struck off the draw on the next restart, until only
# the warmed pair was left.
from app import db
from app.config import LeagueConfig
from app.pipeline.build import prune_to
with db.closing_conn() as conn:
    for year, reason in prune_to(conn, LeagueConfig.load().eligible_years()):
        print(f"[{year}] dropped from the draw — {reason}")
PRUNE
fi

# One worker on purpose: the draft room and mini-game keep their state in this
# process, and the nightly scheduler must fire once, not once per worker.
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers 1
