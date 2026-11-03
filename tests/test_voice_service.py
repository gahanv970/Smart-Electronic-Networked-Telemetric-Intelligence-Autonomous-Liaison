"""Unit tests for the provider-independent voice architecture.

All ElevenLabs traffic is mocked with the official SDK's client shape.  No
network request, API credit, audio device or SAPI COM object is used.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from voice_service import (
    DEFAULT_ELEVENLABS_MODEL,
    DEFAULT_ELEVENLABS_OUTPUT_FORMAT,
    DEFAULT_ELEVENLABS_VOICE_ID,
    DisabledVoiceProvider,
    ElevenLabsVoiceProvider,
    MockVoiceProvider,
    VoiceManager,
    WindowsSapiVoiceProvider,
    _describe_elevenlabs_error,
)


VOICE_ID = "21m00Tcm4TlvDq8ikWAM"


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _config(values=None):
    values = values or {}

    def get_value(key, default=None):
        return values.get(key, default)

    return get_value


def _secret(api_key="fake_key_123", voice_id=VOICE_ID):
    def get_value(key):
        if key == "elevenlabs_api_key":
            return api_key
        if key == "elevenlabs_voice_id":
            return voice_id
        return None

    return get_value


class FakeTTS:
    def __init__(self, audio=b"fake_mp3_audio", error=None):
        self.audio = audio
        self.error = error
        self.calls = []

    def convert(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return [self.audio]


class FakeClient:
    def __init__(self, tts):
        self.text_to_speech = tts

        class FakeVoices:
            def get(self, voice_id):
                return {"voice_id": voice_id}

        self.voices = FakeVoices()


class FakeApiFailure(Exception):
    def __init__(self, status_code, body=None):
        super().__init__("safe test failure")
        self.status_code = status_code
        self.body = body


class FakeVoiceLookup:
    def __init__(self, *, error=None, response=None):
        self.error = error
        self.response = response
        self.calls = []

    def get(self, voice_id):
        self.calls.append(voice_id)
        if self.error is not None:
            raise self.error
        return self.response


class FakeVoiceClient:
    def __init__(self, *, voice_error=None, voice_response=None):
        self.voices = FakeVoiceLookup(error=voice_error, response=voice_response)
        self.text_to_speech = FakeTTS()


def test_mock_voice_provider():
    provider = MockVoiceProvider()
    assert provider.is_available() is True
    provider.speak("Test voice speech")
    assert "Test voice speech" in provider.spoken_texts

    provider.stop()
    assert provider.stopped is True


def test_disabled_voice_provider():
    provider = DisabledVoiceProvider()
    assert provider.is_available() is False
    provider.speak("Silent audio")
    provider.stop()


def test_voice_manager():
    mgr = VoiceManager()
    mgr._provider = MockVoiceProvider()

    assert mgr.is_available() is True
    mgr.speak("Voice output from manager")
    assert "Voice output from manager" in mgr._provider.spoken_texts

    mgr.stop()
    assert mgr._provider.stopped is True


def test_elevenlabs_voice_provider_missing_credentials():
    with patch("voice_service.get_secret", return_value=None), patch(
        "voice_service.get_config_value", side_effect=_config()
    ):
        provider = ElevenLabsVoiceProvider()
        assert provider.is_available() is False
        assert "API Key missing" in provider.get_status()


def test_elevenlabs_voice_provider_uses_default_voice_id():
    with patch(
        "voice_service.get_secret",
        side_effect=_secret(api_key="fake_key_123", voice_id=None),
    ), patch("voice_service.get_config_value", side_effect=_config()):
        provider = ElevenLabsVoiceProvider()
        assert provider._voice_id == DEFAULT_ELEVENLABS_VOICE_ID
        assert provider.is_available() is True
        assert "not yet validated" in provider.get_status()


def test_invalid_output_format_is_not_silently_replaced():
    with patch("voice_service.get_secret", side_effect=_secret()), patch(
        "voice_service.get_config_value",
        side_effect=_config({"elevenlabs_output_format": "pcm_16000"}),
    ):
        provider = ElevenLabsVoiceProvider()
        assert provider.is_available() is False
        assert "mp3_* or wav_*" in provider.get_status()


def test_elevenlabs_uses_official_sdk_and_configured_model():
    tts = FakeTTS()
    client = FakeClient(tts)
    with patch("voice_service.get_secret", side_effect=_secret()), patch(
        "voice_service.get_config_value",
        side_effect=_config(
            {"elevenlabs_model_id": "eleven_turbo_v2_5",
             "elevenlabs_output_format": "mp3_44100_64"}
        ),
    ), patch(
        "voice_service._create_elevenlabs_client", return_value=client
    ) as create_client, patch.object(
        ElevenLabsVoiceProvider, "_deliver_audio"
    ) as play:
        provider = ElevenLabsVoiceProvider()
        assert provider.model_id == "eleven_turbo_v2_5"
        assert provider.output_format == "mp3_44100_64"
        provider.speak("Hello from ElevenLabs!")

        assert _wait_until(lambda: play.call_count == 1)
        create_client.assert_called_once()
        assert create_client.call_args.args[0] == "fake_key_123"
        assert tts.calls[0]["voice_id"] == VOICE_ID
        assert tts.calls[0]["model_id"] == "eleven_turbo_v2_5"
        assert tts.calls[0]["output_format"] == "mp3_44100_64"
        assert tts.calls[0]["text"] == "Hello from ElevenLabs!"
        generation, audio = play.call_args.args
        assert isinstance(generation, int)
        assert audio == b"fake_mp3_audio"


def test_default_model_and_format_are_modern_supported_values():
    with patch("voice_service.get_secret", side_effect=_secret()), patch(
        "voice_service.get_config_value", side_effect=_config()
    ):
        provider = ElevenLabsVoiceProvider()
        assert provider.model_id == DEFAULT_ELEVENLABS_MODEL
        assert provider.output_format == DEFAULT_ELEVENLABS_OUTPUT_FORMAT
        assert provider.is_available() is True


def test_http_401_uses_labeled_sapi_fallback():
    sapi = MagicMock(spec=WindowsSapiVoiceProvider)
    sapi.is_available.return_value = True
    tts = FakeTTS(error=FakeApiFailure(401))
    with patch("voice_service.get_secret", side_effect=_secret()), patch(
        "voice_service.get_config_value", side_effect=_config()
    ), patch(
        "voice_service._create_elevenlabs_client",
        return_value=FakeClient(tts),
    ):
        provider = ElevenLabsVoiceProvider(sapi_fallback=sapi)
        provider.speak("Test 401")

        assert _wait_until(lambda: provider.last_engine == "windows_sapi")
        assert sapi.speak.call_count == 1
        assert "Windows SAPI fallback" in provider.get_status()
        assert "ElevenLabs TTS ready" not in provider.get_status()


def test_fallback_can_be_disabled_without_fake_success():
    sapi = MagicMock(spec=WindowsSapiVoiceProvider)
    sapi.is_available.return_value = True
    tts = FakeTTS(error=FakeApiFailure(429))
    with patch("voice_service.get_secret", side_effect=_secret()), patch(
        "voice_service.get_config_value",
        side_effect=_config({"voice_sapi_fallback": False}),
    ), patch(
        "voice_service._create_elevenlabs_client",
        return_value=FakeClient(tts),
    ):
        provider = ElevenLabsVoiceProvider(sapi_fallback=sapi)
        provider.speak("Test 429")

        assert _wait_until(lambda: "No fallback enabled" in provider.get_status())
        sapi.speak.assert_not_called()
        assert provider.last_engine is None


def test_stop_discards_a_late_generated_utterance():
    started = threading.Event()
    release = threading.Event()

    class BlockingTTS(FakeTTS):
        def convert(self, **kwargs):
            started.set()
            release.wait(3)
            return [b"late_audio"]

    with patch("voice_service.get_secret", side_effect=_secret()), patch(
        "voice_service.get_config_value", side_effect=_config()
    ), patch(
        "voice_service._create_elevenlabs_client",
        return_value=FakeClient(BlockingTTS()),
    ):
        provider = ElevenLabsVoiceProvider()
        play = MagicMock()
        provider._deliver_audio = play
        provider.speak("old speech")
        assert started.wait(2)
        provider.stop()
        release.set()
        time.sleep(0.2)

        play.assert_not_called()
        assert provider.last_engine is None


def test_newer_speech_supersedes_older_request():
    first_started = threading.Event()
    release_first = threading.Event()

    class SequencedTTS(FakeTTS):
        def convert(self, **kwargs):
            if kwargs["text"] == "first":
                first_started.set()
                release_first.wait(3)
                return [b"first_audio"]
            return [b"second_audio"]

    with patch("voice_service.get_secret", side_effect=_secret()), patch(
        "voice_service.get_config_value", side_effect=_config()
    ), patch(
        "voice_service._create_elevenlabs_client",
        return_value=FakeClient(SequencedTTS()),
    ), patch.object(
        ElevenLabsVoiceProvider, "_deliver_audio"
    ) as play:
        provider = ElevenLabsVoiceProvider()
        provider.speak("first")
        assert first_started.wait(2)
        provider.speak("second")
        assert _wait_until(lambda: play.call_count == 1)

        first_generation, first_audio = play.call_args.args
        assert first_audio == b"second_audio"

        release_first.set()
        time.sleep(0.2)
        assert play.call_count == 1
        assert first_generation >= 2


@pytest.mark.parametrize(
    "status_code, expected",
    [
        (400, "voice ID and model ID"),
        (401, "API key"),
        (403, "denied access"),
        (404, "could not find"),
        (422, "could not process"),
        (429, "rate limit"),
        (503, "service error"),
    ],
)
def test_api_errors_are_sanitized_and_meaningful(status_code, expected):
    message = _describe_elevenlabs_error(FakeApiFailure(status_code))
    assert expected in message
    assert "fake_mp3" not in message
    assert "fake_key" not in message


def test_api_error_does_not_echo_response_headers_or_body():
    class RichFailure(Exception):
        status_code = 400

        def __str__(self):
            return "headers={'xi-api-key': 'SUPERSECRET'} body={'text': 'private'}"

    message = _describe_elevenlabs_error(RichFailure())
    assert "SUPERSECRET" not in message
    assert "private" not in message


def test_validate_voice_id_reports_valid_voice():
    client = FakeVoiceClient(voice_response={"voice_id": VOICE_ID})
    with patch("voice_service.get_secret", side_effect=_secret()), patch(
        "voice_service.get_config_value", side_effect=_config()
    ), patch("voice_service._create_elevenlabs_client", return_value=client):
        provider = ElevenLabsVoiceProvider()
        assert provider.validate_voice_id() == "VALID"
        assert provider.voice_validation_status == "VALID"
        assert "VALID" in provider.get_status()
        assert VOICE_ID in client.voices.calls


def test_validate_voice_id_reports_nonexistent_voice():
    client = FakeVoiceClient(
        voice_error=FakeApiFailure(404, body={"detail": "Voice not found"})
    )
    with patch("voice_service.get_secret", side_effect=_secret()), patch(
        "voice_service.get_config_value", side_effect=_config()
    ), patch("voice_service._create_elevenlabs_client", return_value=client):
        provider = ElevenLabsVoiceProvider()
        assert provider.validate_voice_id() == "INVALID"
        assert provider.voice_validation_status == "INVALID"
        assert "Voice not found" in provider.get_status()
        assert "fake_key" not in provider.get_status()


def test_validate_voice_id_reports_inaccessible_voice():
    client = FakeVoiceClient(
        voice_error=FakeApiFailure(
            403, body={"detail": "You do not have access to this voice"}
        )
    )
    with patch("voice_service.get_secret", side_effect=_secret()), patch(
        "voice_service.get_config_value", side_effect=_config()
    ), patch("voice_service._create_elevenlabs_client", return_value=client):
        provider = ElevenLabsVoiceProvider()
        assert provider.validate_voice_id() == "INACCESSIBLE"
        assert provider.voice_validation_status == "INACCESSIBLE"
        assert "do not have access" in provider.get_status().lower()


def test_validate_voice_id_reports_authentication_failure():
    client = FakeVoiceClient(
        voice_error=FakeApiFailure(
            401, body={"detail": "Invalid API key provided"}
        )
    )
    with patch("voice_service.get_secret", side_effect=_secret()), patch(
        "voice_service.get_config_value", side_effect=_config()
    ), patch("voice_service._create_elevenlabs_client", return_value=client):
        provider = ElevenLabsVoiceProvider()
        assert provider.validate_voice_id() == "INVALID"
        status = provider.get_status()
        assert "Invalid API key provided" in status
        assert "fake_key" not in status


def test_validate_voice_id_reports_network_error():
    client = FakeVoiceClient(voice_error=ConnectionError("DNS failed"))
    with patch("voice_service.get_secret", side_effect=_secret()), patch(
        "voice_service.get_config_value", side_effect=_config()
    ), patch("voice_service._create_elevenlabs_client", return_value=client):
        provider = ElevenLabsVoiceProvider()
        status = provider.validate_voice_id()
        assert status == "INVALID"
        message = provider.get_status()
        assert "DNS failed" in message or "network request failed" in message
        assert "fake_key" not in message


def test_missing_sdk_is_reported_clearly():
    assert "SDK is not installed" in _describe_elevenlabs_error(ImportError("x"))
