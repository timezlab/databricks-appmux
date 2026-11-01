import json, os, sys
from hypercorn.asyncio import serve
from hypercorn.config import Config
import asyncio


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            m = await receive()
            if m["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            else:
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return
    await receive()
    body = json.dumps({"http_version": scope["http_version"], "path": scope["path"], "raw_path": (scope.get("raw_path") or b"").decode(),
                       "headers": [[k.decode(), v.decode() if k.decode().lower() not in ("x-forwarded-access-token", "cookie") else "<redacted>"] for k, v in scope["headers"]]}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


cfg = Config()
cfg.bind = [f"0.0.0.0:{os.environ['DATABRICKS_APP_PORT']}"]
print("SPIKE17H2 hypercorn starting on", cfg.bind, flush=True)
asyncio.run(serve(app, cfg))
