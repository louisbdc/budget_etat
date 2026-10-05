import argparse
import sys


def main() -> None:
    p = argparse.ArgumentParser(prog="budget")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="télécharge les sources brutes (cache data/raw/)")
    args = p.parse_args()
    if args.cmd == "fetch":
        from budget_etat import fetch

        sys.exit(fetch.run())
