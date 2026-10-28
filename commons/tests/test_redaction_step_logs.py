"""A secret-named environment value with no credential shape is masked in the step log's
message, fields and error (DF-178): the shape scrubber alone would not catch it."""

import json
import logging

from agentic.telemetry.logs import StepLogs
from sdlc_commons.redaction import redactor

PLAIN = "correct-horse-battery"  # 8+ characters, no credential shape


def test_env_value_without_a_credential_shape_is_masked(tmp_path):
    logs = StepLogs(redactor(env={"ACME_DEPLOY_PASSWORD": PLAIN}))
    log = logging.getLogger("agentic.engine")
    with logs.step(tmp_path, "r-1", "develop", 1) as path:
        try:
            raise RuntimeError(f"login failed for {PLAIN}")
        except RuntimeError:
            log.exception("step failed with %s", PLAIN, extra={"fields": {"secret": PLAIN}})
    text = path.read_text("utf-8")
    assert PLAIN not in text
    record = json.loads(text.splitlines()[-1])
    assert "<redacted>" in record["message"]
    assert record["fields"]["secret"] == "<redacted>"
    assert "<redacted>" in record["error"]


def test_the_same_value_is_kept_when_its_name_is_not_secret(tmp_path):
    redact = redactor(env={"ACME_COLOUR": PLAIN})
    assert redact(f"x {PLAIN}") == f"x {PLAIN}"
