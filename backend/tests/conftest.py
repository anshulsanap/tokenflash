"""Test configuration: ensure the backend package directory is importable.

The backend modules import each other by bare module name (e.g.
``from redactor import PLACEHOLDER_RE``), which requires the ``backend/``
directory itself to be on ``sys.path``. Adding it here lets the tests run
regardless of the directory pytest is invoked from.
"""

import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
