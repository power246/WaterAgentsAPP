from enum import Enum


class DomainEnum(str, Enum):
    def __new__(cls, value: str) -> "DomainEnum":
        if not isinstance(value, str) or not value or value != value.lower():
            raise ValueError("domain enum values must be non-empty lowercase strings")
        member = str.__new__(cls, value)
        member._value_ = value
        return member
