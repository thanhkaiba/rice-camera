# Upload a voice folder

User requested folder upload after the custom voice pack instructions. Implement in the current
preview: choose one ready-made ZeroTTS voice folder, upload, validate, install and select it.

1. Test folder uploads, malformed/oversized NPZ archives, unsafe filenames, duplicate names,
   missing/multiple voice files, authentication and settings selection before implementation.
2. Add a small ZeroTTS pack installer under core: bounded NPZ parsing without pickle, exact
   compatible dimensions and finite float32 values, canonical output, atomic folder install with
   no overwriting. Import only voice.npz; previews and metadata are unnecessary for speech.
3. Add the admin POST route and folder selector using the Settings page's external-form pattern.
   For remote Settings, reuse existing upload transfer jobs to install the voice on the camera
   computer and report the result in Audit. This also works when local administration is disabled
   because the server is in charge. Keep the pack on the server as the authoritative uploaded copy.
4. Update voice instructions, run the full suite and request an independent code review. Restart
   and verify localhost preview. Do not create a real custom voice or modify the active voice
   during verification; use temporary tests for uploads.

Ruling: support remote uploads through the existing file/job transport. The camera computer
redirects Settings to the server when following, so a camera-only uploader would be inaccessible.
