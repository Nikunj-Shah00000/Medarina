"""
TinyFish service — server-side wrapper for live-web evidence retrieval.

Mirrors the frontend tinyfish.ts interface so the backend can be the
TinyFish proxy endpoint that the frontend settings point to, OR can call
a real TinyFish API if TINYFISH_API_KEY / TINYFISH_ENDPOINT are set.

When credentials are not configured, this module logs a clear warning
and returns a 'not_configured' result; it never fabricates evidence.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 20  # seconds

_TINYFISH_ENDPOINT = os.getenv("TINYFISH_ENDPOINT", "").strip()
_TINYFISH_API_KEY = os.getenv("TINYFISH_API_KEY", "").strip()

if _TINYFISH_ENDPOINT:
    log.info("TinyFish endpoint configured: %s", _TINYFISH_ENDPOINT)
else:
    log.warning(
        "TINYFISH_ENDPOINT not set – live evidence retrieval is not active. "
        "Set TINYFISH_ENDPOINT and TINYFISH_API_KEY in .env to enable it."
    )

CLASSIFICATION_VALUES = {"verified", "unverified", "conflicting", "inconclusive"}


async def retrieve_evidence(inv: dict, endpoint: Optional[str] = None, api_key: Optional[str] = None) -> dict:
    """
    Sends the investigation to TinyFish and returns a RetrievalResult-shaped dict.

    Returns:
        {
            "status": "completed"|"failed"|"not_configured",
            "message": str,
            "evidence": [...],   # only when status=="completed"
            "summary": str,
            "riskIndicators": [...],
            "recommendation": str,
        }
    """
    ep = (endpoint or _TINYFISH_ENDPOINT).strip()
    key = (api_key or _TINYFISH_API_KEY).strip()

    if not ep:
        return {
            "status": "not_configured",
            "message": (
                "No TinyFish endpoint configured. "
                "Set TINYFISH_ENDPOINT in the backend .env to enable live retrieval."
            ),
        }

    payload = {
        "claim": inv.get("claim", ""),
        "category": inv.get("category", ""),
        "details": inv.get("details", ""),
        "provider": inv.get("provider"),
        "sourceUrl": inv.get("sourceUrl"),
        "billingCodes": inv.get("billingCodes"),
        "serviceDateFrom": inv.get("serviceDateFrom"),
        "serviceDateTo": inv.get("serviceDateTo"),
        "billedAmount": inv.get("billedAmount"),
    }

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(ep, json=payload, headers=headers)

        if not resp.is_success:
            return {
                "status": "failed",
                "message": f"The TinyFish service responded with HTTP {resp.status_code}.",
            }

        data = resp.json()

    except httpx.TimeoutException:
        return {
            "status": "failed",
            "message": "The TinyFish service did not respond within 20 seconds.",
        }
    except httpx.RequestError as exc:
        return {
            "status": "failed",
            "message": f"Could not reach the TinyFish service ({exc}).",
        }
    except json.JSONDecodeError:
        return {
            "status": "failed",
            "message": "The TinyFish service returned an unrecognised response format.",
        }

    if not isinstance(data, dict) or not isinstance(data.get("evidence"), list):
        return {
            "status": "failed",
            "message": "The TinyFish service returned an unrecognised response format.",
        }

    now = datetime.now(timezone.utc).isoformat()

    def parse_ev(e: Any) -> Optional[dict]:
        if not isinstance(e, dict):
            return None
        title = e.get("title")
        url = e.get("url")
        if not isinstance(title, str) or not isinstance(url, str):
            return None
        classification = e.get("classification", "inconclusive")
        if classification not in CLASSIFICATION_VALUES:
            classification = "inconclusive"
        origin = e.get("origin", "ai-summary")
        if origin not in ("live-source", "ai-summary", "user-provided"):
            origin = "ai-summary"
        try:
            hostname = url.split("/")[2]
        except IndexError:
            hostname = url
        return {
            "id": str(uuid.uuid4()),
            "title": title,
            "url": url,
            "publisher": e.get("publisher") or hostname,
            "published": e.get("published") or None,
            "retrieval": "failed" if e.get("retrieval") == "failed" else "retrieved",
            "origin": origin,
            "classification": classification,
            "summary": str(e.get("summary", "")),
            "retrievedAt": now,
            "provider": "tinyfish",
            "verifies": e.get("verifies") or e.get("claimDetail") or None,
        }

    evidence = [ev for e in data["evidence"] for ev in [parse_ev(e)] if ev is not None]

    return {
        "status": "completed",
        "message": f"{len(evidence)} source{'s' if len(evidence) != 1 else ''} retrieved.",
        "evidence": evidence,
        "summary": str(data.get("summary", "")) or None,
        "riskIndicators": [
            r for r in (data.get("riskIndicators") or []) if isinstance(r, str)
        ],
        "recommendation": str(data.get("recommendation", "")) or None,
    }


async def test_connection(endpoint: str, api_key: str = "") -> dict:
    """Ping the endpoint to verify it's reachable."""
    if not endpoint.strip():
        return {"ok": False, "message": "No endpoint provided."}
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(endpoint.strip(), json={"ping": True}, headers=headers)
        return {
            "ok": resp.is_success,
            "message": f"Endpoint responded with HTTP {resp.status_code}.",
        }
    except httpx.TimeoutException:
        return {"ok": False, "message": "The endpoint did not respond within 20 seconds."}
    except httpx.RequestError as exc:
        return {"ok": False, "message": f"Could not reach the endpoint ({exc})."}
