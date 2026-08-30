"""
GitHub integration for PatchPilot.

Responsible for everything that touches the GitHub API:
- Fetching failed workflow run logs
- Reading the relevant source files at the failing commit
- Creating a branch, committing a patch, and opening a PR
- Leaving an explanatory comment with the agent's reasoning

Requires a GitHub App or PAT with `repo` and `workflow` scopes,
supplied via the GITHUB_TOKEN environment variable.
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from dataclasses import dataclass

import requests

from github import Github, GithubException
from github.Repository import Repository
from github.WorkflowRun import WorkflowRun

logger = logging.getLogger("patchpilot.github")


@dataclass
class FailedRunContext:
    repo_full_name: str
    run_id: int
    head_sha: str
    head_branch: str
    workflow_name: str
    logs_text: str
    changed_files: list[str]


class GitHubClient:
    """Thin, purpose-built wrapper around PyGithub for the PatchPilot workflow."""

    def __init__(self, token: str | None = None):
        self._token = token or os.environ["GITHUB_TOKEN"]
        self._gh = Github(self._token)

    def get_repo(self, full_name: str) -> Repository:
        return self._gh.get_repo(full_name)

    def fetch_failed_run_context(self, full_name: str, run_id: int) -> FailedRunContext:
        """Pull the failing workflow run's logs and metadata."""
        repo = self.get_repo(full_name)
        run: WorkflowRun = repo.get_workflow_run(run_id)

        logs_text = self._download_and_flatten_logs(run)
        commit = repo.get_commit(run.head_sha)
        changed_files = [f.filename for f in commit.files]

        return FailedRunContext(
            repo_full_name=full_name,
            run_id=run_id,
            head_sha=run.head_sha,
            head_branch=run.head_branch,
            workflow_name=run.name,
            logs_text=logs_text,
            changed_files=changed_files,
        )

    def _download_and_flatten_logs(self, run: WorkflowRun, max_chars: int = 40_000) -> str:
        """Download the run's log archive (a zip of per-job text files) and
        flatten it into a single truncated string suitable for an LLM prompt."""
        resp = requests.get(
            run.logs_url,
            headers={
                "Authorization": f"token {self._token}",
                "Accept": "application/vnd.github+json",
            },
            allow_redirects=True,
            timeout=30,
        )
        resp.raise_for_status()
        buf = io.BytesIO(resp.content)
        chunks: list[str] = []
        try:
            with zipfile.ZipFile(buf) as zf:
                for name in sorted(zf.namelist()):
                    if not name.endswith(".txt"):
                        continue
                    text = zf.read(name).decode("utf-8", errors="replace")
                    chunks.append(f"===== {name} =====\n{text}")
        except zipfile.BadZipFile:
            logger.warning("Could not parse log archive for run %s", run.id)
            return "(log archive unavailable)"

        flattened = "\n".join(chunks)
        if len(flattened) > max_chars:
            # Keep the tail — build failures are almost always at the end of the log.
            flattened = "...[truncated]...\n" + flattened[-max_chars:]
        return flattened

    def get_file_contents(self, full_name: str, path: str, ref: str) -> str | None:
        repo = self.get_repo(full_name)
        try:
            f = repo.get_contents(path, ref=ref)
            return f.decoded_content.decode("utf-8", errors="replace")
        except GithubException:
            return None

    def open_fix_pr(
        self,
        full_name: str,
        base_branch: str,
        head_sha: str,
        branch_name: str,
        file_patches: dict[str, str],
        title: str,
        body: str,
    ) -> str:
        """Create a branch off head_sha, commit the given file contents, and open a PR.

        `file_patches` maps repo-relative file path -> new full file content.
        Returns the URL of the created pull request.
        """
        repo = self.get_repo(full_name)

        ref = f"refs/heads/{branch_name}"
        repo.create_git_ref(ref=ref, sha=head_sha)

        for path, new_content in file_patches.items():
            existing = repo.get_contents(path, ref=branch_name)
            repo.update_file(
                path=path,
                message=f"patchpilot: fix {path}",
                content=new_content,
                sha=existing.sha,
                branch=branch_name,
            )

        pr = repo.create_pull(
            title=title,
            body=body,
            head=branch_name,
            base=base_branch,
        )
        return pr.html_url

    def comment_on_commit(self, full_name: str, sha: str, body: str) -> None:
        repo = self.get_repo(full_name)
        commit = repo.get_commit(sha)
        commit.create_comment(body=body)
