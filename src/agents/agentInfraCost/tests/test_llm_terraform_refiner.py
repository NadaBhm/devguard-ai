"""Tests for core.llm_terraform_refiner (Phase 6: Gate-2 feedback loop).

core.llm_provider.call_llm is always monkeypatched here — no test reaches
OpenRouter for real. The focus is the fail-soft contract:

  - valid LLM output -> the refined files replace the originals
  - a valid dockerfile in the reply refines the Dockerfile alongside them
  - every failure mode (None, malformed JSON, missing/empty fields) ->
    the ORIGINAL files come back untouched, never an error.
  - transient provider flakiness -> the refiner re-asks (a fresh request per
    attempt) and only gives up after REFINER_MAX_ATTEMPTS, so a single
    bad reply doesn't silently drop the user's regeneration request.

refine_terraform now returns a ``(TerraformFiles, dockerfile)`` tuple; the
dockerfile is ``None`` when no current Dockerfile was supplied.
"""

from __future__ import annotations

import json
import re

import pytest

from core.llm_terraform_refiner import _fix_dev_mode_cmd, _fix_mixed_apt_apk, refine_terraform
from models.output_schema import TerraformFiles

CURRENT = TerraformFiles(
    main_tf='resource "aws_ecs_cluster" "app" {\n  name = "devguard"\n}',
    variables_tf='variable "aws_region" {\n  default = "us-east-1"\n}',
    outputs_tf='output "alb_dns" {\n  value = aws_lb.app.dns_name\n}',
)

DOCKERFILE = "FROM python:3.12-slim\nCOPY . /app\n"


