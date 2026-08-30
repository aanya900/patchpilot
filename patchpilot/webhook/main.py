"""
PatchPilot webhook service — deployed on Cloud Run.

Receives GitHub `workflow_run` webhook events. On a failed run, it publishes
a job onto Pub/Sub (so the webhook responds to GitHub in milliseconds) and a
separate Cloud Run push subscription endpoint (`/tasks/process-run`) picks
the job up and runs the full agent pipeline, which can take a minute or two.

This two-endpoint split matters for the "long-running, asynchronous
background execution" requirement: GitHub's webhook delivery times out
around 10s, but diagnosing + patching + validating a CI failure with three
LLM calls plus GitHub API round-trips regularly takes longer than that.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

from fastapi import FastAPI, Header, HTTPException, Request
from google.cloud import pubsub_v1

from patchpilot.agents.orchestrator import PatchPilotOrchestrator

logger = logging.getLogger("patchpilot.webhook")
app = FastAPI(title="PatchPilot")

GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
PUBSUB_TOPIC = os.environ.get("PUBSUB_TOPIC", "patchpilot-failed-runs")

_publisher = pubsub_v1.PublisherClient() if GCP_PROJECT else None
try:
    _orchestrator = PatchPilotOrchestrator()
except Exception as exc:  # pragma: no cover - validates local/offline startup
    logger.warning("PatchPilot orchestrator unavailable at startup: %s", exc)
    _orchestrator = None


def _verify_signature(raw_body: bytes, signature_header: str | None) -> None:
    if not GITHUB_WEBHOOK_SECRET:
        return  # local dev without a configured secret
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Missing signature")
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid signature")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
):
    raw_body = await request.body()
    _verify_signature(raw_body, x_hub_signature_256)
    payload = json.loads(raw_body)

    if x_github_event != "workflow_run":
        return {"status": "ignored", "reason": f"event {x_github_event} not handled"}

    run = payload.get("workflow_run", {})
    if run.get("conclusion") != "failure":
        return {"status": "ignored", "reason": "run did not fail"}

    job = {
        "repo_full_name": payload["repository"]["full_name"],
        "run_id": run["id"],
    }

    if _publisher:
        topic_path = _publisher.topic_path(GCP_PROJECT, PUBSUB_TOPIC)
        _publisher.publish(topic_path, json.dumps(job).encode("utf-8"))
        return {"status": "queued", **job}

    if _orchestrator is None:
        raise HTTPException(
            status_code=503,
            detail="PatchPilot is not configured for local processing. Set GITHUB_TOKEN and, if needed, Google ADC or a Firestore service account.",
        )

    # No Pub/Sub configured (e.g. local dev) — process inline for convenience.
    result = await _orchestrator.handle_failed_run(**job)
    return {"status": "processed_inline", **job, "result": result}


@app.post("/tasks/process-run")
async def process_run(request: Request):
    """Push subscription endpoint that Pub/Sub calls with the queued job.

    See infra/pubsub_push_subscription.tf for how this is wired up; Pub/Sub
    push auth (OIDC token) should be enforced at the Cloud Run ingress layer
    via `--no-allow-unauthenticated` + an invoker service account, not here.
    """
    envelope = await request.json()
    try:
        message = envelope["message"]
        data = json.loads(
            __import__("base64").b64decode(message["data"]).decode("utf-8")
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Bad Pub/Sub envelope: {e}")

    if _orchestrator is None:
        raise HTTPException(
            status_code=503,
            detail="PatchPilot orchestrator is unavailable; configure Google ADC / service account credentials before processing queued runs.",
        )

    logger.info("Processing failed run: %s", data)
    result = await _orchestrator.handle_failed_run(
        repo_full_name=data["repo_full_name"], run_id=data["run_id"]
    )
    logger.info("Result: %s", result)
    return {"status": "done", "result": result}
