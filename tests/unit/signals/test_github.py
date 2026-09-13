"""Unit tests for GitHubDeployHistory version control adapter."""

from datetime import UTC, datetime

import httpx
import pytest

from understudy.common.clock import FrozenClock
from understudy.common.errors import GitHubError
from understudy.signals.api import DeployHistory
from understudy.signals.github import (
    GitHubDeployHistory,
    _extract_container_digest,
    _extract_pr_number_from_message,
    _is_migration_file,
    _parse_iso_datetime,
    _parse_manifest_digests,
)

FIXED_NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def test_protocol_conformance() -> None:
    history = GitHubDeployHistory()
    assert isinstance(history, DeployHistory)


def test_parse_iso_datetime() -> None:
    # ISO with Z
    dt_z = _parse_iso_datetime("2026-09-13T10:30:00Z")
    assert dt_z == datetime(2026, 9, 13, 10, 30, 0, tzinfo=UTC)

    # ISO with +00:00 offset
    dt_offset = _parse_iso_datetime("2026-09-13T10:30:00+00:00")
    assert dt_offset == datetime(2026, 9, 13, 10, 30, 0, tzinfo=UTC)

    # Naive timestamp string
    dt_naive = _parse_iso_datetime("2026-09-13 10:30:00")
    assert dt_naive.tzinfo == UTC

    # Non-UTC offset converts to UTC
    dt_est = _parse_iso_datetime("2026-09-13T10:30:00-04:00")
    assert dt_est == datetime(2026, 9, 13, 14, 30, 0, tzinfo=UTC)

    # Invalid timestamp
    with pytest.raises(GitHubError, match="Failed to parse commit timestamp"):
        _parse_iso_datetime("not-a-datetime")


def test_extract_pr_number_from_message() -> None:
    # Standard merge PR
    assert (
        _extract_pr_number_from_message(
            "Merge pull request #24 from Harikeshav-R/feat/phase-2a-fleet"
        )
        == 24
    )
    assert _extract_pr_number_from_message("merge pull request #101 from foo/bar") == 101

    # Squash PR format
    assert _extract_pr_number_from_message("feat(store): add models and migrations (#42)") == 42
    assert (
        _extract_pr_number_from_message(
            "fix(deploy): configmap wiring (#123)\n\nDetailed explanation"
        )
        == 123
    )

    # Generic PR format
    assert _extract_pr_number_from_message("resolves pull request #55") == 55

    # No PR reference
    assert _extract_pr_number_from_message("simple commit without PR reference") is None
    assert _extract_pr_number_from_message("") is None


def test_is_migration_file() -> None:
    assert _is_migration_file("src/understudy/store/migrations/0001_initial.py") is True
    assert _is_migration_file("migrations/versions/001.sql") is True
    assert _is_migration_file("deploy/migrations") is True
    assert _is_migration_file("migrations") is True
    assert _is_migration_file("src\\understudy\\store\\migrations\\0001.py") is True

    assert _is_migration_file("src/understudy/store/models.py") is False
    assert _is_migration_file("deploy/prod/data-service.yaml") is False
    assert _is_migration_file("") is False


def test_extract_container_digest() -> None:
    # Pinned image spec with @sha256:
    d_hex = "1ead9b47c5fe97d3551cc5001844061122dcc49388dcf77ebda51a3b6301f6b7"
    pinned = f"localhost:5001/data-service@sha256:{d_hex}"
    assert _extract_container_digest(pinned) == f"sha256:{d_hex}"

    # Bare sha256 prefix
    assert _extract_container_digest("sha256:abcdef1234567890") == "sha256:abcdef1234567890"

    # Unpinned image reference generates deterministic 71-char sha256 digest
    unpinned = "localhost:5001/data-service:good"
    digest = _extract_container_digest(unpinned)
    assert digest.startswith("sha256:")
    assert len(digest) == 71
    # Monotonic and deterministic
    assert _extract_container_digest(unpinned) == digest


def test_parse_manifest_digests() -> None:
    yaml_text = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: data-service
