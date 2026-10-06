"""The LiveKit webhook path secret never reaches the access log (security M8)."""

import logging

from api.logging_config import MaskSecretPathFilter, install_access_log_masking


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:5000", "POST", path, "1.1", 200),
        None,
    )


def test_webhook_secret_is_masked():
    record = _access_record("/api/v1/livekit/inbound/s3cr3t-value?x=1")
    assert MaskSecretPathFilter().filter(record) is True
    message = record.getMessage()
    assert "s3cr3t-value" not in message
    assert "/api/v1/livekit/inbound/<redacted>" in message


def test_other_paths_untouched():
    record = _access_record("/api/v1/health")
    MaskSecretPathFilter().filter(record)
    assert "/api/v1/health" in record.getMessage()


def test_installed_on_uvicorn_access_once(caplog):
    install_access_log_masking()
    install_access_log_masking()
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, MaskSecretPathFilter) for f in access.filters) == 1

    access.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="uvicorn.access"):
            access.info(
                '%s - "%s %s HTTP/%s" %d',
                "10.0.0.1:5000",
                "POST",
                "/api/v1/livekit/inbound/s3cr3t-value",
                "1.1",
                404,
            )
    finally:
        access.removeHandler(caplog.handler)
    assert "s3cr3t-value" not in caplog.text
    assert "<redacted>" in caplog.text
