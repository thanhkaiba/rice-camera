# Creating a ZeroTTS voice locally

Research verified on 2026-11-12: a local reference WAV can be converted into a ZeroTTS
voice pack using ONNX Runtime alone. No PyTorch, training, API key or reference-audio upload
is needed. Downloading the additional model files requires network access once.
This processing path is available in **Settings → Greeting → Create a voice from a recording**.
The page creates a draft in the background, lets you listen in the browser, and installs it only
when you press **Use this voice**. The original recording is not retained by the app.

## Components

1. The project already has `models/ZeroTTS/onnx/voice_encoder.onnx` (about 140 MB).
2. Download only `moss_audio_tokenizer_encode.onnx` (815,775 bytes) and
   `moss_audio_tokenizer_encode.data` (44,507,136 bytes) from the official
   [MOSS Nano ONNX model](https://huggingface.co/OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX).
   Tested revision: `ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae`.
   Keep these two files beside each other. Both encoder file checksums were verified against
   the Hub metadata. All three decoder files from this export have the same SHA-256 hashes
   as the codec already bundled in ZeroTTS.

This checkout already has both files under `models/ZeroTTS/onnx/codec/`. To set up another
computer with the project dependencies installed, run this Python in the project directory:

```python
from huggingface_hub import snapshot_download

snapshot_download(
    "OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX",
    revision="ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae",
    allow_patterns=["moss_audio_tokenizer_encode.onnx", "moss_audio_tokenizer_encode.data"],
    local_dir="models/ZeroTTS/onnx/codec",
)
```

The app now downloads the pinned ZeroTTS model and these two encoder files automatically
on first voice creation, on the computer serving Settings. The manual commands above are
optional for preparing an offline installation. After setup, voice creation needs no network
or external API. Download failures appear in the creation status; retry after restoring Internet.
Only the finished voice pack is transferred to the camera, which downloads its own speech models.

The SDK's statements that cloning is unavailable concern its public API. The downloaded
ZeroTTS weights now include a voice encoder, and the audio encoder needed before it is
available separately from OpenMOSS. The encoder names refer to two different stages.

## Processing

```
Reference recording
  → prepare 48 kHz float32 stereo waveform
  → MOSS audio encoder → discrete audio codes
  → ZeroTTS voice encoder → speaker embedding
  → voice.npz → existing folder uploader / local voice directory
```

Use one consenting speaker, without background music or other voices. Start with a clean
10–20 second WAV. Trim excessive silence. Use at most 30 seconds for this encoder.
Resample non-48-kHz recordings with scipy and duplicate a mono recording into two channels.

Verified ONNX interfaces:

| Stage | Inputs | Outputs |
|---|---|---|
| MOSS audio encoder | `waveform`: float32 `(B, 2, samples)`; `input_lengths`: int32 `(B,)`, in samples | `audio_codes`: int32 `(B, frames, 16)`; `audio_code_lengths`: int32 `(B,)` |
| ZeroTTS voice encoder | `ref_codes`: int64 `(B, 375, 16)`; `ref_lengths`: int64 `(B,)`, in code frames | `voice_emb`: float32 `(B, 10, 768)` |

For one recording, use `B=1`. Take the valid MOSS code frames, check the length is at most
375, cast codes to int64 and pad to 375 frames with zeros. Pass the actual frame count in
`ref_lengths`; samples and code frames are different units. Changing padded frames after
the valid length produced exactly the same embedding in the verification run.

Write an NPZ containing `voice_emb` and scalar int64 `n_voice_queries=10`. For example:

```python
np.savez("voice.npz", voice_emb=embedding,
         n_voice_queries=np.array(10, dtype=np.int64))
```

The embedding must be finite float32 with shape `(1, 10, 768)`. The existing
`attendance.core.voice.install_voice` validates and installs this format. Once generated,
the recording and MOSS audio encoder are not needed for subsequent speech generation.

## Verification performed

Used the bundled `voices/maichi/preview.wav`, a public synthetic preset sample, as reference:

- 6.678 seconds at 48 kHz became 83 code frames.
- Audio encoder load + encoding: 2.43 seconds on this machine, two CPU threads.
- Voice encoder load + extraction: 0.55 seconds.
- The generated 31,262-byte pack passed the existing uploader's installer validation.
- Loading that pack into ZeroTTS and synthesizing a new sentence produced a finite,
  non-silent 2.80-second WAV. Cold TTS load + synthesis took 21.61 seconds.

Temporary research artifacts:

- `/tmp/rice-moss-encoder/`: the two additional encoder files.
- `/tmp/rice-local-voice-research/voices/offline-sample/voice.npz`: generated pack.
- `/tmp/rice-local-voice-research/offline-sample.wav`: generated speech.
- `/tmp/rice-local-voice-research/result.json`: verification measurements.

No application speech settings or active voice were changed by this research. No new Python
dependencies were installed. Speaker similarity and recording quality have not been evaluated
with a real personal recording; successful inference alone does not establish clone quality.

Application behavior: the WAV upload action validates and prepares audio, then performs model
inference on a background thread. It uses the existing greeting model under the speech lock for
the sample, without playing on the office speaker or asking Claude. The admin explicitly chooses
the new voice after listening. Existing installation validation and remote transfer are reused;
remote selection waits for successful camera-side installation. Raw WAV upload streams stay in
memory, bounded by the 25 MB request limit. Draft packs and preview audio are private admin assets.
