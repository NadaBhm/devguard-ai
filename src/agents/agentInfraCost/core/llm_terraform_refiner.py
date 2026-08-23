"""Feedback-driven artifact refinement via an LLM.

After Gate 2 asks for changes, the LLM rewrites the rendered Terraform files —
plus a supplied Dockerfile — to honor the request. Contract mirrors the other
LLM advisors: Pydantic-validated surface, fail-soft (any failure keeps the
ORIGINAL files; architecture/sizing is never touched here). Uses
``core.llm_provider.call_llm`` (OpenRouter, honours ``OPENROUTER_MODEL``).
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
    """Strict shape for the LLM's raw JSON response; anything else (malformed
    JSON, missing/empty field) triggers the fail-soft fallback. Optional
    ``dockerfile``; ``dockerfiles`` maps repo-relative paths (multi-container).
    """

    main_tf: str
    variables_tf: str
    outputs_tf: str
    dockerfile: str | None = None
    dockerfiles: dict[str, str] | None = None


def _docker_blocks(dockerfiles: dict[str, str] | None) -> str:
    """Render the prompt's Dockerfile section: one labelled block per container
    (single-container legacy payloads just say ``Dockerfile``). Empty/None renders
    "(aucun Dockerfile fourni)" so the LLM won't invent one.
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
    """Rewrite multi-line ``RUN echo '...' > file`` blocks as heredocs: they're
    invalid Dockerfile syntax (continuation only happens at ``\\``, so each extra
    line parses as its own unknown instruction and the build dies at parse time).
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
    # Single-line bodies are already valid; leave untouched (minimal diff).
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
    """Replace ``apk add phpNN-*`` with ``docker-php-ext-install`` — on PHP Alpine
    images those packages don't exist, so ``apk add`` fails; already-loaded modules
    are skipped (rebuild refused, verified) and gd/zip/intl get apk build deps first.
    """
    if not dockerfile:
        return dockerfile
    # Modules already compiled into the official php:8.x-cli-alpine images (from
    # `php -m`); docker-php-ext-install refuses to rebuild a loaded module.
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
                # &&/|| continuation (losing RUN would orphan continuation lines).
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
                # Swallow trailing &&/|| continuation lines of the original block;
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
    """Add ``git`` to a Debian ``apt-get install`` when the repo needs composer:
    its dist-first install falls back to git-cloning ``--prefer-source`` on a
    GitHub 429, and e.g. ``php:8.2-apache`` lacks git ("git was not found").
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
    """Strip stray ``RUN`` splices inside RUN continuation blocks: the refiner
    sometimes splices ``RUN apk add ...`` mid-continuation, producing bogus apt
    packages or "/bin/sh: RUN: not found"; such lines are simply dropped.
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
    """Reattach a RUN prefix lost by the refiner: without the leading ``RUN apk
    add/apt-get install \\`` line, continuation lines parse as unknown instructions;
    docker-php-ext/pecl/apt/composer blocks get ``RUN apk add --no-cache`` back."""
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
    """Add a docroot (-t) to a PHP built-in server CMD using a router script:
    without it request paths resolve against CWD, so a real file under ``public``
    404s even though the router handles existing files (verified live: 404 vs 200
    with ``-t /app/public``); the router's absolute-path checks keep working."""
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
    """Point a PHP built-in server ``-t`` docroot at the real frontend dir: a
    stale path kills ``php -S`` at startup ("Directory does not exist", verified
    live) -> DeployOps rollback; rewrite to the dir the frontend-build COPY creates."""
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
    """Append ``mysqli`` when ``docker-php-ext-install`` lists pdo_mysql: mysqli-API
    apps die with "Class 'mysqli' not found", and since ``php -S`` answers HTTP 200
    with the fatal-error body the probe passes while broken (verified live)."""
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
    """Copy ``server/`` when the refiner only copies composer files: otherwise the
    ``php -S`` router can't load (PHP fatal with HTTP 200 — probe passes, app
    broken, verified live). Injects COPY server/ /app/server/ under /app/server/."""
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
    """Replace ``localhost`` with ``127.0.0.1`` in HEALTHCHECK commands: on Alpine
    PHP images localhost resolves to IPv6 ::1 but the app binds IPv4 only, so the
    probe never connects -> unhealthy -> rollback (verified live on 8.2-cli-alpine)."""
    if not dockerfile:
        return dockerfile
    return re.sub(
        r"(HEALTHCHECK[\s\S]*?)localhost",
        r"\g<1>127.0.0.1",
        dockerfile,
    )


