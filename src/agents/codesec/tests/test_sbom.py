"""Tests for SBOM Generator."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codesec.scanners.sbom import (
    generate_sbom,
    _parse_requirements_txt,
    _parse_package_json,
    _parse_go_mod,
    _run_cyclonedx_py,
    _run_trivy_sbom,
    _generate_fallback_sbom,
)
from codesec.models import SBOM, SbomComponent, SbomFormat, LicenseInfo


class TestParseRequirementsTxt:
    """Test requirements.txt parsing."""

    def test_parse_simple(self):
        content = "flask==2.0.1\nrequests>=2.25.0\n"
        components = _parse_requirements_txt(content)
        assert len(components) == 2
        assert components[0].name == "flask"
        assert components[0].version == "2.0.1"
        assert components[0].purl == "pkg:pypi/flask@2.0.1"

    def test_parse_no_version(self):
        content = "numpy\n"
        components = _parse_requirements_txt(content)
        assert len(components) == 1
        assert components[0].name == "numpy"
        assert components[0].version == "unknown"

    def test_parse_ignores_comments(self):
        content = "# This is a comment\nflask==2.0.1\n"
        components = _parse_requirements_txt(content)
        assert len(components) == 1
        assert components[0].name == "flask"

    def test_parse_empty(self):
        components = _parse_requirements_txt("")
        assert components == []


class TestParsePackageJson:
    """Test package.json parsing."""

    def test_parse_dependencies(self):
        content = json.dumps({
            "dependencies": {"express": "^4.17.1"},
            "devDependencies": {"jest": "~29.0.0"}
        })
        components = _parse_package_json(content)
        assert len(components) == 2
        names = [c.name for c in components]
        assert "express" in names
        assert "jest" in names

    def test_parse_strips_prefix(self):
        content = json.dumps({"dependencies": {"lodash": "^4.17.0"}})
        components = _parse_package_json(content)
        assert components[0].version == "4.17.0"

    def test_parse_invalid_json(self):
        components = _parse_package_json("not json")
        assert components == []


class TestParseGoMod:
    """Test go.mod parsing."""

    def test_parse_require(self):
        content = "module example.com/test\ngo 1.19\nrequire github.com/gin-gonic/gin v1.9.0\n"
        components = _parse_go_mod(content)
        assert len(components) == 1
        assert components[0].name == "github.com/gin-gonic/gin"
        assert components[0].version == "v1.9.0"

    def test_parse_empty(self):
        components = _parse_go_mod("")
        assert components == []


class TestGenerateFallbackSBOM:
    """Test fallback SBOM generation."""

    def test_from_requirements(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text("flask==2.0.1\nrequests==2.25.0\n")

        sbom = _generate_fallback_sbom(repo)
        assert isinstance(sbom, SBOM)
        assert sbom.components_count == 2
        assert sbom.format == SbomFormat.CYCLONE_DX

    def test_from_package_json(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text(json.dumps({"dependencies": {"express": "^4.17.1"}}))

        sbom = _generate_fallback_sbom(repo)
        assert sbom.components_count == 1
        assert sbom.components[0].name == "express"

    def test_empty_repo(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()

        sbom = _generate_fallback_sbom(repo)
        assert sbom.components_count == 0


class TestGenerateSBOM:
    """Test the main SBOM generation function."""

    def test_generate_sbom_python_repo(self, sample_python_repo: Path):
        """Generate SBOM for Python repo."""
        sbom = generate_sbom(sample_python_repo)

        assert isinstance(sbom, SBOM)
        assert sbom.serial_number.startswith("urn:uuid:")
        assert sbom.components_count >= 0

    def test_generate_sbom_empty_repo(self, temp_repo: Path):
        """Generate SBOM for empty repo."""
        sbom = generate_sbom(temp_repo)

        assert isinstance(sbom, SBOM)
        assert sbom.components_count == 0

    @patch("codesec.scanners.sbom._run_cyclonedx_py")
    @patch("codesec.scanners.sbom._run_trivy_sbom")
    def test_generate_sbom_uses_cyclonedx_first(self, mock_trivy, mock_cyclonedx, tmp_path: Path):
        mock_cyclonedx.return_value = SBOM(
            serial_number="urn:uuid:test",
            components_count=5,
            components=[SbomComponent(name="flask", version="2.0.1")],
        )
        mock_trivy.return_value = None

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text("flask==2.0.1\n")

        sbom = generate_sbom(repo)

        mock_cyclonedx.assert_called_once()
        assert sbom.components_count == 5

    @patch("codesec.scanners.sbom._run_cyclonedx_py")
    @patch("codesec.scanners.sbom._run_trivy_sbom")
    def test_generate_sbom_falls_back_to_trivy(self, mock_trivy, mock_cyclonedx, tmp_path: Path):
        mock_cyclonedx.return_value = None  # Failed
        mock_trivy.return_value = SBOM(
            serial_number="urn:uuid:test2",
            components_count=3,
            components=[SbomComponent(name="requests", version="2.25.0")],
        )

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text("requests==2.25.0\n")

        sbom = generate_sbom(repo)

        mock_cyclonedx.assert_called_once()
        mock_trivy.assert_called_once()
        assert sbom.components_count == 3

    @patch("codesec.scanners.sbom._run_cyclonedx_py")
    @patch("codesec.scanners.sbom._run_trivy_sbom")
    def test_generate_sbom_falls_back_to_manifest(self, mock_trivy, mock_cyclonedx, tmp_path: Path):
        mock_cyclonedx.return_value = None
        mock_trivy.return_value = None

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text("flask==2.0.1\n")

        sbom = generate_sbom(repo)

        assert sbom.components_count == 1
        assert sbom.components[0].name == "flask"


class TestSBOMModel:
    """Test SBOM Pydantic model."""

    def test_creation(self):
        sbom = SBOM(
            serial_number="urn:uuid:test",
            components_count=2,
            components=[
                SbomComponent(name="flask", version="2.0.1"),
                SbomComponent(name="requests", version="2.25.0"),
            ],
        )
        assert sbom.serial_number == "urn:uuid:test"
        assert sbom.components_count == 2
        assert sbom.format == SbomFormat.CYCLONE_DX

    def test_with_licenses(self):
        sbom = SBOM(
            serial_number="urn:uuid:test",
            components=[
                SbomComponent(
                    name="flask",
                    version="2.0.1",
                    licenses=[LicenseInfo(id="MIT")],
                )
            ],
        )
        assert sbom.components[0].licenses[0].id == "MIT"

    def test_defaults(self):
        sbom = SBOM(serial_number="urn:uuid:test")
        assert sbom.components_count == 0
        assert sbom.components == []
        assert sbom.version == 1