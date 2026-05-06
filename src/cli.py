from __future__ import annotations

import argparse
from datetime import date, time

from discovery.interfaces import ScanCriteria


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Golfbot CLI")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    scan = subparsers.add_parser("scan", help="Find matching tee times")
    _add_scan_args(scan)

    book = subparsers.add_parser("book", help="Attempt booking when criteria match")
    _add_scan_args(book)
    book.add_argument("--payment-profile-id", required=True)
    book.add_argument("--contact-email", required=True)

    return parser.parse_args()


def _add_scan_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--course-id", required=True)
    parser.add_argument("--play-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--players", required=True, type=int)
    parser.add_argument("--time-start", required=True, help="HH:MM")
    parser.add_argument("--time-end", required=True, help="HH:MM")


def _criteria_from_args(args: argparse.Namespace) -> ScanCriteria:
    return ScanCriteria(
        course_id=args.course_id,
        play_date=date.fromisoformat(args.play_date),
        players=args.players,
        time_start=time.fromisoformat(args.time_start),
        time_end=time.fromisoformat(args.time_end),
    )


def run() -> None:
    args = parse_args()
    criteria = _criteria_from_args(args)

    if args.mode == "scan":
        print(f"scan mode: {criteria}")
        return

    print(
        "book mode:",
        {
            "criteria": criteria,
            "payment_profile_id": args.payment_profile_id,
            "contact_email": args.contact_email,
        },
    )


if __name__ == "__main__":
    run()
