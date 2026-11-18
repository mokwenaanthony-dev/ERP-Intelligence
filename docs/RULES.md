# Business rules catalogue

_Generated from the code by `python manage.py business_rules`; do not edit by hand. A test
(`tests/test_rules_catalogue.py`) fails when this file and the code disagree._

Every rule below is enforced in the service layer: breaking it stops the operation, nothing is
posted, and the API answers 400 with the message shown (placeholders in braces are filled in).
These are the "learned rules" of the system, the part of an ERP that is worth more than its code
(see `docs/VISION.md`).

**269 rules** in 10 areas.

| Area | Rules |
|---|---:|
| control | 6 |
| crm | 38 |
| finance | 33 |
| hr | 9 |
| inventory | 21 |
| planning | 12 |
| production | 51 |
| purchasing | 35 |
| sales | 27 |
| srm | 37 |

## control

| Rule (message shown) | Where | Error |
|---|---|---|
| Keep the question under {MAX_QUESTION} characters. | `assistant.py` explain | AssistantError |
| There is no current problem {key} that you may see. | `assistant.py` facts_for | AssistantError |
| {existing.key} was already {existing.outcome.lower()} by {existing.decided_by.username} on {timezone.localtime(existing.decided_at)}; a decision is not changed afterwards. | `decisions.py` _repeat_or_refuse | DecisionConflict |
| A rejection needs a reason: it is how the recommendations improve. | `decisions.py` decide | DecisionError |
| Outcome must be {' or '.join(Decision.Outcome.values)}. | `decisions.py` decide | DecisionError |
| There is no current problem {key} that you may see (it may already be solved). | `decisions.py` decide | DecisionError |

## crm

| Rule (message shown) | Where | Error |
|---|---|---|
| A quotation needs at least one item. | `services.py` _build_quotation_items | CRMError |
| Material {item.material.number} is not active. | `services.py` _build_quotation_items | CRMError |
| Prices cannot be negative. | `services.py` _build_quotation_items | CRMError |
| Quantities must be greater than zero. | `services.py` _build_quotation_items | CRMError |
| Campaign {campaign.code} belongs to another company code. | `services.py` _check_campaign | CRMError |
| Business partner {customer.number} is not a customer of company code {company_code.code}. | `services.py` _check_customer | CRMError |
| Customer {customer.number} is blocked. | `services.py` _check_customer | CRMError |
| Company code {company_code.code} has no receivables reconciliation account. | `services.py` _receivables_account | CRMError |
| Only a SENT quotation can be accepted (quotation {quotation.quotation_number} is {quotation.status}). | `services.py` accept_quotation | CRMError |
| Quotation {quotation.quotation_number} expired on {quotation.valid_until}. | `services.py` accept_quotation | CRMError |
| Ticket {ticket.ticket_number} must be RESOLVED before it is closed (it is {ticket.status}). | `services.py` close_ticket | CRMError |
| Business partner {customer.number} is blocked. | `services.py` convert_lead | CRMError |
| Only a QUALIFIED lead can be converted (lead {lead.lead_number} is {lead.status}). | `services.py` convert_lead | CRMError |
| The expected value cannot be negative. | `services.py` convert_lead | CRMError |
| A lead needs the name of the company. | `services.py` create_lead | CRMError |
| A new opportunity starts in an open stage ({', '.join(Opportunity.OPEN_STAGES)}). | `services.py` create_opportunity | CRMError |
| The expected value cannot be negative. | `services.py` create_opportunity | CRMError |
| A quotation cannot expire before its date. | `services.py` create_quotation | CRMError |
| Opportunity {opportunity.opportunity_number} is for another customer or company code. | `services.py` create_quotation | CRMError |
| Opportunity {opportunity.opportunity_number} is {opportunity.stage}: it is closed. | `services.py` create_quotation | CRMError |
| Lead {lead.lead_number} is {lead.status} and cannot be disqualified. | `services.py` disqualify_lead | CRMError |
| Say why the lead is disqualified. | `services.py` disqualify_lead | CRMError |
| An activity is about exactly one lead, opportunity or ticket. | `services.py` log_activity | CRMError |
| An activity needs a subject. | `services.py` log_activity | CRMError |
| Opportunity {opportunity.opportunity_number} is already {opportunity.stage}. | `services.py` lose_opportunity | CRMError |
| Say why the opportunity was lost. | `services.py` lose_opportunity | CRMError |
| Move to an open stage ({', '.join(Opportunity.OPEN_STAGES)}); an opportunity is won by accepting its quotation and lost with a reason. | `services.py` move_opportunity | CRMError |
| Opportunity {opportunity.opportunity_number} is {opportunity.stage}: it is closed. | `services.py` move_opportunity | CRMError |
| The probability is a percentage from 0 to 100. | `services.py` move_opportunity | CRMError |
| A ticket needs a subject. | `services.py` open_ticket | CRMError |
| Sales order {sales_order.order_number} belongs to another customer. | `services.py` open_ticket | CRMError |
| Only a NEW lead can be qualified (lead {lead.lead_number} is {lead.status}). | `services.py` qualify_lead | CRMError |
| Quotation {quotation.quotation_number} was accepted (sales order {quotation.sales_order.order_number}); cancel the sales order instead. | `services.py` reject_quotation | CRMError |
| Only a RESOLVED ticket can be reopened (ticket {ticket.ticket_number} is {ticket.status}). | `services.py` reopen_ticket | CRMError |
| Say how the ticket was resolved. | `services.py` resolve_ticket | CRMError |
| Ticket {ticket.ticket_number} is {ticket.status}. | `services.py` resolve_ticket | CRMError |
| Only a DRAFT quotation can be sent (quotation {quotation.quotation_number} is {quotation.status}). | `services.py` send_quotation | CRMError |
| Ticket {ticket.ticket_number} is {ticket.status}; only an OPEN ticket can be started. | `services.py` start_ticket | CRMError |

