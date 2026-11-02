"""
Microphone Recording and Speech-to-Text (STT) Service for S.E.N.T.I.A.L.
Handles PySide6 QtMultimedia audio capture, push-to-talk recording, cancellation,
temporary audio cleanup, and Gemini multimodal speech-to-text transcription.
"""

import io
import logging
import os
import tempfile
import wave
from typing import Optional

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, Signal, Slot
from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices

logger = logging.getLogger(__name__)


def check_microphone_availability() -> bool:
    """Returns True if a valid audio input device (microphone) is connected."""
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if not app:
            app = QApplication.instance() or QApplication([])

        inputs = QMediaDevices.audioInputs()
        return bool(inputs and len(inputs) > 0)
    except Exception as e:
        logger.debug(f"Error checking microphone devices: {e}")
        return False


def create_standard_audio_format() -> QAudioFormat:
    """Returns standard 16kHz Mono 16-bit PCM audio format for speech recognition."""
    fmt = QAudioFormat()
    fmt.setSampleRate(16000)
    fmt.setChannelCount(1)
    fmt.setSampleFormat(QAudioFormat.Int16)
    return fmt


def _audio_sample_width(audio_format: QAudioFormat) -> int:
    """Return the PCM container width for a Qt sample format.

    Only integer PCM is accepted. Wrapping float samples in a PCM WAV header
    would produce corrupt audio while claiming transcription-ready input.
    """
    sample_format = audio_format.sampleFormat()
    widths = {
        QAudioFormat.UInt8: 1,
        QAudioFormat.Int16: 2,
        QAudioFormat.Int32: 4,
    }
    width = widths.get(sample_format)
    if width is None:
        raise RuntimeError(
            f"Unsupported microphone sample format '{sample_format}'. "
            "Integer PCM (8/16/32-bit) is required."
        )
    return width


def _select_supported_format(device) -> QAudioFormat:
    """Negotiate an integer-PCM format the actual input device supports."""
    if not hasattr(device, "isFormatSupported"):
        return create_standard_audio_format()

    candidates = [create_standard_audio_format()]
    for rate, channels in ((48000, 1), (44100, 1), (48000, 2), (44100, 2)):
        candidate = QAudioFormat()
        candidate.setSampleRate(rate)
        candidate.setChannelCount(channels)
        candidate.setSampleFormat(QAudioFormat.Int16)
        candidates.append(candidate)

    for candidate in candidates:
        try:
            if device.isFormatSupported(candidate):
                return candidate
        except Exception:
            continue

    preferred = device.preferredFormat()
    _audio_sample_width(preferred)  # raises for float/non-PCM
    return preferred


