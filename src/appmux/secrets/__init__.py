"""Secret resolution: a ``scope/key`` reference becomes a value only when a child starts.

The registry holds references, never values (data model 4.7). ``resolver`` reads the value with the
appmux service principal and hands it to one caller, the one that builds the child's environment
(``sandbox/environment.py``). Nothing here logs, stores or prints a value; ``scrub_secrets`` is for
any text that might echo one.
"""

from appmux.secrets.resolver import (
    REDACTED,
    DatabricksSecretReader,
    MissingResourceKeys,
    ResolvedResources,
    ResourceValueResolver,
    SecretReader,
    SecretReadFailed,
    SecretUnreadable,
    UnreadableSecret,
    scrub_secrets,
)

__all__ = [
    "REDACTED",
    "DatabricksSecretReader",
    "MissingResourceKeys",
    "ResolvedResources",
    "ResourceValueResolver",
    "SecretReadFailed",
    "SecretReader",
    "SecretUnreadable",
    "UnreadableSecret",
    "scrub_secrets",
]
