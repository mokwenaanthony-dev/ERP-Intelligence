"""Wave 10a: the partner portal. Suppliers and customers plugged in, each seeing only their own documents.

The scenario, on the demo company, judged on 15 November 2026:

* Highveld Supplies CC (the demo supplier) has a released purchase order PO-A: 100 x TG-1001 and
  20 x TG-1002, agreed for 1 December; and an order still being entered (not visible to it yet);
* Second Supplier has its own released purchase order PO-B;
* Acme Retail (the demo customer) has a sales order for 10 x TG-1001 wanted on 10 November (late);
  Second Customer has an order of its own.
"""
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.urls import URLPattern, URLResolver, get_resolver

from apps.accounts.models import Role
from apps.control import services as control
from apps.masterdata.models import BusinessPartner, CustomerRole, Material, SupplierRole
from apps.organization.models import Plant, PurchasingOrganization, SalesOrganization
from apps.planning import mrp
from apps.portal import services
from apps.portal.models import DeliveryNotice, ForecastShare, OrderConfirmation, PartnerAccess
from apps.purchasing import services as purchasing
from apps.sales import services as sales
from apps.srm import services as srm
from tests.conftest import IdempotentAPIClient

pytestmark = pytest.mark.django_db
D = Decimal
API = "/api/portal"
ON = date(2026, 11, 15)
NOW = datetime(2026, 11, 15, 8, 0, tzinfo=dt_timezone.utc)


def partner(demo, name, *, supplier=True):
    """A second business partner with a supplier or customer role in company code 1000."""
    bp = BusinessPartner.objects.create(name=name, vat_number="4999999999" if supplier else "4888888888",
                                        city="Pretoria")
    role, account = (SupplierRole, "2000") if supplier else (CustomerRole, "1100")
    role.objects.create(business_partner=bp, company_code=demo.company, reconciliation_account=demo.account(account))
    return bp


def portal_client(user):
    client = IdempotentAPIClient()
    client.force_authenticate(user=user)
    client.user = user
    return client


@pytest.fixture
def net(demo, make_user):
    """The scenario above, with portal users highveld, second (suppliers) and acme, other (customers)."""
    plant = Plant.objects.get(code="1000")
    org = PurchasingOrganization.objects.get(code="1000")
    widget, gadget = Material.objects.get(number="TG-1001"), Material.objects.get(number="TG-1002")
    second, other = partner(demo, "Second Supplier"), partner(demo, "Second Customer", supplier=False)

    def po(supplier, release=True):
        order = purchasing.create_purchase_order(
            purchasing_organization=org, supplier=supplier, order_date=date(2026, 11, 1),
            delivery_date=date(2026, 12, 1), items=[purchasing.OrderItemInput(widget, D("100"), D("180"), plant),
                                                    purchasing.OrderItemInput(gadget, D("20"), D("300"), plant)])
        return purchasing.release_order(order) if release else order

    demo.po_a, demo.po_draft, demo.po_b = po(demo.supplier), po(demo.supplier, release=False), po(second)
    sorg = SalesOrganization.objects.get(code="1000")

    def so(customer):
        return sales.create_sales_order(sales_organization=sorg, customer=customer, order_date=date(2026, 11, 1),
                                        requested_delivery_date=date(2026, 11, 10),
                                        items=[sales.OrderItemInput(material=widget, quantity=D("10"), plant=plant)])

    demo.so_acme, demo.so_other = so(demo.customer), so(other)
    demo.second, demo.other = second, other
    users = {}
    for username, bp, kind in (("highveld", demo.supplier, "SUPPLIER"), ("second", second, "SUPPLIER"),
                               ("acme", demo.customer, "CUSTOMER"), ("other", other, "CUSTOMER")):
        user = make_user(Role.SUPPLIER if kind == "SUPPLIER" else Role.CUSTOMER, username=username)
        PartnerAccess.objects.create(user=user, company_code=demo.company, partner=bp, kind=kind)
        users[username] = portal_client(user)
    demo.clients = users
    return demo


