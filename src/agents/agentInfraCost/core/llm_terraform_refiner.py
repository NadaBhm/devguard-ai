"""Feedback-driven artifact refinement via an LLM.

Runs after ``generate_terraform`` (module 3) when the orchestrator's Gate 2
asked for changes (``RepoAnalysisInput.user_feedback`` is set). The LLM edits
the already-rendered ``main.tf`` / ``variables.tf`` / ``outputs.tf`` — and,
when a Dockerfile is supplied, it too — to honor the user's request: e.g.
"make it cheaper", "use two AZs", "swap to Graviton", or "move to
python:3.11-slim and add a healthcheck". Returns the files (and optionally
the refined Dockerfile) as validated strings.

Design contract (mirrors ``llm_architecture_advisor`` / ``llm_deployment_advisor``):

- The LLM's usable surface is the three Terraform files plus one optional
  ``dockerfile`` field — it may not add files, change the architecture
  decision, or invent resources outside the existing file set. Output is
  validated with a strict Pydantic shape, and only files that parse are
  accepted.
- Fail-soft: every failure mode (no ``OPENROUTER_API_KEY``, network error,
  timeout, non-JSON reply, malformed/empty HCL, Pydantic rejection) returns
  the ORIGINAL files unchanged — never an error, never a partial write. The
  pipeline must be able to proceed even if the refiner is unavailable.
- The architecture (``compute_type``, sizing) is decided elsewhere
  (``llm_architecture_advisor``); this module never touches it.

Uses ``core.llm_provider.call_llm`` (OpenRouter), which honours
``OPENROUTER_MODEL`` — set to ``nvidia/nemotron-3-ultra-550b-a55b:free`` in
the default environment.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Final

from core.constants import REFINER_MAX_ATTEMPTS, REFINER_RETRY_DELAY_SECONDS
from core.llm_provider import call_llm
from models.output_schema import TerraformFiles
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

_SYSTEM_INSTRUCTION: Final[str] = (
    "Tu es un ingénieur Terraform et Docker senior. On te donne les trois "
    "fichiers Terraform d'une infrastructure AWS, éventuellement le Dockerfile "
    "associé, et une demande de modification de l'utilisateur. Réécris "
    "UNIQUEMENT les fichiers concernés pour satisfaire la demande, en gardant "
    "tout le reste strictement identique. Réponds uniquement avec un JSON de "
    'la forme '
    '{"main_tf": "...", "variables_tf": "...", "outputs_tf": "...", '
    '"dockerfile": "..." | null}, sans texte autour. Chaque valeur doit être '
    "le contenu complet du fichier, échappé dans la chaîne JSON. Si le "
    "Dockerfile n'est pas fourni en entrée, renvoie null. Si la demande de "
    "l'utilisateur ne concerne pas le Dockerfile (rien sur docker, container, "
    "image, base image, healthcheck), renvoie le Dockerfile fourni EXACTEMENT "
    "tel quel, sans le modifier. Ne modifie ni la "
    "configuration de l'architecture ni les valeurs de dimensionnement déjà "
    "décidées, sauf si la demande le dit explicitement. Le "
    "'=== CONTEXTE DU DÉPÔT ===' est un résumé de faits extraits du code du "
    "dépôt, donné uniquement pour information : il n'autorise jamais à créer "
    "des fichiers supplémentaires ni à sortir des trois fichiers fournis.\n"
    "\n"
    "RÈGLE CRITIQUE POUR LE DOCKERFILE : Si tu génères ou modifies le Dockerfile, "
    "tu DOIS uniquement référencer des fichiers de dépendances (requirements.txt, "
    "pyproject.toml, package.json, Cargo.toml, go.mod, pom.xml, build.gradle, etc.) "
    "qui EXISTENT RÉELLEMENT dans le '=== CONTEXTE DU DÉPÔT ==='. Si le dépôt n'a "
    "pas de requirements.txt ni pyproject.toml, n'utilise JAMAIS `pip install -r "
    "requirements.txt` — installe plutôt les packages détectés explicitement "
    "(ex: `pip install fastapi uvicorn` pour un projet FastAPI). Même chose pour "
    "npm/yarn/cargo/maven/gradle : n'invoque un gestionnaire de paquets qu'avec "
    "un fichier manifeste qui existe dans le contexte.\n"
    "\n"
    "IMPORTANT : Si la demande de l'utilisateur concerne le conteneur, le Dockerfile, "
    "l'image, le port, le healthcheck, ou la commande de démarrage — TU DOIS RENVOYER "
    "UN DOCKERFILE COMPLET ET FONCTIONNEL dans le champ 'dockerfile' du JSON. Ne renvoie "
    "JAMAIS null pour le dockerfile quand le feedback cible le conteneur. Un Dockerfile "
    "incomplet (ex: seulement FROM/COPY sans CMD ni dépendances) sera rejeté.\n"
    "\n"
    "ARBORESCENCE DU DÉPÔT : Le '=== CONTEXTE DU DÉPÔT ===' commence par la "
    "'STRUCTURE COMPLÈTE DU DÉPÔT' (arborescence exacte). Utilise-la pour les "
    "chemins COPY / WORKDIR / context de build. Le contexte de build est la "
    "RACINE du dépôt (le pipeline copie la racine entière et construit), donc "
    "tous les chemins COPY sont relatifs à la racine. Le dépôt peut être un "
    "monorepo : un backend (ex: server/composer.json, server/index.php) et un "
    "frontend (ex: front/package.json) dans des sous-dossiers distincts — dans "
    "ce cas le Dockerfile doit cibler le sous-dossier du backend (ex: "
    "`COPY server/composer.json ./` puis WORKDIR /app, ou `COPY server/ ./`), "
    "jamais référencer composer.json à la racine s'il n'y est pas."
)


class _RefinedTerraform(BaseModel):
    """Strict shape the LLM's raw JSON response must match. Anything else —
    malformed JSON, a missing field, or an empty file — triggers the
    fail-soft fallback (original files unchanged). ``dockerfile`` is optional:
    when present it replaces the current Dockerfile, when absent the original
    is kept (backward-compatible with Terraform-only responses). For
    multi-container refinements the LLM returns ``dockerfiles`` — a dict
    keyed by repo-relative path (e.g. ``backend/Dockerfile``) — instead.
    """

    main_tf: str
    variables_tf: str
    outputs_tf: str
    dockerfile: str | None = None
    dockerfiles: dict[str, str] | None = None


def _docker_blocks(dockerfiles: dict[str, str] | None) -> str:
    """Render the Dockerfile section of the prompt.

    One labelled block per container (``path: Dockerfile``): for a
    single-container repo that's just ``Dockerfile`` (legacy singular
    payloads), for a monorepo it's one block per detected file. Empty dict /
    None renders the explicit "(aucun Dockerfile fourni)" marker so the LLM
    knows not to invent one.
    """
    if not dockerfiles:
        return "=== Dockerfile ===\n(aucun Dockerfile fourni)\n\n"
    blocks = []
    for path, content in dockerfiles.items():
        label = f"{path}:" if path and path != "Dockerfile" else ""
        blocks.append(f"=== Dockerfile {label} ===\n{content}")
    return "\n\n".join(blocks) + "\n\n"


def _build_prompt(
    current: TerraformFiles,
    dockerfiles: dict[str, str] | None,
    feedback: str,
    repo_context: str | None = None,
) -> str:
    repo_block = (
        "=== CONTEXTE DU DÉPÔT ===\n" f"{repo_context}\n\n" if repo_context else ""
    )
    return (
        "=== main.tf ===\n"
        f"{current.main_tf}\n\n"
        "=== variables.tf ===\n"
        f"{current.variables_tf}\n\n"
        "=== outputs.tf ===\n"
        f"{current.outputs_tf}\n\n"
        f"{_docker_blocks(dockerfiles)}"
        f"{repo_block}"
        "=== DEMANDE DE L'UTILISATEUR ===\n"
        f"{feedback}\n\n"
        "Renvoie les trois fichiers (et les Dockerfiles si fournis) modifiés en JSON."
    )


def _parse_llm_output(raw_text: str | None) -> _RefinedTerraform | None:
    if not raw_text:
        return None
    text = raw_text.strip()
    if text.startswith("```"):
        # Free-tier providers sometimes wrap the JSON in a markdown code
        # fence. Strip the opening ```lang marker and the closing ``` before
        # parsing so a valid payload isn't rejected over formatting.
        text = text[3:]
        if text[:4].lower() == "json":
            text = text[4:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        payload = json.loads(text)
        return _RefinedTerraform.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        logger.warning("LLM Terraform refinement failed validation: %s", exc)
        return None


def _valid_file(content: str) -> bool:
    return bool(content and content.strip())


def _heredocify_multiline_echo(dockerfile: str) -> str:
    """Rewrite multi-line ``RUN echo '...' > file`` blocks as heredocs.

    The refiner sometimes emits a shell block that legitimately spans several
    lines, e.g. writing a PHP router script:

        RUN echo '<?php
        $uri = parse_url(...);
        ' > /app/router.php

    That is NOT valid Dockerfile syntax — Dockerfile line continuation only
    happens at ``\\``, so every following line parses as its own unknown
    instruction ("unknown instruction: $uri") and the build dies at parse time
    (verified: ``docker buildx build`` on a refiner-produced Dockerfile fails
    with "dockerfile parse error ... unknown instruction"). Rewrite such
    blocks as an equivalent heredoc, which is the idiomatic multi-line form:

        RUN cat <<'EOF' > /app/router.php
        <?php
        $uri = parse_url(...);
        EOF
    """
    if not dockerfile:
        return dockerfile
    pattern = re.compile(
        r"(?m)^\s*RUN echo '([\s\S]*?)'\s*>\s*(\S+)\s*$"
    )
    return pattern.sub(_heredocify_repl, dockerfile)


def _heredocify_repl(match: re.Match) -> str:
    content = match.group(1)
    dest = match.group(2)
    # Single-line bodies (the common case, e.g. `RUN echo '<?php ... ?>' > /app/public/health.php`)
    # are already valid; leave them untouched so the diff stays minimal.
    if "\n" not in content:
        return match.group(0)
    marker = "DOCKERFILE_EOF"
    while marker in content:
        marker = "_" + marker
    return (
        f"RUN cat <<'{marker}' > {dest}\n"
        f"{content.strip()}\n"
        f"{marker}"
    )


def _fix_php_alpine_apk_extensions(dockerfile: str) -> str:
    """Replace ``apk add php82-*`` with ``docker-php-ext-install``.

    The refiner, when building on an official PHP image (``php:8.2-cli-alpine``
    and friends), emits ``RUN apk add --no-cache php82-pdo_mysql php82-mbstring
    ...``. Those ``php82-*`` packages do not exist in the official image's
    Alpine repo (PHP is already compiled into the image), so ``apk add`` exits
    nonzero and the build fails. The canonical way to add extensions to an
    official PHP image is ``docker-php-ext-install``.

    Two more pitfalls are handled so the rewrite actually builds:
    - Extensions already compiled into the base image (mbstring, curl, json,
      opcache, dom, ...) must NOT be passed to ``docker-php-ext-install`` —
      it refuses to rebuild a module that is already loaded, and the build
      dies (verified against ``php:8.2-cli-alpine``).
    - ``gd`` / ``zip`` / ``intl`` need their Alpine build deps first, so the
      matching ``apk add`` (``libpng-dev libjpeg-turbo-dev`` / ``libzip-dev`` /
      ``icu-dev``) is emitted ahead of the install.
    Unknown/non-``phpNN-`` packages are kept on the ``apk add`` line.
    """
    if not dockerfile:
        return dockerfile
    # Modules already compiled into the official php:8.x-cli-alpine images
    # (from `php -m` on php:8.2-cli-alpine). docker-php-ext-install refuses to
    # rebuild a module that's already loaded, so skip these.
    _PRESENT: frozenset[str] = frozenset({
        "core", "ctype", "curl", "date", "dom", "fileinfo", "filter", "hash",
        "iconv", "json", "libxml", "mbstring", "mysqlnd", "openssl", "pcre",
        "pdo", "pdo_sqlite", "phar", "posix", "random", "readline", "reflection",
        "session", "simplexml", "sodium", "spl", "sqlite3", "standard",
        "tokenizer", "xml", "xmlreader", "xmlwriter", "zend opcache", "opcache",
        "zlib",
    })
    # Extensions that need an apk build dependency before docker-php-ext-install.
    _BUILD_DEPS: dict[str, str] = {
        "gd": "libpng-dev libjpeg-turbo-dev",
        "zip": "libzip-dev",
        "intl": "icu-dev",
    }
    lines = dockerfile.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("RUN apk add"):
            j = i
            joined = stripped
            while lines[j].strip().endswith("\\") and j + 1 < len(lines):
                j += 1
                joined += " " + lines[j].strip().rstrip("\\")
            if not re.search(r"\bphp\d+-", joined):
                # No phpNN-* packages: keep the block untouched, including any
                # trailing &&/|| continuation, so the RUN prefix stays attached
                # (dropping it would leave continuation lines to parse as
                # unknown instructions).
                while j + 1 < len(lines) and re.match(r"^\s*(&&|\|\|)", lines[j + 1]):
                    j += 1
                out.extend(lines[i : j + 1])
                i = j + 1
                continue
            php_exts: list[str] = []
            apk_keep: list[str] = []
            pkg_part = re.split(r"\s+&&\s+|\s+\|\|\s+", joined, maxsplit=1)[0]
            for tok in pkg_part.replace("\\", " ").split():
                if tok in ("RUN", "apk", "add", "--no-cache"):
                    continue
                m = re.match(r"^php\d+-(.+)$", tok)
                if m and m.group(1) not in _PRESENT:
                    php_exts.append(m.group(1))
                elif m:
                    continue  # already present in the base image
                else:
                    apk_keep.append(tok)
            if php_exts:
                build_deps = {
                    ext: dep for ext, dep in _BUILD_DEPS.items() if ext in php_exts
                }
                dep_pkgs = sorted(set(d for d in build_deps.values()))
                if apk_keep or dep_pkgs:
                    deps = " ".join(apk_keep + dep_pkgs)
                    out.append(
                        f"RUN apk add --no-cache {deps} && "
                        f"docker-php-ext-install {' '.join(php_exts)}"
                    )
                else:
                    out.append("RUN docker-php-ext-install " + " ".join(php_exts))
                # Swallow any trailing &&/|| continuation lines of the original
                # block (e.g. ``&& docker-php-ext-install ...``, ``&& rm -rf``);
                # the rewrite above already covers their intent.
                while j + 1 < len(lines) and re.match(r"^\s*(&&|\|\|)", lines[j + 1]):
                    j += 1
                i = j + 1
                continue
            # phpNN packages found but all already present in the base image:
            # keep the block untouched.
            out.extend(lines[i : j + 1])
            i = j + 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _ensure_debian_git_for_composer(dockerfile: str) -> str:
    """Add ``git`` to a Debian ``apt-get install`` when the repo needs composer.

    The dist-first composer install falls back to ``--prefer-source`` on a
    GitHub 429 (see the fallback injected elsewhere in this file), and that
    fallback clones the package with git. On ``php:8.2-apache`` (Debian) the
    refiner's ``apt-get install`` list has no git, so ``composer install
    --prefer-source`` dies with "git was not found in your PATH". ``git`` is
    installed by the alpine branch of the sanitizer (apk list), so only the
    Debian apt-get path needs this. The caller only invokes this when the repo
    actually has a composer.json.
    """
    if not dockerfile:
        return dockerfile
    lines = dockerfile.splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("RUN apt-get") and " install " in stripped:
            if re.search(r"\bgit\b", stripped):
                out.append(line)
                continue
            new_line = re.sub(r"\binstall\s+-y\s+", "install -y git ", stripped, count=1)
            if new_line != stripped:
                out.append(line[: len(line) - len(line.lstrip())] + new_line)
                continue
        out.append(line)
    return "\n".join(out)


def _fix_mixed_apt_apk(dockerfile: str) -> str:
    """Strip a stray ``RUN apk add --no-cache`` sequence inside an apt-get line.

    The refiner occasionally merges its two package-install strategies into one
    line::

        RUN apt-get update && apt-get install -y RUN apk add --no-cache libzip-dev zip unzip && docker-php-ext-install ...

    The embedded ``RUN apk add --no-cache`` tokens then become package names
    for ``apt-get install`` (``RUN``, ``apk``, ``add``, ``--no-cache`` are not
    Debian packages), so the build dies at install time. The whole sequence is
    apk-specific noise on a Debian base; stripping it keeps the real packages
    (``libzip-dev zip unzip``) on the apt-get line.
    """
    if not dockerfile:
        return dockerfile
    lines = dockerfile.splitlines()
    out: list[str] = []
    in_apt_get_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("RUN apt-get"):
            in_apt_get_block = stripped.endswith("\\")
            if "apk add --no-cache" in stripped:
                stripped = re.sub(r"RUN apk add --no-cache\s*", "", stripped)
                out.append(line[: len(line) - len(line.lstrip())] + stripped)
            else:
                out.append(line)
            continue
        if in_apt_get_block:
            # Continuation lines of an apt-get RUN: the refiner sometimes
            # splices a whole ``RUN apk add --no-cache \`` line into them
            # (its two install strategies merged), turning ``RUN``/``apk``/
            # ``add``/``--no-cache`` into bogus apt packages. The preceding
            # line already ends in ``\``, so dropping the stray line keeps
            # the continuation flowing into the real package list.
            if re.match(r"^RUN\s+apk\s+add\s+--no-cache\s*\\?$", stripped):
                continue
            in_apt_get_block = stripped.endswith("\\")
        out.append(line)
    return "\n".join(out)


def _fix_detached_install_run(dockerfile: str) -> str:
    """Reattach a RUN prefix to a continuation block that lost its instruction.

    The refiner sometimes emits a package-install block whose leading
    ``RUN apk add --no-cache \\`` (or ``RUN apt-get install ... \\``) line is
    dropped, leaving only the indented continuation lines::

        # Install system dependencies and PHP extensions
            libzip-dev \
            zip \
            ...
            && docker-php-ext-install mysqli pdo pdo_mysql zip

    Every such line parses as its own unknown Dockerfile instruction and the
    build dies at parse time ("unknown instruction: libzip-dev"). When a
    non-instruction line starts a block that ends up invoking
    ``docker-php-ext-install`` / ``pecl install`` / an apt/composer install,
    the missing instruction is reconstructed as ``RUN apk add --no-cache``.
    """
    if not dockerfile:
        return dockerfile
    _INSTRUCTIONS: frozenset[str] = frozenset({
        "from", "run", "copy", "add", "cmd", "entrypoint", "env", "expose",
        "workdir", "user", "arg", "volume", "label", "healthcheck",
        "stopsignal", "shell", "maintainer", "onbuild",
    })
    lines = dockerfile.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        is_instruction = (
            not stripped
            or stripped.startswith("#")
            or stripped.split()[0].lower() in _INSTRUCTIONS
        )
        if is_instruction or not stripped.endswith("\\"):
            out.append(lines[i])
            i += 1
            continue
        j = i
        joined = stripped
        while lines[j].strip().endswith("\\") and j + 1 < len(lines):
            j += 1
            joined += " " + lines[j].strip().rstrip("\\")
        while j + 1 < len(lines) and re.match(r"^\s*(&&|\|\|)", lines[j + 1]):
            j += 1
            joined += " " + lines[j].strip()
        if re.search(r"docker-php-ext-install|pecl install|apt-get|apt install|composer install", joined):
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            out.append(f"{indent}RUN apk add --no-cache \\")
            out.extend(lines[i : j + 1])
            i = j + 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _fix_php_builtin_server_docroot(dockerfile: str) -> str:
    """Add a docroot to a PHP built-in server ``CMD`` that uses a router script.

    The refiner emits ``php -S 0.0.0.0:8000 /app/router.php`` for a final image
    that serves the app's static/health files from a ``public`` dir. Without a
    ``-t`` docroot, the built-in server resolves the request path against the
    CWD — so ``/health.php`` (an actual file under ``/app/public``) returns 404
    even though the router script explicitly returns ``false`` for existing
    files: ``return false`` delegates back to the server's static-file serving,
    which then looks in the wrong directory. Verified live: the refiner's
    exact CMD served ``/health.php`` 404 while ``php -S ... -t /app/public
    /app/router.php`` served it 200. The router's own absolute-path checks
    (``__DIR__ . "/public" . $uri``) keep working regardless of docroot.
    """
    if not dockerfile:
        return dockerfile
    pattern = re.compile(
        r"CMD\s+\[\"php\",\s*\"-S\",\s*\"[^\"]+\",\s*(?!-\"t\",\s*\")\"([^\"]+)\"\]"
    )
    def _repl(match: re.Match) -> str:
        router_path = match.group(1)
        public_dir = str(Path(router_path).parent / "public")
        return (
            f'CMD ["php", "-S", "0.0.0.0:8000", "-t", "{public_dir}", '
            f'"{router_path}"]'
        )
    return pattern.sub(_repl, dockerfile)


def _fix_php_docroot_mismatch(dockerfile: str) -> str:
    """Point a PHP built-in server ``-t`` docroot at the real frontend dir.

    The refiner sometimes emits ``CMD ["php", "-S", ..., "-t", "/app/public",
    "/app/server/index.php"]`` while the Dockerfile copies the frontend build
    to ``/app/server/public/``. ``php -S`` then dies at startup with "Directory
    /app/public does not exist" (verified live), so the container never serves
    and DeployOps rolls back. Rewrite the docroot to the directory the
    frontend-build ``COPY --from`` actually creates.
    """
    if not dockerfile:
        return dockerfile
    build_dest = re.search(
        r"COPY\s+--from=frontend-builder\s+[^\s]+\s+(/\S*public/?)\s*",
        dockerfile,
    )
    if not build_dest:
        return dockerfile
    dest = build_dest.group(1).rstrip("/")
    def _repl(match: re.Match) -> str:
        return match.group(1) + f'"{dest}"'

    return re.sub(
        r'(CMD\s+\["php",\s*"-S",\s*"[^"]+",\s*"-t",\s*)("[^"]+")(?=,\s*"[^"]+"\])',
        _repl,
        dockerfile,
    )


def _fix_missing_mysqli_extension(dockerfile: str) -> str:
    """Add ``mysqli`` to ``docker-php-ext-install`` when the app needs MySQL.

    The refiner typically emits ``docker-php-ext-install pdo_mysql ...`` on an
    official PHP image. Repos that talk to MySQL through the classic ``mysqli``
    API (``new mysqli(...)``) then fail at runtime with "Class 'mysqli' not
    found" on the first request — and because ``php -S`` returns HTTP 200 with
    the fatal error body, the health probe passes and the deployment
    "succeeds" while the app is broken (verified live on the EC2 instance).
    ``mysqli`` is the standard companion of ``pdo_mysql`` and costs nothing, so
    it is appended whenever a ``docker-php-ext-install`` list has ``pdo_mysql``
    but not ``mysqli``.
    """
    if not dockerfile:
        return dockerfile
    lines = dockerfile.splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if (
            "RUN " in line
            and "docker-php-ext-install" in stripped
            and re.search(r"\bpdo_mysql\b", stripped)
            and not re.search(r"\bmysqli\b", stripped)
        ):
            stripped = re.sub(
                r"(?<!\w)pdo_mysql(?!\w)",
                "pdo_mysql mysqli",
                stripped,
                count=1,
            )
            out.append(line[: len(line) - len(line.lstrip())] + stripped)
        else:
            out.append(line)
    return "\n".join(out)


def _fix_missing_server_source_copy(dockerfile: str) -> str:
    """Copy the ``server/`` source when the refiner only copies composer files.

    The refiner sometimes emits a Dockerfile whose runtime image receives only
    ``COPY server/composer.json server/composer.lock* ./server/`` plus the
    frontend build (``COPY --from=frontend-builder ... /app/server/public/``)
    but never the ``server/`` sources themselves. The ``php -S`` router then
    fails to load (``Failed opening required '/app/server/index.php'``) and
    every request returns a PHP fatal error with HTTP 200 — so the health
    probe passes and the deployment "succeeds" while the app is broken
    (verified live on the EC2 instance). Inject a whole-directory
    ``COPY server/ /app/server/`` when the router path lives under
    ``/app/server/`` and no full ``server/`` copy exists.
    """
    if not dockerfile:
        return dockerfile
    if "COPY server/ /app/server/" in dockerfile:
        return dockerfile
    router = re.search(
        r'CMD\s+\["php",\s*"-S",\s*"[^"]+",\s*"-t",\s*"[^"]+",\s*"/app/server/[^"]+"',
        dockerfile,
    )
    if not router:
        return dockerfile
    injection = "\nCOPY server/ /app/server/\n"
    composer_copy = re.search(r"(COPY\s+server/composer\.json[^\n]+\n)", dockerfile)
    if composer_copy:
        at = composer_copy.end(1)
        return dockerfile[:at] + injection + dockerfile[at:]
    workdir = re.search(r"(WORKDIR /app\n)", dockerfile)
    if workdir:
        at = workdir.end(1)
        return dockerfile[:at] + injection + dockerfile[at:]
    return dockerfile


def _fix_healthcheck_localhost(dockerfile: str) -> str:
    """Replace ``localhost`` with ``127.0.0.1`` in HEALTHCHECK commands.

    The refiner writes the image HEALTHCHECK against ``localhost`` (e.g.
    ``wget --spider http://localhost:8000/health.php``). On the Alpine-based
    official PHP images ``localhost`` resolves to IPv6 ``::1``, but the app
    server binds IPv4 only (``php -S 0.0.0.0:8000``), so the probe never
    connects and the container is permanently unhealthy — DeployOps then
    rolls back even though the app is actually up. Verified live on
    ``php:8.2-cli-alpine``: the exact HEALTHCHECK failed against
    ``localhost`` while ``127.0.0.1`` connected and returned 200.
    """
    if not dockerfile:
        return dockerfile
    return re.sub(
        r"(HEALTHCHECK[\s\S]*?)localhost",
        r"\g<1>127.0.0.1",
        dockerfile,
    )


def _fix_inline_frontend_build(dockerfile: str) -> str:
    """Move an inline ``npm build`` out of a PHP image into a Node builder stage.

    The refiner sometimes builds the React frontend *inside* the PHP runtime
    image by ``apt-get install nodejs npm`` + ``npm run build``. On the official
    ``php:8.2-cli`` (Debian) image that ships Node 18, while the repo's
    ``react-scripts`` (this one pins 5.x) needs Node >= 18 and, more
    importantly, the old ``eslint`` pulled in by the distro npm fails to
    compile the app ("Environment key jest/globals is unknown") — verified
    live: the exact refiner Dockerfile fails ``npm run build`` this way. The
    refiner's good runs instead use a ``node:20-alpine`` builder stage. Rewrite
    this image the same way:
    - strip ``nodejs``/``npm`` out of the PHP image's ``apt-get install``;
    - drop the inline ``npm install`` / ``npm run build`` / ``COPY front`` lines
      that ran in the PHP stage;
    - add a ``node:20-alpine AS frontend-builder`` stage that does the build;
    - replace the ``cp -r /app/front/build ...`` copy with ``COPY --from``.

    Only fires when the PHP stage actually builds the frontend inline (has a
    ``npm run build`` and no ``node:`` stage exists), so a Dockerfile that
    already uses a Node builder is left untouched.
    """
    if not dockerfile:
        return dockerfile
    if "npm run build" not in dockerfile and "npm ci" not in dockerfile:
        return dockerfile
    has_node_stage = "node:" in dockerfile and "AS frontend-builder" in dockerfile
    lines = dockerfile.splitlines()
    new_lines: list[str] = []
    inserted = has_node_stage
    in_inline_build = False
    in_php_stage = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("FROM php"):
            in_php_stage = True
            if not inserted:
                new_lines.extend([
                    "# Build the React frontend in a dedicated Node stage so the PHP",
                    "# image never needs apt-installed nodejs (too old for modern",
                    "# react-scripts; npm run build fails: 'Environment key jest/globals",
                    "# is unknown'). The refiner's correct runs already use this shape.",
                    "FROM node:20-alpine AS frontend-builder",
                    "WORKDIR /app/front",
                    "COPY front/package*.json ./",
                    "RUN npm ci",
                    "COPY front/ ./",
                    "RUN npm run build",
                    "",
                ])
                inserted = True
        # Detect the start of the inline frontend build inside the PHP stage.
        # Two shapes occur in the wild: `RUN cd /app/front && npm ...` and
        # `WORKDIR /app/front` + `COPY front/... ./` + `RUN npm run build`.
        if in_php_stage and (
            s.startswith("RUN cd /app/front")
            or s == "WORKDIR /app/front"
            or s.startswith("COPY front/")
            or s.startswith("npm run build")
        ):
            in_inline_build = True
            continue
        if in_inline_build:
            # Consume every line of the inline build until the copy-out line
            # (which references the build output and a public dir) or a real
            # stage/section boundary. Comments and blank lines inside the block
            # are part of the block.
            if "cp -r" in s and "build" in s and "public" in s:
                new_lines.append(
                    "COPY --from=frontend-builder /app/front/build/ /app/server/public/"
                )
                in_inline_build = False
                continue
            if s.startswith(("FROM", "CMD", "EXPOSE", "ENV", "RUN echo")):
                in_inline_build = False
                new_lines.append(line)
                continue
            continue  # consume the inline build line
        new_lines.append(line)
    return "\n".join(_strip_apt_nodejs_block(new_lines))


def _strip_apt_nodejs_block(lines: list[str]) -> list[str]:
    """Drop ``nodejs``/``npm`` tokens from an ``apt-get install`` block that may
    span backslash-continuation lines."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("RUN apt-get") and " install " in s:
            j = i
            block = [lines[i]]
            while lines[j].strip().endswith("\\") and j + 1 < len(lines):
                j += 1
                block.append(lines[j])
            joined = " ".join(
                ln.strip().rstrip("\\").strip() for ln in block
            )
            for pkg in ("nodejs", "npm"):
                joined = re.sub(rf"\s+{re.escape(pkg)}(?=\s|$)", "", joined)
            out.append(joined)
            i = j + 1
            continue
        out.append(lines[i])
        i += 1
    return out


def _fix_dev_mode_cmd(dockerfile: str) -> str:
    """Rewrite dev-mode CMDs to production equivalents (fail-soft).

    Maps npm run dev -> npm start, uvicorn --reload -> uvicorn, flask run ->
    gunicorn, ng serve -> ng serve --configuration production. Only CMD/ENTRYPOINT.
    """
    if not dockerfile:
        return dockerfile
    lines = dockerfile.splitlines()
    out: list[str] = []
    changed = False
    for line in lines:
        stripped = line.strip()
        if not (stripped.startswith("CMD") or stripped.startswith("ENTRYPOINT")):
            out.append(line)
            continue
        # Normalize JSON-array form tokens so the dev-mode patterns match both
        # `CMD ["npm", "run", "dev"]` and `CMD npm run dev`. A match on the
        # normalized argument rebuilds the line in shell form; a non-match
        # keeps the original line byte-for-byte.
        prefix = "CMD" if stripped.startswith("CMD") else "ENTRYPOINT"
        arg_text = stripped[len(prefix):]
        normalized = arg_text
        if re.search(r'^\s*\[', normalized) and "]" in normalized:
            normalized = re.sub(r"[\"']", "", normalized)
            normalized = normalized.replace(",", " ").replace("[", " ").replace("]", " ")
        normalized = " ".join(normalized.split())
        if re.search(r"\bnpm\s+(run\s+)?dev\b", normalized):
            replacement = "npm run start" if re.search(r"npm run start|npm start", stripped) else "npm start"
            normalized = re.sub(r"\bnpm\s+(run\s+)?dev\b", replacement, normalized)
            line = f"{prefix} {normalized}"
            changed = True
        elif re.search(r"\buvicorn\b.*\b--reload\b", normalized) or re.search(r"\buvicorn\b.*--reload", normalized):
            # Strip --reload and --reload-dir <path> in both shell form
            # ("uvicorn ... --reload") and JSON-array form ("--reload"]).
            normalized = re.sub(r"--reload-dir\s+\S+", "", normalized)
            normalized = re.sub(r"--reload", "", normalized)
            line = f"{prefix} {normalized}"
            changed = True
        elif re.search(r"\bflask\s+run\b", normalized):
            normalized = re.sub(r"\bflask\s+run\b", "gunicorn app:app", normalized)
            line = f"{prefix} {normalized}"
            changed = True
        elif re.search(r"\bng\s+serve\b", normalized):
            normalized = re.sub(r"\bng\s+serve\b", "ng serve --configuration production", normalized)
            line = f"{prefix} {normalized}"
            changed = True
        out.append(line)
    # Fail-soft and diff-minimal: no dev-mode CMD -> return the Dockerfile
    # byte-for-byte (other fixers here behave the same way, and the pipeline
    # applies this to every image's Dockerfile on every render).
    if not changed:
        return dockerfile
    return "\n".join(out)


def _sanitize_dockerfile_dependencies(dockerfile: str, repo_context: str | None) -> str:
    """Rewrite dependency installs that reference files absent from the repo context.

    Also hardens the package-manager invocations so the resulting Dockerfile
    builds on the DeployOps host:
    - Composer 2.3+ *blocks* ``composer install`` when any required package is
      affected by a security advisory (e.g. ``firebase/php-jwt``), aborting the
      build with exit 2 unless ``--no-blocking`` is passed. The refiner reliably
      omits that flag, so it is injected here when the repo has a composer.json.
    """
    if not dockerfile or not repo_context:
        return dockerfile

    dockerfile = _fix_dev_mode_cmd(dockerfile)
    dockerfile = _heredocify_multiline_echo(dockerfile)
    dockerfile = _fix_detached_install_run(dockerfile)
    dockerfile = _fix_mixed_apt_apk(dockerfile)
    if "composer.json" in repo_context:
        dockerfile = _ensure_debian_git_for_composer(dockerfile)
    dockerfile = _fix_php_alpine_apk_extensions(dockerfile)
    dockerfile = _fix_missing_mysqli_extension(dockerfile)
    dockerfile = _fix_php_builtin_server_docroot(dockerfile)
    dockerfile = _fix_php_docroot_mismatch(dockerfile)
    dockerfile = _fix_missing_server_source_copy(dockerfile)
    dockerfile = _fix_healthcheck_localhost(dockerfile)
    dockerfile = _fix_inline_frontend_build(dockerfile)

    dep_files = {
        "requirements.txt": ("pip install", ["fastapi", "uvicorn"]),
        "pyproject.toml": ("pip install", ["fastapi", "uvicorn"]),
        "package.json": ("npm install", []),
        "Cargo.toml": ("cargo build", []),
        "go.mod": ("go build", []),
        "pom.xml": ("mvn package", []),
        "build.gradle": ("./gradlew build", []),
    }

    missing_deps = []
    for fname in dep_files:
        if fname not in repo_context:
            missing_deps.append(fname)

    lines = dockerfile.splitlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        # Skip RUN lines that install from missing dependency files
        if stripped.startswith("RUN") and any(
            f" -r {fname}" in stripped or f" install {fname}" in stripped or f" install -r {fname}" in stripped
            for fname in missing_deps
        ):
            if "fastapi" in repo_context.lower() or "uvicorn" in repo_context.lower():
                new_lines.append("RUN pip install --no-cache-dir uvicorn fastapi")
            elif "express" in repo_context.lower() or "node" in repo_context.lower():
                new_lines.append("RUN npm ci --omit=dev")
            else:
                new_lines.append(line)  # keep original if unsure
            continue
        # Skip COPY lines for missing dependency files
        if stripped.startswith("COPY") and any(f" {fname} " in stripped or stripped.endswith(f" {fname}") for fname in missing_deps):
            continue
        # Composer 2.3+ blocks on security advisories by default; the refiner
        # omits --no-blocking and the build dies with exit 2. Inject it when
        # the repo actually has a composer manifest.
        if (
            "composer.json" in repo_context
            and stripped.startswith("RUN")
            and " composer install " in stripped
            and "--no-blocking" not in stripped
        ):
            stripped = stripped.replace("--no-interaction", "--no-interaction --no-blocking", 1)
            if "--no-blocking" not in stripped:
                stripped = stripped.rstrip() + " --no-blocking"
            line = stripped
        # GitHub rate-limits unauthenticated zipball downloads (codeload /
        # api.github.com) per IP; a lockfile that pins dist to GitHub URLs can
        # make composer install die with "Failed to download ... (HTTP/2 429)"
        # even though the same package is fetchable via git. Composer's default
        # preferred-install is dist, and it does NOT fall back to source on a
        # 429. Rewrite the install into a dist-then-source fallback so the
        # build survives transient rate limits (git protocol isn't API-limited).
        if (
            "composer.json" in repo_context
            and stripped.startswith("RUN")
            and " composer install " in stripped
            and "--prefer-source" not in stripped
        ):
            base = stripped.replace("\\\n", " ").rstrip()
            if "||" not in base:
                cmd = base[len("RUN "):] if base.startswith("RUN ") else base
                line = f"RUN {cmd} || {cmd} --prefer-source"
        new_lines.append(line)

    return "\n".join(new_lines)


# Feedback only rewrites the Dockerfile when it explicitly targets container
# concerns. The repo's real Dockerfile (captured by CodeSec) is otherwise
# preserved verbatim — a free-tier LLM happily rewrites an unknown repo's
# Dockerfile into something generic and broken when asked only to "make it
# cheaper" (that bug ships a Dockerfile whose build context can't satisfy it).
_DOCKER_FEEDBACK_TERMS: Final[tuple[str, ...]] = (
    "docker",
    "container",
    "image",
    "base image",
    "base_image",
    "from ",
    "dockerfile",
    "healthcheck",
)


def _feedback_targets_dockerfile(feedback: str) -> bool:
    """True when the user's request plausibly concerns the Dockerfile.

    Conservative keyword scan, not NLP: a mention of any container term means
    the LLM may rewrite the Dockerfile; anything else (cost, AZs, region,
    instance type, "regenerate") leaves it untouched.
    """
    lower = (feedback or "").lower()
    return any(term in lower for term in _DOCKER_FEEDBACK_TERMS)


def _refine_dockerfiles(
    dockerfiles: dict[str, str] | None,
    refined: _RefinedTerraform,
    feedback: str,
    repo_context: str | None,
    force_dockerfile: bool,
) -> dict[str, str] | None:
    """Apply the LLM's Dockerfile edits (plural mode) with per-file
    sanitization.

    Each Dockerfile in the refined output is validated and run through
    ``_sanitize_dockerfile_dependencies`` exactly like the singular path.
    Anything invalid keeps that file's original content (fail-soft, per
    file). Returns the updated path -> content map, or ``None`` when no
    Dockerfiles were supplied in the first place.
    """
    if not dockerfiles:
        return None
    if not (force_dockerfile or _feedback_targets_dockerfile(feedback)):
        logger.info(
            "Feedback doesn't target Dockerfiles; keeping the repo's "
            "Dockerfiles untouched"
        )
        return dockerfiles

    updated: dict[str, str] = {}
    for path, original in dockerfiles.items():
        candidate = (refined.dockerfiles or {}).get(path)
        if candidate is None or not _valid_file(candidate):
            logger.warning(
                "Artifact refiner returned no usable Dockerfile for %s; keeping original",
                path,
            )
            updated[path] = original
        else:
            updated[path] = _sanitize_dockerfile_dependencies(candidate, repo_context)
    return updated


def refine_terraform(
    current: TerraformFiles,
    feedback: str,
    dockerfile: str | None = None,
    dockerfiles: dict[str, str] | None = None,
    repo_context: str | None = None,
    force_dockerfile: bool = False,
) -> tuple[TerraformFiles, str | None] | tuple[TerraformFiles, dict[str, str] | None]:
    """Refine the rendered artifacts from a user prompt.

    Args:
        current: the Terraform files produced by ``generate_terraform``.
        feedback: the user's free-form change request from Gate 2 (or the
            pipeline's first-try "make it match the repo" instruction).
        dockerfile: the effective Dockerfile content (if this is a container
            deployment), refined alongside the Terraform when the LLM edits
            it. Legacy singular mode — return type matches (``str``).
        dockerfiles: multi-container mode — a dict of repo-relative path to
            Dockerfile content, one entry per detected container. When set,
            the return type is the path -> content map instead. Mutually
            exclusive with ``dockerfile``.
        repo_context: the whole-repo digest (``core.repo_ingestor``) so the
            LLM can honor the request against the real code, not just the
            rendered artifacts.
        force_dockerfile: when True, always let the LLM rewrite the Dockerfile
            regardless of whether the feedback mentions container terms. Used
            on the FIRST try, where the Dockerfile is a bare deterministic
            stub (``FROM python:3.12-slim COPY . /app``) that cannot run the
            app — the whole point is to replace it with a real one.

    Returns:
        A ``(TerraformFiles, dockerfile)`` pair honoring the request, or — on
        any LLM failure or invalid output — ``(current, dockerfile)``
        unchanged (fail-soft, same contract as every other LLM call here).
    """
    # Plural mode: every caller passing dockerfiles gets a dict back.
    plural = dockerfiles is not None
    prompt_files = dict(dockerfiles) if dockerfiles else (
        {"Dockerfile": dockerfile} if dockerfile is not None else None
    )

    for attempt in range(1, REFINER_MAX_ATTEMPTS + 1):
        raw_text = call_llm(
            prompt=_build_prompt(current, prompt_files, feedback, repo_context=repo_context),
            system_instruction=_SYSTEM_INSTRUCTION,
        )
        refined = _parse_llm_output(raw_text)
        if (
            refined is not None
            and _valid_file(refined.main_tf)
            and _valid_file(refined.variables_tf)
            and _valid_file(refined.outputs_tf)
        ):
            if plural:
                refined_dockerfiles = _refine_dockerfiles(
                    dockerfiles, refined, feedback, repo_context, force_dockerfile
                )
            else:
                # Dockerfile: refine only when one exists, the feedback explicitly
                # targets container concerns (or force_dockerfile is set — first
                # try), AND the LLM returned a valid replacement. Anything else
                # keeps the original Dockerfile untouched. Backward-compatible:
                # Terraform-only responses keep the original Dockerfile too.
                refined_dockerfile = dockerfile
                if dockerfile is not None and (force_dockerfile or _feedback_targets_dockerfile(feedback)):
                    if refined.dockerfile is None or not _valid_file(refined.dockerfile):
                        logger.warning(
                            "Artifact refiner returned no usable Dockerfile; keeping original"
                        )
                    else:
                        refined_dockerfile = refined.dockerfile
                        refined_dockerfile = _sanitize_dockerfile_dependencies(
                            refined_dockerfile, repo_context
                        )
                elif dockerfile is not None:
                    logger.info(
                        "Feedback doesn't target the Dockerfile; keeping the repo's "
                        "Dockerfile untouched"
                    )

            logger.info("Artifact refiner applied user feedback")
            files = TerraformFiles(
                main_tf=refined.main_tf,
                variables_tf=refined.variables_tf,
                outputs_tf=refined.outputs_tf,
            )
            if plural:
                return files, refined_dockerfiles
            return files, refined_dockerfile

        if attempt < REFINER_MAX_ATTEMPTS:
            logger.warning(
                "Artifact refiner attempt %d/%d produced unusable output; retrying",
                attempt, REFINER_MAX_ATTEMPTS,
            )
            time.sleep(REFINER_RETRY_DELAY_SECONDS)

    logger.info("Artifact refiner unavailable or invalid; keeping original files")
    if plural:
        return current, dockerfiles
    return current, dockerfile
