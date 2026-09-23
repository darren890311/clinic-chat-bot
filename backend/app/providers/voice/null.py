"""Speech that does not leave the machine.

The fallback when no key is configured, and what the tests run against. A
missing `OPENAI_API_KEY` should look like an app whose microphone button is
switched off, not like an app that crashes when you press it — the same
reasoning as the chat endpoint returning a polite unavailable message.

It returns nothing rather than something invented. A recogniser that made up
plausible text would be worse than useless here: the agent would act on it.
"""

from __future__ import annotations

from app.providers.voice.base import (
    Speech,
    Transcript,
    register_stt,
    register_tts,
)

# A one-sample silent WAV. Small enough to inline, real enough that an <audio>
# element accepts it and fires `ended`, so the frontend's playback path is
# exercised by the null provider too.
SILENCE = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x40\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
)


class NullSpeechToText:
    name = "null"

    async def transcribe(self, audio: bytes, *, media_type: str) -> Transcript:
        return Transcript(text="", provider=self.name, model="null")


class NullTextToSpeech:
    name = "null"

    async def speak(self, text: str, *, voice: str | None = None) -> Speech:
        return Speech(audio=SILENCE, media_type="audio/wav", provider=self.name, model="null")


register_stt(NullSpeechToText.name, NullSpeechToText)
register_tts(NullTextToSpeech.name, NullTextToSpeech)
