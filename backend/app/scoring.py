"""Fantasy scoring engine.

All point values live in ``scoring.json`` so the commissioner can retune the
league without touching this module.  Nothing else in the codebase should
contain a literal point value.

The engine takes *raw* box-score lines (the same shape the data pipeline
writes into SQLite) and returns a point total plus an itemised breakdown, so
the weekly recap can show exactly where a player's points came from.

Derived events (cycle, grand slam, quality start) are computed here rather than
stored, because no upstream data source exposes them as first-class box-score
columns.  See ``pipeline/coverage.py`` for which stats each source can and
cannot supply.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

DEFAULT_CONFIG_PATH = Path(__file__).with_name("scoring.json")


@dataclass(frozen=True)
class ScoringConfig:
    batting: dict[str, float]
    pitching: dict[str, float]
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "ScoringConfig":
        raw = json.loads(Path(path or DEFAULT_CONFIG_PATH).read_text())
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ScoringConfig":
        strip = lambda d: {k: float(v) for k, v in d.items() if not k.startswith("_")}
        return cls(
            batting=strip(raw.get("batting", {})),
            pitching=strip(raw.get("pitching", {})),
            options={k: v for k, v in raw.get("options", {}).items() if not k.startswith("_")},
        )

    def to_dict(self) -> dict[str, Any]:
        return {"batting": self.batting, "pitching": self.pitching, "options": self.options}

    def option(self, key: str, default: Any) -> Any:
        return self.options.get(key, default)


@dataclass
class ScoreLine:
    """A scored stat line: total plus per-category contributions."""

    points: float
    breakdown: dict[str, float]

    def merge(self, other: "ScoreLine") -> "ScoreLine":
        combined = dict(self.breakdown)
        for key, value in other.breakdown.items():
            combined[key] = combined.get(key, 0.0) + value
        return ScoreLine(round(self.points + other.points, 4), combined)


def _get(line: Mapping[str, Any], key: str) -> int:
    value = line.get(key)
    if value is None:
        return 0
    return int(value)


# --------------------------------------------------------------------------
# derived event detection
# --------------------------------------------------------------------------

def grand_slams(line: Mapping[str, Any]) -> int:
    """Grand slams in this game.

    Retrosheet event data yields this exactly.  Traditional box scores do not
    carry it, so the pipeline stores an explicit ``slam`` column and leaves it
    at 0 when the source cannot supply it (never guessed from HR + RBI, which
    would produce false positives).
    """
    return _get(line, "slam")


def is_quality_start(line: Mapping[str, Any], cfg: ScoringConfig) -> bool:
    if not _get(line, "gs"):
        return False
    min_outs = int(cfg.option("quality_start_min_outs", 18))
    max_er = int(cfg.option("quality_start_max_er", 3))
    return _get(line, "outs") >= min_outs and _get(line, "er") <= max_er


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def score_batting(
    line: Mapping[str, Any], cfg: ScoringConfig, include_derived: bool = True
) -> ScoreLine:
    """Score one batting line.

    ``include_derived=False`` skips the per-game bonuses (cycle, grand slam).
    Pass it when scoring a *summed* line — a season total trivially contains a
    single, double, triple and home run, which is not a cycle.
    """
    p = cfg.batting
    bb = _get(line, "bb")
    ibb = _get(line, "ibb")
    if not cfg.option("ibb_stacks_with_bb", True):
        bb = max(0, bb - ibb)

    items: dict[str, float] = {
        "R": _get(line, "r") * p.get("R", 0),
        "1B": _get(line, "b1") * p.get("1B", 0),
        "2B": _get(line, "b2") * p.get("2B", 0),
        "3B": _get(line, "b3") * p.get("3B", 0),
        "HR": _get(line, "hr") * p.get("HR", 0),
        "RBI": _get(line, "rbi") * p.get("RBI", 0),
        "SB": _get(line, "sb") * p.get("SB", 0),
        "BB": bb * p.get("BB", 0),
        "IBB": ibb * p.get("IBB", 0),
        "HBP": _get(line, "hbp") * p.get("HBP", 0),
        "K": _get(line, "so") * p.get("K", 0),
    }
    if include_derived:
        slams = grand_slams(line)
        if slams:
            items["SLAM"] = slams * p.get("SLAM", 0)

    items = {k: round(v, 4) for k, v in items.items() if v}
    return ScoreLine(round(sum(items.values()), 4), items)


def score_pitching(
    line: Mapping[str, Any], cfg: ScoringConfig, include_derived: bool = True
) -> ScoreLine:
    """Score one pitching line.

    ``include_derived=False`` skips the quality start, which is a per-game
    event and meaningless on a summed line.
    """
    p = cfg.pitching
    outs = _get(line, "outs")

    items: dict[str, float] = {
        # Partial innings score pro-rata: 5.2 IP is 17 outs, not 5 innings.
        "IP": (outs / 3.0) * p.get("IP", 0),
        "W": _get(line, "w") * p.get("W", 0),
        "CG": _get(line, "cg") * p.get("CG", 0),
        "SV": _get(line, "sv") * p.get("SV", 0),
        "ER": _get(line, "er") * p.get("ER", 0),
        "K": _get(line, "so") * p.get("K", 0),
    }
    if include_derived and is_quality_start(line, cfg):
        items["QS"] = p.get("QS", 0)

    items = {k: round(v, 4) for k, v in items.items() if v}
    return ScoreLine(round(sum(items.values()), 4), items)


def score_day(
    batting: Mapping[str, Any] | None,
    pitching: Mapping[str, Any] | None,
    cfg: ScoringConfig,
) -> ScoreLine:
    """Score one player's full day (two-way players get both halves)."""
    total = ScoreLine(0.0, {})
    if batting:
        total = total.merge(score_batting(batting, cfg))
    if pitching:
        total = total.merge(score_pitching(pitching, cfg))
    return total