# --- the wall: partners never reach the internal API ---------------------------------------------------------

def _internal_paths():
    """Every /api/ route outside the portal, auth and health, with sample values for its parameters."""
    found = []

    def walk(patterns, prefix):
        """Collect the routes under ``prefix`` (recursive through includes)."""
        for p in patterns:
            route = prefix + str(p.pattern)
            if isinstance(p, URLResolver):
                walk(p.url_patterns, route)
            elif isinstance(p, URLPattern):
                found.append(route)

    walk(get_resolver().url_patterns, "/")
    paths = set()
    for route in found:
        route = route.replace("^", "").replace("$", "").replace("\\Z", "").replace("\\.(?P<format>[a-z0-9]+)/?", "")
        if not route.startswith("/api/") or route.startswith(("/api/portal/", "/api/auth/", "/api/health/")):
            continue
        if "(?P<format>" in route:
            continue
        for marker in ("<str:number>", "<int:pk>", "<pk>", "<str:pk>", "(?P<pk>[^/.]+)", "<int:id>"):
            route = route.replace(marker, "1")
        if "<" in route or "(?P" in route:
            continue
        paths.add(route if route.endswith("/") else route + "/")
    return sorted(paths)


def test_partner_users_are_refused_by_every_internal_endpoint(net):
    paths = _internal_paths()
    assert len(paths) > 100  # the scan found the API (guards against checking nothing)
    for client in (net.clients["highveld"], net.clients["acme"]):
        for path in paths:
            for method in ("get", "post"):
                answer = getattr(client, method)(path, {}, format="json")
                assert answer.status_code in (403, 404, 405), (client.user.username, method, path, answer.status_code)


def test_the_default_permission_refuses_partners_too():
    from apps.accounts.permissions import InternalUser
    from types import SimpleNamespace

    request = SimpleNamespace(user=SimpleNamespace(is_authenticated=True, role=Role.SUPPLIER))
    assert InternalUser().has_permission(request, None) is False
    request.user.role = Role.VIEWER
    assert InternalUser().has_permission(request, None) is True


def test_internal_users_and_the_other_side_cannot_use_the_portal(net, client_for):
    assert client_for(Role.PURCHASING_OFFICER).get(f"{API}/purchase-orders/").status_code == 403
    assert client_for(Role.ADMIN).get(f"{API}/me/").status_code == 403  # an admin acts for no partner
    assert net.clients["acme"].get(f"{API}/purchase-orders/").status_code == 403
    assert net.clients["highveld"].get(f"{API}/sales-orders/").status_code == 403


def test_me_and_an_unlinked_partner_user(net, make_user):
    me = net.clients["highveld"].get(f"{API}/me/").json()
    assert (me["kind"], me["partner"], me["company_code"]) == ("SUPPLIER", net.supplier.number, "1000")
    lost = portal_client(make_user(Role.SUPPLIER, username="lost"))
    assert lost.get(f"{API}/me/").status_code == 404 and lost.get(f"{API}/purchase-orders/").status_code == 404


# --- suppliers: orders and confirmations ------------------------------------------------------------------------

def test_a_supplier_sees_its_released_orders_only(net):
    highveld = net.clients["highveld"]
    listed = highveld.get(f"{API}/purchase-orders/").json()
    assert [o["order_number"] for o in listed["purchase_orders"]] == [net.po_a.order_number]  # not the draft
    assert highveld.get(f"{API}/purchase-orders/{net.po_b.order_number}/").status_code == 404  # another supplier's
    assert highveld.get(f"{API}/purchase-orders/{net.po_draft.order_number}/").status_code == 404
    line = highveld.get(f"{API}/purchase-orders/{net.po_a.order_number}/").json()["items"][0]
    assert (line["material"], line["quantity"], line["open"], line["confirmed"]) == ("TG-1001", "100.000",
                                                                                     "100.000", None)


