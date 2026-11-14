"""Request IDs in the service's log lines (no web framework needed)."""
import logging

from app import logs


def test_safe_ids_are_kept_and_unsafe_ones_replaced():
    assert logs.set_request_id("erp-req-12345678") == "erp-req-12345678"
    assert logs.current_request_id() == "erp-req-12345678"
    replaced = logs.set_request_id("bad id\nforged line")
    assert replaced != "bad id\nforged line" and len(replaced) == 12
    assert logs.set_request_id(None) != replaced


def test_every_record_carries_the_request_id(caplog):
    logs.configure()
    logs.set_request_id("erp-req-87654321")
    with caplog.at_level(logging.INFO):
        logging.getLogger("app.test").info("hello")
    assert caplog.records[-1].request_id == "erp-req-87654321"
    formatted = logging.Formatter(logs.FORMAT).format(caplog.records[-1])
    assert "| erp-req-87654321 | app.test | test_logs.test_every_record_carries_the_request_id:" in formatted
