import subprocess
import json
import logging
import time
from pathlib import Path
from typing import Optional, Dict, List, Callable, Any

logger = logging.getLogger(__name__)

class TerraformRunner:
    def __init__(self, working_dir: Path):
        self.working_dir = working_dir
        self.working_dir.mkdir(parents=True, exist_ok=True)

    def _retry_with_backoff(
        self,
        func: Callable[[], Any],
        max_attempts: int = 3,
        base_delay: float = 2.0,
        delays: Optional[List[float]] = None,
    ) -> Any:
        last_exception = None
        for attempt in range(1, max_attempts + 1):
            try:
                return func()
            except Exception as e:
                last_exception = e
                if attempt < max_attempts:
                    if delays:
                        delay = delays[min(attempt - 1, len(delays) - 1)]
                    else:
                        delay = base_delay * (2 ** (attempt - 1))
                    logger.warning(f"Attempt {attempt} failed: {e}. Retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    logger.error(f"All {max_attempts} attempts failed")
        raise last_exception

    def _sanitize_cmd(self, cmd: List[str]) -> List[str]:
        allowed_commands = {
            "init", "plan", "apply", "destroy", "validate",
            "output", "fmt", "refresh", "show"
        }

        if not cmd or cmd[0] not in allowed_commands:
            raise ValueError(f"Invalid terraform command: {cmd[0] if cmd else 'empty'}")

        sanitized = []
        for arg in cmd:
            if not all(c.isalnum() or c in "-_=." for c in arg):
                raise ValueError(f"Invalid characters in argument: {arg}")
            sanitized.append(arg)

        return sanitized

    def _run(self, cmd: List[str]) -> subprocess.CompletedProcess:
        sanitized_cmd = self._sanitize_cmd(cmd)
        full_cmd = ["terraform"] + sanitized_cmd

        logger.info(f"Running: {' '.join(full_cmd)}")
        return subprocess.run(
            full_cmd,
            cwd=self.working_dir,
            capture_output=True,
            text=True,
            check=False,
            shell=False
        )

    def init(self) -> bool:
        def _init():
            # -input=false: a backend change (e.g. first init against the new
            # S3 remote state) prompts an interactive migration question that
            # hangs forever under non-interactive docker exec (no stdin).
            # -migrate-state -force-copy auto-approves that migration when
            # pre-existing local state is on disk (DEPLOYOPS_WORKSPACE_ROOT is
            # a persisted volume, so pre-bucket state can still be present).
            result = self._run(["init", "-input=false", "-migrate-state", "-force-copy"])
            if result.returncode != 0:
                raise RuntimeError(f"init failed: {result.stderr}")
            return True
        return self._retry_with_backoff(_init)

    def plan(self) -> Optional[Dict]:
        def _plan():
            # -input=false: never prompt for required variables in the automated pipeline
            result = self._run(["plan", "-input=false", "-json"])
            if result.returncode != 0:
                raise RuntimeError(f"plan failed: {result.stderr}")
            try:
                lines = result.stdout.strip().split('\n')
                for line in reversed(lines):
                    line = line.strip()
                    if line and line.startswith('{'):
                        parsed = json.loads(line)
                        # change_summary marks a successful plan
                        if parsed.get("type") == "change_summary":
                            return parsed
                        # planned_change and outputs also indicate success
                        if parsed.get("type") in ("planned_change", "outputs"):
                            return parsed
            except json.JSONDecodeError as e:
                raise RuntimeError(f"Failed to parse terraform plan JSON: {e}")
            return None
        return self._retry_with_backoff(_plan)

    def apply(self, auto_approve: bool = True) -> bool:
        def _apply():
            # -input=false: never prompt for required variables mid-apply.
            cmd = ["apply", "-input=false"]
            if auto_approve:
                cmd.append("-auto-approve")
            result = self._run(cmd)
            if result.returncode != 0:
                raise RuntimeError(f"apply failed: {result.stderr}")
            return True
        return self._retry_with_backoff(_apply)

    def destroy(self, auto_approve: bool = True) -> bool:
        def _destroy():
            cmd = ["destroy"]
            if auto_approve:
                cmd.append("-auto-approve")
            result = self._run(cmd)
            if result.returncode != 0:
                raise RuntimeError(f"destroy failed: {result.stderr}")
            return True
        # Destroy failures are often AWS eventual-consistency issues (e.g. a
        # lingering ENI blocking security-group deletion with
        # DependencyViolation) that clear on the order of tens of seconds,
        # not the 2s/4s/8s used for apply/init/plan -- so destroy gets a
        # slower, explicit backoff (feature/destroy-deployment decision).
        return self._retry_with_backoff(_destroy, max_attempts=4, delays=[10.0, 30.0, 60.0])

    def output(self) -> Dict:
        result = self._run(["output", "-json"])
        if result.returncode != 0:
            return {}
        return json.loads(result.stdout)

    def validate(self) -> bool:
        result = self._run(["validate"])
        return result.returncode == 0

    def fmt(self, recursive: bool = True) -> bool:
        cmd = ["fmt"]
        if recursive:
            cmd.append("-recursive")
        result = self._run(cmd)
        return result.returncode == 0