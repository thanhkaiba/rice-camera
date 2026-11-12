# Deployment boundaries

Read [docs/architecture.md](docs/architecture.md) before adding a feature that crosses
the camera/server boundary. These rules apply to the live Mac Camera + web server deployment;
keep standalone camera mode working.

- Mac Camera owns camera hardware, video/face inference, enrolment processing, local
  speech, ZeroTTS models, text synthesis and voice creation. Load/download inference
  models only on the camera computer.
- Web server owns the admin portal, authentication, shared settings, canonical attendance
  data/reports and central integrations/scheduled actions. It validates uploads and
  queues camera work; it must not run camera or speech inference in HTTP handlers or
  background workers.
- A feature shown in Settings does not necessarily execute on the server. Reuse
  authenticated edge jobs and result uploads for camera work, with short HTTP requests,
  polling, expiry, retry handling and bounded payloads. Do not connect directly from the
  public server/browser to the Mac or open an inbound camera port.
- Long camera tasks must leave recognition, heartbeats and attendance delivery responsive.
  Heavy processing must not silently fall back to the server when the camera is offline.
- Keep models/voice runtime caches on the Mac; the server may store reference uploads
  temporarily, validated voice packs, preview results and synced photos as needed.
  Clean temporary inputs/results on completion or expiry cleanup.
- Report actual HTTP status and useful failure details. Check both ends of any protocol
  change, including camera upgrades, proxy payload limits and retry/expiry behavior.
- Verify remote flows without loading models on the server, and preserve explicit
  confirmation before activating a newly created voice.
