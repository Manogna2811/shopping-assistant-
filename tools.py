"""Tools ShopAssist can call. Business rules are enforced here in code, not just in the prompt."""
import calendar
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

BASE = Path(__file__).parent
DATA = BASE / "data"
AUTO_REFUND_LIMIT = 300.00
RETURN_WINDOW_DAYS = 30


def _load(name):
    return json.loads((DATA / f"{name}.json").read_text())


CUSTOMERS = {c["customer_id"]: c for c in _load("customers")}
ORDERS = {o["order_id"]: o for o in _load("orders")}
TICKETS = {t["ticket_id"]: t for t in _load("tickets")}
CUSTOMERS_BY_EMAIL = {c["email"].lower(): c for c in CUSTOMERS.values()}


def _log(filename, record):
    record["at"] = datetime.now().isoformat(timespec="seconds")
    with open(DATA / filename, "a") as f:
        f.write(json.dumps(record) + "\n")


@contextmanager
def _file_lock(filename):
    with open(DATA / filename, "a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _refund_lock():
    with _file_lock("refunds.lock"):
        yield


@contextmanager
def _ticket_lock():
    with _file_lock("tickets.lock"):
        yield


def _save_orders():
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=DATA, delete=False) as f:
            temp_path = Path(f.name)
            json.dump(list(ORDERS.values()), f, indent=2)
            f.write("\n")
        os.replace(temp_path, DATA / "orders.json")
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def _save_tickets(tickets):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=DATA, delete=False) as f:
            temp_path = Path(f.name)
            json.dump(tickets, f, indent=2)
            f.write("\n")
        os.replace(temp_path, DATA / "tickets.json")
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def close_ticket(ticket_id, status="closed"):
    with _ticket_lock():
        tickets = _load("tickets")
        ticket = next((t for t in tickets if t["ticket_id"] == ticket_id), None)
        if ticket is None:
            return {"error": f"Ticket {ticket_id} not found."}
        if ticket["status"] != status:
            ticket["status"] = status
            _save_tickets(tickets)
        TICKETS.clear()
        TICKETS.update({t["ticket_id"]: t for t in tickets})
        return {"success": True, "ticket_id": ticket_id, "status": status}


def get_ticket(ticket_id):
    with _ticket_lock():
        tickets = _load("tickets")
        TICKETS.clear()
        TICKETS.update({t["ticket_id"]: t for t in tickets})
        ticket = TICKETS.get(ticket_id)
        if ticket is None:
            return {"error": f"Ticket {ticket_id} not found."}
        return ticket


def _close_tickets(ticket_ids):
    ticket_ids = set(ticket_ids)
    if not ticket_ids:
        return
    with _ticket_lock():
        tickets = _load("tickets")
        changed = False
        for ticket in tickets:
            if ticket["ticket_id"] in ticket_ids and ticket["status"] != "closed":
                ticket["status"] = "closed"
                changed = True
        if changed:
            _save_tickets(tickets)
        TICKETS.clear()
        TICKETS.update({t["ticket_id"]: t for t in tickets})


def _reconcile_refunds_locked():
    refunds_file = DATA / "refunds.jsonl"
    if not refunds_file.exists():
        return set()

    refunded_order_ids = set()
    ticket_ids = set()
    with refunds_file.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                record = json.loads(line)
                order_id = record.get("order_id")
                if isinstance(order_id, str):
                    refunded_order_ids.add(order_id)
                ticket_id = record.get("ticket_id")
                if isinstance(ticket_id, str):
                    ticket_ids.add(ticket_id)

    changed = False
    for order_id in refunded_order_ids:
        order = ORDERS.get(order_id)
        if order and order["payment_status"] != "refunded":
            order["payment_status"] = "refunded"
            changed = True
    if changed:
        _save_orders()
    _close_tickets(ticket_ids)
    return refunded_order_ids


def _reconcile_refunds():
    if not (DATA / "refunds.jsonl").exists():
        return
    with _refund_lock():
        ORDERS.clear()
        ORDERS.update({o["order_id"]: o for o in _load("orders")})
        _reconcile_refunds_locked()


_reconcile_refunds()


def _verified_order(order_id, email):
    order = ORDERS.get(order_id.strip().upper())
    if not order:
        return None, {"error": f"Order {order_id} not found."}
    owner = CUSTOMERS[order["customer_id"]]
    if owner["email"].lower() != email.strip().lower():
        return None, {"error": "Email does not match this order. Cannot share details or act on it."}
    return order, None


# ---------- tools ----------
def lookup_order(order_id, email):
    order, err = _verified_order(order_id, email)
    return err or order


def list_customer_orders(email):
    cust = CUSTOMERS_BY_EMAIL.get(email.strip().lower())
    if not cust:
        return {"error": "No customer with that email."}
    rows = [
        {"order_id": o["order_id"], "date": o["order_date"], "status": o["status"], "total": o["total"]}
        for o in ORDERS.values() if o["customer_id"] == cust["customer_id"]
    ]
    return {"customer": cust["name"], "orders": sorted(rows, key=lambda r: r["date"], reverse=True)[:10]}


