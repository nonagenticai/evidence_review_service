"""Stable uuids for readable test tenant labels.

Every `tenant_id` column in this database is `uuid` as of migration `0014`, so a
literal like `"tenant-stage-blinding"` no longer round-trips — it is refused at
the boundary and, past it, `invalid input syntax for type uuid`.

The obvious fix is to scatter hand-picked uuids through the suite, and it is the
wrong one: `TENANT = "tenant-stage-blinding"` says what the fixture is FOR, and
`TENANT = "0195f48b-...-a41c"` says nothing. Worse, hand-picked constants collide
silently — two files reusing one uuid share a tenant, and a cross-tenant
isolation test that should fail would pass.

So the label stays and the uuid is derived from it: same label, same uuid, every
run and every process; different labels, different uuids, by construction.
"""

from __future__ import annotations

import uuid

#: Fixed, and arbitrary only in the sense that any constant would do. It must
#: never change: these uuids appear in stored rows during a test run, and a new
#: namespace would silently repartition every fixture's data.
_TEST_TENANT_NAMESPACE = uuid.UUID("0195f48b-d000-7000-8000-00000000decd")


def tenant(label: str) -> str:
    """The uuid for a readable tenant label, as a string.

    Returned as `str` rather than `uuid.UUID` on purpose: these values are sent
    as HTTP headers and bound into `set_config(text, text, bool)` alongside uuid
    columns in the same script. psycopg would send a `uuid.UUID` with the uuid
    OID, and there is no `set_config(text, uuid, bool)`.
    """
    return str(uuid.uuid5(_TEST_TENANT_NAMESPACE, label))
