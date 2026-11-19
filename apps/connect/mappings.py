"""How another ERP's export files translate into ours (Wave 10b, ADR 0020).

A ``Mapping`` describes one source system's files: for each kind of object, which column holds
each of our fields, how its codes translate into ours (a unit "UN" becomes "EA", a site "JHB"
becomes plant "1000"), how dates and decimals are written. The import reads every file through
a mapping, so the rules that load data never depend on where it came from.

``generic`` is the layout documented in ``docs/IMPORT_FORMAT.md``: our field names as column
headings, ISO dates, a full stop for decimals, comma or semicolon separators. Any ERP that can export
a report can produce it. A mapping for Sage X3's own export files is added once it can be built and
tested against a real X3 instance (its SEED demo folder), not guessed.

Reading is careful about what real exports contain: a byte-order mark at the start (Excel's UTF-8),
semicolons as separators (European settings), blank lines, spaces around values, a decimal comma.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from apps.finance.services import PostingError

# Our fields per object: (field, required). The order is the order of the documented layout.
FIELDS: dict[str, tuple[tuple[str, bool], ...]] = {
    "partners": (("external_id", True), ("name", True), ("role", True), ("vat_number", False),
                 ("registration_number", False), ("city", False), ("country", False),
                 ("payment_terms_days", False)),
    "materials": (("external_id", True), ("description", True), ("unit", True), ("type", False),
                  ("number", False), ("group", False), ("plant", False)),
    "stock": (("material", True), ("plant", True), ("quantity", True), ("unit_price", False),
              ("storage_location", False), ("as_of", False)),
    "purchase_orders": (("order", True), ("line", True), ("supplier", True), ("material", True),
                        ("quantity", True), ("price", True), ("order_date", True), ("delivery_date", False),
                        ("plant", False)),
    "sales_orders": (("order", True), ("line", True), ("customer", True), ("material", True),
                     ("quantity", True), ("price", True), ("order_date", True), ("requested_date", False),
                     ("plant", False)),
}
DATE_FIELDS = {"as_of", "order_date", "delivery_date", "requested_date"}
DECIMAL_FIELDS = {"quantity", "unit_price", "price"}


class FileError(PostingError):
    """The file as a whole cannot be read (wrong columns, not text); no row was loaded."""


@dataclass(frozen=True)
class Mapping:
    """One source system's export layout."""

    name: str
    title: str
    # object -> our field -> the source's column heading (missing: the same name as ours)
    columns: dict[str, dict[str, str]] = field(default_factory=dict)
    # our field -> the source's value -> ours (codes such as units, sites, roles)
    values: dict[str, dict[str, str]] = field(default_factory=dict)
    date_formats: tuple[str, ...] = ("%Y-%m-%d",)
    decimal_comma: bool = False

    def column(self, object_type: str, our_field: str) -> str:
        """The heading of the source column that holds one of our fields."""
        return self.columns.get(object_type, {}).get(our_field, our_field)

    def read(self, content: bytes, object_type: str) -> list[tuple[int, dict]]:
        """The file's rows as (row number in the file, our field -> value), raw values cleaned.

        Raises ``FileError`` when the file is not text or a required column is missing.
        """
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise FileError("The file is not UTF-8 text: export it as CSV (UTF-8).") from exc
        lines = text.splitlines()
        if not lines:
            raise FileError("The file is empty.")
        delimiter = max((";", ",", "\t"), key=lines[0].count)
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        headings = {(h or "").strip() for h in (reader.fieldnames or [])}
        missing = [self.column(object_type, f) for f, required in FIELDS[object_type]
                   if required and self.column(object_type, f) not in headings]
        if missing:
            raise FileError(f"Missing column(s) for {object_type}: {', '.join(missing)}.")
        rows = []
        for number, raw in enumerate(reader, start=2):  # row 1 is the headings
            cleaned = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k is not None}
            if not any(cleaned.values()):
                continue  # blank line
            row = {}
            for our_field, _required in FIELDS[object_type]:
                value = cleaned.get(self.column(object_type, our_field), "")
                row[our_field] = self.values.get(our_field, {}).get(value, value)
            rows.append((number, row))
        return rows

    def to_date(self, value: str) -> date | None:
        """A date in one of the mapping's formats; None when empty."""
        if not value:
            return None
        for fmt in self.date_formats:
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"'{value}' is not a date ({' or '.join(self.date_formats)})")

    def to_decimal(self, value: str) -> Decimal | None:
        """A decimal written the source's way; None when empty."""
        if not value:
            return None
        text = value.replace(" ", "")
        if self.decimal_comma:
            text = text.replace(".", "").replace(",", ".")  # 1.234,50 -> 1234.50
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"'{value}' is not a number") from exc


GENERIC = Mapping(name="generic", title="Generic CSV (docs/IMPORT_FORMAT.md)",
                  date_formats=("%Y-%m-%d", "%d/%m/%Y"))

MAPPINGS: dict[str, Mapping] = {GENERIC.name: GENERIC}


def mapping(name: str) -> Mapping:
    """The mapping of a source system, by name."""
    if name not in MAPPINGS:
        raise FileError(f"Unknown source '{name}': use one of {', '.join(sorted(MAPPINGS))}.")
    return MAPPINGS[name]
