from __future__ import annotations

from pathlib import Path

import pytest

from pim_web import profiles as profiles_mod
from pim_web.models import ProfileItem, ProfileSaveRequest


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profiles_mod, "STORE_DIR", tmp_path)
    monkeypatch.setattr(profiles_mod, "STORE_FILE", tmp_path / "profiles.json")


def _sample_req(name: str = "test") -> ProfileSaveRequest:
    return ProfileSaveRequest(
        name=name,
        items=[
            ProfileItem(groupId="g1", accessId="member", displayName="G1", durationHours=4),
            ProfileItem(groupId="g2", accessId="owner", displayName="G2"),
        ],
    )


def test_upsert_creates_and_lists() -> None:
    assert profiles_mod.list_profiles() == []
    p = profiles_mod.upsert_profile(None, _sample_req("dev"))
    assert p.id and p.name == "dev" and len(p.items) == 2
    listed = profiles_mod.list_profiles()
    assert len(listed) == 1 and listed[0].id == p.id


def test_get_and_update() -> None:
    p = profiles_mod.upsert_profile(None, _sample_req("dev"))
    got = profiles_mod.get_profile(p.id)
    assert got is not None and got.name == "dev"

    updated = profiles_mod.upsert_profile(p.id, _sample_req("dev-renamed"))
    assert updated.id == p.id
    assert profiles_mod.get_profile(p.id).name == "dev-renamed"


def test_update_missing_raises() -> None:
    with pytest.raises(KeyError):
        profiles_mod.upsert_profile("nope", _sample_req())


def test_delete() -> None:
    p = profiles_mod.upsert_profile(None, _sample_req())
    assert profiles_mod.delete_profile(p.id) is True
    assert profiles_mod.delete_profile(p.id) is False
    assert profiles_mod.list_profiles() == []


def test_corrupt_file_returns_empty(tmp_path: Path) -> None:
    profiles_mod.STORE_FILE.write_text("not json")
    assert profiles_mod.list_profiles() == []
