"""Speech providers.

Importing this package registers every adapter. Swapping recogniser or voice
is an environment variable, not a code change, and they swap independently.
"""

from app.providers.voice import null, openai_voice  # noqa: F401
from app.providers.voice.base import (
    Speech,
    SpeechError,
    SpeechToText,
    TextToSpeech,
    Transcript,
    get_stt,
    get_tts,
    register_stt,
    register_tts,
    registered_stt,
    registered_tts,
)

__all__ = [
    "Speech",
    "SpeechError",
    "SpeechToText",
    "TextToSpeech",
    "Transcript",
    "get_stt",
    "get_tts",
    "register_stt",
    "register_tts",
    "registered_stt",
    "registered_tts",
]
