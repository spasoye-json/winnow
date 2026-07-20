import argparse
import os
from datetime import UTC, datetime

from winnow import auth
from winnow.db import connect, init_db

DEFAULT_DB_PATH = os.environ.get("WINNOW_DB", "winnow.db")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="winnow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create the database schema")
    init_parser.add_argument("--db", default=DEFAULT_DB_PATH)

    connect_parser = subparsers.add_parser(
        "connect", help="connect a Google account via OAuth"
    )
    connect_parser.add_argument("--db", default=DEFAULT_DB_PATH)
    connect_parser.add_argument(
        "--client-secrets", default=auth.client_secrets_path()
    )

    serve_parser = subparsers.add_parser(
        "serve", help="run the web app and background due-check loop"
    )
    serve_parser.add_argument("--db", default=DEFAULT_DB_PATH)
    serve_parser.add_argument(
        "--client-secrets", default=auth.client_secrets_path()
    )
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)

    score_parser = subparsers.add_parser(
        "score", help="fetch transcripts and score pending videos"
    )
    score_parser.add_argument("--db", default=DEFAULT_DB_PATH)

    args = parser.parse_args(argv)

    if args.command == "init":
        conn = connect(args.db)
        try:
            init_db(conn)
        finally:
            conn.close()
        print(f"initialized {args.db}")
    elif args.command == "connect":
        client_config = auth.load_client_config(args.client_secrets)
        creds = auth.run_consent_flow(client_config)
        conn = connect(args.db)
        try:
            auth.save_credentials(
                conn, creds, connected_at=datetime.now(UTC).isoformat()
            )
        finally:
            conn.close()
        print("connected google account")
    elif args.command == "serve":
        import logging

        import uvicorn

        from winnow.web import create_app

        logging.basicConfig(level=logging.INFO)
        app = create_app(args.db, args.client_secrets)
        uvicorn.run(app, host=args.host, port=args.port)
    elif args.command == "score":
        from winnow.scoring import build_client, model_name, run_scoring
        from winnow.transcript import fetch_transcript

        conn = connect(args.db)
        try:
            run_scoring(conn, fetch_transcript, build_client(), model_name())
        finally:
            conn.close()
        print("scored pending videos")
