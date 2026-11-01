"""Failing fixture startup_crash: exits with status 3 at start, before it binds any port.

The health check can never pass, so a release of it must end ``failed`` and its route must never
open (design 5.5, spec US1 scenario 3). The exit status and the stderr line are what the app log
must show (design 5.6: "a crash is logged with the exit code and the last log lines").

It serves no HTTP path at all: there is no listener, no ``/report`` and no health endpoint.
"""

from __future__ import annotations

import sys

EXIT_STATUS = 3


def main() -> None:
    sys.stderr.write(f"startup_crash: exiting at start with status {EXIT_STATUS}\n")
    sys.stderr.flush()
    sys.exit(EXIT_STATUS)


if __name__ == "__main__":
    main()
