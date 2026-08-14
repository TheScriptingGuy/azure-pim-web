"""FastAPI web server for Azure PIM activation and approval."""

from __future__ import annotations

import asyncio
import base64
import json
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from azure_pim_cli.chrome_launcher import DEFAULT_COPY_PROFILE, DEFAULT_PORT, launch_debug_chrome
from azure_pim_cli.graph_client import GraphClient, TokenExpired
from azure_pim_cli.token_grabber import DEFAULT_CHANNEL, grab_token
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import profiles as profiles_store
from . import service
from .models import (
    ActivatePayload,
    ActivateRequest,
    ActivateResult,
    ActiveGroupItem,
    ApprovalItem,
    ApproveRequest,
    ApproveResult,
    EligibilityItem,
    Profile,
    ProfileActivateRequest,
    ProfileItemStatus,
    ProfileSaveRequest,
    ProfileStatus,
    TokenSetRequest,
    TokenStatus,
)

app = FastAPI(title="PIM Web")

_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC), name="static")

# In-memory single-user session state.
_state: dict[str, Any] = {
    "token": None,
    "token_exp": None,
    "upn": None,
    "principal_id": None,
    "elig_raw": [],  # enriched eligibility dicts cached after last /api/eligibilities call
    "cdp_endpoint": None,  # set by /api/token/grab; used to auto-prime acrs=c1 on activation
}


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}


def _token_valid() -> bool:
    exp = _state.get("token_exp")
    return bool(_state.get("token")) and isinstance(exp, (int, float)) and datetime.now(UTC).timestamp() < exp


def _require_client() -> GraphClient:
    if not _token_valid():
        raise HTTPException(status_code=401, detail="Token missing or expired. Grab a new token first.")
    return GraphClient(_state["token"])


async def _resolve_principal(gc: GraphClient) -> str:
    pid = _state.get("principal_id")
    if not pid:
        me = await service.get_me(gc)
        pid = me["id"]
        _state["principal_id"] = pid
    return pid


async def _ensure_cdp_endpoint() -> str:
    """Return cached CDP endpoint or launch/attach Chrome on the debug port and cache it.

    Idempotent: `launch_debug_chrome` attaches to a Chrome already listening on the port
    instead of spawning a duplicate. Lets acrs re-grab work even when the server
    restarted or the user loaded a token via `/api/token/set` (never hit `/api/token/grab`).
    """
    cdp = _state.get("cdp_endpoint")
    if cdp:
        return cdp
    loop = asyncio.get_event_loop()
    cdp = await loop.run_in_executor(
        None,
        lambda: launch_debug_chrome(port=DEFAULT_PORT, copy_profile=DEFAULT_COPY_PROFILE),
    )
    _state["cdp_endpoint"] = cdp
    return cdp


_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html", headers=_NO_CACHE_HEADERS)


@app.get("/api/token/status", response_model=TokenStatus)
async def token_status() -> TokenStatus:
    exp = _state.get("token_exp")
    expiry_str = datetime.fromtimestamp(exp, tz=UTC).strftime("%Y-%m-%d %H:%M UTC") if exp else None
    return TokenStatus(valid=_token_valid(), expiry=expiry_str, upn=_state.get("upn"))