def _fix_inline_frontend_build(dockerfile: str, repo_context: str | None = None) -> str:
    """Move an inline ``npm build`` out of a PHP image into a Node builder stage:
    apt-installed nodejs/npm in php:8.2-cli is too old (npm's eslint dies: unknown
    key "jest/globals", verified live); rewrite to the good shape — node:20-alpine
    AS frontend-builder. Only fires on inline builds (no node stage yet)."""
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

    # Root-layout guard (no front/ dir): strip inline node-build lines — the app
    # serves fine without compiled vite assets; keeping them guaranteed a dead build.
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
        # Start of an inline frontend build in the PHP stage — two wild shapes:
        # `RUN cd /app/front && npm ...` or WORKDIR/COPY front + `npm run build`.
        if in_php_stage and (
            s.startswith("RUN cd /app/front")
            or s == "WORKDIR /app/front"
            or s.startswith("COPY front/")
            or s.startswith("npm run build")
        ):
            in_inline_build = True
            continue
        if in_inline_build:
            # Consume inline-build lines until the copy-out line (build output +
            # public dir) or a real boundary; comments/blanks belong to the block.
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
    """Rewrite dev-mode CMD/ENTRYPOINTs to production equivalents (fail-soft):
    npm run dev -> npm start, uvicorn --reload -> uvicorn, flask run -> gunicorn,
    ng serve -> ng serve --configuration production."""
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
        # Normalize JSON-array tokens so patterns match both `CMD ["npm", ...]`
        # and shell form; a match rebuilds the line, a non-match keeps it verbatim.
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
            # Strip --reload/--reload-dir <path> in shell and JSON-array form.
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
    # Fail-soft/diff-minimal: no dev-mode CMD -> byte-for-byte original (applied
    # to every image's Dockerfile on every render).
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


def _fix_gradle_maven_mismatch(dockerfile: str, repo_context: str | None) -> str:
    """Rewrite Gradle build to Maven when the repo has pom.xml but no build.gradle.
    The refiner sometimes guesses wrong on Java repos — build dies at COPY."""
    if not dockerfile or not repo_context:
        return dockerfile
    if "gradle" not in dockerfile.lower():
        return dockerfile
    if "pom.xml" not in repo_context:
        return dockerfile
    if "build.gradle" in repo_context or "build.gradle.kts" in repo_context:
        return dockerfile
    dockerfile = re.sub(r"(?mi)^(FROM\s+\S*gradle:\S+)(.*)$",
                        lambda m: m.group(1).replace("gradle:", "maven:") + " AS builder",
                        dockerfile)
    dockerfile = re.sub(r"(?mi)^(\s*RUN\s+)gradle\s+build\s+(.*$)",
                        r"\1mvn -B package -DskipTests && mv target/*.jar app.jar", dockerfile)
    dockerfile = re.sub(r'(?mi)^(\s*COPY\s+)(build\.gradle|settings\.gradle|gradle\.properties)\s+(.*)$',
                        r"\1pom.xml \3", dockerfile)
    dockerfile = re.sub(r"(?mi)^\s*COPY\s+gradle/\s+.*$", "", dockerfile)
    return dockerfile


def _sanitize_dockerfile_dependencies(dockerfile: str, repo_context: str | None) -> str:
    """Rewrite dependency installs referencing files absent from the repo context;
    also inject Composer's ``--no-blocking`` (Composer 2.3+ blocks installs on
    security advisories with exit 2, and the refiner omits the flag)."""
    if not dockerfile or not repo_context:
        return dockerfile

    dockerfile = _fix_dev_mode_cmd(dockerfile)
    dockerfile = _heredocify_multiline_echo(dockerfile)
    dockerfile = _fix_detached_install_run(dockerfile)
    dockerfile = _fix_mixed_apt_apk(dockerfile)
    dockerfile = _fix_bun_without_lockfile(dockerfile, repo_context)
    dockerfile = _fix_gradle_maven_mismatch(dockerfile, repo_context)
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
        # Composer 2.3+ blocks on security advisories (exit 2); the refiner omits
        # --no-blocking, so inject it when the repo has a composer manifest.
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
        # GitHub rate-limits zipball downloads (429) and Composer's dist install
        # never falls back to source on 429 — rewrite as dist || source (git works).
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


# Feedback rewrites the Dockerfile only when it targets container concerns; else
# the captured original ships verbatim (a free-tier LLM broke a valid one once).
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
    """True when the user's request plausibly concerns the Dockerfile — a
    conservative keyword scan, not NLP: cost/AZ/region/"regenerate" requests
    leave it untouched."""
    lower = (feedback or "").lower()
    return any(term in lower for term in _DOCKER_FEEDBACK_TERMS)


def _refine_dockerfiles(
    dockerfiles: dict[str, str] | None,
    refined: _RefinedTerraform,
    feedback: str,
    repo_context: str | None,
    force_dockerfile: bool,
) -> dict[str, str] | None:
    """Apply the LLM's plural Dockerfile edits with per-file sanitization: each
    candidate is validated + sanitized like the singular path, anything invalid
    keeps that file's original (per-file fail-soft); None if none were supplied."""
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
    """Refine rendered artifacts from a prompt (Gate-2 feedback, or the first-try "match the repo"
    instruction). Legacy ``dockerfile`` vs multi-container ``dockerfiles`` map sets the return shape;
    ``repo_context`` feeds the real code; ``force_dockerfile=True`` replaces even a captured Dockerfile
    (first try: the stub can't run). Fail-soft: any LLM failure/invalid output returns inputs unchanged."""
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
                # Dockerfile: refine only when one exists, feedback targets container
                # concerns (or force_dockerfile — first try), AND the LLM returned a
                # valid replacement; anything else keeps the original untouched.
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
