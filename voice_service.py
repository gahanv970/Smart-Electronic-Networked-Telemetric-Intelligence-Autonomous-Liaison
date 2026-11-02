"""
Provider-Independent Voice Architecture for G-AI-ASSISTANT.
Provides an abstract base interface for text-to-speech (TTS),
ensuring zero blocking of the Qt UI loop, lightweight resource consumption,
and future pluggability for Edge TTS or Kokoro TTS.
"""

from abc import ABC, abstractmethod
import logging
import os
import sys
import tempfile
import threading
from typing import Optional

from PySide6.QtCore import QObject, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from config import get_config_value, get_secret, save_config, set_secret

logger = logging.getLogger(__name__)


class VoiceProvider(ABC):
    """
    Abstract interface for Voice / Text-to-Speech engines.
    """

    @abstractmethod
    def speak(self, text: str) -> None:
        """Asynchronously plays spoken audio for text without blocking caller."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Immediately stops any ongoing speech playback."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Returns True if the TTS backend is ready and operational."""
        pass

    @abstractmethod
    def get_status(self) -> str:
        """Returns a human-readable status description."""
        pass


class DisabledVoiceProvider(VoiceProvider):
    """Null provider when voice is disabled."""

    def speak(self, text: str) -> None:
        pass

    def stop(self) -> None:
        pass

    def is_available(self) -> bool:
        return False

    def get_status(self) -> str:
        return "Voice disabled in settings"


class MockVoiceProvider(VoiceProvider):
    """Mock provider for unit tests and verification."""

    def __init__(self):
        self.spoken_texts = []
        self.stopped = False

    def speak(self, text: str) -> None:
        self.spoken_texts.append(text)
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    def is_available(self) -> bool:
        return True

    def get_status(self) -> str:
        return "Mock TTS active"


class AudioPlayerSignals(QObject):
    """Queued signal bridge from the TTS worker to the Qt GUI thread."""

    # (generation, audio_bytes): the generation lets the GUI discard a late
    # response belonging to speech that has since been stopped or replaced.
    play_audio_signal = Signal(object)


#: Stable, documented ElevenLabs model that supports the standard TTS endpoint.
#: Users can override it with ``elevenlabs_model_id`` in config.json or the
#: ``ELEVENLABS_MODEL_ID`` environment variable.
DEFAULT_ELEVENLABS_MODEL = "eleven_multilingual_v2"
#: MP3 is accepted by QMediaPlayer on all supported Windows versions.
DEFAULT_ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"


def _redact_text(value: object, limit: int = 300) -> str:
    """Remove credential-shaped text and cap a diagnostic string."""
    import re

    text = str(value or "")
    patterns = (
        (
            re.compile(
                r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password|authorization)"
                r"\s*[:=]\s*\S+"
            ),
            r"\1=[REDACTED]",
        ),
        (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"), "[REDACTED]"),
        (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "Bearer [REDACTED]"),
    )
    for pattern, replacement in patterns:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def _describe_elevenlabs_error(exc: BaseException) -> str:
    """Map SDK failures to useful messages without echoing request details.

    ElevenLabs ``ApiError.__str__`` includes response headers and body.  We
    deliberately inspect only the status code, so an error response can never
    echo a credential or the spoken text into logs or the UI.
    """
    if isinstance(exc, ImportError):
        return "ElevenLabs SDK is not installed (pip install elevenlabs)."
    status_code = getattr(exc, "status_code", None)
    if status_code == 400:
        return (
            "ElevenLabs rejected the request (HTTP 400). Verify that the "
            "configured voice ID and model ID are available to this account."
        )
    if status_code == 401:
        return "ElevenLabs rejected the API key (HTTP 401)."
    if status_code == 403:
        return "ElevenLabs denied access to this voice or model (HTTP 403)."
    if status_code == 404:
        return "ElevenLabs could not find the configured voice (HTTP 404)."
    if status_code == 422:
        return (
            "ElevenLabs could not process the text with the configured voice "
            "settings (HTTP 422)."
        )
    if status_code == 429:
        return "ElevenLabs rate limit reached (HTTP 429); try again later."
    if isinstance(status_code, int) and status_code >= 500:
        return f"ElevenLabs service error (HTTP {status_code})."
    if status_code is not None:
        return f"ElevenLabs request failed (HTTP {status_code})."

    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "ElevenLabs request timed out."
    if any(token in name for token in ("connection", "network", "transport")):
        return "ElevenLabs network request failed."
    return f"ElevenLabs request failed ({_redact_text(type(exc).__name__)})."


