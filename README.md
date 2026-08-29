# PatchPilot

**An autonomous agent that diagnoses failed CI runs and opens a fix PR — no human triage required.**

Built for the *All Things Agentic* hackathon — **Taskmaster** track.

## The problem

Every failed CI build costs a developer the same 15 minutes: open the run, scroll to the real error buried under noise, figure out what actually broke, fix it, push again. Most of these failures are small and mechanical — a stale dependency pin, a typo, a broken import — but the triage time is fixed regardless of how trivial the fix is.

## What PatchPilot does

PatchPilot watches a repo's GitHub Actions runs. When one fails, it:

1. **Diagnoses** the root cause from the raw CI logs (and remembers past incidents for that repo, so recurring failures are recognized, not re-derived).
2. **Proposes** a minimal, targeted code fix — or explicitly declines if it isn't confident.
3. **Validates** its own proposed fix with an adversarial second pass before it's allowed to become a PR.
4. **Opens a pull request** with the fix and its full reasoning, or, if no safe fix exists, **comments the diagnosis** on the failing commit so a human has a head start.

It never auto-merges. A human always reviews the PR.

## Why this is a real agent, not a chatbot

- It runs **asynchronously in the background**, triggered by a webhook, with no user in the loop.
- It **takes action** — opens a PR, comments on a commit — rather than just producing text a human has to act on.
- It **maintains memory across runs**, using a per-repo incident history to inform future diagnoses.
- It has a **built-in safety gate**: a dedicated validator agent that can veto its own team's proposed patch.

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the full diagram and component breakdown. In short:

```
GitHub (CI fails) → Cloud Run webhook → Pub/Sub → Cloud Run worker
                                                        │
                                        ADK pipeline: LogAnalyzer → PatchGenerator → Validator
                                          (Gemini 3.5 Flash, Firestore-backed memory)
                                                        │
                                       GitHub PR (approved fix) or commit comment (no fix)
```

**Google technologies used:**
- **Gemini 3.5 Flash** via Vertex AI, for all three agent stages
- **Google ADK** (`google-adk`) — a `SequentialAgent` pipeline of three structured-output `Agent`s
- **Cloud Run** — hosts both the webhook receiver and the async worker
- **Pub/Sub** — decouples GitHub's short webhook timeout from the multi-minute agent pipeline
- **Firestore** — per-repo incident memory ("Memory Bank" pattern)

## Project layout

```
patchpilot/
  agents/
    log_analyzer_agent.py      # Stage 1: root-cause diagnosis
    patch_generator_agent.py   # Stage 2: minimal fix proposal
    validator_agent.py         # Stage 3: adversarial safety check
    orchestrator.py            # Wires the pipeline + drives GitHub/Firestore side effects
  services/
    github_client.py           # Log fetching, file reads, PR/comment creation
    memory_store.py             # Firestore-backed incident history
  webhook/
    main.py                     # FastAPI app: webhook receiver + Pub/Sub worker endpoint
tests/
  test_orchestrator.py          # Mocked unit tests for the branching logic
docs/
  architecture.md
Dockerfile
requirements.txt
.env.example
```

## Spin-up instructions

### 1. Prerequisites
- Python 3.12+
- A GCP project with Vertex AI, Cloud Run, Pub/Sub, and Firestore enabled
- A GitHub App or PAT with `repo` + `workflow` scopes, installed on the target repo(s)

### 2. Local setup
```bash
git clone <this-repo-url> && cd patchpilot
pip install -r requirements.txt
cp .env.example .env   # fill in GITHUB_TOKEN at minimum
```

### 3. Run tests (no GCP credentials needed — fully mocked)
```bash
PYTHONPATH=. pytest tests/ -v
```

### 4. Run locally
```bash
export $(cat .env | xargs)
uvicorn patchpilot.webhook.main:app --reload --port 8080
```
Without `GCP_PROJECT` set, the webhook processes jobs inline (synchronously) for easy local testing — point a GitHub webhook at `http://<ngrok-url>/webhook/github` with content type `application/json` and the `workflow_run` event enabled.

### 5. Deploy to Cloud Run
```bash
gcloud builds submit --tag gcr.io/$GCP_PROJECT/patchpilot
gcloud run deploy patchpilot \
  --image gcr.io/$GCP_PROJECT/patchpilot \
  --set-env-vars GCP_PROJECT=$GCP_PROJECT,PUBSUB_TOPIC=patchpilot-failed-runs,GOOGLE_GENAI_USE_VERTEXAI=true \
  --no-allow-unauthenticated   # webhook endpoint should sit behind its own signature check;
                                # the /tasks/process-run endpoint should only be reachable by
                                # the Pub/Sub push subscription's invoker service account
```
Create the Pub/Sub topic and a push subscription targeting `https://<cloud-run-url>/tasks/process-run`, and create the Firestore database in Native mode. Register the webhook URL (`/webhook/github`) on each repo you want PatchPilot watching.

## Cost notes
Every stage uses **Gemini 3.5 Flash** specifically to keep inference cost low relative to a Pro-tier model, since diagnosis/patch/validation don't need frontier reasoning depth for the class of bugs PatchPilot targets. Cloud Run scales to zero between failed builds, so idle cost is ~$0.

## What we'd build next
- Vector-embedding-based similarity search over past incidents (currently a simple substring match, noted in `memory_store.py`) for better recurring-failure detection
- A GitHub Check Run UI showing live pipeline progress instead of only the final PR/comment
- Support for monorepos with per-package diagnosis scoping
