"""Unit tests for configuration and credential management.

The OS keyring is replaced with an in-memory fake; no real Windows credential is
created, changed, or deleted.
"""

from __future__ import annotations

import json

import pytest

import config as config_module
from config import (
    DEFAULT_CONFIG,
    SecretStorageError,
    delete_secret,
    get_config_value,
    get_secret,
    load_config,
    save_config,
    set_secret,
)


@pytest.fixture
def temp_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "APP_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(
        config_module, "SECRETS_FALLBACK_FILE", tmp_path / ".secrets_store"
    )
    monkeypatch.setattr(config_module, "_LEGACY_WARNED_KEYS", set())
    return tmp_path


@pytest.fixture
def memory_keyring(monkeypatch):
    store = {}

    def set_password(service, key, value):
        assert service == config_module.SERVICE_NAME
        store[key] = value

    def get_password(service, key):
        assert service == config_module.SERVICE_NAME
        return store.get(key)

    def delete_password(service, key):
        if key not in store:
            raise KeyError(key)
        del store[key]

    monkeypatch.setattr("keyring.set_password", set_password)
    monkeypatch.setattr("keyring.get_password", get_password)
    monkeypatch.setattr("keyring.delete_password", delete_password)
    return store


def test_config_defaults(temp_config):
    cfg = load_config()
    assert isinstance(cfg, dict)
    assert cfg["gemini_model"] == DEFAULT_CONFIG["gemini_model"]
    assert "memory_enabled" in cfg
    assert "automation_require_confirmation" in cfg


def test_config_save_and_reload(temp_config):
    test_val = "gemini-2.5-flash"
    save_config({"gemini_model": test_val})
    assert get_config_value("gemini_model") == test_val
    assert json.loads((temp_config / "config.json").read_text(encoding="utf-8"))[
        "gemini_model"
    ] == test_val


def test_secrets_management_uses_keyring_only(temp_config, memory_keyring):
    key_name = "test_verification_key_xyz"
    val = "super_secret_token_12345"

    set_secret(key_name, val)
    assert memory_keyring[key_name] == val
    assert get_secret(key_name) == val
    assert not (temp_config / ".secrets_store").exists()

    delete_secret(key_name)
    assert get_secret(key_name) is None


def test_keyring_failure_never_writes_plaintext(temp_config, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr("keyring.set_password", fail)
    with pytest.raises(SecretStorageError) as exc:
        set_secret("gemini_api_key", "secret-value")

    assert "was not saved" in str(exc.value)
    assert "secret-value" not in str(exc.value)
    assert not (temp_config / ".secrets_store").exists()
    assert not (temp_config / "config.json").exists()


def test_keyring_silent_loss_is_detected(temp_config, monkeypatch):
    monkeypatch.setattr("keyring.set_password", lambda *args: None)
    monkeypatch.setattr("keyring.get_password", lambda *args: None)

    with pytest.raises(SecretStorageError):
        set_secret("gemini_api_key", "secret-value")
    assert not (temp_config / ".secrets_store").exists()


def test_legacy_plaintext_store_is_read_but_not_rewritten(
    temp_config, monkeypatch
):
    legacy = temp_config / ".secrets_store"
    original = json.dumps({"gemini_api_key": "legacy-value"})
    legacy.write_text(original, encoding="utf-8")
    monkeypatch.setattr("keyring.get_password", lambda *args: None)
    monkeypatch.setattr("keyring.set_password", pytest.fail)

    assert get_secret("gemini_api_key") == "legacy-value"
    assert legacy.read_text(encoding="utf-8") == original


def test_environment_secret_takes_precedence(temp_config, monkeypatch):
    monkeypatch.setenv("SENTIAL_TEST_TOKEN", "env-value")
    monkeypatch.setattr("keyring.get_password", lambda *args: "keyring-value")

    assert get_secret("sential_test_token") == "env-value"


def test_empty_secret_is_rejected(temp_config, memory_keyring):
    with pytest.raises(ValueError):
        set_secret("gemini_api_key", "")
    assert "gemini_api_key" not in memory_keyring
