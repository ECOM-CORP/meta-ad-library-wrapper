"""Named exceptions so failures are actionable rather than silent."""


class AdLibraryError(Exception):
    """Base class for all errors raised by this library."""


class BootstrapError(AdLibraryError):
    """Playwright could not harvest a usable session (doc_id / fb_dtsg / lsd / cookies)."""


class StaleDocIdError(AdLibraryError):
    """The doc_id has rotated: the endpoint returned an error/empty shape that implies
    the persisted query no longer exists. Re-run bootstrap_session() to refresh it."""


class SessionExpiredError(AdLibraryError):
    """fb_dtsg / lsd / cookies were rejected (logged out or token expired).
    Re-run bootstrap_session() to refresh the session."""


class TransientError(AdLibraryError):
    """A transient/spurious server-side error (e.g. 'A server error occured',
    missing_required_variable_value). The client retries these before giving up; the
    session is NOT invalidated."""