## finance

| Rule (message shown) | Where | Error |
|---|---|---|
| Posted journal documents cannot be deleted; post a reversal instead. | `models.py` delete | ImmutableDocumentError |
| Posted journal documents cannot be changed; post a reversal instead. | `models.py` save | ImmutableDocumentError |
| A line goes to a cost centre or a production order, not both. | `services.py` _check_line | PostingError |
| Account {account.number} is a reconciliation account and needs a business partner. | `services.py` _check_line | PostingError |
| Account {account.number} is blocked for posting. | `services.py` _check_line | PostingError |
| Account {account.number} is not an expense account; it cannot take a cost centre. | `services.py` _check_line | PostingError |
| Account {account.number} is not an expense account; it cannot take an order. | `services.py` _check_line | PostingError |
| Account {account.number} is not in the chart of accounts of {company_code.code}. | `services.py` _check_line | PostingError |
| Account {account.number} needs a cost centre or a production order. | `services.py` _check_line | PostingError |
| Business partner {partner.number} has no {kind} role in {company_code.code} using account {account.number}. | `services.py` _check_line | PostingError |
| Business partner {partner.number} is blocked. | `services.py` _check_line | PostingError |
| Cost centre {cost_center.code} belongs to profit centre {cost_center.profit_center.code}, not {profit_center.code}. | `services.py` _check_line | PostingError |
| Cost centre {cost_center.code} does not belong to {company_code.code}. | `services.py` _check_line | PostingError |
| Cost centre {cost_center.code} is inactive. | `services.py` _check_line | PostingError |
| Each line must be a debit (D) or a credit (C). | `services.py` _check_line | PostingError |
| Line amounts must be greater than zero; use debit or credit for the direction. | `services.py` _check_line | PostingError |
| Profit centre {profit_center.code} does not belong to {company_code.code}. | `services.py` _check_line | PostingError |
| Profit centre {profit_center.code} is inactive. | `services.py` _check_line | PostingError |
| Tax code {line.tax_code.code} does not belong to {company_code.code}. | `services.py` _check_line | PostingError |
| A journal entry needs at least two lines. | `services.py` _create_entry | PostingError |
| Debits and credits do not balance (difference {balance} {currency.code}). | `services.py` _create_entry | PostingError |
| Posting period {period} is closed. | `services.py` _create_entry | PostingError |
| Business partner {partner.number} is not a {kind} of {company_code.code}. | `services.py` _partner_role | PostingError |
| Account {bank_account.number} cannot be used as the bank account. | `services.py` _post_partner_payment | PostingError |
| Allocation of {part} to {item['document_number']} must be between 0.01 and its open amount {item['open_amount']}. | `services.py` _post_partner_payment | PostingError |
| Item {line_id} is not an open item of {kind} {partner.number}. | `services.py` _post_partner_payment | PostingError |
| Payment of {amount} must be fully allocated to open items (allocated {allocated}). | `services.py` _post_partner_payment | PostingError |
| The payment amount must be greater than zero. | `services.py` _post_partner_payment | PostingError |
| Tax code {tax_code.code} has no tax account. | `services.py` _with_tax_lines | PostingError |
| Fiscal year {year} already exists for company code {company_code.code}. | `services.py` create_fiscal_year | PostingError |
| No fiscal year is set up for {posting_date} in company code {company_code.code}. | `services.py` find_period | PostingError |
| Document {entry} has already been reversed. | `services.py` reverse_journal_entry | PostingError |
| Document {entry} is itself a reversal and cannot be reversed. | `services.py` reverse_journal_entry | PostingError |

