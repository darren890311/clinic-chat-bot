"""Domain errors.

Raised by the booking service, translated to HTTP status codes at the API edge
and to spoken explanations by the agent. Keeping them here means neither layer
has to infer what went wrong from a database exception.
"""

from __future__ import annotations


class BookingError(Exception):
    """Base class. Carries a message safe to show a patient."""

    status_code = 400

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class UnknownService(BookingError):
    status_code = 404


class UnknownPractitioner(BookingError):
    status_code = 404


class PractitionerNotQualified(BookingError):
    """Asked for a service this practitioner does not perform."""

    status_code = 409


class SlotUnavailable(BookingError):
    """The requested time is not bookable.

    Raised both when the engine rejects the slot and when the database
    exclusion constraint does, which is the case that matters: two conversations
    confirming the same slot at the same instant.
    """

    status_code = 409


class HoldNotFound(BookingError):
    status_code = 404


class HoldExpired(BookingError):
    """The hold's TTL passed before the patient confirmed."""

    status_code = 410


class AppointmentNotFound(BookingError):
    status_code = 404


class AppointmentNotCancellable(BookingError):
    status_code = 409
