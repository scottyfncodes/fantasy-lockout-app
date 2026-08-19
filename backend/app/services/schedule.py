"""Head-to-head schedule generation.

Regular season: circle-method round robin, so every team plays every other team
before anyone gets a rematch, and home/away alternates between cycles.  Works
for any even team count (8, 10, 12, 14).

Playoffs: a bye-free single-elimination bracket for the top ``playoff_teams``
seeds.  With 8 qualifiers that is quarterfinals -> semifinals -> a two-week
final whose legs are summed.
"""

from __future__ import annotations

from typing import Any

from ..config import LeagueConfig


BYE = "__bye__"


def round_robin_rounds(team_ids: list[str]) -> list[list[tuple[str, str]]]:
    """One full cycle: everyone plays everyone once.

    An odd league gets a phantom opponent, which is the standard way to build
    this: whoever is drawn against it that round has the week off. The circle
    method then works unchanged, and the bye rotates evenly by construction —
    no team sits out twice before another has sat out once.
    """
    if len(team_ids) % 2:
        team_ids = [*team_ids, BYE]
    fixed, rotating = team_ids[0], team_ids[1:]
    rounds: list[list[tuple[str, str]]] = []
    for r in range(len(team_ids) - 1):
        order = [fixed] + rotating[-r:] + rotating[:-r] if r else [fixed] + rotating
        half = len(order) // 2
        rounds.append([
            (order[i], order[-(i + 1)]) for i in range(half)
            if BYE not in (order[i], order[-(i + 1)])
        ])
    return rounds


def regular_season(team_ids: list[str], weeks: int) -> list[list[tuple[str, str]]]:
    """``weeks`` rounds of head-to-head pairings, cycling the round robin.

    Home and away are assigned greedily against a running count rather than by
    round parity: with an odd number of rounds per cycle, parity alone leaves
    some teams hosting nearly twice as often as others.
    """
    base = round_robin_rounds(team_ids)
    home_count = {t: 0 for t in team_ids}
    schedule: list[list[tuple[str, str]]] = []
    for week in range(weeks):
        pairs = base[week % len(base)]
        ordered: list[tuple[str, str]] = []
        for a, b in pairs:
            home, away = (a, b) if home_count[a] <= home_count[b] else (b, a)
            home_count[home] += 1
            ordered.append((home, away))
        schedule.append(ordered)
    return schedule


def bracket_pairings(seeds: list[str]) -> list[tuple[str, str]]:
    """Standard 1v8 / 2v7 / 3v6 / 4v5 seeding for any power-of-two field."""
    n = len(seeds)
    return [(seeds[i], seeds[n - 1 - i]) for i in range(n // 2)]


def playoff_stage_name(round_index: int, total_rounds: int) -> str:
    remaining = total_rounds - round_index
    return {1: "final", 2: "semifinal", 3: "quarterfinal"}.get(remaining, f"round_of_{2 ** remaining}")


def playoff_week_plan(cfg: LeagueConfig) -> list[dict[str, Any]]:
    """Which fantasy weeks host which playoff round.

    Every round is one week except the final, which spans the last two weeks
    and is decided on combined points.
    """
    rounds = cfg.bracket_rounds
    first_playoff_week = cfg.regular_season_weeks + 1
    plan: list[dict[str, Any]] = []
    week = first_playoff_week
    for r in range(rounds):
        stage = playoff_stage_name(r, rounds)
        if stage == "final":
            plan.append({"stage": stage, "round": r + 1, "weeks": [week, week + 1]})
            week += 2
        else:
            plan.append({"stage": stage, "round": r + 1, "weeks": [week]})
            week += 1
    return plan


def validate(cfg: LeagueConfig, team_ids: list[str]) -> None:
    if len(team_ids) != cfg.team_count:
        raise ValueError(f"expected {cfg.team_count} teams, got {len(team_ids)}")
    plan = playoff_week_plan(cfg)
    last_week = plan[-1]["weeks"][-1]
    if last_week != cfg.total_weeks:
        raise ValueError(
            f"playoff plan ends on week {last_week} but the season is "
            f"{cfg.total_weeks} weeks long"
        )
