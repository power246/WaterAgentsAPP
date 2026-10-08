"""M9 domain contract."""

from typing import Annotated
from pydantic import StrictBool, StringConstraints
from app.domain.common import DomainModel, NonEmptyString


class AdminSafeError(DomainModel):
    code: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
    message: NonEmptyString
    retryable: StrictBool = False
