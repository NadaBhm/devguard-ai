"""CodeSec Test Fixtures
======================
Shared pytest fixtures for all CodeSec scanner tests.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_repo():
    """Create a temporary repository directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_python_repo(temp_repo: Path) -> Path:
    """Create a sample Python FastAPI repository structure."""
    # requirements.txt
    (temp_repo / "requirements.txt").write_text(
        "fastapi==0.110.0\nsqlalchemy==2.0.0\npsycopg2-binary==2.9.9\nrequests==2.25.0\n"
    )

    # main.py
    main_py_content = """
from fastapi import FastAPI
from sqlalchemy import create_engine, text

app = FastAPI()
engine = create_engine("postgresql://user:pass@localhost/db")

@app.get("/users/{user_id}")
def get_user(user_id: str):
    query = f"SELECT * FROM users WHERE id = {user_id}"
    with engine.connect() as conn:
        result = conn.execute(text(query))
    return {"user": result.fetchone()}
"""
    (temp_repo / "main.py").write_text(main_py_content)

    # db.py with SQL injection
    (temp_repo / "app").mkdir()
    db_py_content = """
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost/db")

def get_user_raw(user_id):
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return query
"""
    (temp_repo / "app" / "db.py").write_text(db_py_content)

    # .env with fake secret
    env_content = """
DATABASE_URL=postgresql://user:pass@localhost/db
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
"""
    (temp_repo / ".env").write_text(env_content)

    # Dockerfile
    dockerfile_content = """
FROM python:latest
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 8000
USER root
CMD ["uvicorn", "main:app", "--host", "0.0.0.0"]
"""
    (temp_repo / "Dockerfile").write_text(dockerfile_content)

    # docker-compose.yml
    compose_content = """
version: '3.8'
services:
  web:
    build: .
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql://db/postgres
  db:
    image: postgres:15
"""
    (temp_repo / "docker-compose.yml").write_text(compose_content)

    return temp_repo


@pytest.fixture
def sample_node_repo(temp_repo: Path) -> Path:
    """Create a sample Node.js/Express repository structure."""
    (temp_repo / "package.json").write_text(json.dumps({
        "name": "sample-api",
        "version": "1.0.0",
        "dependencies": {
            "express": "^4.18.0",
            "mongoose": "^7.0.0",
            "lodash": "^4.17.0"
        }
    }))

    server_js_content = """
const express = require('express');
const mongoose = require('mongoose');
const app = express();

mongoose.connect('mongodb://localhost/db');

app.get('/user/:id', (req, res) => {
    const query = "SELECT * FROM users WHERE id = " + req.params.id;
    res.send(query);
});

app.listen(3000);
"""
    (temp_repo / "server.js").write_text(server_js_content)

    df_content = """
FROM node:18-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
EXPOSE 3000
USER node
CMD ["node", "server.js"]
"""
    (temp_repo / "Dockerfile").write_text(df_content)

    return temp_repo


@pytest.fixture
def mock_codesec_result():
    """Return a mock CodeSecResult dictionary for testing."""
    return {
        "job_id": "550e8400-e29b-41d4-a716-446655440000",
        "status": "completed",
        "repo_url": "https://github.com/owner/repo-name",
        "stack_detection": {
            "primary_language": "python",
            "frameworks": ["fastapi", "sqlalchemy"],
            "database": "postgresql",
            "build_tool": "pip",
            "container": {"detected": True, "base_image": "python:3.12-slim"},
            "confidence": 0.92,
        },
        "sast_findings": [
            {
                "rule_id": "python.sql-injection",
                "tool": "semgrep",
                "severity": "critical",
                "category": "owasp-top10",
                "file": "app/db.py",
                "line": 24,
                "message": "Possible SQL injection",
            }
        ],
        "secrets": [
            {
                "type": "aws_access_key_id",
                "tool": "gitleaks",
                "file": ".env",
                "line": 3,
                "value_preview": "AKIA...MPLE",
            }
        ],
        "dependencies": {
            "total_packages": 42,
            "vulnerable_packages": [
                {
                    "package": "requests",
                    "installed_version": "2.25.0",
                    "cve_id": "CVE-2023-32681",
                    "severity": "high",
                }
            ],
        },
        "dockerfile_findings": [
            {
                "rule_id": "DS001",
                "tool": "trivy",
                "severity": "high",
                "file": "Dockerfile",
                "line": 5,
                "message": "Running as root user",
            }
        ],
        "sbom": {
            "format": "CycloneDX",
            "spec_version": "1.5",
            "serial_number": "urn:uuid:test",
            "components_count": 42,
        },
        "security_score": {
            "score": 68,
            "grade": "C",
            "breakdown": {
                "sast": 20,
                "secrets": 15,
                "dependencies": 20,
                "dockerfile": 8,
                "sbom": 10,
                "stack_detection": 7,
            },
            "severity_counts": {
                "critical": 1,
                "high": 4,
                "medium": 5,
                "low": 2,
                "info": 0,
            },
            "recommendations": [
                "Fix 1 critical SQL injection in app/db.py:24",
            ],
        },
    }