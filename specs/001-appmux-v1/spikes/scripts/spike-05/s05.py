"""Spike 5 driver: each framework's smallest app behind the prefix proxy, loaded in a headless browser.
usage: s05.py <scenario> ...   (no args = all)"""
import json
import os
import signal
import socket
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, "venv", "bin")
APPS = os.path.join(HERE, "apps")
sys.path.insert(0, HERE)
from prefix_proxy import PrefixProxy

CHROME = os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell")
# what the real platform sets for every App (spike 17), so the framework behaves as it would there
PLATFORM_ENV = {
    "STREAMLIT_SERVER_HEADLESS": "true", "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
    "STREAMLIT_SERVER_ENABLE_CORS": "false", "STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION": "false",
    "GRADIO_ANALYTICS_ENABLED": "false", "PYTHONUNBUFFERED": "1",
}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, secs=40):
    end = time.time() + secs
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def check_fastapi_like(page, expect):
    page.wait_for_function("document.getElementById('out') && document.getElementById('out').textContent !== 'loading'", timeout=15000)
    txt = page.inner_text("#out")
    return txt == expect, txt


def check_streamlit(page, _):
    page.get_by_text("hello streamlit").first.wait_for(timeout=30000)
    page.get_by_text("echo:abc").first.wait_for(timeout=15000)
    return True, "title rendered and server echo received over the websocket"


def check_streamlit_p2(page, _):
    page.get_by_text("page two").first.wait_for(timeout=30000)
    return True, "deep link to page two rendered"


def check_dash(page, _):
    page.wait_for_selector("#o:has-text('echo:a')", timeout=20000)
    page.fill("#i", "xyz")
    page.wait_for_selector("#o:has-text('echo:xyz')", timeout=15000)
    return True, "layout rendered and callback round trip"


def check_gradio(page, _):
    page.get_by_label("in").first.fill("hi", timeout=30000)
    page.get_by_role("button", name="Submit").click(timeout=10000)
    page.wait_for_function("[...document.querySelectorAll('textarea')].some(t => t.value === 'echo:hi')", timeout=20000)
    return True, "event round trip through the queue"


def sc(name, framework, cmd, cwd, env, strip=True, path="/sales/", check=check_fastapi_like, expect="api:true ws:echo:hi"):
    return dict(name=name, framework=framework, cmd=cmd, cwd=cwd, env=env, strip=strip, path=path, check=check, expect=expect)


def scenarios():
    P = "{PORT}"
    uv = [os.path.join(VENV, "uvicorn"), "app:app", "--host", "127.0.0.1", "--port", P]
    st = [os.path.join(VENV, "streamlit"), "run", "app.py", "--server.port", P, "--server.address", "127.0.0.1"]
    py = [os.path.join(VENV, "python"), "app.py"]
    return [
        sc("fastapi-root-path", "FastAPI", uv, f"{APPS}/fastapi", {"UVICORN_ROOT_PATH": "/sales"}),
        sc("fastapi-control-no-setting", "FastAPI", uv, f"{APPS}/fastapi", {}),
        sc("fastapi-hardcoded-links", "FastAPI", uv, f"{APPS}/fastapi", {"UVICORN_ROOT_PATH": "/sales"}, path="/sales/hard", expect="api:true ws:"),
        sc("streamlit-baseurl-strip", "Streamlit", st, f"{APPS}/streamlit", {"STREAMLIT_SERVER_BASE_URL_PATH": "sales"}, strip=True, check=check_streamlit),
        sc("streamlit-baseurl-keep", "Streamlit", st, f"{APPS}/streamlit", {"STREAMLIT_SERVER_BASE_URL_PATH": "sales"}, strip=False, check=check_streamlit),
        sc("streamlit-control-no-setting", "Streamlit", st, f"{APPS}/streamlit", {}, strip=True, check=check_streamlit),
        sc("streamlit-deeplink-no-setting", "Streamlit", st, f"{APPS}/streamlit-mp", {}, strip=True, path="/sales/p2", check=check_streamlit_p2),
        sc("streamlit-deeplink-baseurl-keep", "Streamlit", st, f"{APPS}/streamlit-mp", {"STREAMLIT_SERVER_BASE_URL_PATH": "sales"}, strip=False, path="/sales/p2", check=check_streamlit_p2),
        sc("dash-prefix-env", "Dash", py, f"{APPS}/dash", {"DASH_REQUESTS_PATHNAME_PREFIX": "/sales/", "DASH_ROUTES_PATHNAME_PREFIX": "/"}, check=check_dash),
        sc("dash-control-no-setting", "Dash", py, f"{APPS}/dash", {}, check=check_dash),
        sc("gradio-root-path-env", "Gradio", py, f"{APPS}/gradio", {"GRADIO_ROOT_PATH": "/sales"}, check=check_gradio),
        sc("gradio-control-no-setting", "Gradio", py, f"{APPS}/gradio", {}, check=check_gradio),
        sc("express-relative-links", "Express", ["node", "server.js"], f"{APPS}/express", {}),
        sc("express-hardcoded-links", "Express", ["node", "server.js"], f"{APPS}/express", {}, path="/sales/hard", expect="api:true ws:"),
    ]


def run(s, browser):
    up = free_port()
    px = free_port()
    env = dict(os.environ)
    env.update(PLATFORM_ENV)
    env.update(s["env"])
    env["PORT"] = str(up)
    cmd = [c.replace("{PORT}", str(up)) for c in s["cmd"]]
    proc = subprocess.Popen(cmd, cwd=s["cwd"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    res = {"scenario": s["name"], "framework": s["framework"], "setting": s["env"] or "none", "prefix_stripped": s["strip"], "page": s["path"]}
    proxy = None
    try:
        if not wait_port(up):
            res["error"] = "app did not start"
            return res
        proxy = PrefixProxy(px, up, strip=s["strip"])
        ctx = browser.new_context()
        page = ctx.new_page()
        console, failed, bad = [], [], []
        page.on("console", lambda m: console.append(m.text[:140]) if m.type == "error" else None)
        page.on("requestfailed", lambda r: failed.append(r.url.replace(f"http://127.0.0.1:{px}", "")[:100]))
        page.on("response", lambda r: bad.append(f"{r.status} {r.url.replace(f'http://127.0.0.1:{px}', '')[:100]}") if r.status >= 400 else None)
        try:
            page.goto(f"http://127.0.0.1:{px}{s['path']}", timeout=30000)
            ok, detail = s["check"](page, s["expect"])
            res["works"], res["detail"] = bool(ok), detail
        except Exception as e:
            res["works"], res["detail"] = False, f"{type(e).__name__}: {str(e)[:160]}"
        time.sleep(1)
        res["requests_forwarded"] = len(proxy.forwarded)
        res["websocket_upgrades"] = proxy.upgrades
        res["stray_requests"] = sorted(set(proxy.stray))[:8]
        res["stray_count"] = len(proxy.stray)
        res["http_errors_seen"] = bad[:6]
        res["console_errors"] = console[:4]
        ctx.close()
    finally:
        if proxy:
            proxy.close()
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    return res


if __name__ == "__main__":
    names = sys.argv[1:]
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
        for s in scenarios():
            if names and s["name"] not in names:
                continue
            r = run(s, browser)
            print(json.dumps(r), flush=True)
            out.append(r)
        browser.close()
    json.dump(out, open(os.path.join(HERE, "s05-results.json"), "w"), indent=1)