## hr

| Rule (message shown) | Where | Error |
|---|---|---|
| Payroll is not set up for company code {company_code.code}. | `services.py` _setting | PayrollError |
| There is no PAYE table for tax year {tax_year}; load the SARS tables first. | `services.py` _table | PayrollError |
| Position {position.code} is not in company code {company_code.code}. | `services.py` create_employee | PayrollError |
| The monthly salary cannot be negative. | `services.py` create_employee | PayrollError |
| Employee {employee.personnel_number} has no cost centre: set one on position {employee.position.code} or org unit {employee.position.org_unit.code}. | `services.py` post_payroll_run | PayrollError |
| Month must be between 1 and 12. | `services.py` post_payroll_run | PayrollError |
| Nobody in company code {company_code.code} is employed in {year}-{month}. | `services.py` post_payroll_run | PayrollError |
| Payroll for {year}-{month} is already posted; reverse it to run it again. | `services.py` post_payroll_run | PayrollError |
| Payroll run {run.run_number} is already reversed. | `services.py` reverse_payroll_run | PayrollError |

## inventory

| Rule (message shown) | Where | Error |
|---|---|---|
| Insufficient stock of {material.number} in {item.storage_location}: need {quantity}, have {available}. | `services.py` _stock_effects | StockError |
| Material {material.number} has no stock in plant {plant.code} to value the gain; give a unit price. | `services.py` _stock_effects | StockError |
| A goods issue to a production order (261) needs the order number. | `services.py` _validate | StockError |
| A goods movement needs at least one item. | `services.py` _validate | StockError |
| A transfer needs a destination storage location. | `services.py` _validate | StockError |
| Material {material.number} is a service and is not kept in stock. | `services.py` _validate | StockError |
| Material {material.number} is not active. | `services.py` _validate | StockError |
| Movement {movement_type} cannot refer to a production order. | `services.py` _validate | StockError |
| Movement {movement_type} needs a unit price for material {material.number}. | `services.py` _validate | StockError |
| Quantities must be greater than zero. | `services.py` _validate | StockError |
| Source and destination storage locations must differ. | `services.py` _validate | StockError |
| Storage location {item.storage_location} is not in plant {plant.code}. | `services.py` _validate | StockError |
| Storage location {target} is not in plant {plant.code}. | `services.py` _validate | StockError |
| Unknown movement type {movement_type}. | `services.py` _validate | StockError |
| No {key} account is set up for material type {material_type} in chart {company_code.chart_of_accounts.code}. | `services.py` account_for | StockError |
| Posting period {period} is closed. | `services.py` post_goods_movement | StockError |
| Cannot reverse {document.document_number}: it would leave {material.number} in plant {document.plant.code} with quantity {quantity} and value {value}. Post an inventory adjustment. | `services.py` reverse_material_document | StockError |
| Cannot reverse {document.document_number}: {material.number} in {location} has already been issued. | `services.py` reverse_material_document | StockError |
| Material document {document} has already been reversed. | `services.py` reverse_material_document | StockError |
| Material document {document} is itself a reversal and cannot be reversed. | `services.py` reverse_material_document | StockError |
| Posting period {period} is closed. | `services.py` reverse_material_document | StockError |

## planning

