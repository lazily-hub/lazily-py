"""Positive-evidence floor for the in-process scenario replay ledger."""

import os


# Derived from the completed scenario gate, not from the corpus alone: this is
# the number of canonical (fixture, id) pairs the Python suite actually replayed.
# Override only for the exactness probe; do not lower it to repair a red run.
MIN_SCENARIOS = int(os.environ.get("MIN_SCENARIOS", "151"))
