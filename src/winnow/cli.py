import argparse
import os

from winnow.db import connect, init_db

DEFAULT_DB_PATH = os.environ.get("WINNOW_DB", "winnow.db")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="winnow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create the database schema")
    init_parser.add_argument("--db", default=DEFAULT_DB_PATH)

    args = parser.parse_args(argv)

    if args.command == "init":
        conn = connect(args.db)
        try:
            init_db(conn)
        finally:
            conn.close()
        print(f"initialized {args.db}")
