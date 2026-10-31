"""The evaluation gates run in the test suite, so a retrieval or skill-policy regression fails CI."""
import uuid

import pytest
from sqlalchemy import select

from moks.db import session_scope
from moks.evals import gates, runner
from moks.models import EvalRun


@pytest.mark.parametrize("dataset", ["rag", "skills"])
async def test_every_gate_passes_and_results_record_their_versions(dataset):
    rid = await runner.start(dataset, "rules")
    await runner.execute(rid)
    async with session_scope() as db:
        run = await db.get(EvalRun, rid)
    failed = [(g["name"], g["failures"]) for g in run.gates if not g["passed"]]
    assert not failed, failed
    assert run.kind == dataset and {"dataset", "skills_generation", "pipeline", "prompt"} <= set(run.versions)
