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
export RETRO_REPLAY_DB

cached=$(python -c "
from app import db
db.init_db()
with db.closing_conn() as conn:
    print(conn.execute('SELECT COUNT(*) FROM seasons').fetchone()[0])
")

if [ "$cached" -eq 0 ]; then
    echo "no seasons cached — building ${RETRO_SEASONS} (${RETRO_SOURCE:-synthetic})"
    [ "$RETRO_ALLOW_MISSING_IL" = "0" ] || lenient="--allow-missing-il"
    python -m app.pipeline.build --years "$RETRO_SEASONS" \
        --source "${RETRO_SOURCE:-synthetic}" $lenient
else
    echo "seasons already cached in ${RETRO_REPLAY_DB}"
fi

# One worker on purpose: the draft room and mini-game keep their state in this
# process, and the nightly scheduler must fire once, not once per worker.
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers 1