| Rule (message shown) | Where | Error |
|---|---|---|
| No contract, preferred supplier or earlier purchase order for {planned.material.number}: name the supplier and the price. | `mrp.py` convert | MRPError |
| No earlier price from supplier {supplier.number} for {planned.material.number}: give the net price. | `mrp.py` convert | MRPError |
| Planned order {planned.run.run_number}/{planned.number} is {planned.status}: convert the planned orders of the latest MRP run. | `mrp.py` convert | MRPError |
| The BOMs are deeper than {MAX_LEVELS} levels. | `mrp.py` low_level_codes | MRPError |
| Plant {plant.code} has no demand, receipts or planning parameters: nothing to plan. | `mrp.py` run_mrp | MRPError |
| The MRP horizon must be 1 to {MAX_HORIZON} months. | `mrp.py` run_mrp | MRPError |
| Material numbers {', '.join(odd)} contain characters the forecasting contract does not allow. | `services.py` run_forecast | ForecastError |
| Name at least one material, or leave materials out to forecast all with demand. | `services.py` run_forecast | ForecastError |
| Plant {plant.code} has no delivered demand in the {history_months} months before {first_period}: nothing to forecast. | `services.py` run_forecast | ForecastError |
| The horizon must be 1 to {MAX_HORIZON} months. | `services.py` run_forecast | ForecastError |
| Use {HISTORY_RANGE[0]} to {HISTORY_RANGE[1]} months of history. | `services.py` run_forecast | ForecastError |
| {', '.join(services_)} are services: only stock materials are forecast. | `services.py` run_forecast | ForecastError |

## production

| Rule (message shown) | Where | Error |
|---|---|---|
| Order {order.order_number} is {order.get_status_display().lower()}; only released orders can be posted to. | `orders.py` _require_released | ProductionError |
| Order {order.order_number} has postings; complete it instead of cancelling. | `orders.py` cancel_order | ProductionError |
| Order {order.order_number} is {order.get_status_display().lower()}. | `orders.py` cancel_order | ProductionError |
| Activity {code} cannot be negative. | `orders.py` confirm_operation | ProductionError |
| Activity {code} is not recorded at work centre {work_center.code}. | `orders.py` confirm_operation | ProductionError |
| Event {external_id} was already used for operation {existing.operation.operation_number} of order {order.order_number}. | `orders.py` confirm_operation | ProductionError |
| Nothing to confirm: give a yield, scrap or activity quantity. | `orders.py` confirm_operation | ProductionError |
| Operation {operation_number} of order {order.order_number} is already finally confirmed. | `orders.py` confirm_operation | ProductionError |
| Order {order.order_number} has no operation {operation_number}. | `orders.py` confirm_operation | ProductionError |
| Yield and scrap cannot be negative. | `orders.py` confirm_operation | ProductionError |
| Give a storage location of plant {plant.code} for the finished goods. | `orders.py` create_order | ProductionError |
| No active BOM for {material.number} in plant {plant.code} on {start_date}. | `orders.py` create_order | ProductionError |
| Order quantity must be greater than zero. | `orders.py` create_order | ProductionError |
| The finish date cannot be before the start date. | `orders.py` create_order | ProductionError |
| Every component of order {order.order_number} has already been issued. | `orders.py` issue_components | ProductionError |
| {item.material.number} is not a component of order {order.order_number}. | `orders.py` issue_components | ProductionError |
| Order {order.order_number} has {open_quantity} still to deliver; {quantity} would be an over-delivery. | `orders.py` receive_output | ProductionError |
| Quantity must be greater than zero. | `orders.py` receive_output | ProductionError |
| Order {order.order_number} is {order.get_status_display().lower()}, not created. | `orders.py` release_order | ProductionError |
| This BOM would make {material.number} a component of itself: {chain}. | `services.py` _check_no_cycle | ProductionError |
| Material {material.number} is inactive. | `services.py` _check_produced | ProductionError |
| {material.number} is a {material.get_material_type_display().lower()}; only finished and semi-finished products have a {kind}. | `services.py` _check_produced | ProductionError |
| Cost roll-up of {material.number} is deeper than {MAX_LEVELS} levels. | `services.py` _estimate | ProductionError |
| No active BOM for {material.number} in plant {plant.code} on {on}. | `services.py` _estimate | ProductionError |
| No price for {component.number} in plant {plant.code}: there is no stock to value it and no BOM to roll it up. | `services.py` _estimate | ProductionError |
| Each {kind} number may appear only once. | `services.py` _numbered | ProductionError |
| No rate for activity {activity_type.code} at cost centre {cost_center.code} on {on}. | `services.py` activity_rate | ProductionError |
| Line {code} already exists in plant {area.plant.code}. | `services.py` check_line_code | ProductionError |
| Activity type {activity_type.code} is not in company code {company_code.code}. | `services.py` check_work_center | ProductionError |
| Cost centre {cost_center.code} is inactive. | `services.py` check_work_center | ProductionError |
| Cost centre {cost_center.code} is not a production cost centre. | `services.py` check_work_center | ProductionError |
| Cost centre {cost_center.code} is not in company code {company_code.code}. | `services.py` check_work_center | ProductionError |
| Line {line.code} is not in plant {plant.code}. | `services.py` check_work_center | ProductionError |
| Quantity must be greater than zero. | `services.py` cost_estimate | ProductionError |
| A BOM for {material.number} in plant {plant.code} valid from {valid_from} exists; create the new version with a later date. | `services.py` create_bom | ProductionError |
| A BOM needs at least one component. | `services.py` create_bom | ProductionError |
| Component {entry.component.number} is inactive. | `services.py` create_bom | ProductionError |
| Each component may appear only once; add the quantities together. | `services.py` create_bom | ProductionError |
| Quantity of {entry.component.number} must be greater than zero. | `services.py` create_bom | ProductionError |
| {entry.component.number} is a service and cannot be a BOM component. | `services.py` create_bom | ProductionError |
| {material.number} cannot be a component of itself. | `services.py` create_bom | ProductionError |
| A routing for {material.number} in plant {plant.code} valid from {valid_from} exists; create the new version with a later date. | `services.py` create_routing | ProductionError |
| A routing needs at least one operation. | `services.py` create_routing | ProductionError |
| Activity quantities of {code} cannot be negative. | `services.py` create_routing | ProductionError |
| Activity {code} appears twice in operation {op.operation_number}. | `services.py` create_routing | ProductionError |
| Activity {code} in operation {op.operation_number} has no quantity. | `services.py` create_routing | ProductionError |
| Activity {code} is not recorded at work centre {work_center.code}. | `services.py` create_routing | ProductionError |
| Work centre {work_center.code} is inactive. | `services.py` create_routing | ProductionError |
| Work centre {work_center.code} is not in plant {plant.code}. | `services.py` create_routing | ProductionError |
| No active BOM for {material.number} in plant {plant.code} on {on}. | `services.py` explode_bom | ProductionError |
| BOM of {material.number} is deeper than {MAX_LEVELS} levels. | `services.py` walk | ProductionError |

