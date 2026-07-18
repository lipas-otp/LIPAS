"""The single public base exception for LIPAS."""
from __future__ import annotations


class LipasError(Exception):
    """Base class for all lipas-raised exceptions."""


class ClaimIdConflict(LipasError):
    """One claim id was reused for two different claim payloads.

    Re-delivering the same claim is safe. Reusing its identity for a different
    fact is a producer bug and is rejected rather than silently overwriting or
    duplicating audit history.
    """


class OrphanedEffectError(LipasError):
    """A stable effect id has an intent but no known terminal outcome.

    Durable execution must not silently submit the same operation again.  The
    caller may reconcile it or make an explicit retry decision under a new
    effect identity.
    """
