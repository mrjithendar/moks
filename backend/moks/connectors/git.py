"""Git connector with a provider adapter interface; GitHub is the first provider.

Read operations (tree, file, commits, compare) serve investigations and RAG ingestion. The only
write operation, opening a pull request, is used solely by the executor for an approved
`open_pull_request` action plan.
"""
from __future__ import annotations

import base64
import re
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from ..redaction import redact
from .base import BoundedHttp, ConnectorError, HttpLimits, resolve_credential

SHA = re.compile(r"^[0-9a-f]{7,40}$")
REPO = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$")
# Never read or index these, whatever the repository says.
SECRET_PATHS = re.compile(r"(?i)(^|/)(\.env(\..*)?|.*\.pem|.*\.key|id_rsa.*|.*kubeconfig.*|.*secret.*\.ya?ml|.*\.tfstate.*|credentials(\.json)?|\.npmrc|\.pypirc|\.netrc)$")
MAX_FILE_BYTES = 400_000


class GitProvider(Protocol):
    async def test(self) -> str: ...
    async def tree(self, repo: str, ref: str | None) -> tuple[str, list[dict]]: ...
    async def read(self, repo: str, path: str, ref: str | None) -> tuple[str, str]: ...
    async def commits(self, repo: str, path: str | None, since: datetime | None, limit: int) -> list[dict]: ...
    async def commit(self, repo: str, sha: str) -> dict: ...
    async def compare(self, repo: str, base: str, head: str) -> dict: ...
    def blob_url(self, repo: str, path: str, ref: str, start_line: int | None = None, end_line: int | None = None) -> str: ...


