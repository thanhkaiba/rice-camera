# Mac Camera and web server responsibilities

For the live two-machine deployment, put hardware access and model inference on the
Mac Camera. Put administration, shared data and central business actions on the web
server. The location of a button does not decide where its work runs: a Settings
action can ask the Mac to do the work and return its result to the browser.

## Where work belongs

| Responsibility | Mac Camera | Web server / browser |
|---|---|---|
| Camera hardware and video | RTSP/USB access, capture, frame health, live processing | Portal shows status and links to the local camera page |
| Face recognition and enrolment | Detection, tracking, embeddings, recognition, processing uploaded reference photos | Admin chooses people/review decisions, queues photos, stores synced results |
| Attendance events | Detect arrivals, keep a durable offline delivery queue, retry delivery | Ingest/deduplicate events; own canonical attendance data, corrections, reports and late/missing actions |
| Settings and people | Apply synced settings/gallery; keep local detection-area calibration | Admin editing, validation and shared configuration; publish changes to the Mac |
| Greetings | Select daily Auto voice; generate speech and play the office speaker | Edit greeting settings and queue tests; browser plays returned previews |
| Text voice tests | Load/reuse ZeroTTS, synthesize WAV with the selected voice | Queue text/voice, return 202, poll and serve the resulting WAV |
| Create a voice | Run audio/voice encoders and synthesize a draft preview | Validate/temporarily queue the recording; store returned draft; activate only after Use and camera installation acknowledgement |
| Models and runtime caches | Download, verify and keep vision/TTS/encoder models and speech caches | No vision/TTS model download, inference or fallback, including background workers |
| Files | Installed runtime voice packs and camera-side files | Validated packs for transfer, synced photos, finished drafts and short-lived upload/result data |
| Authentication and integrations | Hold the edge credential; use synced rules for local behavior | Admin/viewer authentication, edge-token verification, Base/HR, Google Chat and central scheduled actions |
| Browser hardware | — | Browser requests microphone permission, records the reference and plays previews; it does not run ZeroTTS |

Existing greeting text preparation (including optional Claude/weather requests) stays
with the camera greeter. This convention does not move those calls into the server.
Shared code in `attendance/core/` may be used on both machines; inspect its callers
before deciding where a new operation belongs.

## Communication rules

1. The Mac initiates authenticated HTTPS connections to the server using `EdgeClient`.
   Reuse the existing sync, job and result APIs. Do not require inbound access to the Mac.
2. The server validates input, queues work and returns promptly. Long operations use
   202 plus browser polling, rather than keeping a Gunicorn/nginx request open during
   a model download or synthesis.
3. Camera workers must preserve capture/recognition, heartbeats and event delivery.
   A background browser request alone is insufficient if its camera handler blocks
   the edge delivery loop for minutes.
4. Give jobs deadlines and bounded results. Retried delivery must not repeat completed
   work unnecessarily. Expired jobs must stop blocking later syncs even if the browser
   closes. Never run the model on the server as an offline-camera fallback.
5. Authenticate camera uploads and restrict browser results to authorized admins.
   Validate file type, size and destination; clean temporary recordings/results after
   completion or expiry cleanup. Keep models and speech caches out of the server data
   directory. Finished voice drafts/packs may be retained for explicit use and backup.
6. Respect proxy limits in both directions. Returned WAVs currently use PUT chunks of
   at most 512 KB, with a 4 MB total limit. Reference recordings remain a multipart
   upload of up to 25 MB; nginx must allow that input size. Show HTTP 413/502/504 and
   useful camera errors rather than replacing them with a generic message.
7. Update both implementations for protocol changes and document rollout order. For
   new camera job kinds, update the Mac before exposing the server action; an older
   camera may not understand them. Keep timeout cleanup as a safeguard.

The Mac normally pulls jobs every 30 seconds, so pickup latency is additional to
processing time. Text tests currently expire after two minutes; voice creation after
ten minutes. A server restart discards in-memory text preview results; the browser
must generate again. See [voice behavior](voice.md) for the user-facing flow.

## Code entry points

| Area | Entry points |
|---|---|
| Camera application and link | `attendance/core/pipeline.py`, `attendance/core/edge.py` |
| Camera inference and speech | `attendance/vision/`, `attendance/core/greeting.py`, `attendance/core/voice_creation.py`, `attendance/core/voice_models.py` |
| Server startup and scheduling | `attendance/server.py` |
| Portal actions and authenticated edge API | `attendance/web/settings.py`, `attendance/web/edge_api.py` |
| Server adapter for camera voice creation | `attendance/core/remote_voice_creation.py` |
| Durable job/event storage | `attendance/store/edge.py` |

Standalone camera mode can still host its own admin pages and process local voice
requests. Do not remove that behavior when implementing remote routing. Live two-machine
mode must keep central side effects on the server to avoid duplicate notifications/actions.

For a new feature, identify its executor, data owner, offline behavior and result path
before implementing it. Tests should exercise both ends of remote flows, verify that
server model loading cannot occur, and cover failures, expiry, retries and explicit
activation where applicable.
