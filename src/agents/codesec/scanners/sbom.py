
"""
CodeSec SBOM Generator
=========================
Generates Software Bill of Materials in CycloneDX format from package files.

US-1.1.6: As a compliance officer, I want an SBOM so that I can track dependencies.

Technology Decision (ADR):
- Primary: cyclonedx-py — official OWASP CycloneDX tool for Python, supports
  requirements.txt, Pipfile, poetry.lock, and installed environments. OSS,
  Apache-2.0, outputs CycloneDX 1.5 JSON.
- Fallback: Trivy fs --format cyclonedx — multi-ecosystem SBOM generation
  when cyclonedx-py is unavailable or for non-Python repos.
- Not chosen: Syft — excellent but focused on container images rather than
  source manifest files.

"""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from ..config import TOOLS
from ..models import LicenseInfo, SBOM, SbomComponent, SbomFormat
from . import ScannerError, find_files, read_file_safe, run_subprocess

logger = logging.getLogger(__name__)


def _parse_requirements_txt(content: str) -> list[SbomComponent]:
    """Parse requirements.txt into SbomComponent list."""
    components: list[SbomComponent] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Handle formats: package==1.0, package>=1.0, package~=1.0
        match = re.match(r"^([a-zA-Z0-9_.-]+)([<>=~!]+)([a-zA-Z0-9_.-]+)", line)
        if match:
            name, _op, version = match.groups()
            components.append(
                SbomComponent(
                    type="library",
                    name=name,
                    version=version,
                    purl=f"pkg:pypi/{name}@{version}",
                    licenses=[],
                    source_file="requirements.txt",
                )
            )
        else:
            # Package without version specifier
            components.append(
                SbomComponent(
                    type="library",
                    name=line,
                    version="unknown",
                    purl=f"pkg:pypi/{line}",
                    licenses=[],
                    source_file="requirements.txt",
                )
            )
    return components


def _parse_package_json(content: str) -> list[SbomComponent]:
    """Parse package.json into SbomComponent list."""
    components: list[SbomComponent] = []
    try:
        data = json.loads(content)
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        for name, version in deps.items():
            # Strip semver prefixes (^, ~, >=, etc.)
            clean_version = version.lstrip("^~>=<!")
            components.append(
                SbomComponent(
                    type="library",
                    name=name,
                    version=clean_version,
                    purl=f"pkg:npm/{name}@{clean_version}",
                    licenses=[],
                    source_file="package.json",
                )
            )
    except json.JSONDecodeError:
        pass
    return components


def _parse_go_mod(content: str) -> list[SbomComponent]:
    """Parse go.mod into SbomComponent list."""
    components: list[SbomComponent] = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("require "):
            parts = line.replace("require ", "").strip().split()
            if len(parts) >= 2:
                components.append(
                    SbomComponent(
                        type="library",
                        name=parts[0],
                        version=parts[1],
                        purl=f"pkg:golang/{parts[0]}@{parts[1]}",
                        licenses=[],
                        source_file="go.mod",
                    )
                )
    return components


def _run_cyclonedx_py(repo_path: Path) -> SBOM | None:
    """Run cyclonedx-py to generate SBOM from Python environment."""
    tool = TOOLS["cyclonedx"]
    if not tool.enabled:
        return None

    output_path = repo_path / ".codesec_sbom.json"

    # Try poetry.lock first, then requirements.txt
    req_files = find_files(repo_path, patterns=("requirements*.txt", "poetry.lock", "Pipfile"))
    if not req_files:
        return None

    cmd = [
        tool.executable,
        "environment",
        "--json",
        f"--output-file={output_path}",
        str(repo_path),
    ]

    try:
        result = run_subprocess(cmd, cwd=repo_path, timeout=tool.timeout_seconds)
    except ScannerError:
        output_path.unlink(missing_ok=True)
        return None

    if not output_path.exists():
        return None

    try:
        with open(output_path, "r") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        output_path.unlink(missing_ok=True)
        return None
    finally:
        output_path.unlink(missing_ok=True)

    components: list[SbomComponent] = []
    for comp in data.get("components", []):
        licenses = [LicenseInfo(id=lic.get("id"), name=lic.get("name")) for lic in comp.get("licenses", [])]
        components.append(
            SbomComponent(
                type=comp.get("type", "library"),
                name=comp.get("name", "unknown"),
                version=comp.get("version", "unknown"),
                purl=comp.get("purl"),
                licenses=licenses,
                source_file=comp.get("properties", [{}])[0].get("value") if comp.get("properties") else None,
            )
        )

    return SBOM(
        format=SbomFormat.CYCLONE_DX,
        spec_version=data.get("specVersion", "1.5"),
        serial_number=data.get("serialNumber", f"urn:uuid:{uuid.uuid4()}"),
        version=data.get("version", 1),
        components_count=len(components),
        components=components,
    )