@app.post("/api/token/grab")
async def token_grab() -> JSONResponse:
    loop = asyncio.get_event_loop()
    try:
        cdp_endpoint = await _ensure_cdp_endpoint()
        token = await loop.run_in_executor(
            None,
            lambda: grab_token(cdp_endpoint=cdp_endpoint, channel=DEFAULT_CHANNEL, require_acrs=True),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Token grab failed: {exc}") from exc

    _apply_token(token)
    exp = _state.get("token_exp")
    expiry_str = datetime.fromtimestamp(exp, tz=UTC).strftime("%Y-%m-%d %H:%M UTC") if exp else None
    return JSONResponse({"ok": True, "expiry": expiry_str, "upn": _state.get("upn")})


@app.post("/api/token/set")
async def token_set(body: TokenSetRequest) -> JSONResponse:
    _apply_token(body.token.strip())
    exp = _state.get("token_exp")
    expiry_str = datetime.fromtimestamp(exp, tz=UTC).strftime("%Y-%m-%d %H:%M UTC") if exp else None
    return JSONResponse({"ok": True, "expiry": expiry_str, "upn": _state.get("upn")})


async def _activate_with_acrs_retry(
    gc: GraphClient,
    principal_id: str,
    payloads: list[ActivatePayload],
    elig_raw: list[dict],
) -> list[ActivateResult]:
    """Run activation; on AcrsValidationFailed, open PIM blade in a fresh tab, sniff c1 token, retry.

    Graph rejects tokens whose `acrs` claim lacks `c1` (or whose step-up MFA auth-time is
    too stale for the tenant's Conditional Access policy). `grab_token(require_acrs=True)`
    reuses the existing Chrome (via CDP) to open the PIM activation blade — portal's own
    XHRs then mint a fresh c1 token which we sniff off the Authorization header. If Chrome
    isn't attached yet (server restarted, or user set token via /api/token/set),
    `_ensure_cdp_endpoint` launches/attaches it lazily so no prior `/api/token/grab` call
    is required.
    """
    results = await service.activate_items(gc, principal_id, payloads, elig_raw)

    def _needs_acrs(r: ActivateResult) -> bool:
        return r.status == "Failed" and "AcrsValidationFailed" in (r.detail or "")

    failed_keys = {(r.groupId, r.accessId) for r in results if _needs_acrs(r)}
    if not failed_keys:
        return results

    loop = asyncio.get_event_loop()
    try:
        cdp_endpoint = await _ensure_cdp_endpoint()
        new_token = await loop.run_in_executor(
            None,
            lambda: grab_token(
                cdp_endpoint=cdp_endpoint,
                channel=DEFAULT_CHANNEL,
                require_acrs=True,
            ),
        )
    except Exception as exc:
        for r in results:
            if _needs_acrs(r):
                r.detail = f"{r.detail} | acrs re-grab failed: {exc}"
        return results

    _apply_token(new_token)
    retry_payloads = [p for p in payloads if (p.groupId, p.accessId) in failed_keys]
    gc2 = GraphClient(new_token)
    try:
        retry_results = await service.activate_items(gc2, principal_id, retry_payloads, elig_raw)
    finally:
        await gc2.aclose()

    keep = [r for r in results if (r.groupId, r.accessId) not in failed_keys]
    return keep + retry_results


def _apply_token(token: str) -> None:
    payload = _decode_jwt_payload(token)
    _state["token"] = token
    _state["token_exp"] = payload.get("exp")
    _state["upn"] = payload.get("upn") or payload.get("preferred_username")
    _state["principal_id"] = payload.get("oid")
    _state["elig_raw"] = []  # invalidate cached eligibilities on token change


@app.get("/api/eligibilities", response_model=list[EligibilityItem])
async def eligibilities() -> list[EligibilityItem]:
    gc = _require_client()
    try:
        principal_id = await _resolve_principal(gc)
        items, raw = await service.get_eligibilities(gc, principal_id)
        _state["elig_raw"] = raw
        return items
    except TokenExpired:
        _state["token"] = None
        raise HTTPException(status_code=401, detail="Token expired.")
    finally:
        await gc.aclose()


@app.get("/api/approvals", response_model=list[ApprovalItem])
async def approvals() -> list[ApprovalItem]:
    gc = _require_client()
    try:
        return await service.get_approvals(gc)
    except TokenExpired:
        _state["token"] = None
        raise HTTPException(status_code=401, detail="Token expired.")
    finally:
        await gc.aclose()


@app.get("/api/active", response_model=list[ActiveGroupItem])
async def active_groups() -> list[ActiveGroupItem]:
    gc = _require_client()
    try:
        return await service.get_active_assignments(gc)
    except TokenExpired:
        _state["token"] = None
        raise HTTPException(status_code=401, detail="Token expired.")
    finally:
        await gc.aclose()


@app.post("/api/activate", response_model=list[ActivateResult])
async def activate(body: ActivateRequest) -> list[ActivateResult]:
    elig_raw = _state.get("elig_raw") or []
    if not elig_raw:
        raise HTTPException(status_code=400, detail="Load eligibilities first before activating.")
    gc = _require_client()
    try:
        principal_id = await _resolve_principal(gc)
        return await _activate_with_acrs_retry(gc, principal_id, body.items, elig_raw)
    except TokenExpired:
        _state["token"] = None
        raise HTTPException(status_code=401, detail="Token expired.")
    finally:
        await gc.aclose()


@app.post("/api/approve", response_model=list[ApproveResult])
async def approve(body: ApproveRequest) -> list[ApproveResult]:
    gc = _require_client()
    try:
        return await service.approve_items(gc, body.items)
    except TokenExpired:
        _state["token"] = None
        raise HTTPException(status_code=401, detail="Token expired.")
    finally:
        await gc.aclose()


@app.get("/api/profiles", response_model=list[ProfileStatus])
async def list_profiles() -> list[ProfileStatus]:
    elig_raw = _state.get("elig_raw") or []
    elig_map = {(e["groupId"], e["accessId"]): e for e in elig_raw}
    result: list[ProfileStatus] = []
    for p in profiles_store.list_profiles():
        items: list[ProfileItemStatus] = []
        for it in p.items:
            e = elig_map.get((it.groupId, it.accessId))
            available = e is not None
            items.append(
                ProfileItemStatus(
                    **it.model_dump(),
                    available=available,
                    policyMaxDurationHours=int(e.get("policyMaxDurationHours") or 8) if e else None,
                    requiresTicket=bool(e.get("requiresTicket", False)) if e else False,
                    requiresMfa=bool(e.get("requiresMfa", False)) if e else False,
                    unavailableReason=None if available else "Not in current eligibilities",
                )
            )
        result.append(ProfileStatus(id=p.id, name=p.name, items=items))
    return result


@app.post("/api/profiles", response_model=Profile)
async def create_profile(req: ProfileSaveRequest) -> Profile:
    return profiles_store.upsert_profile(None, req)


@app.put("/api/profiles/{pid}", response_model=Profile)
async def update_profile(pid: str, req: ProfileSaveRequest) -> Profile:
    try:
        return profiles_store.upsert_profile(pid, req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc


@app.delete("/api/profiles/{pid}")
async def delete_profile(pid: str) -> JSONResponse:
    if not profiles_store.delete_profile(pid):
        raise HTTPException(status_code=404, detail="Profile not found")
    return JSONResponse({"ok": True})


@app.post("/api/profiles/{pid}/activate", response_model=list[ActivateResult])
async def activate_profile(pid: str, body: ProfileActivateRequest) -> list[ActivateResult]:
    p = profiles_store.get_profile(pid)
    if p is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    elig_raw = _state.get("elig_raw") or []
    if not elig_raw:
        raise HTTPException(status_code=400, detail="Load eligibilities first before activating.")
    elig_map = {(e["groupId"], e["accessId"]): e for e in elig_raw}

    payloads: list[ActivatePayload] = []
    skipped: list[ActivateResult] = []
    for it in p.items:
        e = elig_map.get((it.groupId, it.accessId))
        if e is None:
            skipped.append(
                ActivateResult(
                    groupId=it.groupId,
                    accessId=it.accessId,
                    status="Unavailable",
                    detail="Role no longer eligible — skipped",
                )
            )
            continue
        max_h = int(e.get("policyMaxDurationHours") or 8)
        requested = it.durationHours or body.durationHours or max_h
        payloads.append(
            ActivatePayload(
                groupId=it.groupId,
                accessId=it.accessId,
                durationHours=min(requested, max_h),
                justification=body.justification,
                ticketNumber=it.ticketNumber,
            )
        )

    if not payloads:
        return skipped

    gc = _require_client()
    try:
        principal_id = await _resolve_principal(gc)
        results = await _activate_with_acrs_retry(gc, principal_id, payloads, elig_raw)
    except TokenExpired as exc:
        _state["token"] = None
        raise HTTPException(status_code=401, detail="Token expired.") from exc
    finally:
        await gc.aclose()
    return skipped + results


def start() -> None:
    webbrowser.open("http://127.0.0.1:8080")
    uvicorn.run("pim_web.main:app", host="127.0.0.1", port=8080, reload=False)


if __name__ == "__main__":
    start()
