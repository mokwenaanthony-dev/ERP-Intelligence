"""Loading another ERP's data so the intelligence layer can run beside it (Wave 10b, ADR 0020).

The company keeps its ERP (Sage X3, SAP, anything that exports a report). Its master data and open
documents are loaded here as files, through the same service functions the API uses, so every
business rule applies to imported data exactly as to data entered by hand: a partner needs a role,
a purchase order needs a supplier of the company code, stock goes in through goods movements with
their journal entries. A rule broken by a row rejects that row, with the rule's own message.

**Idempotent at two levels** (ADR 0010):

* the same file again returns the first batch and changes nothing (its fingerprint); if that load
  rejected rows, only those rows are tried again (v0.22.1): their cause may have been fixed since;
* every row is matched to what was loaded before by the source's own key (``ExternalReference``):
  unchanged rows stay unchanged, a changed partner or material is updated, a document already
  loaded is not created twice. Stock is a snapshot: only the difference from what we hold is
  posted, so the same snapshot again posts nothing.

Each row (each order, for documents) is loaded in its own transaction: one bad row is rejected with
its reason and the rest still loads. Load in the order of ``ObjectType``: orders refer to partners
and materials by the source's keys, and are refused until those keys are known.

What is not done: changing an order already loaded (the row is refused, saying so), posting goods
receipts, deliveries or invoices of the other ERP (the books stay its job), deleting anything.
Logs carry counts, numbers and keys, never names or row contents.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.crm.services import _receivables_account
from apps.finance.services import PostingError
from apps.inventory import services as inventory
from apps.inventory.models import MovementType
from apps.masterdata.models import BusinessPartner, CustomerRole, Material, MaterialPlant, SupplierRole, UnitOfMeasure
from apps.organization.models import Plant, PurchasingOrganization, SalesOrganization, StorageLocation
from apps.purchasing import services as purchasing
from apps.sales import services as sales
from apps.srm.services import _payables_account

from .mappings import FileError, Mapping, mapping
from .models import ExternalReference, ImportBatch, ObjectType

logger = logging.getLogger(__name__)

ROLES = {"CUSTOMER", "SUPPLIER", "BOTH"}


class RowError(PostingError):
    """One row breaks a rule; it is rejected with this message and the others still load."""


@dataclass
class Result:
    """What happened to one row (or one order)."""

    row: int
    key: str
    status: str  # created, updated, unchanged, rejected
    number: str = ""
    detail: str = ""


def _fingerprint(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class Loader:
    """Loads one file's rows for one company code from one source."""

    def __init__(self, company_code, source: Mapping, batch: ImportBatch, user, on: date):
        self.company, self.source, self.batch, self.user, self.on = company_code, source, batch, user, on

    # --- references -------------------------------------------------------------------------------------

    def ref(self, object_type: str, key: str) -> ExternalReference | None:
        """What was loaded before under the source's key, if anything."""
        return ExternalReference.objects.filter(company_code=self.company, source=self.source.name,
                                                object_type=object_type, external_key=key).first()

    def remember(self, object_type: str, key: str, number: str, fingerprint: str, existing=None):
        """Record (or refresh) the link between the source's key and our number."""
        if existing:
            existing.number, existing.row_fingerprint, existing.updated_at = number, fingerprint, timezone.now()
            existing.save(update_fields=["number", "row_fingerprint", "updated_at"])
            return existing
        return ExternalReference.objects.create(company_code=self.company, source=self.source.name,
                                                object_type=object_type, external_key=key, number=number,
                                                row_fingerprint=fingerprint, first_batch=self.batch)

    def partner(self, key: str, role: str) -> BusinessPartner:
        """A business partner loaded earlier from this source, which must have the role asked for."""
        ref = self.ref(ObjectType.PARTNERS, key)
        if ref is None:
            raise RowError(f"No partner '{key}' has been loaded from {self.source.name}: load partners first.")
        partner = BusinessPartner.objects.get(number=ref.number)
        roles = SupplierRole if role == "SUPPLIER" else CustomerRole
        if not roles.objects.filter(business_partner=partner, company_code=self.company).exists():
            raise RowError(f"Partner '{key}' ({partner.number}) is not a {role.lower()} in {self.company.code}.")
        return partner

    def material(self, key: str) -> Material:
        """A material loaded earlier from this source."""
        ref = self.ref(ObjectType.MATERIALS, key)
        if ref is None:
            raise RowError(f"No material '{key}' has been loaded from {self.source.name}: load materials first.")
        return Material.objects.get(number=ref.number)

    def plant(self, code: str) -> Plant:
        """One of the company code's plants (the first when none is given)."""
        plants = Plant.objects.filter(company_code=self.company).order_by("code")
        found = plants.filter(code=code).first() if code else plants.first()
        if found is None:
            raise RowError(f"Plant '{code}' is not a plant of company code {self.company.code}.")
        return found

    def decimal(self, value: str, name: str, *, positive=True) -> Decimal:
        """A required number from a row."""
        try:
            number = self.source.to_decimal(value)
        except ValueError as exc:
            raise RowError(f"{name}: {exc}.") from exc
        if number is None:
            raise RowError(f"{name} is required.")
        if positive and number <= 0:
            raise RowError(f"{name} must be greater than zero.")
        return number

    def day(self, value: str, name: str, *, required=False) -> date | None:
        """A date from a row."""
        try:
            found = self.source.to_date(value)
        except ValueError as exc:
            raise RowError(f"{name}: {exc}.") from exc
        if required and found is None:
            raise RowError(f"{name} is required.")
        return found

    # --- partners and materials ---------------------------------------------------------------------------

    def load_partner(self, row: dict) -> tuple[str, str, str]:
        """Create, update or keep one business partner with its roles; returns (status, number, detail)."""
        key, role = row["external_id"], row["role"].upper()
        if not row["name"]:
            raise RowError("name is required.")
        if role not in ROLES:
            raise RowError(f"role must be CUSTOMER, SUPPLIER or BOTH, not '{row['role']}'.")
        country = (row["country"] or "ZA").upper()
        if len(country) != 2:
            raise RowError(f"country must be a two-letter code, not '{row['country']}'.")
        terms = int(row["payment_terms_days"]) if row["payment_terms_days"].isdigit() else 30
        fingerprint = _fingerprint(row)
        existing = self.ref(ObjectType.PARTNERS, key)
        detail = ""
        if existing:
            partner = BusinessPartner.objects.get(number=existing.number)
            if existing.row_fingerprint == fingerprint:
                return "unchanged", partner.number, ""
            status = "updated"
        else:
            partner = (BusinessPartner.objects.filter(vat_number=row["vat_number"]).first()
                       if row["vat_number"] else None)
            if partner:
                status, detail = "updated", f"linked to existing partner {partner.number} by VAT number"
            else:
                partner, status = BusinessPartner(created_by=self.user), "created"
        partner.name, partner.city, partner.country = row["name"], row["city"] or partner.city, country
        partner.vat_number = row["vat_number"] or partner.vat_number
        partner.registration_number = row["registration_number"] or partner.registration_number
        partner.updated_by = self.user
        partner.save()
        if role in ("CUSTOMER", "BOTH"):
            CustomerRole.objects.get_or_create(business_partner=partner, company_code=self.company, defaults={
                "reconciliation_account": _receivables_account(self.company), "payment_terms_days": terms,
                "created_by": self.user, "updated_by": self.user})
        if role in ("SUPPLIER", "BOTH"):
            SupplierRole.objects.get_or_create(business_partner=partner, company_code=self.company, defaults={
                "reconciliation_account": _payables_account(self.company), "payment_terms_days": terms,
                "created_by": self.user, "updated_by": self.user})
        self.remember(ObjectType.PARTNERS, key, partner.number, fingerprint, existing)
        return status, partner.number, detail

    def load_material(self, row: dict) -> tuple[str, str, str]:
        """Create, update or keep one material (and its plant view); returns (status, number, detail)."""
        key = row["external_id"]
        number = (row["number"] or key).upper()
        if len(number) > 18:
            raise RowError(f"The material number '{number}' is longer than 18 characters: give a shorter number.")
        kind = (row["type"] or "HAWA").upper()
        if kind not in Material.MaterialType.values:
            raise RowError(f"type must be one of {', '.join(Material.MaterialType.values)}, not '{row['type']}'.")
        unit = UnitOfMeasure.objects.filter(code=row["unit"].upper()).first()
        if unit is None:
            raise RowError(f"Unit '{row['unit']}' is not known: map it to one of "
                           f"{', '.join(UnitOfMeasure.objects.order_by('code').values_list('code', flat=True))}.")
        plant = self.plant(row["plant"]) if row["plant"] else None
        fingerprint = _fingerprint(row)
        existing = self.ref(ObjectType.MATERIALS, key)
        detail = ""
        if existing:
            material = Material.objects.get(number=existing.number)
            if existing.row_fingerprint == fingerprint:
                return "unchanged", material.number, ""
            status = "updated"
        else:
            material = Material.objects.filter(number=number).first()
            if material:
                status, detail = "updated", f"linked to existing material {material.number}"
            else:
                material, status = Material(number=number, created_by=self.user), "created"
        material.description, material.base_unit = row["description"], unit
        material.material_type, material.material_group = kind, row["group"] or material.material_group
        material.updated_by = self.user
        material.save()
        if plant:
            MaterialPlant.objects.get_or_create(material=material, plant=plant,
                                                defaults={"created_by": self.user, "updated_by": self.user})
        self.remember(ObjectType.MATERIALS, key, material.number, fingerprint, existing)
        return status, material.number, detail

    # --- stock --------------------------------------------------------------------------------------------

    def load_stock(self, row: dict) -> tuple[str, str, str]:
        """Bring our stock to the source's snapshot by posting only the difference."""
        material = self.material(row["material"])
        plant = self.plant(row["plant"])
        locations = StorageLocation.objects.filter(plant=plant).order_by("code")
        code = row["storage_location"]
        location = locations.filter(code=code).first() if code else locations.first()
        if location is None:
            raise RowError(f"Storage location '{row['storage_location']}' is not in plant {plant.code}.")
        wanted = self.decimal(row["quantity"], "quantity", positive=False)
        if wanted < 0:
            raise RowError("quantity cannot be negative.")
        price = self.source.to_decimal(row["unit_price"]) if row["unit_price"] else None
        as_of = self.day(row["as_of"], "as_of") or self.on
        held = inventory.location_stock(material, location)
        difference = (wanted - held).quantize(Decimal("0.001"))
        if difference == 0:
            return "unchanged", material.number, f"{held} in stock at {plant.code}/{location.code}"
        plant_quantity, _value = inventory.plant_stock(material, plant)
        if difference > 0 and plant_quantity == 0:
            if price is None:
                raise RowError("unit_price is required for the first stock of a material at a plant (initial entry).")
            movement = MovementType.INITIAL_ENTRY
        else:
            movement = MovementType.INVENTORY_GAIN if difference > 0 else MovementType.INVENTORY_LOSS
        document = inventory.post_goods_movement(
            movement_type=movement, plant=plant, posting_date=as_of, user=self.user,
            header_text=f"Stock snapshot from {self.source.name}",
            items=[inventory.ItemInput(material=material, storage_location=location, quantity=abs(difference),
                                       unit_price=price if movement != MovementType.INVENTORY_LOSS else None)])
        detail = f"{held} -> {wanted} at {plant.code}/{location.code} (movement {movement.value})"
        return "updated", document.document_number, detail

    # --- open orders -----------------------------------------------------------------------------------------

    def load_order(self, object_type: str, key: str, lines: list[dict]) -> tuple[str, str, str]:
        """Create one open purchase or sales order from its lines; the same order again is unchanged."""
        lines = sorted(lines, key=lambda r: (r["line"].zfill(10), r["material"]))
        fingerprint = _fingerprint(lines)
        existing = self.ref(object_type, key)
        if existing:
            if existing.row_fingerprint == fingerprint:
                return "unchanged", existing.number, ""
            raise RowError(f"Order {key} changed in {self.source.name} after it was loaded as {existing.number}; "
                           "changing loaded orders is not supported yet: change it here or cancel and reload.")
        first = lines[0]
        buying = object_type == ObjectType.PURCHASE_ORDERS
        partner_field = "supplier" if buying else "customer"
        if len({line[partner_field] for line in lines}) > 1 or len({line["order_date"] for line in lines}) > 1:
            raise RowError(f"All lines of order {key} need the same {partner_field} and order date.")
        partner = self.partner(first[partner_field], "SUPPLIER" if buying else "CUSTOMER")
        order_date = self.day(first["order_date"], "order_date", required=True)
        if buying:
            items = [purchasing.OrderItemInput(material=self.material(line["material"]),
                                               quantity=self.decimal(line["quantity"], "quantity"),
                                               net_price=self.decimal(line["price"], "price"),
                                               plant=self.plant(line["plant"])) for line in lines]
            org = PurchasingOrganization.objects.filter(company_code=self.company).order_by("code").first()
            order = purchasing.create_purchase_order(
                purchasing_organization=org, supplier=partner, order_date=order_date, items=items,
                delivery_date=self.day(first["delivery_date"], "delivery_date"), supplier_reference=key,
                user=self.user)
            purchasing.release_order(order)
        else:
            items = [sales.OrderItemInput(material=self.material(line["material"]),
                                          quantity=self.decimal(line["quantity"], "quantity"),
                                          net_price=self.decimal(line["price"], "price"),
                                          plant=self.plant(line["plant"])) for line in lines]
            org = SalesOrganization.objects.filter(company_code=self.company).order_by("code").first()
            order = sales.create_sales_order(
                sales_organization=org, customer=partner, order_date=order_date, items=items,
                requested_delivery_date=self.day(first["requested_date"], "requested_date"), customer_reference=key,
                user=self.user)
        self.remember(object_type, key, order.order_number, fingerprint)
        return "created", order.order_number, f"{len(lines)} line(s)"


