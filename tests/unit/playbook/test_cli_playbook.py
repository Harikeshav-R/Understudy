"""Unit tests for ust playbook CLI commands (seed, match, list)."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from understudy.cli import app

runner = CliRunner()


def test_cli_playbook_seed_fake(tmp_path: Path) -> None:
    seed_data = [
        {
            "playbook_id": "pb_test_seed",
            "failure_class": "bad_deploy",
            "signature_text": "failure_class: bad_deploy\naffected_service: data-service",
            "plan": {
                "plan_id": "plan_test_01",
                "candidate_index": 0,
                "action": "restart_workload",
                "params": {"workload": "data-service"},
                "target_resources": [
                    {"namespace": "ust-twin", "kind": "Deployment", "name": "data-service"}
                ],
                "declared_blast_set": ["data-service"],
                "inverse": None,
                "rationale": "Restart",
                "origin": "playbook",
            },
            "evidence_refs": ["run_1"],
            "origin": "seed",
        }
    ]
    seed_file = tmp_path / "seed.json"
    seed_file.write_text(json.dumps(seed_data), encoding="utf-8")

    result = runner.invoke(app, ["playbook", "seed", "--from", str(seed_file), "--fake"])
    assert result.exit_code == 0
    assert "Seeded 1 playbook(s)" in result.stdout


def test_cli_playbook_seed_file_not_found() -> None:
    result = runner.invoke(app, ["playbook", "seed", "--from", "nonexistent.json"])
    assert result.exit_code == 1
    assert "Seed file not found" in result.stderr


def test_cli_playbook_seed_invalid_json(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not json", encoding="utf-8")

    result = runner.invoke(app, ["playbook", "seed", "--from", str(bad_file)])
    assert result.exit_code == 1
    assert "Failed to parse seed playbooks JSON" in result.stderr


def test_cli_playbook_seed_store_failure(tmp_path: Path) -> None:
    seed_data = [
        {
            "playbook_id": "pb_1",
            "failure_class": "bad_deploy",
            "signature_text": "sig",
            "plan": {
                "plan_id": "p1",
                "candidate_index": 0,
                "action": "no_action",
                "params": {"workload": "data-service"},
                "target_resources": [],
                "declared_blast_set": [],
                "inverse": None,
                "rationale": "None",
                "origin": "playbook",
            },
        }
    ]
    f = tmp_path / "s.json"
    f.write_text(json.dumps(seed_data), encoding="utf-8")

    with patch(
        "understudy.store.postgres.PostgresPlaybookStore.save_playbook",
        new_callable=AsyncMock,
    ) as mock_save:
        mock_save.side_effect = RuntimeError("DB connection error")
        result = runner.invoke(app, ["playbook", "seed", "--from", str(f)])
        assert result.exit_code == 1
        assert "Failed to seed playbooks into store" in result.stderr


def test_cli_playbook_match_fake() -> None:
    context_file = Path("fixtures/context_bad_deploy.json")
    assert context_file.exists()

    result = runner.invoke(app, ["playbook", "match", "--context", str(context_file), "--fake"])
    assert result.exit_code == 0
    assert "Matched playbook 'pb_bad_deploy_data_service'" in result.stdout
    assert "cosine: 1.00 > 0.8" in result.stdout
    assert "Confirmation Reason:" in result.stdout


def test_cli_playbook_match_fake_json() -> None:
    context_file = Path("fixtures/context_bad_deploy.json")
    result = runner.invoke(
        app, ["playbook", "match", "--context", str(context_file), "--fake", "--json"]
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["matched"] is True
    assert data["playbook_id"] == "pb_bad_deploy_data_service"
    assert data["similarity"] >= 0.8


def test_cli_playbook_match_file_not_found() -> None:
    result = runner.invoke(app, ["playbook", "match", "--context", "missing.json"])
    assert result.exit_code == 1
    assert "Context file not found" in result.stderr


def test_cli_playbook_match_invalid_json(tmp_path: Path) -> None:
    bad_ctx = tmp_path / "bad_ctx.json"
    bad_ctx.write_text("invalid json", encoding="utf-8")

    result = runner.invoke(app, ["playbook", "match", "--context", str(bad_ctx)])
    assert result.exit_code == 1
    assert "Failed to parse IncidentContext" in result.stderr


def test_cli_playbook_match_rejection() -> None:
    context_file = Path("fixtures/context_bad_deploy.json")
    with patch(
        "understudy.playbook.confirmation.PlaybookConfirmer.confirm",
        new_callable=AsyncMock,
    ) as mock_confirm:
        from understudy.playbook.confirmation import ConfirmationResult

        mock_confirm.return_value = ConfirmationResult(
            retained_playbook_id=None,
            reason="Rejected by mock arbiter",
            confidence=0.5,
        )
        result = runner.invoke(app, ["playbook", "match", "--context", str(context_file), "--fake"])
        assert result.exit_code == 0
        assert "No playbook match confirmed: Rejected by mock arbiter" in result.stdout


def test_cli_playbook_match_store_failure() -> None:
    context_file = Path("fixtures/context_bad_deploy.json")
    with patch(
        "understudy.store.postgres.PostgresPlaybookStore.search_playbooks_with_scores",
        new_callable=AsyncMock,
    ) as mock_search:
        mock_search.side_effect = RuntimeError("DB connection error")
        with patch(
            "understudy.playbook.embeddings.OpenRouterEmbeddingClient.embed",
            new_callable=AsyncMock,
        ) as mock_embed:
            mock_embed.return_value = [0.1] * 1024
            result = runner.invoke(app, ["playbook", "match", "--context", str(context_file)])
            assert result.exit_code == 1
            assert "Playbook match error" in result.stderr


def test_cli_playbook_list_fake() -> None:
    result = runner.invoke(app, ["playbook", "list", "--fake"])
    assert result.exit_code == 0
    assert "Stored Playbooks" in result.stdout
    assert "pb_bad_deploy_data_service" in result.stdout


def test_cli_playbook_list_fake_json() -> None:
    result = runner.invoke(app, ["playbook", "list", "--fake", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) >= 1
    assert any(item["playbook_id"] == "pb_bad_deploy_data_service" for item in data)


def test_cli_playbook_list_failure() -> None:
    with patch(
        "understudy.store.postgres.PostgresPlaybookStore.list_playbooks",
        new_callable=AsyncMock,
    ) as mock_list:
        mock_list.side_effect = RuntimeError("DB unreachable")
        result = runner.invoke(app, ["playbook", "list"])
        assert result.exit_code == 1
        assert "Failed to list playbooks" in result.stderr


def test_cli_playbook_seed_single_dict_with_embedding(tmp_path: Path) -> None:
    single_seed = {
        "playbook_id": "pb_single",
        "failure_class": "bad_deploy",
        "signature_text": "sig",
        "embedding": [0.05] * 1024,
        "plan": {
            "plan_id": "p_single",
            "candidate_index": 0,
            "action": "no_action",
            "params": {"workload": "data-service"},
            "target_resources": [],
            "declared_blast_set": [],
            "inverse": None,
            "rationale": "None",
            "origin": "playbook",
        },
    }
    f = tmp_path / "single.json"
    f.write_text(json.dumps(single_seed), encoding="utf-8")
    result = runner.invoke(app, ["playbook", "seed", "--from", str(f), "--fake"])
    assert result.exit_code == 0
    assert "Seeded 1 playbook(s)" in result.stdout


def test_cli_playbook_match_fake_no_seed_fixture() -> None:
    context_file = Path("fixtures/context_bad_deploy.json")
    original_exists = Path.exists

    def mock_exists(self: Path) -> bool:
        if "playbooks_seed.json" in str(self):
            return False
        return original_exists(self)

    with patch("pathlib.Path.exists", autospec=True, side_effect=mock_exists):
        result = runner.invoke(app, ["playbook", "match", "--context", str(context_file), "--fake"])
        assert result.exit_code == 0
        assert "No candidate playbooks found" in result.stdout


def test_cli_playbook_list_fake_no_seed_fixture() -> None:
    original_exists = Path.exists

    def mock_exists(self: Path) -> bool:
        if "playbooks_seed.json" in str(self):
            return False
        return original_exists(self)

    with patch("pathlib.Path.exists", autospec=True, side_effect=mock_exists):
        result = runner.invoke(app, ["playbook", "list", "--fake"])
        assert result.exit_code == 0
        assert "Stored Playbooks (0):" in result.stdout
