"""
ValidatorAgent

Final gate before PatchPilot is allowed to open a PR. Re-reads the proposed
patch against the original diagnosis with an adversarial eye: does this
patch actually address the stated root cause, does it introduce obvious new
risk, and does it stay within the "minimal, targeted fix" mandate.

This is the guardrail that keeps a single-pass LLM patch from being merged
on trust alone — PatchPilot always opens a PR for human review, never
auto-merges, but this stage stops obviously-bad patches from reaching a
human's queue at all.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from google.adk.agents import Agent

MODEL = "gemini-3.5-flash"


class ValidationResult(BaseModel):
    approved: bool = Field(description="True if the patch is safe to open as a PR")
    concerns: list[str] = Field(
        default_factory=list, description="Specific issues found, empty if none"
    )
    summary: str = Field(description="One sentence verdict for the audit log")


INSTRUCTION = """\
You are a skeptical senior reviewer performing a final safety check on an
LLM-generated patch before it is opened as a pull request for a human.

You will be given the original diagnosis and the proposed fix (full file
contents plus PR title/body).

Check for:
- Does the patch plausibly address the stated root_cause? Reject if it
  looks unrelated or only papers over a symptom.
- Is the change scoped to what's needed, or did it touch unrelated code?
- Are there any obviously dangerous changes: deleted error handling,
  disabled tests instead of fixing them, hardcoded secrets/credentials,
  removed security checks, broadened permissions?
- Is proposed_fix.can_fix even true? If the patch generator declined to
  fix, approved must be false.

Be strict. When in doubt, reject and list the concern — a rejected patch
just means PatchPilot files an issue instead of a PR, which is a safe
fallback. Approving a bad patch is the costly mistake.

Respond ONLY with the structured ValidationResult output.
"""

validator_agent = Agent(
    name="validator_agent",
    model=MODEL,
    description="Final adversarial safety check on a proposed patch before it becomes a PR.",
    instruction=INSTRUCTION,
    output_schema=ValidationResult,
    output_key="validation",
)