def _run(loader: Loader, object_type: str, rows: list[tuple[int, dict]], only: set[str] | None = None) -> list[Result]:
    """Load every row (every order) in its own transaction; a broken rule rejects only that row.

    With ``only``, just the rows (orders) with those keys are loaded: a retry of rejected rows.
    """
    results: list[Result] = []

    def attempt(row_number: int, key: str, step):
        """Run one row's step; record its result, or its rejection with the rule's message."""
        try:
            with transaction.atomic():
                status, number, detail = step()
            results.append(Result(row_number, key, status, number, detail))
        except PostingError as exc:  # a business rule of the import or of the module it calls
            results.append(Result(row_number, key, "rejected", "", str(exc)))
        except IntegrityError:
            results.append(Result(row_number, key, "rejected", "", "Conflicts with a record that already exists."))

    if object_type in (ObjectType.PURCHASE_ORDERS, ObjectType.SALES_ORDERS):
        orders: dict[str, list] = defaultdict(list)
        first_row: dict[str, int] = {}
        for number, row in rows:
            if not row["order"]:
                results.append(Result(number, "", "rejected", "", "order is required."))
                continue
            orders[row["order"]].append(row)
            first_row.setdefault(row["order"], number)
        for key, lines in orders.items():
            if only is None or key in only:
                attempt(first_row[key], key, lambda k=key, ls=lines: loader.load_order(object_type, k, ls))
    else:
        step = {ObjectType.PARTNERS: loader.load_partner, ObjectType.MATERIALS: loader.load_material,
                ObjectType.STOCK: loader.load_stock}[object_type]
        key_field = "material" if object_type == ObjectType.STOCK else "external_id"
        for number, row in rows:
            key = row[key_field] + (f"@{row['plant']}" if object_type == ObjectType.STOCK else "")
            if not row[key_field]:
                results.append(Result(number, "", "rejected", "", f"{key_field} is required."))
                continue
            if only is None or key in only:
                attempt(number, key, lambda r=row: step(r))
    return sorted(results, key=lambda r: r.row)


