# Kokoro TTS
Local-only Kokoro TTS server.  

**Featuring**:  
- windows-wide selectable content TTS (global hotkey)
- web UI 
- [**OpenWebUI**](https://openwebui.com/) integration.
- API (and openai compatible)



## Setup

1. Install Python 3.10+.
2. (Optional) Install espeak-ng for broader language fallback:
   - https://github.com/espeak-ng/espeak-ng/releases
3. Run setup:

```powershell
./setup.ps1
```

## Run (read aloud anything anywhere)

Start the reader app:

```powershell
./reader.ps1
```

**What it does**:
- Auto-starts the **local server** on launch (server runs at: `http://127.0.0.1:8000`).
- Runs in the system tray.
- Default global hotkey: `Ctrl+Alt+S` (customizable).
- Reads selected text from most apps by triggering *copy* and sending text to the local Kokoro server.
- Right-click tray functionalities:
   - `Speak Selected` (default hotkey behaviour) -> read aloud selected text.
   - `Speak Clipboard` -> read aloud the last copied text
   - `Settings` -> change endpoint, hotkey, voice, speed and start/end server.
   - `Start Server` / `Stop Server`
   - `Open Kokoro UI` -> opens the Kokoro demo UI page
- Reader prints live debug logs to the terminal (hotkey events, capture length, request/playback, server start/stop).
---
> *global hotkey listener uses native Windows `RegisterHotKey`,   
which is more reliable across apps than low-level keyboard hooks.*  
> *reader reuses a persistent HTTP session and includes latency logs for TTS response debug.*

## API usage

### Default endpoint

```powershell
$body = @{ text = "Hello from Kokoro"; voice = "af_heart"; speed = 1.0 } | ConvertTo-Json
Invoke-WebRequest -Method Post -ContentType "application/json" -Body $body -OutFile out.wav -Uri http://127.0.0.1:8000/tts
```

### OpenAI-compatible endpoint

```powershell
$body = @{ input = "Hello from Kokoro"; voice = "af_heart"; model = "kokoro" } | ConvertTo-Json
Invoke-WebRequest -Method Post -ContentType "application/json" -Body $body -OutFile out.wav -Uri http://127.0.0.1:8000/v1/audio/speech
```

## CLI usage

```powershell
./speak.ps1 -Text "Hello from Kokoro"
```

## OpenWebUI integration (local)

In *OpenWebUI* `Admin Panel` -> `Settings` -> `Audio`:

- Text-to-Speech Engine: OpenAI
- API Base URL: `http://127.0.0.1:8000/v1` (if docker, use `http://host.docker.internal:8000/v1`)
- API Key: not-needed (you can type anything if a required field)
- TTS Model: kokoro
- TTS Voice: af_heart (or any voice from /voices)


## Voices

List voices:
```
GET http://127.0.0.1:8000/voices
```
Default voice is `af_heart`.

## Notes

- Audio format is `WAV`.
- Text length limit: `20000` characters (you can adjust, but more chars processed requires more effort).
- Cache is stored under `cache/` (generated in root directory).
- You can run the server manually with `./run.ps1`.
- If a hotkey is already used by another app, **Reader** will show a registration failure message
