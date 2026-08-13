"""ProSportsTransactions.com -> injured-list stints.

PST publishes a searchable, well-structured transaction log.  The injured-list
view gives one row per transaction with columns:

    Date | Team | Acquired | Relinquished | Notes

A player appearing under *Relinquished* with a note like "placed on 10-day IL"
starts a stint; the same player under *Acquired* with "activated from 10-day
IL" ends it.  This module pairs those rows into ``(start_date, end_date)``
stints keyed to our player IDs.

Terms of use
------------
PST is a free, human-facing site with no public API and no published bulk feed.
Before running this against the live site: keep the request rate low (this
module sleeps between pages by default), identify the client honestly via
User-Agent, cache aggressively so a season is fetched at most once, and check
the site's current terms.  ``preflight()`` reports whether the host is even
reachable; if it isn't, the pipeline falls back to synthetic IL data rather
than silently producing a season with no injuries.
"""

from __future__ import annotations

import datetime as dt
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Iterable

BASE_URL = "https://www.prosportstransactions.com/baseball/Search/SearchResults.php"
USER_AGENT = "RetroSeasonReplay/1.0 (fantasy league replay tool; contact commissioner)"
PAGE_SIZE = 25
POLITE_DELAY_SECONDS = 1.5

PLACED_RE = re.compile(r"placed on|transferred to|moved to", re.I)
ACTIVATED_RE = re.compile(r"activated|reinstated|returned to (the )?lineup", re.I)
IL_KIND_RE = re.compile(r"(\d+)-day (?:IL|DL)", re.I)


class SourceUnavailable(RuntimeError):
    pass


class _TableParser(HTMLParser):
    """Pulls the datatable rows out of a PST results page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._in_table = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrd = dict(attrs)
        if tag == "table" and "datatable" in (attrd.get("class") or ""):
            self._in_table = True
        elif self._in_table and tag == "tr":
            self._row = []
        elif self._in_table and tag == "td":
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._in_table:
            self._in_table = False
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def preflight(timeout: int = 10) -> dict[str, Any]:
    try:
        req = urllib.request.Request(BASE_URL, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 400
            return {"ready": ok, "detail": f"HTTP {resp.status}"}
    except Exception as exc:  # noqa: BLE001
        return {"ready": False, "detail": f"{type(exc).__name__}: {exc}"}


def _page_url(year: int, start: int) -> str:
    params = {
        "Player": "", "Team": "",
        "BeginDate": f"{year}-01-01", "EndDate": f"{year}-12-31",
        "ILChkBx": "yes", "InjuriesChkBx": "yes",
        "Submit": "Search", "start": str(start),
    }
    return f"{BASE_URL}?{urllib.parse.urlencode(params)}"


def fetch_rows(year: int, max_pages: int = 400, delay: float = POLITE_DELAY_SECONDS) -> list[list[str]]:
    rows: list[list[str]] = []
    for page in range(max_pages):
        url = _page_url(year, page * PAGE_SIZE)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                html = resp.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            raise SourceUnavailable(f"could not fetch {url}: {exc}") from exc
        parser = _TableParser()
        parser.feed(html)
        body = [r for r in parser.rows if len(r) >= 5 and re.match(r"\d{4}-\d{2}-\d{2}", r[0])]
        if not body:
            break
        rows.extend(body)
        if len(body) < PAGE_SIZE:
            break
        time.sleep(delay)
    return rows


def _clean_name(cell: str) -> str:
    """PST prefixes names with a bullet and may append '(DFA)' style notes."""
    name = cell.replace("•", " ").strip()
    name = re.sub(r"\s*\(.*?\)\s*", " ", name)
    return " ".join(name.split())


def normalise_key(name: str) -> str:
    key = name.lower()
    key = re.sub(r"[^a-z ]", "", key)
    key = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", key)
    return " ".join(key.split())


def parse_stints(rows: Iterable[list[str]], year: int) -> list[dict[str, Any]]:
    """Pair 'placed on IL' rows with their matching 'activated' rows."""
    open_stints: dict[str, dict[str, Any]] = {}
    stints: list[dict[str, Any]] = []

    for row in sorted(rows, key=lambda r: r[0]):
        date, _team, acquired, relinquished, notes = row[0], row[1], row[2], row[3], row[4]
        kind_match = IL_KIND_RE.search(notes)
        kind = f"{kind_match.group(1)}-day IL" if kind_match else "IL"

        if relinquished and PLACED_RE.search(notes or ""):
            for name in _split_names(relinquished):
                key = normalise_key(name)
                if key in open_stints:
                    continue  # transfer between IL types; keep the original start
                open_stints[key] = {
                    "name": name, "season": year, "start_date": date,
                    "end_date": None, "kind": kind, "note": notes,
                }
        if acquired and ACTIVATED_RE.search(notes or ""):
            for name in _split_names(acquired):
                key = normalise_key(name)
                stint = open_stints.pop(key, None)
                if stint is None:
                    continue
                stint["end_date"] = date
                stints.append(stint)

    stints.extend(open_stints.values())  # never activated => out for the season
    return stints


def _split_names(cell: str) -> list[str]:
    parts = [p for p in re.split(r"•|;", cell) if p.strip()]
    return [_clean_name(p) for p in parts] or ([_clean_name(cell)] if cell.strip() else [])


def attach_player_ids(
    stints: list[dict[str, Any]], players: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve PST names to season player IDs. Returns (matched, unmatched names)."""
    index: dict[str, list[dict[str, Any]]] = {}
    for p in players:
        index.setdefault(normalise_key(p["name"]), []).append(p)

    matched: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for stint in stints:
        candidates = index.get(normalise_key(stint["name"]), [])
        if len(candidates) != 1:
            # Ambiguous or absent: recording a guess would lock the wrong player
            # out of lineups, so skip and report instead.
            unmatched.append(stint["name"])
            continue
        matched.append({
            "season": stint["season"],
            "player_id": candidates[0]["player_id"],
            "start_date": stint["start_date"],
            "end_date": stint["end_date"],
            "kind": stint["kind"],
            "note": stint["note"],
        })
    return matched, unmatched


def coverage_check(stints: list[dict[str, Any]], year: int, min_expected: int = 300) -> dict[str, Any]:
    """Is this season's IL data complete enough to replay honestly?

    A modern MLB season has 500+ IL placements. Far fewer means the scrape or
    the source is incomplete, and the season should be dropped from the random
    draw rather than replayed with silently missing injuries.
    """
    dated = [s for s in stints if s.get("start_date")]
    months = {dt.date.fromisoformat(s["start_date"]).month for s in dated}
    in_season_months = {m for m in months if 4 <= m <= 9}
    ok = len(dated) >= min_expected and len(in_season_months) >= 5
    return {
        "year": year,
        "stints": len(dated),
        "months_covered": sorted(in_season_months),
        "ok": ok,
        "reason": None if ok else (
            f"only {len(dated)} IL stints across months {sorted(in_season_months)}; "
            "expected 300+ spread over the season"
        ),
    }


def build(year: int, players: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    check = preflight()
    if not check["ready"]:
        raise SourceUnavailable(f"prosportstransactions unreachable: {check['detail']}")
    rows = fetch_rows(year)
    stints = parse_stints(rows, year)
    matched, unmatched = attach_player_ids(stints, players)
    report = coverage_check(matched, year)
    report["unmatched_names"] = len(unmatched)
    report["unmatched_sample"] = sorted(set(unmatched))[:20]
    return matched, report
