"""
Unit tests for Microphone Service and Speech-to-Text (STT) Transcription.
Tests recording lifecycle, cancellation, missing microphone handling,
and mocked multimodal Gemini audio transcription.
"""

from unittest.mock import MagicMock, patch
import pytest

from microphone_service import (
    MicrophoneService,
    check_microphone_availability,
    transcribe_audio_bytes,
)


def test_microphone_availability_check():
    with patch("PySide6.QtMultimedia.QMediaDevices.audioInputs", return_value=["Mic1"]):
        assert check_microphone_availability() is True

    with patch("PySide6.QtMultimedia.QMediaDevices.audioInputs", return_value=[]):
        assert check_microphone_availability() is False


def test_microphone_service_recording_lifecycle():
    svc = MicrophoneService()
    assert svc.is_recording() is False

    mock_src = MagicMock()
    mock_buf = MagicMock()
    mock_buf.data.return_value.data.return_value = b"\x00" * 3200

    svc._audio_source = mock_src
    svc._buffer = mock_buf
    svc._is_recording = True

    assert svc.is_recording() is True

    # Test stop_recording
    wav_bytes = svc.stop_recording()
    assert len(wav_bytes) > 0
    assert svc.is_recording() is False

    # Test cancel_recording when recording
    svc._audio_source = mock_src
    svc._buffer = mock_buf
    svc._is_recording = True

    svc.cancel_recording()
    assert svc.is_recording() is False


def test_transcribe_audio_bytes_short_audio():
    ai_provider = MagicMock()
    # Empty or small byte array returns empty string without calling API
    res = transcribe_audio_bytes(b"short", ai_provider)
    assert res == ""


def test_transcribe_audio_bytes_unconfigured():
    ai_provider = MagicMock()
    ai_provider.is_configured.return_value = False

    # Throws RuntimeError if Gemini key is missing
    with pytest.raises(RuntimeError) as excinfo:
        transcribe_audio_bytes(b"x" * 300, ai_provider)
    assert "Gemini API key is not configured" in str(excinfo.value)


def test_transcribe_audio_bytes_mocked_success():
    ai_provider = MagicMock()
    ai_provider.is_configured.return_value = True
    ai_provider._use_modern_sdk = True
    ai_provider.get_current_model.return_value = "gemini-2.0-flash"

    mock_resp = MagicMock()
    mock_resp.text = "Open Notepad and type hello"
    ai_provider._client.models.generate_content.return_value = mock_resp

    sample_wav = b"x" * 500
    res = transcribe_audio_bytes(sample_wav, ai_provider)

    assert res == "Open Notepad and type hello"
    assert ai_provider._client.models.generate_content.called
