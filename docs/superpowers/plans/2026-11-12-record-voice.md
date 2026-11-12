# Record a voice in Settings

User requested sample texts and direct microphone recording instead of choosing a file.
Implement in the existing local Settings flow, preserving the WAV upload as an optional fallback.

1. Test browser WAV encoding and recorder lifecycle with Node's built-in test runner: valid PCM,
   mono downmix, 30s cap, short audio rejection, permission denial, stop/error/page-exit cleanup,
   and stale permission results. No production dependency is added.
2. Add one browser script for MediaRecorder capture, conversion with decodeAudioData to a bounded
   PCM16 WAV, local replay and stream/object URL cleanup. Use native browser APIs. HTTPS or localhost
   is required; do not access the microphone until Start is pressed. Stop automatically at 30 seconds.
3. Add three Vietnamese sample passages, microphone selection, Start/Stop and local replay in
   Settings. Feed the recorded Blob to the existing create endpoint only on Create voice. Creation,
   preview and explicit activation remain unchanged; no audio is sent to a third-party service.
4. Run JS tests and full Python tests, review, restart and verify the local UI. Test browser recording
   with a fake microphone if a local browser harness is available; never record real hardware for tests.
