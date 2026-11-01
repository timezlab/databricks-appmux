"""The package modes: where a build may take packages from (design 5.3, FR-031, FR-032, T134).

The mode is a platform setting for all children (``PlatformSettings.package_mode``; which mode is
the default is the platform's business, not this module's). It decides two things:

* which sources the resolution may use for a requirement the environment cannot satisfy: hashed
  wheels (``wheelhouse``), the package index (``index``), or wheels first and the index for what is
  missing (``wheelhouse+index``);
* what the build may reach on the network. ``build_egress`` hands the build an all-deny list unless
  the step really needs the index: a wheel install reads local files and has nothing to reach, so
  an untrusted wheel's install hook cannot call out while it runs.

The three words are the ones the settings and the admin API already use, so a stored value always
parses.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Final

from appmux.sandbox.rules import EgressRule


class PackageMode(StrEnum):
    INDEX = "index"
    WHEELHOUSE = "wheelhouse"
    WHEELHOUSE_INDEX = "wheelhouse+index"

    @property
    def uses_wheels(self) -> bool:
        """Whether a requirement may be met by a hashed wheel (the content store and sources)."""
        return self is not PackageMode.INDEX

    @property
    def uses_index(self) -> bool:
        """Whether a requirement no wheel covers may be taken from the package index."""
        return self is not PackageMode.WHEELHOUSE

    @property
    def builds_source_distributions(self) -> bool:
        """Only ``index`` mode builds a source distribution; the wheel modes install wheels only."""
        return self is PackageMode.INDEX


# `deny 0.0.0.0/0` matches every address. The rule engine always lets the virtual subnet through
# first (slirp's gateway and DNS, rules.py), so this is "deny everything the rules govern".
DENY_ALL_EGRESS: Final = (
    EgressRule(
        action="deny",
        cidr="0.0.0.0/0",
        port="any",
        comment="a build that needs no package index reaches nothing",
    ),
)


def build_egress(
    mode: PackageMode, app_rules: Sequence[EgressRule], *, needs_index: bool
) -> tuple[EgressRule, ...]:
    """The app-level egress list for one build step.

    ``wheelhouse`` is all-deny always (FR-032). The index modes keep the app's own rules only for a
    step that really talks to the index; any other step of theirs (installing local wheels,
    ``pip check``) is all-deny as well.
    """
    if mode.uses_index and needs_index:
        return tuple(app_rules)
    return DENY_ALL_EGRESS
