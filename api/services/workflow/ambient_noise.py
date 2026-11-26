"""Organization scoping for custom ambient noise files.

``workflow_configurations.ambient_noise_configuration.storage_key`` is a raw
object-storage key that the pipeline downloads and plays under every call.
Only keys issued by ``/workflow/ambient-noise/upload-url`` for the workflow's
own organization (``ambient-noise/{org_id}/...``) may be used; any other key
(another org's upload, a call recording) would be played to this org's
callers or copied into its namespace.
"""

from loguru import logger

from api.utils.storage_key import is_org_scoped_key

CONFIG_KEY = "ambient_noise_configuration"
_KEY_FIELDS = ("storage_key", "storage_backend")


def _without_key(cfg: dict) -> dict:
    return {k: v for k, v in cfg.items() if k not in _KEY_FIELDS}


def is_own_ambient_noise_key(storage_key: str, organization_id: int) -> bool:
    return is_org_scoped_key(storage_key, "ambient-noise", organization_id)


def foreign_ambient_noise_key(
    workflow_configurations: dict | None, organization_id: int
) -> str | None:
    """The configured custom key if it does not belong to the organization."""
    cfg = (workflow_configurations or {}).get(CONFIG_KEY)
    key = cfg.get("storage_key") if isinstance(cfg, dict) else None
    if key and not is_own_ambient_noise_key(str(key), organization_id):
        return str(key)
    return None


def scoped_ambient_noise_config(
    workflow_configurations: dict | None, organization_id: int | None
) -> dict | None:
    """The ambient noise config to run with; a foreign custom key is dropped
    (default noise plays instead) so rows written before the write-side check
    cannot reach another org's audio."""
    cfg = (workflow_configurations or {}).get(CONFIG_KEY)
    if not isinstance(cfg, dict):
        return None
    key = cfg.get("storage_key")
    if key and (
        organization_id is None
        or not is_own_ambient_noise_key(str(key), organization_id)
    ):
        logger.warning(
            f"Ignoring ambient noise storage_key outside org {organization_id}"
        )
        return _without_key(cfg)
    return cfg


def drop_unchanged_foreign_key(
    workflow_configurations: dict | None,
    stored_configurations: dict | None,
    organization_id: int,
) -> dict | None:
    """The configs to save, minus a foreign key that is already stored.

    A workflow saved before the write-side check may still hold a foreign key,
    and the UI sends ``workflow_configurations`` back whole on every save:
    that unchanged key is dropped as the runtime drops it (review round 2 R8).
    A foreign key the save would newly write stays for the caller to refuse.
    """
    key = foreign_ambient_noise_key(workflow_configurations, organization_id)
    if key is None:
        return workflow_configurations
    stored = (stored_configurations or {}).get(CONFIG_KEY)
    if not isinstance(stored, dict) or stored.get("storage_key") != key:
        return workflow_configurations
    return {
        **workflow_configurations,
        CONFIG_KEY: _without_key(workflow_configurations[CONFIG_KEY]),
    }
