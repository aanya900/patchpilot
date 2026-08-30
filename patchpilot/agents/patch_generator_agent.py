"""
PatchGeneratorAgent

Second stage of the pipeline. Takes the LogAnalyzerAgent's structured
diagnosis plus the current contents of the offending files, and produces
a minimal, targeted patch: full new file contents for each file it touches,
plus a human-readable PR title/body explaining the change.

Deliberately scoped to *small, defensible* fixes (dependency pins, obvious
null checks, off-by-one/typo-class bugs, config corrections). Anything the
model isn't confident about should come back with proposed_fix=None so the
pipeline can fall back to filing an issue instead of guessing.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from google.adk.agents import Agent

MODEL = "gemini-3.5-flash"


class FilePatch(BaseModel):
    path: str = Field(description="Repo-relative file path")
    new_content: str = Field(description="The full new content of the file after the fix")
    explanation: str = Field(description="One sentence on what changed in this file and why")


class ProposedFix(BaseModel):
    can_fix: bool = Field(description="False if no safe, confident fix is possible")
    patches: list[FilePatch] = Field(default_factory=list)
    pr_title: str = Field(description="Concise PR title, e.g. 'fix: pin requests to 2.32.x'")
    pr_body: str = Field(description="PR description: root cause, what changed, why it's safe")
    risk_notes: str = Field(
        description="Any caveats a human reviewer should double-check before merging"
    )


INSTRUCTION = """\
You are a careful software engineer writing a minimal fix for a CI failure.

You will be given:
1. The structured diagnosis from the log analysis stage (root cause, failure
   type, offending files, confidence, reasoning).
2. The current full contents of each offending file.

Rules:
- Only propose a fix if the diagnosis confidence is reasonably high AND the
  failure_type is one you can safely address in code (dependency, syntax,
  test_assertion, config). For failure_type "flaky", "infra", or "unknown",
  set can_fix to false — do not guess at a code change for those.
- Make the SMALLEST change that plausibly fixes the root cause. Do not
  refactor unrelated code, do not reformat whole files, do not change
  unrelated logic.
- Return the FULL new content for every file you touch, not a diff snippet.
- Write a clear PR title and body: state the root cause in one sentence,
  explain what changed, and note test coverage or manual verification the
  reviewer should still do.
- Always fill risk_notes honestly, even for high-confidence fixes — e.g.
  "verified against the failing log, but no local test run was possible in
  this sandboxed environment."

Respond ONLY with the structured ProposedFix output.
"""

patch_generator_agent = Agent(
    name="patch_generator_agent",
    model=MODEL,
    description="Proposes a minimal, targeted code fix for a diagnosed CI failure.",
    instruction=INSTRUCTION,
    output_schema=ProposedFix,
    output_key="proposed_fix",
)
