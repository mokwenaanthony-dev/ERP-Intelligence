"""The UNS contract, enforced: every message checked against ``contract/uns/v1/`` (Wave 6e, ADR 0021).

``docs/UNS_CONTRACT.md`` says what the messages look like; the JSON Schema files say it for machines.
These tests make CI fail when the two sides could drift apart:

* the schema files are valid JSON Schema, and they name the same messages as ``apps/uns/contract.py``;
* every example follows its schema, and every message type has an example;
* everything the ERP publishes (equipment, operation requests, every answer it gives the MES, posted,
  replayed or rejected) follows the contract, and sends no field the contract does not document;
* the MES messages these tests send follow it too, so the ERP is tested with what the MES really sends;
* ``SHA256SUMS``, which the MES pins, is up to date, so a contract change is never silent.

The scenario is ``tests/test_uns.py``'s: frame order MO00000001 released at the press
``1000/1000/assy/l1/press3``.
"""
import json
from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from jsonschema import Draft202012Validator

from apps.production import orders
from apps.uns import contract, listener, publisher, schemas
from tests import test_uns as scenario
from tests.test_uns import PERFORMANCE, RESULTS, performance, released_frame_order
from tests.test_uns_state import state

pytestmark = pytest.mark.django_db
uns = scenario.uns  # the same fixture: UNS on, demo production data, a memory broker
EXAMPLES = sorted(schemas.EXAMPLES_DIR.glob("*.json"))


def message(path):
    """An example message, parsed."""
    return json.loads(path.read_text(encoding="utf-8"))


def follows_the_contract(payload):
    """Assert a payload follows its schema and sends only documented fields; return it parsed (or None)."""
    assert schemas.errors(payload) == []
    if not payload:
        return None  # an empty retained payload clears the topic
    parsed = json.loads(payload)
    undocumented = set(parsed["data"]) - schemas.documented_fields(parsed["schema"])
    assert not undocumented, f"{parsed['schema']} sends fields the contract does not document: {undocumented}"
    return parsed


# --- the files -------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(schemas.schema_files()))
def test_every_schema_file_is_valid_json_schema(name):
    Draft202012Validator.check_schema(schemas.schema_files()[name])


def test_the_files_and_the_code_name_the_same_messages():
    assert schemas.message_types() == sorted([contract.EQUIPMENT, contract.OPERATION_REQUEST,
                                              contract.OPERATION_PERFORMANCE, contract.CONFIRMATION_RESULT,
                                              contract.EQUIPMENT_STATE])
    assert schemas.schema_files()["equipment-state"]["properties"]["data"]["properties"]["state"]["enum"] == \
        list(contract.STATES)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_every_example_follows_its_schema(path):
    follows_the_contract(path.read_bytes())


def test_every_message_type_has_an_example():
    assert {message(p)["schema"] for p in EXAMPLES} == set(schemas.message_types())


def test_the_checksums_the_mes_pins_are_up_to_date():
    out = StringIO()
    call_command("uns_contract", "--check", stdout=out)
    assert "SHA256SUMS is up to date" in out.getvalue()


# --- what the ERP publishes ------------------------------------------------------------------------

def test_what_the_erp_publishes_follows_the_contract(uns):
    order = released_frame_order(uns, "10")
    publisher.publish_outbox(broker=uns.broker)
    publisher.republish(broker=uns.broker)  # without event IDs: retained state may carry none
    orders.complete_order(order, date(2026, 6, 1))
    publisher.publish_outbox(broker=uns.broker)  # clears the request: an empty payload

    kinds = [follows_the_contract(payload) for _topic, payload, _retain in uns.broker.published]
    published = {m["schema"] for m in kinds if m}
    assert published == {contract.EQUIPMENT, contract.OPERATION_REQUEST}
    assert None in kinds  # the cleared request was checked too


@pytest.mark.parametrize("payloads", [
    [performance()],                                          # posted
    [performance(), performance()],                           # posted, then replayed
    [performance(order="MO99999999")],                        # rejected by a business rule
    [performance(yield_quantity="lots")],                     # rejected: not a decimal string
    [performance(event_id="")],                               # rejected: no idempotency key
    [b"not json"],                                            # rejected: event_id unknown, so null
    [b'{"schema": "operation-performance/v1"}'],              # rejected: not an envelope
], ids=["posted", "replayed", "rule", "decimal", "no-event-id", "not-json", "no-data"])
def test_every_answer_the_erp_gives_follows_the_contract(uns, payloads):
    released_frame_order(uns)
    for payload in payloads:
        listener.handle_performance(uns.broker, PERFORMANCE, payload)
    answers = [follows_the_contract(p) for t, p, _ in uns.broker.published if t == RESULTS]
    assert len(answers) == len(payloads)
    assert answers[-1]["data"]["replayed"] is (len(payloads) == 2)


