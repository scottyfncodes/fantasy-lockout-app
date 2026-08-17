# Retro Season Replay

A fantasy league companion for replaying a historical MLB season day by day.
Managers draft real players from a finished season, set weekly lineups, bid
blind FAAB on free agents, and watch the actual box scores decide head-to-head
matchups.

Built to keep a league busy through a lockout: nothing here depends on games
being played today.

---

## Quick start

```bash
# 1. backend
cd backend
pip install -r requirements.txt

# 2. cache some seasons (see "Data sources" — synthetic works offline)
python -m app.pipeline.build --years 2000-2019 --source synthetic

# 3. run it
uvicorn app.main:app --reload            # http://localhost:8000

# 4. frontend (separate terminal)
cd frontend
npm install
npm run dev                              # http://localhost:5173
```

The Vite dev server proxies `/api` and `/ws` to the backend. For a single
process in production, run `npm run build` and start uvicorn — FastAPI serves
`frontend/dist` when it exists.

Tests: `cd backend && python -m pytest` (≈45s; it drafts and replays a full
season). To see it with data in it:

```bash
python -m scripts.seed_demo --teams 10 --humans 3 --weeks 7   # prints a join code
python -m scripts.ui_smoke --code <CODE> ...                  # every page in a real browser
python -m scripts.balance_report --year 2019 --teams 12       # what each slot is worth
```

`ui_smoke` walks all nine pages in headless Chromium and fails on any console
error, uncaught exception or failed request — a typecheck proves the code
compiles, not that a page renders. It needs `pip install playwright`; nothing
else does, so it is not in `requirements.txt`. Pass `--mobile` for a phone
viewport.

`balance_report` answers "is this roster shape still sensible under this
scoring config?" — see [Positional value](#positional-value).

---

## How a league runs

1. **Lobby.** The commissioner creates a league and shares a six-character
   join code. Managers enter a team name and lock in.
2. **Close the lobby** — by hand, or on a countdown so nobody has to chase
   stragglers. Empty seats fill with bots and the replay season is **drawn at
   random** from the cached, eligible years — nobody picks it.
3. **Draft order.** A ten-second Speed Round: everyone taps the same moving
   ball at the same moment, most taps picks first. (A provably-fair randomizer
   is a config switch away.)
4. **Snake draft** in a live room — picks land instantly for everyone, on a
   90-second pick clock so one dropped connection cannot stall thirteen other
   managers. The clock is server-side (a reconnect does not reset it) and
   `draft_pick_seconds: 0` turns it off.
5. **Season.** Every night at 8:00 PM Central the replay advances one day.
   Points accrue Monday–Sunday; the higher weekly total wins the matchup. The
   morning after, **Last Night** shows what your starters did, what the bench
   did instead, and which way the week's matchup moved.
6. **Playoffs.** Weeks 19–22: quarterfinals, semifinals, then a two-week final
   decided on combined points.

## Where real-time is used (and where it isn't)

WebSockets are used in exactly two places, because those are the only two
where managers act at the same instant:

| Feature | Transport | Why |
| --- | --- | --- |
| Draft-order mini-game | WebSocket | Simultaneous play; the server owns the clock, the ball and the tap counts |
| Draft room | WebSocket | Everyone must see picks land, or two managers take the same player |
| Lineups | REST | You only touch your own team |
| Waiver bids | REST | Blind by design — invisibility is the feature |
| Standings, box scores, player pages | REST | Read on load |

The server is authoritative in the mini-game: clients send *taps*, never
scores, and taps are rate-limited to 12/second.

---

## Data sources

The real pipeline is **Retrosheet** (box scores) + **ProSportsTransactions**
(injured list). Both need outbound network access, and Retrosheet also needs
the Chadwick tool suite (`cwdaily`) to turn event files into daily player
lines. Check with:

```bash
python -m app.pipeline.build --preflight
python -m app.pipeline.build --coverage     # the full stat-by-stat matrix
```

**A synthetic season generator is the offline default** (`--source synthetic`).
It produces a complete, internally consistent season from a seed — box scores
built from simulated plate appearances, pitching lines allocated against the
opposing team's actual hits and runs, rare events at roughly their real rates.
Every scoring category fires, which makes it the right fixture for tests and a
working demo when the network is unavailable.

### Stats that aren't in a box score

The scoring rules ask for several stats no traditional box score carries. This
was flagged before the schema was locked; `app/pipeline/coverage.py` is the
authority and the app surfaces it on the rules page.

| Stat | Retrosheet + Chadwick | pybaseball (BR/FanGraphs/Statcast) |
| --- | --- | --- |
| IBB | native (`B_IBB`) | native |
| Grand slam | native (`B_HR4`) | Statcast only, 2008+ |
| Cycle | derived from 1B/2B/3B/HR | derived |
| Quality start | derived (≥18 outs, ≤3 ER) | derived |

