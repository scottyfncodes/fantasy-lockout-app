"""League configuration.

Every knob the commissioner can turn lives here.  Roster shape, team count,
bench/IL depth, FAAB budget and season length are all config values — the
engine reads them and never assumes 12 teams or a 23-man active roster.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

DEFAULT_CONFIG_PATH = Path(__file__).with_name("league_config.json")

# Slots that accept any batter / any pitcher rather than a specific position.
UTIL_SLOT = "UTIL"
ANY_PITCHER_SLOT = "P"
BATTER_POSITIONS = ("C", "1B", "2B", "3B", "SS", "OF", "DH")
PITCHER_POSITIONS = ("SP", "RP")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LeagueConfig:
    team_count: int = 12
    min_teams: int = 8
    max_teams: int = 14
    lobby_timeout_seconds: int = 300

    active_slots: dict[str, int] = field(
        default_factory=lambda: {
            "C": 1, "1B": 1, "2B": 1, "3B": 1, "SS": 1,
            "OF": 3, "UTIL": 3, "SP": 2, "RP": 3, "P": 4,
        }
    )
    bench_size: int = 17
    il_size: int = 5

    # What the league *says* the roster is. The itemised slot list above is the
    # authority on composition; these two numbers exist so a mismatch surfaces
    # instead of being silently resolved. See `roster_discrepancy`.
    declared_active_size: int = 23
    declared_roster_size: int = 45

    regular_season_weeks: int = 18
    playoff_teams: int = 8
    playoff_weeks: int = 4

    faab_budget: int = 100
    bots_use_waivers: bool = True
    waiver_clear_days: int = 2
    freeze_adds_final_weeks: int = 0

    eligible_year_min: int = 2000
    eligible_year_max: int | None = None
    excluded_years: tuple[int, ...] = (2020,)

    draft_order_mode: str = "speed_round"  # or "randomizer"
    speed_round_seconds: int = 10
    # Seconds a manager gets to make a pick before the room picks for them.
    # 0 disables the clock entirely and the draft waits indefinitely.
    draft_pick_seconds: int = 90

    # ---- derived -------------------------------------------------------
    @property
    def active_size(self) -> int:
        return sum(self.active_slots.values())

    @property
    def roster_size(self) -> int:
        return self.active_size + self.bench_size + self.il_size

    @property
    def total_weeks(self) -> int:
        return self.regular_season_weeks + self.playoff_weeks

    @property
    def finals_weeks(self) -> tuple[int, int]:
        last = self.total_weeks
        return (last - 1, last)

    def roster_discrepancy(self) -> dict[str, Any] | None:
        """Report a mismatch between the slot list and the declared totals.

        The league rules give both an itemised active roster (C 1, 1B 1, 2B 1,
        3B 1, SS 1, OF 3, UTIL 3, SP 2, RP 3, P 4) and headline totals of 23
        active / 45 overall.  The itemised list adds up to 20, so the totals
        cannot both be right.  Rather than invent three unspecified slots or
        quietly contradict the headline, the engine uses the itemised list —
        it is the only unambiguous statement of *composition* — and reports the
        gap here so the commissioner decides.  Closing it is one config edit,
        e.g. UTIL 3 -> 5 and P 4 -> 5.
        """
        if self.active_size == self.declared_active_size and \
                self.roster_size == self.declared_roster_size:
            return None
        return {
            "itemised_active_size": self.active_size,
            "declared_active_size": self.declared_active_size,
            "itemised_roster_size": self.roster_size,
            "declared_roster_size": self.declared_roster_size,
            "message": (
                f"The active slot list adds up to {self.active_size} starters "
                f"({self.roster_size} with a {self.bench_size}-man bench and "
                f"{self.il_size} IL slots), but the league rules also state "
                f"{self.declared_active_size} active / {self.declared_roster_size} total. "
                "The slot list is being used. Adjust active_slots (or the declared "
                "totals) in commissioner settings to settle it."
            ),
        }

    def eligible_years(self, today: dt.date | None = None) -> list[int]:
        today = today or dt.date.today()
        hi = self.eligible_year_max if self.eligible_year_max is not None else today.year - 1
        return [
            y for y in range(self.eligible_year_min, hi + 1)
            if y not in set(self.excluded_years)
        ]

    # ---- (de)serialisation --------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "LeagueConfig":
        raw = json.loads(Path(path or DEFAULT_CONFIG_PATH).read_text())
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LeagueConfig":
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in raw.items() if k in known and not k.startswith("_")}
        if "excluded_years" in kwargs and kwargs["excluded_years"] is not None:
            kwargs["excluded_years"] = tuple(kwargs["excluded_years"])
        cfg = cls(**kwargs)
        cfg.validate()
        return cfg

    def merged(self, overrides: Mapping[str, Any] | None) -> "LeagueConfig":
        if not overrides:
            return self
        known = {f for f in self.__dataclass_fields__}
        clean = {k: v for k, v in overrides.items() if k in known and v is not None}
        if "excluded_years" in clean:
            clean["excluded_years"] = tuple(clean["excluded_years"])
        if "active_slots" in clean:
            clean["active_slots"] = dict(clean["active_slots"])
        cfg = replace(self, **clean)
        cfg.validate()
        return cfg

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["excluded_years"] = list(self.excluded_years)
        d["active_size"] = self.active_size
        d["roster_size"] = self.roster_size
        d["total_weeks"] = self.total_weeks
        d["roster_discrepancy"] = self.roster_discrepancy()
        return d

    # ---- validation ----------------------------------------------------
    def validate(self) -> None:
        if not self.min_teams <= self.team_count <= self.max_teams:
            raise ConfigError(
                f"team_count {self.team_count} must be between "
                f"{self.min_teams} and {self.max_teams}"
            )
        if self.team_count % 2 != 0:
            raise ConfigError("team_count must be even so every team has a weekly opponent")
        if self.min_teams < 2 or self.min_teams % 2:
            raise ConfigError("min_teams must be an even number >= 2")
        if any(v < 0 for v in self.active_slots.values()):
            raise ConfigError("active slot counts cannot be negative")
        if self.bench_size < 0 or self.il_size < 0:
            raise ConfigError("bench_size and il_size cannot be negative")
        if self.playoff_teams > self.team_count:
            raise ConfigError(
                f"playoff_teams ({self.playoff_teams}) exceeds team_count ({self.team_count})"
            )
        if self.playoff_teams & (self.playoff_teams - 1):
            raise ConfigError("playoff_teams must be a power of two for a bye-free bracket")
        if self.playoff_weeks < self.bracket_rounds + 1:
            raise ConfigError(
                "playoff_weeks must cover one week per round plus the 2-week final"
            )
        if self.faab_budget < 0:
            raise ConfigError("faab_budget cannot be negative")
        if self.draft_pick_seconds < 0:
            raise ConfigError("draft_pick_seconds cannot be negative")

    @property
    def bracket_rounds(self) -> int:
        rounds, teams = 0, self.playoff_teams
        while teams > 1:
            teams //= 2
            rounds += 1
        return rounds


def pool_depth_check(cfg: LeagueConfig, pool_size: int, free_agent_floor: int = 150) -> dict:
    """Sanity-check that the season's player pool can support the league.

    Called before a draft goes live: ``team_count x roster_size`` players get
    drafted, and the waiver pool needs meaningful depth left over.
    """
    needed = cfg.team_count * cfg.roster_size
    surplus = pool_size - needed
    return {
        "pool_size": pool_size,
        "drafted_players": needed,
        "free_agents_after_draft": surplus,
        "free_agent_floor": free_agent_floor,
        "ok": surplus >= free_agent_floor,
        "message": (
            f"{pool_size} players available; {needed} will be drafted "
            f"({cfg.team_count} teams x {cfg.roster_size}), leaving {surplus} free agents."
        ),
    }