# --- what the MES sends ----------------------------------------------------------------------------

def test_the_mes_messages_these_tests_send_follow_the_contract():
    follows_the_contract(performance())
    without_activities = json.loads(performance())
    del without_activities["data"]["activities"]  # may be omitted: the ERP derives them from the plan
    follows_the_contract(json.dumps(without_activities).encode())
    follows_the_contract(state())
    follows_the_contract(state(order=None, operation=None, since=None, value="IDLE"))


@pytest.mark.parametrize("change, problem", [
    ({"yield_quantity": 100}, "data/yield_quantity: 100 is not of type 'string'"),
    ({"yield_quantity": "-1"}, "data/yield_quantity"),
    ({"posting_date": "2026-02-30"}, "is not a 'date'"),
    ({"operation_number": "10"}, "data/operation_number"),
    ({"activities": {"MACH": 10.5}}, "data/activities/MACH"),
    ({"final": "yes"}, "data/final"),
])
def test_a_performance_event_that_breaks_the_contract_is_caught(change, problem):
    event = message(schemas.EXAMPLES_DIR / "operation-performance.json")
    event["data"].update(change)
    assert any(problem in line for line in schemas.errors(event))


@pytest.mark.parametrize("edit, problem", [
    (lambda m: m.pop("event_id"), "'event_id' is a required property"),        # an event needs its key
    (lambda m: m.update(timestamp="2026-06-01 10:15"), "timestamp"),           # ISO 8601 with an offset
    (lambda m: m.update(schema="operation-performance/v9"), "Unknown schema"),
    (lambda m: m.pop("data"), "'data' is a required property"),
])
def test_an_envelope_that_breaks_the_contract_is_caught(edit, problem):
    event = message(schemas.EXAMPLES_DIR / "operation-performance.json")
    edit(event)
    assert any(problem in line for line in schemas.errors(event))


def test_answers_are_consistent_with_their_status():
    posted = message(schemas.EXAMPLES_DIR / "confirmation-result.json")
    posted["data"]["reason"] = "a posted answer has no reason"
    assert any("data/reason" in line for line in schemas.errors(posted))
    rejected = message(schemas.EXAMPLES_DIR / "confirmation-result-rejected.json")
    rejected["data"]["confirmation_number"] = "0001"
    assert any("data/confirmation_number" in line for line in schemas.errors(rejected))


def test_topic_paths_are_lower_case_codes():
    cell = message(schemas.EXAMPLES_DIR / "equipment.json")
    cell["data"]["uns_path"] = "1000/1000/ASSY/l1/press3"
    assert any("data/uns_path" in line for line in schemas.errors(cell))


def test_the_listener_stays_a_tolerant_reader(uns):
    """A quantity sent as a JSON number breaks the contract, but the ERP still posts it: consumers
    are not made stricter by the schemas, so a schema change can never stop production (ADR 0021)."""
    released_frame_order(uns)
    payload = performance(yield_quantity=100)
    assert schemas.errors(payload)
    assert listener.handle_performance(uns.broker, PERFORMANCE, payload)["status"] == "POSTED"


# --- the command -----------------------------------------------------------------------------------

def test_the_command_checks_a_captured_message(tmp_path):
    good, bad, empty = tmp_path / "good.json", tmp_path / "bad.json", tmp_path / "empty.json"
    good.write_bytes(performance())
    bad.write_bytes(performance(yield_quantity=100))
    empty.write_bytes(b"")
    out = StringIO()
    call_command("uns_contract", "--message", str(good), stdout=out)
    assert "follows the contract (operation-performance/v1)" in out.getvalue()
    call_command("uns_contract", "--message", str(empty), stdout=out)
    assert "clears a retained topic" in out.getvalue()
    with pytest.raises(CommandError, match="yield_quantity: 100 is not of type 'string'"):
        call_command("uns_contract", "--message", str(bad), stdout=StringIO())
    with pytest.raises(CommandError, match="No such file"):
        call_command("uns_contract", "--message", str(tmp_path / "missing.json"), stdout=StringIO())


def test_the_command_writes_the_checksums_when_they_are_out_of_date(tmp_path, monkeypatch):
    sums = tmp_path / "SHA256SUMS"
    sums.write_text("stale\n", encoding="utf-8")
    monkeypatch.setattr(schemas, "SUMS_FILE", sums)
    with pytest.raises(CommandError, match="out of date"):
        call_command("uns_contract", "--check", stdout=StringIO())
    out = StringIO()
    call_command("uns_contract", stdout=out)
    assert "SHA256SUMS written" in out.getvalue() and sums.read_text(encoding="utf-8") == schemas.checksums()
    call_command("uns_contract", stdout=out)  # idempotent: a second run changes nothing
    assert out.getvalue().endswith("SHA256SUMS is up to date.\n")
