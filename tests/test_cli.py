from winnow.cli import main
from winnow.db import connect

EXPECTED_TABLES = {
    "oauth_credentials",
    "channels",
    "topics",
    "videos",
    "scores",
    "feedback",
    "settings",
}


def table_names(path):
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def test_init_command_creates_database(tmp_path):
    db_path = tmp_path / "winnow.db"
    main(["init", "--db", str(db_path)])
    assert db_path.exists()
    assert EXPECTED_TABLES <= table_names(str(db_path))


def test_init_command_is_idempotent(tmp_path):
    db_path = tmp_path / "winnow.db"
    main(["init", "--db", str(db_path)])
    main(["init", "--db", str(db_path)])
    assert EXPECTED_TABLES <= table_names(str(db_path))
