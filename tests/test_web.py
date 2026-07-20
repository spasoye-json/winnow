from fastapi.testclient import TestClient

from winnow.cli import main
from winnow.web import create_app


def test_app_serves_and_runs_loop_without_credentials(tmp_path):
    db_path = tmp_path / "winnow.db"
    main(["init", "--db", str(db_path)])

    app = create_app(str(db_path), str(tmp_path / "missing_secrets.json"))
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