## purchasing

| Rule (message shown) | Where | Error |
|---|---|---|
| A purchase order needs at least one item. | `services.py` _build_items | PurchasingError |
| Give a price of zero or more for material {material.number}. | `services.py` _build_items | PurchasingError |
| Material {material.number} is a service; only stock materials can be ordered. | `services.py` _build_items | PurchasingError |
| Material {material.number} is not active. | `services.py` _build_items | PurchasingError |
| Plant {plant.code if plant else '-'} does not belong to {company_code.code}. | `services.py` _build_items | PurchasingError |
| Quantities must be greater than zero. | `services.py` _build_items | PurchasingError |
| Storage location {location or '-'} is not in plant {plant.code}. | `services.py` _build_items | PurchasingError |
| Tax code {tax_code.code} is not an input tax code of {company_code.code}. | `services.py` _build_items | PurchasingError |
| Business partner {supplier.number} is not a supplier of company code {company_code.code}. | `services.py` _supplier_role | PurchasingError |
| Supplier {supplier.number} is blocked. | `services.py` _supplier_role | PurchasingError |
| A {order.status} order cannot be cancelled. | `services.py` cancel_order | PurchasingError |
| The order has goods receipts; reverse them before cancelling. | `services.py` cancel_order | PurchasingError |
| Supplier invoice {invoice.invoice_number} has payments; reverse the payment first. | `services.py` cancel_supplier_invoice | PurchasingError |
| Supplier invoice {invoice.invoice_number} is already cancelled. | `services.py` cancel_supplier_invoice | PurchasingError |
| Goods can only be received on RELEASED orders (this one is {order.status}). | `services.py` post_goods_receipt | PurchasingError |
| Item {item.order_item.item_number}: receiving {item.quantity} exceeds the open quantity {item.order_item.open_quantity}. | `services.py` post_goods_receipt | PurchasingError |
| Item {item.order_item} does not belong to order {order.order_number}. | `services.py` post_goods_receipt | PurchasingError |
| Nothing left to receive on this order. | `services.py` post_goods_receipt | PurchasingError |
| One goods receipt is for one plant; split the items into separate receipts. | `services.py` post_goods_receipt | PurchasingError |
| Receipt quantities must be greater than zero. | `services.py` post_goods_receipt | PurchasingError |
| Each order item can appear only once on an invoice. | `services.py` post_supplier_invoice | PurchasingError |
| Give the supplier's invoice number. | `services.py` post_supplier_invoice | PurchasingError |
| Invoice quantities must be greater than zero. | `services.py` post_supplier_invoice | PurchasingError |
| Invoice {supplier_reference} from supplier {supplier.number} is already posted. | `services.py` post_supplier_invoice | PurchasingError |
| Item {order_item.item_number}: invoice price {price} differs from the order price {order_item.net_price} by more than {setting.price_tolerance_percent}%. | `services.py` post_supplier_invoice | PurchasingError |
| Item {order_item.item_number}: invoicing {item.quantity} but only {order_item.to_invoice_quantity} has been received and not yet invoiced. | `services.py` post_supplier_invoice | PurchasingError |
| Item {order_item} does not belong to order {order.order_number}. | `services.py` post_supplier_invoice | PurchasingError |
| Nothing has been received on {order.order_number} that is not yet invoiced. | `services.py` post_supplier_invoice | PurchasingError |
| Order {order.order_number} is {order.status} and cannot be invoiced. | `services.py` post_supplier_invoice | PurchasingError |
| The invoice total must be greater than zero. | `services.py` post_supplier_invoice | PurchasingError |
| Purchasing is not set up for chart {company_code.chart_of_accounts.code}. | `services.py` purchasing_setting | PurchasingError |
| Only OPEN orders can be released (this one is {order.status}). | `services.py` release_order | PurchasingError |
| Items can only change while the order is OPEN (it is {order.status}). | `services.py` replace_order_items | PurchasingError |
| Goods receipt {receipt.receipt_number} is already reversed. | `services.py` reverse_goods_receipt | PurchasingError |
| Goods receipt {receipt.receipt_number}: item {order_item.item_number} has been invoiced; cancel the supplier invoice first. | `services.py` reverse_goods_receipt | PurchasingError |

