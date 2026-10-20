"""
Database layer — Supabase PostgreSQL via the supabase-py client.

Falls back to an in-memory store when SUPABASE_URL / SUPABASE_SERVICE_KEY
are not configured so that the server still starts and the frontend can
connect without a database.

All public functions accept / return plain dicts or lists of dicts that
correspond to the Pydantic models in models.py.
"""

from __future__ import annotations

import os
import uuid
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supabase client (optional)
# ---------------------------------------------------------------------------

_supabase = None

try:
    from supabase import create_client, Client

    _url = os.getenv("SUPABASE_URL", "").strip()
    _key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    if _url and _key:
        _supabase: Client = create_client(_url, _key)  # type: ignore[assignment]
        log.info("Supabase client initialised.")
    else:
        log.warning(
            "SUPABASE_URL or SUPABASE_SERVICE_KEY not set – using in-memory store."
        )
except Exception as exc:  # pragma: no cover
    log.warning("Could not initialise Supabase client: %s", exc)

# ---------------------------------------------------------------------------
# In-memory fallback
# ---------------------------------------------------------------------------

_mem_investigations: dict[str, dict] = {}
_mem_reports: dict[str, dict] = {}  # keyed by report id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inv_from_row(row: dict) -> dict:
    """
    Supabase stores nested JSON columns as Python objects already.
    We just normalise keys so the API returns camelCase.
    """
    return row


def _to_snake(d: dict) -> dict:
    """
    The DB schema uses snake_case columns; model uses camelCase.
    Map the fields that differ.
    """
    mapping = {
        "createdAt": "created_at",
        "sourceUrl": "source_url",
        "billedAmount": "billed_amount",
        "billingDate": "billing_date",
        "serviceDateFrom": "service_date_from",
        "serviceDateTo": "service_date_to",
        "billingCodes": "billing_codes",
        "clinicalTimeline": "clinical_timeline",
        "reviewerNotes": "reviewer_notes",
        "riskIndicators": "risk_indicators",
        "retrievalError": "retrieval_error",
    }
    result: dict = {}
    for k, v in d.items():
        result[mapping.get(k, k)] = v
    return result


def _from_snake(row: dict) -> dict:
    """DB row → camelCase dict."""
    mapping = {
        "created_at": "createdAt",
        "source_url": "sourceUrl",
        "billed_amount": "billedAmount",
        "billing_date": "billingDate",
        "service_date_from": "serviceDateFrom",
        "service_date_to": "serviceDateTo",
        "billing_codes": "billingCodes",
        "clinical_timeline": "clinicalTimeline",
        "reviewer_notes": "reviewerNotes",
        "risk_indicators": "riskIndicators",
        "retrieval_error": "retrievalError",
    }
    result: dict = {}
    for k, v in row.items():
        result[mapping.get(k, k)] = v
    return result


# ---------------------------------------------------------------------------
# Investigations CRUD
# ---------------------------------------------------------------------------


