"""Frozen learning datasets (review fix): a dataset keeps the redacted records as exported when it was built, its
checksum covers those contents, and a withdrawn or deleted source invalidates it instead of changing it.
Real PostgreSQL, including the 0035 trigger.
"""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, text

from moks.db import session_scope
from moks.ml import learning
from moks.models import LearningDataset, LearningRecord
from tests.conftest import login, make_env, make_user
from tests.test_db_grants import roles  # noqa: F401  (the fixture; autouse there, used explicitly here)

NOW = datetime.now(timezone.utc)


async def _records(env: str, n: int = 4) -> list[uuid.UUID]:
    ids = []
    async with session_scope() as db:
        for i in range(n):
            r = LearningRecord(schema_version=1, kind="investigation", environment=env, namespace="shop", features={"symptoms": [f"k{i}"]},
                               label={"confirmed_cause": f"cause {i}"}, label_state="approved", fingerprint=uuid.uuid4().hex, content_hash=uuid.uuid4().hex,
                               event_from=NOW + timedelta(hours=i))
            db.add(r)
            await db.flush()
            ids.append(r.id)
    return ids


async def _reviewer(env: str) -> uuid.UUID:
    return await make_user(f"rev-{env}-{uuid.uuid4().hex[:4]}@example.com", ["ml_reviewer"])


async def _build(env: str) -> uuid.UUID:
    async with session_scope() as db:
        return (await learning.build_dataset(db, name=f"snap-{env}", purpose="skill_ranking", created_by=None, environment=env)).id


async def _export(ds_id: uuid.UUID) -> list[dict]:
    async with session_scope() as db:
        return await learning.export_rows(db, await db.get(LearningDataset, ds_id))


async def test_an_edited_label_does_not_change_an_existing_version():
    env = f"sn-{uuid.uuid4().hex[:6]}"
    ids = await _records(env)
    ds_id = await _build(env)
    before = await _export(ds_id)
    reviewer = await _reviewer(env)
    async with session_scope() as db:
        ds = await db.get(LearningDataset, ds_id)
        checksum, version = ds.checksum, ds.version
        assert ds.checksum_version == 2 and learning.provenance(ds)["reproducible"]
        rec = await db.get(LearningRecord, ids[0])
        await learning.review(db, rec, reviewer, approve=True, confirmed_cause="a different cause after review")  # still approved
    after = await _export(ds_id)
    assert after == before and "a different cause" not in json.dumps(after)
    async with session_scope() as db:
        ds = await db.get(LearningDataset, ds_id)
        assert (ds.checksum, ds.state) == (checksum, "built")
    new = await _build(env)  # today's contents are a new version with a new checksum
    async with session_scope() as db:
        nd = await db.get(LearningDataset, new)
        assert nd.version == version + 1 and nd.checksum != checksum
    assert "a different cause" in json.dumps(await _export(new))


async def test_the_checksum_covers_the_exported_contents_and_tampering_is_refused():
    env = f"sn-{uuid.uuid4().hex[:6]}"
    await _records(env)
    ds_id = await _build(env)
    async with session_scope() as db:
        ds = await db.get(LearningDataset, ds_id)
        items = (await db.execute(text("SELECT record_id, split, group_key, payload_sha256, payload FROM learning_dataset_items WHERE dataset_id = :d"), {"d": ds_id})).all()
        assert ds.checksum == learning.contents_checksum(ds, [(str(r), s, g, h) for r, s, g, h, _ in items])
        assert all(h == hashlib.sha256(json.dumps(p, sort_keys=True, default=str).encode()).hexdigest() for *_, h, p in items)
        # the same ids and splits with different contents would have the same old (v1) checksum but not this one
        legacy = hashlib.sha256(json.dumps(sorted([str(r), s] for r, s, *_ in items), sort_keys=True, default=str).encode()).hexdigest()
        assert legacy != ds.checksum
        await db.execute(text("""UPDATE learning_dataset_items SET payload = jsonb_set(payload, '{label,confirmed_cause}', '"edited in place"')
                                 WHERE dataset_id = :d AND record_id = :r"""), {"d": ds_id, "r": items[0][0]})
    with pytest.raises(learning.DatasetUnavailable) as e:
        await _export(ds_id)
    assert e.value.status == 409