def _patch_call_llm(monkeypatch: pytest.MonkeyPatch, return_value) -> None:
    monkeypatch.setattr(
        "core.llm_terraform_refiner.call_llm",
        lambda *args, **kwargs: return_value,
    )


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries must not slow the unit tests: zero the backoff sleep."""
    monkeypatch.setattr("core.llm_terraform_refiner.REFINER_RETRY_DELAY_SECONDS", 0)


def _valid_reply(**overrides) -> str:
    payload = {
        "main_tf": CURRENT.main_tf,
        "variables_tf": CURRENT.variables_tf,
        "outputs_tf": CURRENT.outputs_tf,
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestRefinerNominal:
    def test_valid_output_replaces_files(self, monkeypatch) -> None:
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": 'resource "aws_ecs_cluster" "app" {\n  name = "devguard-cost-optimized"\n}',
                    "variables_tf": 'variable "aws_region" {\n  default = "eu-west-1"\n}',
                    "outputs_tf": 'output "alb_dns" {\n  value = aws_lb.app.dns_name\n}',
                }
            ),
        )

        files, dockerfile = refine_terraform(CURRENT, "use eu-west-1 and a cheaper cluster")

        assert files.main_tf != CURRENT.main_tf
        assert "eu-west-1" in files.variables_tf
        assert isinstance(files, TerraformFiles)
        # no dockerfile supplied -> returns None, never a fabricated file
        assert dockerfile is None

    def test_feedback_is_included_in_the_prompt(self, monkeypatch) -> None:
        captured = {}

        def fake_call_llm(*args, **kwargs):
            captured["prompt"] = kwargs.get("prompt", args[0] if args else None)
            return json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                }
            )

        monkeypatch.setattr("core.llm_terraform_refiner.call_llm", fake_call_llm)

        refine_terraform(CURRENT, "make it cheaper please")

        assert "make it cheaper please" in captured["prompt"]

    def test_repo_context_is_included_in_the_prompt(self, monkeypatch) -> None:
        """Gate-2 regeneration with a whole-repo digest: the LLM must see the
        repo facts, not just the rendered artifacts."""
        captured = {}

        def fake_call_llm(*args, **kwargs):
            captured["prompt"] = kwargs.get("prompt", args[0] if args else None)
            return json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                }
            )

        monkeypatch.setattr("core.llm_terraform_refiner.call_llm", fake_call_llm)

        refine_terraform(
            CURRENT, "use two AZs", repo_context="app listens on port 9000, /healthz"
        )

        assert "=== CONTEXTE DU DÉPÔT ===" in captured["prompt"]
        assert "port 9000" in captured["prompt"]

    def test_repo_context_is_omitted_when_absent(self, monkeypatch) -> None:
        """Backward compatibility: no digest, no repo section in the prompt."""
        captured = {}

        def fake_call_llm(*args, **kwargs):
            captured["prompt"] = kwargs.get("prompt", args[0] if args else None)
            return json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                }
            )

        monkeypatch.setattr("core.llm_terraform_refiner.call_llm", fake_call_llm)

        refine_terraform(CURRENT, "cheaper")

        assert "=== CONTEXTE DU DÉPÔT ===" not in captured["prompt"]

    def test_valid_dockerfile_in_reply_refines_it(self, monkeypatch) -> None:
        """A container run with a Dockerfile: the LLM may edit it too when the
        feedback explicitly targets the Dockerfile."""
        refined_dockerfile = "FROM python:3.11-slim\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY . /app\n"
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": refined_dockerfile,
                }
            ),
        )

        files, dockerfile = refine_terraform(
            CURRENT,
            "utilise python 3.11 dans le dockerfile et multi-stage",
            dockerfile=DOCKERFILE,
        )

        assert files == CURRENT
        assert dockerfile == refined_dockerfile

    def test_composer_install_is_hardened_with_no_blocking(self, monkeypatch) -> None:
        """Composer 2.3+ aborts ``composer install`` (exit 2) when any required
        package is affected by a security advisory, unless ``--no-blocking`` is
        passed — the refiner reliably omits it, so the sanitizer injects it
        whenever the repo has a composer.json. Without this the multi-stage
        Dockerfile for a PHP monorepo fails the build at the composer stage."""
        repo_context = (
            "STRUCTURE COMPLÈTE DU DÉPÔT\n"
            "- server/composer.json\n"
            "- server/composer.lock\n"
            "- server/index.php\n"
            "- front/package.json\n"
        )
        refined_dockerfile = (
            "FROM composer:2 AS builder\n"
            "WORKDIR /app\n"
            "COPY composer.json ./\n"
            "RUN composer install --no-dev --optimize-autoloader --no-interaction\n"
        )
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": refined_dockerfile,
                }
            ),
        )

        files, dockerfile = refine_terraform(
            CURRENT,
            "génère un Dockerfile PHP fonctionnel",
            dockerfile=DOCKERFILE,
            repo_context=repo_context,
            force_dockerfile=True,
        )

        assert files == CURRENT
        assert "--no-blocking" in dockerfile
        assert "composer install --no-dev --optimize-autoloader --no-interaction --no-blocking" in dockerfile

    def test_composer_install_gets_source_fallback_for_github_ratelimit(self, monkeypatch) -> None:
        """GitHub rate-limits unauthenticated zipball downloads per IP; a
        composer.lock pinning dist to codeload/api.github.com URLs makes
        ``composer install`` die with HTTP/2 429 even when git protocol works.
        Composer's default dist-first install does not fall back to source on a
        429, so the sanitizer must rewrite the install as dist-then-source."""
        repo_context = (
            "STRUCTURE COMPLÈTE DU DÉPÔT\n"
            "- server/composer.json\n"
            "- server/composer.lock\n"
            "- server/index.php\n"
        )
        refined_dockerfile = (
            "FROM composer:2 AS builder\n"
            "WORKDIR /app\n"
            "COPY composer.json ./\n"
            "RUN composer install --no-dev --optimize-autoloader --no-interaction --no-blocking\n"
        )
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": refined_dockerfile,
                }
            ),
        )

        files, dockerfile = refine_terraform(
            CURRENT,
            "génère un Dockerfile PHP fonctionnel",
            dockerfile=DOCKERFILE,
            repo_context=repo_context,
            force_dockerfile=True,
        )

        assert files == CURRENT
        install_line = next(
            l for l in dockerfile.splitlines() if " composer install " in l
        )
        assert "|| composer install --no-dev --optimize-autoloader --no-interaction --no-blocking --prefer-source" in install_line
        assert not install_line.replace("|| composer", "").startswith("RUN RUN")

    def test_multiline_run_echo_is_rewritten_as_heredoc(self, monkeypatch) -> None:
        """The refiner occasionally emits a multi-line ``RUN echo '...' > file``
        block (e.g. a PHP router script). That is invalid Dockerfile — every
        line after the first parses as its own unknown instruction, and the
        build dies at parse time ("dockerfile parse error ... unknown
        instruction: $uri"). The sanitizer must rewrite such blocks as
        heredocs so they build."""
        repo_context = (
            "STRUCTURE COMPLÈTE DU DÉPÔT\n"
            "- server/composer.json\n"
            "- server/index.php\n"
            "- front/package.json\n"
        )
        refined_dockerfile = (
            "FROM php:8.2-cli-alpine\n"
            "RUN echo '<?php\n"
            "$uri = parse_url($_SERVER[\"REQUEST_URI\"], PHP_URL_PATH);\n"
            "if ($uri !== \"/\" && file_exists(__DIR__ . \"/public\" . $uri)) {\n"
            "    return false;\n"
            "}\n"
            "require_once __DIR__ . \"/server/index.php\";\n"
            "' > /app/router.php\n"
            "EXPOSE 8000\n"
        )
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": refined_dockerfile,
                }
            ),
        )

        files, dockerfile = refine_terraform(
            CURRENT,
            "génère un Dockerfile PHP fonctionnel",
            dockerfile=DOCKERFILE,
            repo_context=repo_context,
            force_dockerfile=True,
        )

        assert files == CURRENT
        assert "cat <<'DOCKERFILE_EOF' > /app/router.php" in dockerfile
        assert "require_once __DIR__ . \"/server/index.php\";" in dockerfile
        assert "DOCKERFILE_EOF" in dockerfile
        # The heredoc body is the only place bare PHP lines appear; no line
        # outside the heredoc may start with a PHP fragment.
        in_heredoc = False
        for line in dockerfile.splitlines():
            if line.startswith("RUN cat <<"):
                in_heredoc = True
            elif line.strip() == "DOCKERFILE_EOF":
                in_heredoc = False
            elif not in_heredoc and line.strip().startswith(("$uri", "if ($uri", "require_once", "return false", "}")):
                assert False, f"bare PHP line outside heredoc: {line!r}"

    def test_apk_php_packages_rewritten_to_docker_php_ext_install(self, monkeypatch) -> None:
        """The refiner emits ``apk add php82-*`` in an official PHP alpine
        image. Those packages do not exist in that image's Alpine repo (PHP is
        already compiled in), so ``apk add`` fails and the build dies. The
        sanitizer must rewrite the block to ``docker-php-ext-install`` for the
        extensions that are NOT already present, skipping modules already
        compiled into the base image (mbstring, curl, json, ...) and adding
        apk build deps for gd/zip/intl."""
        repo_context = (
            "STRUCTURE COMPLÈTE DU DÉPÔT\n"
            "- server/composer.json\n"
            "- server/index.php\n"
            "- front/package.json\n"
        )
        refined_dockerfile = (
            "FROM php:8.2-cli-alpine\n"
            "RUN apk add --no-cache \\\n"
            "    php82-pdo_mysql \\\n"
            "    php82-mbstring \\\n"
            "    php82-curl \\\n"
            "    php82-zip \\\n"
            "    php82-gd \\\n"
            "    php82-intl \\\n"
            "    php82-json \\\n"
            "    php82-dom \\\n"
            "    php82-simplexml\n"
            "WORKDIR /app\n"
        )
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": refined_dockerfile,
                }
            ),
        )

        files, dockerfile = refine_terraform(
            CURRENT,
            "génère un Dockerfile PHP fonctionnel",
            dockerfile=DOCKERFILE,
            repo_context=repo_context,
            force_dockerfile=True,
        )

        assert files == CURRENT
        assert "RUN apk add --no-cache icu-dev libpng-dev libjpeg-turbo-dev libzip-dev && docker-php-ext-install pdo_mysql mysqli zip gd intl" in dockerfile
        # Already-present modules must not be passed to docker-php-ext-install
        # (it refuses to rebuild a loaded module) nor kept as apk packages.
        assert "php82-mbstring" not in dockerfile
        assert "mbstring" not in dockerfile.split("docker-php-ext-install")[1].split("\n")[0]
        assert "curl" not in dockerfile.split("docker-php-ext-install")[1].split("\n")[0]
        assert "json" not in dockerfile.split("docker-php-ext-install")[1].split("\n")[0]
        assert "dom" not in dockerfile.split("docker-php-ext-install")[1].split("\n")[0]

    def test_php_builtin_server_cmd_gains_docroot(self, monkeypatch) -> None:
        """The refiner emits ``php -S 0.0.0.0:8000 /app/router.php`` with no
        ``-t`` docroot. The PHP built-in server then resolves request paths
        against the CWD, so ``/health.php`` — a real file under the image's
        ``public`` dir that the router explicitly returns ``false`` for —
        serves 404 (verified live). The sanitizer must inject ``-t
        /app/public`` ahead of the router script; the router's absolute-path
        checks are unaffected by the docroot."""
        repo_context = (
            "STRUCTURE COMPLÈTE DU DÉPÔT\n"
            "- server/composer.json\n"
            "- server/index.php\n"
            "- front/package.json\n"
        )
        refined_dockerfile = (
            "FROM php:8.2-cli-alpine\n"
            "RUN docker-php-ext-install pdo_mysql\n"
            "WORKDIR /app\n"
            "COPY server/ /app/server/\n"
            "COPY --from=frontend-builder /app/front/build /app/public\n"
            "RUN echo '<?php http_response_code(200); echo \"OK\"; ?>' > /app/public/health.php\n"
            "RUN echo '<?php \$uri = parse_url(\$_SERVER[\"REQUEST_URI\"], PHP_URL_PATH); if (file_exists(__DIR__ . \"/public\" . \$uri)) { return false; } require_once __DIR__ . \"/server/index.php\";' > /app/router.php\n"
            "EXPOSE 8000\n"
            "CMD [\"php\", \"-S\", \"0.0.0.0:8000\", \"/app/router.php\"]\n"
        )
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": refined_dockerfile,
                }
            ),
        )

        files, dockerfile = refine_terraform(
            CURRENT,
            "génère un Dockerfile PHP fonctionnel",
            dockerfile=DOCKERFILE,
            repo_context=repo_context,
            force_dockerfile=True,
        )

        assert files == CURRENT
        assert 'CMD ["php", "-S", "0.0.0.0:8000", "-t", "/app/public", "/app/router.php"]' in dockerfile

    def test_php_builtin_server_docroot_points_at_real_public_dir(self, monkeypatch) -> None:
        """The refiner sometimes emits ``-t /app/public`` while the frontend
        build is copied to ``/app/server/public/``. ``php -S`` dies at startup
        with "Directory /app/public does not exist" (verified live), so the
        container never serves. The sanitizer must point the docroot at the
        directory the frontend-builder COPY actually creates."""
        repo_context = (
            "STRUCTURE COMPLÈTE DU DÉPÔT\n"
            "- server/composer.json\n"
            "- server/index.php\n"
            "- front/package.json\n"
        )
        refined_dockerfile = (
            "FROM node:20-alpine AS frontend-builder\n"
            "WORKDIR /app/front\n"
            "COPY front/package*.json ./\n"
            "RUN npm ci\n"
            "COPY front/ ./\n"
            "RUN npm run build\n"
            "\n"
            "FROM php:8.2-cli\n"
            "RUN apt-get update && apt-get install -y git unzip zip && docker-php-ext-install pdo_mysql\n"
            "COPY server/ /app/server/\n"
            "COPY --from=frontend-builder /app/front/build/ /app/server/public/\n"
            "EXPOSE 8000\n"
            'CMD ["php", "-S", "0.0.0.0:8000", "-t", "/app/public", "/app/server/index.php"]\n'
        )
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": refined_dockerfile,
                }
            ),
        )

        files, dockerfile = refine_terraform(
            CURRENT,
            "génère un Dockerfile PHP fonctionnel",
            dockerfile=DOCKERFILE,
            repo_context=repo_context,
            force_dockerfile=True,
        )

        assert files == CURRENT
        assert (
            'CMD ["php", "-S", "0.0.0.0:8000", "-t", "/app/server/public", "/app/server/index.php"]'
            in dockerfile
        )
        assert "COPY server/ /app/server/\n" in dockerfile
        assert re.search(
            r"docker-php-ext-install[^\n]*pdo_mysql\b[^\n]*\bmysqli",
            dockerfile,
        )

    def test_mysqli_added_when_pdo_mysql_installed(self, monkeypatch) -> None:
        """The refiner installs ``pdo_mysql`` but the app connects to MySQL via
        the classic ``mysqli`` API. Without the ``mysqli`` extension, every
        request dies with "Class 'mysqli' not found" — yet ``php -S`` still
        returns HTTP 200, so the health probe passes and the deployment
        "succeeds" while the app is broken (verified live). The sanitizer must
        append ``mysqli`` to the ``docker-php-ext-install`` list whenever
        ``pdo_mysql`` is present."""
        repo_context = (
            "STRUCTURE COMPLÈTE DU DÉPÔT\n"
            "- server/composer.json\n"
            "- server/composer.lock\n"
            "- server/index.php\n"
            "- front/package.json\n"
        )
        refined_dockerfile = (
            "FROM php:8.2-cli\n"
            "RUN apt-get update && apt-get install -y git unzip zip && docker-php-ext-install pdo_mysql zip\n"
            "COPY server/ /app/server/\n"
            "EXPOSE 8000\n"
            'CMD ["php", "-S", "0.0.0.0:8000", "/app/server/index.php"]\n'
        )
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": refined_dockerfile,
                }
            ),
        )

        files, dockerfile = refine_terraform(
            CURRENT,
            "génère un Dockerfile PHP fonctionnel",
            dockerfile=DOCKERFILE,
            repo_context=repo_context,
            force_dockerfile=True,
        )

        assert files == CURRENT
        assert re.search(
            r"docker-php-ext-install[^\n]*pdo_mysql\b[^\n]*\bmysqli",
            dockerfile,
        )

    def test_detached_install_run_is_reattached(self, monkeypatch) -> None:
        """The refiner sometimes emits a package-install block whose leading
        ``RUN apk add --no-cache \\`` line is dropped, leaving only the
        indented continuation lines. Each of those parses as its own unknown
        Dockerfile instruction, so the build dies at parse time. The sanitizer
        must reattach the RUN prefix (and keep non-``phpNN`` blocks intact so
        the earlier ``apk add`` rewrite doesn't re-detach them)."""
        repo_context = (
            "STRUCTURE COMPLÈTE DU DÉPÔT\n"
            "- server/composer.json\n"
            "- server/index.php\n"
            "- front/package.json\n"
        )
        refined_dockerfile = (
            "FROM php:8.2-cli-alpine\n"
            "\n"
            "# Install system dependencies and PHP extensions\n"
            "    libzip-dev \\\n"
            "    zip \\\n"
            "    unzip \\\n"
            "    git \\\n"
            "    linux-headers \\\n"
            "    $PHPIZE_DEPS \\\n"
            "    && docker-php-ext-install mysqli pdo pdo_mysql zip \\\n"
            "    && pecl install redis \\\n"
            "    && docker-php-ext-enable redis\n"
            "\n"
            "WORKDIR /app\n"
            "COPY server/ ./\n"
        )
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": refined_dockerfile,
                }
            ),
        )

        files, dockerfile = refine_terraform(
            CURRENT,
            "génère un Dockerfile PHP fonctionnel",
            dockerfile=DOCKERFILE,
            repo_context=repo_context,
            force_dockerfile=True,
        )

        assert files == CURRENT
        assert 'RUN apk add --no-cache \\' in dockerfile
        assert "libzip-dev \\" in dockerfile
        assert "&& docker-php-ext-install mysqli pdo pdo_mysql zip \\" in dockerfile
        assert "&& docker-php-ext-enable redis" in dockerfile

    def test_healthcheck_localhost_replaced_with_127_0_0_1(self, monkeypatch) -> None:
        """The refiner writes the image HEALTHCHECK against ``localhost``, which
        resolves to IPv6 ``::1`` on Alpine while ``php -S 0.0.0.0`` binds IPv4
        only — the probe never connects and the container is permanently
        unhealthy, causing DeployOps to roll back a working app. The sanitizer
        must swap ``localhost`` for ``127.0.0.1`` in the HEALTHCHECK."""
        repo_context = (
            "STRUCTURE COMPLÈTE DU DÉPÔT\n"
            "- server/composer.json\n"
            "- server/index.php\n"
            "- front/package.json\n"
        )
        refined_dockerfile = (
            "FROM php:8.2-cli-alpine\n"
            "RUN docker-php-ext-install pdo_mysql\n"
            "WORKDIR /app\n"
            "COPY server/ /app/server/\n"
            "RUN echo '<?php http_response_code(200); echo \"OK\"; ?>' > /app/public/health.php\n"
            "EXPOSE 8000\n"
            "HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \\\n"
            "    CMD wget --no-verbose --tries=1 --spider http://localhost:8000/health.php || exit 1\n"
            "CMD [\"php\", \"-S\", \"0.0.0.0:8000\", \"/app/router.php\"]\n"
        )
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": refined_dockerfile,
                }
            ),
        )

        files, dockerfile = refine_terraform(
            CURRENT,
            "génère un Dockerfile PHP fonctionnel",
            dockerfile=DOCKERFILE,
            repo_context=repo_context,
            force_dockerfile=True,
        )

        assert files == CURRENT
        assert "http://localhost:8000" not in dockerfile
        assert "http://127.0.0.1:8000/health.php" in dockerfile
        assert 'CMD ["php", "-S", "0.0.0.0:8000", "-t", "/app/public", "/app/router.php"]' in dockerfile

    def test_inline_frontend_build_moved_to_node_stage(self, monkeypatch) -> None:
        """The refiner occasionally builds the React frontend inside the PHP
        image via apt-installed nodejs/npm. The distro Node (v18 on Debian
        bookworm) cannot build this repo's react-scripts — ``npm run build``
        dies with 'Environment key jest/globals is unknown'. The sanitizer must
        split the build into a node:20-alpine builder stage, strip nodejs/npm
        from the PHP image's apt install, and COPY --from the build output
        (the refiner's correct runs already use this shape)."""
        repo_context = (
            "STRUCTURE COMPLÈTE DU DÉPÔT\n"
            "- server/composer.json\n"
            "- server/index.php\n"
            "- front/package.json\n"
            "- front/package-lock.json\n"
        )
        refined_dockerfile = (
            "FROM php:8.2-cli\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "    nodejs \\\n"
            "    npm \\\n"
            "    git \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
            "RUN docker-php-ext-install -j$(nproc) \\\n"
            "    pdo_mysql \\\n"
            "    zip \\\n"
            "    gd\n"
            "WORKDIR /app\n"
            "COPY server/composer.json /app/server/\n"
            "RUN cd /app/server && composer install --no-dev --optimize-autoloader --no-interaction\n"
            "COPY front/package.json /app/front/\n"
            "RUN cd /app/front && npm install --prefer-offline --no-audit --no-fund\n"
            "COPY front/ /app/front/\n"
            "RUN cd /app/front && npm run build\n"
            "COPY server/ /app/server/\n"
            "RUN mkdir -p /app/server/public && cp -r /app/front/build/* /app/server/public/\n"
            "EXPOSE 8000\n"
            "CMD [\"php\", \"-S\", \"0.0.0.0:8000\", \"-t\", \"/app/server/public\", \"/app/server/index.php\"]\n"
        )
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": refined_dockerfile,
                }
            ),
        )

        files, dockerfile = refine_terraform(
            CURRENT,
            "génère un Dockerfile PHP fonctionnel",
            dockerfile=DOCKERFILE,
            repo_context=repo_context,
            force_dockerfile=True,
        )

        assert files == CURRENT
        assert "FROM node:20-alpine AS frontend-builder" in dockerfile
        assert "RUN npm run build" in dockerfile
        # The PHP stage must no longer install nodejs/npm.
        apt_block = dockerfile.split("RUN apt-get", 1)[1].split("\n\n", 1)[0]
        assert "nodejs" not in apt_block
        assert "npm" not in apt_block
        # Inline frontend build must be gone; output comes via COPY --from.
        assert "COPY front/package.json /app/front/" not in dockerfile
        assert "npm install --prefer-offline" not in dockerfile
        assert "COPY --from=frontend-builder /app/front/build/ /app/server/public/" in dockerfile
        assert 'CMD ["php", "-S", "0.0.0.0:8000", "-t", "/app/server/public", "/app/server/index.php"]' in dockerfile

    def test_inline_frontend_build_workdir_shape_removed(self, monkeypatch) -> None:
        """A second refiner shape builds the frontend inline via
        ``WORKDIR /app/front`` + ``COPY front/... ./`` + ``npm run build``
        (no ``cd /app/front &&`` prefix). The sanitizer must drop that block
        from the PHP stage and COPY --from the node builder output — even when
        a node stage is already present in the refiner output."""
        repo_context = (
            "STRUCTURE COMPLÈTE DU DÉPÔT\n"
            "- server/composer.json\n"
            "- server/index.php\n"
            "- front/package.json\n"
            "- front/package-lock.json\n"
        )
        refined_dockerfile = (
            "FROM node:20-alpine AS frontend-builder\n"
            "WORKDIR /app/front\n"
            "COPY front/package*.json ./\n"
            "RUN npm ci\n"
            "COPY front/ ./\n"
            "RUN npm run build\n"
            "\n"
            "FROM php:8.2-cli\n"
            "RUN apt-get update && apt-get install -y \\\n"
            "    git \\\n"
            "    unzip \\\n"
            "    libzip-dev \\\n"
            "    libpng-dev \\\n"
            "    libonig-dev \\\n"
            "    libxml2-dev \\\n"
            "    zip \\\n"
            "    curl \\\n"
            "    && docker-php-ext-install pdo_mysql zip mbstring exif pcntl bcmath gd\n"
            "COPY --from=composer:latest /usr/bin/composer /usr/bin/composer\n"
            "WORKDIR /app\n"
            "COPY server/composer.json server/composer.lock* ./\n"
            "RUN composer install --no-dev --optimize-autoloader --no-interaction --no-blocking\n"
            "COPY server/ ./server/\n"
            "# Build frontend\n"
            "WORKDIR /app/front\n"
            "COPY front/package.json front/package-lock.json* ./\n"
            "RUN npm ci --only=production\n"
            "\n"
            "COPY front/ ./\n"
            "RUN npm run build\n"
            "\n"
            "# Copy frontend build to server public directory\n"
            "RUN mkdir -p /app/server/public && cp -r build/* /app/server/public/\n"
            "# Create health check endpoint\n"
            "RUN echo '<?php echo \"ok\"; ?>' > /app/server/public/health.php\n"
            "EXPOSE 8000\n"
            "CMD [\"php\", \"-S\", \"0.0.0.0:8000\", \"-t\", \"public\", \"router.php\"]\n"
        )
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": refined_dockerfile,
                }
            ),
        )

        files, dockerfile = refine_terraform(
            CURRENT,
            "génère un Dockerfile PHP fonctionnel",
            dockerfile=DOCKERFILE,
            repo_context=repo_context,
            force_dockerfile=True,
        )

        assert files == CURRENT
        assert "FROM node:20-alpine AS frontend-builder" in dockerfile
        assert "COPY --from=frontend-builder /app/front/build/ /app/server/public/" in dockerfile
        php_stage = dockerfile.split("FROM php", 1)[1]
        assert "npm run build" not in php_stage
        assert "npm ci --only=production" not in php_stage
        assert "WORKDIR /app/front" not in php_stage
        assert "cp -r build/* /app/server/public/" not in php_stage
        assert "docker-php-ext-install pdo_mysql zip mbstring exif pcntl bcmath gd" in php_stage
        assert "RUN echo '<?php echo \"ok\"; ?>' > /app/server/public/health.php" in php_stage
        assert "EXPOSE 8000" in php_stage

    def test_dockerfile_not_targeted_by_feedback_keeps_original(self, monkeypatch) -> None:
        """Feedback that doesn't mention container concerns must NOT let the
        LLM's rewritten Dockerfile through — the repo's real Dockerfile stays
        untouched even if the reply carries a (possibly broken) replacement."""
        llm_dockerfile = "FROM scratch\nCOPY . /app\nCMD [\"bogus\"]\n"
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": llm_dockerfile,
                }
            ),
        )

        files, dockerfile = refine_terraform(CURRENT, "make it cheaper", dockerfile=DOCKERFILE)

        assert files == CURRENT
        assert dockerfile == DOCKERFILE

    def test_dockerfile_kept_when_feedback_only_asks_terraform(self, monkeypatch) -> None:
        """The exact regression from the real run: 'make it cheaper' rewrote
        the Dockerfile into a broken generic one. It must now be preserved."""
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": (
                        "FROM python:3.12-slim\nCOPY requirements.txt .\nCMD uvicorn main:app\n"
                    ),
                }
            ),
        )

        files, dockerfile = refine_terraform(CURRENT, "make it cheaper", dockerfile=DOCKERFILE)

        assert files == CURRENT
        assert dockerfile == DOCKERFILE

    def test_dockerfile_absent_in_reply_keeps_original(self, monkeypatch) -> None:
        """Backward compatibility: a Terraform-only reply must not wipe the
        current Dockerfile."""
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                }
            ),
        )

        _, dockerfile = refine_terraform(CURRENT, "cheaper please", dockerfile=DOCKERFILE)

        assert dockerfile == DOCKERFILE

    def test_empty_dockerfile_in_reply_keeps_original(self, monkeypatch) -> None:
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": "   ",
                }
            ),
        )

        _, dockerfile = refine_terraform(CURRENT, "cheaper please", dockerfile=DOCKERFILE)

        assert dockerfile == DOCKERFILE


