"""The portal demo script (``scripts/demo_portal.py``) runs green against a live server seeded like Docker.

The script is what a person runs to see Wave 10a work end to end (``docs/DEMO.md``); this test makes
sure it keeps working as the system changes. It seeds a fresh database with ``seed_data``, starts a
real HTTP server (pytest-django's ``live_server``) and runs the script twice: the second run must give
the same answers, because every write it makes is idempotent.
"""
import importlib.util
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

from apps.portal.models import DeliveryNotice, OrderConfirmation

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "demo_portal.py"


def load_script():
    """Import the script as a module (it lives outside the Django apps)."""
    spec = importlib.util.spec_from_file_location("demo_portal", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.django_db(transaction=True)
def test_the_demo_runs_green_twice(live_server, capsys):
    call_command("seed_data", stdout=StringIO())
    demo = load_script()
    assert demo.main(["--base-url", live_server.url]) == 0
    first = capsys.readouterr().out
    assert "The wall holds: internal master data answers 403 to a supplier" in first
    assert "lines changed 1, then 0 the second time" in first
    assert "Control tower (purchasing): Confirmed later than agreed" in first
    assert "The wall holds for customers too" in first and first.rstrip().endswith("same answers.")

    assert demo.main(["--base-url", live_server.url]) == 0  # again: the same steps, the same answers
    second = capsys.readouterr().out
    assert "lines changed 0, then 0 the second time" in second
    assert OrderConfirmation.objects.count() == 1 and DeliveryNotice.objects.count() == 1


def test_a_wrong_answer_stops_the_demo(capsys):
    demo = load_script()
    with pytest.raises(demo.DemoFailed, match="expected 403, got 200"):
        demo.expect(200, 403, "the role wall")
    assert demo.main(["--base-url", "http://127.0.0.1:9"]) == 1  # nothing listens there
    assert "did not answer" in capsys.readouterr().out
