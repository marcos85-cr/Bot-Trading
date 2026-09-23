import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from guardian import __version__
from guardian.application.trainer import ParameterTrainer
from guardian.infrastructure.settings import Settings
from guardian.main import build_app


@pytest.fixture
def database_path() -> Path:
    path = Path("work") / "test-databases" / f"api-{uuid4().hex}.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    yield path
    for suffix in ("", "-shm", "-wal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def test_health_and_dashboard(database_path: Path):
    # Keep test state isolated while exercising the fully wired HTTP application.
    settings = Settings(_env_file=None, database_path=database_path)
    with TestClient(build_app(settings)) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {
            "status": "ok",
            "mode": "paper",
            "version": __version__,
            "simulation_version": ParameterTrainer.SIMULATION_VERSION,
        }
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "Binance Guardian" in dashboard.text
        assert "Carteras sombra" in dashboard.text
        assert 'id="mainEquityChart"' in dashboard.text
        assert 'id="engineStateText"' in dashboard.text
        assert client.get("/assets/shadow.css").status_code == 200
        assert client.get("/assets/console.css").status_code == 200
        assert client.get("/assets/chart.css").status_code == 200
        assert "connectEventStream" in client.get("/assets/app.js").text
        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["running"] is False
        assert status.json()["shadow"]["enabled"] is True
        assert status.json()["system"]["version"] == __version__
        assert client.get("/api/equity").json() == []
        assert client.get("/api/market?interval=2m").status_code == 400
        assert client.post("/api/shadow/not-a-model/promote").status_code == 400


def test_token_protects_api(database_path: Path):
    settings = Settings(
        _env_file=None,
        database_path=database_path,
        dashboard_token="a" * 32,
    )
    with TestClient(build_app(settings)) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/events/stream").status_code == 401
        response = client.get("/api/status", headers={"Authorization": f"Bearer {'a' * 32}"})
        assert response.status_code == 200


def test_event_stream_announces_readiness_without_waiting(database_path: Path):
    app = build_app(Settings(_env_file=None, database_path=database_path))
    route = next(
        route
        for route in app.routes
        if getattr(route, "path", "") == "/api/events/stream"
    )

    async def first_frame() -> tuple[str, str]:
        response = await route.endpoint()
        frame = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return response.media_type, frame

    media_type, frame = asyncio.run(first_frame())
    assert media_type == "text/event-stream"
    assert 'data: {"stream": "ready"}' in frame
