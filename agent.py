"""ShopAssist: a customer-support agent built on Groq's hosted models.

Setup:
    pip install groq python-dotenv
    put your key in a .env file next to this script:  groq_api_key=gsk_...
    (get a key at https://console.groq.com/keys)
Usage:
    python agent.py                 # interactive chat
    python agent.py --ticket T00042 # auto-process one ticket from the data
Override the model:  SHOPASSIST_MODEL=openai/gpt-oss-20b python agent.py
"""
import argparse
import json
import os
from datetime import date
from pathlib import Path

#from dotenv import load_dotenv
from groq import Groq

from tools import (CUSTOMERS, TOOL_SCHEMAS, cancel_order, check_refund_eligibility,
                  check_warranty_claim, close_ticket, escalate_to_human, get_ticket, issue_refund,
                  lookup_order, run_tool)

#load_dotenv(Path(__file__).with_name(".env"))   # reads groq_api_key from the .env file
MODEL = os.getenv("SHOPASSIST_MODEL", "openai/gpt-oss-120b")
MAX_STEPS = 8

SYSTEM = """You are ShopAssist, the customer support agent for ShopEase, an online electronics store.

Rules:
- Be concise, warm and professional.
- Ask for the customer's email and order ID if you don't have them. Never reveal order details without a matching email.
- Always use tools for facts about orders, refunds and warranty. Never guess or invent order data, policy or tracking info.
- Call search_policy before answering policy questions.
- For refunds: call check_refund_eligibility first, then issue_refund only if eligible and requires_human is false.
- Cancel orders only when their verified status is processing; shipped and delivered orders cannot be cancelled.
- Escalate with escalate_to_human when: the tool says requires_human, a double charge or fraud is reported,
  the customer is abusive or threatens legal action, asks for a human, or the policy doesn't clearly cover the case.
- Never ask for or repeat card numbers or passwords.
- After taking an action, tell the customer exactly what you did and what happens next."""

# Groq uses the OpenAI-style tool format, so convert our schemas once.
GROQ_TOOLS = [
    {"type": "function",
     "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in TOOL_SCHEMAS
]


_CLIENT = None


def _client():
    """Create the Groq client on first use so a missing key gives a clear message."""
    global _CLIENT
    if _CLIENT is None:
        key = "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"
        if not key:
            raise RuntimeError("groq_api_key not found. Add the line groq_api_key=gsk_... to the .env file "
                               "next to agent.py (create a key at https://console.groq.com/keys).")
        _CLIENT = Groq(api_key=key)
    return _CLIENT


def run_turn(messages, trace=None):
    """Run the tool-use loop until the model gives a final text answer."""
    for _ in range(MAX_STEPS):
        resp = _client().chat.completions.create(
            model=MODEL, messages=[{"role": "system", "content": SYSTEM}] + messages,
            tools=GROQ_TOOLS, tool_choice="auto", temperature=0.2)
        msg = resp.choices[0].message
        entry = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            entry["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in msg.tool_calls]
        messages.append(entry)
        if not msg.tool_calls:
            return msg.content
        for call in msg.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            out = run_tool(call.function.name, args)
            print(f"  [tool] {call.function.name}({json.dumps(args)}) -> {json.dumps(out)[:150]}")
            if trace is not None:
                trace.append({"tool": call.function.name, "args": args, "result": out})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(out)})
    return "I'm having trouble finishing this, so I'm handing you to a human agent."


def chat():
    messages = []
    print(f"ShopAssist ready (model: {MODEL}). Type 0 (or 'quit') to exit.\n")
    try:
        while (user := input("You: ").strip()).lower() not in ("0", "quit", "exit"):
            if not user:
                continue
            messages.append({"role": "user", "content": user})
            print(f"\nShopAssist: {run_turn(messages)}\n")
    except (KeyboardInterrupt, EOFError):
        pass
    print("\nGoodbye!")


