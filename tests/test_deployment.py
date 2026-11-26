"""Static checks for the container deployment artifacts (no Docker daemon here)."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_builds_the_app_and_persists_data():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["tp-check"]
    assert service["build"] == {"context": ".", "dockerfile": "docker/Dockerfile"}
    assert "8000:8000" in service["ports"]
    assert service["volumes"] == ["tp-data:/data"]
    assert "tp-data" in compose["volumes"]
    assert service["environment"]["TP_DB_PATH"] == "/data/tp.sqlite3"


def test_compose_ships_a_legacy_sample_loader():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    loader = compose["services"]["legacy-sample"]
    assert loader["profiles"] == ["demo"]
    assert loader["build"] == {"context": ".", "dockerfile": "docker/Dockerfile"}
    assert loader["volumes"] == ["tp-data:/data"]
    assert loader["depends_on"] == {"tp-check": {"condition": "service_healthy"}}
    assert loader["restart"] == "no"
    assert "scripts/load_legacy_sample.py" in loader["command"]
    assert "samples/legacy_2026-10.json" in loader["command"]
    assert (ROOT / "samples" / "legacy_2026-10.json").is_file()


def test_dockerfile_installs_requirements_and_runs_uvicorn():
    text = (ROOT / "docker" / "Dockerfile").read_text()
    instructions = [
        line.split()[0]
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and not line.startswith(" ")
    ]
    assert instructions[0] == "FROM"
    assert "COPY requirements.txt" in text
    assert "COPY app ./app" in text
    assert "COPY scripts ./scripts" in text
    assert "COPY samples ./samples" in text
    assert "USER appuser" in text
    assert "EXPOSE 8000" in text
    assert "HEALTHCHECK" in text
    assert '"uvicorn", "app.api:app"' in text
    # The container writes to /data; that directory must exist before dropping root.
    assert "mkdir -p /data" in text


def test_dockerignore_excludes_secrets_and_local_state():
    ignored = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    for entry in (".git", "tests", "data", "*.sqlite3", "requirements-dev.txt"):
        assert entry in ignored
