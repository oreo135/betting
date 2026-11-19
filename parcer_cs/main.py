import argparse
import json
import sys
from src.utils import setup_logger
from src.search import find_player
from src.player import get_player_stats, get_player_stats_overview, save_player
from src.matches import collect_player_matches, build_ml_dataset
from src.events import find_event, collect_event_player_ids, find_and_collect
from src import browser_client, db

logger = setup_logger()


def main():
    db.init_db()
    parser = argparse.ArgumentParser(description="HLTV player parser")
    parser.add_argument("nick", nargs="?", help="Player nickname to search")
    parser.add_argument("--event", metavar="QUERY", help="Collect player IDs for a tournament (search by name)")
    parser.add_argument("--event-id", metavar="ID", type=int, help="Skip search: use this event ID directly")
    parser.add_argument("--event-slug", metavar="SLUG", help="Slug for --event-id (required if --event-id is given)")
    parser.add_argument("--pause", type=float, default=2.0, help="Seconds between team page requests (default 2)")
    parser.add_argument("--save", action="store_true", help="Save result to data/")
    parser.add_argument("--stats-overview", action="store_true", help="Parse /stats/players overview (header, cards, statistics, featured, teammates, form)")
    parser.add_argument("--matches", action="store_true", help="Collect match history (L1+L3)")
    parser.add_argument("--ml", action="store_true", help="Output flat ML-ready records instead of raw match data")
    parser.add_argument("--start", metavar="YYYY-MM-DD", help="Match history start date")
    parser.add_argument("--end", metavar="YYYY-MM-DD", help="Match history end date")
    parser.add_argument("--limit", type=int, default=None, help="Max matches to fetch (L3)")
    parser.add_argument("--team", metavar="QUERY", help="First team name for matchup scouting")
    parser.add_argument("--team2", metavar="QUERY", help="Second team name for matchup scouting")
    parser.add_argument("--collect-matchup", action="store_true", help="Collect last --n-matches maps for both teams and print scouting summary")
    parser.add_argument("--n-matches", type=int, default=30, help="Maps per player for --collect-matchup (default 30)")
    # Live match tracking
    parser.add_argument("--match-id", type=int, metavar="ID", help="HLTV match ID for live tracking")
    parser.add_argument("--match-slug", metavar="SLUG", help="HLTV match slug (from URL)")
    parser.add_argument("--live-snapshot", action="store_true", help="Capture one HTML snapshot + parse state (use with --match-id/--match-slug)")
    parser.add_argument("--live-poll", action="store_true", help="Start polling loop for live scores (use with --match-id/--match-slug)")
    parser.add_argument("--interval", type=int, default=30, help="Poll interval in seconds for --live-poll (default 30)")
    parser.add_argument("--live-series", action="store_true", help="Print stored score time-series for a match (use with --match-id)")
    args = parser.parse_args()

    # ── Live match pipeline ──────────────────────────────────────────────────
    if args.live_snapshot or args.live_poll or args.live_series:
        from src.live import (
            get_live_match_state, save_html_snapshot,
            poll_live_score, dump_match_series,
        )
        from src import browser_client as _bc

        if args.live_series:
            if not args.match_id:
                parser.error("--live-series requires --match-id")
            dump_match_series(args.match_id)
            return

        if not args.match_id or not args.match_slug:
            parser.error("--live-snapshot and --live-poll require --match-id and --match-slug")

        try:
            if args.live_snapshot:
                maps = get_live_match_state(args.match_id, args.match_slug)
                # also save HTML for selector inspection
                html = _bc.get_html_snapshot()
                snap_path = save_html_snapshot(html, args.match_id)
                print(f"Snapshot saved: {snap_path}")
                for m in maps:
                    score = f"{m.score_t1}:{m.score_t2}" if m.score_t1 is not None else " -:-"
                    live_tag = "  LIVE" if m.status == "live" else ""
                    print(f"  [{m.map_index}] {m.map_name:<12} {score:>7}  ({m.status}){live_tag}")
            elif args.live_poll:
                poll_live_score(
                    args.match_id,
                    args.match_slug,
                    interval_seconds=args.interval,
                    save_snapshots=True,
                )
        finally:
            _bc.close()
        return

    # ── Matchup pipeline ─────────────────────────────────────────────────────
    if args.collect_matchup:
        import glob
        import os
        if not args.team or not args.team2:
            parser.error("--collect-matchup requires --team and --team2")
        from src.matchup import (
            collect_matchup, run_checks,
            save_matchup, print_matchup_summary, print_warnings,
        )
        logger.info(
            f"=== Matchup: {args.team!r} vs {args.team2!r}, last {args.n_matches} maps ==="
        )
        try:
            data = collect_matchup(
                args.team, args.team2,
                n_matches=args.n_matches,
                pause=args.pause,
            )
            if not data:
                logger.error("Matchup collection failed.")
                sys.exit(1)

            warnings = run_checks(data, n_matches=args.n_matches)

            print_matchup_summary(data)
            print_warnings(warnings)

            if args.save:
                team_a_name = data["teams"][0]["name"]
                team_b_name = data["teams"][1]["name"]
                out_dir = save_matchup(data, warnings, team_a_name, team_b_name)
                print(f"\nSaved to {out_dir}/")

                # ── Cleanup: show old data/matchup_*.json and ask ────────────
                old_files = sorted(glob.glob(os.path.join("data", "matchup_*.json")))
                if old_files:
                    print(f"\nFound {len(old_files)} old matchup file(s) in data/ root:")
                    for fpath in old_files:
                        st = os.stat(fpath)
                        mtime = __import__("datetime").datetime.fromtimestamp(st.st_mtime)
                        print(f"  {fpath}  ({st.st_size:,} B, {mtime:%Y-%m-%d %H:%M})")
                    try:
                        answer = input("\nDelete these files? [y/N] ").strip().lower()
                    except EOFError:
                        answer = "n"
                    if answer == "y":
                        for fpath in old_files:
                            os.remove(fpath)
                            print(f"  Deleted: {fpath}")
                    else:
                        print("  Skipped — files untouched.")
        finally:
            browser_client.close()
        return

    # ── Event pipeline ──────────────────────────────────────────────────────
    if args.event_id:
        if not args.event_slug:
            parser.error("--event-id requires --event-slug")
        logger.info(f"=== Event pipeline: id={args.event_id} slug={args.event_slug} ===")
        try:
            data = collect_event_player_ids(args.event_id, args.event_slug, pause=args.pause)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            if args.save:
                import os
                os.makedirs("data", exist_ok=True)
                path = f"data/event_{args.event_id}_players.json"
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                logger.info(f"Saved to {path}")
        finally:
            browser_client.close()
        return

    if args.event:
        logger.info(f"=== Event search: {args.event!r} ===")
        try:
            result = find_and_collect(args.event, pause=args.pause)
            if "candidates" in result:
                print("Multiple events found — refine your query or use --event-id / --event-slug:")
                print(json.dumps(result["candidates"], ensure_ascii=False, indent=2))
            elif "error" in result:
                print(result["error"])
                sys.exit(1)
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if args.save:
                    import os
                    os.makedirs("data", exist_ok=True)
                    ev = result.get("event", {})
                    path = f"data/event_{ev.get('id', 'unknown')}_players.json"
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(result, f, ensure_ascii=False, indent=2)
                    logger.info(f"Saved to {path}")
        finally:
            browser_client.close()
        return

    # ── Player pipeline ──────────────────────────────────────────────────────
    if not args.nick:
        parser.print_help()
        sys.exit(1)

    logger.info(f"=== Starting parser for nick='{args.nick}' ===")

    try:
        player_ref = find_player(args.nick)
        if not player_ref:
            logger.error("Player not found, exiting.")
            sys.exit(1)

        pid, pnick = player_ref["id"], player_ref["nick"]

        if args.stats_overview:
            data = get_player_stats_overview(pid, pnick)
            if not data:
                logger.error("Failed to parse stats overview.")
                sys.exit(1)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            if args.save:
                import os
                path = f"data/{pnick}_{pid}_overview.json"
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                logger.info(f"Saved to {path}")
        elif args.matches:
            matches = collect_player_matches(
                pid, pnick,
                start_date=args.start,
                end_date=args.end,
                limit=args.limit,
            )
            if args.ml:
                output = build_ml_dataset(matches, pnick)
            else:
                output = matches
            print(json.dumps(output, ensure_ascii=False, indent=2))
            if args.save:
                import os
                suffix = "_ml" if args.ml else "_full"
                path = f"data/matches/{pid}{suffix}.json"
                os.makedirs("data/matches", exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(output, f, ensure_ascii=False, indent=2)
                logger.info(f"Saved matches to {path}")
        else:
            data = get_player_stats(pid, pnick)
            if not data:
                logger.error("Failed to parse player stats.")
                sys.exit(1)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            if args.save:
                path = save_player(data)
                logger.info(f"Saved to {path}")

    finally:
        browser_client.close()


if __name__ == "__main__":
    main()
