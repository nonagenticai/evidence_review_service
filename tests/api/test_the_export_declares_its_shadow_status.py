"""`manifest.shadow_mode` must state what this deployment IS, not a literal.

`flow.py:687` read `"shadow_mode": True`, hardcoded, with no Settings field, no
env var and no review state anywhere behind it — `grep -rn "shadow" src/` returned
that one line.

## Why a wrong constant here is not a cosmetic defect

`shadow_mode` is a **required** member of the sealed manifest in both
`urn:datacompany:inclusion-set-export:1` and `:2`, and its own `$comment` in
`research_contracts/schemas/evidence/` says why it lives there rather than beside
the row:

    Whether this export was produced while the deriving stage was running in
    shadow. It is a property OF the export and belongs in the sealed manifest: an
    export that silently changed its shadow status after the fact would otherwise
    reproduce the same hash.

So the value is inside `manifest_hash`, and `:2` folds the items in through
`items_hash` as well. It cannot be corrected afterwards — `inclusion_exports` is
append-only and the only honest repair is to re-take the export. The first real
reviewer-produced inclusion set would have returned `201`, self-verified as
`"verified": true`, and permanently declared itself a rehearsal artifact; a
consumer branching on `shadow_mode` — the exact reason the member exists —
discards every real export it is ever sent.

## Why BOTH directions are asserted, and why one test would have proved nothing

A single test asserting `shadow_mode is False` under default settings passes
against a source line that hardcodes `False`, which is the same defect with the
opposite constant and the same absence of a fact behind it. The pair is the
assertion: the manifest says `false` when nothing is configured, and `true` when
the deployment declares itself a rehearsal — so the member is reading
configuration rather than carrying a literal.

Measured on `4ea77a0`, before the fix:

    test_a_default_deployment_seals_shadow_mode_false  FAILED
        AssertionError: the manifest sealed shadow_mode=True on a deployment
        that declared nothing
    test_a_rehearsal_deployment_seals_shadow_mode_true PASSED

The second passing on the broken code is the point, and it is recorded here as
the weaker of the two rather than counted as a second gate: a hardcoded `True`
satisfies it by accident. It becomes load-bearing the moment the first one is
made to pass.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")
SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-shadow-status")
LEAD = "shadow-lead"
REVIEWER = "shadow-reviewer"

#: The environment variable a rehearsal deployment sets. Named here rather than
#: imported so the test pins the SPELLING too: a rename in `config.py` that left
#: the manifest in gitops untouched would otherwise stay green here and seal
#: `false` on the rehearsal cluster.
SHADOW_ENV = "SHADOW_MODE"


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


@contextmanager
def env(**values: str | None) -> Iterator[None]:
    """Set process env for the block, and drop the `get_settings` cache both ways.

    `get_settings` is `lru_cache(maxsize=1)`, so a value set after the first call
    is invisible and a value left cached after the block leaks into every later
    test in the session. Clearing on both edges is what keeps this file from
    turning the rest of the suite into a shadow deployment.
    """
    from config import get_settings

    previous = {name: os.environ.get(name) for name in values}
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    get_settings.cache_clear()
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()


@pytest.fixture(scope="module")
def client():  # noqa: ANN201
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def screened(client) -> dict[str, str]:  # noqa: ANN001
    """A review screened to completion — one `POST /exports` away.

    Exports are append-only and NOT unique per review, so this fixture is taken
    once and both tests seal their own export from it. That is deliberate: the
    two exports differ in exactly one input, the environment, which is what makes
    the pair an experiment rather than two unrelated assertions.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "shadow", "external_key": f"shadow-{uuid.uuid4()}", "title": "Shadow status"},
        headers=headers(),
    ).json()["review_id"]

    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 1,
        },
        headers=headers(),
    ).json()
    client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock", headers=headers())
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(),
    )
    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": f"Shadow paper {n}", "year": 2020 + n, "doi": f"10.1000/sh.{uuid.uuid4()}"}
                    for n in range(3)
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code in (200, 201), imported.text
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())

    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [REVIEWER]},
        headers=headers(),
    )
    queue = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id, "limit": 200},
        headers=headers(REVIEWER, "reviewer"),
    ).json()
    assert len(queue["items"]) == 3, queue
    for item in queue["items"]:
        decided = client.post(
            f"/api/reviews/{review_id}/decisions",
            json={"stage_id": stage_id, "review_record_id": item["review_record_id"], "disposition": "include"},
            headers=headers(REVIEWER, "reviewer"),
        )
        assert decided.status_code == 201, decided.text
    return {"review_id": review_id, "stage_id": stage_id}


