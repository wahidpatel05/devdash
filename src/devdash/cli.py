from __future__ import annotations

import argparse
import sys
from pathlib import Path

from devdash import __version__
from devdash.config import ConfigError, default_config_path, load_config, write_example_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devdash", description=__doc__ or "Dev project dashboard")
    parser.add_argument("-c", "--config", type=Path, help=f"config file (default: {default_config_path()})")
    parser.add_argument("--init-config", action="store_true", help="write an example config and exit")
    parser.add_argument("--version", action="version", version=f"devdash {__version__}")
    args = parser.parse_args(argv)

    path = (args.config or default_config_path()).expanduser()

    if args.init_config:
        try:
            write_example_config(path)
        except (ConfigError, OSError) as e:
            print(f"devdash: {e}", file=sys.stderr)
            return 1
        print(f"Wrote example config to {path}")
        return 0

    try:
        config, error = load_config(path), None
    except ConfigError as e:
        config, error = None, f"{e}\n\nPress q to quit."

    from devdash.ui import DevDashApp  # deferred so --help/--init-config stay fast

    app = DevDashApp(config, error)
    try:
        app.run()
    finally:
        if app.manager:  # normally already stopped; this catches crashes and SIGHUP
            app.manager.kill_all_sync()
    return app.return_code or 0
