"""
Configuration and secure credential management module for G-AI-ASSISTANT.

Persistent non-secret settings live in JSON.  New credential writes go only to
the operating-system keyring.  The legacy ``data/.secrets_store`` file remains
readable so existing users can migrate, but this module never writes new values
to it.  If secure storage is unavailable, saving fails loudly instead of
silently creating a plaintext copy.
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SERVICE_NAME = "G_AI_ASSISTANT"

# Application data directory (stored inside project root for portability and permissions)
PROJECT_ROOT = Path(__file__).resolve().parent
APP_DIR = PROJECT_ROOT / "data"
CONFIG_FILE = APP_DIR / "config.json"
# Legacy read-only store.  Never write new secrets here.
SECRETS_FALLBACK_FILE = APP_DIR / ".secrets_store"

_LEGACY_WARNED_KEYS: set = set()


class SecretStorageError(RuntimeError):
    """Raised when a credential cannot be stored in secure OS storage."""

DEFAULT_CONFIG: Dict[str, Any] = {
    "gemini_model": "gemini-2.0-flash",
    "memory_enabled": True,
    "automation_require_confirmation": True,
    "gmail_user_email": "",
    "voice_provider": "disabled",
    "theme": "liquid_glass_dark",
    # Agent execution limits (Phase 2). Centralized here so the engine and any
    # future settings UI read the same values; the agent never extends them.
    "agent_max_tool_calls": 8,
    "agent_tool_timeout_seconds": 60,
    "agent_workflow_timeout_seconds": 300,
    "agent_max_turns": 12,
    "agent_confirmation_timeout_seconds": 60,
}


def _ensure_app_dir() -> Path:
    """Ensures the application data directory exists."""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning(f"Could not create app dir {APP_DIR}: {e}")
    return APP_DIR


def load_config() -> Dict[str, Any]:
    """
    Loads application configuration from the persistent JSON file.
    Merges with DEFAULT_CONFIG for any missing keys.
    """
    _ensure_app_dir()
    config = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    config.update(data)
        except Exception as e:
            logger.error(f"Error loading configuration from {CONFIG_FILE}: {e}")
    return config


def save_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merges updates into the existing configuration and persists to disk.
    """
    _ensure_app_dir()
    current = load_config()
    current.update(updates)
    try:
        temp_file = CONFIG_FILE.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=4)
        if temp_file.exists():
            temp_file.replace(CONFIG_FILE)
        logger.info(f"Configuration successfully saved to {CONFIG_FILE}")
    except Exception as e:
        logger.error(f"Failed to save configuration to {CONFIG_FILE}: {e}")
    return current


def get_config_value(key: str, default: Any = None) -> Any:
    """Gets a specific configuration value."""
    config = load_config()
    return config.get(key, default)


# ----------------------------------------------------------------------
# Secure Secret Storage (OS keyring; legacy plaintext read-only)
# ----------------------------------------------------------------------

def _fallback_get_secret(key: str) -> Optional[str]:
    """Read an existing legacy plaintext secret (migration only)."""
    if not SECRETS_FALLBACK_FILE.exists():
        return None
    try:
        with open(SECRETS_FALLBACK_FILE, "r", encoding="utf-8") as f:
            secrets = json.load(f)
            return secrets.get(key)
    except Exception:
        return None


def _fallback_delete_secret(key: str) -> None:
    """Fallback secret deletion."""
    if not SECRETS_FALLBACK_FILE.exists():
        return
    try:
        with open(SECRETS_FALLBACK_FILE, "r", encoding="utf-8") as f:
            secrets = json.load(f)
        if key in secrets:
            del secrets[key]
            with open(SECRETS_FALLBACK_FILE, "w", encoding="utf-8") as f:
                json.dump(secrets, f)
    except Exception:
        pass


def set_secret(key: str, value: str) -> None:
    """Store a credential in the operating-system keyring.

    Raises :class:`SecretStorageError` when secure storage is unavailable or
    does not retain the value.  There is intentionally no plaintext fallback.
    """
    if not key or not isinstance(key, str):
        raise ValueError("Secret key must be a non-empty string.")
    if value is None:
        delete_secret(key)
        return
    if not isinstance(value, str):
        raise TypeError("Secret value must be a string or None.")
    if not value:
        raise ValueError("Secret value cannot be empty; use None to delete it.")

    try:
        import keyring

        keyring.set_password(SERVICE_NAME, key, value)
        # Verify immediately: some backends fail silently instead of raising.
        stored = keyring.get_password(SERVICE_NAME, key)
        if stored != value:
            raise SecretStorageError(
                "The operating-system keyring did not retain the credential."
            )
        logger.debug("Secret '%s' saved to OS Keyring.", key)
    except SecretStorageError:
        raise
    except Exception as exc:
        # Never fall back to a readable file.  Do not include the secret value
        # or the underlying backend message in the exception surface.
        logger.error(
            "Secure storage failed for secret '%s' (%s); no plaintext copy was written.",
            key,
            type(exc).__name__,
        )
        raise SecretStorageError(
            "Secure OS credential storage is unavailable or rejected the "
            f"credential '{key}'. The secret was not saved. Configure Windows "
            "Credential Manager/keyring and try again."
        ) from exc


def get_secret(key: str) -> Optional[str]:
    """
    Retrieves a secret from environment, OS Keyring, or the legacy plaintext
    store (read-only, for migration).
    """
    # 1. Environment variable first (e.g. GEMINI_API_KEY)
    env_name = key.upper()
    if env_name in os.environ and os.environ[env_name].strip():
        return os.environ[env_name].strip()

    # 2. OS Keyring
    try:
        import keyring
        val = keyring.get_password(SERVICE_NAME, key)
        if val:
            return val
    except Exception as exc:
        logger.debug("Keyring lookup for '%s' failed: %s", key, type(exc).__name__)

    # 3. Existing legacy store, read only.  Never write new values here.
    legacy_value = _fallback_get_secret(key)
    if legacy_value and key not in _LEGACY_WARNED_KEYS:
        _LEGACY_WARNED_KEYS.add(key)
        logger.warning(
            "Secret '%s' is being read from the legacy plaintext store. "
            "Save it again in Settings to migrate it to the OS keyring; the "
            "legacy file is not deleted automatically.",
            key,
        )
    return legacy_value


def delete_secret(key: str) -> None:
    """Deletes a secret from Keyring and fallback store."""
    try:
        import keyring
        keyring.delete_password(SERVICE_NAME, key)
    except Exception:
        pass
    _fallback_delete_secret(key)
