"""Live shop-floor state (Wave 6d): what each cell is doing now, from the Unified Namespace.

Edge agents and the MES publish each cell's state as a retained ``equipment-state/v1`` message on
``<cell>/state/status`` (UNS contract v1). The UNS listener passes every such message to
``record_cell_state``, which keeps the latest one per work centre in ``CellState``.

Rules, so that repeating or reordering messages is harmless (ADR 0010, ADR 0011):

* **Unchanged state is not written.** The retained message arrives again on every reconnect; if
  state, since, order and operation are the same, nothing is saved and no outbox event is made.
* **Older never overwrites newer.** Each message carries its timestamp; a report older than the one
  stored (a late QoS 1 redelivery) is ignored.
* **Cleared means unknown.** An empty retained payload (the cell was removed from the namespace)
  deletes the row: the ERP no longer claims to know the cell's state.

Saving or deleting the row writes an outbox event (``production.cellstate``), which re-projects the
cell's graph node with its state.
"""
import logging
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction

from .models import CellState, WorkCenter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Report:
    """One state report, already checked against the contract."""

    state: str
    since: datetime | None
    order_number: str
    operation_number: int | None
    source: str
    reported_at: datetime


def work_center_for_path(path: str) -> WorkCenter | None:
    """The work centre whose UNS path is ``path`` (``<company>/<plant>/<area>/<line>/<cell>``), or None."""
    levels = path.split("/")
    if len(levels) != 5:
        return None
    company, plant, area, line, cell = levels
    return (WorkCenter.objects.select_related("plant__company_code", "line__area")
            .filter(plant__company_code__code__iexact=company, plant__code__iexact=plant,
                    line__area__code__iexact=area, line__code__iexact=line, code__iexact=cell)
            .first())


def record_cell_state(work_center: WorkCenter, report: Report) -> str:
    """Keep ``report`` as the cell's latest state. Returns what happened: ``created``, ``updated``,
    ``unchanged`` or ``stale``.
    """
    with transaction.atomic():
        # Lock the row, so two reports handled at the same time are compared one after the other.
        current = CellState.objects.select_for_update().filter(work_center=work_center).first()
        if current is not None:
            if report.reported_at < current.reported_at:
                logger.info("Cell %s: stale %s report from %s ignored (have %s from %s)", work_center.uns_path,
                            report.state, report.reported_at.isoformat(), current.state,
                            current.reported_at.isoformat())
                return "stale"
            same = (current.state, current.since, current.order_number, current.operation_number) == (
                report.state, report.since, report.order_number, report.operation_number)
            if same:
                return "unchanged"  # the retained message again: nothing to write, no outbox event
        fields = {"state": report.state, "since": report.since, "order_number": report.order_number,
                  "operation_number": report.operation_number, "source": report.source,
                  "reported_at": report.reported_at}
        _, created = CellState.objects.update_or_create(work_center=work_center, defaults=fields)
    logger.info("Cell %s is %s%s", work_center.uns_path, report.state,
                f" ({report.order_number}/{report.operation_number:04d})"
                if report.order_number and report.operation_number else "")
    return "created" if created else "updated"


def clear_cell_state(work_center: WorkCenter) -> bool:
    """Forget the cell's state (its retained topic was cleared). Returns True if there was one."""
    deleted, _ = CellState.objects.filter(work_center=work_center).delete()
    if deleted:
        logger.info("Cell %s: state cleared from the namespace", work_center.uns_path)
    return bool(deleted)


def plant_states(plant) -> dict[int, CellState]:
    """Every known cell state of a plant, by work centre id (for the equipment tree)."""
    return {s.work_center_id: s for s in CellState.objects.filter(work_center__plant=plant)}


def state_data(cell_state: CellState | None) -> dict | None:
    """A cell state as the API shows it, or None when the ERP has no report for the cell."""
    if cell_state is None:
        return None
    return {"state": cell_state.state, "since": cell_state.since, "order": cell_state.order_number or None,
            "operation_number": cell_state.operation_number, "source": cell_state.source,
            "reported_at": cell_state.reported_at}
