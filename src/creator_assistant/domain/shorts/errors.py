class ShortsError(RuntimeError):
    """A user-facing Shorts workflow error."""


class InvalidSourceError(ShortsError):
    """The selected source cannot be analysed safely."""


class TranscriptionUnavailableError(ShortsError):
    """No usable local speech-recognition backend is available."""


class InvalidClipError(ShortsError):
    """Candidate boundaries or render settings are invalid."""
