import json
from pathlib import Path

import pytest

from agentInfraCost.main import build_parser, main

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_gemini_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


class TestRunCommand:
    def test_writes_pending_output_json_to_file(self, tmp_path: Path) -> None:
        input_path = FIXTURES_DIR / "sample_input.json"
        out_path = tmp_path / "output.json"

        exit_code = main(["run", str(input_path), "--out", str(out_path)])

        assert exit_code == 0
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["compute_type"] == "ecs"
        assert payload["approval"]["status"] == "pending"
        assert payload["aws_config"]["lambda"] is None
        assert payload["aws_config"]["ec2"] is None

    def test_prints_to_stdout_when_no_out_given(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        input_path = FIXTURES_DIR / "sample_input_variant_lambda_candidate.json"

        exit_code = main(["run", str(input_path)])

        assert exit_code == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["compute_type"] == "lambda"

    def test_custom_region_and_environment_are_applied(self, tmp_path: Path) -> None:
        input_path = FIXTURES_DIR / "sample_input.json"
        out_path = tmp_path / "output.json"

        main(
            [
                "run",
                str(input_path),
                "--region",
                "eu-west-1",
                "--environment",
                "prod",
                "--out",
                str(out_path),
            ]
        )

        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["aws_config"]["region"] == "eu-west-1"
        assert payload["artifacts"]["terraform"]["variables"]["environment"] == "prod"

    def test_low_confidence_fixture_returns_nonzero_exit_code(self) -> None:
        input_path = FIXTURES_DIR / "sample_input_variant_low_confidence.json"

        exit_code = main(["run", str(input_path)])

        assert exit_code == 1


class TestApproveAndRejectCommands:
    def _produce_pending_output(self, tmp_path: Path) -> Path:
        input_path = FIXTURES_DIR / "sample_input.json"
        out_path = tmp_path / "pending.json"
        main(["run", str(input_path), "--out", str(out_path)])
        return out_path

    def test_approve_transitions_to_approved(self, tmp_path: Path) -> None:
        pending_path = self._produce_pending_output(tmp_path)
        approved_path = tmp_path / "approved.json"

        exit_code = main(
            [
                "approve",
                str(pending_path),
                "--approved-by",
                "user@example.com",
                "--out",
                str(approved_path),
            ]
        )

        assert exit_code == 0
        payload = json.loads(approved_path.read_text(encoding="utf-8"))
        assert payload["approval"]["status"] == "approved"
        assert payload["approval"]["approved_by"] == "user@example.com"

    def test_reject_transitions_to_rejected(self, tmp_path: Path) -> None:
        pending_path = self._produce_pending_output(tmp_path)
        rejected_path = tmp_path / "rejected.json"

        exit_code = main(["reject", str(pending_path), "--out", str(rejected_path)])

        assert exit_code == 0
        payload = json.loads(rejected_path.read_text(encoding="utf-8"))
        assert payload["approval"]["status"] == "rejected"

    def test_approving_an_already_approved_file_returns_nonzero_exit_code(
        self, tmp_path: Path
    ) -> None:
        pending_path = self._produce_pending_output(tmp_path)
        approved_path = tmp_path / "approved.json"
        main(
            [
                "approve",
                str(pending_path),
                "--approved-by",
                "user@example.com",
                "--out",
                str(approved_path),
            ]
        )

        exit_code = main(
            ["approve", str(approved_path), "--approved-by", "someone-else@example.com"]
        )

        assert exit_code == 1

    def test_approve_requires_approved_by_flag(self, tmp_path: Path) -> None:
        pending_path = self._produce_pending_output(tmp_path)
        with pytest.raises(SystemExit):
            main(["approve", str(pending_path)])


class TestArgumentParserDefaults:
    def test_run_defaults_match_pipeline_constants(self) -> None:
        from agentInfraCost.core.pipeline import DEFAULT_ENVIRONMENT, DEFAULT_REGION

        parser = build_parser()
        args = parser.parse_args(["run", "input.json"])
        assert args.region == DEFAULT_REGION
        assert args.environment == DEFAULT_ENVIRONMENT
