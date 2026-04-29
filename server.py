from __future__ import annotations

import io
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from cache import cache_key, cache_path
from kokoro import KPipeline
from voices import DEFAULT_VOICE, VOICES, lang_for_voice


APP_DIR = Path(__file__).resolve().parent
CACHE_DIR = APP_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

SAMPLE_RATE = 24000
MAX_CHARS = 20000

_pipelines: Dict[str, KPipeline] = {}


def get_pipeline(lang_code: str) -> KPipeline:
    pipeline = _pipelines.get(lang_code)
    if pipeline is None:
        pipeline = KPipeline(lang_code=lang_code)
        _pipelines[lang_code] = pipeline
    return pipeline


def synthesize(text: str, voice: str, speed: float) -> np.ndarray:
    lang_code = lang_for_voice(voice)
    pipeline = get_pipeline(lang_code)
    generator = pipeline(text, voice=voice, speed=speed, split_pattern=r"\n+")
    chunks = [audio for _, _, audio in generator]
    if not chunks:
        raise ValueError("No audio generated")
    if len(chunks) == 1:
        return chunks[0]
    return np.concatenate(chunks)


def audio_to_wav_bytes(audio: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    sf.write(buffer, audio, SAMPLE_RATE, format="WAV")
    buffer.seek(0)
    return buffer.read()


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1)
    voice: str = Field(DEFAULT_VOICE)
    speed: float = Field(1.0, ge=0.5, le=2.0)
    format: str = Field("wav")


class OpenAITTSRequest(BaseModel):
    model: Optional[str] = None
    input: Optional[str] = None
    text: Optional[str] = None
    voice: Optional[str] = None
    response_format: Optional[str] = None
    speed: Optional[float] = None


app = FastAPI(title="Kokoro TTS", version="0.1.0")


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/voices")
def voices() -> JSONResponse:
    return JSONResponse({"default": DEFAULT_VOICE, "voices": VOICES})


@app.get("/")
def index() -> HTMLResponse:
    html = """<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Kokoro TTS</title>
    <style>
      :root {
        color-scheme: light;
        --bg: #f7f5f2;
        --card: #ffffff;
        --ink: #1b1b1b;
        --muted: #6b6b6b;
        --accent: #1f6feb;
      }
      body {
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
        background: radial-gradient(circle at 20% 20%, #f1efe9, #f7f5f2 40%, #f3f2f0 100%);
        color: var(--ink);
        margin: 0;
        padding: 24px;
      }
      .wrap {
        max-width: 820px;
        margin: 0 auto;
      }
      .card {
        background: var(--card);
        border: 1px solid #e1ded8;
        border-radius: 14px;
        padding: 18px;
        box-shadow: 0 12px 30px rgba(0, 0, 0, 0.06);
      }
      h1 {
        font-size: 24px;
        margin: 0 0 8px 0;
      }
      p {
        color: var(--muted);
        margin: 0 0 16px 0;
      }
      textarea {
        width: 100%;
        height: 180px;
        border-radius: 10px;
        border: 1px solid #d6d2cb;
        padding: 12px;
        font-size: 14px;
        resize: vertical;
        box-sizing: border-box;
      }
      .row {
        display: flex;
        gap: 12px;
        align-items: center;
        margin-top: 12px;
        flex-wrap: wrap;
      }
      select, input {
        border: 1px solid #d6d2cb;
        border-radius: 8px;
        padding: 8px 10px;
        font-size: 14px;
        background: #fff;
      }
      button {
        background: var(--accent);
        color: white;
        border: none;
        border-radius: 10px;
        padding: 10px 16px;
        cursor: pointer;
        font-weight: 600;
      }
      button:disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }
      audio {
        width: 100%;
        margin-top: 12px;
      }
      .status {
        margin-top: 8px;
        color: var(--muted);
        font-size: 12px;
      }
    </style>
  </head>
  <body>
    <div class=\"wrap\">
      <div class=\"card\">
        <h1>Kokoro TTS</h1>
        <p>Local-only text to speech. Enter text, pick a voice, and play.</p>
        <textarea id=\"text\">Hello. This is Kokoro TTS running locally.</textarea>
        <div class=\"row\">
          <label>
            Voice
            <select id=\"voice\"></select>
          </label>
          <label>
            Speed
            <input id=\"speed\" type=\"number\" min=\"0.5\" max=\"2\" step=\"0.1\" value=\"1.0\" />
          </label>
          <button id=\"go\">Generate</button>
        </div>
        <audio id=\"player\" controls></audio>
        <div class=\"status\" id=\"status\"></div>
      </div>
    </div>
    <script>
      const voiceSelect = document.getElementById("voice");
      const statusEl = document.getElementById("status");
      const goBtn = document.getElementById("go");
      const player = document.getElementById("player");

      async function loadVoices() {
        const res = await fetch("/voices");
        const data = await res.json();
        voiceSelect.innerHTML = "";
        for (const voice of data.voices) {
          const opt = document.createElement("option");
          opt.value = voice;
          opt.textContent = voice;
          if (voice === data.default) opt.selected = true;
          voiceSelect.appendChild(opt);
        }
      }

      async function generate() {
        const text = document.getElementById("text").value.trim();
        const voice = voiceSelect.value;
        const speed = parseFloat(document.getElementById("speed").value || "1");
        if (!text) return;
        goBtn.disabled = true;
        statusEl.textContent = "Generating audio...";
        const res = await fetch("/tts", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, voice, speed })
        });
        if (!res.ok) {
          statusEl.textContent = "Error: " + (await res.text());
          goBtn.disabled = false;
          return;
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        player.src = url;
        await player.play();
        statusEl.textContent = "Done.";
        goBtn.disabled = false;
      }

      goBtn.addEventListener("click", generate);
      loadVoices();
    </script>
  </body>
</html>"""
    return HTMLResponse(html)


