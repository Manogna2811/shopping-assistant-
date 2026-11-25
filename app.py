"""ShopAssist - simple Streamlit UI.   Run:  streamlit run app.py
Enter a ticket ID (e.g. T00042) to resolve it, or just chat. Type 0 to start over."""
import re

import streamlit as st

import agent
import tools

st.title("ShopAssist")
st.caption("Enter a ticket ID like T00042 to resolve it, or just chat. Type 0 to start over.")

st.session_state.setdefault("shown", [])   # messages displayed on screen
st.session_state.setdefault("llm", [])     # conversation sent to the model (chat mode)


def snapshot(ticket_id):
    """Current ticket status and its order, read fresh from the data files."""
    ticket = tools.get_ticket(ticket_id)
    if "error" in ticket:
        return None, None
    order = next((o for o in tools._load("orders") if o["order_id"] == ticket["order_id"]), None)
    return ticket["status"], order


def handle_ticket(ticket_id):
    t0, o0 = snapshot(ticket_id)
    reply = agent.ticket_reply(ticket_id)
    t1, o1 = snapshot(ticket_id)
    if t0 is None:
        return reply
    lines = [f"**Ticket {ticket_id}:** {t0} → {t1}"]
    if o0 and o1:
        lines.append(f"**Order {o1['order_id']}:** {o0['status']} → {o1['status']}")
        lines.append(f"**Payment:** {o0['payment_status']} → {o1['payment_status']}")
    return "\n\n".join(lines) + "\n\n---\n\n" + reply


for m in st.session_state.shown:
    with st.chat_message(m["role"]):
        st.markdown(m["content"].replace("$", "\\$"))

if text := st.chat_input("Ticket ID or message"):
    text = text.strip()
    if text == "0":
        st.session_state.shown, st.session_state.llm = [], []
        st.rerun()
    st.session_state.shown.append({"role": "user", "content": text})
    with st.spinner("Working..."):
        try:
            if re.fullmatch(r"T\d+", text, re.I):
                reply = handle_ticket(text.upper())
            else:
                st.session_state.llm.append({"role": "user", "content": text})
                reply = agent.run_turn(st.session_state.llm)
        except Exception as e:  # e.g. Ollama not running or model not pulled
            reply = f"Something went wrong: {e}"
    st.session_state.shown.append({"role": "assistant", "content": reply})
    st.rerun()
