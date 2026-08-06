"""Shared test fixtures."""
import pytest
from pathlib import Path


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """Create a fake repo with README and Python file."""
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    
    readme = repo / "README.md"
    readme.write_text("# Test Repo\n\nThis is a Python project.")
    
    main_py = repo / "main.py"
    main_py.write_text("def hello(): return 'world'\n")
    
    return repo