def test_confirming_is_kept_as_a_history_and_repeating_changes_nothing(net):
    highveld, number = net.clients["highveld"], net.po_a.order_number
    body = {"lines": [{"item_number": 10, "quantity": "100", "delivery_date": "2026-12-01"},
                      {"item_number": 20, "quantity": "20", "delivery_date": "2026-12-05"}], "note": "Two trucks"}
    first = highveld.post(f"{API}/purchase-orders/{number}/confirm/", body, format="json").json()
    assert first["lines_changed"] == 2 and first["items"][1]["confirmed"]["delivery_date"] == "2026-12-05"
    assert highveld.post(f"{API}/purchase-orders/{number}/confirm/", body, format="json").json()["lines_changed"] == 0
    later = {"lines": [{"item_number": 20, "quantity": "20", "delivery_date": "2026-12-08"}]}
    assert highveld.post(f"{API}/purchase-orders/{number}/confirm/", later, format="json").json()["lines_changed"] == 1
    assert OrderConfirmation.objects.count() == 3  # the history stays; the latest (8 December) counts
    for bad in ({"item_number": 10, "quantity": "101", "delivery_date": "2026-12-01"},
                {"item_number": 10, "quantity": "5", "delivery_date": "2026-10-01"},
                {"item_number": 30, "quantity": "5", "delivery_date": "2026-12-01"}):
        assert highveld.post(f"{API}/purchase-orders/{number}/confirm/", {"lines": [bad]},
                             format="json").status_code == 400
    assert net.clients["second"].post(f"{API}/purchase-orders/{number}/confirm/", body,
                                      format="json").status_code == 404


def test_mrp_plans_the_receipt_on_the_confirmed_date(net, make_user):
    plant = Plant.objects.get(code="1000")
    before = mrp.snapshot(plant, ON, 6)["materials"]["TG-1002"]["receipts"]
    assert before == [["2026-12-01", "60.000"]]  # 20 on each of PO-A, the draft and PO-B, summed per day
    access = services.access_for(net.clients["highveld"].user)
    services.confirm(access, net.po_a.order_number, [services.LineConfirmation(20, D("20"), date(2026, 12, 12))],
                     user=access.user, now=NOW)
    after = mrp.snapshot(plant, ON, 6)["materials"]["TG-1002"]["receipts"]
    assert after == [["2026-12-01", "40.000"], ["2026-12-12", "20.000"]]  # Highveld's 20 moved to its date


def test_the_control_tower_sees_a_late_confirmation(net):
    access = services.access_for(net.clients["highveld"].user)
    services.confirm(access, net.po_a.order_number, [services.LineConfirmation(20, D("20"), date(2026, 12, 5))],
                     user=access.user, now=NOW)
    late = [p for p in control.problems(net.company, on=ON, now=NOW) if p["key"].startswith("confirmed-late:")]
    assert [p["key"] for p in late] == [f"confirmed-late:{net.po_a.order_number}/20:20261205"]
    assert "4 day(s) after the agreed 2026-12-01" in late[0]["detail"] and late[0]["owner_role"] == \
        Role.PURCHASING_OFFICER
    assert late[0]["recommendation"]["action"].startswith("Accept the new date (MRP already plans with it)")