def ticket_reply(ticket_id, trace=None):
    """Handle one ticket and return the reply text (used by the CLI and the web UI)."""
    t = get_ticket(ticket_id)
    if "error" in t:
        return t['error']
    if t["status"].lower() in ("closed", "cancelled"):
        return f"Ticket {ticket_id} is already {t['status'].lower()}."
    email = CUSTOMERS[t["customer_id"]]["email"]
    category = t["category"].lower()
    if category == "refund":
        as_of = t["created_at"]
        eligibility = check_refund_eligibility(
            t["order_id"], email, reason="changed_mind", as_of=as_of
        )
        if "error" in eligibility:
            reply = f"I couldn't verify refund eligibility: {eligibility['error']} The ticket remains open."
        elif eligibility["requires_human"]:
            handoff = escalate_to_human(
                summary=f"Refund request for {t['order_id']}: {eligibility['explanation']}",
                reason=eligibility["explanation"],
                priority=t["priority"],
                ticket_id=ticket_id,
            )
            reply = (f"This refund needs human review. {handoff['message']} "
                     f"The ticket remains open.")
        elif eligibility["eligible"]:
            refund = issue_refund(
                t["order_id"], email, eligibility["amount"], as_of=as_of, ticket_id=ticket_id
            )
            if refund.get("success"):
                closed = close_ticket(ticket_id)
                if not closed.get("success"):
                    raise RuntimeError(closed["error"])
                reply = (f"Your refund of ${refund['amount']:.2f} for order {t['order_id']} "
                         "has been issued to the original payment method. It should arrive "
                         "within 5-7 business days. This ticket is now closed.")
            else:
                details = refund.get("details", refund.get("error", "Unknown refund error."))
                reply = f"I couldn't issue the refund: {details} The ticket remains open."
        elif eligibility["reason_code"] == "outside_return_window":
            closed = close_ticket(ticket_id)
            if not closed.get("success"):
                raise RuntimeError(closed["error"])
            days = eligibility["days_since_delivery"]
            reply = (f"I'm unable to issue a refund for order {t['order_id']}: the request "
                     f"was made {days} days after delivery, outside the "
                     "30-day return window. This ticket is now closed.")
        elif eligibility["reason_code"] == "already_refunded":
            closed = close_ticket(ticket_id)
            if not closed.get("success"):
                raise RuntimeError(closed["error"])
            reply = (f"Order {t['order_id']} is already marked as refunded, so no additional "
                     "refund was issued. This ticket is now closed.")
        else:
            reply = (f"I couldn't issue a refund for order {t['order_id']}: "
                     f"{eligibility['explanation']} The ticket remains open.")
        return reply

    is_cancellation = (category in ("cancellation", "cancel", "cancel_order") or
                       "cancel" in t["message"].lower())
    if is_cancellation:
        result = cancel_order(t["order_id"], email)
        if result.get("success"):
            already = result.get("already_cancelled")
            closed = close_ticket(ticket_id, "closed" if already else "cancelled")
            if not closed.get("success"):
                raise RuntimeError(closed["error"])
            if already:
                reply = f"Order {t['order_id']} was already cancelled. This ticket is now closed."
            else:
                reply = (f"Order {t['order_id']} was cancelled while it was still processing. "
                         "The order and this ticket are now marked as cancelled.")
        elif result.get("status") in ("delivered", "shipped"):
            closed = close_ticket(ticket_id)
            if not closed.get("success"):
                raise RuntimeError(closed["error"])
            reply = f"{result['error']} This ticket is now closed."
        else:
            reply = f"I couldn't cancel order {t['order_id']}: {result.get('error', 'Unknown error.')}"
        return reply

    if category == "warranty":
        claim = check_warranty_claim(t["order_id"], email, t["created_at"])
        if "error" in claim:
            reply = f"I couldn't verify the warranty claim: {claim['error']} The ticket remains open."
        elif claim["eligible"]:
            handoff = escalate_to_human(
                summary=f"Valid warranty claim for order {t['order_id']}; call the customer.",
                reason="Warranty claim is within six months of delivery; follow up by phone.",
                priority=t["priority"],
                ticket_id=ticket_id,
            )
            if not handoff.get("success"):
                raise RuntimeError("Could not create the warranty follow-up.")
            closed = close_ticket(ticket_id)
            if not closed.get("success"):
                raise RuntimeError(closed["error"])
            reply = ("Your warranty claim is valid. A support agent will call you; "
                     "the refund amount will be credited after the call. This ticket is now closed.")
        else:
            reply = f"{claim['explanation']} The ticket remains open."
        return reply

    if category in ("delivery", "order_status"):
        order = lookup_order(t["order_id"], email)
        ticket_resolved = True
        if "error" in order:
            reply = f"I couldn't verify the order status: {order['error']} The ticket remains open."
            ticket_resolved = False
        elif order["status"] == "delivered":
            reply = f"Order {t['order_id']} was delivered on {order['delivered_date']}."
        elif order["status"] == "shipped":
            age = (date.fromisoformat(t["created_at"]) -
                   date.fromisoformat(order["order_date"])).days
            if age > 10:
                handoff = escalate_to_human(
                    summary=f"Delivery investigation needed for order {t['order_id']}.",
                    reason="Shipped order has had no movement for more than 10 days.",
                    priority=t["priority"],
                    ticket_id=ticket_id,
                )
                if not handoff.get("success"):
                    raise RuntimeError("Could not create the delivery investigation.")
                ticket_resolved = False
                reply = (f"Your order is still in transit and appears delayed. "
                         f"{handoff['message']}")
            else:
                reply = (f"Order {t['order_id']} is in transit and is expected to arrive "
                         "within the next few days.")
        elif order["status"] == "processing":
            reply = (f"Order {t['order_id']} is being prepared and has not shipped yet. "
                     "Standard delivery usually takes 3-7 days after dispatch.")
        else:
            reply = f"Order {t['order_id']} currently has status '{order['status']}'."
        if ticket_resolved:
            closed = close_ticket(ticket_id)
            if not closed.get("success"):
                raise RuntimeError(closed["error"])
            reply += " This ticket is now closed."
        return reply

    prompt = (f"Ticket {t['ticket_id']} (priority {t['priority']}, category {t['category']}).\n"
              f"Customer email (verified via account login): {email}\n"
              f"Customer message: {t['message']}\n\n"
              f"Resolve it if routine; otherwise escalate with ticket_id={t['ticket_id']}. "
              f"Write the reply to send to the customer.")
    return run_turn([{'role': 'user', 'content': prompt}], trace)


def process_ticket(ticket_id):
    print(f"\nShopAssist reply:\n{ticket_reply(ticket_id)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticket")
    args = ap.parse_args()
    process_ticket(args.ticket) if args.ticket else chat()
