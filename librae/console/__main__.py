"""``python -m librae.console [streamlit options]`` runs the console app.

The console has no login, so it listens on 127.0.0.1 unless the caller names
another address with ``--server.address`` (or ``STREAMLIT_SERVER_ADDRESS``);
Streamlit's own default is every interface.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from streamlit.web import cli

DEFAULT_ADDRESS = "127.0.0.1"


def streamlit_argv(args: list[str], environ: dict[str, str]) -> list[str]:
    """The ``streamlit run`` command line for ``args``, bound to localhost by default."""
    app = Path(__file__).with_name("app.py")
    chosen = any(a == "--server.address" or a.startswith("--server.address=") for a in args)
    if not chosen and "STREAMLIT_SERVER_ADDRESS" not in environ:
        args = ["--server.address", DEFAULT_ADDRESS, *args]
    return ["streamlit", "run", str(app), *args]


def main() -> None:
    sys.argv = streamlit_argv(sys.argv[1:], dict(os.environ))
    sys.exit(cli.main())


if __name__ == "__main__":
    main()
