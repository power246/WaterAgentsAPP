"""Bounded nonnegative count."""

from typing import Annotated
from pydantic import Field

Count = Annotated[int, Field(strict=True, ge=0)]