## sales

| Rule (message shown) | Where | Error |
|---|---|---|
| A sales order needs at least one item. | `services.py` _build_items | SalesError |
| Material {material.number} is not active. | `services.py` _build_items | SalesError |
| Plant {plant.code if plant else '-'} does not belong to {company_code.code}. | `services.py` _build_items | SalesError |
| Prices cannot be negative. | `services.py` _build_items | SalesError |
| Quantities must be greater than zero. | `services.py` _build_items | SalesError |
| Tax code {tax_code.code} is not an output tax code of {company_code.code}. | `services.py` _build_items | SalesError |
| Business partner {customer.number} is not a customer of company code {company_code.code}. | `services.py` _customer_role | SalesError |
| Customer {customer.number} is blocked. | `services.py` _customer_role | SalesError |
| Invoice {invoice.invoice_number} has payments; reverse the payment first. | `services.py` cancel_invoice | SalesError |
| Invoice {invoice.invoice_number} is already cancelled. | `services.py` cancel_invoice | SalesError |
| A {order.status} order cannot be cancelled. | `services.py` cancel_order | SalesError |
| The order has deliveries; reverse them before cancelling. | `services.py` cancel_order | SalesError |
| Only OPEN orders can be confirmed (this one is {order.status}). | `services.py` confirm_order | SalesError |
| Delivery quantities must be greater than zero. | `services.py` post_delivery | SalesError |
| Give the items to deliver or a storage location to deliver everything from. | `services.py` post_delivery | SalesError |
| Item {item.order_item.item_number}: delivering {item.quantity} exceeds the open quantity {item.order_item.open_quantity}. | `services.py` post_delivery | SalesError |
| Item {item.order_item} does not belong to order {order.order_number}. | `services.py` post_delivery | SalesError |
| Nothing left to deliver on this order. | `services.py` post_delivery | SalesError |
| One delivery ships from one plant; split the items into separate deliveries. | `services.py` post_delivery | SalesError |
| Only CONFIRMED orders can be delivered (this one is {order.status}). | `services.py` post_delivery | SalesError |
| Delivery {delivery.delivery_number} has already been invoiced. | `services.py` post_invoice | SalesError |
| Delivery {delivery.delivery_number} is reversed and cannot be invoiced. | `services.py` post_invoice | SalesError |
| No price for {material.number} in sales organisation {sales_organization.code} on {on}. | `services.py` price_for | SalesError |
| Items can only change while the order is OPEN (it is {order.status}). | `services.py` replace_order_items | SalesError |
| No revenue account is set up for material type {material.material_type}. | `services.py` revenue_setting | SalesError |
| Delivery {delivery.delivery_number} has been invoiced; cancel the invoice first. | `services.py` reverse_delivery | SalesError |
| Delivery {delivery.delivery_number} is already reversed. | `services.py` reverse_delivery | SalesError |