def test_overdue_advice_uses_what_the_supplier_said(net):
    access = services.access_for(net.clients["highveld"].user)
    on = date(2026, 12, 3)  # the agreed 1 December has passed
    key = f"late-receipt:{net.po_a.order_number}/10"

    def advice():
        """The recommendation for the overdue line 10 on 3 December."""
        return next(p for p in control.problems(net.company, on=on, now=NOW) if p["key"] == key)["recommendation"]

    assert advice()["action"].startswith("Ask Highveld Supplies CC for a confirmed date")
    services.confirm(access, net.po_a.order_number, [services.LineConfirmation(10, D("100"), date(2026, 12, 9))],
                     user=access.user, now=NOW)
    assert advice()["action"].startswith("Highveld Supplies CC confirmed 100.000 x TG-1001 for 2026-12-09")
    services.send_notice(access, reference="DN-77", order_number=net.po_a.order_number, ship_date=date(2026, 12, 3),
                         arrival_date=date(2026, 12, 4), items={10: D("100")}, user=access.user)
    assert advice()["action"].startswith("On its way: delivery notice AS00000001 (DN-77) arrives 2026-12-04")


# --- suppliers: delivery notices -------------------------------------------------------------------------------

def test_delivery_notices_are_idempotent_per_reference(net, client_for):
    highveld = net.clients["highveld"]
    body = {"reference": "DN-1001", "order_number": net.po_a.order_number, "ship_date": "2026-11-28",
            "arrival_date": "2026-11-30", "items": [{"item_number": 10, "quantity": "60"}]}
    first = highveld.post(f"{API}/delivery-notices/", body, format="json")
    assert first.status_code == 201 and first.json()["notice_number"] == "AS00000001"
    again = highveld.post(f"{API}/delivery-notices/", body, format="json")
    assert again.status_code == 200 and again.json()["notice_number"] == "AS00000001"
    changed = highveld.post(f"{API}/delivery-notices/", {**body, "arrival_date": "2026-12-02"}, format="json")
    assert changed.status_code == 409 and "use a new reference" in changed.json()["detail"]
    too_much = highveld.post(f"{API}/delivery-notices/", {**body, "reference": "DN-1002",
                                                          "items": [{"item_number": 10, "quantity": "101"}]},
                             format="json")
    assert too_much.status_code == 400 and DeliveryNotice.objects.count() == 1
    assert net.clients["second"].get(f"{API}/delivery-notices/").json()["count"] == 0
    warehouse = client_for(Role.INVENTORY_MANAGER).get(f"{API}/network/delivery-notices/?company_code=1000").json()
    assert warehouse["delivery_notices"][0]["reference"] == "DN-1001"


# --- suppliers: quotes --------------------------------------------------------------------------------------------

def test_a_supplier_quotes_in_person_and_never_sees_another_quote(net, make_user):
    rfq = srm.create_rfq(purchasing_organization=PurchasingOrganization.objects.get(code="1000"), title="Widgets 2027",
                         deadline=date(2026, 11, 30), items=[srm.RFQItemInput(Material.objects.get(number="TG-1001"),
                                                                              D("500"))],
                         suppliers=[net.supplier, net.second])
    srm.send_rfq(rfq)
    highveld, second = net.clients["highveld"], net.clients["second"]
    answer = highveld.post(f"{API}/rfqs/{rfq.rfq_number}/quote/", {"prices": {"TG-1001": "175.50"},
                                                                   "delivery_days": 7, "reference": "Q-9"},
                           format="json")
    assert answer.status_code == 200 and answer.json()["your_quote"]["status"] == "SUBMITTED"
    assert answer.json()["items"][0]["your_price"] == "175.50"
    seen_by_second = second.get(f"{API}/rfqs/").json()["rfqs"][0]
    assert seen_by_second["items"][0]["your_price"] is None  # its own (empty) quote, never Highveld's
    unknown = highveld.post(f"{API}/rfqs/{rfq.rfq_number}/quote/", {"prices": {"TG-9999": "1"}, "delivery_days": 7},
                            format="json")
    assert unknown.status_code == 400
    outsider = partner(net, "Third Supplier")
    third = make_user(Role.SUPPLIER, username="third")
    PartnerAccess.objects.create(user=third, company_code=net.company, partner=outsider, kind="SUPPLIER")
    not_invited = portal_client(third).post(f"{API}/rfqs/{rfq.rfq_number}/quote/",
                                            {"prices": {"TG-1001": "1"}, "delivery_days": 1}, format="json")
    assert not_invited.status_code == 404


