"""Error types.

Everything here is fatal by design. This tool feeds an invoice, so when
something doesn't look the way we expect we stop and say why, rather than
carrying on and producing numbers that look plausible.
"""


class FKEError(Exception):
    """Base class for every error this tool raises deliberately."""


class ConfigError(FKEError):
    """config.toml or the environment is wrong."""


class LoginError(FKEError):
    """Couldn't log in, or couldn't tell whether we logged in."""


class SchemaError(FKEError):
    """The HTML wasn't shaped the way we expect.

    Raised when the portal's markup has changed under us. The message should
    always say what we looked for, what we found instead, and which URL.
    """


class DataError(FKEError):
    """The HTML was shaped right but a value in it couldn't be trusted.

    e.g. an unparseable balance, a date we can't read, a detail page whose
    booking ref doesn't match the one we asked for.
    """
