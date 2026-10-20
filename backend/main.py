"""
Medarina FastAPI backend.

Path bootstrap — ensures sibling modules (models, database, …) resolve
correctly whether uvicorn is launched from the project root or from inside
the backend/ directory.

Endpoints
---------
GET  /health
GET  /api/investigations
POST /api/investigations
GET  /api/investigations/{id}
PATCH /api/investigations/{id}
DELETE /api/investigations/{id}
POST /api/investigations/{id}/evidence
POST /api/investigations/{id}/retrieve
POST /api/investigations/{id}/analyse
POST /api/investigations/{id}/reports
GET  /api/investigations/{id}/reports
GET  /api/reports/{report_id}
GET  /api/reports
POST /api/tinyfish/test
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from models import (
    AddEvidenceRequest,
    CreateInvestigationRequest,
    Evidence,
    HealthResponse,
    Investigation,
    RunRetrievalRequest,
    SavedReport,
    UpdateInvestigationRequest,
)
import database as db
import gemini_service as gemini
import tinyfish_service as tf_svc

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CORS configuration
# ---------------------------------------------------------------------------

_ALLOWED_ORIGINS_RAW = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:5173,http://localhost:8443,http://localhost:3000,http://127.0.0.1:5173",
)
ALLOWED_ORIGINS = [o.strip() for o in _ALLOWED_ORIGINS_RAW.split(",") if o.strip()]

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Medarina backend starting. Allowed origins: %s", ALLOWED_ORIGINS)
    yield
    log.info("Medarina backend stopped.")


app = FastAPI(
    title="Medarina API",
    description="Healthcare claims investigation platform backend.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse, tags=["System"])
def health():
    return HealthResponse(
        status="ok",
        version="1.0.0",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Investigations
# ---------------------------------------------------------------------------


@app.get("/api/investigations", tags=["Investigations"])
def list_investigations(
    archived: Optional[bool] = Query(None),
    status: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    items = db.list_investigations(
        archived=archived, status=status, category=category, q=q,
        limit=limit, offset=offset
    )
    return {"items": items, "total": len(items)}


@app.post("/api/investigations", tags=["Investigations"], status_code=201)
async def create_investigation(body: CreateInvestigationRequest, background_tasks: BackgroundTasks):
    inv = db.create_investigation(body.model_dump())
    # Start retrieval in background if requested
    if body.runNow:
        background_tasks.add_task(_run_retrieval_task, inv["id"])
    log.info("Created investigation %s (ref=%s)", inv["id"], inv["ref"])
    return inv


@app.get("/api/investigations/{inv_id}", tags=["Investigations"])
def get_investigation(inv_id: str):
    inv = db.get_investigation(inv_id)
    if inv is None:
        raise HTTPException(404, detail="Investigation not found.")
    return inv


@app.patch("/api/investigations/{inv_id}", tags=["Investigations"])
def update_investigation(inv_id: str, body: UpdateInvestigationRequest):
    patch = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not patch:
        raise HTTPException(400, detail="No fields to update.")
    updated = db.update_investigation(inv_id, patch)
    if updated is None:
        raise HTTPException(404, detail="Investigation not found.")
    return updated


@app.delete("/api/investigations/{inv_id}", tags=["Investigations"], status_code=204)
def delete_investigation(inv_id: str):
    ok = db.delete_investigation(inv_id)
    if not ok:
        raise HTTPException(404, detail="Investigation not found.")


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@app.post("/api/investigations/{inv_id}/evidence", tags=["Evidence"], status_code=201)
def add_evidence(inv_id: str, body: AddEvidenceRequest):
    ev_data = body.model_dump()
    ev_data["retrieval"] = "not_attempted"
    saved = db.add_evidence(inv_id, ev_data)
    if saved is None:
        raise HTTPException(404, detail="Investigation not found.")
    return saved


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


@app.post("/api/investigations/{inv_id}/retrieve", tags=["Retrieval"])
async def trigger_retrieval(
    inv_id: str,
    body: Optional[RunRetrievalRequest] = None,
    background_tasks: BackgroundTasks = None,
):
    inv = db.get_investigation(inv_id)
    if inv is None:
        raise HTTPException(404, detail="Investigation not found.")
    if inv.get("sample"):
        raise HTTPException(400, detail="Retrieval is disabled for sample investigations.")

    endpoint = (body.tinyfishEndpoint if body else None) or None
    api_key = (body.tinyfishApiKey if body else None) or None

    background_tasks.add_task(_run_retrieval_task, inv_id, endpoint, api_key)
    db.update_investigation(
        inv_id,
        {
            "status": "retrieving",
            "retrievalError": None,
            "retrieval": {"status": "retrieving", "startedAt": datetime.now(timezone.utc).isoformat()},
        },
    )
    return {"message": "Retrieval started.", "investigationId": inv_id}


async def _run_retrieval_task(inv_id: str, endpoint: Optional[str] = None, api_key: Optional[str] = None):
    from datetime import datetime, timezone

    inv = db.get_investigation(inv_id)
    if inv is None:
        return

    started_at = datetime.now(timezone.utc).isoformat()
    result = await tf_svc.retrieve_evidence(inv, endpoint=endpoint, api_key=api_key)
    completed_at = datetime.now(timezone.utc).isoformat()

    if result["status"] == "completed":
        user_evidence = [
            e for e in (inv.get("evidence") or [])
            if isinstance(e, dict) and e.get("origin") == "user-provided"
        ]
        new_evidence = result.get("evidence") or []
        has_conflict = any(
            e.get("classification") == "conflicting" for e in new_evidence
        )
        patch = {
            "evidence": user_evidence + new_evidence,
            "summary": result.get("summary"),
            "riskIndicators": result.get("riskIndicators") or [],
            "recommendation": result.get("recommendation"),
            "status": "review" if has_conflict else "pending",
            "retrievalError": None,
            "retrieval": {
                "status": "completed",
                "startedAt": started_at,
                "completedAt": completed_at,
                "message": result.get("message", ""),
            },
        }
        # Run Gemini analysis if evidence was retrieved
        if new_evidence:
            merged_inv = {**inv, **patch}
            analysis = gemini.analyse_evidence(merged_inv)
            if analysis.get("summary"):
                patch["summary"] = analysis["summary"]
            if analysis.get("riskIndicators"):
                patch["riskIndicators"] = list(set(patch["riskIndicators"] + analysis["riskIndicators"]))
            if analysis.get("recommendation") and not patch.get("recommendation"):
                patch["recommendation"] = analysis["recommendation"]

        db.update_investigation(inv_id, patch)
        log.info("Retrieval completed for %s: %s source(s)", inv_id, len(new_evidence))
    else:
        db.update_investigation(
            inv_id,
            {
                "status": "failed" if result["status"] == "failed" else "pending",
                "retrievalError": result.get("message"),
                "retrieval": {
                    "status": result["status"],
                    "startedAt": started_at,
                    "completedAt": completed_at,
                    "message": result.get("message"),
                },
            },
        )
        log.warning("Retrieval %s for %s: %s", result["status"], inv_id, result.get("message"))


# ---------------------------------------------------------------------------
# AI analysis
# ---------------------------------------------------------------------------


@app.post("/api/investigations/{inv_id}/analyse", tags=["AI"])
def analyse(inv_id: str):
    inv = db.get_investigation(inv_id)
    if inv is None:
        raise HTTPException(404, detail="Investigation not found.")

    questions = gemini.verification_questions(inv)
    analysis = gemini.analyse_evidence(inv)

    patch: dict = {}
    if analysis.get("summary"):
        patch["summary"] = analysis["summary"]
    if analysis.get("riskIndicators") is not None:
        patch["riskIndicators"] = analysis["riskIndicators"]
    if analysis.get("recommendation"):
        patch["recommendation"] = analysis["recommendation"]
    if patch:
        db.update_investigation(inv_id, patch)

    return {
        "verificationQuestions": questions,
        "summary": analysis.get("summary"),
        "riskIndicators": analysis.get("riskIndicators") or [],
        "recommendation": analysis.get("recommendation"),
    }


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@app.post("/api/investigations/{inv_id}/reports", tags=["Reports"], status_code=201)
def save_report(inv_id: str):
    report = db.save_report(inv_id)
    if report is None:
        raise HTTPException(404, detail="Investigation not found.")
    return report


@app.get("/api/investigations/{inv_id}/reports", tags=["Reports"])
def list_inv_reports(inv_id: str):
    if db.get_investigation(inv_id) is None:
        raise HTTPException(404, detail="Investigation not found.")
    return {"items": db.list_reports(inv_id)}


@app.get("/api/reports", tags=["Reports"])
def list_all_reports():
    return {"items": db.list_reports()}


@app.get("/api/reports/{report_id}", tags=["Reports"])
def get_report(report_id: str):
    report = db.get_report(report_id)
    if report is None:
        raise HTTPException(404, detail="Report not found.")
    return report


# ---------------------------------------------------------------------------
# TinyFish proxy / ping
# ---------------------------------------------------------------------------


@app.post("/api/tinyfish/test", tags=["TinyFish"])
async def test_tinyfish(body: dict):
    endpoint = body.get("endpoint", "")
    api_key = body.get("apiKey", "")
    result = await tf_svc.test_connection(endpoint, api_key)
    return result


@app.post("/api/tinyfish/retrieve", tags=["TinyFish"])
async def tinyfish_retrieve(body: dict):
    """
    Pass-through endpoint: the frontend's tinyfish.ts can point its
    retrievalEndpoint to this URL.  We forward to the real TinyFish API
    using the server-side credentials, then return the result to the browser.
    """
    if body.get("ping"):
        return {"pong": True}

    inv_stub = {
        "claim": body.get("claim", ""),
        "category": body.get("category", ""),
        "details": body.get("details", ""),
        "provider": body.get("provider"),
        "sourceUrl": body.get("sourceUrl"),
        "billingCodes": body.get("billingCodes"),
        "serviceDateFrom": body.get("serviceDateFrom"),
        "serviceDateTo": body.get("serviceDateTo"),
        "billedAmount": body.get("billedAmount"),
        "evidence": [],
    }

    result = await tf_svc.retrieve_evidence(inv_stub)
    if result["status"] == "not_configured":
        raise HTTPException(503, detail=result.get("message", "TinyFish not configured."))
    if result["status"] == "failed":
        raise HTTPException(502, detail=result.get("message", "Retrieval failed."))
    return result
