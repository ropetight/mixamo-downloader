"""Exception hierarchy used across the Mixamo Downloader."""


class MixamoError(Exception):
    """Base class for every error raised by this package."""


class Stopped(MixamoError):
    """Raised when the user asked the download to stop.

    This is a control-flow exception: it unwinds whatever blocking call is
    in progress (a backoff sleep, a monitor poll, a file download) so the
    worker can shut down promptly instead of finishing the current item.
    """


class AuthError(MixamoError):
    """The access token is missing, expired or was rejected by Mixamo."""


class TokenUnavailable(AuthError):
    """A fresh token was requested from the UI but never arrived."""


class RateLimited(MixamoError):
    """Mixamo answered 429 and the retry budget was exhausted."""


class TransientError(MixamoError):
    """A network or 5xx failure that survived every retry."""


class ExportFailed(MixamoError):
    """Mixamo reported that the export job itself failed."""


class ExportTimeout(MixamoError):
    """The export job never reached the 'completed' state in time."""


class ApiError(MixamoError):
    """Mixamo answered with an unexpected status code or payload."""
