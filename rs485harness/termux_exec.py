"""Entry point for `termux-usb -e`: receives the USB file descriptor as the
LAST argument, exports it as $RSH_USB_FD and runs the normal `rsh` CLI with
the remaining arguments, so `usb:A` / `usb:B` port specs resolve.

    termux-usb -r -e "python -m rs485harness.termux_exec sniff --ch1 usb:A --quiet" /dev/bus/usb/001/020

tools/termux/rsh-usb wraps exactly this (and picks the device for you).
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    argv = sys.argv[1:]
    if not argv or not argv[-1].isdigit():
        print("termux_exec: expected the USB fd as last argument "
              "(run through termux-usb -e / tools/termux/rsh-usb)", file=sys.stderr)
        return 2
    os.environ["RSH_USB_FD"] = argv[-1]
    from .cli import main as cli_main
    return cli_main(argv[:-1])


if __name__ == "__main__":
    sys.exit(main())
