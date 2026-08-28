"""Framework-specific Dockerfile templates for common stacks.

When stack detection identifies a known framework, use a battle-tested
template instead of relying on LLM generation. Templates are matched by
(primary_language, framework) pairs detected by CodeSec.

Only fires when the repo has NO existing Dockerfile — repos that ship one
keep using it (the refiner sanitizes but doesn't replace).
"""

from typing import Optional


# Each entry: (base_image, build_steps, expose_port, health_path, start_cmd)
_TEMPLATES: dict[tuple[str, str], dict] = {
    # --- Java / Spring Boot (Maven) ---
    ("java", "spring"): {
        "base_image": "eclipse-temurin:17-jre",
        "build_tool": "maven",
        "build": (
            "FROM maven:3.9-eclipse-temurin-17 AS builder\n"
            "WORKDIR /app\n"
            "{copy_deps}\n"
            "{copy_src}\n"
            "RUN mvn -B package -DskipTests\n"
        ),
        "runtime": (
            "FROM eclipse-temurin:17-jre\n"
            "WORKDIR /app\n"
            "COPY --from=builder /app/target/*.jar app.jar\n"
            "EXPOSE 8080\n"
            'HEALTHCHECK --interval=30s --timeout=5s CMD curl -f http://localhost:8080/ || exit 1\n'
            'CMD ["java", "-jar", "app.jar"]\n'
        ),
        "health_path": "/actuator/health",
    },
    # --- Java / Spring Boot (Gradle) ---
    ("java", "gradle_spring"): {
        "base_image": "eclipse-temurin:17-jre",
        "build_tool": "gradle",
        "build": (
            "FROM gradle:8.5-jdk17 AS builder\n"
            "WORKDIR /app\n"
            "{copy_deps}\n"
            "{copy_src}\n"
            "RUN gradle bootJar --no-daemon -x test\n"
        ),
        "runtime": (
            "FROM eclipse-temurin:17-jre\n"
            "WORKDIR /app\n"
            "COPY --from=builder /app/build/libs/*.jar app.jar\n"
            "EXPOSE 8080\n"
            'HEALTHCHECK --interval=30s --timeout=5s CMD curl -f http://localhost:8080/ || exit 1\n'
            'CMD ["java", "-jar", "app.jar"]\n'
        ),
        "health_path": "/actuator/health",
    },
    # --- Python / Flask ---
    ("python", "flask"): {
        "base_image": "python:3.12-slim",
        "build": "",
        "runtime": (
            "FROM python:3.12-slim\n"
            "WORKDIR /app\n"
            "{copy_deps}\n"
            "RUN pip install --no-cache-dir -r requirements.txt\n"
            "{copy_src}\n"
            "EXPOSE 5000\n"
            'HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen(\'http://localhost:5000/\')" || exit 1\n'
            'ENV FLASK_APP=app.py\n'
            'CMD ["flask", "run", "--host=0.0.0.0"]\n'
        ),
        "health_path": "/",
    },
    # --- Python / FastAPI ---
    ("python", "fastapi"): {
        "base_image": "python:3.12-slim",
        "build": "",
        "runtime": (
            "FROM python:3.12-slim\n"
            "WORKDIR /app\n"
            "{copy_deps}\n"
            "RUN pip install --no-cache-dir -r requirements.txt\n"
            "{copy_src}\n"
            "EXPOSE 8000\n"
            'HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen(\'http://localhost:8000/docs\')" || exit 1\n'
            'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]\n'
        ),
        "health_path": "/docs",
    },
    # --- JavaScript / Express ---
    ("javascript", "express"): {
        "base_image": "node:20-alpine",
        "build": "",
        "runtime": (
            "FROM node:20-alpine\n"
            "WORKDIR /app\n"
            "{copy_deps}\n"
            "RUN npm ci --only=production\n"
            "{copy_src}\n"
            "EXPOSE 3000\n"
            'HEALTHCHECK --interval=30s --timeout=5s CMD wget -q --spider http://localhost:3000/ || exit 1\n'
            'CMD ["node", "{entry_file}"]\n'
        ),
        "health_path": "/",
    },
    # --- PHP / Laravel ---
    ("php", "laravel"): {
        "base_image": "php:8.2-cli",
        "build": "",
        "runtime": (
            "FROM php:8.2-cli\n"
            "RUN apt-get update && apt-get install -y libzip-dev zip unzip \\\n"
            "    && docker-php-ext-install pdo_mysql mbstring\n"
            "COPY --from=composer:latest /usr/bin/composer /usr/bin/composer\n"
            "WORKDIR /app\n"
            "COPY composer.json composer.lock ./\n"
            "RUN composer install --no-dev --optimize-autoloader --no-interaction --no-scripts\n"
            "COPY . .\n"
            "RUN php artisan key:generate --force 2>/dev/null; true\n"
            "EXPOSE 8000\n"
            'CMD ["php", "artisan", "serve", "--host=0.0.0.0", "--port=8000"]\n'
        ),
        "health_path": "/",
    },
    # --- Go binary ---
    ("go", ""): {
        "base_image": "golang:1.21-alpine",
        "build": (
            "FROM golang:1.21-alpine AS builder\n"
            "WORKDIR /app\n"
            "COPY go.mod go.sum ./\n"
            "RUN go mod download\n"
            "COPY . .\n"
            "RUN go build -o server .\n"
        ),
        "runtime": (
            "FROM alpine:3.18\n"
            "WORKDIR /app\n"
            "COPY --from=builder /app/server .\n"
            "EXPOSE 8080\n"
            'CMD ["./server"]\n'
        ),
        "health_path": "/",
    },
}

