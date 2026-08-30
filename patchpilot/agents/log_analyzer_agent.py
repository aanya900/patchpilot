"""
LogAnalyzerAgent

First stage of the PatchPilot pipeline. Reads the raw CI log dump for a
failed run (plus any similar past incidents pulled from Memory Bank) and
produces a structured root-cause diagnosis that downstream agents consume.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from google.adk.agents import Agent

MODEL = "gemini-3.5-flash"


class Diagnosis(BaseModel):
    root_cause: str = Field(description="One or two sentence plain-English root cause")
    failure_type: str = Field(
        description="One of: dependency, syntax, test_assertion, flaky, config, infra, unknown"
    )
    offending_files: list[str] = Field(
        description="Repo-relative paths most likely responsible for the failure"
    )
    confidence: float = Field(description="0.0-1.0 confidence in this diagnosis")
    is_recurring: bool = Field(
        description="True if this matches a pattern seen in past incidents for this repo"
    )
    reasoning: str = Field(description="Short chain of evidence from the logs supporting the diagnosis")


INSTRUCTION = """\
You are a senior build engineer diagnosing a failed CI run.

You will be given:
1. The flattened CI log output for the failing workflow run.
2. A list of files changed in the commit that triggered the run.
3. Zero or more past incidents recorded for this repository (for pattern matching).

Your job is ONLY to diagnose — do not propose a fix here. Read the logs
carefully, work backward from the first real error (not downstream noise
caused by it), and identify:
- the true root cause
- which specific files are responsible
- whether this looks like a repeat of a past incident

Be conservative with confidence: if the logs are ambiguous or the failure
looks like infra flakiness (timeouts, network errors, runner OOM) rather
than a code defect, say so explicitly and set failure_type to "flaky" or
"infra" with lower confidence — PatchPilot should not attempt code changes
for those cases.

Respond ONLY with the structured Diagnosis output.
"""

log_analyzer_agent = Agent(
    name="log_analyzer_agent",
    model=MODEL,
    description="Diagnoses the root cause of a failed CI run from its logs.",
    instruction=INSTRUCTION,
    output_schema=Diagnosis,
    output_key="diagnosis",
)
