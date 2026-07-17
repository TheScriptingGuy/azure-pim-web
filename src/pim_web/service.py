"""Thin async service layer — delegates to azure_pim_cli business logic."""

from __future__ import annotations

import asyncio

from azure_pim_cli import cache as cache_mod
from azure_pim_cli.cli import (
    activate,
    approve,
    fetch_active_group_ids,
    fetch_eligibilities,
    fetch_me,
    fetch_pending_approvals,
)
from azure_pim_cli.graph_client import GraphClient

from .models import (
    ActivatePayload,
    ActivateResult,
    ActiveGroupItem,
    ApprovalItem,
    ApprovePayload,
    ApproveResult,
    EligibilityItem,
)

FETCH_WORKERS = 8


async def get_eligibilities(gc: GraphClient, principal_id: str) -> tuple[list[EligibilityItem], list[dict]]:
    """Return (UI models, raw enriched dicts). Raw dicts needed for activate_items."""
    cached = cache_mod.load()
    previous = cached["eligible"] if cached else None

    if cached is not None and cache_mod.is_fresh(cached, principal_id):
        raw = cached["eligible"]
    else:
        raw = await fetch_eligibilities(gc, principal_id, FETCH_WORKERS)
        cache_mod.save(principal_id, raw)

    cache_mod.mark_new(raw, previous)
    active_ids = await fetch_active_group_ids(gc)

    items = [
        EligibilityItem(
            groupId=e["groupId"],
            displayName=e["displayName"],
            description=e.get("description") or None,
            accessId=e["accessId"],
            endDateTime=e.get("endDateTime") or None,
            policyMaxDurationHours=int(e.get("policyMaxDurationHours") or 8),
            requiresJustification=bool(e.get("requiresJustification", True)),
            requiresTicket=bool(e.get("requiresTicket", False)),
            requiresMfa=bool(e.get("requiresMfa", False)),
            isNew=bool(e.get("isNew", False)),
        )
        for e in raw
        if e.get("groupId") not in active_ids
    ]
    raw_filtered = [e for e in raw if e.get("groupId") not in active_ids]
    return items, raw_filtered


async def get_approvals(gc: GraphClient) -> list[ApprovalItem]:
    raw = await fetch_pending_approvals(gc)
    return [
        ApprovalItem(
            requestId=r["requestId"],
            approvalId=r["approvalId"],
            groupId=r["groupId"],
            displayName=r["displayName"],
            accessId=r["accessId"],
            requester=r["requester"],
            justification=r.get("justification") or None,
            duration=r.get("duration") or None,
        )
        for r in raw
    ]


async def activate_items(
    gc: GraphClient,
    principal_id: str,
    items: list[ActivatePayload],
    elig_raw: list[dict],
) -> list[ActivateResult]:
    elig_map = {(e["groupId"], e["accessId"]): e for e in elig_raw}

    async def _one(payload: ActivatePayload) -> ActivateResult:
        key = (payload.groupId, payload.accessId)
        elig = elig_map.get(key)
        if elig is None:
            return ActivateResult(
                groupId=payload.groupId,
                accessId=payload.accessId,
                status="NotEligible",
                detail="No matching eligibility found",
            )
        status, detail = await activate(
            gc,
            principal_id,
            elig,
            justification=payload.justification or "",
            hours=payload.durationHours,
            ticket=payload.ticketNumber or None,
        )
        return ActivateResult(
            groupId=payload.groupId,
            accessId=payload.accessId,
            status=status,
            detail=detail,
        )

    sem = asyncio.Semaphore(FETCH_WORKERS)

    async def _throttled(p: ActivatePayload) -> ActivateResult:
        async with sem:
            return await _one(p)

    return list(await asyncio.gather(*[_throttled(p) for p in items]))


async def approve_items(gc: GraphClient, items: list[ApprovePayload]) -> list[ApproveResult]:
    async def _one(payload: ApprovePayload) -> ApproveResult:
        status, detail = await approve(
            gc,
            {"approvalId": payload.approvalId, "requestId": payload.approvalId},
            payload.justification,
        )
        return ApproveResult(approvalId=payload.approvalId, ok=status == "Approved", detail=detail)

    return list(await asyncio.gather(*[_one(p) for p in items]))


async def get_active_assignments(gc: GraphClient) -> list[ActiveGroupItem]:
    raw = await gc.list_pim_group_active_assignments()
    items = []
    for r in raw:
        group = r.get("group") or {}
        sched = r.get("scheduleInfo") or {}
        exp = sched.get("expiration") or {}
        end_dt = exp.get("endDateTime") or None
        items.append(
            ActiveGroupItem(
                groupId=group.get("id") or r.get("groupId") or "",
                displayName=group.get("displayName") or "?",
                description=group.get("description") or None,
                accessId=r.get("accessId") or "member",
                endDateTime=end_dt,
                status=r.get("status") or "Provisioned",
            )
        )
    return items


async def get_me(gc: GraphClient) -> dict:
    return await fetch_me(gc)
