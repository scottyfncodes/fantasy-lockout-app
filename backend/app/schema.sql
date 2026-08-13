-- Retro Season Replay schema.
--
-- Two halves:
--   1. Cached historical data for a replayed season (players, games, box-score
--      lines, IL transactions).  Written once by the data pipeline, read-only
--      afterwards.
--   2. Live league state (teams, draft, lineups, waivers, matchups).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ==========================================================================
-- 1. historical season data
-- ==========================================================================

CREATE TABLE IF NOT EXISTS seasons (
    year            INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,             -- 'retrosheet' | 'pybaseball' | 'synthetic'
    opening_day     TEXT NOT NULL,             -- ISO date of first real game
    final_game_day  TEXT NOT NULL,
    all_star_monday TEXT NOT NULL,             -- Monday of the skipped break week
    player_count    INTEGER NOT NULL DEFAULT 0,
    game_count      INTEGER NOT NULL DEFAULT 0,
    coverage_json   TEXT NOT NULL DEFAULT '{}',-- which stats this source supplied
    eligible        INTEGER NOT NULL DEFAULT 1,-- 0 => excluded from the random draw
    ineligible_reason TEXT,
    ingested_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS players (
    player_id   TEXT NOT NULL,
    season      INTEGER NOT NULL REFERENCES seasons(year) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    mlb_team    TEXT NOT NULL,
    positions   TEXT NOT NULL,   -- comma separated, real defensive positions that season
    is_pitcher  INTEGER NOT NULL DEFAULT 0,
    bats        TEXT,
    throws      TEXT,
    PRIMARY KEY (player_id, season)
);
CREATE INDEX IF NOT EXISTS idx_players_season ON players(season);

CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    season  INTEGER NOT NULL REFERENCES seasons(year) ON DELETE CASCADE,
    date    TEXT NOT NULL,
    home    TEXT NOT NULL,
    away    TEXT NOT NULL,
    home_runs INTEGER NOT NULL DEFAULT 0,
    away_runs INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_games_season_date ON games(season, date);

CREATE TABLE IF NOT EXISTS batting_lines (
    game_id   TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    player_id TEXT NOT NULL,
    season    INTEGER NOT NULL,
    date      TEXT NOT NULL,
    team      TEXT NOT NULL,
    pa   INTEGER NOT NULL DEFAULT 0,
    ab   INTEGER NOT NULL DEFAULT 0,
    r    INTEGER NOT NULL DEFAULT 0,
    h    INTEGER NOT NULL DEFAULT 0,
    b1   INTEGER NOT NULL DEFAULT 0,
    b2   INTEGER NOT NULL DEFAULT 0,
    b3   INTEGER NOT NULL DEFAULT 0,
    hr   INTEGER NOT NULL DEFAULT 0,
    rbi  INTEGER NOT NULL DEFAULT 0,
    bb   INTEGER NOT NULL DEFAULT 0,   -- includes IBB, per box-score convention
    ibb  INTEGER NOT NULL DEFAULT 0,
    hbp  INTEGER NOT NULL DEFAULT 0,
    so   INTEGER NOT NULL DEFAULT 0,
    sb   INTEGER NOT NULL DEFAULT 0,
    cs   INTEGER NOT NULL DEFAULT 0,
    slam INTEGER NOT NULL DEFAULT 0,   -- grand slams; 0 when source can't supply
    pos  TEXT,                          -- position played in this game
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_bat_player ON batting_lines(season, player_id, date);
CREATE INDEX IF NOT EXISTS idx_bat_date ON batting_lines(season, date);

CREATE TABLE IF NOT EXISTS pitching_lines (
    game_id   TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    player_id TEXT NOT NULL,
    season    INTEGER NOT NULL,
    date      TEXT NOT NULL,
    team      TEXT NOT NULL,
    gs   INTEGER NOT NULL DEFAULT 0,
    outs INTEGER NOT NULL DEFAULT 0,
    bf   INTEGER NOT NULL DEFAULT 0,
    h    INTEGER NOT NULL DEFAULT 0,
    r    INTEGER NOT NULL DEFAULT 0,
    er   INTEGER NOT NULL DEFAULT 0,
    bb   INTEGER NOT NULL DEFAULT 0,
    ibb  INTEGER NOT NULL DEFAULT 0,
    hbp  INTEGER NOT NULL DEFAULT 0,
    so   INTEGER NOT NULL DEFAULT 0,
    hr   INTEGER NOT NULL DEFAULT 0,
    w    INTEGER NOT NULL DEFAULT 0,
    l    INTEGER NOT NULL DEFAULT 0,
    sv   INTEGER NOT NULL DEFAULT 0,
    cg   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_pit_player ON pitching_lines(season, player_id, date);
CREATE INDEX IF NOT EXISTS idx_pit_date ON pitching_lines(season, date);

CREATE TABLE IF NOT EXISTS il_stints (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    season     INTEGER NOT NULL,
    player_id  TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date   TEXT,                    -- NULL => out for the rest of the season
    kind       TEXT NOT NULL DEFAULT 'IL',
    note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_il_player ON il_stints(season, player_id);

-- ==========================================================================
-- 2. live league state
-- ==========================================================================

CREATE TABLE IF NOT EXISTS leagues (
    id            TEXT PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,   -- short join code in the share link
    name          TEXT NOT NULL,
    commissioner_token TEXT NOT NULL,
    config_json   TEXT NOT NULL,
    scoring_json  TEXT NOT NULL,
    season_year   INTEGER,                -- NULL until the random draw
    phase         TEXT NOT NULL,          -- lobby|year_reveal|minigame|draft|season|playoffs|complete
    current_week  INTEGER NOT NULL DEFAULT 0,
    last_simulated_date TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
    id         TEXT PRIMARY KEY,
    league_id  TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    manager_token TEXT,
    is_bot     INTEGER NOT NULL DEFAULT 0,
    seat       INTEGER NOT NULL,          -- join order
    locked_in  INTEGER NOT NULL DEFAULT 0,
    draft_slot INTEGER,                   -- 1-based, set by the mini-game
    faab_remaining INTEGER NOT NULL DEFAULT 100,
    wins   INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    ties   INTEGER NOT NULL DEFAULT 0,
    points_for REAL NOT NULL DEFAULT 0,
    eliminated INTEGER NOT NULL DEFAULT 0,
    UNIQUE (league_id, seat)
);

CREATE TABLE IF NOT EXISTS minigame_scores (
    league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    team_id   TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    score     INTEGER NOT NULL DEFAULT 0,
    finished  INTEGER NOT NULL DEFAULT 0,
    tiebreak  REAL NOT NULL DEFAULT 0,    -- deterministic jitter, breaks equal scores
    PRIMARY KEY (league_id, team_id)
);

CREATE TABLE IF NOT EXISTS draft_picks (
    league_id  TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    overall    INTEGER NOT NULL,
    round      INTEGER NOT NULL,
    pick_in_round INTEGER NOT NULL,
    team_id    TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    player_id  TEXT,
    auto       INTEGER NOT NULL DEFAULT 0,
    picked_at  TEXT,
    PRIMARY KEY (league_id, overall)
);

CREATE TABLE IF NOT EXISTS rosters (
    league_id    TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    team_id      TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    player_id    TEXT NOT NULL,
    acquired_week INTEGER NOT NULL DEFAULT 0,
    acquired_via TEXT NOT NULL DEFAULT 'draft',
    PRIMARY KEY (league_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_rosters_team ON rosters(team_id);

-- One row per filled slot per week.  slot is a position code, 'BENCH' or 'IL'.
CREATE TABLE IF NOT EXISTS lineups (
    league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    team_id   TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    week      INTEGER NOT NULL,
    player_id TEXT NOT NULL,
    slot      TEXT NOT NULL,
    PRIMARY KEY (league_id, team_id, week, player_id)
);
CREATE INDEX IF NOT EXISTS idx_lineups_week ON lineups(league_id, week);

CREATE TABLE IF NOT EXISTS lineup_locks (
    league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    team_id   TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    week      INTEGER NOT NULL,
    locked_at TEXT NOT NULL,
    PRIMARY KEY (league_id, team_id, week)
);

CREATE TABLE IF NOT EXISTS matchups (
    league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    week      INTEGER NOT NULL,
    slot      INTEGER NOT NULL,          -- index within the week
    home_team_id TEXT REFERENCES teams(id) ON DELETE CASCADE,
    away_team_id TEXT REFERENCES teams(id) ON DELETE CASCADE,
    home_points REAL NOT NULL DEFAULT 0,
    away_points REAL NOT NULL DEFAULT 0,
    winner_team_id TEXT,
    complete  INTEGER NOT NULL DEFAULT 0,
    stage     TEXT NOT NULL DEFAULT 'regular', -- regular|quarterfinal|semifinal|final
    series_id TEXT,                      -- groups the two finals weeks
    PRIMARY KEY (league_id, week, slot)
);

-- Per-player, per-week fantasy points as actually credited to a team.
CREATE TABLE IF NOT EXISTS scoring_lines (
    league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    week      INTEGER NOT NULL,
    date      TEXT NOT NULL,
    team_id   TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    player_id TEXT NOT NULL,
    slot      TEXT NOT NULL,
    points    REAL NOT NULL,
    breakdown_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (league_id, week, date, player_id)
);
CREATE INDEX IF NOT EXISTS idx_scoring_team_week ON scoring_lines(league_id, team_id, week);

CREATE TABLE IF NOT EXISTS waiver_bids (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    week      INTEGER NOT NULL,          -- week the bid is processed FOR
    team_id   TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    add_player_id  TEXT NOT NULL,
    drop_player_id TEXT,
    amount    INTEGER NOT NULL,
    priority  INTEGER NOT NULL DEFAULT 1,-- manager's own ordering of their bids
    status    TEXT NOT NULL DEFAULT 'pending', -- pending|won|lost|invalid|cancelled
    reason    TEXT,
    created_at TEXT NOT NULL,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bids_league_week ON waiver_bids(league_id, week);

-- Dropped players sit here until they clear to free agency.
CREATE TABLE IF NOT EXISTS waiver_wire (
    league_id  TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    player_id  TEXT NOT NULL,
    dropped_by TEXT,
    dropped_on TEXT NOT NULL,
    clears_on  TEXT NOT NULL,
    PRIMARY KEY (league_id, player_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    week      INTEGER NOT NULL,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,             -- draft|add|drop|faab_win|il_place|il_activate
    team_id   TEXT,
    player_id TEXT,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tx_league ON transactions(league_id, id);
