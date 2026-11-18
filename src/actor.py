"""The acting principal, and how it is bound to a database transaction.

This module is the seam reviewer blinding (ER-004.2) hangs off, so it is written
before anything it protects.

Two rules it encodes, both from the specification rather than from convenience:

* **A decision needs a human.** ``12-datacompany-solution-profile-specification.md:121-124``
  gives machines authority to "normalize, score, cluster, rank and propose" and
  reserves inclusion/exclusion for a human. A caller carrying a tenant but no
  subject is therefore refused at this layer, not merely discouraged in review:
  a service or n8n principal cannot record a screening decision at all.
* **The GUCs are transaction-scoped.** ``SET LOCAL`` / ``set_config(..., is_local => true)``
  and never plain ``SET``. Every connection reaches Postgres through PgBouncer,
  which reuses server connections across clients; a session-scoped GUC would leak
  one reviewer's identity into the next reviewer's transaction, and the failure
  would be intermittent and invisible. ``kag_common.database.rls`` already
  established this for the tenant GUC; the actor and role GUCs follow it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.orm import Session

#: Transaction-scoped GUCs read by the row-level-security policies. Names are
#: repeated in the migration that creates those policies; ``tests`` assert the two
#: agree, because a policy referencing a GUC nobody sets is a policy that silently
#: matches nothing.
TENANT_GUC = "app.current_tenant"
ACTOR_GUC = "app.actor_subject_id"
ROLE_GUC = "app.actor_role"


class ActorRole(StrEnum):
    """What the caller may see, not what they may do.

    ``LEAD`` is the only role that reads across reviewers, and it exists because
    somebody has to be able to adjudicate and export. It is not an admin flag:
    a lead is still refused as adjudicator on a conflict they took part in
    (``01-evidence-review-8810.md:659``).
    """

    REVIEWER = "reviewer"
    LEAD = "lead"


@dataclass(frozen=True)
class Actor:
    """An authenticated principal acting inside one tenant."""

    #: A `uuid.UUID`, not a `str`, and the type is load-bearing rather than tidy.
    #:
    #: `0014` made all 25 `tenant_id` columns `uuid`, and psycopg returns a
    #: `uuid.UUID` from a `uuid` column regardless of `as_uuid`. Left as `str`,
    #: every `row.tenant_id == actor.tenant_id` in the service would have become
    #: silently False — no exception, no log line, just a tenant that matches
    #: nothing. `api/dependencies` is the one place the conversion happens, and
    #: it refuses a value that is not a UUID rather than carrying it inward.
    tenant_id: uuid.UUID
    subject_id: str | None
    role: ActorRole = ActorRole.REVIEWER
    #: The credential identified a MACHINE, even though it carries a subject.
    #:
    #: This exists because "a service principal has no subject" was false. A
    #: Keycloak client-credentials token has a perfectly good `sub` — the
    #: service account user's UUID — so under real auth every service principal
    #: sailed through `require_human`, and `ER-004`'s "service/n8n principals
    #: cannot submit a human decision" held only on the header path used by
    #: local `AUTH_BYPASS` stacks, where a missing `X-Subject-Id` means no
    #: subject at all. The guard was real in tests and absent in production.
    is_service_principal: bool = False
    #: What to CALL this principal, when the credential said.
    #:
    #: Not an identifier and never treated as one. `subject_id` remains the
    #: attribution that anything evidentiary hangs off; this is a label placed
    #: on top of it so a lead's ruling reads as a person rather than a UUID.
    #: `None` whenever the token carried no usable name, which is a normal
    #: outcome and not an error — the renderer falls back to the subject id.
    #:
    #: Deliberately not set for a service principal: a machine's
    #: `service-account-…` username is not a person's name, and writing it
    #: beside a human decision would misdescribe who acted.
    display_name: str | None = None

    @property
    def is_human(self) -> bool:
        """A person, not a machine wearing a subject.

        Two conditions, because either alone is insufficient: a subject must be
        present (a tenant alone is a service), AND the credential must not have
        identified itself as a service account (which has a subject).
        """
        return bool(self.subject_id and self.subject_id.strip()) and not self.is_service_principal


class NotAHumanPrincipalError(PermissionError):
    """Raised when a service principal attempts a human judgement."""


def require_human(actor: Actor) -> str:
    """Return the acting subject, or refuse.

    Screening automation ranks and proposes. It does not decide. This is the
    single place that distinction is enforced, so that "no automatic exclusion
    path exists" is a property of the code rather than a claim about it.
    """
    subject = (actor.subject_id or "").strip()
    if not subject:
        raise NotAHumanPrincipalError(
            "this operation records an attributable human judgement; the caller presented a "
            "tenant but no subject, which is a service principal"
        )
    if actor.is_service_principal:
        # A service account's `sub` is a real, stable identifier — which is
        # precisely what makes this dangerous: without this branch the ledger
        # would record a decision attributed to a machine account and nothing
        # downstream could tell it from a person.
        raise NotAHumanPrincipalError(
            "this operation records an attributable human judgement; the credential is a "
            "service account, whose subject identifies a client rather than a person"
        )
    return subject


def bind_actor(session: Session, actor: Actor) -> None:
    """Bind ``actor`` to the current transaction for the RLS policies to read.

    ``is_local => true`` on every one. Parameterised, never interpolated: these
    values arrive from a token and reach a ``set_config`` call, so a formatted
    string here would be an injection site in the one function whose whole job is
    to be trustworthy.
    """
    session.execute(
        text(f"SELECT set_config('{TENANT_GUC}', :tenant, true)"),  # noqa: S608 - constant name, bound value
        {"tenant": str(actor.tenant_id)},
    )
    session.execute(
        text(f"SELECT set_config('{ACTOR_GUC}', :subject, true)"),  # noqa: S608
        {"subject": actor.subject_id or ""},
    )
    session.execute(
        text(f"SELECT set_config('{ROLE_GUC}', :role, true)"),  # noqa: S608
        {"role": str(actor.role)},
    )
