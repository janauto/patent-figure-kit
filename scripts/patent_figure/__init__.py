"""Patent figure toolchain, v2.

The package is deliberately layered so that `occ_backend` is the only module that
imports OpenCASCADE (`OCP.*`); every other module works on plain numpy arrays and
dicts and is unit testable without CAD libraries installed.

This file carries the version string only — importing a submodule here would drag
the OCC dependency into the pure modules.
"""

from __future__ import annotations

__version__ = "2.0.0"