def search_policy(topic):
    text = (BASE / "policy.md").read_text()
    sections = re.split(r"\n(?=## )", text)
    words = set(re.findall(r"\w+", topic.lower()))
    scored = sorted(sections, key=lambda s: -len(words & set(re.findall(r"\w+", s.lower()))))
    return {"policy_sections": scored[:2]}


def check_refund_eligibility(order_id, email, reason="changed_mind", as_of=None):
    """reason: changed_mind | defective | not_delivered"""
    order, err = _verified_order(order_id, email)
    if err:
        return err
    eligibility_date = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of or date.today()
    total, status = order["total"], order["status"]
    result = {"order_id": order["order_id"], "amount": total, "eligible": False,
              "requires_human": False, "explanation": "", "reason_code": "not_eligible"}

    if order["payment_status"] == "refunded":
        result["reason_code"] = "already_refunded"
        result["explanation"] = "Order has already been refunded."
    elif order["payment_status"] == "failed":
        result["reason_code"] = "payment_failed"
        result["requires_human"] = True
        result["explanation"] = "Payment failed. If the customer says money was deducted, finance must check."
    elif status != "delivered":
        result["reason_code"] = "not_delivered"
        result["explanation"] = "A refund requires a delivered order."
    else:
        days = (eligibility_date - date.fromisoformat(order["delivered_date"])).days
        result["days_since_delivery"] = days
        if 0 <= days < RETURN_WINDOW_DAYS:
            result.update(eligible=True, reason_code="eligible",
                          explanation=f"Delivered {days} days ago, within the {RETURN_WINDOW_DAYS}-day window.")
        elif days < 0:
            result["reason_code"] = "delivery_after_request"
            result["explanation"] = "Delivery date is after the eligibility date."
        elif reason == "defective":
            result["reason_code"] = "warranty_review"
            result["explanation"] = "Outside return window. Use check_warranty for defective items."
        else:
            result["reason_code"] = "outside_return_window"
            result["explanation"] = f"Delivered {days} days ago, outside the {RETURN_WINDOW_DAYS}-day return window."

    if result["eligible"] and total >= AUTO_REFUND_LIMIT:
        result["eligible"] = False
        result["reason_code"] = "approval_required"
        result["requires_human"] = True
        result["explanation"] += f" Amount must be below ${AUTO_REFUND_LIMIT:.0f} for automatic approval: escalate."
    return result


def cancel_order(order_id, email):
    with _refund_lock():
        ORDERS.clear()
        ORDERS.update({o["order_id"]: o for o in _load("orders")})
        _reconcile_refunds_locked()
        order, err = _verified_order(order_id, email)
        if err:
            return err
        if order["status"] == "delivered":
            return {"error": "Delivered orders cannot be cancelled.", "status": "delivered"}
        if order["status"] == "shipped":
            return {"error": "Orders in transit cannot be cancelled.", "status": "shipped"}
        if order["status"] == "cancelled":
            return {"success": True, "order_id": order["order_id"], "status": "cancelled",
                    "already_cancelled": True}
        if order["status"] != "processing":
            return {"error": f"Order status '{order['status']}' cannot be cancelled.",
                    "status": order["status"]}
        order["status"] = "cancelled"
        _save_orders()
        return {"success": True, "order_id": order["order_id"], "status": "cancelled"}


def issue_refund(order_id, email, amount, as_of=None, ticket_id=None):
    with _refund_lock():
        ORDERS.clear()
        ORDERS.update({o["order_id"]: o for o in _load("orders")})
        refunded_order_ids = _reconcile_refunds_locked()

        order, err = _verified_order(order_id, email)
        if err:
            return err

        if order["order_id"] in refunded_order_ids:
            return {"error": "A refund has already been issued for this order."}

        check = check_refund_eligibility(order_id, email, as_of=as_of)
        if not check["eligible"] or check["requires_human"]:
            return {"error": "Refund not permitted automatically.", "details": check["explanation"]}
        if abs(amount - check["amount"]) > 0.01:
            return {"error": f"Amount must equal order total {check['amount']}."}

        _log("refunds.jsonl", {"order_id": check["order_id"], "amount": amount,
                               "ticket_id": ticket_id})
        order["payment_status"] = "refunded"
        _save_orders()
        if ticket_id:
            _close_tickets([ticket_id])
        return {"success": True, "order_id": check["order_id"], "amount": amount,
                "message": "Refund issued to original payment method in 5-7 business days."}


