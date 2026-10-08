"""Bounded M9 scalar identities."""

from typing import Annotated
from pydantic import Field, StringConstraints

Version = Annotated[int, Field(strict=True, ge=1)]
Revision = Annotated[int, Field(strict=True, ge=0)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ProfileId = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
