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
cd retro-replay/backend
pip install -r requirements.txt

# 2. cache some seasons (see "Data sources" — synthetic works offline)
python -m app.pipeline.build --years 2000-2019 --source synthetic

# 3. run it
uvicorn app.main:app --reload            # http://localhost:8000

# 4. frontend (separate terminal)
cd retro-replay/frontend
npm install
npm run dev                              # http://localhost:5173
```

The Vite dev server proxies `/api` and `/ws` to the backend. For a single
process in production, run `npm run build` and start uvicorn — FastAPI serves
`frontend/dist` when it exists.

Tests: `cd backend && python -m pytest` (≈2 min; it drafts and replays a full
season).

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
4. **Snake draft** in a live room — picks land instantly for everyone.
5. **Season.** Every night at 8:00 PM Central the replay advances one day.
   Points accrue Monday–Sunday; the higher weekly total wins the matchup.
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
| No-hitter / perfect game | derived | derived |
| Pickoff | in the event stream, not a `cwdaily` column | Statcast only, 2008+ |
| **Hold** | **not available without deriving game state from event files — not implemented in v1** | **missing entirely from per-game logs** |

Holds are the real gap. The schema stores an explicit `hld` column so a
deriver can be added without a migration, but with either live source the
HLD category scores 0 until one is written. The synthetic generator supplies
holds, so the scoring path itself is exercised. **This is worth settling
before a real league runs**: either write the event-file hold deriver, or drop
HLD from the scoring config.

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
   on waivers before they clear. No same-day pickups, no drop-and-re-add to
   dodge a rival's bid, no visibility into other bids.

What this does *not* do is erase a manager's memory of the season. The rules
page says so plainly, and `freeze_adds_final_weeks` is there if the league
decides late-season sniping matters.

**IL rule.** A player's status is read on the Monday the week begins — the
lineup-lock moment. Already on the IL: unstartable, stashable in an IL slot.
Hurt mid-week: stays active and simply stops producing, as in any real weekly
league. Judging by the whole week would mean the lineup screen knew about an
injury that hadn't happened yet.

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

Still worth a decision before a real season:

* the **hold** sourcing gap described above;
* whether the roster totals should be 42 or 45;
* whether to enable `freeze_adds_final_weeks`.

### Not in v1

* **Trades** — out of scope per the spec.
* **Manual bracket editing.** The bracket is generated from regular-season
  seeding and advances automatically; there is no override beyond editing
  `playoff_teams` before the playoffs start.
* **A hold deriver** for Retrosheet event files (see above).
* **Sim pace controls.** Fixed at one day per night, per the spec; the
  commissioner's `advance` endpoint exists for catching up, not for changing
  the cadence.

---

## Layout

```
backend/
  app/
    scoring.py / scoring.json      point values + derived events (cycle, QS, NH, PG)
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
  tests/                           scoring, calendar, schedule, rosters,
                                   lineups/IL, waivers, replay, bot integrity, API
frontend/
  src/pages/                       lobby, draft, team, waivers, standings,
                                   matchups, player, rules, commissioner
  src/components/SpeedRound.tsx    the mini-game
```