def check_warranty(order_id, email, product_id):
    order, err = _verified_order(order_id, email)
    if err:
        return err
    if not order["delivered_date"]:
        return {"error": "Order has not been delivered; warranty not started."}
    item = next((i for i in order["items"] if i["product_id"] == product_id), None)
    if not item:
        return {"error": f"Product {product_id} is not in this order."}
    days = (date.today() - date.fromisoformat(order["delivered_date"])).days
    months = days / 30.4
    in_warranty = months <= item["warranty_months"]
    return {"product": item["name"], "warranty_months": item["warranty_months"],
            "months_since_delivery": round(months, 1), "in_warranty": in_warranty,
            "next_step": "Offer replacement or refund (customer-caused damage excluded)." if in_warranty
            else "Warranty expired; politely decline and explain."}


def _add_months(day, months):
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return day.replace(year=year, month=month,
                       day=min(day.day, calendar.monthrange(year, month)[1]))


def check_warranty_claim(order_id, email, ticket_created_at):
    order, err = _verified_order(order_id, email)
    if err:
        return err
    if order["status"] != "delivered" or not order["delivered_date"]:
        return {"eligible": False, "explanation": "A warranty claim requires a delivered order."}

    delivered = date.fromisoformat(order["delivered_date"])
    created = date.fromisoformat(ticket_created_at)
    eligible = delivered <= created <= _add_months(delivered, 6)
    return {
        "eligible": eligible,
        "explanation": ("Warranty claim is within six months of delivery."
                        if eligible else "Warranty claim is outside the six-month window."),
        "delivery_date": str(delivered),
    }


def escalate_to_human(summary, reason, priority="medium", ticket_id=None):
    handoff = "ESC-" + datetime.now().strftime("%H%M%S")
    _log("escalations.jsonl", {"handoff_id": handoff, "ticket_id": ticket_id, "reason": reason,
                               "priority": priority, "summary": summary})
    return {"success": True, "handoff_id": handoff, "message": "A human agent will follow up within 24 hours."}


TOOLS = {f.__name__: f for f in [lookup_order, list_customer_orders, search_policy,
                                 check_refund_eligibility, issue_refund, cancel_order,
                                 check_warranty, escalate_to_human]}

_EMAIL = {"type": "string", "description": "Customer's email address, used to verify identity"}
_ORDER = {"type": "string", "description": "Order ID like ORD-10042"}

TOOL_SCHEMAS = [
    {"name": "lookup_order", "description": "Get order details (status, items, delivery, payment). Requires matching email.",
     "input_schema": {"type": "object", "properties": {"order_id": _ORDER, "email": _EMAIL}, "required": ["order_id", "email"]}},
    {"name": "list_customer_orders", "description": "List a customer's recent orders when they don't know the order ID.",
     "input_schema": {"type": "object", "properties": {"email": _EMAIL}, "required": ["email"]}},
    {"name": "search_policy", "description": "Look up ShopEase policy (refunds, delivery, warranty, payments, escalation). Use before answering policy questions.",
     "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
    {"name": "check_refund_eligibility", "description": "Check whether an order qualifies for refund and whether a human must approve.",
     "input_schema": {"type": "object", "properties": {"order_id": _ORDER, "email": _EMAIL,
                      "reason": {"type": "string", "enum": ["changed_mind", "defective", "not_delivered"]},
                      "as_of": {"type": "string", "description": "Optional eligibility date in YYYY-MM-DD format."}},
                      "required": ["order_id", "email"]}},
    {"name": "issue_refund", "description": "Issue a full refund. Only works if eligible and under the auto-approval limit. Call check_refund_eligibility first.",
     "input_schema": {"type": "object", "properties": {"order_id": _ORDER, "email": _EMAIL, "amount": {"type": "number"},
                      "as_of": {"type": "string", "description": "Optional eligibility date in YYYY-MM-DD format."},
                      "ticket_id": {"type": "string", "description": "Ticket to close after a successful refund."}},
                      "required": ["order_id", "email", "amount"]}},
    {"name": "cancel_order", "description": "Cancel an order only while its status is processing. Shipped and delivered orders cannot be cancelled.",
     "input_schema": {"type": "object", "properties": {"order_id": _ORDER, "email": _EMAIL},
                      "required": ["order_id", "email"]}},
    {"name": "check_warranty", "description": "Check if a product in an order is still under warranty.",
     "input_schema": {"type": "object", "properties": {"order_id": _ORDER, "email": _EMAIL,
                      "product_id": {"type": "string", "description": "e.g. P07"}}, "required": ["order_id", "email", "product_id"]}},
    {"name": "escalate_to_human", "description": "Hand the case to a human agent with a clear summary.",
     "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}, "reason": {"type": "string"},
                      "priority": {"type": "string", "enum": ["low", "medium", "high"]}, "ticket_id": {"type": "string"}},
                      "required": ["summary", "reason"]}},
]


def run_tool(name, args):
    fn = TOOLS.get(name)
    if not fn:
        return {"error": f"Unknown tool {name}"}
    try:
        return fn(**args)
    except TypeError as e:
        return {"error": f"Bad arguments: {e}"}