def _create_elevenlabs_client(api_key: str, timeout_seconds: float):
    """Create the official SDK client lazily so importing the UI is cheap."""
    from elevenlabs.client import ElevenLabs

    return ElevenLabs(api_key=api_key, timeout=timeout_seconds)


class ElevenLabsVoiceProvider(VoiceProvider):
    """ElevenLabs TTS using the official SDK.

    Audio is generated on a daemon worker thread and played on the Qt GUI
    thread.  Each request has a monotonic generation number: when the user
    starts another utterance or presses stop, any older request is discarded
    even if it completes later.  A Windows SAPI fallback is optional and is
    always labeled as the engine that actually spoke.
    """

    def __init__(self, *, sapi_fallback: Optional["WindowsSapiVoiceProvider"] = None):
        self._api_key: Optional[str] = None
        self._voice_id: Optional[str] = None
        self._model_id: str = DEFAULT_ELEVENLABS_MODEL
        self._output_format: str = DEFAULT_ELEVENLABS_OUTPUT_FORMAT
        self._timeout_seconds: float = 20.0
        self._fallback_enabled: bool = True
        self._status: str = "Initializing ElevenLabs..."
        self._available: bool = False
        self._current_player: Optional[QMediaPlayer] = None
        self._audio_output: Optional[QAudioOutput] = None
        self._temp_file_path: Optional[str] = None
        self._last_engine: Optional[str] = None
        self._sapi_fallback = (
            sapi_fallback if sapi_fallback is not None else WindowsSapiVoiceProvider()
        )
        self._lock = threading.Lock()
        self._generation = 0

        # Qt thread-safe signal for audio playback.  Connections are queued
        # because the emitting thread is not the GUI thread.
        self._signals = AudioPlayerSignals()
        self._signals.play_audio_signal.connect(self._on_play_audio_bytes)

        self.reload_credentials()

    @property
    def last_engine(self) -> Optional[str]:
        """Engine that produced the most recent utterance (or None)."""
        with self._lock:
            return self._last_engine

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def output_format(self) -> str:
        return self._output_format

    def reload_credentials(self) -> None:
        """Load credentials/configuration; this does not make a network call."""
        self._api_key = (get_secret("elevenlabs_api_key") or "").strip() or None
        self._voice_id = (
            (get_secret("elevenlabs_voice_id") or "").strip() or None
        ) or str(get_config_value("elevenlabs_voice_id", "") or "").strip() or None
        self._model_id = (
            os.environ.get("ELEVENLABS_MODEL_ID")
            or str(get_config_value("elevenlabs_model_id", DEFAULT_ELEVENLABS_MODEL))
        ).strip()
        self._output_format = (
            os.environ.get("ELEVENLABS_OUTPUT_FORMAT")
            or str(
                get_config_value(
                    "elevenlabs_output_format", DEFAULT_ELEVENLABS_OUTPUT_FORMAT
                )
            )
        ).strip()
        self._fallback_enabled = bool(
            get_config_value("voice_sapi_fallback", True)
        )
        try:
            self._timeout_seconds = max(
                1.0, float(get_config_value("elevenlabs_timeout_seconds", 20.0))
            )
        except (TypeError, ValueError):
            self._timeout_seconds = 20.0

        if not self._api_key:
            self._available = False
            self._status = (
                "ElevenLabs API Key missing (configure it in Settings or "
                "ELEVENLABS_API_KEY)"
            )
        elif not self._voice_id:
            self._available = False
            self._status = (
                "ElevenLabs Voice ID missing (configure it in Settings or "
                "ELEVENLABS_VOICE_ID)"
            )
        elif not self._model_id:
            self._available = False
            self._status = "ElevenLabs model ID is empty."
        elif not (
            self._output_format.startswith("mp3_")
            or self._output_format.startswith("wav_")
        ):
            self._available = False
            self._status = (
                "ElevenLabs output format must be an mp3_* or wav_* format "
                f"(got {_redact_text(self._output_format, 60)!r})."
            )
        else:
            self._available = True
            masked_id = (
                self._voice_id[:6] + "..."
                if len(self._voice_id) > 6
                else self._voice_id
            )
            # Credentials are configured, but only a real request can prove
            # that the account can use this voice/model.
            self._status = (
                f"ElevenLabs configured (Voice ID: {masked_id}); "
                "not yet validated by a request"
            )

    def _next_generation(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _set_engine(self, engine: Optional[str]) -> None:
        with self._lock:
            self._last_engine = engine

    def _set_status(self, message: str) -> None:
        # Worker threads update this simple immutable value directly.  A queued
        # Qt signal would let a stale "generating" status overwrite "stopped"
        # after the event loop finally processed it.
        with self._lock:
            self._status = message

    def _emit_status(self, message: str) -> None:
        self._set_status(message)

    def speak(self, text: str) -> None:
        """Generate and play one utterance, replacing any current speech."""
        clean_text = (text or "").strip()
        if not clean_text:
            return

        generation = self._next_generation()
        self._set_engine(None)

        if not self.is_available():
            reason = self.get_status()
            logger.warning("ElevenLabs TTS unavailable: %s", reason)
            self._fall_back(clean_text, generation, reason)
            return

        def _worker() -> None:
            if not self._is_current(generation):
                return
            self._emit_status("ElevenLabs: generating audio...")
            try:
                client = _create_elevenlabs_client(
                    self._api_key or "", self._timeout_seconds
                )
                from elevenlabs.types import VoiceSettings

                audio = client.text_to_speech.convert(
                    voice_id=self._voice_id or "",
                    text=clean_text,
                    model_id=self._model_id,
                    output_format=self._output_format,
                    voice_settings=VoiceSettings(
                        stability=0.5,
                        similarity_boost=0.75,
                        use_speaker_boost=True,
                    ),
                )
                if isinstance(audio, (bytes, bytearray)):
                    audio_bytes = bytes(audio)
                else:
                    audio_bytes = b"".join(
                        bytes(chunk) for chunk in (audio or []) if chunk
                    )
            except Exception as exc:  # noqa: BLE001 - provider boundary
                if not self._is_current(generation):
                    return
                reason = _describe_elevenlabs_error(exc)
                logger.error("ElevenLabs TTS failed: %s", reason)
                self._emit_status(f"{reason} Falling back to Windows SAPI." if self._fallback_enabled else f"{reason} No fallback enabled.")
                self._fall_back(clean_text, generation, reason)
                return

            if not self._is_current(generation):
                return
            if not audio_bytes:
                reason = "ElevenLabs returned no audio data."
                logger.error(reason)
                self._fall_back(clean_text, generation, reason)
                return

            # Let the GUI thread own QMediaPlayer.  The generation is checked
            # again there in case stop() happened during the network call.
            self._deliver_audio(generation, audio_bytes)

        worker = threading.Thread(
            target=_worker,
            daemon=True,
            name="ElevenLabsTTS",
        )
        worker.start()

    def _fall_back(self, text: str, generation: int, reason: str) -> None:
        """Use SAPI only when enabled/available, and label it explicitly."""
        if not self._is_current(generation):
            return
        if not self._fallback_enabled:
            self._emit_status(f"{reason} No fallback enabled.")
            return
        if not self._sapi_fallback.is_available():
            self._emit_status(f"{reason} Windows SAPI fallback is unavailable.")
            return

        self._set_engine("windows_sapi")
        self._sapi_fallback.speak(text)
        self._emit_status(f"{reason} Using Windows SAPI fallback.")

    def _deliver_audio(self, generation: int, audio_bytes: bytes) -> None:
        """Hand generated audio to the Qt main thread."""
        self._signals.play_audio_signal.emit((generation, audio_bytes))

    def _dispose_player(self) -> None:
        """Release the current QMediaPlayer before deleting its temp file."""
        player, output = self._current_player, self._audio_output
        self._current_player = None
        self._audio_output = None
        if player is not None:
            try:
                player.stop()
                player.setSource(QUrl())
                player.setAudioOutput(None)
                player.deleteLater()
            except Exception as exc:  # noqa: BLE001 - Qt teardown
                logger.debug("Could not release ElevenLabs player: %s", exc)
        if output is not None:
            try:
                output.deleteLater()
            except Exception:
                pass

    @Slot(object)
    def _on_play_audio_bytes(self, payload: object) -> None:
        """Create and start playback on the GUI thread."""
        try:
            generation, audio_bytes = payload  # type: ignore[misc]
        except (TypeError, ValueError):
            logger.error("Ignoring malformed ElevenLabs playback payload.")
            return
        if not isinstance(generation, int) or not isinstance(audio_bytes, bytes):
            logger.error("Ignoring malformed ElevenLabs playback payload.")
            return
        if not self._is_current(generation):
            return

        try:
            self._dispose_player()
            self._cleanup_temp_file()

            suffix = ".wav" if self._output_format.startswith("wav_") else ".mp3"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                handle.write(audio_bytes)
                temp_path = handle.name
            self._temp_file_path = temp_path

            player = QMediaPlayer()
            output = QAudioOutput()
            player.setAudioOutput(output)
            output.setVolume(1.0)
            player.mediaStatusChanged.connect(self._on_media_status_changed)
            player.errorOccurred.connect(self._on_media_error)
            player.setSource(QUrl.fromLocalFile(temp_path))
            self._current_player = player
            self._audio_output = output
            self._set_engine("elevenlabs")
            player.play()
            self._status = "ElevenLabs: playing audio."
        except Exception as exc:  # noqa: BLE001 - Qt/audio boundary
            logger.error(
                "ElevenLabs audio playback failed: %s", _redact_text(exc)
            )
            self._set_engine(None)
            self._dispose_player()
            self._cleanup_temp_file()
            # Do not fall back here: this method no longer has the source text,
            # and speaking an empty string while reporting SAPI fallback would
            # be a false success.
            self._status = f"ElevenLabs playback error: {_redact_text(exc)}"

    def _on_media_status_changed(self, status) -> None:
        """Release resources when the current media reaches a terminal state."""
        if status not in (
            QMediaPlayer.MediaStatus.EndOfMedia,
            QMediaPlayer.MediaStatus.InvalidMedia,
        ):
            return
        if self.sender() is not self._current_player:
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._status = "ElevenLabs: playback finished."
        else:
            self._status = "ElevenLabs: playback failed (invalid media)."
        self._dispose_player()
        self._cleanup_temp_file()

    def _on_media_error(self, error, error_string: str = "") -> None:
        if self.sender() is not self._current_player:
            return
        logger.error(
            "ElevenLabs media error: %s %s",
            getattr(error, "name", error),
            _redact_text(error_string),
        )
        self._status = "ElevenLabs: playback error."
        self._dispose_player()
        self._cleanup_temp_file()

    def _cleanup_temp_file(self) -> None:
        """Remove the temporary audio file if the player has released it."""
        path = self._temp_file_path
        self._temp_file_path = None
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError as exc:
                logger.debug("Could not delete temp audio file: %s", exc)

    def stop(self) -> None:
        """Cancel pending generation, stop playback, and clean up audio."""
        self._next_generation()  # invalidates any in-flight worker
        self._dispose_player()
        self._sapi_fallback.stop()
        self._cleanup_temp_file()
        self._set_engine(None)
        if self._available:
            masked_id = (
                self._voice_id[:6] + "..."
                if self._voice_id and len(self._voice_id) > 6
                else self._voice_id
            )
            self._status = (
                f"ElevenLabs configured (Voice ID: {masked_id}); stopped"
            )
        else:
            self._status = "ElevenLabs TTS stopped."

    def is_available(self) -> bool:
        return self._available

    def get_status(self) -> str:
        return self._status


class WindowsSapiVoiceProvider(VoiceProvider):
    """
    Native Windows SAPI provider using standard Windows COM / SAPI.
    Zero external dependencies, completely non-blocking via worker threads.
    """

    def __init__(self):
        self._sapi = None
        self._available = False
        self._status = "Initializing..."
        self._lock = threading.Lock()
        self._check_availability()

    def _check_availability(self) -> None:
        if sys.platform != "win32":
            self._available = False
            self._status = "SAPI only available on Windows"
            return

        try:
            # Test if SAPI is accessible via win32com or pythoncom
            import win32com.client
            # Attempt to create SAPI object
            test_voice = win32com.client.Dispatch("SAPI.SpVoice")
            self._sapi = test_voice
            self._available = True
            self._status = "Windows SAPI TTS ready"
            logger.info("Windows SAPI Voice Provider initialized.")
        except Exception as e:
            logger.debug(f"win32com SAPI not available: {e}. Voice fallback enabled.")
            self._available = False
            self._status = f"Windows SAPI not initialized: {e}"

    def speak(self, text: str) -> None:
        clean_text = (text or "").strip()
        if not clean_text or not self._available or not self._sapi:
            return

        def _worker():
            try:
                # Flag 1 = SVSFlagsAsync (Async speech in SAPI)
                with self._lock:
                    if self._sapi:
                        self._sapi.Speak(clean_text, 1)
            except Exception as e:
                logger.error(f"SAPI speak error: {e}")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def stop(self) -> None:
        if not self._available or not self._sapi:
            return
        try:
            with self._lock:
                if self._sapi:
                    # Flag 2 = SVSFPurgeBeforeSpeak with empty string cancels pending audio
                    self._sapi.Speak("", 2)
        except Exception as e:
            logger.debug(f"SAPI stop error: {e}")

    def is_available(self) -> bool:
        return self._available

    def get_status(self) -> str:
        return self._status


class VoiceManager:
    """
    Coordinates active voice provider based on configuration.
    """

    def __init__(self):
        self._provider: VoiceProvider = DisabledVoiceProvider()
        self.reload_provider()

    def reload_provider(self) -> None:
        """Reloads voice provider from configuration."""
        try:
            self._provider.stop()
        except Exception as exc:  # noqa: BLE001 - provider teardown
            logger.debug("Could not stop previous voice provider: %s", exc)

        provider_name = str(
            get_config_value("voice_provider", "disabled") or "disabled"
        ).lower()
        if provider_name == "elevenlabs":
            self._provider = ElevenLabsVoiceProvider()
        elif provider_name == "windows_sapi":
            self._provider = WindowsSapiVoiceProvider()
        elif provider_name == "mock":
            self._provider = MockVoiceProvider()
        else:
            self._provider = DisabledVoiceProvider()
        logger.info(f"VoiceManager active provider: {self._provider.__class__.__name__}")

    def set_provider_type(self, provider_type: str) -> None:
        save_config({"voice_provider": provider_type})
        self.reload_provider()

    def speak(self, text: str) -> None:
        # The provider owns its availability policy (including an explicitly
        # labeled SAPI fallback).  Do not silently drop speech just because a
        # credential is missing.
        self._provider.speak(text)

    def stop(self) -> None:
        self._provider.stop()

    def is_available(self) -> bool:
        return self._provider.is_available()

    def get_status(self) -> str:
        return self._provider.get_status()

    @property
    def last_engine(self) -> Optional[str]:
        return getattr(self._provider, "last_engine", None)