def _run_trivy_sbom(repo_path: Path) -> SBOM | None:
    """Run Trivy to generate SBOM as fallback."""
    tool = TOOLS["trivy"]
    if not tool.enabled:
        return None

    output_path = repo_path / ".codesec_trivy_sbom.json"
    cmd = [
        tool.executable,
        "fs",
        "--format=cyclonedx",
        f"--output={output_path}",
        str(repo_path),
    ]

    try:
        result = run_subprocess(cmd, cwd=repo_path, timeout=tool.timeout_seconds)
    except ScannerError:
        output_path.unlink(missing_ok=True)
        return None

    if not output_path.exists():
        return None

    try:
        with open(output_path, "r") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        output_path.unlink(missing_ok=True)
        return None
    finally:
        output_path.unlink(missing_ok=True)

    components: list[SbomComponent] = []
    for comp in data.get("components", []):
        licenses = [LicenseInfo(id=lic.get("license", {}).get("id")) for lic in comp.get("licenses", [])]
        components.append(
            SbomComponent(
                type=comp.get("type", "library"),
                name=comp.get("name", "unknown"),
                version=comp.get("version", "unknown"),
                purl=comp.get("purl"),
                licenses=licenses,
                source_file=None,
            )
        )

    return SBOM(
        format=SbomFormat.CYCLONE_DX,
        spec_version=data.get("specVersion", "1.5"),
        serial_number=data.get("serialNumber", f"urn:uuid:{uuid.uuid4()}"),
        version=data.get("version", 1),
        components_count=len(components),
        components=components,
    )


def _generate_fallback_sbom(repo_path: Path) -> SBOM:
    """Generate SBOM by parsing manifest files directly when no tools are available."""
    components: list[SbomComponent] = []

    # Python requirements.txt
    req_files = find_files(repo_path, patterns=("requirements*.txt",))
    for rf in req_files:
        content = read_file_safe(rf, max_size_mb=1)
        if content:
            components.extend(_parse_requirements_txt(content))

    # package.json
    pkg_files = find_files(repo_path, patterns=("package.json",))
    for pf in pkg_files:
        content = read_file_safe(pf, max_size_mb=1)
        if content:
            components.extend(_parse_package_json(content))

    # go.mod
    go_files = find_files(repo_path, patterns=("go.mod",))
    for gf in go_files:
        content = read_file_safe(gf, max_size_mb=1)
        if content:
            components.extend(_parse_go_mod(content))

    return SBOM(
        format=SbomFormat.CYCLONE_DX,
        spec_version="1.5",
        serial_number=f"urn:uuid:{uuid.uuid4()}",
        version=1,
        components_count=len(components),
        components=components,
    )


def generate_sbom(repo_path: Path) -> SBOM:
    """
    Generate SBOM for a repository.

    Tries cyclonedx-py first, then Trivy, then falls back to manifest parsing.

    Args:
        repo_path: Path to the cloned repository.

    Returns:
        SBOM model with component inventory.
    """
    # Try cyclonedx-py
    try:
        sbom = _run_cyclonedx_py(repo_path)
        if sbom and sbom.components_count > 0:
            logger.info("cyclonedx-py generated SBOM with %d components", sbom.components_count)
            return sbom
    except ScannerError as exc:
        logger.warning("cyclonedx-py failed: %s", exc)

    # Try Trivy
    try:
        sbom = _run_trivy_sbom(repo_path)
        if sbom and sbom.components_count > 0:
            logger.info("Trivy generated SBOM with %d components", sbom.components_count)
            return sbom
    except ScannerError as exc:
        logger.warning("Trivy SBOM failed: %s", exc)

    # Fallback to manifest parsing
    sbom = _generate_fallback_sbom(repo_path)
    logger.info("Fallback SBOM generated with %d components", sbom.components_count)
    return sbom