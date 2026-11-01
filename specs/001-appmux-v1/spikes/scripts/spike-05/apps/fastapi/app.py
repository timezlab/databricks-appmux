# Unmodified w.r.t. the prefix: links are built with url_for, nothing names /sales.
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/api/ping", name="ping")
def ping():
    return {"ok": True}


@app.websocket("/ws", name="ws")
async def ws(sock: WebSocket):
    await sock.accept()
    msg = await sock.receive_text()
    await sock.send_text("echo:" + msg)
    await sock.close()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    wsurl = str(request.url_for("ws")).replace("http", "ws", 1)
    return f'<!doctype html><body data-api="{request.url_for("ping")}" data-ws="{wsurl}"><div id="out">loading</div><script src="{request.url_for("static", path="app.js")}"></script></body>'


@app.get("/hard", response_class=HTMLResponse)
def hard():  # a typical app with a hard-coded absolute link
    return '<!doctype html><body data-api="/api/ping" data-ws="" ><div id="out">loading</div><script src="/static/app.js"></script></body>'
