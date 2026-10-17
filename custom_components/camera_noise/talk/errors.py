"""Why sound never reached a camera's speaker, in terms that say what to check.

A port of Peekaboo's TalkbackFailure. The failures look alike on the wire
but need different answers: a Tapo without Third-Party Compatibility
refuses a perfectly correct password, and calling that a wrong password
sends someone to re-type something that was never wrong.
"""

from __future__ import annotations

import enum
import socket


class Failure(enum.Enum):
    # The camera refused the credential it was given.
    REJECTED = "rejected"
    # A Tapo refused the credential; the setting is the likelier cause.
    NEEDS_THIRD_PARTY_ACCESS = "needs_third_party_access"
    # Nothing answered: camera off, or not on this network.
    UNREACHABLE = "unreachable"
    # The camera has no speaker to talk to, or will not open one.
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"

    @property
    def worth_retrying(self) -> bool:
        """Only failures about getting through. Retrying a refused credential
        fixes nothing, and on a Tapo every attempt counts towards a lockout."""
        return self in (Failure.UNREACHABLE, Failure.UNKNOWN)

    @staticmethod
    def of(error: OSError) -> "Failure":
        if isinstance(error, (ConnectionRefusedError, TimeoutError, socket.timeout, socket.gaierror)):
            return Failure.UNREACHABLE
        if isinstance(error, OSError) and error.errno in (113, 10065, 10060, 10061):
            # No route to host / timed out / refused, as Windows and Linux spell them.
            return Failure.UNREACHABLE
        return Failure.UNKNOWN


class TalkbackError(Exception):
    def __init__(self, failure: Failure, message: str) -> None:
        super().__init__(message)
        self.failure = failure

    def __str__(self) -> str:
        return f"{self.failure.value}: {super().__str__()}"
