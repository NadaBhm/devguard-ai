"""Tests for CodeSec's Dockerfile content capture (agent._capture_dockerfile_content).

Downstream agents (InfraCost / DeployOps) need the repo's real Dockerfile to
build the image. The capture must prefer the Dockerfile the stack detector
identified, then fall back to scanning for Dockerfile* variants (the old
exact-match ``rglob("Dockerfile")`` missed files like
``infrastructure/Dockerfile.backend``).
"""

from pathlib import Path

from codesec.agent import _capture_dockerfile_content, _capture_dockerfile_contents
from codesec.models import ContainerInfo, StackDetection


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


def _stack(dockerfile_path: str | None) -> StackDetection:
    return StackDetection(
        primary_language="python",
        frameworks=["fastapi"],
        database="postgresql",
        build_tool="pip",
        confidence=0.9,
        container=ContainerInfo(
            detected=dockerfile_path is not None,
            base_image="python:3.12-slim" if dockerfile_path else None,
            dockerfile_path=dockerfile_path,
        ),
    )


def test_prefers_detected_dockerfile_path(tmp_path: Path):
    repo = _make_repo(tmp_path, {
        "infrastructure/Dockerfile.backend": (
            "FROM python:3.12-slim\nCOPY --from=builder /app /app\n"
        ),
        "frontend/Dockerfile.frontend": "FROM node:20\n",
    })

    content = _capture_dockerfile_content(repo, _stack("infrastructure/Dockerfile.backend"))

    assert content is not None
    assert "COPY --from=builder" in content


def test_fallback_scans_dockerfile_variants(tmp_path: Path):
    repo = _make_repo(tmp_path, {
        "infrastructure/Dockerfile.backend": "FROM python:3.12-slim\nCOPY . /app\n",
    })

    content = _capture_dockerfile_content(repo, _stack(None))

    assert content is not None
    assert "FROM python:3.12-slim" in content


def test_fallback_finds_bare_dockerfile(tmp_path: Path):
    repo = _make_repo(tmp_path, {
        "Dockerfile": "FROM python:3.9\nCMD [\"python\", \"app.py\"]\n",
    })

    content = _capture_dockerfile_content(repo, _stack(None))

    assert content is not None
    assert "python:3.9" in content


def test_no_dockerfile_returns_none(tmp_path: Path):
    repo = _make_repo(tmp_path, {
        "src/main.py": "print('hi')\n",
        "README.md": "# repo\n",
    })

    assert _capture_dockerfile_content(repo, _stack(None)) is None


def test_detected_path_missing_uses_scan(tmp_path: Path):
    repo = _make_repo(tmp_path, {
        "Dockerfile.web": "FROM alpine:3.19\n",
    })

    content = _capture_dockerfile_content(repo, _stack("deleted/Dockerfile"))

    assert content is not None
    assert "alpine:3.19" in content


def _stack_multi() -> StackDetection:
    return StackDetection(
        primary_language="python",
        frameworks=["fastapi"],
        database="postgresql",
        build_tool="pip",
        confidence=0.9,
        containers=[
            ContainerInfo(detected=True, base_image="python:3.12-slim", dockerfile_path="backend/Dockerfile"),
            ContainerInfo(detected=True, base_image="node:20", dockerfile_path="frontend/Dockerfile"),
        ],
    )


def test_multi_container_capture_returns_all(tmp_path: Path):
    repo = _make_repo(tmp_path, {
        "backend/Dockerfile": "FROM python:3.12-slim\nCOPY . /app\n",
        "frontend/Dockerfile": "FROM node:20\nCMD [\"npm\", \"start\"]\n",
    })

    contents = _capture_dockerfile_contents(repo, _stack_multi())

    assert contents == {
        "backend/Dockerfile": "FROM python:3.12-slim\nCOPY . /app\n",
        "frontend/Dockerfile": "FROM node:20\nCMD [\"npm\", \"start\"]\n",
    }


def test_singular_capture_mirrors_primary_of_multi(tmp_path: Path):
    repo = _make_repo(tmp_path, {
        "backend/Dockerfile": "FROM python:3.12-slim\nCOPY . /app\n",
        "frontend/Dockerfile": "FROM node:20\n",
    })

    content = _capture_dockerfile_content(repo, _stack_multi())

    assert content == "FROM python:3.12-slim\nCOPY . /app\n"
