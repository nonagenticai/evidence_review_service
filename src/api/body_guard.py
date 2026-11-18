"""What every free-form JSON request body must survive before it is used.

Two checks, in this order, on any body carrying caller-supplied JSON of a shape
this service does not model field-by-field:

1. **Bounded depth.** The body is about to be canonicalised under RFC 8785 and
   written to a JSONB column, and both of those walks are recursive.
2. **The pre-ADR regulatory denylist** (`ER-002.7`), at any depth.

## Why the depth check exists separately from the parser's

`infrastructure/imports/import_parser` already bounds depth for the payloads it
parses (`_audit_json`, `MAX_JSON_DEPTH`). That covers imported RECORDS. It does
not cover a request body that reaches a JSONB column without passing through a
parser at all, and this service has two of those:

* `ProtocolBody.inclusion_criteria` / `exclusion_criteria` / `stage_definitions`
  are `dict[str, Any]` / `list[dict[str, Any]]`, so Pydantic validates the outer
  shape and nothing inside it. Measured on `393ae61`: a 1,200-level body returned
  a bare **`500 text/plain`** — not problem+json, so there was nothing for the
  gateway to forward. Only a tenant header was needed to reach it.
* `DedupRunBody.config` was neither depth-bounded nor denylist-guarded, and feeds
  `deduplication_engine.input_digest`, which canonicalises it under RFC 8785.
  Measured: the same `500`, and separately a `201` for a config carrying a
  guarded discriminator.

The thrower in both cases was `find_forbidden_keys`, which is now iterative — so
these bodies no longer crash *there*. That is not sufficient on its own: past the
guard the same structure still reaches `canonical_sha256` and the JSONB write,
which recurse on their own budget. The bound is what makes the refusal typed and
early rather than moved one frame down the call stack.

`MAX_BODY_DEPTH` is deliberately its own constant rather than an import of
`MAX_JSON_DEPTH`. They happen to be the same number and answer to different
owners: one bounds a bibliographic record inside a file a user uploaded, the
other bounds a request body this service designed the schema for. Coupling them
would mean a change made for one silently re-tunes the other.
"""

from __future__ import annotations

from typing import Any

from api.problem_details import ForbiddenDiscriminatorError, ValidationFailedError
from domain.policies.denylist import find_forbidden_keys

#: Deepest a request body may nest. `stage_definitions` in the deepest protocol
#: anybody has written is four levels; sixty-four is generous by an order of
#: magnitude and still far below the ~1,000-frame interpreter limit that the
#: recursive walkers downstream (RFC 8785 canonicalisation, the JSONB encoder)
#: are each measured against on their own.
MAX_BODY_DEPTH = 64


def _path_label(path: tuple[object, ...]) -> str:
    """A JSON-pointer-ish path, elided in the middle when it is absurdly long.

    A depth failure produces a 64-segment path of nothing but `.a`, which is
    noise in a problem+json `detail`. The ends are the informative part. Same
    shape as `import_parser._path_label`, for the same reason.
    """
    parts = [f"[{p}]" if isinstance(p, int) else f".{p}" for p in path]
    if len(parts) > 8:
        parts = parts[:4] + [f" … {len(parts) - 8} more … "] + parts[-4:]
    return "$" + "".join(parts)


def require_bounded_depth(payload: Any, what: str, *, max_depth: int = MAX_BODY_DEPTH) -> None:
    """Refuse a body that nests past `max_depth`.

    Iterative on an explicit stack, not recursive — the same reason
    `import_parser._audit_json` is: a recursive depth check cannot survive the
    input it exists to reject.

    Depth-first, so a deep body is refused after roughly `max_depth` node visits
    rather than after visiting the whole structure. That is what makes this
    usable as a pre-filter in front of an expensive walk: the cost of refusing is
    bounded by the bound, not by the size of the thing being refused.

    The path is carried as a tuple and rendered only on failure. Every request
    body walks this, so formatting a human-readable path at each of possibly
    millions of nodes in order to discard all of them would be paying import
    latency for an error message nobody sees.

    `max_depth` is a parameter because one caller measures a structure the others
    see through a wrapper — see `routers/imports.ENVELOPE_DEPTH_BOUND`. It is not
    an invitation to pick a number per call site.
    """
    stack: list[tuple[Any, int, tuple[object, ...]]] = [(payload, 1, ())]
    while stack:
        node, depth, path = stack.pop()
        if depth > max_depth:
            raise ValidationFailedError(
                f"{what} nests more than {max_depth} levels deep at {_path_label(path)}. "
                "Review criteria and engine configuration are flat objects; nothing legitimate "
                "is this deep, and the canonical hash and the JSONB write would both exhaust the "
                "stack on it."
            )
        if isinstance(node, dict):
            for key, value in node.items():
                stack.append((value, depth + 1, (*path, key)))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                stack.append((item, depth + 1, (*path, index)))


def guard_body(payload: Any, what: str) -> None:
    """Depth, then the denylist. Raises; returns `None` when the body is clean.

    Order matters. The denylist walk no longer crashes at any depth, but a body
    that is going to be refused for depth anyway should not first be walked in
    full — and a caller who sends both problems at once is better told about the
    structural one, which is the one that makes the rest of the body unusable.

    `ValidationFailedError` (422 `validation_failed`) rather than
    `UnrecognizedPayloadError` for the depth case, even though the parser uses
    the latter for the same *condition*: that slug's documented remedy is
    "re-export the file — usually as UTF-8 — or correct `source_kind`", which is
    meaningless advice for a JSON request body this service defined the schema
    for. The import path keeps `unrecognized_payload` because there the input
    really is a file somebody exported.
    """
    require_bounded_depth(payload, what)
    hits = find_forbidden_keys(payload)
    if hits:
        raise ForbiddenDiscriminatorError(
            "the request carries a guarded pre-ADR regulatory discriminator",
            forbidden_paths=hits,
        )