## srm

| Rule (message shown) | Where | Error |
|---|---|---|
| Business partner {supplier.number} is not a supplier of company code {company_code.code}. | `services.py` _check_supplier | SRMError |
| Supplier {supplier.number} is blocked. | `services.py` _check_supplier | SRMError |
| Company code {company_code.code} has no payables reconciliation account. | `services.py` _payables_account | SRMError |
| Application {application.application_number} cannot be approved without a {' and a '.join(missing)}. | `services.py` approve_application | SRMError |
| Application {application.application_number} is {application.status}. | `services.py` approve_application | SRMError |
| Business partner {partner.number} with VAT number {application.vat_number} is blocked. | `services.py` approve_application | SRMError |
| Only a SENT request can be awarded ({rfq.rfq_number} is {rfq.status}). | `services.py` award_rfq | SRMError |
| Supplier {quote.supplier.number} has not quoted for {rfq.rfq_number}. | `services.py` award_rfq | SRMError |
| That quote belongs to another request, not {rfq.rfq_number}. | `services.py` award_rfq | SRMError |
| Request {rfq.rfq_number} was awarded (purchase order {rfq.purchase_order.order_number}); cancel the purchase order instead. | `services.py` cancel_rfq | SRMError |
| A contract cannot end before it starts. | `services.py` create_contract | SRMError |
| A contract needs at least one item. | `services.py` create_contract | SRMError |
| Each material may appear only once on a contract. | `services.py` create_contract | SRMError |
| Prices cannot be negative and target quantities must be greater than zero. | `services.py` create_contract | SRMError |
| A request for quotation needs at least one item. | `services.py` create_rfq | SRMError |
| Each material may appear only once in a request for quotation. | `services.py` create_rfq | SRMError |
| Invite at least two different suppliers, so their prices can be compared. | `services.py` create_rfq | SRMError |
| Material {item.material.number} is not active. | `services.py` create_rfq | SRMError |
| Quantities must be greater than zero. | `services.py` create_rfq | SRMError |
| The period cannot end before it starts. | `services.py` evaluate_supplier | SRMError |
| The quality score is 1 (poor) to 5 (excellent). | `services.py` evaluate_supplier | SRMError |
| Application {application.application_number} is {application.status}. | `services.py` reject_application | SRMError |
| Say why the application is rejected. | `services.py` reject_application | SRMError |
| A release needs at least one item. | `services.py` release_order | SRMError |
| Contract {contract.contract_number} is valid from {contract.valid_from} to {contract.valid_to}, not on {order_date}. | `services.py` release_order | SRMError |
| Contract {contract.contract_number} is {contract.status}. | `services.py` release_order | SRMError |
| Material {material.number} is not on contract {contract.contract_number}. | `services.py` release_order | SRMError |
| Only {left} of {material.number} is left on contract {contract.contract_number} (target {line.target_quantity}). | `services.py` release_order | SRMError |
| Quantities must be greater than zero. | `services.py` release_order | SRMError |
| Only a DRAFT request can be sent ({rfq.rfq_number} is {rfq.status}). | `services.py` send_rfq | SRMError |
| An application needs the name of the company. | `services.py` submit_application | SRMError |
| The B-BBEE level is 1 to 8 (leave it empty if non-compliant). | `services.py` submit_application | SRMError |
| Give the delivery time in days. | `services.py` submit_quote | SRMError |
| Prices cannot be negative. | `services.py` submit_quote | SRMError |
| Quote a price for every item of request {rfq.rfq_number}, and nothing else. | `services.py` submit_quote | SRMError |
| Request {rfq.rfq_number} is {rfq.status}: it does not take quotes. | `services.py` submit_quote | SRMError |
| The deadline of request {rfq.rfq_number} was {rfq.deadline}. | `services.py` submit_quote | SRMError |
