"""Deriving holds and pickoffs from Retrosheet event data.

`cwdaily` gives every counting stat this league scores except two: **holds**
and **pickoffs**.  Neither is a box-score column anywhere — a hold is a
statement about the *game state* when a reliever entered and left, and a
pickoff is a specific event code in the play-by-play.  Both are recoverable
from Chadwick's `cwevent` output, which is one row per play with the score,
inning, pitcher and event code attached.

The hold rule implemented here is the official one:

    A relief pitcher earns a hold when he
      1. enters the game in a save situation,
      2. records at least one out,
      3. leaves the game without his team having surrendered the lead, and
      4. is not credited with the win or the save.

A save situation, at the moment of entry, means the pitcher's team leads and
either the lead is three runs or fewer, or the tying run is on base, at bat, or
on deck.  Without base-state at entry we approximate "tying run on deck" with
the standard lead-of-three-or-fewer test plus the potential-tying-run rule that
`cwevent` does give us via the runners-on columns.

Multiple relievers can earn a hold in the same game; the winning and saving
pitchers cannot.

This module is deliberately independent of the network: it transforms rows,
so it is unit-testable with hand-written events, which is how it is tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# cwevent EVENT_CD values. 8 is a pickoff; 4/6 are stolen base / caught
# stealing, which share the "runner event" family but are not pickoffs.
EVENT_PICKOFF = 8

MAX_SAVE_SITUATION_LEAD = 3


def _int(row: Any, key: str, default: int = 0) -> int:
    value = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
    if value in (None, "", "NA"):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _str(row: Any, key: str) -> str:
    value = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
    return "" if value is None else str(value)


@dataclass
class Appearance:
    """One pitcher's stint in one game, reconstructed from the event stream."""

    game_id: str
    pitcher_id: str
    team: str
    entered_index: int
    lead_at_entry: int = 0
    tying_run_close: bool = False
    outs_recorded: int = 0
    lead_lost: bool = False
    final_lead: int = 0
    is_starter: bool = False
    pickoffs: int = 0
    last_index: int = 0
    _lead_seen: list[int] = field(default_factory=list)

    @property
    def entered_in_save_situation(self) -> bool:
        if self.lead_at_entry <= 0:
            return False
        return self.lead_at_entry <= MAX_SAVE_SITUATION_LEAD or self.tying_run_close


def scan_game(events: Sequence[Any]) -> dict[str, Appearance]:
    """Walk one game's events in order and rebuild every pitching appearance.

    Expects cwevent-style rows with at least: GAME_ID, PIT_ID, INN_CT,
    BAT_HOME_ID, AWAY_SCORE_CT, HOME_SCORE_CT, EVENT_OUTS_CT, EVENT_CD, and the
    three base-runner columns (BASE1_RUN_ID etc.).
    """
    appearances: dict[str, Appearance] = {}
    order: list[str] = []

    for index, event in enumerate(events):
        pitcher = _str(event, "PIT_ID")
        if not pitcher:
            continue
        # The pitcher's team is the one *not* batting.
        batting_home = _int(event, "BAT_HOME_ID")
        pitcher_is_home = batting_home == 0
        away, home = _int(event, "AWAY_SCORE_CT"), _int(event, "HOME_SCORE_CT")
        own, opp = (home, away) if pitcher_is_home else (away, home)
        lead = own - opp

        app = appearances.get(pitcher)
        if app is None:
            runners = sum(
                1 for key in ("BASE1_RUN_ID", "BASE2_RUN_ID", "BASE3_RUN_ID")
                if _str(event, key)
            )
            app = Appearance(
                game_id=_str(event, "GAME_ID"),
                pitcher_id=pitcher,
                team="home" if pitcher_is_home else "away",
                entered_index=index,
                lead_at_entry=lead,
                # Tying run on base, at bat or on deck: with runners on, a lead
                # no bigger than runners + 2 keeps the tying run in play.
                tying_run_close=lead > 0 and lead <= runners + 2,
                is_starter=not order or (len(order) == 0),
            )
            appearances[pitcher] = app
            order.append(pitcher)

        app.outs_recorded += _int(event, "EVENT_OUTS_CT")
        app.last_index = index
        app.final_lead = lead
        app._lead_seen.append(lead)
        if _int(event, "EVENT_CD") == EVENT_PICKOFF:
            app.pickoffs += 1

    # The first pitcher for each side is that side's starter.
    seen_sides: set[str] = set()
    for pitcher in order:
        app = appearances[pitcher]
        app.is_starter = app.team not in seen_sides
        seen_sides.add(app.team)

    for app in appearances.values():
        # The lead is "lost" if it ever reached zero or went negative while he
        # was responsible for the game.
        app.lead_lost = any(l <= 0 for l in app._lead_seen[1:]) if app._lead_seen else False
    return appearances


def derive_holds(
    events: Sequence[Any],
    winning_pitcher_id: str | None = None,
    saving_pitcher_id: str | None = None,
) -> dict[str, int]:
    """``{pitcher_id: 1}`` for every reliever who earned a hold in this game."""
    holds: dict[str, int] = {}
    for pitcher_id, app in scan_game(events).items():
        if app.is_starter:
            continue
        if pitcher_id in (winning_pitcher_id, saving_pitcher_id):
            continue
        if app.outs_recorded < 1:
            continue
        if not app.entered_in_save_situation:
            continue
        if app.lead_lost:
            continue
        if app.final_lead <= 0:
            continue
        holds[pitcher_id] = 1
    return holds


def derive_pickoffs(events: Sequence[Any]) -> dict[str, int]:
    """``{pitcher_id: count}`` of pickoffs charged to each pitcher."""
    return {
        pitcher_id: app.pickoffs
        for pitcher_id, app in scan_game(events).items()
        if app.pickoffs
    }


def group_by_game(events: Iterable[Any]) -> dict[str, list[Any]]:
    games: dict[str, list[Any]] = {}
    for event in events:
        games.setdefault(_str(event, "GAME_ID"), []).append(event)
    return games


def apply_to_pitching_lines(
    pitching_lines: list[dict[str, Any]], events: Iterable[Any]
) -> dict[str, int]:
    """Fill in `hld` and `pick` on already-parsed pitching lines.

    Returns a small summary so the ingest CLI can report how much it found —
    silence would be indistinguishable from "the deriver did nothing".
    """
    by_game = group_by_game(events)
    lines_by_key = {(l["game_id"], l["player_id"]): l for l in pitching_lines}

    filled_holds = 0
    filled_picks = 0
    for game_id, game_events in by_game.items():
        winner = next(
            (l["player_id"] for l in pitching_lines
             if l["game_id"] == game_id and l.get("w")), None,
        )
        saver = next(
            (l["player_id"] for l in pitching_lines
             if l["game_id"] == game_id and l.get("sv")), None,
        )
        for pitcher_id, value in derive_holds(game_events, winner, saver).items():
            line = lines_by_key.get((game_id, pitcher_id))
            if line is not None:
                line["hld"] = value
                filled_holds += value
        for pitcher_id, value in derive_pickoffs(game_events).items():
            line = lines_by_key.get((game_id, pitcher_id))
            if line is not None:
                line["pick"] = value
                filled_picks += value

    return {"games": len(by_game), "holds": filled_holds, "pickoffs": filled_picks}