# --- suppliers: the shared outlook -------------------------------------------------------------------------------

def test_the_outlook_is_shared_only_by_agreement(net, client_for):
    highveld = net.clients["highveld"]
    assert highveld.get(f"{API}/outlook/").status_code == 403
    buyer = client_for(Role.PURCHASING_OFFICER)
    body = {"company_code": "1000", "supplier": net.supplier.number, "active": True}
    assert buyer.post(f"{API}/network/forecast-shares/", body, format="json").json()["changed"] is True
    assert buyer.post(f"{API}/network/forecast-shares/", body, format="json").json()["changed"] is False
    assert client_for(Role.VIEWER).post(f"{API}/network/forecast-shares/", body, format="json").status_code == 403
    outlook = highveld.get(f"{API}/outlook/?months=3").json()
    widget = next(m for m in outlook["materials"] if m["material"] == "TG-1001")
    assert len(widget["months"]) == 3  # this month and the next two (dates are today's, not the scenario's)
    assert sum(D(cell["ordered"]) for cell in widget["months"].values()) == D("100.000")
    assert "not an order" in outlook["note"]
    assert net.clients["second"].get(f"{API}/outlook/").status_code == 403  # agreed with Highveld only
    assert buyer.post(f"{API}/network/forecast-shares/", {**body, "active": False}, format="json").json()["active"] \
        is False
    assert highveld.get(f"{API}/outlook/").status_code == 403
    assert ForecastShare.objects.get().ended_at is not None


# --- customers ---------------------------------------------------------------------------------------------------

def test_a_customer_sees_its_orders_and_whether_they_are_late(net):
    acme = net.clients["acme"]
    orders = acme.get(f"{API}/sales-orders/").json()
    assert [o["order_number"] for o in orders["sales_orders"]] == [net.so_acme.order_number]
    order = orders["sales_orders"][0]
    assert order["late"] is True and order["items"][0]["open"] == "10.000"  # wanted 10 November, not delivered
    assert acme.get(f"{API}/sales-orders/{net.so_other.order_number}/").status_code == 404
    assert net.clients["other"].get(f"{API}/sales-orders/{net.so_acme.order_number}/").status_code == 404


# --- commands -------------------------------------------------------------------------------------------------

def test_create_partner_user(net):
    from django.contrib.auth import get_user_model

    out = StringIO()
    call_command("create_partner_user", "--company", "1000", "--partner", net.second.number, "--username", "sam",
                 stdout=out)
    assert "Created: sam" in out.getvalue() and "changepassword sam" in out.getvalue()
    sam = get_user_model().objects.get(username="sam")
    assert sam.role == Role.SUPPLIER and not sam.has_usable_password()  # no password on a command line
    call_command("create_partner_user", "--company", "1000", "--partner", net.second.number, "--username", "sam",
                 stdout=out)
    assert "Already linked (unchanged)" in out.getvalue()
    with pytest.raises(CommandError, match="already linked"):
        call_command("create_partner_user", "--company", "1000", "--partner", net.supplier.number, "--username",
                     "sam", stdout=out)
    with pytest.raises(CommandError, match="not a customer"):
        call_command("create_partner_user", "--company", "1000", "--partner", net.second.number, "--username", "x",
                     "--kind", "CUSTOMER", stdout=out)


def test_setup_demo_portal_is_idempotent(demo, make_user):
    make_user(Role.PURCHASING_OFFICER, username="purchasing_officer")
    out = StringIO()
    call_command("setup_demo_portal", stdout=out)
    call_command("setup_demo_portal", stdout=out)
    text = out.getvalue()
    assert "Created portal user highveld" in text and "Kept portal user highveld" in text
    assert "already shared" in text and PartnerAccess.objects.count() == 2