def _count(batch: ImportBatch, results: list[Result]) -> None:
    for status in ("created", "updated", "unchanged", "rejected"):
        setattr(batch, status, sum(r.status == status for r in results))
    batch.results = [r.__dict__ for r in sorted(results, key=lambda r: r.row)]


def _retry(batch: ImportBatch, layout: Mapping, content: bytes, user, on: date) -> ImportBatch:
    """The same file again, when its earlier load rejected rows: try those rows once more (v0.22.1).

    A rejected row may load now: the partner it needed was loaded since, a period was opened. Rows
    that loaded before are not touched (no second stock posting, no second order); a rejected row
    with no key cannot load and stays as it was. The batch keeps one result per row, the latest.
    """
    with transaction.atomic():
        batch = ImportBatch.objects.select_for_update().get(pk=batch.pk)  # one retry at a time
        earlier = [Result(**r) for r in batch.results]
        keys = {r.key for r in earlier if r.status == "rejected" and r.key}
        if not keys:
            return batch
        rows = layout.read(content, batch.object_type)
        retried = _run(Loader(batch.company_code, layout, batch, user, on), batch.object_type, rows, only=keys)
        by_key = {r.key: r for r in retried}
        merged = [by_key.get(r.key, r) if r.status == "rejected" else r for r in earlier]
        _count(batch, merged)
        batch.save()
    batch.retried = len(retried)
    batch.now_loaded = sum(r.status != "rejected" for r in retried)
    logger.info("Import of %s %s for %s repeated: %d rejected row(s) retried, %d now loaded", batch.source,
                batch.object_type, batch.company_code.code, batch.retried, batch.now_loaded)
    return batch


