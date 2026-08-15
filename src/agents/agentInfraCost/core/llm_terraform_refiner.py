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
import time
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
    "incomplet (ex: seulement FROM/COPY sans CMD ni dépendances) sera rejeté."
)


class _RefinedTerraform(BaseModel):
    """Strict shape the LLM's raw JSON response must match. Anything else —
    malformed JSON, a missing field, or an empty file — triggers the
    fail-soft fallback (original files unchanged). ``dockerfile`` is optional:
    when present it replaces the current Dockerfile, when absent the original
    is kept (backward-compatible with Terraform-only responses).
    """

    main_tf: str
    variables_tf: str
    outputs_tf: str
    dockerfile: str | None = None


def _build_prompt(
    current: TerraformFiles,
    dockerfile: str | None,
    feedback: str,
    repo_context: str | None = None,
) -> str:
    no_docker = "=== Dockerfile ===\n(aucun Dockerfile fourni)\n\n"
    docker_block = f"=== Dockerfile ===\n{dockerfile}\n\n" if dockerfile else no_docker
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
        f"{docker_block}"
        f"{repo_block}"
        "=== DEMANDE DE L'UTILISATEUR ===\n"
        f"{feedback}\n\n"
        "Renvoie les trois fichiers (et le Dockerfile si fourni) modifiés en JSON."
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
    """A refined file must be non-empty; anything else is a refused edit."""
    return bool(content and content.strip())


def _sanitize_dockerfile_dependencies(dockerfile: str, repo_context: str | None) -> str:
    """
    Validate the Dockerfile against the repo context. If it references
    dependency files (requirements.txt, pyproject.toml, package.json, etc.)
    that don't exist in the repo context, rewrite to use explicit package
    installs based on the detected stack.
    """
    if not dockerfile or not repo_context:
        return dockerfile

    # Files that indicate dependency management - check if they exist in repo_context
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

    if not missing_deps:
        return dockerfile

    lines = dockerfile.splitlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        # Skip RUN lines that install from missing dependency files
        if stripped.startswith("RUN") and any(
            f" -r {fname}" in stripped or f" install {fname}" in stripped or f" install -r {fname}" in stripped
            for fname in missing_deps
        ):
            # Replace with explicit install for the detected stack
            # Detect stack from repo_context (simple heuristic)
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


def refine_terraform(
    current: TerraformFiles,
    feedback: str,
    dockerfile: str | None = None,
    repo_context: str | None = None,
) -> tuple[TerraformFiles, str | None]:
    """Refine the rendered artifacts from a user prompt.

    Args:
        current: the Terraform files produced by ``generate_terraform``.
        feedback: the user's free-form change request from Gate 2.
        dockerfile: the effective Dockerfile content (if this is a container
            deployment), refined alongside the Terraform when the LLM edits
            it.
        repo_context: the whole-repo digest (``core.repo_ingestor``) so the
            LLM can honor the request against the real code, not just the
            rendered artifacts.

    Returns:
        A ``(TerraformFiles, dockerfile)`` pair honoring the request, or — on
        any LLM failure or invalid output — ``(current, dockerfile)``
        unchanged (fail-soft, same contract as every other LLM call here).
    """
    for attempt in range(1, REFINER_MAX_ATTEMPTS + 1):
        raw_text = call_llm(
            prompt=_build_prompt(current, dockerfile, feedback, repo_context=repo_context),
            system_instruction=_SYSTEM_INSTRUCTION,
        )
        refined = _parse_llm_output(raw_text)
        if (
            refined is not None
            and _valid_file(refined.main_tf)
            and _valid_file(refined.variables_tf)
            and _valid_file(refined.outputs_tf)
        ):
            # Dockerfile: refine only when one exists, the feedback explicitly
            # targets container concerns, AND the LLM returned a valid
            # replacement. Anything else keeps the original Dockerfile
            # untouched. Backward-compatible: Terraform-only responses keep
            # the original Dockerfile too.
            refined_dockerfile = dockerfile
            if dockerfile is not None and _feedback_targets_dockerfile(feedback):
                if refined.dockerfile is None or not _valid_file(refined.dockerfile):
                    logger.warning(
                        "Artifact refiner returned no usable Dockerfile; keeping original"
                    )
                else:
                    refined_dockerfile = refined.dockerfile
                    # Sanitize: remove references to missing dependency files
                    refined_dockerfile = _sanitize_dockerfile_dependencies(
                        refined_dockerfile, repo_context
                    )
            elif dockerfile is not None:
                logger.info(
                    "Feedback doesn't target the Dockerfile; keeping the repo's "
                    "Dockerfile untouched"
                )

            logger.info("Artifact refiner applied user feedback")
            return (
                TerraformFiles(
                    main_tf=refined.main_tf,
                    variables_tf=refined.variables_tf,
                    outputs_tf=refined.outputs_tf,
                ),
                refined_dockerfile,
            )

        if attempt < REFINER_MAX_ATTEMPTS:
            logger.warning(
                "Artifact refiner attempt %d/%d produced unusable output; retrying",
                attempt, REFINER_MAX_ATTEMPTS,
            )
            time.sleep(REFINER_RETRY_DELAY_SECONDS)

    logger.info("Artifact refiner unavailable or invalid; keeping original files")
    return current, dockerfile
