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

from . import service
from .models import (
    ActiveGroupItem,
    ActivateRequest,
    ActivateResult,
    ApprovalItem,
    ApproveRequest,
    ApproveResult,
    EligibilityItem,
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


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.get("/api/token/status", response_model=TokenStatus)
async def token_status() -> TokenStatus:
    exp = _state.get("token_exp")
    expiry_str = datetime.fromtimestamp(exp, tz=UTC).strftime("%Y-%m-%d %H:%M UTC") if exp else None
    return TokenStatus(valid=_token_valid(), expiry=expiry_str, upn=_state.get("upn"))


@app.post("/api/token/grab")
async def token_grab() -> JSONResponse:
    loop = asyncio.get_event_loop()
    try:
        cdp_endpoint = await loop.run_in_executor(
            None,
            lambda: launch_debug_chrome(port=DEFAULT_PORT, copy_profile=DEFAULT_COPY_PROFILE),
        )
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
        return await service.activate_items(gc, principal_id, body.items, elig_raw)
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


def start() -> None:
    webbrowser.open("http://127.0.0.1:8080")
    uvicorn.run("pim_web.main:app", host="127.0.0.1", port=8080, reload=False)


if __name__ == "__main__":
    start()
