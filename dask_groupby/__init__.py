#!/usr/bin/env python
# flake8: noqa
"""Top-level module for dask_groupby ."""
from .core import groupby_reduce  # noqa

try:
    from importlib.metadata import version as _version
except ImportError:
    # if the fallback library is missing, we are doomed.
    from importlib_metadata import version as _version  # type: ignore[no-redef]

try:
    __version__ = _version("dask_groupby")
except Exception:
    # Local copy or not installed with setuptools.
    # Disable minimum version checks on downstream libraries.
    __version__ = "999"