@pytest.mark.parametrize("how", ["withdraw", "delete"])
async def test_a_withdrawn_or_deleted_source_invalidates_the_dataset(how):
    env = f"sn-{uuid.uuid4().hex[:6]}"
    ids = await _records(env)
    ds_id = await _build(env)
    reviewer = await _reviewer(env)
    async with session_scope() as db:
        if how == "withdraw":
            await learning.review(db, await db.get(LearningRecord, ids[1]), reviewer, approve=False, note="wrong cause")
        else:  # retention or a deleted investigation; the item rows stay, as references
            await db.execute(delete(LearningRecord).where(LearningRecord.id == ids[1]))
    async with session_scope() as db:
        ds = await db.get(LearningDataset, ds_id)
        assert ds.state == "invalidated" and str(ids[1]) in ds.invalidated_reason
        prov = learning.provenance(ds)
        assert prov == {**prov, "reproducible": False, "provenance": "invalidated"}
        left = (await db.execute(text("SELECT count(*), count(payload) FROM learning_dataset_items WHERE dataset_id = :d"), {"d": ds_id})).one()
        assert left == (4, 0)  # membership kept for the record, contents cleared
    with pytest.raises(learning.DatasetUnavailable) as e:
        await _export(ds_id)
    assert e.value.status == 410


async def test_a_legacy_dataset_reports_its_provenance_honestly(client):
    env = f"sn-{uuid.uuid4().hex[:6]}"
    await make_env(env)
    email = f"{env}@example.com"
    await make_user(email, ["ml_admin"])
    await _records(env)
    ds_id = await _build(env)
    async with session_scope() as db:  # what a dataset built before migration 0035 looks like
        await db.execute(text("UPDATE learning_datasets SET checksum_version = 1 WHERE id = :d"), {"d": ds_id})
        await db.execute(text("UPDATE learning_dataset_items SET payload = NULL, payload_sha256 = NULL WHERE dataset_id = :d"), {"d": ds_id})
    h = await login(client, email)
    listed = next(d for d in (await client.get("/moks/api/ml/learning/datasets", headers=h)).json() if d["id"] == str(ds_id))
    assert listed["reproducible"] is False and listed["provenance"] == "legacy_live_records" and "today's records" in listed["reason"]
    r = await client.get(f"/moks/api/ml/learning/datasets/{ds_id}/export", headers=h)
    assert r.status_code == 200 and r.headers["X-MOKS-Dataset-Provenance"] == "legacy_live_records"
    assert all(json.loads(line)["provenance"] == "live" for line in r.text.strip().splitlines())
    async with session_scope() as db:
        await db.execute(text("UPDATE learning_datasets SET state = 'invalidated', invalidated_reason = 'x' WHERE id = :d"), {"d": ds_id})
    assert (await client.get(f"/moks/api/ml/learning/datasets/{ds_id}/export", headers=h)).status_code == 410


async def test_dataset_retention_deletes_the_snapshot_with_the_dataset():
    env = f"sn-{uuid.uuid4().hex[:6]}"
    await _records(env)
    ds_id = await _build(env)
    async with session_scope() as db:
        await db.execute(text("UPDATE learning_datasets SET expires_at = now() - interval '1 day' WHERE id = :d"), {"d": ds_id})
    async with session_scope() as db:
        assert (await learning.purge(db))["datasets"] >= 1
        assert (await db.execute(select(LearningDataset).where(LearningDataset.id == ds_id))).scalar_one_or_none() is None
        assert (await db.execute(text("SELECT count(*) FROM learning_dataset_items WHERE dataset_id = :d"), {"d": ds_id})).scalar_one() == 0


async def test_retention_and_withdrawal_work_under_the_component_roles(roles):  # noqa: F811
    """The worker purges and the API reviews, each under its own grants; both can fire the invalidation trigger."""
    from tests.test_db_grants import role

    env = f"sn-{uuid.uuid4().hex[:6]}"
    ids = await _records(env)
    ds_id = await _build(env)
    async with session_scope() as db:
        await db.execute(text("UPDATE learning_records SET expires_at = now() - interval '1 day' WHERE id = :r"), {"r": ids[0]})
    token = role.set("moks_worker")
    try:
        async with session_scope() as db:
            assert (await learning.purge(db))["records"] >= 1
    finally:
        role.reset(token)
    async with session_scope() as db:
        assert (await db.get(LearningDataset, ds_id)).state == "invalidated"
    ds2 = await _build(env)
    reviewer = await _reviewer(env)
    token = role.set("moks_api")
    try:
        async with session_scope() as db:
            await learning.review(db, await db.get(LearningRecord, ids[1]), reviewer, approve=False)
    finally:
        role.reset(token)
    async with session_scope() as db:
        assert (await db.get(LearningDataset, ds2)).state == "invalidated"
        await db.execute(text("UPDATE learning_datasets SET expires_at = now() - interval '1 day' WHERE id = :d"), {"d": ds2})
    token = role.set("moks_worker")
    try:
        async with session_scope() as db:
            assert (await learning.purge(db))["datasets"] >= 1  # dataset retention is the worker's job too
    finally:
        role.reset(token)
