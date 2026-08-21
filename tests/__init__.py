"""Test package. Present so `tests.conftest` resolves as one module under both pytest and
mypy; without it the same file is discovered twice under two names and type-checking aborts.
"""
