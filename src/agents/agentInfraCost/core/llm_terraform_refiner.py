"""Feedback-driven artifact refinement via an LLM.

Runs after ``generate_terraform`` when the orchestrator's Gate 2 asked for
changes (``RepoAnalysisInput.user_feedback`` is set): the LLM edits the
rendered ``main.tf`` / ``variables.tf`` / ``outputs.tf`` — and, when a
Dockerfile is supplied, that too — to honor requests like "make it cheaper"
or "use two AZs". Returns validated strings.

Design contract (mirrors ``llm_architecture_advisor`` / ``llm_deployment_advisor``):
the LLM's usable surface is the three Terraform files plus one optional
``dockerfile`` field, strictly Pydantic-validated; and fail-soft — every
failure mode returns the ORIGINAL files unchanged, never a partial write,
so the pipeline proceeds even if the refiner is unavailable. The
architecture (``compute_type``, sizing) is decided elsewhere
(``llm_architecture_advisor``); this module never touches it.

Uses ``core.llm_provider.call_llm`` (OpenRouter), which honours
``OPENROUTER_MODEL``.
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

    The refiner sometimes emits a shell block legitimately spanning several
    lines (e.g. writing a PHP router script). That is NOT valid Dockerfile
    syntax — continuation only happens at ``\\``, so every following line
    parses as its own unknown instruction and the build dies at parse time
    (verified: ``docker buildx build`` fails with "dockerfile parse error ...
    unknown instruction"). Rewrite as the idiomatic heredoc form instead.
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

    On official PHP Alpine images (``php:8.2-cli-alpine`` and friends) the
    ``php82-*`` packages don't exist in the Alpine repo (PHP is compiled into
    the image), so ``apk add`` exits nonzero and the build fails. Two more
    pitfalls are handled so the rewrite actually builds: extensions already
    compiled into the base image must NOT go through ``docker-php-ext-install``
    (it refuses to rebuild a loaded module — verified against
    ``php:8.2-cli-alpine``), and gd/zip/intl need their apk build deps first.
    Unknown/non-``phpNN-`` packages stay on the ``apk add`` line.
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
                    continue
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

    Composer's dist-first install falls back to ``--prefer-source`` on a
    GitHub 429 (fallback injected elsewhere in this file), and that fallback
    clones with git; on ``php:8.2-apache`` the refiner's apt list has no git,
    so the fallback dies with "git was not found in your PATH". Only the
    Debian apt-get path needs this (the alpine branch installs git); the
    caller only invokes this when the repo actually has a composer.json.
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
    """Strip stray ``RUN`` splices inside RUN continuation blocks.

    The refiner sometimes splices ``RUN apk add --no-cache \\<newline>`` into
    the middle of another instruction's continuation, producing either bogus
    apt packages or "/bin/sh: RUN: not found". An instruction keyword can
    never legally appear mid-continuation, so such lines are simply dropped.
    """
    if not dockerfile:
        return dockerfile
    lines = dockerfile.splitlines()
    out: list[str] = []
    in_continuation_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("RUN"):
            if in_continuation_block:
                continue  # stray splice inside previous RUN's continuation
            in_apt_block = stripped.startswith("RUN apt-get")
            if in_apt_block and "apk add --no-cache" in stripped:
                fixed = re.sub(r"RUN apk add --no-cache\s*", "", stripped)
                out.append(line[: len(line) - len(line.lstrip())] + fixed)
                in_continuation_block = fixed.endswith("\\")
            else:
                out.append(line)
                in_continuation_block = stripped.endswith("\\")
            continue
        if in_continuation_block:
            if re.match(r"^RUN\s+\S", stripped):
                continue
            in_continuation_block = stripped.endswith("\\")
        out.append(line)
    return "\n".join(out)


def _fix_detached_install_run(dockerfile: str) -> str:
    """Reattach a RUN prefix to a continuation block that lost its instruction.

    When the refiner drops the leading ``RUN apk add --no-cache \\`` (or
    ``apt-get install``) line, every remaining indented continuation line
    parses as its own unknown Dockerfile instruction and the build dies at
    parse time. When such a block ends up invoking ``docker-php-ext-install``
    / ``pecl install`` / an apt/composer install, the missing instruction is
    reconstructed as ``RUN apk add --no-cache``.
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

    Without ``-t``, the built-in server resolves request paths against the
    CWD, so an actual file under ``public`` (e.g. ``/health.php``) returns
    404 even though the router returns ``false`` for existing files.
    Verified live: the refiner's exact CMD served ``/health.php`` 404 while
    ``php -S ... -t /app/public ...`` served it 200. The router's own
    absolute-path checks keep working regardless of docroot.
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

    The refiner sometimes emits ``-t /app/public`` while the Dockerfile copies
    the frontend build to ``/app/server/public/``; ``php -S`` then dies at
    startup with "Directory /app/public does not exist" (verified live), the
    container never serves and DeployOps rolls back. Rewrite the docroot to
    the directory the frontend-build ``COPY --from`` actually creates.
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

    Repos talking to MySQL through the classic ``mysqli`` API fail at runtime
    with "Class 'mysqli' not found" when only ``pdo_mysql`` was installed —
    and because ``php -S`` returns HTTP 200 with the fatal error body, the
    health probe passes and the deployment "succeeds" while the app is broken
    (verified live on the EC2 instance). ``mysqli`` costs nothing, so append
    it whenever a ``docker-php-ext-install`` list has pdo_mysql but not it.
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

    A runtime image receiving only ``composer.json``/.lock plus the frontend
    build leaves the ``php -S`` router unable to load (PHP fatal error with
    HTTP 200), so the health probe passes while the app is broken (verified
    live on the EC2 instance). Inject a whole-directory
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

    On Alpine-based official PHP images ``localhost`` resolves to IPv6
    ``::1``, but the app server binds IPv4 only, so the probe never connects,
    the container is permanently unhealthy and DeployOps rolls back even
    though the app is up. Verified live on ``php:8.2-cli-alpine``: the exact
    HEALTHCHECK failed against ``localhost`` while ``127.0.0.1`` returned 200.
    """
    if not dockerfile:
        return dockerfile
    return re.sub(
        r"(HEALTHCHECK[\s\S]*?)localhost",
        r"\g<1>127.0.0.1",
        dockerfile,
    )


def _fix_inline_frontend_build(dockerfile: str, repo_context: str | None = None) -> str:
    """Move an inline ``npm build`` out of a PHP image into a Node builder stage.

    Building the React frontend *inside* the PHP runtime image (apt-get
    nodejs npm + npm run build) fails: php:8.2-cli (Debian) ships an old
    Node whose distro npm's old eslint dies
    compiling with "Environment key jest/globals is unknown" (verified live),
    while good runs use a ``node:20-alpine`` builder stage. Rewrite this image
    the same way: strip nodejs/npm from the apt-get install, drop the inline
    build lines, add a ``node:20-alpine AS frontend-builder`` stage, and
    replace the ``cp -r`` copy with ``COPY --from``.

    Only fires when the PHP stage actually builds the frontend inline (has a
    ``npm run build`` and no ``node:`` stage), so an existing Node-builder
    Dockerfile is left untouched.
    """
    if not dockerfile:
        return dockerfile
    if "npm run build" not in dockerfile and "npm ci" not in dockerfile:
        return dockerfile
    has_node_stage = "node:" in dockerfile and "AS frontend-builder" in dockerfile

    # Hallucinated frontend-builder stage for a repo with no front/ dir:
    # drop the whole stage, then fall through to the root-layout strip.
    context_has_front = bool(repo_context) and "front/" in repo_context
    if has_node_stage and not context_has_front and "AS frontend-builder" in dockerfile:
        lines_tmp = dockerfile.splitlines()
        pruned: list[str] = []
        in_builder_stage = False
        for line in lines_tmp:
            s = line.strip()
            if re.match(r"^FROM\s+\S*node:\S+\s+AS\s+frontend-builder", s):
                in_builder_stage = True
                continue
            if in_builder_stage:
                if s.startswith("FROM"):
                    in_builder_stage = False
                else:
                    continue
            pruned.append(line)
        dockerfile = "\n".join(pruned)
        has_node_stage = False

    # Root-layout guard (no front/ dir): strip inline node-build lines from
    # the PHP stage instead of injecting a front/-layout builder stage. The
    # app serves fine without compiled vite assets; keeping them guaranteed
    # a dead build.
    if not has_node_stage and "front/" not in dockerfile:
        out: list[str] = []
        skipping_comment = False
        for line in dockerfile.splitlines():
            s = line.strip()
            if s.startswith("#"):
                low = s.lower()
                skipping_comment = (
                    "node dependencies" in low
                    or "frontend build" in low
                    or ("package.json" in low and "copy" not in low)
                )
                if skipping_comment:
                    continue
            elif re.match(r"^COPY\s+--from=frontend-builder\b", s):
                # Dangling reference: no such stage exists.
                continue
            elif re.match(r"^COPY\s+(--from=\S+\s+)?(package\.json|package-lock\.json|\./?package)", s):
                continue
            elif re.match(r"^RUN\s+npm\s+(ci|install)\b", s):
                continue
            elif re.match(r"^RUN\s+npm\s+run\s+build\b", s):
                continue
            else:
                skipping_comment = False
            out.append(line)
        return "\n".join(out)

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
            continue
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


def _fix_bun_without_lockfile(dockerfile: str, repo_context: str | None) -> str:
    """Rewrite bun invocations to npm/node when the repo has no bun lockfile
    (base images ship no bun binary, so every bun line fails the build)."""
    if not dockerfile or not repo_context:
        return dockerfile
    if "bun" not in dockerfile:
        return dockerfile
    if "bun.lockb" in repo_context or "bun.lock" in repo_context or "bunfig.toml" in repo_context:
        return dockerfile
    dockerfile = re.sub(r"\bbun\s+install\b", "npm install", dockerfile)
    dockerfile = re.sub(r"\bbun\s+run\b", "npm run", dockerfile)
    node = '"node"'
    dockerfile = re.sub(
        r'(?m)^(\s*(?:CMD|ENTRYPOINT)\s*\[?\s*")bun("\s*,)',
        lambda m: m.group(1) + "node" + m.group(2),
        dockerfile,
    )
    dockerfile = re.sub(
        r"(?m)^(\s*(?:CMD|ENTRYPOINT)\s*\[\s*)bun(\s*,)",
        lambda m: m.group(1) + node + m.group(2),
        dockerfile,
    )
    dockerfile = re.sub(r"(?m)^(\s*(?:CMD|ENTRYPOINT)\s+)\bbun\s+", r"\1node ", dockerfile)
    return dockerfile


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
    dockerfile = _fix_bun_without_lockfile(dockerfile, repo_context)
    if "composer.json" in repo_context:
        dockerfile = _ensure_debian_git_for_composer(dockerfile)
    dockerfile = _fix_php_alpine_apk_extensions(dockerfile)
    dockerfile = _fix_missing_mysqli_extension(dockerfile)
    dockerfile = _fix_php_builtin_server_docroot(dockerfile)
    dockerfile = _fix_php_docroot_mismatch(dockerfile)
    dockerfile = _fix_missing_server_source_copy(dockerfile)
    dockerfile = _fix_healthcheck_localhost(dockerfile)
    dockerfile = _fix_inline_frontend_build(dockerfile, repo_context=repo_context)

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
