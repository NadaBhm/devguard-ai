"""

    this file provides a class `TerraformRunner` to run Terraform commands in a specified working directory.
    example usage:
    
    from pathlib import Path
    from src.lib.terraform.runner import TerraformRunner
    
    working_dir = Path("/path/to/terraform/configs")
    runner = TerraformRunner(working_dir)
    if runner.init():
        plan = runner.plan()
        if plan:
            print("Plan successful:", plan)
            if runner.apply():
                print("Apply successful")
    
"""

import subprocess
import json
import logging
from pathlib import Path
from typing import Optional, Dict

logger = logging.getLogger(__name__)

class TerraformRunner:
    def __init__(self, working_dir: Path):
        self.working_dir = working_dir
        self.working_dir.mkdir(parents=True, exist_ok=True)
    
    def _run(self, cmd: list) -> subprocess.CompletedProcess:
        full_cmd = ["terraform"] + cmd
        logger.info(f"Running: {' '.join(full_cmd)}")
        return subprocess.run(
            full_cmd,
            cwd=self.working_dir,
            capture_output=True,
            text=True,
            check=False
        )
    
    def init(self) -> bool:
        result = self._run(["init"])
        if result.returncode != 0:
            logger.error(f"init failed: {result.stderr}")
            return False
        return True
    
    def plan(self) -> Optional[Dict]:
        result = self._run(["plan", "-json"])
        if result.returncode != 0:
            logger.error(f"plan failed: {result.stderr}")
            return None
        return json.loads(result.stdout)
    
    def apply(self, auto_approve: bool = True) -> bool:
        cmd = ["apply"]
        if auto_approve:
            cmd.append("-auto-approve")
        result = self._run(cmd)
        if result.returncode != 0:
            logger.error(f"apply failed: {result.stderr}")
            return False
        return True
    
    def destroy(self, auto_approve: bool = True) -> bool:
        cmd = ["destroy"]
        if auto_approve:
            cmd.append("-auto-approve")
        result = self._run(cmd)
        if result.returncode != 0:
            logger.error(f"destroy failed: {result.stderr}")
            return False
        return True
    
    def output(self) -> Dict:
        result = self._run(["output", "-json"])
        if result.returncode != 0:
            return {}
        return json.loads(result.stdout)
    
    def validate(self) -> bool:
        result = self._run(["validate"])
        return result.returncode == 0