class TestRefinerFailSoft:
    """Every failure path must return the originals unchanged."""

    def test_none_reply_returns_originals(self, monkeypatch) -> None:
        _patch_call_llm(monkeypatch, None)
        files, dockerfile = refine_terraform(CURRENT, "x", dockerfile=DOCKERFILE)
        assert files == CURRENT
        assert dockerfile == DOCKERFILE

    def test_malformed_json_returns_originals(self, monkeypatch) -> None:
        _patch_call_llm(monkeypatch, "this is not json")
        files, dockerfile = refine_terraform(CURRENT, "x", dockerfile=DOCKERFILE)
        assert files == CURRENT
        assert dockerfile == DOCKERFILE

    def test_missing_fields_returns_originals(self, monkeypatch) -> None:
        _patch_call_llm(monkeypatch, json.dumps({"main_tf": "only this"}))
        files, dockerfile = refine_terraform(CURRENT, "x", dockerfile=DOCKERFILE)
        assert files == CURRENT
        assert dockerfile == DOCKERFILE

    def test_empty_file_returns_originals(self, monkeypatch) -> None:
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": "",
                    "variables_tf": "   ",
                    "outputs_tf": 'output "x" {}\n',
                }
            ),
        )
        files, dockerfile = refine_terraform(CURRENT, "x", dockerfile=DOCKERFILE)
        assert files == CURRENT
        assert dockerfile == DOCKERFILE

    def test_empty_string_reply_returns_originals(self, monkeypatch) -> None:
        _patch_call_llm(monkeypatch, "")
        files, dockerfile = refine_terraform(CURRENT, "x", dockerfile=DOCKERFILE)
        assert files == CURRENT
        assert dockerfile == DOCKERFILE


