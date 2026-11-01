"""The writes behind start, stop, activate and the config edit (T106, T107; FR-006, FR-009).

An operation on a running app is a row the registry holds and the reconciler makes true (design
5.2), so what this module builds are ``Change`` values:

* ``desired_state_change`` is Start and Stop. Nothing else changes; the reconciler starts or takes
  down the copy and the route.
* ``target_release_change`` points ``app_config.target_release_no`` at a release and asks for the
  app to run, as an Update does. Activate uses it, after moving the release to ``pending``.
* ``plan_config_edit`` is ``PUT /apps/{app}/config``. A change of the env entries or of the wheel
  sources is a change of what the child runs with, so it becomes a **new release with the same
  snapshot** and goes through the same swap as an Update (FR-009). The health check and the
  own-SP keys belong to the app, not to a release, so they are written on ``app_config`` alone;
  resources and secrets are not touched. A release keeps the config it was created with, which is
  what lets Activate bring back an older release exactly as it was.

All of it is pure: the snapshot in, ``Change`` values out. The route writes them with the audit
entry, in one transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from appmux.registry.models import AppConfig, EnvEntry, Release, ReleaseConfig
from appmux.registry.protocol import Change
from appmux.registry.releases import insert_release_change, next_release_no
from appmux.registry.snapshot import RegistrySnapshot
from appmux.registry.writers import WriteKind


class ConfigEditRefused(Exception):
    """The edit cannot be applied to this app now; ``message`` says what to do next.

    ``code`` is the short word the admin API answers with. The message never carries an env value.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True, slots=True)
class ConfigEdit:
    """The writes of one config edit, in order; ``release_no`` is the release it creates, if any."""

    changes: tuple[Change, ...]
    release_no: int | None


def desired_state_change(config: AppConfig, desired_state: str, *, updated_by: str) -> Change:
    """The ``app_config`` update that starts (``running``) or stops (``stopped``) the app."""
    return Change(
        WriteKind.APP_CONFIG_UPDATE,
        key={"app_slug": config.app_slug},
        values={"desired_state": desired_state, "updated_by": updated_by},
        expected_version=config.version,
    )


def target_release_change(config: AppConfig, release_no: int, *, updated_by: str) -> Change:
    """The ``app_config`` update that targets ``release_no`` and asks for the app to run."""
    return Change(
        WriteKind.APP_CONFIG_UPDATE,
        key={"app_slug": config.app_slug},
        values={
            "target_release_no": release_no,
            "desired_state": "running",
            "updated_by": updated_by,
        },
        expected_version=config.version,
    )


def config_source_release(snapshot: RegistrySnapshot, config: AppConfig) -> Release | None:
    """The release a config edit copies: the target, failing that the active one.

    The target comes first because an edit made to repair a failed Update must start from the
    snapshot that Update stored. A deleted release has no snapshot and is never a source.
    """
    candidates: list[Release | None] = []
    if config.target_release_no is not None:
        candidates.append(snapshot.release(config.app_slug, config.target_release_no))
    candidates.extend(
        release for release in snapshot.releases_for(config.app_slug) if release.status == "active"
    )
    return next(
        (r for r in candidates if r is not None and r.deleted_at is None and r.snapshot_digest),
        None,
    )


def plan_config_edit(
    snapshot: RegistrySnapshot,
    config: AppConfig,
    *,
    env: Sequence[EnvEntry],
    wheel_sources: Sequence[str],
    health_path: str,
    health_deadline_s: int,
    own_sp_client_id_key: str | None,
    own_sp_client_secret_key: str | None,
    updated_by: str,
) -> ConfigEdit:
    """The writes of a config edit, or ``ConfigEditRefused`` if there is nothing to apply it to.

    ``env`` and ``wheel_sources`` are compared with the source release's; if they differ a new
    release is created (same ``snapshot_digest``, ``detected`` and ``wheels``, the new config) and
    made the target, in the same ``app_config`` update that stores the other fields. The app is
    not started by an edit: a stopped app builds the new release when it is started.
    """
    app_values: dict[str, object] = {
        "health_path": health_path,
        "health_deadline_s": health_deadline_s,
        "own_sp_client_id_key": own_sp_client_id_key,
        "own_sp_client_secret_key": own_sp_client_secret_key,
        "updated_by": updated_by,
    }
    wanted = ReleaseConfig(env=list(env), wheel_sources=list(wheel_sources))
    source = config_source_release(snapshot, config)
    if source is None and wanted != ReleaseConfig():
        raise ConfigEditRefused(
            "this app has no release to apply env entries or wheel sources to; press Update first",
            code="no_release",
        )
    if source is None or source.config == wanted:
        return ConfigEdit((_config_update(config, app_values),), release_no=None)

    release_no = next_release_no(snapshot, config.app_slug)
    insert = insert_release_change(
        app_slug=config.app_slug,
        release_no=release_no,
        snapshot_digest=_digest_of(source),
        detected=source.detected,
        config=wanted,
        created_by=updated_by,
    )
    # The wheels the source release installed are part of what it stores; the new release builds
    # from the same ones and resolves nothing again.
    insert = Change(
        insert.kind,
        values={**insert.values, "wheels": [w.model_dump(mode="json") for w in source.wheels]},
    )
    app_values["target_release_no"] = release_no
    return ConfigEdit((insert, _config_update(config, app_values)), release_no=release_no)


def _config_update(config: AppConfig, values: dict[str, object]) -> Change:
    return Change(
        WriteKind.APP_CONFIG_UPDATE,
        key={"app_slug": config.app_slug},
        values=values,
        expected_version=config.version,
    )


def _digest_of(release: Release) -> str:
    if release.snapshot_digest is None:
        raise ValueError("a release without a snapshot cannot be copied")
    return release.snapshot_digest
