"""One spelling for one instant, for the whole service.

A root-level module beside `actor.py` and `database.py`, because this is a
cross-cutting concern and every layer needs it: the API renders instants, the
application layer builds contract artifacts out of them, and both have to agree.

## Why there is exactly one function here

There were THREE spellings, and that is not a style problem.

* `flow._utc_z` renders a trailing `Z`, and its docstring says why:
  `urn:datacompany:stable-identity:1`'s `timestamp` primitive **admits only the `Z`
  form**. Both spellings are valid RFC 3339 and mean the same instant, but they
  are not interchangeable at the contract boundary.
* `review_jobs._iso` rendered `+00:00`. It was written to fix a real, measured
  defect — one call returning `…+02:00` and the next `…+00:00` for the same
  instant — and fixed it in the wrong direction, toward a spelling the released
  grammar refuses.
* Everything else called `.isoformat()` bare, which renders whatever offset the
  value happens to carry.

`ER-007` then put two of those in ONE response body: `created_at` as `+00:00`
in the envelope and `created_at` as `Z` inside the manifest it contained. I
wrote a test asserting that as intended behaviour. It was not intended, it was
two mistakes agreeing with each other.

So: `Z`, everywhere, because the contract has no choice and the envelope has no
reason to differ from the contract it carries.

## Why naive datetimes are treated as UTC rather than converted

`astimezone` on a naive datetime assumes LOCAL time, which would stamp a local
offset with a `Z` and silently move the instant — the exact failure `_utc_z`'s
docstring warns about, arriving through the input rather than the output. Every
timestamp column in this schema is `timestamptz` so a naive value should be
unreachable; if one appears anyway, reading it as UTC is wrong in a way that is
off by a fixed, discoverable amount, where reading it as local is wrong in a way
that depends on where the process is running.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["utc_z"]


def utc_z(value: datetime | None) -> str | None:
    """RFC 3339 in UTC with a trailing ``Z``. ``None`` passes through.

    The single renderer for every instant this service puts on a wire or into a
    hashed artifact. If you are about to write ``.isoformat()``, this is what
    you wanted — `tests/unit/test_one_instant_has_one_spelling.py` will tell you
    so, and it holds a floored ratchet of the call sites that predate it.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
