"""Play the partner portal's demo against a running ERP (Wave 10a): a supplier and a customer, end to end.

Run from the project folder with the Docker stack up and the demo data seeded (``seed_data``, or
``setup_demo_portal`` on its own):

    python scripts/demo_portal.py
    python scripts/demo_portal.py --base-url http://localhost:8000

What it does, checking each answer (it stops with exit code 1 at the first one that is wrong):

1. signs in as ``highveld`` (a person at Highveld Supplies CC, a supplier) and checks the role wall:
   internal master data must answer 403;
2. finds Highveld's first open purchase order line and confirms it a week later than agreed,
   twice: the second time must change nothing (idempotent);
3. announces the delivery with a delivery notice, twice: the second time must return the same notice;
4. reads the demand outlook purchasing shares with Highveld;
5. signs in as ``purchasing_officer`` and finds the "confirmed later than agreed" problem in the
   control tower, and the delivery notice in the warehouse's list;
6. signs in as ``acme`` (a customer) and shows its orders and which are late; the wall holds for it too.

Running it again repeats the same steps with the same answers: every write is idempotent.
The demo password is read from DEMO_PASSWORD (default: the seed users' local demo password); it is
never printed. Uses only the standard library, and never a proxy (the ERP is local).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta

DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "ChangeMe123!")  # local demo data only, like seed_data
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # straight to the local ERP


class DemoFailed(Exception):
    """An answer was not what the demo expects."""


class Client:
    """A signed-in user of the ERP's API (JWT), sending an Idempotency-Key with every change (ADR 0010)."""

    def __init__(self, base_url: str, username: str):
        self.base, self.username = base_url.rstrip("/"), username
        status, body = self._send("POST", "/api/auth/token/", {"username": username, "password": DEMO_PASSWORD},
                                  token=None)
        if status != 200:
            raise DemoFailed(f"Could not sign in as {username} ({status}). Has seed_data run in this database?")
        self.token = body["access"]

    def _send(self, method: str, path: str, body=None, *, token="self"):
        """Send one request; returns (status, decoded JSON or None)."""
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {self.token if token == 'self' else token}"
        if method != "GET":
            headers["Idempotency-Key"] = str(uuid.uuid4())
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with OPENER.open(request, timeout=30) as answer:
                raw = answer.read()
                return answer.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw) if raw else None
            except ValueError:
                return error.code, None

    def get(self, path: str):
        """GET a path; returns (status, body)."""
        return self._send("GET", path)

    def post(self, path: str, body: dict):
        """POST a JSON body; returns (status, body)."""
        return self._send("POST", path, body)


def expect(status: int, wanted, what: str) -> None:
    """Stop the demo when a status is not one of those wanted."""
    wanted = wanted if isinstance(wanted, tuple) else (wanted,)
    if status not in wanted:
        raise DemoFailed(f"{what}: expected {' or '.join(map(str, wanted))}, got {status}")


def say(text: str) -> None:
    """Print one line of the demo's story."""
    print(text, flush=True)


