from unicodedata import normalize as unicode_normalize


def normalize_parameter_concept(value: str) -> str:
    return " ".join(unicode_normalize("NFKC", value).casefold().split())