class GitHubProvider:
    def __init__(self, config: dict[str, Any], credential_ref: dict[str, Any] | None, transport: httpx.AsyncBaseTransport | None = None):
        token = resolve_credential(credential_ref) if credential_ref else None
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.api = (config.get("api_url") or "https://api.github.com").rstrip("/")
        self.web = (config.get("web_url") or "https://github.com").rstrip("/")
        self.http = BoundedHttp("git", self.api, headers=headers, limits=HttpLimits(timeout_seconds=float(config.get("timeout_seconds", 20))),
                                transport=transport)
        self.repositories = [r for r in config.get("repositories") or []]

    async def aclose(self) -> None:
        await self.http.aclose()

    async def test(self) -> str:
        if not self.repositories:
            raise ConnectorError("config", "git connector needs config.repositories (owner/name list)")
        r = await self.http.get_json("repo", f"/repos/{self.repositories[0]}")
        return f"GitHub reachable; {r.get('full_name')} default branch {r.get('default_branch')} ({'private' if r.get('private') else 'public'})"

    async def default_branch(self, repo: str) -> str:
        return (await self.http.get_json("repo", f"/repos/{repo}")).get("default_branch", "main")

    async def tree(self, repo: str, ref: str | None) -> tuple[str, list[dict]]:
        ref = ref or await self.default_branch(repo)
        commit = await self.http.get_json("commit", f"/repos/{repo}/commits/{quote(ref, safe='')}")
        sha = commit["sha"]
        body = await self.http.get_json("tree", f"/repos/{repo}/git/trees/{sha}", params={"recursive": "1"})
        if body.get("truncated"):
            raise ConnectorError("too_large", f"{repo} tree is too large for one listing; narrow the source paths")
        return sha, [{"path": t["path"], "sha": t["sha"], "size": t.get("size", 0)} for t in body.get("tree", []) if t.get("type") == "blob"]

    async def read(self, repo: str, path: str, ref: str | None) -> tuple[str, str]:
        if SECRET_PATHS.search(path):
            raise ConnectorError("forbidden", f"{path} looks like a secret file and is never read")
        params = {"ref": ref} if ref else None
        body = await self.http.get_json("contents", f"/repos/{repo}/contents/{quote(path)}", params=params)
        if isinstance(body, list) or body.get("type") != "file":
            raise ConnectorError("bad_request", f"{path} is not a file")
        if body.get("size", 0) > MAX_FILE_BYTES:
            raise ConnectorError("too_large", f"{path} is larger than {MAX_FILE_BYTES} bytes")
        text = base64.b64decode(body.get("content", "")).decode("utf-8", "replace")
        return text, body.get("sha", "")

    async def blob(self, repo: str, sha: str) -> str:
        body = await self.http.get_json("blob", f"/repos/{repo}/git/blobs/{sha}")
        if body.get("size", 0) > MAX_FILE_BYTES:
            raise ConnectorError("too_large", "blob too large")
        return base64.b64decode(body.get("content", "")).decode("utf-8", "replace")

    async def commits(self, repo: str, path: str | None, since: datetime | None, limit: int = 20) -> list[dict]:
        params: dict[str, Any] = {"per_page": min(limit, 100)}
        if path:
            params["path"] = path
        if since:
            params["since"] = since.isoformat()
        rows = await self.http.get_json("commits", f"/repos/{repo}/commits", params=params)
        return [{"sha": c["sha"], "short": c["sha"][:8], "message": c["commit"]["message"].splitlines()[0][:200],
                 "author": (c["commit"].get("author") or {}).get("name"), "date": (c["commit"].get("author") or {}).get("date"),
                 "url": c.get("html_url")} for c in rows[:limit]]

    async def commit(self, repo: str, sha: str) -> dict:
        c = await self.http.get_json("commit", f"/repos/{repo}/commits/{quote(sha, safe='')}")
        return {"sha": c["sha"], "message": c["commit"]["message"][:500], "date": (c["commit"].get("author") or {}).get("date"),
                "author": (c["commit"].get("author") or {}).get("name"), "url": c.get("html_url"),
                "files": [{"path": f["filename"], "status": f["status"], "changes": f.get("changes")} for f in c.get("files", [])[:50]]}

    async def compare(self, repo: str, base: str, head: str) -> dict:
        c = await self.http.get_json("compare", f"/repos/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}")
        return {"status": c.get("status"), "ahead_by": c.get("ahead_by"), "behind_by": c.get("behind_by"), "url": c.get("html_url"),
                "commits": [{"sha": x["sha"][:8], "message": x["commit"]["message"].splitlines()[0][:160]} for x in c.get("commits", [])[-20:]],
                "files": [{"path": f["filename"], "status": f["status"]} for f in c.get("files", [])[:50]]}

    def blob_url(self, repo: str, path: str, ref: str, start_line: int | None = None, end_line: int | None = None) -> str:
        url = f"{self.web}/{repo}/blob/{ref}/{quote(path)}"
        if start_line:
            url += f"#L{start_line}" + (f"-L{end_line}" if end_line and end_line != start_line else "")
        return url

    # ---- the only write path (executor, approved plans only) -------------------------

    async def open_pull_request(self, repo: str, base: str, branch: str, path: str, content: str, message: str, title: str, body: str) -> dict:
        base_sha = (await self.http.get_json("ref", f"/repos/{repo}/git/ref/heads/{quote(base, safe='')}"))["object"]["sha"]
        await self._post(f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})
        existing_sha = None
        try:
            existing = await self.http.get_json("contents", f"/repos/{repo}/contents/{quote(path)}", params={"ref": branch})
            existing_sha = existing.get("sha")
        except ConnectorError as exc:
            if exc.kind != "not_found":
                raise
        payload = {"message": message, "content": base64.b64encode(content.encode()).decode(), "branch": branch}
        if existing_sha:
            payload["sha"] = existing_sha
        await self._put(f"/repos/{repo}/contents/{quote(path)}", payload)
        pr = await self._post(f"/repos/{repo}/pulls", {"title": title, "head": branch, "base": base, "body": body, "draft": True})
        return {"number": pr.get("number"), "url": pr.get("html_url"), "branch": branch}

    async def _post(self, path: str, payload: dict) -> dict:
        return await self._send("POST", path, payload)

    async def _put(self, path: str, payload: dict) -> dict:
        return await self._send("PUT", path, payload)

    async def _send(self, method: str, path: str, payload: dict) -> dict:
        r = await self.http.client.request(method, path, json=payload)
        if r.status_code >= 400:
            raise ConnectorError("forbidden" if r.status_code in (401, 403) else "bad_request", f"GitHub {method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()


PROVIDERS = {"github": GitHubProvider}


class GitConnector:
    """Wraps one provider plus the repository allowlist."""

    type = "git"

    def __init__(self, config: dict[str, Any], credential_ref: dict[str, Any] | None = None, transport: httpx.AsyncBaseTransport | None = None):
        provider = config.get("provider", "github")
        if provider not in PROVIDERS:
            raise ConnectorError("config", f"unsupported git provider {provider!r} (supported: {', '.join(PROVIDERS)})")
        repos = config.get("repositories") or []
        bad = [r for r in repos if not REPO.match(str(r))]
        if not repos or bad:
            raise ConnectorError("config", "config.repositories must list owner/name repositories")
        self.provider = PROVIDERS[provider](config, credential_ref, transport)
        self.repositories = list(repos)

    async def aclose(self) -> None:
        await self.provider.aclose()

    def check_repo(self, repo: str) -> None:
        if repo not in self.repositories:
            raise ConnectorError("forbidden", f"repository {repo} is not in this connector's allowlist")

    async def test(self) -> str:
        return await self.provider.test()

    async def read_file(self, repo: str, path: str, ref: str | None) -> dict:
        self.check_repo(repo)
        text, sha = await self.provider.read(repo, path, ref)
        ref_used = ref or await self.provider.default_branch(repo)
        return {"repo": repo, "path": path, "ref": ref_used, "blob": sha, "text": redact(text),
                "url": self.provider.blob_url(repo, path, ref_used)}

    async def recent_commits(self, repo: str, path: str | None, since: datetime | None, limit: int) -> list[dict]:
        self.check_repo(repo)
        return await self.provider.commits(repo, path, since, limit)

    async def compare_deployment(self, repo: str, images: list[str], annotations: dict[str, str]) -> dict:
        """Relate a running Deployment to Git only when real metadata exists. Never guesses."""
        self.check_repo(repo)
        evidence: list[dict] = []
        candidates: list[tuple[str, str]] = []
        for img in images:
            tag = img.split("@")[0].rsplit(":", 1)[-1] if ":" in img.split("/")[-1] else ""
            if SHA.match(tag):
                candidates.append(("image tag", tag))
            evidence.append({"image": img, "tag": tag or None, "digest_pinned": "@sha256:" in img})
        for key in ("app.kubernetes.io/version", "org.opencontainers.image.revision", "moks.io/git-revision", "git-commit", "revision"):
            v = annotations.get(key, "")
            if SHA.match(v):
                candidates.append((f"annotation {key}", v))
        if not candidates:
            return {"repo": repo, "resolved": False, "images": evidence,
                    "reason": "No commit SHA in the image tags or revision annotations; the deployed revision cannot be determined from Git without guessing."}
        source, sha = candidates[0]
        try:
            commit = await self.provider.commit(repo, sha)
        except ConnectorError as exc:
            if exc.kind == "not_found":
                return {"repo": repo, "resolved": False, "images": evidence, "reason": f"{source} {sha} is not a commit in {repo}."}
            raise
        branch = await self.provider.default_branch(repo)
        cmp = await self.provider.compare(repo, commit["sha"], branch)
        return {"repo": repo, "resolved": True, "source": source, "deployed_commit": commit, "default_branch": branch,
                "behind_default_branch_by": cmp.get("ahead_by"), "undeployed_commits": cmp.get("commits"), "compare_url": cmp.get("url"),
                "images": evidence}