def supplier_part(base: str) -> dict:
    """Steps 1 to 4, as Highveld. Returns what the later steps check (order, line, reference)."""
    highveld = Client(base, "highveld")
    status, me = highveld.get("/api/portal/me/")
    expect(status, 200, "who am I")
    say(f"1. Signed in as highveld: {me['name']} ({me['partner']}), a {me['kind'].lower()}")
    status, _ = highveld.get("/api/masterdata/business-partners/")
    expect(status, 403, "the role wall (internal master data)")
    say("   The wall holds: internal master data answers 403 to a supplier")

    status, body = highveld.get("/api/portal/purchase-orders/")
    expect(status, 200, "purchase orders")
    line = next(((o, i) for o in body["purchase_orders"] if o["status"] == "RELEASED"
                 for i in o["items"] if float(i["open"]) > 0), None)
    if line is None:
        raise DemoFailed("Highveld has no open purchase order line: run manage.py setup_demo_portal")
    order, item = line
    agreed = date.fromisoformat(order["delivery_date"]) if order["delivery_date"] else date.today()
    later = agreed + timedelta(days=7)
    confirm = {"lines": [{"item_number": item["item_number"], "quantity": item["open"],
                          "delivery_date": later.isoformat()}], "note": "Demo: one week later"}
    path = f"/api/portal/purchase-orders/{order['order_number']}/confirm/"
    status, first = highveld.post(path, confirm)
    expect(status, 200, "confirm")
    status, again = highveld.post(path, confirm)
    expect(status, 200, "confirm again")
    if again["lines_changed"] != 0:
        raise DemoFailed("The same confirmation again changed a line: it should change nothing")
    say(f"2. Confirmed {order['order_number']}/{item['item_number']}: {item['open']} x {item['material']} for "
        f"{later} (agreed {agreed}); lines changed {first['lines_changed']}, then {again['lines_changed']} the "
        "second time")

    reference = f"DEMO-{order['order_number']}-{item['item_number']}"
    notice = {"reference": reference, "order_number": order["order_number"],
              "ship_date": (later - timedelta(days=1)).isoformat(), "arrival_date": later.isoformat(),
              "items": [{"item_number": item["item_number"], "quantity": item["open"]}]}
    status, sent = highveld.post("/api/portal/delivery-notices/", notice)
    expect(status, (200, 201), "delivery notice")
    status, resent = highveld.post("/api/portal/delivery-notices/", notice)
    expect(status, 200, "the same delivery notice again")
    if resent["notice_number"] != sent["notice_number"]:
        raise DemoFailed("The same delivery notice again made a second notice")
    say(f"3. Delivery notice {sent['notice_number']} ({reference}) arrives {later}; sent again: the same notice")

    status, outlook = highveld.get("/api/portal/outlook/?months=3")
    expect(status, (200, 403), "outlook")
    if status == 403:
        say("4. Demand outlook: not shared with Highveld (purchasing has not agreed)")
    else:
        say(f"4. Demand outlook for the next 3 months ({outlook['note']})")
        for material in outlook["materials"]:
            cells = ", ".join(f"{month}: ordered {cell['ordered']}, planned {cell['planned']}"
                              for month, cell in material["months"].items())
            say(f"   {material['material']:<8} {cells}")
    return {"order": order["order_number"], "item": item["item_number"], "reference": reference,
            "late": later > agreed}


def company_part(base: str, found: dict) -> None:
    """Step 5, as purchasing: what Highveld said shows up inside the company."""
    buyer = Client(base, "purchasing_officer")
    status, inbox = buyer.get("/api/control/problems/?company_code=1000&area=purchasing")
    expect(status, 200, "problems inbox")
    document = f"{found['order']}/{found['item']}"
    seen = [p for p in inbox["problems"] if document in p["title"]]
    if not seen:
        raise DemoFailed(f"No problem about {document} in the purchasing inbox")
    problem = seen[0]
    say(f"5. Control tower (purchasing): {problem['title']}")
    say(f"   {problem['detail']}")
    say(f"   Recommend: {problem['recommendation']['action']}")
    status, notices = buyer.get("/api/portal/network/delivery-notices/?company_code=1000")
    expect(status, 200, "delivery notices for receiving")
    if not any(n["reference"] == found["reference"] for n in notices["delivery_notices"]):
        raise DemoFailed("The warehouse does not see the delivery notice")
    say(f"   The warehouse sees delivery notice {found['reference']} in its list")


def customer_part(base: str) -> None:
    """Step 6, as Acme: a customer's view of its own orders."""
    acme = Client(base, "acme")
    status, body = acme.get("/api/portal/sales-orders/")
    expect(status, 200, "sales orders")
    late = [o["order_number"] for o in body["sales_orders"] if o["late"]]
    listed = f" ({', '.join(late[:5])})" if late else ""
    say(f"6. Signed in as acme: {body['count']} order(s), {len(late)} late{listed}")
    status, _ = acme.get("/api/portal/purchase-orders/")
    expect(status, 403, "a customer asking for purchase orders")
    status, _ = acme.get("/api/control/kpis/?company_code=1000")
    expect(status, 403, "the role wall for a customer")
    say("   The wall holds for customers too: supplier pages and the control tower answer 403")


def main(argv: list[str] | None = None) -> int:
    """Run the demo; 0 when every answer was as expected, 1 otherwise."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    base = parser.parse_args(argv).base_url
    try:
        found = supplier_part(base)
        company_part(base, found)
        customer_part(base)
    except DemoFailed as failure:
        say(f"FAILED: {failure}")
        return 1
    except urllib.error.URLError as error:
        say(f"FAILED: the ERP at {base} did not answer ({error.reason}). Is the Docker stack up?")
        return 1
    say("Done: every answer was as expected. Run it again: the same steps give the same answers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
