"""Local profile store — JSON file in user home dir."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from .models import Profile, ProfileSaveRequest

STORE_DIR = Path.home() / ".pim-web"
STORE_FILE = STORE_DIR / "profiles.json"


def _load_all() -> list[dict]:
    if not STORE_FILE.exists():
        return []
    try:
        data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_all(data: list[dict]) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    STORE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_profiles() -> list[Profile]:
    return [Profile.model_validate(p) for p in _load_all()]


def get_profile(pid: str) -> Profile | None:
    for p in _load_all():
        if p.get("id") == pid:
            return Profile.model_validate(p)
    return None


def upsert_profile(pid: str | None, req: ProfileSaveRequest) -> Profile:
    data = _load_all()
    payload_items = [i.model_dump() for i in req.items]
    if pid is None:
        pid = uuid.uuid4().hex[:12]
        data.append({"id": pid, "name": req.name, "items": payload_items})
    else:
        found = False
        for p in data:
            if p.get("id") == pid:
                p["name"] = req.name
                p["items"] = payload_items
                found = True
                break
        if not found:
            raise KeyError(pid)
    _save_all(data)
    return Profile(id=pid, name=req.name, items=req.items)


def delete_profile(pid: str) -> bool:
    data = _load_all()
    new = [p for p in data if p.get("id") != pid]
    if len(new) == len(data):
        return False
    _save_all(new)
    return True
