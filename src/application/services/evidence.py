"""Accepted evidence — `ER-006`, migrations `ER-M017` and `ER-M018`.

What a review ACCEPTS, and who said so. `screening.py` decides which reports are
in; this module records what those reports say and who stands behind it.

## The authority split, and why it is not "lead-only"

Recording an observation is a reviewer's work. **Accepting** it is the authority
act, so `decide_acceptance` is the only function here behind
`require_review_lead_for`. That mirrors screening exactly — reviewers decide,
the lead adjudicates and seals — and it mirrors the programme's own correction:
*"KARMA produces proposals. Human or deterministic domain-policy actions in
owning services create accepted records."*

Making creation lead-only would have been the safe-looking choice and the wrong
one. `require_review_lead`'s docstring records what that costs: a person who can
open a review and configure it but cannot then do the work in it has a product
that does not function, and the pressure that follows goes somewhere worse than
a permission check.

## The inclusion rule lives here, not in a trigger

A research object may only be built from a report the review INCLUDED. That is a
derived fact — `flow_derivation.derive` computes it from the immutable decision
history — so it is checked against `flow.included_record_ids`, the one
authority, rather than re-implemented in PL/pgSQL where it would drift silently.
`0017`'s docstring states the same split from the database's side.

## No events

`ER-006` allocates five domain events. **None are emitted, because there is
nowhere to emit them.** Measured on the production database 2026-09-02:
`evidence_review` has 26 tables and `domain_outbox`, `durable_jobs` and
`job_attempts` are **not among them** — the `pyproject.toml` header's claim that
"the jobs/outbox tables exist" is stale and is corrected in the same change as
this file. Writing an event means first creating the outbox (`FND-003`), which
is a different package. An event written to a table that does not exist is not a
smaller version of eventing; it is a crash.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from actor import Actor, require_human
from api.problem_details import NotFoundError, UnprocessableStateError
from application.services import events
from application.services import flow as flow_svc
from application.services.reviews import active_protocol, load_review, require_review_lead_for
from models.evidence import (
    ACCEPTANCE_DECISIONS,
    ACCEPTANCE_SUBJECTS,
    BASIS_KINDS,
    EvidenceAcceptance,
    ExtractionDataset,
    ExtractionDatasetVersion,
    Observation,
    ObservationVersion,
    ResearchAssertion,
    ResearchAssertionSupport,
    ResearchAssertionVersion,
    ResearchObject,
    ResearchObjectRecord,
    ResearchObjectVersion,
)

#: The schema every version in this package declares itself against.
#:
#: `ER-006`'s aggregates have no released schema in `research_contracts` yet —
#: the repo ships `stable-identity:1`, `immutable-version:1` and
#: `bitemporal-assertion:1`, which are the ENVELOPE primitives, not a body for an
#: observation. So this names the package, not a document, and it is deliberately
#: NOT a URL: `8810` already shipped a `schema_ref` pointing at a document that
#: did not exist, and the cost of that was a whole slice of `8811` blocked on a
#: dangling `$ref`. A ref that resolves is a promise; this one does not pretend to.
EVIDENCE_SCHEMA_REF = "urn:datacompany:evidence-review-accepted-evidence:unreleased"


def _hash(payload: dict[str, Any]) -> str:
    """RFC 8785 through the published contract wheel — `FND-005`, never re-solved.

    The same function `deduplication_engine.input_digest` uses, for the reason it
    gives: *"A `json.dumps` here would be a second, quietly different definition."*
    """
    from research_contracts.canonical.engine import canonical_sha256

    return canonical_sha256(payload)


def _locked_protocol_id(session: Session, review_id: uuid.UUID) -> uuid.UUID:
    """The protocol every version in this package pins — `16-…:§5.3`.

    `0017`'s `er_pins_locked_protocol` refuses an unlocked one at the database, so
    this is the readable refusal rather than the guard. Both, deliberately: the
    trigger is what holds when a future caller forgets this function.
    """
    protocol = active_protocol(session, review_id)
    if protocol is None or protocol.locked_at is None:
        raise UnprocessableStateError(
            "this review has no locked protocol version; evidence cannot be judged "
            "against a protocol that can still change"
        )
    return protocol.protocol_version_id


# --------------------------------------------------------------------------
# ER-006.1 — research objects
# --------------------------------------------------------------------------


def create_research_object(
    session: Session,
    actor: Actor,
    review_id: uuid.UUID,
    *,
    object_key: str,
    content: dict[str, Any],
    rationale: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """One study, and the reports of it — `ER-006.1`.

    `records` is not optional and not allowed to be empty. A research object with
    no report behind it is a study nobody screened, and the whole point of this
    aggregate is that "studies included" can finally be counted separately from
    "reports included".
    """
    subject = require_human(actor)
    load_review(session, review_id)
    protocol_version_id = _locked_protocol_id(session, review_id)

    if not records:
        raise UnprocessableStateError(
            "a research object must name at least one review record; a study with no report behind it is not evidence"
        )
    if sum(1 for r in records if r.get("link_role") == "primary") != 1:
        raise UnprocessableStateError(
            "exactly one linked record must be the 'primary' report: with none the study "
            "cannot be cited, with two there is no single citation"
        )

    included = flow_svc.included_record_ids(session, review_id)
    for record in records:
        record_id = uuid.UUID(str(record["review_record_id"]))
        if record_id not in included:
            raise UnprocessableStateError(
                f"review record {record_id} is not included by this review's flow ledger; "
                "evidence may only be extracted from reports the review included"
            )

    obj = ResearchObject(
        review_id=review_id,
        tenant_id=actor.tenant_id,
        object_key=object_key,
        created_by_subject_id=subject,
    )
    session.add(obj)
    session.flush()

    version = _write_object_version(
        session,
        actor,
        obj,
        content=content,
        rationale=rationale,
        revision_kind="initial",
        version_number=1,
        supersedes_version_id=None,
        protocol_version_id=protocol_version_id,
    )

    for record in records:
        session.add(
            ResearchObjectRecord(
                research_object_id=obj.research_object_id,
                review_id=review_id,
                tenant_id=actor.tenant_id,
                review_record_id=uuid.UUID(str(record["review_record_id"])),
                review_record_version_id=uuid.UUID(str(record["review_record_version_id"])),
                corpus_version_id=uuid.UUID(str(record["corpus_version_id"])),
                link_role=record["link_role"],
                rationale=record.get("rationale") or rationale,
                linked_by_subject_id=subject,
                linked_by_display_name=actor.display_name,
            )
        )
    session.flush()
    return research_object_summary(session, obj, version)


def _write_object_version(
    session: Session,
    actor: Actor,
    obj: ResearchObject,
    *,
    content: dict[str, Any],
    rationale: str,
    revision_kind: str,
    version_number: int,
    supersedes_version_id: uuid.UUID | None,
    protocol_version_id: uuid.UUID,
) -> ResearchObjectVersion:
    version = ResearchObjectVersion(
        research_object_id=obj.research_object_id,
        review_id=obj.review_id,
        tenant_id=obj.tenant_id,
        version_number=version_number,
        supersedes_version_id=supersedes_version_id,
        protocol_version_id=protocol_version_id,
        schema_ref=EVIDENCE_SCHEMA_REF,
        # The hash covers the CONTENT and the identity it belongs to, and
        # nothing recorded after the fact. `16-…:§14.6.7`: no later acceptance,
        # appraisal or association may be hashed back into this version.
        content_hash=_hash(
            {
                "research_object_id": str(obj.research_object_id),
                "version_number": version_number,
                "content": content,
            }
        ),
        content=content,
        revision_kind=revision_kind,
        rationale=rationale,
        created_by_subject_id=require_human(actor),
        created_by_display_name=actor.display_name,
    )
    session.add(version)
    session.flush()
    return version


def revise_research_object(
    session: Session,
    actor: Actor,
    review_id: uuid.UUID,
    research_object_id: uuid.UUID,
    *,
    content: dict[str, Any],
    rationale: str,
    revision_kind: str,
) -> dict[str, Any]:
    """A new version superseding the current one — never an edit.

    `revision_kind` distinguishes a `correction` from a `material_change`
    (`DM-AT-04`). Both produce a new version; only the reader can tell them
    apart, and only if the writer was asked to say which.
    """
    load_review(session, review_id)
    obj = session.get(ResearchObject, research_object_id)
    if obj is None or obj.review_id != review_id:
        raise NotFoundError("no such research object in this review")
    if revision_kind == "initial":
        raise UnprocessableStateError("only the first version may be 'initial'")

    head = _version_head(session, research_object_id)
    version = _write_object_version(
        session,
        actor,
        obj,
        content=content,
        rationale=rationale,
        revision_kind=revision_kind,
        version_number=head.version_number + 1,
        supersedes_version_id=head.research_object_version_id,
        protocol_version_id=_locked_protocol_id(session, review_id),
    )
    return research_object_summary(session, obj, version)


def _version_head(session: Session, research_object_id: uuid.UUID) -> ResearchObjectVersion:
    """The tail of the supersession chain — the current content.

    Ordered by `version_number`, which `uq_research_object_version_number` makes
    unique per object and `ck_research_object_version_chain` makes contiguous
    from 1. There is deliberately no `is_current` column: `0005` records why one
    would be a second answer to the same question.
    """
    head = session.execute(
        select(ResearchObjectVersion)
        .where(ResearchObjectVersion.research_object_id == research_object_id)
        .order_by(ResearchObjectVersion.version_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if head is None:
        raise NotFoundError("research object has no versions")
    return head


def research_object_summary(session: Session, obj: ResearchObject, version: ResearchObjectVersion) -> dict[str, Any]:
    links = session.execute(
        select(ResearchObjectRecord).where(ResearchObjectRecord.research_object_id == obj.research_object_id)
    ).scalars()
    return {
        "research_object_id": str(obj.research_object_id),
        "review_id": str(obj.review_id),
        "object_key": obj.object_key,
        "current_version": {
            "research_object_version_id": str(version.research_object_version_id),
            "version_number": version.version_number,
            "revision_kind": version.revision_kind,
            "content_hash": version.content_hash,
            "content": version.content,
            "rationale": version.rationale,
            "protocol_version_id": str(version.protocol_version_id),
            "created_by": version.created_by_display_name or version.created_by_subject_id,
        },
        "records": [
            {
                "review_record_id": str(link.review_record_id),
                "review_record_version_id": str(link.review_record_version_id),
                "corpus_version_id": str(link.corpus_version_id),
                "link_role": link.link_role,
            }
            for link in links
        ],
    }


def list_research_objects(session: Session, review_id: uuid.UUID) -> dict[str, Any]:
    objects = session.execute(
        select(ResearchObject).where(ResearchObject.review_id == review_id).order_by(ResearchObject.object_key)
    ).scalars()
    return {
        "items": [
            research_object_summary(session, obj, _version_head(session, obj.research_object_id)) for obj in objects
        ]
    }


# --------------------------------------------------------------------------
# ER-006.2 / ER-006.4 — manual extraction and observations
# --------------------------------------------------------------------------


def record_extraction(
    session: Session,
    actor: Actor,
    review_id: uuid.UUID,
    research_object_id: uuid.UUID,
    *,
    dataset_key: str,
    rationale: str,
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    """One dataset version and the observations in it — `ER-006.2`, `ER-006.4`.

    The MANUAL writer. `ER-006.3`'s proposal path (KARMA proposes, a human
    accepts or revises) is not built: `INT-KARMA`'s immutable `ProposalBundle`
    contract does not exist, and a `proposal_decisions` table with no proposal
    producer is a table with no writer. `extraction_method` is a closed
    vocabulary of exactly `('manual',)` for the same reason — a vocabulary entry
    no code can write is a lie about what the system can do.

    Every observation must carry an expert-assertion basis (`16-…:§3.7` arm 2).
    Arm 1 — an exact `EvidenceLocation` — is refused here AND by
    `ck_observation_located_arm_unverifiable`, because `EvidenceLocation` is
    owned by Ingestion/Documents (`§7.2`) and `8810` has no client that can
    verify a reference into it. `DM-AT-25` says an unverifiable pinned ref fails
    closed; storing it anyway and calling it evidence is the failure this
    service exists to prevent.
    """
    subject = require_human(actor)
    load_review(session, review_id)
    protocol_version_id = _locked_protocol_id(session, review_id)

    obj = session.get(ResearchObject, research_object_id)
    if obj is None or obj.review_id != review_id:
        raise NotFoundError("no such research object in this review")
    if not observations:
        raise UnprocessableStateError("an extraction with no observations records nothing")

    for observed in observations:
        basis = observed.get("basis_kind")
        if basis not in BASIS_KINDS:
            raise UnprocessableStateError(f"basis_kind must be one of {BASIS_KINDS}")
        if basis == "evidence_located":
            raise UnprocessableStateError(
                "evidence_location_unverifiable: an exact EvidenceLocation is owned by "
                "Ingestion/Documents and 8810 cannot yet verify a reference into it, so a "
                "located claim would be unverifiable evidence. Record the value as an "
                "expert assertion, with who is asserting it and why"
            )

    dataset = session.execute(
        select(ExtractionDataset).where(
            ExtractionDataset.research_object_id == research_object_id,
            ExtractionDataset.dataset_key == dataset_key,
        )
    ).scalar_one_or_none()
    if dataset is None:
        dataset = ExtractionDataset(
            review_id=review_id,
            tenant_id=actor.tenant_id,
            research_object_id=research_object_id,
            dataset_key=dataset_key,
            created_by_subject_id=subject,
        )
        session.add(dataset)
        session.flush()
        previous_version_id, next_number = None, 1
    else:
        head = session.execute(
            select(ExtractionDatasetVersion)
            .where(ExtractionDatasetVersion.extraction_dataset_id == dataset.extraction_dataset_id)
            .order_by(ExtractionDatasetVersion.version_number.desc())
            .limit(1)
        ).scalar_one()
        previous_version_id, next_number = head.extraction_dataset_version_id, head.version_number + 1

    dataset_version = ExtractionDatasetVersion(
        extraction_dataset_id=dataset.extraction_dataset_id,
        review_id=review_id,
        tenant_id=actor.tenant_id,
        version_number=next_number,
        supersedes_version_id=previous_version_id,
        protocol_version_id=protocol_version_id,
        schema_ref=EVIDENCE_SCHEMA_REF,
        content_hash=_hash(
            {
                "extraction_dataset_id": str(dataset.extraction_dataset_id),
                "version_number": next_number,
                "observations": [
                    {"variable_key": o["variable_key"], "value": o["value"], "unit": o.get("unit")}
                    for o in observations
                ],
            }
        ),
        extraction_method="manual",
        rationale=rationale,
        created_by_subject_id=subject,
        created_by_display_name=actor.display_name,
    )
    session.add(dataset_version)
    session.flush()

    written = [
        _write_observation(session, actor, dataset, dataset_version, observed, protocol_version_id)
        for observed in observations
    ]
    session.flush()

    return {
        "extraction_dataset_id": str(dataset.extraction_dataset_id),
        "dataset_key": dataset.dataset_key,
        "research_object_id": str(research_object_id),
        "current_version": {
            "extraction_dataset_version_id": str(dataset_version.extraction_dataset_version_id),
            "version_number": dataset_version.version_number,
            "content_hash": dataset_version.content_hash,
            "extraction_method": dataset_version.extraction_method,
        },
        "observations": written,
    }


def _write_observation(
    session: Session,
    actor: Actor,
    dataset: ExtractionDataset,
    dataset_version: ExtractionDatasetVersion,
    observed: dict[str, Any],
    protocol_version_id: uuid.UUID,
) -> dict[str, Any]:
    """One variable's value, versioned, with its basis.

    An observation whose `variable_key` already exists in this dataset is a
    CORRECTION: a new version superseding the current one. It is not a second
    observation of the same variable, and `uq_observation_variable` makes that
    structural rather than a convention this function has to remember.
    """
    variable_key = observed["variable_key"]
    observation = session.execute(
        select(Observation).where(
            Observation.extraction_dataset_id == dataset.extraction_dataset_id,
            Observation.variable_key == variable_key,
        )
    ).scalar_one_or_none()

    if observation is None:
        observation = Observation(
            review_id=dataset.review_id,
            tenant_id=dataset.tenant_id,
            extraction_dataset_id=dataset.extraction_dataset_id,
            variable_key=variable_key,
            created_by_subject_id=require_human(actor),
        )
        session.add(observation)
        session.flush()
        supersedes, number = None, 1
    else:
        head = session.execute(
            select(ObservationVersion)
            .where(ObservationVersion.observation_id == observation.observation_id)
            .order_by(ObservationVersion.version_number.desc())
            .limit(1)
        ).scalar_one()
        supersedes, number = head.observation_version_id, head.version_number + 1

    version = ObservationVersion(
        observation_id=observation.observation_id,
        review_id=dataset.review_id,
        tenant_id=dataset.tenant_id,
        version_number=number,
        supersedes_version_id=supersedes,
        protocol_version_id=protocol_version_id,
        extraction_dataset_version_id=dataset_version.extraction_dataset_version_id,
        schema_ref=EVIDENCE_SCHEMA_REF,
        content_hash=_hash(
            {
                "observation_id": str(observation.observation_id),
                "version_number": number,
                "variable_key": variable_key,
                "value": observed["value"],
                "unit": observed.get("unit"),
            }
        ),
        value=observed["value"],
        unit=observed.get("unit"),
        basis_kind="expert_assertion",
        expert_subject_id=observed.get("expert_subject_id") or require_human(actor),
        expert_role=observed.get("expert_role") or actor.role.value,
        expert_rationale=observed["expert_rationale"],
        # `§3.7` arm 2 requires a DECISION reference, not only a rationale. The
        # dataset version is that decision: it is the immutable, attributable act
        # in which this value was recorded.
        expert_decision_ref=dataset_version.extraction_dataset_version_id,
        created_by_subject_id=require_human(actor),
        created_by_display_name=actor.display_name,
    )
    session.add(version)
    session.flush()
    if supersedes is not None:
        # A first write raises nothing: `09-…:§3.7` names a SUPERSEDED event and
        # an ACCEPTED one, and no "created". An observation becomes a fact a
        # consumer may use when it is accepted, which is where that is said.
        events.emit_observation_superseded(session, observation, version)
    return {
        "observation_id": str(observation.observation_id),
        "observation_version_id": str(version.observation_version_id),
        "variable_key": variable_key,
        "version_number": number,
        "value": version.value,
        "unit": version.unit,
        "content_hash": version.content_hash,
        "basis_kind": version.basis_kind,
    }


# --------------------------------------------------------------------------
# ER-006.5 — research assertions
# --------------------------------------------------------------------------


def record_assertion(
    session: Session,
    actor: Actor,
    review_id: uuid.UUID,
    *,
    assertion_key: str,
    statement: str,
    content: dict[str, Any],
    support: list[dict[str, Any]],
) -> dict[str, Any]:
    """A reviewed claim, and the observations for AND against it — `ER-006.5`.

    `support` carries a `stance`, and `contradicts` is a first-class value rather
    than an omission. A claim that can only cite its confirming evidence is the
    specific dishonesty a systematic review exists to prevent, so the schema
    makes recording the disconfirming observation possible and the API makes it
    cheap.
    """
    subject = require_human(actor)
    load_review(session, review_id)
    protocol_version_id = _locked_protocol_id(session, review_id)

    if not support:
        raise UnprocessableStateError(
            "an assertion must cite at least one observation; a claim supported by "
            "nothing recorded is not a reviewed claim"
        )

    assertion = session.execute(
        select(ResearchAssertion).where(
            ResearchAssertion.review_id == review_id,
            ResearchAssertion.assertion_key == assertion_key,
        )
    ).scalar_one_or_none()
    if assertion is None:
        assertion = ResearchAssertion(
            review_id=review_id,
            tenant_id=actor.tenant_id,
            assertion_key=assertion_key,
            created_by_subject_id=subject,
        )
        session.add(assertion)
        session.flush()
        supersedes, number = None, 1
    else:
        head = session.execute(
            select(ResearchAssertionVersion)
            .where(ResearchAssertionVersion.research_assertion_id == assertion.research_assertion_id)
            .order_by(ResearchAssertionVersion.version_number.desc())
            .limit(1)
        ).scalar_one()
        supersedes, number = head.research_assertion_version_id, head.version_number + 1

    version = ResearchAssertionVersion(
        research_assertion_id=assertion.research_assertion_id,
        review_id=review_id,
        tenant_id=actor.tenant_id,
        version_number=number,
        supersedes_version_id=supersedes,
        protocol_version_id=protocol_version_id,
        schema_ref=EVIDENCE_SCHEMA_REF,
        content_hash=_hash(
            {
                "research_assertion_id": str(assertion.research_assertion_id),
                "version_number": number,
                "statement": statement,
                "content": content,
                # The SUPPORT is part of what the claim is, so it is hashed. The
                # observation versions it cites are not re-hashed into it —
                # `16-…:§14.6.7` — only their ids are.
                "support": sorted((str(s["observation_version_id"]), s["stance"]) for s in support),
            }
        ),
        statement=statement,
        content=content,
        created_by_subject_id=subject,
        created_by_display_name=actor.display_name,
    )
    session.add(version)
    session.flush()

    for cited in support:
        observation_version_id = uuid.UUID(str(cited["observation_version_id"]))
        observation_version = session.get(ObservationVersion, observation_version_id)
        if observation_version is None or observation_version.review_id != review_id:
            raise NotFoundError(f"no such observation version in this review: {observation_version_id}")
        session.add(
            ResearchAssertionSupport(
                research_assertion_version_id=version.research_assertion_version_id,
                observation_version_id=observation_version_id,
                review_id=review_id,
                tenant_id=actor.tenant_id,
                stance=cited["stance"],
            )
        )
    session.flush()
    events.emit_assertion_recorded(session, assertion, version)

    return {
        "research_assertion_id": str(assertion.research_assertion_id),
        "assertion_key": assertion.assertion_key,
        "current_version": {
            "research_assertion_version_id": str(version.research_assertion_version_id),
            "version_number": version.version_number,
            "statement": version.statement,
            "content_hash": version.content_hash,
        },
        "support": [
            {"observation_version_id": str(s["observation_version_id"]), "stance": s["stance"]} for s in support
        ],
    }


# --------------------------------------------------------------------------
# ER-006 — acceptance, the authority act
# --------------------------------------------------------------------------

#: The column on `EvidenceAcceptance` each `subject_kind` fills. The DDL keeps
#: the two in agreement (`ck_acceptance_kind_matches_subject`); this keeps the
#: writer from having to remember which is which.
_SUBJECT_COLUMNS: dict[str, str] = {
    "research_object_version": "research_object_version_id",
    "extraction_dataset_version": "extraction_dataset_version_id",
    "observation_version": "observation_version_id",
}


def _subject_version(
    session: Session, subject_kind: str, subject_version_id: uuid.UUID, review_id: uuid.UUID
) -> ResearchObjectVersion | ExtractionDatasetVersion | ObservationVersion:
    """The version an acceptance is about, scoped to this review.

    Written as an explicit branch rather than a table lookup so the three return
    types stay distinguishable. A `dict[str, type]` reads tidier and costs the
    type checker every guarantee it could otherwise give about `content_hash`.
    """
    row: ResearchObjectVersion | ExtractionDatasetVersion | ObservationVersion | None
    if subject_kind == "research_object_version":
        row = session.get(ResearchObjectVersion, subject_version_id)
    elif subject_kind == "extraction_dataset_version":
        row = session.get(ExtractionDatasetVersion, subject_version_id)
    else:
        row = session.get(ObservationVersion, subject_version_id)
    if row is None or row.review_id != review_id:
        raise NotFoundError(f"no such {subject_kind} in this review")
    return row


def decide_acceptance(
    session: Session,
    actor: Actor,
    review_id: uuid.UUID,
    *,
    subject_kind: str,
    subject_version_id: uuid.UUID,
    decision: str,
    rationale: str,
    supersedes_acceptance_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Accept or withdraw an exact version — the one lead-only act in this module.

    Acceptance is not a column on the version. It is a row here, over an exact
    version, carrying the version's content hash as a WITNESS: that is what makes
    "accepted" mean "accepted THIS content" once the decision has travelled into
    a snapshot or an export and left the version table behind.
    `evidence_acceptances_witnesses_its_subject` refuses a witness that does not
    match, so the column cannot become a free-text field the writer fills in.

    Re-accepting after a withdrawal CONTINUES the chain — it cites the
    withdrawal. It is not a second chain root, and
    `uq_acceptance_one_chain_per_subject` refuses one. The first version of the
    constraint that governs this got it wrong in the other direction and made a
    withdrawn version permanently unacceptable; running it is what found that.
    """
    # The SAME lock `snapshots.request_snapshot` takes — see
    # `reviews.lock_review`. A snapshot's gates read whether a version is
    # currently accepted and this function changes that; the two touch different
    # tables, so without a shared lock both commit and a snapshot can pin a
    # version withdrawn a millisecond earlier.
    from application.services.reviews import lock_review

    lock_review(session, review_id)

    require_review_lead_for(session, actor, review_id, "accept evidence")
    subject = require_human(actor)

    if subject_kind not in ACCEPTANCE_SUBJECTS:
        raise UnprocessableStateError(f"subject_kind must be one of {ACCEPTANCE_SUBJECTS}")
    if decision not in ACCEPTANCE_DECISIONS:
        raise UnprocessableStateError(f"decision must be one of {ACCEPTANCE_DECISIONS}")

    version = _subject_version(session, subject_kind, subject_version_id, review_id)
    column = _SUBJECT_COLUMNS[subject_kind]

    acceptance = EvidenceAcceptance(
        review_id=review_id,
        tenant_id=actor.tenant_id,
        subject_kind=subject_kind,
        subject_content_hash=version.content_hash,
        protocol_version_id=_locked_protocol_id(session, review_id),
        decision=decision,
        rationale=rationale,
        supersedes_acceptance_id=supersedes_acceptance_id,
        decided_by_subject_id=subject,
        decided_by_display_name=actor.display_name,
    )
    setattr(acceptance, column, subject_version_id)
    session.add(acceptance)
    session.flush()
    events.emit_acceptance(session, acceptance, subject_kind=subject_kind, decision=decision)

    return {
        "acceptance_id": str(acceptance.acceptance_id),
        "subject_kind": subject_kind,
        "subject_version_id": str(subject_version_id),
        "subject_content_hash": acceptance.subject_content_hash,
        "decision": decision,
        "rationale": rationale,
        "supersedes_acceptance_id": str(supersedes_acceptance_id) if supersedes_acceptance_id else None,
        "decided_by": acceptance.decided_by_display_name or acceptance.decided_by_subject_id,
    }


def list_acceptances(session: Session, review_id: uuid.UUID) -> dict[str, Any]:
    """Every acceptance decision, oldest first — the chain, not a snapshot.

    Returned as history rather than as "what is currently accepted", because the
    current state is the tail of each chain and computing it here would be a
    second answer to a question the rows already answer.
    """
    rows = session.execute(
        select(EvidenceAcceptance)
        .where(EvidenceAcceptance.review_id == review_id)
        .order_by(EvidenceAcceptance.decided_at)
    ).scalars()
    return {
        "items": [
            {
                "acceptance_id": str(row.acceptance_id),
                "subject_kind": row.subject_kind,
                "subject_version_id": str(row.subject_version_id) if row.subject_version_id else None,
                "subject_content_hash": row.subject_content_hash,
                "decision": row.decision,
                "rationale": row.rationale,
                "supersedes_acceptance_id": (
                    str(row.supersedes_acceptance_id) if row.supersedes_acceptance_id else None
                ),
                "decided_by": row.decided_by_display_name or row.decided_by_subject_id,
                "decided_at": row.decided_at.isoformat().replace("+00:00", "Z") if row.decided_at else None,
            }
            for row in rows
        ]
    }
