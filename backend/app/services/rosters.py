"""Roster shape: slot expansion, eligibility matching and feasibility.

Filling a lineup is a bipartite matching problem — a player may be eligible for
several slots (a 2B/OF fits 2B, OF or UTIL), so greedy assignment can strand a
slot that only one player could have filled.  Everything that needs to answer
"can this set of players fill these slots?" goes through :func:`max_matching`.

The same routine backs three things:
  * lineup validation and auto-fill,
  * the draft's feasibility check (don't let a manager draft themselves into a
    roster that cannot field a legal lineup),
  * bot lineup setting.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .players import eligible_slots


def expand_slots(active_slots: dict[str, int]) -> list[str]:
    """['C','1B',...,'OF','OF','OF',...] — one entry per startable spot."""
    from .players import slot_fill_order
    out: list[str] = []
    for slot in slot_fill_order(active_slots):
        out.extend([slot] * active_slots[slot])
    return out


def eligibility_map(
    players: Sequence[dict[str, Any]], active_slots: dict[str, int]
) -> dict[str, list[str]]:
    return {p["player_id"]: eligible_slots(p, active_slots.keys()) for p in players}


def max_matching(
    players: Sequence[dict[str, Any]],
    active_slots: dict[str, int],
    forced: dict[str, str] | None = None,
) -> dict[int, str]:
    """Assign players to slot instances, maximising slots filled.

    Returns ``{slot_index: player_id}`` over :func:`expand_slots`.  ``forced``
    pins specific players to specific slot names (a manager's own choices) and
    the rest are matched around them.
    """
    slots = expand_slots(active_slots)
    elig = eligibility_map(players, active_slots)
    assignment: dict[int, str] = {}
    taken: set[str] = set()
    locked: set[int] = set()

    for player_id, slot_name in (forced or {}).items():
        for idx, name in enumerate(slots):
            if name == slot_name and idx not in assignment:
                if slot_name not in elig.get(player_id, []):
                    raise ValueError(f"{player_id} is not eligible at {slot_name}")
                assignment[idx] = player_id
                taken.add(player_id)
                # A manager's own choice is immovable: the matching fills the
                # rest around it rather than reshuffling it away.
                locked.add(idx)
                break
        else:
            raise ValueError(f"no free {slot_name} slot")

    slot_of_player: dict[str, int] = {v: k for k, v in assignment.items()}

    def augment(player_id: str, seen: set[int]) -> bool:
        for idx, name in enumerate(slots):
            if name not in elig.get(player_id, []) or idx in seen or idx in locked:
                continue
            seen.add(idx)
            occupant = assignment.get(idx)
            if occupant is None or augment(occupant, seen):
                assignment[idx] = player_id
                slot_of_player[player_id] = idx
                return True
        return False

    for p in players:
        pid = p["player_id"]
        if pid in taken or pid in slot_of_player:
            continue
        augment(pid, set())
    return assignment


def unfilled_slots(
    players: Sequence[dict[str, Any]], active_slots: dict[str, int]
) -> list[str]:
    slots = expand_slots(active_slots)
    filled = max_matching(players, active_slots)
    return [name for idx, name in enumerate(slots) if idx not in filled]


def can_fill_active(
    players: Sequence[dict[str, Any]], active_slots: dict[str, int]
) -> bool:
    return not unfilled_slots(players, active_slots)


def draft_feasible(
    roster: Sequence[dict[str, Any]],
    candidate: dict[str, Any],
    active_slots: dict[str, int],
    picks_remaining_after: int,
) -> tuple[bool, str]:
    """Would taking ``candidate`` leave enough picks to field a legal lineup?

    A manager who spends every pick on outfielders should be stopped before the
    draft ends with no catcher, not after.
    """
    prospective = list(roster) + [candidate]
    gaps = unfilled_slots(prospective, active_slots)
    if len(gaps) > picks_remaining_after:
        need = ", ".join(sorted(set(gaps)))
        return False, (
            f"only {picks_remaining_after} picks left but {len(gaps)} active slots "
            f"would still be unfillable ({need})"
        )
    return True, ""


def roster_summary(
    players: Iterable[dict[str, Any]], active_slots: dict[str, int]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in players:
        for slot in eligible_slots(p, active_slots.keys()):
            counts[slot] = counts.get(slot, 0) + 1
    return counts