Every scoring category is supported on the Retrosheet path. Only the grand-slam
bonus is a gap on the pybaseball path, and only before 2008.

The categories that made this hard — **holds, pickoffs, no-hitters and perfect
games** — were removed from the scoring rules. Holds and pickoffs are not
columns in any source (a hold is a statement about game state, not a season
total), and both would have needed a play-by-play deriver. They are gone from
`scoring.json`, from the schema, and from the pipeline. A hitless complete game
still scores well through IP, K, CG, W and QS — it just gets no separate
bonus.

### Season eligibility

The random draw is restricted to 2000+ (ProSportsTransactions is reliably
structured from roughly then). The pipeline additionally marks a season
ineligible — excluding it from the draw rather than replaying it with holes —
when:

* the real calendar can't host 22 fantasy weeks (this is what excludes 2020's
  60-game season),
* the player pool is too thin for a 14-team league plus a free-agent pool,
* the IL scrape came back sparse or patchy.

---

## Keeping a replay honest

Every game already happened, which creates a failure mode no ordinary fantasy
league has. Three rules, all enforced in code:

1. **Bots may use full-season data while drafting, and nothing but pre-week
   data afterwards.** Drafting from a finished season is symmetric information
   — every human is doing the same. Setting lineups with it would make bots
   unbeatable. `tests/test_bot_integrity.py` makes `season_totals` raise inside
   in-season code paths, so wiring hindsight into lineups, waivers or the
   weekly rollover fails the build.
2. **In-season stat views are capped at the last simulated date** — free-agent
   pool, player pages, lineup screens, bot decisions. They are also floored at
   the start of fantasy week 1, so real games played before the replay window
   (a season opening on a Thursday plays several) never leak in.
3. **Waivers are blind FAAB, processed weekly**, with dropped players sitting
   on waivers before they clear, and rosters frozen once the playoffs start.
   No same-day pickups, no drop-and-re-add to dodge a rival's bid, no
   visibility into other bids.

The **Last Night** page is the one place hindsight is deliberately shown: it
tells you what your benched players scored on a day that has already been
replayed. That is the hindsight every fantasy manager gets and every rival can
see. It is bounded — only replayed dates, never a future one — and a player is
kept off a bench he was not yet on, using `acquired_week`. A player since
dropped simply does not appear, which is the honest limit of a roster table
that stores the present; the page says so when you look at an older day.

What this does *not* do is erase a manager's memory of the season. The rules
page says so plainly. Rosters freeze once the playoffs begin, which removes the
window where memory is worth the most; `freeze_adds_final_weeks` extends that
back into the regular season if a league wants it.

**IL rule.** A player's status is read on the Monday the week begins — the
lineup-lock moment. Already on the IL: unstartable, stashable in an IL slot.
Hurt mid-week: stays active and simply stops producing, as in any real weekly
league. Judging by the whole week would mean the lineup screen knew about an
injury that hadn't happened yet.

---

## Positional value

Changing the scoring rules changes which positions are worth drafting, so
`scripts/balance_report.py` prints what each slot is actually worth: what
starters score, what replacement level is, the gap between them (VOR — the real
worth of a slot), and how many of the startable players belong to that slot
*alone* rather than being borrowed from another position.

That last column is the one that catches problems. On a synthetic 2019 with 12
teams:

```
  SLOT    x  POOL     BEST  STARTER     REPL      VOR  PER TEAM    SOLE
  C       1    60      938      823      743       80       823  11/12
  OF      3   166      960      717      604      113      2150  24/36
  SP      2   213      678      589      550       39      1178  11/24
  RP      3   330      678      514      406      107      1541   5/36
```

Catcher is genuinely scarce (11 of 12 startable catchers play nowhere else).
The RP slot looks healthy on points, but only **5 of its 36 startable players
are relief-only** — the rest are swingmen carried by starts. Pure relievers
run best 460 / 36th-best 256 against a replacement-level outfielder at 604.

Two things to keep in mind before acting on it: a player eligible at several
slots is counted at each of them (standard for scarcity analysis, which is why
the SOLE column exists), and **these numbers come from the synthetic
generator** — they describe its assumptions about playing time, not baseball.
Run it against a real cached season before changing any rules.

`--compare '{"pitching":{"SV":20}}'` re-runs everything with overrides applied
and shows the delta. It warns when an override names a category the cached
season has no data for, so a zero delta never gets misread as "no effect".

---

## Configuration

Nothing about league shape is hardcoded. `app/league_config.json` holds the
defaults; the commissioner can override any of them at creation, and roster
shape locks once the draft begins.

`app/scoring.json` holds every point value. No other module contains a
literal point value, so retuning scoring never touches core logic.

---

## Decisions and open questions

Places where the spec was ambiguous or self-contradictory. Each was resolved
explicitly rather than silently.

