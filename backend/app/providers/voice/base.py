"""The speech ports.

Two of them, because recognition and synthesis are separate purchases: a
practice might want a cheap transcriber and a good-sounding voice, or a
transcriber that has heard of dental terminology paired with whatever is
included in their cloud bill. Binding them together would make that one
decision instead of two.

Nothing above this module knows which vendor is answering. In particular the
agent does not: a voice turn is the same text, through the same loop, with the
same tools. The channel changes where the words came from and where the reply
goes, and nothing else. That is the point — if speech could reach the booking
logic by a different path, everything asserted about the chat path would have
to be asserted again about this one.

Audio crosses this boundary as bytes plus a media type, never as a file path
or a provider's own handle. The browser records whatever its platform gives it
— webm/opus on Chrome, mp4 on Safari — and an adapter either accepts that or
is responsible for converting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class SpeechError(RuntimeError):
    """A speech provider failed.

    `retryable` separates a rate limit or a timeout — where the patient can
    simply be asked to say it again — from a rejected recording, where asking
    again produces the same failure and they should be offered the keyboard.
    """

    def __init__(self, provider: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.retryable = retryable


@dataclass(frozen=True)
class Transcript:
    """What was heard, and how sure the provider is.

    `text` may be empty: silence, a misfired button, a room too loud to use.
    That is not an error and callers must handle it as an ordinary outcome,
    because it is the single most common one in a waiting room.
    """

    text: str
    provider: str
    model: str
    # Providers report confidence on different scales, or not at all. None
    # means "not offered", which is different from "not confident" and must
    # not be rendered as a warning to the patient.
    confidence: float | None = None


@dataclass(frozen=True)
class Speech:
    """Synthesised audio, ready to hand to an <audio> element."""

    audio: bytes
    media_type: str
    provider: str
    model: str


@runtime_checkable
class SpeechToText(Protocol):
    name: str

    async def transcribe(self, audio: bytes, *, media_type: str) -> Transcript: ...


@runtime_checkable
class TextToSpeech(Protocol):
    name: str

    async def speak(self, text: str, *, voice: str | None = None) -> Speech: ...


_STT: dict[str, type] = {}
_TTS: dict[str, type] = {}


def register_stt(name: str, factory: type) -> type:
    _STT[name] = factory
    return factory


def register_tts(name: str, factory: type) -> type:
    _TTS[name] = factory
    return factory


def get_stt(name: str, **kwargs: Any) -> SpeechToText:
    try:
        return _STT[name](**kwargs)
    except KeyError:
        raise SpeechError(name, f"no recogniser registered; available: {sorted(_STT)}") from None


def get_tts(name: str, **kwargs: Any) -> TextToSpeech:
    try:
        return _TTS[name](**kwargs)
    except KeyError:
        raise SpeechError(name, f"no synthesiser registered; available: {sorted(_TTS)}") from None


def registered_stt() -> list[str]:
    return sorted(_STT)


def registered_tts() -> list[str]:
    return sorted(_TTS)
