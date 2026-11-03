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
DEFAULT_ELEVENLABS_MODEL = "eleven_flash_v2_5"
DEFAULT_ELEVENLABS_VOICE_ID = "pNInz6obpgDQGcFmaJgB"
#: MP3 is accepted by QMediaPlayer on all supported Windows versions.
DEFAULT_ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"

VOICE_VALIDATION_STATE_VALID = "VALID"
VOICE_VALIDATION_STATE_INVALID = "INVALID"
VOICE_VALIDATION_STATE_INACCESSIBLE = "INACCESSIBLE"
VOICE_VALIDATION_STATE_NOT_YET_VALIDATED = "NOT_YET_VALIDATED"


def _normalize_setting(value: object) -> str:
    """Strip whitespace and quote wrappers from loaded settings."""
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text


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


def _collect_api_error_detail(exc: BaseException) -> str:
    """Extract a sanitized API error detail from SDK exceptions."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        values = []
        for key in ("detail", "message", "error", "title"):
            if key in body:
                values.append(str(body[key]))
        for nested in ("errors", "details"):
            if nested in body:
                nested_value = body[nested]
                if isinstance(nested_value, (list, tuple)):
                    values.extend(str(item) for item in nested_value)
                else:
                    values.append(str(nested_value))
        if values:
            return _redact_text(" ".join(values), 400)
    if isinstance(body, (list, tuple)):
        joined = " ".join(str(item) for item in body)
        if joined:
            return _redact_text(joined, 400)
    if isinstance(body, str) and body.strip():
        return _redact_text(body, 400)
    message = str(exc)
    if message and message not in {"None", "headers: None, status_code: None, body: None"}:
        return _redact_text(message, 400)
    return ""


def _describe_voice_validation_error(exc: BaseException) -> str:
    """Return a sanitized validation failure description with the API detail."""
    detail = _collect_api_error_detail(exc).strip()
    base = _describe_elevenlabs_error(exc)
    if detail:
        if detail.startswith(base):
            return detail
        return f"{base} Details: {detail}"
    return base


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
            _normalize_setting(get_secret("elevenlabs_voice_id"))
            or _normalize_setting(get_config_value("elevenlabs_voice_id", DEFAULT_ELEVENLABS_VOICE_ID))
            or DEFAULT_ELEVENLABS_VOICE_ID
        )
        self._model_id = (
            _normalize_setting(os.environ.get("ELEVENLABS_MODEL_ID"))
            or _normalize_setting(
                get_config_value("elevenlabs_model_id", DEFAULT_ELEVENLABS_MODEL)
            )
            or DEFAULT_ELEVENLABS_MODEL
        )
        self._output_format = (
            _normalize_setting(os.environ.get("ELEVENLABS_OUTPUT_FORMAT"))
            or _normalize_setting(
                get_config_value(
                    "elevenlabs_output_format", DEFAULT_ELEVENLABS_OUTPUT_FORMAT
                )
            )
            or DEFAULT_ELEVENLABS_OUTPUT_FORMAT
        )
        self._fallback_enabled = bool(
            get_config_value("voice_sapi_fallback", True)
        )
        try:
            self._timeout_seconds = max(
                1.0, float(get_config_value("elevenlabs_timeout_seconds", 20.0))
            )
        except (TypeError, ValueError):
            self._timeout_seconds = 20.0

        self._voice_validation_state = VOICE_VALIDATION_STATE_NOT_YET_VALIDATED

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
            self._status = self._format_validation_status(
                VOICE_VALIDATION_STATE_NOT_YET_VALIDATED
            )

    @property
    def voice_validation_status(self) -> str:
        with self._lock:
            return self._voice_validation_state

    def _masked_voice_id(self) -> str:
        voice_id = self._voice_id or ""
        if len(voice_id) <= 6:
            return voice_id
        return voice_id[:6] + "..."

    def _format_validation_status(
        self, state: str, detail: str = ""
    ) -> str:
        masked_id = self._masked_voice_id()
        if state == VOICE_VALIDATION_STATE_VALID:
            return f"ElevenLabs voice validation: VALID (Voice ID: {masked_id})."
        if state == VOICE_VALIDATION_STATE_INVALID:
            detail_text = detail.strip() or "The configured voice ID does not exist or was rejected."
            return (
                f"ElevenLabs voice validation: INVALID (Voice ID: {masked_id}) - "
                f"{detail_text}"
            )
        if state == VOICE_VALIDATION_STATE_INACCESSIBLE:
            detail_text = detail.strip() or "This account cannot access the configured voice."
            return (
                f"ElevenLabs voice validation: INACCESSIBLE (Voice ID: {masked_id}) - "
                f"{detail_text}"
            )
        return (
            f"ElevenLabs configured (Voice ID: {masked_id}); "
            "not yet validated by a request"
        )

    def _set_voice_validation_state(self, state: str, detail: str = "") -> None:
        with self._lock:
            self._voice_validation_state = state
        self._set_status(self._format_validation_status(state, detail))

    def _classify_voice_validation_error(self, exc: BaseException) -> str:
        status_code = getattr(exc, "status_code", None)
        if status_code == 403:
            return VOICE_VALIDATION_STATE_INACCESSIBLE
        return VOICE_VALIDATION_STATE_INVALID

    def _validate_voice_id_with_client(self, client) -> str:
        """Validate the configured voice against the authenticated ElevenLabs account."""
        try:
            client.voices.get(self._voice_id or "")
        except Exception as exc:  # noqa: BLE001 - provider boundary
            state = self._classify_voice_validation_error(exc)
            message = _describe_voice_validation_error(exc)
            self._set_voice_validation_state(state, message)
            logger.warning("ElevenLabs voice validation failed: %s", message)
            return state
        self._set_voice_validation_state(
            VOICE_VALIDATION_STATE_VALID,
            "Voice verified against the authenticated ElevenLabs account.",
        )
        return VOICE_VALIDATION_STATE_VALID

    def validate_voice_id(self) -> str:
        """Query the authenticated ElevenLabs account and confirm the voice is usable."""
        if not self._api_key or not self._voice_id or not self.is_available():
            return self.voice_validation_status
        try:
            client = _create_elevenlabs_client(
                self._api_key or "", self._timeout_seconds
            )
            return self._validate_voice_id_with_client(client)
        except Exception as exc:  # noqa: BLE001 - provider boundary
            state = self._classify_voice_validation_error(exc)
            message = _describe_voice_validation_error(exc)
            self._set_voice_validation_state(state, message)
            logger.warning("ElevenLabs voice validation failed: %s", message)
            return state

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
            self._emit_status("ElevenLabs: validating voice...")
            try:
                client = _create_elevenlabs_client(
                    self._api_key or "", self._timeout_seconds
                )
                validation_state = self._validate_voice_id_with_client(client)
                if validation_state != VOICE_VALIDATION_STATE_VALID:
                    if not self._is_current(generation):
                        return
                    reason = self.get_status()
                    logger.warning(
                        "Skipping ElevenLabs synthesis because the configured voice is not valid for this account: %s",
                        reason,
                    )
                    self._fall_back(clean_text, generation, reason)
                    return

                from elevenlabs.types import VoiceSettings

                model_id = self._model_id or DEFAULT_ELEVENLABS_MODEL
                audio = None
                for attempt in range(2):
                    try:
                        audio = client.text_to_speech.convert(
                            voice_id=self._voice_id or "",
                            text=clean_text,
                            model_id=model_id,
                            output_format=self._output_format,
                            voice_settings=VoiceSettings(
                                stability=0.5,
                                similarity_boost=0.75,
                                use_speaker_boost=True,
                            ),
                        )
                        break
                    except Exception as exc:  # noqa: BLE001 - provider boundary
                        status_code = getattr(exc, "status_code", None)
                        if (
                            attempt == 0
                            and status_code == 400
                            and model_id != "eleven_multilingual_v2"
                        ):
                            logger.warning(
                                "ElevenLabs model '%s' rejected the request (HTTP 400); retrying once with fallback model 'eleven_multilingual_v2'.",
                                model_id,
                            )
                            model_id = "eleven_multilingual_v2"
                            continue
                        raise
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
                response = getattr(exc, "response", None)
                response_text = getattr(response, "text", None)
                if response_text is None and response is not None:
                    response_text = getattr(response, "content", None)
                if isinstance(response_text, (bytes, bytearray)):
                    response_text = bytes(response_text).decode("utf-8", "replace")
                logger.error(
                    "ElevenLabs TTS failed: status_code=%s response_text=%s",
                    getattr(exc, "status_code", None),
                    _redact_text(str(response_text or "")),
                )
                self._emit_status(f"{reason} Falling back to Windows SAPI." if self._fallback_enabled else f"{reason} No fallback enabled.")
                self._fall_back(clean_text, generation, reason)
                return

            if not self._is_current(generation):
                return
            self._emit_status("ElevenLabs: generating audio...")
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

    def validate_voice_id(self) -> str:
        validator = getattr(self._provider, "validate_voice_id", None)
        if callable(validator):
            return validator()
        return getattr(self._provider, "voice_validation_status", "NOT_YET_VALIDATED")

    @property
    def voice_validation_status(self) -> str:
        return getattr(self._provider, "voice_validation_status", "NOT_YET_VALIDATED")

    @property
    def last_engine(self) -> Optional[str]:
        return getattr(self._provider, "last_engine", None)