**The active roster does not add up.** The itemised slots (C 1, 1B 1, 2B 1,
3B 1, SS 1, OF 3, UTIL 3, SP 2, RP 3, P 4) sum to **20**, not the stated 23 —
so the roster totals 42, not 45. The engine uses the itemised list, since it
is the only unambiguous statement of composition, and reports the gap through
`LeagueConfig.roster_discrepancy()`, which the UI shows as a banner until the
commissioner settles it. Setting `UTIL: 5` and `P: 5` gives exactly 23/45.

**Unused IL slots hold bench players.** The whole roster is drafted, but only
part of it is hurt in any given week. With a strict 17-man bench, a healthy
week would be unfillable. Bench capacity is therefore
`bench_size + (il_size − il_used)`; the roster limit still binds.

**Regular season length.** The spec says 18 weeks throughout but "24 regular
season weeks" once, in the schedule-generator bullet. 18 is used — it is the
number that appears everywhere else and the one that makes 22 total weeks
work.

**The 22-week window is anchored at the front of the season.** Week 1 is the
first Monday on or after Opening Day; the All-Star week is skipped entirely
and does not consume a week; the real season's last few weeks go unused. This
makes the fantasy-week → date mapping total and unambiguous.

**Playoff field is fixed at 8 regardless of league size** — confirmed as
designed. An 8-team league sends everyone to the bracket; a 14-team league
cuts six. `playoff_teams` is config, so a commissioner who wants a 4-team
bracket can set one.

**Bot fill shrinks toward the minimum.** If five humans show up to a 12-team
league, the league starts at 8 (the configured minimum, rounded even) rather
than seating seven bots. Change `min_teams` to change that.

**IBB double-counts by default.** Box-score BB includes intentional walks, so
an IBB scores BB (1) + IBB (1) = 2 — the literal reading of the rules. Set
`options.ibb_stacks_with_bb: false` to score it once.

**"8:00 PM CST" is implemented as 20:00 `America/Chicago`**, which follows the
local clock through daylight saving. That is what a league means by it.

**The league shrinks to fit the lobby, and the config is rewritten to match.**
If five managers show up to a 12-team league it starts at 8 — and the stored
config is updated to say 8, because the schedule generator has to build
fixtures for teams that exist. A smaller-than-8 league also shrinks its
playoff bracket to the largest bye-free field it can fill.

**Holds, pickoffs, no-hitters and perfect games were dropped** from the
scoring rules after the fact, so the schema no longer carries `hld`, `pick` or
`errors_allowed` and the pipeline no longer derives them. Re-adding any of them
means restoring the column, the generator field and the scoring entry together.

**Rosters are 40, and the discrepancy is settled.** The original rules quoted
both an itemised 20 starters and a headline 23 active / 45 total. The league
settled on **40**: the itemised 20 starters, a 15-man bench and 5 IL slots. The
`roster_discrepancy` check stays in place — a commissioner can still edit
`active_slots` into disagreement with the declared totals, and a roster that
quietly stops matching the rules page is worth catching.

**Rosters freeze when the playoffs begin.** Once the bracket starts, an add is
pure memory sniping: the eight teams still alive know exactly who caught fire
in September, and the bracket should be decided by the team a manager built.
`freeze_adds_final_weeks` is a separate, optional knob that extends the freeze
back into the last N weeks of the regular season (0 by default).

### Not in v1

* **Trades** — out of scope per the spec.
* **Manual bracket editing.** The bracket is generated from regular-season
  seeding and advances automatically; there is no override beyond editing
  `playoff_teams` before the playoffs start.
* **Sim pace controls.** Fixed at one day per night, per the spec; the
  commissioner's `advance` endpoint exists for catching up, not for changing
  the cadence.

---

## Layout

```
backend/
  app/
    scoring.py / scoring.json      point values + derived events (cycle, slam, QS)
    config.py  / league_config.json  every league knob
    season_calendar.py             fantasy weeks ↔ real dates, All-Star skip
    schema.sql, db.py              SQLite cache + live league state
    pipeline/
      coverage.py                  what each source can actually supply
      retrosheet.py                event files → daily box scores (via Chadwick)
      prosportstransactions.py     IL stints, with name matching + gap checks
      synthetic.py                 deterministic offline season generator
      build.py                     ingest CLI + eligibility verdict
    services/
      leagues, minigame, draft, rosters, lineups, il, waivers,
      replay, standings, schedule, players, timeline, bots
    api/routes.py                  REST
    api/live.py                    WebSockets: mini-game + draft room
    scheduler.py                   nightly 8pm CST job
  scripts/
    seed_demo.py                   build a league with real data in it
    ui_smoke.py                    walk every page in a real browser
    balance_report.py              what each roster slot is worth
  tests/                           scoring, calendar, schedule, rosters,
                                   lineups/IL, waivers, replay, bot integrity, API
frontend/
  src/pages/                       lobby, draft, team, last night, waivers,
                                   standings, matchups, player, rules,
                                   commissioner
  src/components/SpeedRound.tsx    the mini-game
```