def import_file(company_code, *, source: str, object_type: str, content: bytes, user, file_name: str = "",
                on: date | None = None) -> tuple[ImportBatch, bool]:
    """Load one exported file; returns (batch, created).

    The same file again returns the first batch. If that load rejected rows, those rows (and only
    those) are tried again, and the batch shows ``retried`` and ``now_loaded``.
    """
    if object_type not in ObjectType.values:
        raise FileError(f"Unknown object '{object_type}': use one of {', '.join(ObjectType.values)}.")
    layout = mapping(source)
    fingerprint = _fingerprint(company_code.code, source, object_type, hashlib.sha256(content).hexdigest())
    existing = ImportBatch.objects.filter(fingerprint=fingerprint).select_related("company_code").first()
    if existing:
        if existing.rejected:
            return _retry(existing, layout, content, user, on or timezone.localdate()), False
        logger.info("Import of %s %s for %s repeated: returning batch %s", source, object_type, company_code.code,
                    existing.pk)
        return existing, False
    rows = layout.read(content, object_type)
    try:
        batch = ImportBatch.objects.create(company_code=company_code, source=source, object_type=object_type,
                                           file_name=file_name[:200], fingerprint=fingerprint, rows=len(rows),
                                           imported_by=user)
    except IntegrityError:  # the same file at the same moment: the first one is the batch
        return ImportBatch.objects.get(fingerprint=fingerprint), False
    results = _run(Loader(company_code, layout, batch, user, on or timezone.localdate()), object_type, rows)
    _count(batch, results)
    batch.save()
    logger.info("Imported %s %s for %s: %d row(s), %d created, %d updated, %d unchanged, %d rejected",
                source, object_type, company_code.code, batch.rows, batch.created, batch.updated, batch.unchanged,
                batch.rejected)
    return batch, True


def batch_view(batch: ImportBatch) -> dict:
    """An import batch as the API shows it."""
    return {"id": batch.pk, "company_code": batch.company_code.code, "source": batch.source,
            "object": batch.object_type, "file_name": batch.file_name, "rows": batch.rows,
            "created": batch.created, "updated": batch.updated, "unchanged": batch.unchanged,
            "rejected": batch.rejected, "imported_by": batch.imported_by.username, "imported_at": batch.imported_at,
            "retried": getattr(batch, "retried", 0), "now_loaded": getattr(batch, "now_loaded", 0),
            "results": batch.results}
