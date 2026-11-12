"""The Unified Namespace contract, version 1, as code (``docs/UNS_CONTRACT.md``).

Topic paths, schema names and the message envelope live here and nowhere else, so the publisher,
the listener and the tests cannot drift apart. Amounts and quantities are written as exact decimal
strings, as in the API (ADR 0008).
"""
import json
from datetime import datetime

from django.utils import timezone

from apps.common.renderers import ExactDecimalEncoder

SOURCE = "erp-intelligence"
EQUIPMENT = "equipment/v1"
OPERATION_REQUEST = "operation-request/v1"
OPERATION_PERFORMANCE = "operation-performance/v1"
CONFIRMATION_RESULT = "confirmation-result/v1"
EQUIPMENT_STATE = "equipment-state/v1"
STATES = ("RUNNING", "IDLE", "SETUP", "DOWN", "OFFLINE")

# Equipment-level topics: <enterprise>/<site>/<area>/<line>/<cell>/<namespace>/<name>
PERFORMANCE_SUBSCRIPTION = "+/+/+/+/+/events/operation-performance"
STATE_SUBSCRIPTION = "+/+/+/+/+/state/status"  # retained cell state from edge agents and the MES (Wave 6d)


def equipment_topic(uns_path: str) -> str:
    """Retained definition of a cell."""
    return f"{uns_path}/definition/equipment"


def operation_request_topic(uns_path: str, order_number: str, operation_number: int) -> str:
    """Retained request for one operation of an order, at the cell that performs it (lower case)."""
    return f"{uns_path}/schedule/operation-requests/{order_number.lower()}-{operation_number:04d}"


def confirmation_result_topic(company_code: str, plant: str) -> str:
    """Site-level topic where the ERP answers every performance event."""
    return f"{company_code.lower()}/{plant.lower()}/erp/events/confirmation-result"


def envelope(schema: str, data: dict, event_id: str | None = None, at: datetime | None = None) -> bytes:
    """A message in the contract's envelope, as UTF-8 JSON bytes."""
    message = {"schema": schema, "event_id": event_id, "timestamp": (at or timezone.now()).isoformat(),
               "source": SOURCE, "data": data}
    return json.dumps(message, cls=ExactDecimalEncoder).encode("utf-8")