class MicrophoneService(QObject):
    """
    Dedicated service for managing microphone capture, push-to-talk recording,
    cancellation, and audio stream processing.
    """

    recording_started = Signal()
    recording_stopped = Signal(bytes)  # Emits WAV audio bytes
    recording_cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._audio_source: Optional[QAudioSource] = None
        self._buffer: Optional[QBuffer] = None
        self._is_recording: bool = False
        self._format = create_standard_audio_format()

    def is_recording(self) -> bool:
        return self._is_recording

    def has_microphone(self) -> bool:
        return check_microphone_availability()

    def _release_audio_resources(self) -> None:
        """Stop and release Qt audio objects exactly once."""
        source, buffer = self._audio_source, self._buffer
        self._audio_source = None
        self._buffer = None
        if source is not None:
            try:
                source.stop()
            except Exception:
                pass
            try:
                source.deleteLater()
            except Exception:
                pass
        if buffer is not None:
            try:
                buffer.close()
            except Exception:
                pass
            try:
                buffer.deleteLater()
            except Exception:
                pass

    @Slot()
    def start_recording(self) -> bool:
        """Starts capturing microphone audio."""
        if self._is_recording:
            return True

        if not self.has_microphone():
            logger.warning("No microphone input device detected.")
            return False

        try:
            default_device = QMediaDevices.defaultAudioInput()
            if not default_device or getattr(default_device, "isNull", lambda: True)():
                logger.warning("No valid default audio input device.")
                self._is_recording = False
                return False

            # Use a format the device actually accepted. The chosen format is
            # later used verbatim for the WAV header.
            self._format = _select_supported_format(default_device)
            _audio_sample_width(self._format)

            self._buffer = QBuffer()
            if not self._buffer.open(QIODevice.ReadWrite):
                raise RuntimeError("Could not open the microphone audio buffer.")

            self._audio_source = QAudioSource(default_device, self._format, self)
            self._audio_source.start(self._buffer)
            self._is_recording = True
            self.recording_started.emit()
            logger.info(
                "Microphone recording started (%s Hz, %s channel(s), %s-bit PCM).",
                self._format.sampleRate(),
                self._format.channelCount(),
                _audio_sample_width(self._format) * 8,
            )
            return True
        except Exception as exc:
            logger.error("Failed to start microphone recording: %s", exc)
            self._is_recording = False
            self._release_audio_resources()
            return False

    @Slot()
    def stop_recording(self) -> bytes:
        """Stops recording and returns WAV bytes using the negotiated format."""
        if not self._is_recording or not self._audio_source or not self._buffer:
            self._is_recording = False
            return b""

        try:
            source = self._audio_source
            buffer = self._buffer
            try:
                source.stop()
            except Exception:
                pass
            self._is_recording = False

            raw_data = bytes(buffer.data().data())
            sample_rate = self._format.sampleRate()
            channels = self._format.channelCount()
            sample_size = _audio_sample_width(self._format)
            self._release_audio_resources()

            wav_bytes = self._pcm_to_wav(
                raw_data,
                channels=channels,
                sample_rate=sample_rate,
                sample_size=sample_size,
            )

            logger.info(
                "Microphone recording stopped (%s bytes captured).", len(wav_bytes)
            )
            self.recording_stopped.emit(wav_bytes)
            return wav_bytes
        except Exception as exc:
            logger.error("Error stopping microphone recording: %s", exc)
            self._is_recording = False
            self._release_audio_resources()
            return b""

    @Slot()
    def cancel_recording(self) -> None:
        """Cancels recording and discards audio buffer."""
        self._is_recording = False
        self._release_audio_resources()
        logger.info("Microphone recording cancelled.")
        self.recording_cancelled.emit()

    def _pcm_to_wav(self, pcm_data: bytes, channels: int, sample_rate: int, sample_size: int) -> bytes:
        """Converts raw PCM audio bytes to WAV byte format."""
        if not pcm_data:
            return b""

        out = io.BytesIO()
        with wave.open(out, "wb") as wf:
            wf.setnchannels(channels if channels > 0 else 1)
            wf.setsampwidth(sample_size)
            wf.setframerate(sample_rate if sample_rate > 0 else 16000)
            wf.writeframes(pcm_data)
        return out.getvalue()


def transcribe_audio_bytes(wav_bytes: bytes, ai_provider) -> str:
    """
    Transcribes audio WAV bytes into text using Google GenAI SDK multimodal vision/audio input.
    """
    if not wav_bytes or len(wav_bytes) < 200:
        logger.debug("Audio bytes empty or too small for transcription.")
        return ""

    if not ai_provider or not ai_provider.is_configured():
        raise RuntimeError("Gemini API key is not configured for Speech-to-Text.")

    if getattr(ai_provider, "_use_modern_sdk", False) and getattr(ai_provider, "_client", None):
        try:
            from google.genai import types

            audio_part = types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav")
            prompt = (
                "Transcribe the spoken audio accurately into plain text. "
                "Do NOT add commentary, explanations, or notes outside the transcript."
            )

            response = ai_provider._client.models.generate_content(
                model=ai_provider.get_current_model(),
                contents=[audio_part, prompt]
            )

            if response and response.text:
                return response.text.strip()
            return ""
        except Exception as e:
            logger.error(f"Gemini audio transcription failed: {e}")
            raise RuntimeError(f"Speech-to-Text error: {e}") from e
    else:
        raise RuntimeError("Modern google-genai SDK client required for speech recognition.")