def validate_text(text: str) -> str:
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")
    if len(text) > MAX_CHARS:
        raise HTTPException(status_code=400, detail=f"Text too long (max {MAX_CHARS} chars)")
    return text


def validate_format(fmt: str) -> str:
    fmt = fmt.lower()
    if fmt != "wav":
        raise HTTPException(status_code=400, detail="Only wav is supported")
    return fmt


def validate_voice(voice: str) -> str:
    if voice not in VOICES:
        raise HTTPException(status_code=400, detail="Unknown voice")
    return voice


@app.post("/tts")
def tts(request: TTSRequest) -> StreamingResponse:
    text = validate_text(request.text)
    voice = validate_voice(request.voice or DEFAULT_VOICE)
    fmt = validate_format(request.format)
    speed = float(request.speed)

    key = cache_key(text, voice, speed, fmt)
    path = cache_path(CACHE_DIR, key, fmt)
    if path.exists():
        return StreamingResponse(path.open("rb"), media_type="audio/wav")

    try:
        audio = synthesize(text, voice, speed)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    data = audio_to_wav_bytes(audio)
    path.write_bytes(data)
    return StreamingResponse(io.BytesIO(data), media_type="audio/wav")


@app.post("/v1/audio/speech")
def openai_tts(request: OpenAITTSRequest) -> StreamingResponse:
    text = request.input or request.text or ""
    text = validate_text(text)
    voice = validate_voice(request.voice or DEFAULT_VOICE)
    fmt = validate_format(request.response_format or "wav")
    speed = float(request.speed or 1.0)

    key = cache_key(text, voice, speed, fmt)
    path = cache_path(CACHE_DIR, key, fmt)
    if path.exists():
        return StreamingResponse(path.open("rb"), media_type="audio/wav")

    try:
        audio = synthesize(text, voice, speed)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    data = audio_to_wav_bytes(audio)
    path.write_bytes(data)
    return StreamingResponse(io.BytesIO(data), media_type="audio/wav")
