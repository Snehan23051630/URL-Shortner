"""Top-level paths reserved for the application and its documentation."""

RESERVED_ALIASES = frozenset({
    "api", "docs", "redoc", "openapi", "openapi.json", "health",
    "favicon", "favicon.ico", "admin", "metrics",
})


def is_reserved_alias(value: str) -> bool:
    return value.casefold() in RESERVED_ALIASES
