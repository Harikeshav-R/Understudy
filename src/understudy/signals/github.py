"""GitHub version control adapter for deploy history and migration detection."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import yaml

from understudy.common.clock import Clock, resolve_clock
from understudy.common.config import get_settings
from understudy.common.errors import GitHubError
from understudy.common.logging import get_logger
from understudy.contracts.incident import DeployRef
from understudy.signals.api import DeployHistory

logger = get_logger(__name__)

_PR_MERGE_PATTERN = re.compile(r"Merge pull request #(\d+)", re.IGNORECASE)
_PR_SQUASH_PATTERN = re.compile(r"\(#(\d+)\)(?:\s*$|\n)")
_PR_GENERIC_PATTERN = re.compile(r"(?:^|\s)pull request #(\d+)", re.IGNORECASE)


def _parse_iso_datetime(date_str: str) -> datetime:
    """Parse an ISO 8601 timestamp into a timezone-aware UTC datetime."""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception as exc:
        raise GitHubError(f"Failed to parse commit timestamp {date_str!r}: {exc}") from exc


def _extract_pr_number_from_message(message: str) -> int | None:
    """Extract pull request number from standard GitHub commit message formats."""
    match = _PR_MERGE_PATTERN.search(message)
    if match:
        return int(match.group(1))

    match = _PR_SQUASH_PATTERN.search(message)
    if match:
        return int(match.group(1))

    match = _PR_GENERIC_PATTERN.search(message)
    if match:
        return int(match.group(1))

    return None


def _is_migration_file(path: str) -> bool:
    """Check whether a path points to a database migration file."""
    normalized = path.strip().replace("\\", "/")
    return (
        "migrations/" in normalized
        or normalized.startswith("migrations/")
        or normalized.endswith("/migrations")
        or normalized == "migrations"
    )


def _extract_container_digest(image_spec: str) -> str:
    """Extract raw digest or derive a deterministic sha256 digest from an image spec.

    If an explicit sha256 digest is present (e.g. repo@sha256:abc...), returns sha256:abc...
    If an unpinned image tag is specified (e.g. localhost:5001/svc:good), generates a
    deterministic 71-character sha256:<hex> digest from the image string.
    """
    clean_spec = image_spec.strip()
    if "@sha256:" in clean_spec:
        return clean_spec.split("@", 1)[1]

    if clean_spec.startswith("sha256:"):
        return clean_spec

    # Deterministic fallback digest for unpinned image references
    hex_hash = hashlib.sha256(clean_spec.encode("utf-8")).hexdigest()
    return f"sha256:{hex_hash}"


def _parse_manifest_digests(yaml_content: str) -> dict[str, str]:
    """Parse Kubernetes Deployment documents in YAML text to extract container image digests."""
    digests: dict[str, str] = {}
    try:
        documents = yaml.safe_load_all(yaml_content)
        for doc in documents:
            if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
                continue
            spec = doc.get("spec")
            if not isinstance(spec, dict):
                continue
            template = spec.get("template")
            if not isinstance(template, dict):
                continue
            pod_spec = template.get("spec")
            if not isinstance(pod_spec, dict):
                continue
            containers = pod_spec.get("containers")
            if not isinstance(containers, list):
                continue

            for container in containers:
                if not isinstance(container, dict):
                    continue
                c_name = container.get("name")
                c_image = container.get("image")
                if c_name and isinstance(c_name, str) and c_image and isinstance(c_image, str):
                    digests[c_name] = _extract_container_digest(c_image)
    except Exception as exc:
        logger.warning("github_manifest_yaml_parse_failed", error=str(exc))
    return digests


class GitHubDeployHistory(DeployHistory):
    """GitHub version control adapter for deploy history and migration detection."""

    def __init__(
        self,
        token: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        base_url: str = "https://api.github.com",
        manifest_path: str = "deploy/prod",
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
    ) -> None:
        settings = get_settings()

        resolved_token = token if token is not None else settings.secrets.github_token
        # Ignore dummy template placeholder
        if resolved_token and resolved_token.strip().startswith("ghp_..."):
            resolved_token = None
        self.token = resolved_token.strip() if resolved_token and resolved_token.strip() else None

        resolved_repo = repo or settings.secrets.github_repo or "Harikeshav-R/Understudy"
        self.repo = resolved_repo.strip()
        self.branch = branch.strip() if branch and branch.strip() else None
        self.base_url = base_url.rstrip("/")
        self.manifest_path = manifest_path.strip("/")
        self.timeout = timeout
        self.clock = resolve_clock(clock)

        self._client = client
        self._owns_client = client is None
        # Cache parsed digests by git blob SHA to avoid redundant network queries
        self._blob_digest_cache: dict[str, dict[str, str]] = {}

    def _get_headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        """Construct request headers with optional authorization."""
        headers = {
            "Accept": accept,
            "User-Agent": "Understudy",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get_client(self) -> httpx.AsyncClient:
        """Return the active HTTP client, lazily initializing if owned."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client if created internally."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> GitHubDeployHistory:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.close()

    async def recent_deploys(self, limit: int = 5) -> list[DeployRef]:
        """Fetch recent deployment metadata from the GitHub repository."""
        if limit <= 0:
            return []

        commits = await self._fetch_commits(limit)
        deploys: list[DeployRef] = []

        for commit in commits:
            commit_sha = commit.get("sha")
            if not commit_sha or not isinstance(commit_sha, str):
                continue

            commit_data = commit.get("commit", {})
            committer_info = commit_data.get("committer") or {}
            author_info = commit_data.get("author") or {}
            date_str = committer_info.get("date") or author_info.get("date")

            if date_str and isinstance(date_str, str):
                deployed_at = _parse_iso_datetime(date_str)
            else:
                deployed_at = self.clock.now()

            pr_number = await self._resolve_pr_number(commit)
            contains_migration = await self._check_migration(commit_sha)
            image_digests = await self._extract_image_digests(commit_sha)

            deploy_ref = DeployRef(
                commit_sha=commit_sha,
                image_digests=image_digests,
                deployed_at=deployed_at,
                pr_number=pr_number,
                contains_migration=contains_migration,
            )
            deploys.append(deploy_ref)

        logger.info(
            "github_recent_deploys_fetched",
            repo=self.repo,
            count=len(deploys),
            limit=limit,
        )
        return deploys

    async def _fetch_commits(self, limit: int) -> list[dict[str, Any]]:
        """Query commit list from GitHub API."""
        client = self._get_client()
        url = f"{self.base_url}/repos/{self.repo}/commits"
        params: dict[str, Any] = {"per_page": min(limit, 100)}
        if self.branch:
            params["sha"] = self.branch

        try:
            response = await client.get(
                url,
                headers=self._get_headers(),
                params=params,
            )
        except httpx.RequestError as exc:
            raise GitHubError(f"GitHub API commit query request failed: {exc}") from exc

        if response.status_code != 200:
            msg = f"GitHub API commit query failed: {response.status_code} {response.text}"
            raise GitHubError(msg, details={"status_code": response.status_code, "url": url})

        try:
            data = response.json()
            if isinstance(data, list):
                return cast("list[dict[str, Any]]", data)
            raise GitHubError(f"GitHub commits endpoint returned non-list response: {type(data)}")
        except Exception as exc:
            if isinstance(exc, GitHubError):
                raise
            raise GitHubError(f"Failed to decode GitHub commits JSON: {exc}") from exc

    async def _resolve_pr_number(self, commit: dict[str, Any]) -> int | None:
        """Resolve associated pull request number from message or GitHub API."""
        commit_data = commit.get("commit", {})
        message = commit_data.get("message", "")
        if isinstance(message, str):
            from_msg = _extract_pr_number_from_message(message)
            if from_msg is not None:
                return from_msg

        commit_sha = commit.get("sha")
        if not commit_sha or not isinstance(commit_sha, str):
            return None

        client = self._get_client()
        url = f"{self.base_url}/repos/{self.repo}/commits/{commit_sha}/pulls"

        try:
            response = await client.get(
                url,
                headers=self._get_headers(),
            )
            if response.status_code == 200:
                pulls = response.json()
                if isinstance(pulls, list) and pulls:
                    first_pr = pulls[0]
                    if isinstance(first_pr, dict) and "number" in first_pr:
                        return int(first_pr["number"])
                return None
            if response.status_code in (404, 422):
                return None
            msg = f"GitHub commit pulls query failed: {response.status_code} {response.text}"
            raise GitHubError(
                msg,
                details={"status_code": response.status_code, "sha": commit_sha},
            )
        except httpx.RequestError as exc:
            raise GitHubError(f"GitHub commit pulls request error for {commit_sha}: {exc}") from exc

    async def _check_migration(self, commit_sha: str) -> bool:
        """Query commit details to check whether modified files include database migrations."""
        client = self._get_client()
        url = f"{self.base_url}/repos/{self.repo}/commits/{commit_sha}"

        try:
            response = await client.get(
                url,
                headers=self._get_headers(),
            )
        except httpx.RequestError as exc:
            raise GitHubError(f"GitHub commit detail query failed for {commit_sha}: {exc}") from exc

        if response.status_code != 200:
            msg = f"GitHub commit detail query failed: {response.status_code} {response.text}"
            raise GitHubError(
                msg,
                details={"status_code": response.status_code, "sha": commit_sha},
            )

        try:
            data = response.json()
            if not isinstance(data, dict):
                return False
            files = data.get("files", [])
            if not isinstance(files, list):
                return False

            for f in files:
                if isinstance(f, dict):
                    filename = f.get("filename")
                    if filename and isinstance(filename, str) and _is_migration_file(filename):
                        return True
            return False
        except Exception as exc:
            raise GitHubError(
                f"Failed to parse commit detail files JSON for {commit_sha}: {exc}"
            ) from exc

    async def _extract_image_digests(self, commit_sha: str) -> dict[str, str]:
        """Fetch deploy manifests at commit SHA and extract container image digests."""
        client = self._get_client()
        url = f"{self.base_url}/repos/{self.repo}/contents/{self.manifest_path}"

        try:
            response = await client.get(
                url,
                headers=self._get_headers(),
                params={"ref": commit_sha},
            )
        except httpx.RequestError as exc:
            raise GitHubError(
                f"GitHub contents request failed for {self.manifest_path}: {exc}"
            ) from exc

        if response.status_code == 404:
            logger.warning(
                "github_manifest_directory_not_found", path=self.manifest_path, sha=commit_sha
            )
            return {}

        if response.status_code != 200:
            raise GitHubError(
                f"GitHub contents query failed with status {response.status_code}: {response.text}",
                details={
                    "status_code": response.status_code,
                    "path": self.manifest_path,
                    "sha": commit_sha,
                },
            )

        try:
            items = response.json()
            if not isinstance(items, list):
                return {}
        except Exception as exc:
            raise GitHubError(
                f"Failed to decode GitHub contents JSON for {self.manifest_path}: {exc}"
            ) from exc

        digests: dict[str, str] = {}
        raw_headers = self._get_headers(accept="application/vnd.github.raw+json")

        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            blob_sha = item.get("sha")
            item_type = item.get("type")

            if item_type != "file" or not name or not isinstance(name, str):
                continue
            if not (name.endswith(".yaml") or name.endswith(".yml")):
                continue

            # Check in-memory cache for this blob SHA
            if blob_sha and isinstance(blob_sha, str) and blob_sha in self._blob_digest_cache:
                digests.update(self._blob_digest_cache[blob_sha])
                continue

            item_path = item.get("path") or f"{self.manifest_path}/{name}"
            file_url = f"{self.base_url}/repos/{self.repo}/contents/{item_path}"

            try:
                file_resp = await client.get(
                    file_url,
                    headers=raw_headers,
                    params={"ref": commit_sha},
                )
            except httpx.RequestError as exc:
                raise GitHubError(
                    f"Failed to fetch manifest file {item_path} at {commit_sha}: {exc}"
                ) from exc

            if file_resp.status_code != 200:
                logger.warning(
                    "github_manifest_file_fetch_failed",
                    path=item_path,
                    sha=commit_sha,
                    status=file_resp.status_code,
                )
                continue

            parsed = _parse_manifest_digests(file_resp.text)
            if blob_sha and isinstance(blob_sha, str):
                self._blob_digest_cache[blob_sha] = parsed
            digests.update(parsed)

        return digests