def _sealed_manifest(client, review_id: str) -> dict:  # noqa: ANN001
    """Take an export and return the manifest AS STORED, not as returned by the seal.

    Read back through `GET /exports/{id}` rather than off the `POST` response,
    because the stored payload is the artifact a consumer receives and the thing
    `manifest_hash` covers. A value that were correct in the response and wrong in
    the row would be the whole defect with an extra step.
    """
    created = client.post(f"/api/reviews/{review_id}/exports", headers=headers())
    assert created.status_code == 201, created.text
    export_id = created.json()["export_id"]

    fetched = client.get(f"/api/reviews/{review_id}/exports/{export_id}", headers=headers())
    assert fetched.status_code == 200, fetched.text
    return fetched.json()["payload"]["manifest"]


def test_a_default_deployment_seals_shadow_mode_false(client, screened) -> None:
    """THE GATE. Nothing configured means this is a real run, and it says so.

    `SHADOW_MODE` is explicitly REMOVED rather than merely left alone: the
    process could be carrying a value from a sibling test or a developer's shell,
    and "the default" has to mean the absence of the variable, not whatever the
    environment happens to hold.
    """
    with env(**{SHADOW_ENV: None}):
        manifest = _sealed_manifest(client, screened["review_id"])

    assert "shadow_mode" in manifest, (
        "shadow_mode is a REQUIRED member of both inclusion-set-export schemas; a manifest without "
        "it cannot be sealed against the declared contract at all"
    )
    assert manifest["shadow_mode"] is False, (
        f"the manifest sealed shadow_mode={manifest['shadow_mode']!r} on a deployment that declared "
        "nothing. The value is inside manifest_hash and items_hash and the row is append-only, so "
        "every real inclusion set this service produces is permanently a rehearsal artifact."
    )


def test_a_rehearsal_deployment_seals_shadow_mode_true(client, screened) -> None:
    """The other direction. Without this, `False` would be one more hardcoded literal.

    PASSED on the unfixed source, where the literal was `True`. It is kept
    because it is what stops the fix being "change the constant": the pair can
    only both pass if the member reads configuration.
    """
    with env(**{SHADOW_ENV: "true"}):
        manifest = _sealed_manifest(client, screened["review_id"])

    assert manifest["shadow_mode"] is True, (
        f"a deployment that set {SHADOW_ENV}=true sealed shadow_mode={manifest['shadow_mode']!r}; a "
        "rehearsal that presents itself as a real run is the same defect pointed the other way"
    )


def test_an_unparseable_shadow_mode_refuses_rather_than_defaulting(client) -> None:
    """A value nobody can read must not be silently answered `false`.

    `_get_bool` returns the DEFAULT for anything it does not recognise, and the
    gitops manifest reasons about that explicitly for `ENABLE_AUTH`: "a typo here
    degrades to today's behaviour rather than to open". That argument does not
    transfer. `ENABLE_AUTH` defaults to the safe answer; `shadow_mode` defaults to
    `False`, and `False` is the UNSAFE answer for the only deployment that ever
    sets the variable. A rehearsal cluster with `SHADOW_MODE: "shadow"` in its
    manifest would seal `false` into a hash and there is no later signal — the
    export verifies, the row is append-only, and the artifact is indistinguishable
    from a real one.

    So the tolerant parse is not reused for this field. Asserted on `Settings`
    rather than through an export because the refusal has to happen before any
    request is served.
    """
    from config import Settings

    with env(**{SHADOW_ENV: "shadow"}):
        with pytest.raises(ValueError) as raised:
            Settings()

    message = str(raised.value)
    assert SHADOW_ENV in message, f"the refusal must name the variable; got {message!r}"
    assert "shadow" in message, f"the refusal must quote the value it could not read; got {message!r}"