spec:
  template:
    spec:
      containers:
        - name: data-service
          image: localhost:5001/data-service@sha256:abc123def456
---
apiVersion: v1
kind: Service
metadata:
  name: data-service
spec:
  ports:
    - port: 8000
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: auth-service
spec:
  template:
    spec:
      containers:
        - name: auth-service
          image: localhost:5001/auth-service:good
"""
    digests = _parse_manifest_digests(yaml_text)
    assert digests["data-service"] == "sha256:abc123def456"
    assert digests["auth-service"].startswith("sha256:")
    assert len(digests["auth-service"]) == 71

    # Malformed / empty YAML handled safely
    assert _parse_manifest_digests("") == {}
    assert _parse_manifest_digests("not: valid: yaml: [") == {}

    # Deployment missing containers or invalid structure
    assert _parse_manifest_digests("apiVersion: apps/v1\nkind: Deployment\n") == {}
    assert _parse_manifest_digests("apiVersion: apps/v1\nkind: Deployment\nspec: 123") == {}

    bad_specs = [
        "apiVersion: apps/v1\nkind: Deployment\nspec:\n  template: 123",
        "apiVersion: apps/v1\nkind: Deployment\nspec:\n  template:\n    spec: 123",
        (
            "apiVersion: apps/v1\nkind: Deployment\nspec:\n"
            "  template:\n    spec:\n      containers: 'x'"
        ),
        (
            "apiVersion: apps/v1\nkind: Deployment\nspec:\n"
            "  template:\n    spec:\n      containers:\n        - 'x'"
        ),
        (
            "apiVersion: apps/v1\nkind: Deployment\nspec:\n  template:\n    spec:\n"
            "      containers:\n        - name: 123\n          image: null"
        ),
    ]
    for spec_yaml in bad_specs:
        assert _parse_manifest_digests(spec_yaml) == {}


def test_token_and_repo_resolution() -> None:
    # Explicit token and repo
    h1 = GitHubDeployHistory(token="token123", repo="custom/repo", branch="feat")
    assert h1.token == "token123"
    assert h1.repo == "custom/repo"
    assert h1.branch == "feat"

    headers1 = h1._get_headers()
    assert headers1["Authorization"] == "Bearer token123"

    # Template token ignored
    h2 = GitHubDeployHistory(token="ghp_...", repo="test/repo")
    assert h2.token is None
    headers2 = h2._get_headers()
    assert "Authorization" not in headers2

    # Blank token string ignored
    h3 = GitHubDeployHistory(token="   ", repo="test/repo")
    assert h3.token is None


@pytest.mark.asyncio
async def test_recent_deploys_limit_bounds() -> None:
    clock = FrozenClock(FIXED_NOW)
    history = GitHubDeployHistory(clock=clock)
    assert await history.recent_deploys(limit=0) == []
    assert await history.recent_deploys(limit=-1) == []


@pytest.mark.asyncio
async def test_recent_deploys_successful_flow() -> None:
    clock = FrozenClock(FIXED_NOW)

    d_hex = "1111222233334444555566667777888899990000111122223333444455556666"
    manifest_yaml = f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: data-service
spec:
  template:
    spec:
      containers:
        - name: data-service
          image: localhost:5001/data-service@sha256:{d_hex}
"""

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)

        # 1. Commit listing
        if url_str.endswith("/repos/test-owner/test-repo/commits?per_page=2"):
            return httpx.Response(
                200,
                json=[
                    {
                        "sha": "c0ffee1111111111111111111111111111111111",
                        "commit": {
                            "committer": {"date": "2026-09-13T10:00:00Z"},
                            "message": "feat(store): initial migration (#101)",
                        },
                    },
                    {
                        "sha": "c0ffee2222222222222222222222222222222222",
                        "commit": {
                            "author": {"date": "2026-09-13T09:00:00Z"},
                            "message": "feat(service): normal commit without pr in msg",
                        },
                    },
                ],
            )

        # 2. PR pulls endpoint for commit 2
        if "/commits/c0ffee2222222222222222222222222222222222/pulls" in url_str:
            return httpx.Response(200, json=[{"number": 99, "title": "PR 99"}])

        # 3. Commit detail for commit 1 (contains migration)
        if url_str.endswith("/commits/c0ffee1111111111111111111111111111111111"):
            return httpx.Response(
                200,
                json={
                    "files": [
                        {"filename": "src/understudy/store/migrations/0001_initial.py"},
                        {"filename": "src/understudy/store/models.py"},
                    ]
                },
            )

        # 4. Commit detail for commit 2 (no migration)
        if url_str.endswith("/commits/c0ffee2222222222222222222222222222222222"):
            return httpx.Response(
                200,
                json={"files": [{"filename": "src/understudy/service/app.py"}]},
            )

        # 5. Manifest directory contents
        if "/contents/deploy/prod?" in url_str:
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "data-service.yaml",
                        "path": "deploy/prod/data-service.yaml",
                        "sha": "blob_sha_123",
                        "type": "file",
                    },
                    {
                        "name": "dependencies.yaml",
                        "path": "deploy/prod/dependencies.yaml",
                        "sha": "blob_sha_456",
                        "type": "file",
                    },
                    {
                        "name": "subfolder",
                        "path": "deploy/prod/subfolder",
                        "sha": "tree_sha_789",
                        "type": "dir",
                    },
                ],
            )

        # 6. Manifest file raw text
        if "/contents/deploy/prod/data-service.yaml?" in url_str:
            return httpx.Response(200, text=manifest_yaml)

        return httpx.Response(404, text="Not Found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        history = GitHubDeployHistory(
            token="test-token",
            repo="test-owner/test-repo",
            client=http_client,
            clock=clock,
        )

        deploys = await history.recent_deploys(limit=2)
        assert len(deploys) == 2

        # Commit 1 assertions
        d1 = deploys[0]
        assert d1.commit_sha == "c0ffee1111111111111111111111111111111111"
        assert d1.deployed_at == datetime(2026, 9, 13, 10, 0, 0, tzinfo=UTC)
        assert d1.pr_number == 101
        assert d1.contains_migration is True
        assert d1.image_digests == {"data-service": f"sha256:{d_hex}"}

        # Commit 2 assertions
        d2 = deploys[1]
        assert d2.commit_sha == "c0ffee2222222222222222222222222222222222"
        assert d2.deployed_at == datetime(2026, 9, 13, 9, 0, 0, tzinfo=UTC)
        assert d2.pr_number == 99
        assert d2.contains_migration is False
        assert d2.image_digests == {"data-service": f"sha256:{d_hex}"}


