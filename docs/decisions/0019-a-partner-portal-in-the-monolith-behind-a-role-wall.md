# 0019. A partner portal in the monolith, behind a role wall; partners inform, the company decides

- **Status:** Accepted, 2026-11-18 (Wave 10a)

## Context

The north star ([`docs/VISION.md`](../VISION.md)) asks that "everything and everyone is plugged into
the network", including a digital supply chain connected to suppliers and buyers. After Wave 9 the
ERP knew a great deal about its suppliers and customers, but they could tell it nothing directly.
Order confirmations and delivery dates arrived by phone or e-mail and were typed in, or never typed
in at all. Customers had to ask where their orders were.

Letting people from outside the company log in changes the risk. Several internal areas let *any*
signed-in user read: master data, organisation, production master data and the knowledge graph.
So did the project-wide default permission, "IsAuthenticated". A supplier login would have seen
every business partner and the whole graph.

## Decision

- **Two new roles, SUPPLIER and CUSTOMER**, each linked to one business partner in one company code
  (`PartnerAccess`). A login without that link can do nothing.
- **A wall at the centre, not in each view.** `RolePermission`, the class every module's permission
  is built from, refuses the partner roles before it looks at anything else. That includes the
  "every signed-in user may read" areas. The project default becomes `InternalUser` (signed in *and*
  not a partner role), so a view that forgets to name its permission is still closed. A test walks
  every `/api/` route outside the portal and checks that a supplier and a customer are refused,
  for GET and for POST.
- **The portal is its own app (`/api/portal/`)** in the modular monolith (ADR 0001). Every query
  starts from the user's partner. Another partner's document answers 404, exactly like one that
  does not exist, so nobody can probe for other partners' numbers.
- **Suppliers inform; the company decides.**
  - A supplier **confirms** each purchase order line, with a quantity and a delivery date. Each
    change is kept as history and the latest counts. The same confirmation again adds nothing.
  - A supplier sends **delivery notices**, identified by its own delivery note number. The same
    notice again returns the first; a different notice under the same number is refused (409).
    Neither can exceed the quantity still open.
  - A supplier **quotes** on requests it was invited to, through the SRM rules. It sees its own
    quote, never anyone else's.
  - None of this posts anything: goods are still received, checked and posted in the ERP.
- **What partners tell us reaches planning at once.**
  - MRP plans a purchase line's receipt on the latest confirmed date. A new date changes the
    snapshot's fingerprint, so the next 15-minute cycle replans.
  - The control tower flags a confirmed date later than agreed as a new problem kind,
    `confirmed-late`. Its key includes the confirmed date, so a newer confirmation is a new problem.
    It is owned by purchasing.
  - An overdue line's recommendation now uses what the supplier said: on its way (with the
    notice), confirmed for a later date, or no word at all.
  - On-time delivery is still measured against the **agreed** date. A confirmation explains a
    delay; it does not excuse it.
- **The demand outlook is shared only by agreement.** Purchasing switches it on or off per supplier
  (`ForecastShare`). Switching off keeps the record of when it was on. The outlook covers only the
  materials that supplier supplies: what is ordered and open, what MRP plans to buy, and the
  forecast where there is one. It is labelled "an outlook, not an order".
- **Customers** see their own orders: lines, delivered, invoiced, open, deliveries, invoices, and
  whether a line is late against the date they asked for.
- **Logins without passwords on the command line.** `create_partner_user` sets no password. It says
  to run `changepassword`, so no password ends up in a shell history. The demo users
  (`setup_demo_portal`) use the seed users' local demo password.

## Alternatives considered

- **A separate portal service with its own database, synchronised with the ERP:** the usual way
  with SAP Ariba or a supplier network product. It would mean a second copy of orders to keep in
  step, for one developer, and the same rules implemented twice. The portal's views are thin and
  its rules live in one service module, so it can be split out later behind the same API.
- **Object-level checks in each internal view:** dozens of places to get right, and one forgotten
  one leaks. One check in the shared base class, plus a test over every route, cannot be forgotten.
- **Letting supplier confirmations change the purchase order's delivery date:** simpler, but it
  would rewrite what was agreed. The OTIF measure would then excuse every late supplier.
- **Sharing the outlook with every supplier by default:** it reveals volumes and plans. Agreeing it
  per supplier is the safeguard of the north star.
- **E-mail or EDI messages instead of an API:** EDI (for example EDIFACT ORDRSP and DESADV) is what
  large suppliers use. The service functions here are the same steps, so an EDI adapter can call
  them later.

## Consequences

- A supplier's new date reaches MRP, the control tower and the problem's owner within one planning
  cycle, without anyone retyping it.
- Partners see exactly their own documents. The wall is tested against every route, so a new
  internal endpoint is covered automatically.
- The business-rules catalogue picked up the portal's 23 rules by itself (292 in all).
- Not yet: confirmations and notices on the knowledge graph, partner users managed by the partner's
  own administrator, EDI, and notices matched to goods receipts automatically.
- Wave 10b adds the other half of "no rip-and-replace": an adapter that reads master data and open
  documents from an existing ERP, so this intelligence can run beside it.
