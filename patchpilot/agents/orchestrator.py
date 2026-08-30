"""
PatchPilot orchestrator.

Wires log_analyzer_agent -> patch_generator_agent -> validator_agent into a
single ADK SequentialAgent, then drives the surrounding side effects
(fetching logs, reading files, opening the PR, writing to Memory Bank) that
sit outside the pure LLM reasoning.

Flow for one failed CI run:

  1. Fetch failing run logs + changed files          (GitHubClient)
  2. Pull recent incidents for this repo              (MemoryStore)
  3. Run the ADK pipeline: diagnose -> patch -> validate
  4. If validated and approved:
       - open a PR with the patch
       - record the incident as "pr_opened"
     Else:
       - comment on the commit with the diagnosis so a human has a head start
       - record the incident as "no_fix_found" / "validation_failed"
"""

from __future__ import annotations

import logging
import uuid

from google.adk.agents import SequentialAgent
from google.adk.runners import InMemoryRunner
from google.genai import types

from patchpilot.agents.log_analyzer_agent import log_analyzer_agent
from patchpilot.agents.patch_generator_agent import patch_generator_agent
from patchpilot.agents.validator_agent import validator_agent
from patchpilot.services.github_client import GitHubClient
from patchpilot.services.memory_store import Incident, MemoryStore

logger = logging.getLogger("patchpilot.orchestrator")

APP_NAME = "patchpilot"

pipeline = SequentialAgent(
    name="patchpilot_pipeline",
    description=(
        "End-to-end CI failure remediation pipeline: diagnose the failure, "
        "propose a minimal fix, then adversarially validate it before it's "
        "allowed to become a pull request."
    ),
    sub_agents=[log_analyzer_agent, patch_generator_agent, validator_agent],
)


class PatchPilotOrchestrator:
    def __init__(
        self,
        github_client: GitHubClient | None = None,
        memory_store: MemoryStore | None = None,
    ):
        self.github = github_client or GitHubClient()
        self.memory = memory_store or MemoryStore()
        self.runner = InMemoryRunner(agent=pipeline, app_name=APP_NAME)

    async def handle_failed_run(self, repo_full_name: str, run_id: int) -> dict:
        """Main entry point, called by the webhook handler for a
        `workflow_run` event with conclusion == "failure"."""

        ctx = self.github.fetch_failed_run_context(repo_full_name, run_id)
        past_incidents = self.memory.recent_incidents(repo_full_name, limit=5)

        file_contents = {
            path: self.github.get_file_contents(repo_full_name, path, ctx.head_sha)
            for path in ctx.changed_files
        }

        prompt = self._build_prompt(ctx, past_incidents, file_contents)

        session_id = str(uuid.uuid4())
        user_id = f"patchpilot-{repo_full_name.replace('/', '-')}"
        await self.runner.session_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )

        final_state: dict = {}
        async for event in self.runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        ):
            if event.is_final_response():
                session = await self.runner.session_service.get_session(
                    app_name=APP_NAME, user_id=user_id, session_id=session_id
                )
                final_state = session.state

        diagnosis = final_state.get("diagnosis", {})
        proposed_fix = final_state.get("proposed_fix", {})
        validation = final_state.get("validation", {})

        if proposed_fix.get("can_fix") and validation.get("approved"):
            branch_name = f"patchpilot/fix-run-{run_id}"
            patches = {p["path"]: p["new_content"] for p in proposed_fix.get("patches", [])}
            pr_url = self.github.open_fix_pr(
                full_name=repo_full_name,
                base_branch=ctx.head_branch,
                head_sha=ctx.head_sha,
                branch_name=branch_name,
                file_patches=patches,
                title=proposed_fix["pr_title"],
                body=self._render_pr_body(proposed_fix, diagnosis, validation),
            )
            outcome = "pr_opened"
            self.memory.record_incident(
                Incident(
                    run_id=run_id,
                    repo_full_name=repo_full_name,
                    root_cause=diagnosis.get("root_cause", ""),
                    patch_summary=proposed_fix.get("pr_title", ""),
                    outcome=outcome,
                    pr_url=pr_url,
                )
            )
            return {"outcome": outcome, "pr_url": pr_url, "diagnosis": diagnosis}

        outcome = "validation_failed" if proposed_fix.get("can_fix") else "no_fix_found"
        self.github.comment_on_commit(
            repo_full_name,
            ctx.head_sha,
            self._render_no_fix_comment(diagnosis, validation),
        )
        self.memory.record_incident(
            Incident(
                run_id=run_id,
                repo_full_name=repo_full_name,
                root_cause=diagnosis.get("root_cause", ""),
                patch_summary="",
                outcome=outcome,
            )
        )
        return {"outcome": outcome, "diagnosis": diagnosis, "validation": validation}

    @staticmethod
    def _build_prompt(ctx, past_incidents: list[dict], file_contents: dict[str, str | None]) -> str:
        files_block = "\n\n".join(
            f"--- {path} ---\n{content or '(could not fetch file contents)'}"
            for path, content in file_contents.items()
        )
        history_block = (
            "\n".join(
                f"- [{i.get('outcome')}] {i.get('root_cause')}" for i in past_incidents
            )
            or "(no prior incidents recorded for this repo)"
        )
        return f"""\
Repository: {ctx.repo_full_name}
Failing workflow: {ctx.workflow_name} (run {ctx.run_id}, commit {ctx.head_sha})

## Past incidents for this repo
{history_block}

## CI log output
{ctx.logs_text}

## Changed files at the failing commit
{files_block}

Diagnose the failure, propose a minimal fix if one is safely possible, then
validate that fix before it can be opened as a pull request.
"""

    @staticmethod
    def _render_pr_body(proposed_fix: dict, diagnosis: dict, validation: dict) -> str:
        return f"""\
{proposed_fix.get('pr_body', '')}

---
**Opened automatically by PatchPilot**

- Root cause: {diagnosis.get('root_cause')}
- Diagnosis confidence: {diagnosis.get('confidence')}
- Validator verdict: {validation.get('summary')}
- Risk notes: {proposed_fix.get('risk_notes')}

Please review before merging — PatchPilot never auto-merges.
"""

    @staticmethod
    def _render_no_fix_comment(diagnosis: dict, validation: dict) -> str:
        concerns = "\n".join(f"- {c}" for c in validation.get("concerns", [])) or "(none)"
        return f"""\
🤖 **PatchPilot diagnosis** (no PR opened — see below)

**Root cause:** {diagnosis.get('root_cause')}
**Failure type:** {diagnosis.get('failure_type')}
**Confidence:** {diagnosis.get('confidence')}

{diagnosis.get('reasoning', '')}

A fix was {'not attempted' if not diagnosis else 'attempted but did not pass validation'}:
{concerns}

This diagnosis is left here to save a human triage step — no code change
was made automatically.
"""