@pytest.mark.asyncio
async def test_recent_deploys_branch_parameter() -> None:
    requested_url = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested_url
        requested_url = str(request.url)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        history = GitHubDeployHistory(
            repo="owner/repo",
            branch="feat/test-branch",
            client=http_client,
        )
        await history.recent_deploys(limit=1)
        assert "sha=feat%2Ftest-branch" in requested_url or "sha=feat/test-branch" in requested_url


@pytest.mark.asyncio
async def test_recent_deploys_pr_resolution_fallbacks() -> None:
    call_counts = {"pulls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/commits?per_page=1"):
            return httpx.Response(
                200,
                json=[{"sha": "c0ffee1", "commit": {"message": "no pr in message"}}],
            )
        if "/commits/c0ffee1/pulls" in url_str:
            call_counts["pulls"] += 1
            if call_counts["pulls"] == 1:
                return httpx.Response(404, text="Not Found")
            if call_counts["pulls"] == 2:
                return httpx.Response(422, text="Unprocessable")
            if call_counts["pulls"] == 3:
                return httpx.Response(200, json=[])
            if call_counts["pulls"] == 4:
                return httpx.Response(200, json=["not-a-dict"])
            if call_counts["pulls"] == 5:
                return httpx.Response(200, json=[{"not_number": 1}])
            return httpx.Response(500, text="GitHub Server Error")

        # Mock detail and contents empty
        if url_str.endswith("/commits/c0ffee1"):
            return httpx.Response(200, json={"files": []})
        if "/contents/deploy/prod?" in url_str:
            return httpx.Response(200, json=[])
        return httpx.Response(404, text="Not Found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        history = GitHubDeployHistory(repo="owner/repo", client=http_client)

        # 1. 404 yields None
        deploys = await history.recent_deploys(limit=1)
        assert deploys[0].pr_number is None

        # 2. 422 yields None
        deploys = await history.recent_deploys(limit=1)
        assert deploys[0].pr_number is None

        # 3. Empty list yields None
        deploys = await history.recent_deploys(limit=1)
        assert deploys[0].pr_number is None

        # 4. Non-dict list item yields None
        deploys = await history.recent_deploys(limit=1)
        assert deploys[0].pr_number is None

        # 5. Dict without "number" yields None
        deploys = await history.recent_deploys(limit=1)
        assert deploys[0].pr_number is None

        # 6. 500 raises GitHubError
        with pytest.raises(GitHubError, match="GitHub commit pulls query failed: 500"):
            await history.recent_deploys(limit=1)

        # Test direct resolution edge cases for missing commit_sha or non-str message
        assert await history._resolve_pr_number({"sha": None, "commit": {}}) is None
        assert await history._resolve_pr_number({"sha": None, "commit": {"message": 12345}}) is None


@pytest.mark.asyncio
async def test_recent_deploys_commit_detail_edge_cases() -> None:
    mode = "500"

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/commits?per_page=1"):
            return httpx.Response(
                200,
                json=[{"sha": "c0ffee1", "commit": {"message": "fix (#1)"}}],
            )
        if url_str.endswith("/commits/c0ffee1"):
            if mode == "500":
                return httpx.Response(500, text="Detail 500")
            if mode == "non_dict":
                return httpx.Response(200, json=["unexpected"])
            if mode == "non_list_files":
                return httpx.Response(200, json={"files": "not-a-list"})
            if mode == "non_dict_file":
                return httpx.Response(200, json={"files": ["not-a-dict"]})
            if mode == "empty_filename":
                return httpx.Response(200, json={"files": [{"filename": None}]})
            if mode == "bad_json":
                return httpx.Response(200, text="invalid-json-syntax{")
        if "/contents/deploy/prod?" in url_str:
            return httpx.Response(200, json=[])
        return httpx.Response(404, text="Not Found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        history = GitHubDeployHistory(repo="owner/repo", client=http_client)

        # 500 raises GitHubError
        with pytest.raises(GitHubError, match="GitHub commit detail query failed: 500"):
            await history.recent_deploys(limit=1)

        # Non-dict returns contains_migration=False
        mode = "non_dict"
        deploys = await history.recent_deploys(limit=1)
        assert deploys[0].contains_migration is False

        # Non-list files returns contains_migration=False
        mode = "non_list_files"
        deploys = await history.recent_deploys(limit=1)
        assert deploys[0].contains_migration is False

        # Non-dict file items handled
        mode = "non_dict_file"
        deploys = await history.recent_deploys(limit=1)
        assert deploys[0].contains_migration is False

        # Empty filename handled
        mode = "empty_filename"
        deploys = await history.recent_deploys(limit=1)
        assert deploys[0].contains_migration is False

        # Bad JSON decode raises GitHubError
        mode = "bad_json"
        with pytest.raises(GitHubError, match="Failed to parse commit detail files JSON"):
            await history.recent_deploys(limit=1)


@pytest.mark.asyncio
async def test_recent_deploys_manifest_directory_edge_cases() -> None:
    mode = "404"

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/commits?per_page=1"):
            return httpx.Response(
                200,
                json=[{"sha": "c0ffee1", "commit": {"message": "msg (#1)"}}],
            )
        if url_str.endswith("/commits/c0ffee1"):
            return httpx.Response(200, json={"files": []})
        if "/contents/deploy/prod?" in url_str:
            if mode == "404":
                return httpx.Response(404, text="Directory Not Found")
            if mode == "500":
                return httpx.Response(500, text="Contents Server Error")
            if mode == "bad_json":
                return httpx.Response(200, text="invalid-contents-json{")
            if mode == "non_list":
                return httpx.Response(200, json={"not": "a list"})
            if mode == "file_edges":
                return httpx.Response(
                    200,
                    json=[
                        "not-a-dict",
                        {"name": None, "type": "file"},
                        {"name": 123, "type": "file"},
                        {"name": "readme.txt", "type": "file"},
                        {"name": "subfolder", "type": "dir"},
                        {
                            "name": "svc1.yaml",
                            "type": "file",
                            "sha": "blob1",
                            "path": "deploy/prod/svc1.yaml",
                        },
                        {"name": "svc2.yaml", "type": "file", "sha": None},  # No path, no sha
                    ],
                )
        if "/contents/deploy/prod/svc1.yaml?" in url_str and mode == "file_edges":
            return httpx.Response(500, text="File fetch error")
        if "/contents/deploy/prod/svc2.yaml?" in url_str and mode == "file_edges":
            svc2_content = (
                "apiVersion: apps/v1\nkind: Deployment\nspec:\n"
                "  template:\n    spec:\n      containers:\n"
                "        - name: svc2\n          image: localhost:5001/svc2:good\n"
            )
            return httpx.Response(200, text=svc2_content)

        return httpx.Response(404, text="Not Found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        history = GitHubDeployHistory(repo="owner/repo", client=http_client)

        # 404 manifest directory logs warning and returns empty digests
        mode = "404"
        deploys = await history.recent_deploys(limit=1)
        assert deploys[0].image_digests == {}

        # 500 manifest directory raises GitHubError
        mode = "500"
        with pytest.raises(GitHubError, match="GitHub contents query failed with status 500"):
            await history.recent_deploys(limit=1)

        # Bad json in directory contents raises GitHubError
        mode = "bad_json"
        with pytest.raises(GitHubError, match="Failed to decode GitHub contents JSON"):
            await history.recent_deploys(limit=1)

        # Non-list response returns empty digests
        mode = "non_list"
        deploys = await history.recent_deploys(limit=1)
        assert deploys[0].image_digests == {}

        # File edges handled: 500 skips svc1, svc2 parses cleanly
        mode = "file_edges"
        deploys = await history.recent_deploys(limit=1)
        assert "svc2" in deploys[0].image_digests


@pytest.mark.asyncio
async def test_recent_deploys_commits_errors() -> None:
    mode = "500"

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/commits" in url_str:
            if mode == "500":
                return httpx.Response(500, text="Commits error")
            if mode == "non_list":
                return httpx.Response(200, json={"error": "bad format"})
            if mode == "bad_json":
                return httpx.Response(200, text="not-json{")
        return httpx.Response(404, text="Not Found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        history = GitHubDeployHistory(repo="owner/repo", client=http_client)

        # 500 error
        with pytest.raises(GitHubError, match="GitHub API commit query failed: 500"):
            await history.recent_deploys(limit=1)

        # Non-list JSON
        mode = "non_list"
        with pytest.raises(GitHubError, match="GitHub commits endpoint returned non-list response"):
            await history.recent_deploys(limit=1)

        # Bad JSON syntax
        mode = "bad_json"
        with pytest.raises(GitHubError, match="Failed to decode GitHub commits JSON"):
            await history.recent_deploys(limit=1)


@pytest.mark.asyncio
async def test_recent_deploys_request_errors() -> None:
    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(failing_handler)) as http_client:
        history = GitHubDeployHistory(repo="owner/repo", client=http_client)

        with pytest.raises(GitHubError, match="GitHub API commit query request failed"):
            await history.recent_deploys(limit=1)


@pytest.mark.asyncio
async def test_recent_deploys_subrequest_errors() -> None:
    phase = "pulls"

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/commits?per_page=1"):
            # message without PR so it calls /pulls
            return httpx.Response(200, json=[{"sha": "c0ffee1", "commit": {"message": "msg"}}])
        if phase == "pulls" and "/pulls" in url_str:
            raise httpx.ReadTimeout("Pulls timeout", request=request)
        if phase == "commit_detail" and url_str.endswith("/commits/c0ffee1"):
            raise httpx.ReadTimeout("Detail timeout", request=request)
        if phase == "contents" and "/contents/deploy/prod?" in url_str:
            raise httpx.ReadTimeout("Contents timeout", request=request)
        if phase == "file" and "/contents/deploy/prod/file.yaml?" in url_str:
            raise httpx.ReadTimeout("File timeout", request=request)
        if url_str.endswith("/commits/c0ffee1/pulls"):
            return httpx.Response(200, json=[{"number": 1}])
        if url_str.endswith("/commits/c0ffee1"):
            return httpx.Response(200, json={"files": []})
        if "/contents/deploy/prod?" in url_str:
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "file.yaml",
                        "type": "file",
                        "path": "deploy/prod/file.yaml",
                        "sha": "blob1",
                    }
                ],
            )
        return httpx.Response(404, text="Not Found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        history = GitHubDeployHistory(repo="owner/repo", client=http_client)

        # Pulls request error
        phase = "pulls"
        with pytest.raises(GitHubError, match="GitHub commit pulls request error"):
            await history.recent_deploys(limit=1)

        # Commit detail request error
        phase = "commit_detail"
        with pytest.raises(GitHubError, match="GitHub commit detail query failed"):
            await history.recent_deploys(limit=1)

        # Contents request error
        phase = "contents"
        with pytest.raises(GitHubError, match="GitHub contents request failed"):
            await history.recent_deploys(limit=1)

        # File request error
        phase = "file"
        with pytest.raises(GitHubError, match="Failed to fetch manifest file"):
            await history.recent_deploys(limit=1)


@pytest.mark.asyncio
async def test_commit_parsing_skips_and_clock_fallback() -> None:
    clock = FrozenClock(FIXED_NOW)

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/commits" in url_str:
            return httpx.Response(
                200,
                json=[
                    {"sha": None, "commit": {}},  # Skipped: no sha
                    {"sha": 12345, "commit": {}},  # Skipped: non-string sha
                    {
                        "sha": "c0ffee1",
                        "commit": {"message": "ok (#1)"},
                    },  # No date: falls back to clock.now()
                ],
            )
        if "/contents/deploy/prod?" in url_str:
            return httpx.Response(200, json=[])
        return httpx.Response(404, text="Not Found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        history = GitHubDeployHistory(repo="owner/repo", client=http_client, clock=clock)
        deploys = await history.recent_deploys(limit=5)
        assert len(deploys) == 1
        assert deploys[0].commit_sha == "c0ffee1"
        assert deploys[0].deployed_at == FIXED_NOW


@pytest.mark.asyncio
async def test_client_lifecycle_uninitialized() -> None:
    h = GitHubDeployHistory(repo="owner/repo")
    assert h._client is None
    await h.close()


@pytest.mark.asyncio
async def test_client_lifecycle_owned() -> None:
    h = GitHubDeployHistory(repo="owner/repo")
    client = h._get_client()
    assert isinstance(client, httpx.AsyncClient)
    await h.close()
    assert h._client is None


@pytest.mark.asyncio
async def test_client_lifecycle_injected() -> None:
    injected = httpx.AsyncClient()
    try:
        h = GitHubDeployHistory(repo="owner/repo", client=injected)
        assert h._get_client() is injected
        await h.close()
        assert h._client is injected
    finally:
        await injected.aclose()


@pytest.mark.asyncio
async def test_client_context_manager() -> None:
    async with GitHubDeployHistory(repo="owner/repo") as h:
        client = h._get_client()
        assert isinstance(client, httpx.AsyncClient)
    assert h._client is None
