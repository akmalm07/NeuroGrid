"""Run the installed service in the signed base Python process."""

import sys
from pathlib import Path


if __name__ == "__main__":
    packages = Path(__file__).resolve().parent / ".venv" / "Lib" / "site-packages"
    sys.path.insert(0, str(packages))
    from neurogrid_node.service import main

    main()
