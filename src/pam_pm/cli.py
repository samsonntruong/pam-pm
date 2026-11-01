"""PAM PM command line interface (minimal placeholder)."""

import argparse

from pam_pm import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pam", description="PAM PM placeholder CLI")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("start", help="Start placeholder")
    sub.add_parser("doctor", help="Doctor placeholder")
    sub.add_parser("review", help="Review placeholder")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return
    if args.command == "start":
        print("start: not implemented yet")
    elif args.command == "doctor":
        print("doctor: ok")
    elif args.command == "review":
        print("review: not implemented yet")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
