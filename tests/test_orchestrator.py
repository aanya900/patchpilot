"""
Unit tests for PatchPilotOrchestrator's side-effect logic.

These tests mock GitHubClient and MemoryStore entirely and stub the ADK
runner's final session state, so they verify the orchestration/branching
logic (PR opened vs. comment-only fallback) without making any real network
or LLM calls — suitable for CI itself.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from google.auth.exceptions import DefaultCredentialsError

from patchpilot.agents.orchestrator import PatchPilotOrchestrator
from patchpilot.services.github_client import FailedRunContext
from patchpilot.services.memory_store import MemoryStore


def _make_ctx() -> FailedRunContext:
    return FailedRunContext(
        repo_full_name="acme/widget",
        run_id=42,
        head_sha="deadbeef",
        head_branch="main",
        workflow_name="CI",
        logs_text="ModuleNotFoundError: No module named 'requests'",
        changed_files=["requirements.txt"],
    )


@pytest.fixture
def mock_github():
    gh = MagicMock()
    gh.fetch_failed_run_context.return_value = _make_ctx()
    gh.get_file_contents.return_value = "flask==2.0.0\n"
    gh.open_fix_pr.return_value = "https://github.com/acme/widget/pull/99"
    return gh


@pytest.fixture
def mock_memory():
    mem = MagicMock()
    mem.recent_incidents.return_value = []
    return mem


def _fake_final_state(state: dict):
    """Patch InMemoryRunner internals so run_async yields one final event
    whose session ends up with the given state, without touching real ADK
    session/runner internals in the test."""

    async def _fake_run_async(*args, **kwargs):
        ev = MagicMock()
        ev.is_final_response.return_value = True
        yield ev

    return _fake_run_async


def test_memory_store_runs_without_google_credentials():
    with patch("patchpilot.services.memory_store.firestore.Client", side_effect=DefaultCredentialsError("no creds")):
        store = MemoryStore()

    assert store.recent_incidents("acme/widget") == []
    store.record_incident(
        MagicMock(
            repo_full_name="acme/widget",
            run_id=123,
        )
    )


@pytest.mark.asyncio
async def test_high_confidence_fix_opens_pr(mock_github, mock_memory):
    orch = PatchPilotOrchestrator(github_client=mock_github, memory_store=mock_memory)

    state = {
        "diagnosis": {
            "root_cause": "requirements.txt pins an unavailable flask version",
            "failure_type": "dependency",
            "confidence": 0.92,
            "reasoning": "pip install failed resolving flask==2.0.0",
            "is_recurring": False,
            "offending_files": ["requirements.txt"],
        },
        "proposed_fix": {
            "can_fix": True,
            "patches": [
                {"path": "requirements.txt", "new_content": "flask==3.0.3\n", "explanation": "bump pin"}
            ],
            "pr_title": "fix: bump flask to 3.0.3",
            "pr_body": "The pinned flask version is no longer resolvable.",
            "risk_notes": "Not run against local test suite in this sandbox.",
        },
        "validation": {"approved": True, "concerns": [], "summary": "Patch matches root cause."},
    }

    orch.runner.run_async = _fake_final_state(state)
    orch.runner.session_service.create_session = AsyncMock()
    orch.runner.session_service.get_session = AsyncMock(
        return_value=MagicMock(state=state)
    )

    result = await orch.handle_failed_run("acme/widget", 42)

    assert result["outcome"] == "pr_opened"
    assert result["pr_url"] == "https://github.com/acme/widget/pull/99"
    mock_github.open_fix_pr.assert_called_once()
    mock_memory.record_incident.assert_called_once()
    assert mock_memory.record_incident.call_args.args[0].outcome == "pr_opened"


@pytest.mark.asyncio
async def test_low_confidence_falls_back_to_comment(mock_github, mock_memory):
    orch = PatchPilotOrchestrator(github_client=mock_github, memory_store=mock_memory)

    state = {
        "diagnosis": {
            "root_cause": "Intermittent network timeout hitting an external API in tests",
            "failure_type": "flaky",
            "confidence": 0.4,
            "reasoning": "Same test passed on retry in a prior run",
            "is_recurring": True,
            "offending_files": [],
        },
        "proposed_fix": {
            "can_fix": False,
            "patches": [],
            "pr_title": "",
            "pr_body": "",
            "risk_notes": "",
        },
        "validation": {"approved": False, "concerns": ["No fix proposed"], "summary": "Rejected: no fix."},
    }

    orch.runner.run_async = _fake_final_state(state)
    orch.runner.session_service.create_session = AsyncMock()
    orch.runner.session_service.get_session = AsyncMock(
        return_value=MagicMock(state=state)
    )

    result = await orch.handle_failed_run("acme/widget", 42)

    assert result["outcome"] == "no_fix_found"
    mock_github.open_fix_pr.assert_not_called()
    mock_github.comment_on_commit.assert_called_once()
    assert mock_memory.record_incident.call_args.args[0].outcome == "no_fix_found"
