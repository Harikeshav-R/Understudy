"""Unit tests for scripts/check_mocks.py ensuring mock registry integrity."""

from pathlib import Path

from scripts.check_mocks import check_parity, parse_mock_registry, scan_source_markers


def test_scan_source_markers(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    py_file1 = src_dir / "mod_a.py"
    py_file1.write_text("# Normal comment\n# MOCKED: First mock description\nx = 1\n")
    py_file2 = src_dir / "mod_b.py"
    py_file2.write_text("y = 2\n")

    markers = scan_source_markers(src_dir)
    assert "mod_a.py" in markers
    assert len(markers["mod_a.py"]) == 1
    assert markers["mod_a.py"][0] == (2, "First mock description")
    assert "mod_b.py" not in markers


def test_scan_source_markers_nonexistent(tmp_path: Path) -> None:
    markers = scan_source_markers(tmp_path / "nonexistent")
    assert markers == {}


def test_parse_mock_registry(tmp_path: Path) -> None:
    mocks_file = tmp_path / "MOCKS.md"
    mocks_file.write_text(
        "# Mock Registry\n\n"
        "| Module | What is mocked | Why | Real path | Issue | Status |\n"
        "|---|---|---|---|---|---|\n"
        "| understudy.signals.datadog | Datadog | free tier | signals/datadog.py | #47 | open |\n"
    )
    modules = parse_mock_registry(mocks_file)
    assert modules == ["understudy.signals.datadog"]


def test_parse_mock_registry_placeholder(tmp_path: Path) -> None:
    mocks_file = tmp_path / "MOCKS.md"
    mocks_file.write_text(
        "# Mock Registry\n\n"
        "| Module | What is mocked | Why | Real path | Issue | Status |\n"
        "|---|---|---|---|---|---|\n"
        "| _(none yet)_ | | | | | |\n"
    )
    modules = parse_mock_registry(mocks_file)
    assert modules == []


def test_parse_mock_registry_nonexistent(tmp_path: Path) -> None:
    modules = parse_mock_registry(tmp_path / "nonexistent.md")
    assert modules == []


def test_check_parity_clean(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "clean.py").write_text("def foo(): pass\n")
    docs = tmp_path / "docs"
    docs.mkdir()
    mocks = docs / "MOCKS.md"
    mocks.write_text(
        "| Module | What | Why | Real | Issue | Status |\n"
        "|---|---|---|---|---|---|\n"
        "| _(none yet)_ | | | | | |\n"
    )

    ok, errors = check_parity(tmp_path, mocks)
    assert ok is True
    assert errors == []


def test_check_parity_unregistered_code_marker(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "unregistered.py").write_text("# MOCKED: secret mock\n")
    docs = tmp_path / "docs"
    docs.mkdir()
    mocks = docs / "MOCKS.md"
    mocks.write_text(
        "| Module | What | Why | Real | Issue | Status |\n"
        "|---|---|---|---|---|---|\n"
        "| _(none yet)_ | | | | | |\n"
    )

    ok, errors = check_parity(tmp_path, mocks)
    assert ok is False
    assert len(errors) == 1
    assert "Unregistered # MOCKED: marker" in errors[0]


def test_scan_source_markers_ignores_only_relative_excluded_dirs(tmp_path: Path) -> None:
    # An ancestor directory of search_dir that happens to start with "venv" must not
    # suppress markers found inside search_dir itself (only relative components count).
    search_dir = tmp_path / "venv_wrapper" / "src"
    search_dir.mkdir(parents=True)
    (search_dir / "real.py").write_text("# MOCKED: still findable\nx = 1\n")

    markers = scan_source_markers(search_dir)
    assert "real.py" in markers


def test_check_parity_overlapping_prefix_not_matched(tmp_path: Path) -> None:
    # A registered module must not match an unrelated file whose path merely shares
    # a prefix with it (e.g. "pagerduty" registered, marker lives in "pagerduty_v2.py").
    src = tmp_path / "src"
    src.mkdir()
    (src / "pagerduty_v2.py").write_text("# MOCKED: unrelated marker\n")
    docs = tmp_path / "docs"
    docs.mkdir()
    mocks = docs / "MOCKS.md"
    mocks.write_text(
        "| Module | What | Why | Real | Issue | Status |\n"
        "|---|---|---|---|---|---|\n"
        "| pagerduty | Pagerduty | free tier | pagerduty.py | #1 | open |\n"
    )

    ok, errors = check_parity(tmp_path, mocks)
    assert ok is False
    assert any("pagerduty_v2.py" in err for err in errors)


def test_check_parity_services_marker_unregistered(tmp_path: Path) -> None:
    services = tmp_path / "services"
    (services / "auth_service").mkdir(parents=True)
    (services / "auth_service" / "app.py").write_text("# MOCKED: synthetic token acceptance\n")
    docs = tmp_path / "docs"
    docs.mkdir()
    mocks = docs / "MOCKS.md"
    mocks.write_text(
        "| Module | What | Why | Real | Issue | Status |\n"
        "|---|---|---|---|---|---|\n"
        "| _(none yet)_ | | | | | |\n"
    )

    ok, errors = check_parity(tmp_path, mocks)
    assert ok is False
    assert len(errors) == 1
    assert "services/auth_service/app.py" in errors[0]


def test_check_parity_services_marker_registered(tmp_path: Path) -> None:
    services = tmp_path / "services"
    (services / "auth_service").mkdir(parents=True)
    (services / "auth_service" / "app.py").write_text("# MOCKED: synthetic token acceptance\n")
    docs = tmp_path / "docs"
    docs.mkdir()
    mocks = docs / "MOCKS.md"
    mocks.write_text(
        "| Module | What | Why | Real | Issue | Status |\n"
        "|---|---|---|---|---|---|\n"
        "| services.auth_service.app | Synthetic tokens | demo | app.py | #1 | open |\n"
    )

    ok, errors = check_parity(tmp_path, mocks)
    assert ok is True
    assert errors == []


def test_check_parity_stale_registry_row(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "clean.py").write_text("def foo(): pass\n")
    docs = tmp_path / "docs"
    docs.mkdir()
    mocks = docs / "MOCKS.md"
    mocks.write_text(
        "| Module | What | Why | Real | Issue | Status |\n"
        "|---|---|---|---|---|---|\n"
        "| understudy.fake.ghost | Ghost mock | ghost | ghost.py | #99 | open |\n"
    )

    ok, errors = check_parity(tmp_path, mocks)
    assert ok is False
    assert len(errors) == 1
    assert "Row in MOCKS.md for 'understudy.fake.ghost' has no corresponding" in errors[0]
