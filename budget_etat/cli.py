import argparse
import sys


def main() -> None:
    p = argparse.ArgumentParser(prog="budget")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="télécharge les sources brutes (cache data/raw/)")
    sub.add_parser("inspect", help="profile les fichiers bruts -> data/INSPECTION.md")
    sub.add_parser("ingest", help="normalise data/raw/ -> data/budget.duckdb")
    s = sub.add_parser("serve", help="lance l'interface web locale")
    s.add_argument("--port", type=int, default=8000)
    sub.add_parser("all", help="fetch + inspect + ingest + serve")
    args = p.parse_args()

    from budget_etat import fetch, ingest, inspect_raw, server

    if args.cmd == "fetch":
        sys.exit(fetch.run())
    if args.cmd == "inspect":
        sys.exit(inspect_raw.run())
    if args.cmd == "ingest":
        sys.exit(ingest.run())
    if args.cmd == "serve":
        server.serve(port=args.port)
    if args.cmd == "all":
        # Les échecs réseau / parseurs sont affichés mais n'empêchent pas de lancer l'interface.
        fetch.run()
        inspect_raw.run()
        ingest.run()
        server.serve()
