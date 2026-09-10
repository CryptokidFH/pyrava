"""Exceptions raised by the pyrava package."""

from __future__ import annotations


class BaravaError(Exception):
    """Base class for every error raised by this library."""


class ParseError(BaravaError):
    """The wire format could not be decoded."""

    def __init__(self, message: str, source: str = "", position: int | None = None):
        self.source = source
        self.position = position
        if position is not None and source:
            lo = max(0, position - 24)
            hi = min(len(source), position + 24)
            message = f"{message} (at {position}: ...{source[lo:hi]}...)"
        super().__init__(message)


class EncodeError(BaravaError):
    """A value could not be represented in the wire format."""


class TransportError(BaravaError):
    """The device could not be reached, or returned a non-OK status."""


class DiscoveryError(BaravaError):
    """mDNS discovery failed or found nothing."""


class CompileError(BaravaError):
    """An animation script is malformed."""
