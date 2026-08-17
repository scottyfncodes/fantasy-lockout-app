#!/bin/sh
# Cache the season data before serving, then hand off to uvicorn.
#
# A fresh disk has no seasons in it, and a league cannot be created without one
# — the year is drawn at random from the cached eligible years. Building on
# first boot means a new deploy comes up playable instead of coming up broken.
# It is skipped once the data is there, so restarts and redeploys are fast.
set -e

: "${RETRO_REPLAY_DB:=/data/replay.sqlite3}"
# Ten seasons is a real random draw and costs about a minute of first boot on a
# small instance. Widen it with RETRO_SEASONS=2000-2019 if you want more years
# in the hat — budget roughly 11 MB of disk and a couple of seconds per season.
: "${RETRO_SEASONS:=2010-2019}"
: "${PORT:=8000}"
export RETRO_REPLAY_DB

cached=$(python -c "
from app import db
db.init_db()
with db.closing_conn() as conn:
    print(conn.execute('SELECT COUNT(*) FROM seasons').fetchone()[0])
")

if [ "$cached" -eq 0 ]; then
    echo "no seasons cached — building ${RETRO_SEASONS} (${RETRO_SOURCE:-synthetic})"
    python -m app.pipeline.build --years "$RETRO_SEASONS" --source "${RETRO_SOURCE:-synthetic}"
else
    echo "seasons already cached in ${RETRO_REPLAY_DB}"
fi

# One worker on purpose: the draft room and mini-game keep their state in this
# process, and the nightly scheduler must fire once, not once per worker.
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers 1
