from __future__ import annotations

from pydantic import BaseModel


class EligibilityItem(BaseModel):
    groupId: str
    displayName: str
    description: str | None = None
    accessId: str
    endDateTime: str | None = None
    policyMaxDurationHours: int
    requiresJustification: bool
    requiresTicket: bool
    requiresMfa: bool
    isNew: bool = False


class ApprovalItem(BaseModel):
    requestId: str
    approvalId: str
    groupId: str
    displayName: str
    accessId: str
    requester: str
    justification: str | None = None
    duration: str | None = None


class ActivatePayload(BaseModel):
    groupId: str
    accessId: str
    durationHours: int
    justification: str | None = None
    ticketNumber: str | None = None


class ActivateRequest(BaseModel):
    items: list[ActivatePayload]


class ActivateResult(BaseModel):
    groupId: str
    accessId: str
    requestId: str | None = None
    status: str
    detail: str = ""


class ApprovePayload(BaseModel):
    approvalId: str
    steps: list[str] = []
    justification: str


class ApproveRequest(BaseModel):
    items: list[ApprovePayload]


class ApproveResult(BaseModel):
    approvalId: str
    ok: bool
    detail: str = ""


class ActiveGroupItem(BaseModel):
    groupId: str
    displayName: str
    description: str | None = None
    accessId: str
    endDateTime: str | None = None
    status: str


class TokenSetRequest(BaseModel):
    token: str


class TokenStatus(BaseModel):
    valid: bool
    expiry: str | None = None
    upn: str | None = None
