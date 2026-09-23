"""Recognition and synthesis through the OpenAI SDK.

Chosen because the key was already in the environment for the second LLM
adapter, so voice costs the practice one vendor relationship rather than two.
Nothing here is relied upon anywhere else: both classes satisfy the ports in
`base.py`, and `STT_PROVIDER` / `TTS_PROVIDER` pick them by name.

The transcription model is asked for plain text rather than the verbose JSON
with word timings. Nothing downstream uses timings — the agent gets a
sentence — and the smaller response is one less thing to parse.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.providers.voice.base import (
    Speech,
    SpeechError,
    Transcript,
    register_stt,
    register_tts,
)

# Container formats browsers actually produce, mapped to the filename the API
# infers the format from. Chrome records webm/opus, Safari mp4/aac, Firefox ogg.
EXTENSIONS = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
}

# Small enough that a misfire is not a bill, large enough for anything a
# person says in one breath. A minute of opus is well under this.
MAX_AUDIO_BYTES = 8 * 1024 * 1024


def _client(existing: Any, api_key: str, provider: str) -> Any:
    if existing is not None:
        return existing
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise SpeechError(provider, "the openai package is not installed") from exc
    if not api_key:
        raise SpeechError(provider, "OPENAI_API_KEY is not set")
    return AsyncOpenAI(api_key=api_key, timeout=30.0)


class OpenAISpeechToText:
    name = "openai"

    def __init__(self, *, model: str | None = None, client: Any = None) -> None:
        settings = get_settings()
        self.model = model or settings.stt_model
        self._client = client
        self._api_key = settings.openai_api_key

    async def transcribe(self, audio: bytes, *, media_type: str) -> Transcript:
        if len(audio) > MAX_AUDIO_BYTES:
            raise SpeechError(self.name, "that recording is too long")

        # The API takes a file, and infers the container from its name. The
        # browser tells us what it recorded; a type we do not recognise is
        # sent as webm rather than rejected, because the alternative is
        # refusing a patient over a MIME string we have not seen before.
        base = media_type.split(";")[0].strip().lower()
        extension = EXTENSIONS.get(base, "webm")

        client = _client(self._client, self._api_key, self.name)
        try:
            response = await client.audio.transcriptions.create(
                model=self.model,
                file=(f"speech.{extension}", audio, base or "audio/webm"),
                response_format="text",
                # English only, by the brief. Saying so stops the recogniser
                # deciding an accented English sentence is another language
                # and translating it into something the agent then acts on.
                language="en",
            )
        except Exception as exc:  # noqa: BLE001 - normalised below
            raise SpeechError(self.name, str(exc), retryable=_retryable(exc)) from exc

        text = response if isinstance(response, str) else getattr(response, "text", "")
        return Transcript(text=text.strip(), provider=self.name, model=self.model)


class OpenAITextToSpeech:
    name = "openai"

    def __init__(self, *, model: str | None = None, client: Any = None) -> None:
        settings = get_settings()
        self.model = model or settings.tts_model
        self.voice = settings.tts_voice
        self._client = client
        self._api_key = settings.openai_api_key

    async def speak(self, text: str, *, voice: str | None = None) -> Speech:
        client = _client(self._client, self._api_key, self.name)
        try:
            response = await client.audio.speech.create(
                model=self.model,
                voice=voice or self.voice,
                input=text,
                # MP3 rather than a streaming format: the reply is two or
                # three sentences, and every browser plays it without asking
                # what codecs are available.
                response_format="mp3",
            )
            audio = await response.aread() if hasattr(response, "aread") else response.content
        except Exception as exc:  # noqa: BLE001 - normalised below
            raise SpeechError(self.name, str(exc), retryable=_retryable(exc)) from exc

        return Speech(audio=audio, media_type="audio/mpeg", provider=self.name, model=self.model)


def _retryable(exc: Exception) -> bool:
    """Whether saying it again would plausibly work.

    Matched on the class name rather than by importing the SDK's exception
    types, so this module still imports when the package is absent.
    """
    return type(exc).__name__ in {
        "APITimeoutError",
        "APIConnectionError",
        "RateLimitError",
        "InternalServerError",
    }


register_stt(OpenAISpeechToText.name, OpenAISpeechToText)
register_tts(OpenAITextToSpeech.name, OpenAITextToSpeech)
