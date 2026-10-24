#!/usr/bin/env python3
"""Entry point kept at this path for the Telegram bot; the code lives in the albumproc package."""

import sys

from albumproc.cli import main

if __name__ == "__main__":
    sys.exit(main())