class TestRefinerRetries:
    """The free OpenRouter tiers flake on the first reply; the refiner must
    re-ask (a fresh request each attempt) before settling for fail-soft."""

    def test_retries_after_unusable_output_then_applies(self, monkeypatch) -> None:
        calls = {"count": 0}

        def flaky_call_llm(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return None
            return _valid_reply(
                main_tf='resource "aws_ecs_cluster" "app" {\n  name = "retried"\n}'
            )

        monkeypatch.setattr("core.llm_terraform_refiner.call_llm", flaky_call_llm)

        files, dockerfile = refine_terraform(CURRENT, "retry please")

        assert calls["count"] == 2
        assert "retried" in files.main_tf
        assert dockerfile is None

    def test_retries_past_malformed_reply(self, monkeypatch) -> None:
        calls = {"count": 0}

        def flaky_call_llm(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] < 3:
                return "not json at all"
            return _valid_reply(outputs_tf='output "alb" {\n  value = "x"\n}')

        monkeypatch.setattr("core.llm_terraform_refiner.call_llm", flaky_call_llm)

        files, _ = refine_terraform(CURRENT, "keep trying")

        assert calls["count"] == 3
        assert '"x"' in files.outputs_tf

    def test_exhausts_attempts_then_keeps_originals(self, monkeypatch) -> None:
        calls = {"count": 0}

        def always_bad(*args, **kwargs):
            calls["count"] += 1
            return "not json"

        monkeypatch.setattr("core.llm_terraform_refiner.call_llm", always_bad)
        monkeypatch.setattr("core.llm_terraform_refiner.REFINER_MAX_ATTEMPTS", 3)

        files, dockerfile = refine_terraform(CURRENT, "x", dockerfile=DOCKERFILE)

        assert calls["count"] == 3
        assert files == CURRENT
        assert dockerfile == DOCKERFILE

    def test_markdown_fenced_json_is_parsed(self, monkeypatch) -> None:
        _patch_call_llm(
            monkeypatch,
            "```json\n"
            + _valid_reply(main_tf='resource "aws_ecs_cluster" "app" {\n  name = "fenced"\n}')
            + "\n```",
        )

        files, _ = refine_terraform(CURRENT, "fence me")

        assert "fenced" in files.main_tf


class TestRefinerMultiContainer:
    DOCKERFILES = {
        "Dockerfile": "FROM python:3.12-slim\nCOPY . /app\n",
        "frontend/Dockerfile": "FROM nginx:1.27\nCOPY . /usr/share/nginx/html\n",
    }

    def test_plural_refines_every_dockerfile(self, monkeypatch) -> None:
        _patch_call_llm(
            monkeypatch,
            _valid_reply(
                dockerfiles={
                    "Dockerfile": "FROM python:3.12-slim\nRUN pip install fastapi\nCOPY . /app\n",
                    "frontend/Dockerfile": "FROM nginx:1.27-alpine\nCOPY . /usr/share/nginx/html\n",
                }
            ),
        )

        files, dockerfiles = refine_terraform(
            CURRENT, "switch to the alpine base image and pin python", dockerfiles=self.DOCKERFILES
        )

        assert files == CURRENT
        assert "nginx:1.27-alpine" in dockerfiles["frontend/Dockerfile"]
        assert "fastapi" in dockerfiles["Dockerfile"]

    def test_plural_missing_file_keeps_original(self, monkeypatch) -> None:
        # LLM only returns one Dockerfile; the other must survive unchanged.
        _patch_call_llm(
            monkeypatch,
            _valid_reply(dockerfiles={"Dockerfile": "FROM python:3.11-slim\nCOPY . /app\n"}),
        )

        files, dockerfiles = refine_terraform(
            CURRENT, "change the base image python version", dockerfiles=self.DOCKERFILES
        )

        assert dockerfiles["frontend/Dockerfile"] == self.DOCKERFILES["frontend/Dockerfile"]
        assert "python:3.11-slim" in dockerfiles["Dockerfile"]

    def test_plural_non_docker_feedback_keeps_all(self, monkeypatch) -> None:
        _patch_call_llm(
            monkeypatch,
            _valid_reply(
                dockerfiles={
                    "Dockerfile": "FROM hacked:evil\n",
                    "frontend/Dockerfile": "FROM hacked:evil\n",
                }
            ),
        )

        files, dockerfiles = refine_terraform(
            CURRENT, "make it cheaper", dockerfiles=self.DOCKERFILES
        )

        assert dockerfiles == self.DOCKERFILES

    def test_plural_llm_failure_keeps_originals(self, monkeypatch) -> None:
        _patch_call_llm(monkeypatch, "not json at all")

        files, dockerfiles = refine_terraform(
            CURRENT, "change python version", dockerfiles=self.DOCKERFILES
        )

        assert files == CURRENT
        assert dockerfiles == self.DOCKERFILES

    def test_plural_fail_soft_returns_original_map(self, monkeypatch) -> None:
        _patch_call_llm(
            monkeypatch,
            _valid_reply(main_tf="", variables_tf="", outputs_tf=""),
        )

        files, dockerfiles = refine_terraform(
            CURRENT, "change python version", dockerfiles=self.DOCKERFILES
        )

        assert files == CURRENT
        assert dockerfiles == self.DOCKERFILES


def test_fix_mixed_apt_apk_drops_spliced_continuation_line() -> None:
    """A ``RUN apk add --no-cache \\`` line spliced into an apt-get RUN's
    backslash continuation (the refiner merging its two install strategies)
    must be dropped, not left to become bogus apt packages. Regression for
    the Jupyter REST API E2E failure: build died because ``RUN``/``apk``/
    ``add``/``--no-cache`` were treated as Debian packages."""
    corrupted = (
        "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
        "    RUN apk add --no-cache \\\n"
        "    tzdata \\\n"
        "    python3-setuptools \\\n"
        "    git \\\n"
        "    && apt-get clean && rm -rf /var/lib/apt/lists/*\n"
    )
    fixed = _fix_mixed_apt_apk(corrupted)
    assert "RUN apk add --no-cache" not in fixed
    assert "python3-setuptools" in fixed
    assert "git" in fixed
    assert "&& apt-get clean" in fixed


def test_fix_mixed_apt_apk_keeps_normal_continuation_intact() -> None:
    """A legitimate apt-get RUN with a plain continuation is untouched."""
    clean = (
        "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
        "    tzdata \\\n"
        "    git \\\n"
        "    && apt-get clean\n"
    )
    assert _fix_mixed_apt_apk(clean) == clean.rstrip("\n")


def test_fix_dev_mode_cmd_npm_dev_becomes_npm_start() -> None:
    """Regression (live devverse): the repo's own Dockerfile ran `npm run dev`
    (Next.js dev server), which OOM'd/crashed in ECS after compiling. The
    deterministic fixer must map dev-mode CMDs to production equivalents."""
    dockerfile = (
        "FROM node:18-alpine\n"
        "WORKDIR /app\n"
        "COPY package*.json ./\n"
        "RUN npm ci\n"
        "COPY . .\n"
        "CMD [\"npm\", \"run\", \"dev\"]\n"
    )
    fixed = _fix_dev_mode_cmd(dockerfile)
    assert "npm start" in fixed
    assert "npm run dev" not in fixed


def test_fix_dev_mode_cmd_prefers_existing_start_script() -> None:
    """When the Dockerfile already references a start script, dev should map
    to `npm run start` (respecting the repo's script name) over `npm start`."""
    dockerfile = (
        "FROM node:18-alpine\n"
        "COPY . .\n"
        'RUN npm run start\n'
        "CMD [\"npm\", \"run\", \"dev\"]\n"
    )
    fixed = _fix_dev_mode_cmd(dockerfile)
    assert "npm run start" in fixed
    assert "npm run dev" not in fixed


def test_fix_dev_mode_cmd_strips_uvicorn_reload() -> None:
    """`uvicorn --reload` runs a file-watcher process that's useless and
    wasteful in a container; the production server is the same uvicorn
    invocation without the watcher."""
    dockerfile = (
        "FROM python:3.12-slim\n"
        "COPY . .\n"
        "CMD [\"uvicorn\", \"app.main:app\", \"--host\", \"0.0.0.0\", \"--port\", \"8000\", \"--reload\"]\n"
    )
    fixed = _fix_dev_mode_cmd(dockerfile)
    assert "--reload" not in fixed
    assert "app.main:app" in fixed


def test_fix_dev_mode_cmd_flask_run_becomes_gunicorn() -> None:
    dockerfile = (
        "FROM python:3.12-slim\n"
        "COPY . .\n"
        "CMD [\"flask\", \"run\", \"--host\", \"0.0.0.0\", \"--port\", \"8000\"]\n"
    )
    fixed = _fix_dev_mode_cmd(dockerfile)
    assert "gunicorn" in fixed
    assert "flask run" not in fixed


def test_fix_dev_mode_cmd_ng_serve_gets_production() -> None:
    dockerfile = (
        "FROM node:18-alpine\n"
        "COPY . .\n"
        "CMD [\"ng\", \"serve\", \"--host\", \"0.0.0.0\"]\n"
    )
    fixed = _fix_dev_mode_cmd(dockerfile)
    assert "--configuration production" in fixed


def test_fix_dev_mode_cmd_leaves_production_cmd_untouched() -> None:
    dockerfile = (
        "FROM node:18-alpine\n"
        "COPY . .\n"
        'CMD ["npm", "start"]\n'
    )
    assert _fix_dev_mode_cmd(dockerfile) == dockerfile


def test_fix_dev_mode_cmd_never_rewrites_run_instructions() -> None:
    """`npm run dev` inside a RUN (build-time dev-server setup) is not the
    container's entrypoint and must stay untouched."""
    dockerfile = (
        "FROM node:18-alpine\n"
        "RUN npm run dev -- --install\n"
        "CMD [\"node\", \"server.js\"]\n"
    )
    fixed = _fix_dev_mode_cmd(dockerfile)
    assert "npm run dev -- --install" in fixed
    assert 'CMD ["node", "server.js"]' in fixed
