"""The business-rules catalogue (Wave 9b): ``docs/RULES.md`` is generated from the code and kept true.

A rule added, changed or removed in the service layer must show in the catalogue in the same pull
request; otherwise this test fails and says how to regenerate it.
"""
import ast
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from apps.common import rules


def test_the_catalogue_matches_the_code():
    current = rules.catalogue_path().read_text(encoding="utf-8")
    assert current == rules.render(rules.collect()), \
        "docs/RULES.md is out of date: run python manage.py business_rules and commit the file"


def test_well_known_rules_are_in_it():
    messages = {(r.area, r.message) for r in rules.collect()}
    assert ("sales", "Customer {customer.number} is blocked.") in messages
    assert ("finance", "Posted journal documents cannot be changed; post a reversal instead.") in messages
    assert ("control", "A rejection needs a reason: it is how the recommendations improve.") in messages
    assert len(messages) > 200  # guards against the scan silently finding nothing


def test_error_classes_are_found_through_inheritance():
    tree = ast.parse("class PostingError(Exception): pass\nclass A(PostingError): pass\n"
                     "class B(A): pass\nclass C(ValueError): pass")
    assert rules.rule_errors({None: tree}) == {"PostingError", "ImmutableDocumentError", "A", "B"}


@pytest.mark.parametrize("source,expected", [
    ('"Plain."', "Plain."),
    ('f"Order {order.number} is closed."', "Order {order.number} is closed."),
    ('"Part one " + f"and {two}."', "Part one and {two}."),
    ("message", "{message}"),
    (None, ""),
])
def test_messages_are_readable(source, expected):
    node = ast.parse(source, mode="eval").body if source else None
    assert rules.message_of(node) == expected


def test_passed_on_messages_are_not_counted_twice():
    assert rules._passed_on("{str(exc)}") and not rules._passed_on("Order {number} is closed.")


def test_the_command_checks_and_writes_idempotently(tmp_path, monkeypatch):
    out = StringIO()
    call_command("business_rules", "--check", stdout=out)
    assert "is current" in out.getvalue()
    target = tmp_path / "RULES.md"
    monkeypatch.setattr(rules, "catalogue_path", lambda: target)
    with pytest.raises(CommandError, match="out of date"):
        call_command("business_rules", "--check", stdout=out)
    call_command("business_rules", stdout=out)
    assert "written" in out.getvalue() and target.read_text(encoding="utf-8").startswith("# Business rules")
    call_command("business_rules", stdout=out)
    assert "unchanged" in out.getvalue()  # running it again changes nothing
