"""Defaults and migration for saved pelican test options."""

PELICAN_DEFAULT_MAX_TOKENS = 32768
PELICAN_SETTINGS_VERSION = 1


def saved_output_limit(settings: dict) -> str:
    """Use the saved value; factory defaults only apply to absent/invalid data."""
    value = settings.get("pelican_max_tokens", PELICAN_DEFAULT_MAX_TOKENS)
    try:
        tokens = int(value)
    except (ValueError, TypeError, OverflowError):
        tokens = PELICAN_DEFAULT_MAX_TOKENS
    if tokens < 256:
        tokens = PELICAN_DEFAULT_MAX_TOKENS
    return str(tokens)
