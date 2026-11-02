"""
Unit tests for Gemini Intent Understanding and Output Validation.
Tests structured intent extraction, parameter sanitization, allowlist enforcement,
JSON fallback handling, and mock-based execution.
"""

import json
from unittest.mock import patch
import pytest

from gemini_api import GeminiAIProvider, validate_intent_output


def test_validate_intent_output_defaults():
    # Empty or invalid dict defaults safely
    res = validate_intent_output({})
    assert res["intent_type"] == "general_chat"
    assert res["response_text"] == "I've processed your request."
    assert res["urgency"] == "low"
    assert isinstance(res["parameters"], dict)


def test_validate_intent_output_launch_app_allowlist():
    # Approved app passes and sets requires_confirmation
    valid_data = {
        "intent_type": "launch_app",
        "response_text": "Opening Notepad.",
        "parameters": {"app_id": "notepad"},
        "urgency": "low",
        "requires_confirmation": False
    }
    res = validate_intent_output(valid_data)
    assert res["intent_type"] == "launch_app"
    assert res["parameters"]["app_id"] == "notepad"
    assert res["requires_confirmation"] is True

    # Unapproved app is rejected and downgraded to general_chat
    unapproved_data = {
        "intent_type": "launch_app",
        "response_text": "Opening malicious app.",
        "parameters": {"app_id": "malicious_tool.exe"},
    }
    res_unapproved = validate_intent_output(unapproved_data)
    assert res_unapproved["intent_type"] == "general_chat"
    assert "Access Denied" in res_unapproved["response_text"]
    assert res_unapproved["parameters"] == {}


def test_validate_intent_output_open_url_security():
    # Prohibited non-HTTP scheme rejected
    invalid_url_data = {
        "intent_type": "open_url",
        "response_text": "Opening local file.",
        "parameters": {"url": "file:///C:/Windows/System32/cmd.exe"}
    }
    res = validate_intent_output(invalid_url_data)
    assert res["intent_type"] == "general_chat"
    assert "Security violation" in res["response_text"]

    # Valid HTTPS URL accepted
    valid_url_data = {
        "intent_type": "open_url",
        "response_text": "Opening Google.",
        "parameters": {"url": "https://www.google.com"}
    }
    res_valid = validate_intent_output(valid_url_data)
    assert res_valid["intent_type"] == "open_url"


def test_understand_intent_unconfigured_throws():
    provider = GeminiAIProvider()

    # Throws RuntimeError if key is not configured
    with pytest.raises(RuntimeError) as excinfo:
        provider.understand_intent("Open Notepad")
    assert "API key is not configured" in str(excinfo.value)


@patch.object(GeminiAIProvider, "is_configured", return_value=True)
@patch.object(GeminiAIProvider, "send_message")
def test_understand_intent_mocked_success(mock_send, mock_cfg):
    provider = GeminiAIProvider()
    provider.set_api_key("fake_test_key_123")

    mock_send.return_value = json.dumps({
        "intent_type": "create_task",
        "response_text": "Task scheduled.",
        "parameters": {"title": "Finish Phase 3 Audit"},
        "urgency": "high",
        "requires_confirmation": True
    })

    result = provider.understand_intent("Remind me to finish Phase 3 Audit")
    assert result["intent_type"] == "create_task"
    assert result["parameters"]["title"] == "Finish Phase 3 Audit"
    assert result["urgency"] == "high"
    assert result["requires_confirmation"] is True


@patch.object(GeminiAIProvider, "is_configured", return_value=True)
@patch.object(GeminiAIProvider, "send_message")
def test_understand_intent_mocked_json_decode_fallback(mock_send, mock_cfg):
    provider = GeminiAIProvider()
    provider.set_api_key("fake_test_key_123")

    # Non-JSON string response should fall back cleanly to general_chat
    mock_send.return_value = "I am a plain text response from Gemini without JSON formatting."

    result = provider.understand_intent("Hello!")
    assert result["intent_type"] == "general_chat"
    assert "plain text response" in result["response_text"]
