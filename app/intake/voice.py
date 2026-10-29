"""Voice notes (design doc 7.3 step 1, 7.9): audio -> transcript through `Role.VOICE`.

The recording goes to Muse Spark Open as an `input_audio` part (the browser records audio/webm
and converts it to 16 kHz mono WAV before upload, because the endpoint takes WAV or MP3). The
transcript then follows the typed-message path unchanged, so a voice note and the same words
typed give the same parse. In mock mode the mock provider "hears" only the committed fixture clips
(`tests/fixtures/voice/transcripts.json`, keyed by the audio's sha256).
"""

import base64

from app.llm.cache import PROMPTS
from app.llm.gateway import LLMGateway
from app.llm.types import LLMRequest, Role

MAX_BYTES = 2_000_000  # about a minute of 16 kHz mono WAV
FORMATS = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
}


class VoiceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def audio_format(content_type: str | None, filename: str | None) -> str:
    fmt = FORMATS.get((content_type or "").split(";")[0].strip().lower())
    if fmt is None and filename:
        fmt = {"wav": "wav", "mp3": "mp3"}.get(filename.rsplit(".", 1)[-1].lower())
    if fmt is None:
        raise VoiceError("AUDIO_FORMAT", "Send the voice note as WAV or MP3.")
    return fmt


def build_request(raw: bytes, fmt: str) -> LLMRequest:
    return LLMRequest(
        role=Role.VOICE,
        messages=[
            {"role": "system", "content": (PROMPTS / "voice.md").read_text(encoding="utf-8")},
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": base64.b64encode(raw).decode(), "format": fmt},
                    },
                ],
            },
        ],
        max_tokens=4000,  # it reasons first; 2,000 ran out once in six trials
        timeout_s=60.0,
    )


async def transcribe(raw: bytes, fmt: str, gateway: LLMGateway) -> str:
    if not raw:
        raise VoiceError("EMPTY_AUDIO", "The voice note is empty.")
    if len(raw) > MAX_BYTES:
        raise VoiceError("AUDIO_TOO_LONG", "Keep voice notes under a minute.")
    resp = await gateway.complete(build_request(raw, fmt))
    text = " ".join((resp.text or "").split()).strip().strip('"“”')
    if not text:
        raise VoiceError(
            "VOICE_UNCLEAR", "Sorry, I could not hear that. Please type it or try again."
        )
    return text