def list_investigations(
    archived: Optional[bool] = None,
    status: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    if _supabase:
        query = _supabase.table("investigations").select("*")
        if archived is not None:
            query = query.eq("archived", archived)
        if status:
            query = query.eq("status", status)
        if category:
            query = query.eq("category", category)
        if q:
            query = query.or_(
                f"ref.ilike.%{q}%,claim.ilike.%{q}%,summary.ilike.%{q}%,provider.ilike.%{q}%"
            )
        res = (
            query.order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return [_from_snake(r) for r in (res.data or [])]
    # in-memory
    items = list(_mem_investigations.values())
    if archived is not None:
        items = [i for i in items if i.get("archived", False) == archived]
    if status:
        items = [i for i in items if i.get("status") == status]
    if category:
        items = [i for i in items if i.get("category") == category]
    if q:
        ql = q.lower()
        items = [
            i
            for i in items
            if any(
                ql in str(i.get(f, "")).lower()
                for f in ["ref", "claim", "summary", "provider"]
            )
        ]
    items.sort(key=lambda i: i.get("createdAt", ""), reverse=True)
    return items[offset : offset + limit]


def get_investigation(inv_id: str) -> Optional[dict]:
    if _supabase:
        res = (
            _supabase.table("investigations").select("*").eq("id", inv_id).execute()
        )
        rows = res.data or []
        return _from_snake(rows[0]) if rows else None
    return _mem_investigations.get(inv_id)


def create_investigation(data: dict) -> dict:
    """
    data is a camelCase dict matching CreateInvestigationRequest fields.
    Returns the saved investigation dict.
    """
    year = datetime.now().year
    existing_count = len(_mem_investigations) if not _supabase else _count_real()
    ref = (data.get("ref") or "").strip() or f"INV-{year}-{str(existing_count + 1).zfill(4)}"

    inv: dict = {
        "id": str(uuid.uuid4()),
        "ref": ref,
        "createdAt": _now(),
        "claim": data["claim"].strip(),
        "category": data["category"],
        "details": (data.get("details") or "").strip(),
        "provider": (data.get("provider") or "").strip() or None,
        "sourceUrl": (data.get("sourceUrl") or "").strip() or None,
        "status": "pending",
        "sample": False,
        "summary": None,
        "evidence": [],
        "riskIndicators": [],
        "recommendation": None,
        "notes": "",
        "retrievalError": None,
        "outcome": None,
        "archived": False,
        "serviceDateFrom": data.get("serviceDateFrom") or None,
        "serviceDateTo": data.get("serviceDateTo") or None,
        "billingDate": data.get("billingDate") or None,
        "billedAmount": data.get("billedAmount"),
        "billingCodes": [c if isinstance(c, dict) else c.model_dump() for c in (data.get("billingCodes") or [])],
        "clinicalTimeline": [t if isinstance(t, dict) else t.model_dump() for t in (data.get("clinicalTimeline") or [])],
        "reviewerNotes": (data.get("reviewerNotes") or "").strip() or None,
        "retrieval": {"status": "queued"},
    }

    if _supabase:
        row = _to_snake(inv)
        # JSON columns
        for col in ("evidence", "risk_indicators", "billing_codes", "clinical_timeline", "retrieval"):
            if col in row and isinstance(row[col], (list, dict)):
                row[col] = json.dumps(row[col]) if False else row[col]  # supabase-py handles it
        _supabase.table("investigations").insert(row).execute()
    else:
        _mem_investigations[inv["id"]] = inv

    return inv


def _count_real() -> int:
    try:
        res = _supabase.table("investigations").select("id", count="exact").execute()
        return res.count or 0
    except Exception:
        return 0


def update_investigation(inv_id: str, patch: dict) -> Optional[dict]:
    """patch is camelCase."""
    existing = get_investigation(inv_id)
    if existing is None:
        return None

    updated = {**existing, **{k: v for k, v in patch.items() if v is not None}}
    # allow explicit False / empty list
    for k, v in patch.items():
        if v is False or v == [] or v == "":
            updated[k] = v

    if _supabase:
        snake_patch = _to_snake({k: v for k, v in patch.items()})
        _supabase.table("investigations").update(snake_patch).eq("id", inv_id).execute()
    else:
        _mem_investigations[inv_id] = updated

    return updated


def delete_investigation(inv_id: str) -> bool:
    if _supabase:
        _supabase.table("investigations").delete().eq("id", inv_id).execute()
        return True
    if inv_id in _mem_investigations:
        del _mem_investigations[inv_id]
        return True
    return False


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def add_evidence(inv_id: str, ev: dict) -> Optional[dict]:
    inv = get_investigation(inv_id)
    if inv is None:
        return None
    ev = {**ev, "id": str(uuid.uuid4()), "retrievedAt": _now()}
    evidence = list(inv.get("evidence", []))
    evidence.append(ev)
    update_investigation(inv_id, {"evidence": evidence})
    return ev


# ---------------------------------------------------------------------------
# Saved Reports
# ---------------------------------------------------------------------------


def save_report(inv_id: str) -> Optional[dict]:
    inv = get_investigation(inv_id)
    if inv is None:
        return None

    snapshot = {k: v for k, v in inv.items()}
    report: dict = {
        "id": str(uuid.uuid4()),
        "investigationId": inv_id,
        "generatedAt": _now(),
        "reviewStatus": inv.get("status", "pending"),
        "outcome": inv.get("outcome") or "Not determined",
        "snapshot": snapshot,
    }

    if _supabase:
        _supabase.table("saved_reports").insert(
            {
                "id": report["id"],
                "investigation_id": inv_id,
                "generated_at": report["generatedAt"],
                "review_status": report["reviewStatus"],
                "outcome": report["outcome"],
                "snapshot": snapshot,
            }
        ).execute()
    else:
        _mem_reports[report["id"]] = report

    return report


def list_reports(inv_id: Optional[str] = None) -> list[dict]:
    if _supabase:
        query = _supabase.table("saved_reports").select("*").order(
            "generated_at", desc=True
        )
        if inv_id:
            query = query.eq("investigation_id", inv_id)
        res = query.execute()
        rows = res.data or []
        return [
            {
                "id": r["id"],
                "investigationId": r["investigation_id"],
                "generatedAt": r["generated_at"],
                "reviewStatus": r["review_status"],
                "outcome": r["outcome"],
                "snapshot": r["snapshot"],
            }
            for r in rows
        ]
    reports = list(_mem_reports.values())
    if inv_id:
        reports = [r for r in reports if r["investigationId"] == inv_id]
    reports.sort(key=lambda r: r["generatedAt"], reverse=True)
    return reports


def get_report(report_id: str) -> Optional[dict]:
    if _supabase:
        res = (
            _supabase.table("saved_reports")
            .select("*")
            .eq("id", report_id)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r["id"],
            "investigationId": r["investigation_id"],
            "generatedAt": r["generated_at"],
            "reviewStatus": r["review_status"],
            "outcome": r["outcome"],
            "snapshot": r["snapshot"],
        }
    return _mem_reports.get(report_id)
