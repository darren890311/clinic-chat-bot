"""Calendar providers.

Importing this package registers every adapter, so `get_provider("google")`
works without the caller knowing which module defines it. Adding a third
calendar system means one module and one import line.
"""

from app.providers.calendar import google, microsoft, null  # noqa: F401
from app.providers.calendar.base import (
    CalendarCredentials,
    CalendarError,
    CalendarProvider,
    ExternalEvent,
    get_provider,
    register,
    registered_providers,
)

__all__ = [
    "CalendarCredentials",
    "CalendarError",
    "CalendarProvider",
    "ExternalEvent",
    "get_provider",
    "register",
    "registered_providers",
]
