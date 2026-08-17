"""Open every page in a real browser and fail on any console error.

A typecheck proves the code compiles; it does not prove a page renders. This
walks the app the way a manager would, captures screenshots, and treats any
uncaught exception, console error or failed request as a failure.

    python -m scripts.ui_smoke --base http://localhost:8077 --code ABC123 \
        --manager-token ... --commissioner-token ...
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

# Ignorable noise: nothing here indicates a broken page.
IGNORE = ("favicon", "Download the React DevTools")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8077")
    ap.add_argument("--code", required=True)
    ap.add_argument("--manager-token", required=True)
    ap.add_argument("--team-id", required=True)
    ap.add_argument("--commissioner-token", required=True)
    ap.add_argument("--out", default="/tmp/ui-smoke")
    ap.add_argument("--mobile", action="store_true")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    pages = [
        ("home", "/"),
        ("lobby", f"/l/{args.code}"),
        ("team", f"/l/{args.code}/team"),
        ("last-night", f"/l/{args.code}/day"),
        ("matchups", f"/l/{args.code}/matchups"),
        ("standings", f"/l/{args.code}/standings"),
        ("waivers", f"/l/{args.code}/waivers"),
        ("draft", f"/l/{args.code}/draft"),
        ("rules", f"/l/{args.code}/rules"),
        ("commissioner", f"/l/{args.code}/commissioner"),
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        context = browser.new_context(
            viewport={"width": 390, "height": 844} if args.mobile
            else {"width": 1280, "height": 900},
            device_scale_factor=2 if args.mobile else 1,
        )
        page = context.new_page()
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                if m.type == "error" and not any(s in m.text for s in IGNORE) else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: problems.append(f"requestfailed: {r.url}")
                if not any(s in r.url for s in IGNORE) else None)

        # Seed the tokens the app expects in local storage.
        page.goto(f"{args.base}/", wait_until="networkidle")
        page.evaluate(
            """([code, manager, team, commish]) => {
                localStorage.setItem(`rsr:manager:${code}`, manager);
                localStorage.setItem(`rsr:team:${code}`, team);
                localStorage.setItem(`rsr:commish:${code}`, commish);
            }""",
            [args.code, args.manager_token, args.team_id, args.commissioner_token],
        )

        for name, path in pages:
            before = len(problems)
            page.goto(f"{args.base}{path}", wait_until="networkidle")
            page.wait_for_timeout(700)
            body = page.inner_text("body")
            if "Loading" in body and len(body) < 120:
                problems.append(f"{name}: page never finished loading")
            if not body.strip():
                problems.append(f"{name}: rendered nothing")
            suffix = "-mobile" if args.mobile else ""
            page.screenshot(path=str(out / f"{name}{suffix}.png"), full_page=True)
            status = "ok" if len(problems) == before else "PROBLEMS"
            print(f"  {name:<14} {status}  ({len(body.split())} words)")

        # Exercise the one interaction the screenshots cannot: saving a lineup.
        page.goto(f"{args.base}/l/{args.code}/team", wait_until="networkidle")
        save = page.get_by_role("button", name="Save lineup")
        if save.count():
            save.first.click()
            page.wait_for_timeout(900)
            if "saved" not in page.inner_text("body").lower():
                problems.append("lineup save produced no confirmation")
            else:
                print("  lineup save    ok")

        browser.close()

    if problems:
        print("\nPROBLEMS FOUND:")
        for p_ in dict.fromkeys(problems):
            print(f"  - {p_}")
        return 1
    print(f"\nall pages clean — screenshots in {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
