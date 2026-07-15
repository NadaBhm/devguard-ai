"""
Class `TerraformRunner` to run Terraform commands in a specified working directory.

Example usage:

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
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

class TerraformRunner:
    def __init__(self, working_dir: Path):
        self.working_dir = working_dir
        self.working_dir.mkdir(parents=True, exist_ok=True)
    
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
    
    def fmt(self, recursive: bool = True) -> bool:
        cmd = ["fmt"]
        if recursive:
            cmd.append("-recursive")
        result = self._run(cmd)
        return result.returncode == 0