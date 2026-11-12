# Create a local voice from WAV

The user approved implementing the researched WAV → voice pack → preview → use flow.
Work in this checkout so the local preview can use it.

1. Add tests for WAV validation/resampling, bounded single-job generation, saved job status,
   failed/interrupted jobs, preview access and explicit activation. Watch the new tests fail.
2. Add local encoder orchestration and a background creation job. Keep raw recordings only
   in memory; persist draft packs, preview WAV and status. Reuse the existing greeting model
   under its speech lock for preview generation, without playing audio or calling Claude.
3. Add authenticated creation/status/preview/use routes and a Settings form with polling,
   browser audio preview and a separate Use action. Reuse existing voice installation and
   server-to-camera transfer; remote selection still waits for successful installation.
4. Copy the already verified official MOSS encoder files into models/ZeroTTS/onnx/codec.
   No new dependencies or inference-time external API calls. Generation on an admin server
   requires the ZeroTTS and MOSS model files on that server too.
5. Update docs, run full tests, review changes, verify real creation/preview/activation using
   a temporary app, and restart localhost preview without changing the user's selected voice.

Scope: WAV only, 3–30 seconds, one or two channels, at most 25 MB. One creation job per
process. A restart marks an interrupted job failed; finished drafts survive. No training,
recording widget, transcription, pitch controls, automatic voice substitution or model uploads.
