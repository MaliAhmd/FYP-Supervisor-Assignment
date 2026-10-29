import json
import logging

from app.logging_config import MetadataFormatter


def test_logs_exclude_message_arguments_exceptions_and_redact_secrets():
    record = logging.LogRecord("supervisorai", logging.INFO, "", 1, "secret %s", ("private-token",), None)
    record.event_name = "gateway.failed"
    record.metadata = {"authorization": "Bearer hidden", "nested": {"api_key": "private"}, "project_id": "p1"}
    output = MetadataFormatter().format(record)
    assert "hidden" not in output
    assert "private" not in output
    assert "secret" not in output
    assert json.loads(output)["metadata"]["project_id"] == "p1"
