from app.application.transcripts.redaction import _CredentialRedactor


class StreamingCredentialFilter:
    """Incremental credential redaction with bounded private look-behind."""

    def __init__(self, private_values: tuple[str, ...] = ()) -> None:
        self._redactor = _CredentialRedactor(private_values)

    @property
    def buffered_characters(self) -> int:
        return self._redactor.buffered_characters

    def feed(self, chunk: str) -> str:
        return self._redactor.feed(chunk)

    def cancel(self) -> str:
        return self._redactor.cancel()

    def finish(self) -> str:
        return self._redactor.finish()
