"""streamlit-inspector: a small Streamlit app that shows a WebSocket session through appmux.

It is a standard Databricks App: nothing in it knows about appmux, and the same folder runs on a
standalone App. Started with ``streamlit run app.py``; the port, address and headless settings come
from the ``STREAMLIT_*`` variables the platform sets (and appmux sets, design 5.4). appmux sets no
Streamlit base-path variable: the app works with the prefix stripped, and
``STREAMLIT_SERVER_BASE_URL_PATH`` would break it.

What it shows
    The page reports who is calling (the identity headers of the session's WebSocket handshake,
    read with ``st.context.headers``), which release is running (``VERSION`` below and
    ``APP_GREETING`` from the environment), when this process started and how many sessions it has
    served. A button and a text box show that a session stays connected: the click count and the
    text live in the session, so they survive a rerun and reset on a reload. The session count
    resets when the process is replaced (an Update, a rollback, a config change or a restart).

Health check
    ``GET /`` answers 200 with Streamlit's static shell (appmux's default health path) and
    ``GET /_stcore/health`` answers 200 ``ok``. Neither needs a WebSocket, so a plain GET never
    runs this script: only a browser session does.

The access token is reported as present or absent and never echoed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import streamlit as st

VERSION: Final = "1.0.0"

# Headers the proxy sets and appmux forwards (design 7.1). appmux drops any client copy of them.
EMAIL_HEADER: Final = "X-Forwarded-Email"
USER_HEADER: Final = "X-Forwarded-User"
USERNAME_HEADER: Final = "X-Forwarded-Preferred-Username"
PREFIX_HEADER: Final = "X-Forwarded-Prefix"
REQUEST_ID_HEADER: Final = "X-Request-Id"
OBO_HEADER: Final = "X-Forwarded-Access-Token"


@dataclass
class ProcessState:
    started_at: datetime
    sessions: int = 0


@st.cache_resource
def process_state() -> ProcessState:
    """One object per server process: made by the first session, gone with the process."""
    return ProcessState(started_at=datetime.now(UTC))


state = process_state()
if "counted" not in st.session_state:
    st.session_state["counted"] = True
    state.sessions += 1

headers = st.context.headers

st.title(f"streamlit-inspector v{VERSION}")
st.write(os.environ.get("APP_GREETING", "(APP_GREETING is not set)"))

st.subheader("Who is calling")
st.json(
    {
        "email": headers.get(EMAIL_HEADER),
        "user": headers.get(USER_HEADER),
        "preferred_username": headers.get(USERNAME_HEADER),
        # Present only for an OBO-delegated app and a visitor allowed to pass it (design 7.1).
        "access_token_header_present": OBO_HEADER in headers,
        "x_forwarded_prefix": headers.get(PREFIX_HEADER),
        "appmux_base_path": os.environ.get("APPMUX_BASE_PATH"),
        "request_id": headers.get(REQUEST_ID_HEADER),
    }
)

st.subheader("This process")
st.json(
    {
        "version": VERSION,
        "streamlit": st.__version__,
        "started_at": state.started_at.isoformat(timespec="seconds"),
        "sessions_since_start": state.sessions,
    }
)

st.subheader("This session")
if "clicks" not in st.session_state:
    st.session_state["clicks"] = 0
if st.button("Click"):
    st.session_state["clicks"] += 1
st.write(f"Clicks: {st.session_state['clicks']}")
st.write(f"Echo: {st.text_input('Type something', max_chars=100)}")
