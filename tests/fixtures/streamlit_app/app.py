"""Fixture streamlit_app: a minimal Streamlit app.

A well-behaved Streamlit child for the routing, framework-variable and memory tests (design 5.4,
spec US10). Streamlit is held by the App environment (1.38.0), so nothing has to be installed. It
is started the way a standalone Databricks App starts it, ``streamlit run app.py``; the port,
address and headless settings come from the ``STREAMLIT_*`` variables that appmux (like Databricks)
sets, so the command carries no flags. appmux sets no Streamlit base-path variable: the app works
with the prefix stripped, and ``STREAMLIT_SERVER_BASE_URL_PATH`` would break it.

What it shows
    a title ``streamlit_app fixture``, then a JSON block with
    ``{"fixture": "streamlit_app", "status": "ok", "streamlit": "<version>",
    "port": "<STREAMLIT_SERVER_PORT or unset>", "base_path": "<APPMUX_BASE_PATH or unset>"}``,
    then a button ``Click`` and the line ``Clicks: <n>``, counted in the session state.

Health check
    ``GET /_stcore/health`` answers 200 ``ok`` once the server is up; the page itself needs a
    websocket, so a plain ``GET /`` only returns the static shell.
"""

from __future__ import annotations

import os

import streamlit as st

UNSET = "unset"

st.title("streamlit_app fixture")
st.json(
    {
        "fixture": "streamlit_app",
        "status": "ok",
        "streamlit": st.__version__,
        "port": os.environ.get("STREAMLIT_SERVER_PORT", UNSET),
        "base_path": os.environ.get("APPMUX_BASE_PATH", UNSET),
    }
)

if "clicks" not in st.session_state:
    st.session_state["clicks"] = 0
if st.button("Click"):
    st.session_state["clicks"] += 1
st.write(f"Clicks: {st.session_state['clicks']}")
