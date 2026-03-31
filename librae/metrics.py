"""Backward-compat shim — real module is librae.core.metrics."""
from librae.core.metrics import *  # noqa: F401,F403
from librae.core.metrics import compute_all, _infer_annual_periods, _safe_qs  # noqa: F811
