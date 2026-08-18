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

if [ -n "$missing" ]; then
    echo "seasons to cache: ${missing} (${RETRO_SOURCE:-synthetic})"
    [ "$RETRO_ALLOW_MISSING_IL" = "0" ] || lenient="--allow-missing-il"
    # Deliberately not fatal. A failed ingest used to kill the container under
    # set -e, so the host restarted it, so it failed again — a crash loop whose
    # only symptom is 502 Bad Gateway, with the actual reason buried in a log
    # nobody thinks to open. Booting anyway means the app can be reached and
    # say what went wrong, which is worth more than refusing to start.
    if ! python -m app.pipeline.build --years "$missing" \
            --source "${RETRO_SOURCE:-synthetic}" $lenient $il_file; then
        echo "WARNING: could not cache ${missing} from ${RETRO_SOURCE:-synthetic}."
        echo "WARNING: starting anyway; /api/health reports what is cached."
        echo "WARNING: set RETRO_SOURCE=synthetic to come up without the network."
    fi
else
    echo "all requested seasons already cached in ${RETRO_REPLAY_DB}"
fi

# Years cached under an older setting stay on the disk and keep turning up in
# the random draw, so the configured range is enforced on every boot.
python - <<'PRUNE'
import os
from app import db
from app.pipeline.build import parse_years, prune_to
with db.closing_conn() as conn:
    for year, reason in prune_to(conn, parse_years(os.environ["RETRO_SEASONS"])):
        print(f"[{year}] dropped from the draw — {reason}")
PRUNE

# One worker on purpose: the draft room and mini-game keep their state in this
# process, and the nightly scheduler must fire once, not once per worker.
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers 1
