#!/usr/bin/env python3
"""proof-of-effect: prove that config, CLI-flag and env-var changes actually take effect.

Run `python3 poe.py --help`. Needs Python 3.9+ and git; PyYAML enables the workflow checks.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from poe.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
