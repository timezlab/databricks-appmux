"""A small fake Next.js export and an ASGI call helper for the tests of the admin UI server.

The export is modelled on what ``next build`` writes with ``output: "export"``,
``trailingSlash: true`` and the base path ``/_appmux`` (the shape was copied once from a real
export: one ``_R_`` bootstrap script, the ``self.__next_f`` flight scripts, ``async`` chunk
scripts that have a ``src``, a ``404.html``, a directory with an ``index.html`` per page, and the
``__next.*.txt`` prefetch files beside them). It is written into a temporary directory, so these
tests never depend on ``web/`` or on a build having run.

``build_export`` returns the inline script texts it wrote, exactly as they stand between the tags,
so a test can compute the hash it expects without calling the code under test.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from starlette.types import ASGIApp, Message, Scope

BUILD_ID: Final = "buildid0123456789"
CHUNK_PATH: Final = "/_appmux/_next/static/chunks/main-0a1b2c.js"
STYLESHEET_PATH: Final = "/_appmux/_next/static/chunks/main-0a1b2c.css"

# The scripts every page has, as Next writes them (the bootstrap, then the flight data).
BOOTSTRAP_SCRIPT: Final = (
    '(globalThis["TURBOPACK"] || (globalThis["TURBOPACK"] = [])).push('
    '{"otherChunks":["static/chunks/main-0a1b2c.js"],"runtimeModuleIds":[94553]});'
)
FLIGHT_START_SCRIPT: Final = "(self.__next_f=self.__next_f||[]).push([0])"


def flight_script(payload: str) -> str:
    """The flight-data script Next writes: ``payload`` sits inside a JSON string literal."""
    return 'self.__next_f.push([1,"' + payload + '"])'


# A script text with the awkward things a flight payload holds: an escaped quote, a backslash-n
# that is two characters in the file, a non-ASCII letter, an angle bracket and a closing tag that
# Next escapes so that it cannot end the element early.
LAUNCHER_FLIGHT: Final = flight_script(
    '1:\\"$Sreact.fragment\\"\\n2:\\"Launcher \u00e9\\" if (a<b) \\u003c/script>\\n'
)
APP_FLIGHT: Final = flight_script('1:\\"$Sreact.fragment\\"\\n2:\\"App page\\"\\n')
NOT_FOUND_FLIGHT: Final = flight_script('1:\\"$Sreact.fragment\\"\\n2:\\"Not found\\"\\n')
DASHBOARD_FLIGHT: Final = flight_script('1:\\"$Sreact.fragment\\"\\n2:\\"Dashboard\\"\\n')

# The text of a page sits after its scripts. A distinctive marker lets a test tell the pages apart.
PAGE_MARKERS: Final = {
    "index.html": "marker-launcher",
    "404.html": "marker-not-found",
    "app/index.html": "marker-app",
    "dashboard/index.html": "marker-dashboard",
}


def next_page(marker: str, inline_scripts: Sequence[str]) -> str:
    """A page the way Next writes one: chunk scripts with a ``src`` in the head, inline ones last.

    The first inline script is the bootstrap, which carries ``id="_R_"``.
    """
    bootstrap, *flight = inline_scripts
    inline = f'<script id="_R_">{bootstrap}</script>' + "".join(
        f"<script>{text}</script>" for text in flight
    )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charSet="utf-8"/>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
        f'<link rel="stylesheet" href="{STYLESHEET_PATH}" data-precedence="next"/>'
        f'<script src="{CHUNK_PATH}" async=""></script>'
        f'<script src="{CHUNK_PATH}" noModule=""></script>'
        f"<title>{marker}</title></head>"
        f'<body><div hidden=""><!--$--><!--/$--></div><main id="main">{marker}</main>'
        f"{inline}</body></html>"
    )


@dataclass(frozen=True)
class FakeExport:
    root: Path
    # The inline script texts of each HTML file, by the file's path below ``root``.
    inline_scripts: Mapping[str, tuple[str, ...]]


def build_export(root: Path) -> FakeExport:
    """Write the fake export into ``root`` (created if needed) and describe it."""
    scripts = {
        "index.html": (BOOTSTRAP_SCRIPT, FLIGHT_START_SCRIPT, LAUNCHER_FLIGHT),
        "404.html": (BOOTSTRAP_SCRIPT, FLIGHT_START_SCRIPT, NOT_FOUND_FLIGHT),
        "app/index.html": (BOOTSTRAP_SCRIPT, FLIGHT_START_SCRIPT, APP_FLIGHT),
        "dashboard/index.html": (BOOTSTRAP_SCRIPT, FLIGHT_START_SCRIPT, DASHBOARD_FLIGHT),
    }
    files = {name: next_page(PAGE_MARKERS[name], texts) for name, texts in scripts.items()}
    files.update(
        {
            "index.txt": "flight data of the launcher\n",
            "__next._tree.txt": ':HL["/_appmux/_next/static/chunks/main-0a1b2c.css","style"]\n',
            "__next.__PAGE__.txt": '1:"$Sreact.fragment"\n',
            "app/index.txt": "flight data of the app page\n",
            "app/__next._tree.txt": "0:{}\n",
            "app/__next.app.__PAGE__.txt": '1:"$Sreact.fragment"\n',
            "bare/data.json": '{"kind": "a directory with no index page"}\n',
            "_next/static/chunks/main-0a1b2c.js": "console.log('chunk');\n",
            "_next/static/chunks/main-0a1b2c.css": "body { margin: 0; }\n",
            f"_next/static/{BUILD_ID}/_buildManifest.js": "self.__BUILD_MANIFEST = {};\n",
            "unhashed.js": "console.log('not under _next/static');\n",
            "icon.svg": '<svg xmlns="http://www.w3.org/2000/svg"/>\n',
        }
    )
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
    return FakeExport(root=root, inline_scripts=scripts)


# --- calling an ASGI application directly ------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


async def asgi_call(
    app: ASGIApp,
    method: str = "GET",
    path: str = "/",
    *,
    raw_path: bytes | None = None,
    query: bytes = b"",
    headers: Sequence[tuple[str, str]] = (),
) -> Reply:
    """Send one request. ``path`` is what a server hands over (percent-decoded once);
    ``raw_path`` is the target as it came on the wire, ``path`` encoded when left out."""
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode() if raw_path is None else raw_path,
        "query_string": query,
        "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    pending: list[Message] = [{"type": "http.request", "body": b"", "more_body": False}]
    sent: list[Message] = []

    async def receive() -> Message:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return Reply(
        status=start["status"],
        headers={name.decode().lower(): value.decode() for name, value in start["headers"]},
        body=body,
    )
