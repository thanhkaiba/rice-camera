# Local Vietnamese greetings

Install the project dependencies. Selecting ZeroTTS downloads the pinned
[ZeroTTS model](https://huggingface.co/zeroweight-ai/ZeroTTS) automatically on camera startup
or first speech. Voice creation also downloads its encoders on first use. Allow about 1 GB
and an Internet connection for initial setup. Files are kept in `models/ZeroTTS/`, beside
the active config, across restarts and app updates. Downloads are checked with SHA-256
before installation; failed downloads are retried on next use. No TTS API key is needed.
Once models are installed, speech and voice creation work offline.

If audio generation reports `No module named 'soundfile'`, install the project requirements
in the camera app’s Python environment (or the standalone app serving Settings), then restart that application. If it instead
reports `cannot load library 'libsndfile.so'`, Python found SoundFile but cannot load its
native Linux library. SoundFile's platform wheels normally bundle that library; a source
installation needs the OS package (on Debian/Ubuntu: `sudo apt-get install libsndfile1`).
For a standalone Docker deployment, install the library in the container performing audio generation.
See [SoundFile installation](https://python-soundfile.readthedocs.io/en/latest/#installation).
Packaged desktop apps need the native library included at build time; the package self-test
checks WAV reading/writing and resampling before release.

Custom packs in `data/voices/` must still be transferred using the uploader or copied separately;
they are never downloaded from a public repository.

In **Settings → Greeting**, select **ZeroTTS** as the Vietnamese speech engine,
choose a voice name, save, and press **Say a test greeting**. The speaker is on the
camera computer; the admin's browser does not play it. The test also calls Claude
when a Claude key is configured, as it did before.

To test other content, use **Try any text with ZeroTTS**. Choose a voice in the dropdown,
enter up to 500 characters, and press **Generate audio**. Press Play in the audio player
when ready. This uses the current dropdown selection without saving settings, calls no
Claude API, and plays only in the browser. Each output is capped at 20 seconds, so longer
text may be cut short. On a remote portal, the server queues the text and selected voice for
the camera computer, which generates WAV audio without playing its speaker. The browser
polls for the result; no long HTTP request waits for model loading or synthesis. The server
does not load or download ZeroTTS for text previews. Auto is resolved on the camera using
its daily choice, and custom packs need only be installed on the camera.

Update both the server and camera app before using remote text previews. Keep the camera
online; it picks up the request on its next sync (normally within 30 seconds). A preview
expires after two minutes; only one can be pending at a time. Text and audio are held
temporarily in the delivery queue until delivery or expiry cleanup, and returned WAVs are
not cached by the browser. A server restart discards pending browser results; generate
again. HTTP errors show their actual status code and camera errors include the cause.
The camera uploads WAV audio in chunks of at most 512 KB, below nginx’s default 1 MB
request body limit, with a 4 MB limit for the complete preview. Audio quality is unchanged.

Presets: `maichi`, `baotrang`, `kimoanh`, `hamy`, `giahuy`, `huuduc`, `quangminh`, `tiendat`.
English greetings keep the configured macOS voice. The speed setting applies only to macOS.
Existing installations keep macOS as their default engine.

The **ZeroTTS voice** dropdown lists the presets and packs found directly under the
configured **Custom voice folder** (`<name>/voice.npz`). Save a changed folder to refresh
the list. On the remote portal, voices installed only on the camera computer appear after
its next heartbeat and a page reload. The current selection is kept even if its pack is missing.
On the remote portal, server-only custom packs are marked **awaiting camera confirmation**
and cannot be selected or saved until the camera reports them. Downloadable presets remain
selectable. Upload and camera acknowledgment continue to use the existing transfer flow.

Choose **Auto · change voice every day** to rotate alphabetically through installed voices,
including custom packs. One voice is kept for the whole local calendar day in the configured
time zone and remembered across restarts. The next day that speech or status is requested,
the next voice is selected. Adding a pack keeps today's choice; removing the chosen pack
selects another available voice. With one pack, Auto keeps that voice. English keeps its
configured macOS voice. `auto` is reserved and cannot be used as a new custom pack name.

The model is loaded once on first use with two CPU inference threads and reused.
Generation and playback are serialized with test requests. Audio is generated as a complete
utterance before playback, capped at 20 seconds of generated audio; the first model load and
generation take additional time. Camera processing stays on its existing thread.
Missing model/voice files, synthesis errors and speaker errors use the existing greeting error
reporting; the engine does not silently substitute another person's voice.

## Custom voice

### Create from a recording, entirely locally

In **Settings → Greeting → Create a voice from a recording**:

1. Enter a new voice name and choose one of the three Vietnamese sample passages.
2. Select your microphone and press **Start recording**. Allow microphone access when asked.
3. Read the passage naturally, at a steady distance from the microphone, for about 10–20 seconds.
4. Press **Stop recording** and listen to your recording in the first audio player. Press
   **Start recording** again if needed. Recording stops automatically at 30 seconds and when
   you leave or hide the page.
5. Press **Create voice** only when the recording sounds clear.

Microphone capture requires **HTTPS or localhost**. Use `http://localhost:8000` on this computer;
plain HTTP on a LAN IP does not grant browser microphone access. Device names may only appear
after the browser grants permission. Recording starts only when you press Start, and is kept in
the browser until Create voice sends its WAV to this app. No external voice service receives it.

Alternatively, expand **Or choose an existing WAV file** and choose a clean **3–30 second WAV**,
mono or stereo, at most 25 MB. You do not need to create or select a file when using the microphone.

On the remote portal, generation runs on the camera computer after its next sync. The
server only validates the recording and stores the returned draft; it does not load or
download speech or encoder models. A standalone camera app still creates drafts locally.
The page shows progress and an audio player when ready. **Listen**, then press **Use this voice** to install
and select it. Creation alone leaves the active voice unchanged.

The standalone camera app keeps the uploaded recording in RAM. The remote portal temporarily
queues the reference WAV under `data/uploads/` so the authenticated camera can fetch it;
it removes the recording after completion or expiry cleanup. Creation expires after ten
minutes. The generated pack and sample WAV are stored under `voice-drafts/`. One creation
job runs at a time; a finished draft survives a restart. An interrupted job needs a new upload.
The preview plays in the browser; the existing test greeting plays on the camera computer.

No external voice service, Claude call or training is used for creation. The camera computer
needs the local ZeroTTS model plus two MOSS audio encoder files; they download there on
first use. See [local model setup](voice-local.md). Update both server and camera before use.
Using the created voice reuses the server-to-camera transfer below. Reference recordings
are still sent from the browser as one multipart upload (up to 25 MB); if nginx rejects that
upload, the page reports HTTP 413. The existing proxy upload limit must allow the recording.

### Import a finished voice pack

Obtain a compatible ZeroTTS voice pack using your own or a consenting speaker's recording.
The [official workflow](https://github.com/zeroweight-ai/ZeroTTS/blob/main/docs/VOICES.md)
uses https://platform.zeroweight.ai/audio to create and download the pack. Creating the pack
there uploads the recording; speaking with the downloaded pack happens locally.

Unpack the downloaded ZIP on your computer. In **Settings → Greeting → Upload voice.npz**,
enter a new **Voice name**, choose `voice.npz` inside the voice folder, then press
**Upload and use voice**. No ZIP is needed; the file is limited to 128 KB.
Alternatively, expand **Or upload an entire voice folder**, choose the folder containing
`voice.npz`, then press its **Upload and use voice** button.
The folder name becomes the voice name (spaces are converted to underscores). The app checks
the pack, stores its `voice.npz` in `data/voices`, and selects it automatically. Existing names
are protected: rename the folder if that name is already installed. Other files in the folder,
such as previews and metadata, are not imported. Choose one folder, at most 10 MB total.
This installs a finished voice pack; it does not turn WAV recordings into voices.

On the remote admin server, the pack is also transferred to the camera computer on the next
sync. Update both installations to a version supporting voice uploads first. The Change log
reports whether installation on the office computer succeeded. A queued pack needs that
computer online before it can speak. Remote uploads select the new voice only after that
computer confirms successful installation; failures keep the previous speech settings.

Alternatively, copy the pack to the **camera computer** manually:

```
data/voices/office/
  voice.npz
  meta.json       # optional
  preview.wav     # optional
```

Set **Custom voice folder** to `data/voices`, **ZeroTTS voice** to `office`, save and test.
Paths are relative to the project's config directory, or absolute paths on the camera computer.
Custom names override presets with the same name. Missing model files download automatically on the
camera computer. Packs copied manually are not transferred by settings sync;
use the folder uploader for transfer from the server. Back up custom packs.

The project's WAV creation action connects the audio and voice encoders directly through ONNX
Runtime; it does not depend on a cloning API in the ZeroTTS SDK. Browser enrolment guidance
continues to use the browser's English speech engine.
