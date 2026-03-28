#!/usr/bin/env python3
"""
Print what notify.py fetches and how many pass the Canada + intern + CS filter.
No email, no changes to state/seen.json. Run from repo root:

  pip install -r requirements.txt
  python verify_sources.py
"""
from __future__ import annotations

from notify import (
    collect_jobs,
    is_canada_location,
    load_config,
    matches_intern_cs,
)


def main() -> None:
    cfg = load_config()
    rss_n = len(cfg.get("rss") or [])
    gh_n = len(cfg.get("greenhouse_boards") or [])
    lev_n = len(cfg.get("lever") or [])
    print(f"Configured sources: rss={rss_n}, greenhouse_boards={gh_n}, lever={lev_n}")
    print()

    all_jobs = collect_jobs(cfg)
    print(f"Total job postings fetched (all titles): {len(all_jobs)}")

    filtered = [
        j for j in all_jobs if matches_intern_cs(j.title) and is_canada_location(j.location_text)
    ]
    print(f"After intern/co-op + CS + Canada filter: {len(filtered)}")
    print()

    if not all_jobs:
        print("Nothing was returned from APIs. Check feeds.yaml and network.")
        return

    if filtered:
        print("Sample matches (up to 15):")
        for j in filtered[:15]:
            loc = (j.location_text or "")[:100]
            print(f"  • {j.title}")
            print(f"    {j.url}")
            if loc:
                print(f"    ({loc}…)" if len(j.location_text or "") > 100 else f"    ({loc})")
    else:
        print("No rows matched all filters. Sample raw titles (up to 10) for debugging:")
        for j in all_jobs[:10]:
            print(f"  • {j.title} [{j.source}]")


if __name__ == "__main__":
    main()
