"""The speech layer.

What matters here is not that a particular vendor transcribes well — that is
the vendor's problem and testing it would test the network. It is that the
boundary holds: the ports are satisfied, a swap changes nothing above them,
the browser's own recording format is accepted whatever it turns out to be,
and the failures a waiting room actually produces — silence, noise, a
misfired button — are ordinary outcomes rather than errors.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.providers.voice import (
    SpeechError,
    SpeechToText,
    TextToSpeech,
    Transcript,
    get_stt,
    get_tts,
    registered_stt,
    registered_tts,
)
from app.providers.voice.null import NullSpeechToText, NullTextToSpeech
from app.providers.voice.openai_voice import OpenAISpeechToText, OpenAITextToSpeech

AUDIO = b"\x1aE\xdf\xa3fake opus payload"


class FakeOpenAI:
    """Captures the request and returns what the SDK would."""

    def __init__(self, *, text: str = "I need a cleaning", audio: bytes = b"mp3") -> None:
        self.transcribe_request: dict[str, Any] = {}
        self.speech_request: dict[str, Any] = {}
        outer = self

        class Transcriptions:
            async def create(self, **kwargs: Any) -> Any:
                outer.transcribe_request = kwargs
                return text

        class Speech:
            async def create(self, **kwargs: Any) -> Any:
                outer.speech_request = kwargs
                return SimpleNamespace(content=audio)

        self.audio = SimpleNamespace(transcriptions=Transcriptions(), speech=Speech())


# --- the boundary -----------------------------------------------------------


async def test_every_adapter_satisfies_the_port() -> None:
    """Structural, like the calendar and model ports.

    Adding a vendor is one class and an environment variable. This fails if a
    new adapter drifts from the shape everything above it relies on.
    """
    for stt in (NullSpeechToText(), OpenAISpeechToText(client=FakeOpenAI())):
        assert isinstance(stt, SpeechToText)
    for tts in (NullTextToSpeech(), OpenAITextToSpeech(client=FakeOpenAI())):
        assert isinstance(tts, TextToSpeech)

    assert "null" in registered_stt() and "openai" in registered_stt()
    assert "null" in registered_tts() and "openai" in registered_tts()


async def test_recogniser_and_voice_are_chosen_independently() -> None:
    """Two purchases, not one.

    A practice may want a cheap transcriber and a good-sounding voice. Binding
    them together would make that one decision instead of two.
    """
    assert get_stt("null").name == "null"
    assert get_tts("null").name == "null"
    assert isinstance(get_stt("openai", client=FakeOpenAI()), OpenAISpeechToText)


async def test_an_unregistered_provider_names_the_ones_that_exist() -> None:
    with pytest.raises(SpeechError) as exc:
        get_stt("whisper-on-a-pi")
    assert "available:" in str(exc.value) and "openai" in str(exc.value)


# --- what the browser actually hands us -------------------------------------


@pytest.mark.parametrize(
    ("media_type", "extension"),
    [
        ("audio/webm;codecs=opus", "webm"),  # Chrome
        ("audio/mp4", "mp4"),  # Safari
        ("audio/ogg;codecs=opus", "ogg"),  # Firefox
        ("audio/wav", "wav"),
    ],
)
async def test_the_recording_format_survives_the_browser_it_came_from(
    media_type: str, extension: str
) -> None:
    """The API infers the container from the filename, so it has to be right.

    Each browser records something different and none of them asks. Getting
    this wrong is a recogniser that rejects every patient on one platform.
    """
    client = FakeOpenAI()
    await OpenAISpeechToText(client=client).transcribe(AUDIO, media_type=media_type)

    filename, payload, content_type = client.transcribe_request["file"]
    assert filename.endswith(f".{extension}")
    assert payload == AUDIO


async def test_an_unrecognised_media_type_is_attempted_rather_than_refused() -> None:
    """A MIME string we have not seen is not a reason to turn a patient away.

    Browsers add codecs parameters and invent container spellings. Guessing
    webm and letting the provider decide is recoverable; refusing is not.
    """
    client = FakeOpenAI()
    result = await OpenAISpeechToText(client=client).transcribe(
        AUDIO, media_type="audio/x-something-new"
    )
    assert client.transcribe_request["file"][0].endswith(".webm")
    assert result.text == "I need a cleaning"


async def test_recognition_is_pinned_to_english() -> None:
    """The brief says English only.

    Left open, the recogniser decides an accented English sentence is another
    language and hands back a translation, which the agent then acts on.
    """
    client = FakeOpenAI()
    await OpenAISpeechToText(client=client).transcribe(AUDIO, media_type="audio/webm")
    assert client.transcribe_request["language"] == "en"


async def test_a_recording_too_large_is_refused_before_it_is_uploaded() -> None:
    client = FakeOpenAI()
    with pytest.raises(SpeechError):
        await OpenAISpeechToText(client=client).transcribe(
            b"0" * (9 * 1024 * 1024), media_type="audio/webm"
        )
    assert client.transcribe_request == {}, "nothing should have been sent"


# --- the outcomes a waiting room produces -----------------------------------


async def test_hearing_nothing_is_an_outcome_and_not_an_error() -> None:
    """Silence, a misfired button, a room too loud to use.

    This is the most common result of a push-to-talk button and it must not
    raise: the caller shows an empty transcript and the patient tries again.
    """
    result = await OpenAISpeechToText(client=FakeOpenAI(text="   ")).transcribe(
        AUDIO, media_type="audio/webm"
    )
    assert result.text == ""
    assert isinstance(result, Transcript)


async def test_the_null_recogniser_invents_nothing() -> None:
    """Without a key, the button is off — it does not hallucinate a booking.

    A recogniser that returned plausible text when it had heard nothing would
    be worse than useless: the agent would act on it.
    """
    assert (await NullSpeechToText().transcribe(AUDIO, media_type="audio/webm")).text == ""


async def test_a_timeout_is_worth_repeating_and_a_rejected_recording_is_not() -> None:
    """The difference decides what the patient is told.

    "Say that again" is the right answer to a timeout and the wrong answer to
    audio the provider will reject identically every time.
    """

    class Failing(FakeOpenAI):
        def __init__(self, exc: Exception) -> None:
            super().__init__()
            outer_exc = exc

            class Transcriptions:
                async def create(self, **kwargs: Any) -> Any:
                    raise outer_exc

            self.audio = SimpleNamespace(transcriptions=Transcriptions())

    class APITimeoutError(Exception): ...

    class BadRequestError(Exception): ...

    with pytest.raises(SpeechError) as timed_out:
        await OpenAISpeechToText(client=Failing(APITimeoutError("slow"))).transcribe(
            AUDIO, media_type="audio/webm"
        )
    assert timed_out.value.retryable

    with pytest.raises(SpeechError) as rejected:
        await OpenAISpeechToText(client=Failing(BadRequestError("unsupported"))).transcribe(
            AUDIO, media_type="audio/webm"
        )
    assert not rejected.value.retryable


async def test_synthesis_returns_audio_a_browser_can_play_without_negotiation() -> None:
    result = await OpenAITextToSpeech(client=FakeOpenAI(audio=b"ID3mp3")).speak("Held for you.")
    assert result.audio == b"ID3mp3"
    assert result.media_type == "audio/mpeg"


# --- the endpoints ----------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    os.environ["STT_PROVIDER"] = "null"
    os.environ["TTS_PROVIDER"] = "null"
    from app.config import get_settings

    get_settings.cache_clear()
    import app.api.routes as routes

    routes.settings = get_settings()
    from app.main import app

    with TestClient(app) as c:
        yield c
    os.environ.pop("STT_PROVIDER", None)
    os.environ.pop("TTS_PROVIDER", None)
    get_settings.cache_clear()


def test_the_client_can_ask_whether_the_microphone_is_worth_offering(client) -> None:
    """A button that looks live and is not is worse than no button."""
    body = client.get("/api/voice").json()
    assert body["available"] is True
    assert body["stt"] == "null"


def test_an_empty_recording_is_rejected_with_something_a_patient_can_read(client) -> None:
    response = client.post("/api/voice/transcribe", files={"audio": ("s.webm", b"", "audio/webm")})
    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()


def test_synthesis_returns_playable_bytes_and_is_never_cached(client) -> None:
    """No-store because the audio carries the patient's appointment details."""
    response = client.post("/api/voice/speak", json={"text": "Held for you."})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/")
    assert response.headers["cache-control"] == "no-store"
    assert response.content


def test_an_essay_cannot_be_turned_into_an_audio_bill(client) -> None:
    response = client.post("/api/voice/speak", json={"text": "a" * 5000})
    assert response.status_code == 422
