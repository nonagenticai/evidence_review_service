"""Naming the adjudicator must not name a reviewer.

`_adjudication_summary` and `blinded_reads.to_public` look alike — both
serialize a screening decision and both carry a subject id — and they are
guarded differently on purpose.

* The adjudication payload is spliced in behind
  `adjudication and not owes_this_conflict else REDACTED`, on a route that is
  already lead-only.
* Reviewer attribution in `to_public` is guarded by `_may_attribute`, which
  tests the ROLE and nothing else.

So a display name added to the first inherits a real guard, and the same field
added to the second would hand every lead the NAMES of the two reviewers whose
blinding the whole subsystem exists to protect. The distinction is one line of
code apart and is worth a test that fails if someone closes the gap the wrong
way.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from application.services.blinded_reads import DecisionView
from application.services.screening import _adjudication_summary
from models.screening import ScreeningAdjudication


def _adjudication(**over: object) -> ScreeningAdjudication:
    fields: dict[str, object] = {
        "adjudication_id": uuid.uuid4(),
        "conflict_id": uuid.uuid4(),
        "review_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "disposition": "include",
        "exclusion_reason_code": None,
        "rationale": "Both reviewers read the population differently; the registration settles it.",
        "adjudicator_subject_id": "lead-carol",
        "adjudicator_display_name": None,
        "supersedes_adjudication_id": None,
        "decided_at": datetime.now(UTC),
    }
    fields.update(over)
    return ScreeningAdjudication(**fields)


def test_the_summary_carries_the_name_when_there_is_one() -> None:
    summary = _adjudication_summary(_adjudication(adjudicator_display_name="Carol Lead"))

    assert summary["adjudicator_display_name"] == "Carol Lead"
    # The id is not replaced by the label. It remains the attribution that
    # anything evidentiary hangs off.
    assert summary["adjudicator_subject_id"] == "lead-carol"


def test_the_key_is_present_even_when_the_name_is_unknown() -> None:
    """Always present, `None` when unknown — not omitted.

    Every row written before this column existed has `NULL` here permanently:
    `screening_adjudications` is append-only, so no backfill is possible. A key
    that vanished for those rows would make a caller branch on absence, and the
    only thing absence could ever mean is "this row is old", which is not a fact
    a caller should have to infer from a missing key.
    """
    summary = _adjudication_summary(_adjudication(adjudicator_display_name=None))

    assert "adjudicator_display_name" in summary
    assert summary["adjudicator_display_name"] is None
    assert summary["adjudicator_subject_id"] == "lead-carol", "the fallback target survives"


def test_a_reviewer_decision_is_never_given_a_display_name() -> None:
    """The blinding guard, stated as a test rather than as a comment.

    `to_public` is reached by a reviewer's own reads. Attribution here is
    role-gated only, so a name on this payload is a blinding widening — the two
    reviewers would become identifiable to each other by name, which is the one
    thing `ER-004.2` exists to prevent.
    """
    view = DecisionView(
        decision_id=uuid.uuid4(),
        review_record_id=uuid.uuid4(),
        review_record_version_id=uuid.uuid4(),
        stage_id=uuid.uuid4(),
        round_number=1,
        disposition="exclude",
        exclusion_reason_code="wrong_population",
        rationale="Mixed cardiology cohort.",
        decided_at=datetime.now(UTC),
        supersedes_decision_id=None,
        reviewer_subject_id="reviewer-b",
    )

    body = view.to_public()

    assert not [k for k in body if "display_name" in k], (
        "no display name may appear on the reviewer-facing decision payload"
    )
    # The control: this payload does still attribute by ID when the caller is
    # entitled to it, so the assertion above is about the NAME and is not
    # passing merely because the payload is empty.
    assert body["reviewer_subject_id"] == "reviewer-b"