# Framework aliases for matching
_ALIASES = {
    "spring-boot": "spring",
    "springboot": "spring",
    "express.js": "express",
    "expressjs": "express",
}


def match_template(
    primary_language: str,
    frameworks: list[str],
    detected_files: list[str],
) -> Optional[dict]:
    """Find the best matching template for a detected stack.

    Args:
        primary_language: e.g. "python", "java", "javascript"
        frameworks: e.g. ["flask", "sqlalchemy"], ["spring"]
        detected_files: e.g. ["requirements.txt", "app.py"]

    Returns:
        Template dict if matched, None otherwise.
    """
    lang = primary_language.strip().lower()
    fws = [f.strip().lower() for f in frameworks]

    # Normalize aliases
    norm_fws = [_ALIASES.get(f, f) for f in fws]

    file_set = {f.lower() for f in detected_files}

    # Java: detect build tool from presence of pom.xml vs build.gradle
    if lang == "java" and "spring" in norm_fws:
        if "pom.xml" in file_set:
            tmpl = _TEMPLATES.get(("java", "spring"))
            if tmpl:
                return _resolve_template(tmpl, lang, "spring", file_set)
        elif any(f in file_set for f in ("build.gradle", "build.gradle.kts")):
            tmpl = _TEMPLATES.get(("java", "gradle_spring"))
            if tmpl:
                return _resolve_template(tmpl, lang, "gradle_spring", file_set)

    # Try exact (lang, framework) match
    for fw in norm_fws:
        key = (lang, fw)
        tmpl = _TEMPLATES.get(key)
        if tmpl:
            return _resolve_template(tmpl, lang, fw, file_set)

    # Fall back: language-only match for Go (no specific framework needed)
    if lang == "go" and not norm_fws:
        tmpl = _TEMPLATES.get(("go", ""))
        if tmpl:
            return _resolve_template(tmpl, lang, "", file_set)

    return None


def _resolve_template(
    tmpl: dict, lang: str, fw: str, file_set: set[str]
) -> dict:
    """Resolve copy_deps/copy_src placeholders based on what files exist."""
    result = dict(tmpl)

    # Dependency files per language
    dep_files = {
        "python": ["requirements.txt", "pyproject.toml"],
        "javascript": ["package.json"],
        "typescript": ["package.json"],
        "java": [],  # handled by Maven/Gradle COPY
        "php": ["composer.json"],
        "go": ["go.mod"],
    }
    deps = [f for f in dep_files.get(lang, []) if f in file_set]
    deps_str = "\n".join(f"COPY {d} ./" for d in deps) if deps else ""
    src_files = [f for f in file_set if f not in deps]
    src_str = "\n".join(f"COPY {f} ./" for f in sorted(src_files)[:10]) if src_files else "COPY . ."

    build = result.get("build", "")
    runtime = result.get("runtime", "")

    if build:
        build = build.replace("{copy_deps}", deps_str).replace("{copy_src}", src_str)
    runtime = runtime.replace("{copy_deps}", deps_str).replace("{copy_src}", src_str)
    runtime = runtime.replace("{entry_file}", _guess_entry(file_set))

    # Compose final Dockerfile: builder stage + runtime stage
    dockerfile = build + "\n" + runtime if build else runtime
    result["dockerfile"] = dockerfile
    return result


def _guess_entry(file_set: set[str]) -> str:
    """Guess the main entry file for Node.js apps."""
    for candidate in ("server.js", "index.js", "app.js", "main.js"):
        if candidate in file_set:
            return candidate
    return "index.js"


def get_health_path(lang: str, fw: str) -> str:
    """Return known health path for a framework, or '/' as default."""
    key = (lang.lower(), _ALIASES.get(fw.lower(), fw.lower()))
    tmpl = _TEMPLATES.get(key)
    return tmpl["health_path"] if tmpl else "/"
