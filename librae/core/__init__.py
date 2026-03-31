"""Core domain model — shared by backtest and live runtimes.

Pure computation, no I/O. Contains strategy protocol, cost model,
executor logic, metrics, and utility functions.
"""

EPSILON = 1e-9
