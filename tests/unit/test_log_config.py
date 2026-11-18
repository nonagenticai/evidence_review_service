"""The log's `environment` stamp must agree with the service's own default.

They did not. `config.Settings` defaulted `ENVIRONMENT` to `production` and
`log_config` defaulted it to `development`, and the production deployment sets
neither — so every log line the live service emitted claimed to come from a
development environment. An operator filtering their log store for
`environment=production` would have seen nothing from this service and read
that as silence rather than as a mislabel.

The value is deliberately duplicated rather than imported (logging is
configured before settings are parsed, and a logger that depends on
configuration parsing cannot report a configuration error). This test is what
makes the duplication safe.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from config import Settings  # noqa: E402
from log_config import DEFAULT_ENVIRONMENT, add_service_context  # noqa: E402


def test_the_two_defaults_agree(monkeypatch) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    assert DEFAULT_ENVIRONMENT == Settings().environment


def test_an_unset_environment_is_stamped_production(monkeypatch) -> None:
    """The exact production case: the deployment sets no ENVIRONMENT."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    event = add_service_context(None, "info", {})
    assert event["environment"] == "production"


def test_an_explicit_environment_still_wins(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert add_service_context(None, "info", {})["environment"] == "staging"


def test_the_service_name_is_stamped(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    assert add_service_context(None, "info", {})["service"] == "evidence-review"
