class StableValidationValueError(ValueError):
    def __init__(self, error_type: str, message: str) -> None:
        self.error_type = error_type
        super().__init__(message)


def stable_validation_error(
    error_type: str,
    message: str,
    context: dict[str, object] | None = None,
) -> StableValidationValueError:
    if context:
        message = message.format(**context)
    return StableValidationValueError(error_type, message)
