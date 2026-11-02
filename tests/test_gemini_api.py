"""
Unit tests for Gemini AI Provider (using mocks to test provider logic without live API expenditure).
"""

from unittest.mock import MagicMock, patch
import pytest

from gemini_api import DEFAULT_MODEL, SUPPORTED_MODELS, GeminiAIProvider


def test_provider_initialization():
    provider = GeminiAIProvider()
    assert provider.get_current_model() == DEFAULT_MODEL
    assert not provider.is_configured()
    assert len(provider.get_available_models()) >= 4


def test_provider_model_selection():
    provider = GeminiAIProvider()
    provider.set_model("gemini-2.5-pro")
    assert provider.get_current_model() == "gemini-2.5-pro"

    with pytest.raises(ValueError):
        provider.set_model("")


def test_provider_api_key_configuration():
    provider = GeminiAIProvider()
    provider.set_api_key("AIzaSyTestKey123456789012345678901234567")
    assert provider.is_configured()

    provider.set_api_key("")
    assert not provider.is_configured()


def test_provider_mocked_message_generation():
    provider = GeminiAIProvider()
    provider.set_api_key("fake_key_for_testing")

    mock_response = MagicMock()
    mock_response.text = "Hello! I am S.E.N.T.I.A.L., ready to assist you."

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    provider._client = mock_client
    provider._use_modern_sdk = True

    history = [{"role": "user", "parts": ["Introduce yourself."]}]
    response = provider.send_message(
        history,
        system_context="User is testing the assistant.",
    )

    assert "S.E.N.T.I.A.L." in response
    assert mock_client.models.generate_content.called
    call_kwargs = mock_client.models.generate_content.call_args.kwargs
    assert "User is testing the assistant." in call_kwargs["config"].system_instruction


def test_provider_public_modern_client_accessors():
    provider = GeminiAIProvider()
    marker = object()

    provider._use_modern_sdk = True
    provider._client = marker
    assert provider.uses_modern_sdk() is True
    assert provider.get_modern_client() is marker

    provider._use_modern_sdk = False
    assert provider.uses_modern_sdk() is False
    assert provider.get_modern_client() is None

    provider._use_modern_sdk = True
    provider._client = None
    provider._api_key = None
    assert provider.get_modern_client() is None


def test_provider_mocked_quota_error():
    provider = GeminiAIProvider()
    provider.set_api_key("fake_key_for_testing")

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = Exception("429 ResourceExhausted: Quota exceeded")

    provider._client = mock_client
    provider._use_modern_sdk = True

    history = [{"role": "user", "parts": ["Hello"]}]
    with pytest.raises(RuntimeError) as exc_info:
        provider.send_message(history)

    assert "quota or rate limit exceeded" in str(exc_info.value).lower()
