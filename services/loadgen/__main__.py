"""Module execution entry point for python -m services.loadgen."""

import sys

from services.loadgen.main import main

if __name__ == "__main__":
    sys.exit(main())
