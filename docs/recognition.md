# Recognition: models, tuning, accuracy

## Model choice

Two backends are supported; `evaluate` compares them on your own photos.

| | `insightface` (default) | `opencv` |
|---|---|---|
| Detector | SCRFD-10G (5 landmarks) | YuNet (5 landmarks) |
| Recogniser | ArcFace ResNet50, WebFace600K | SFace |
| Accuracy | Higher, notably on blurred and small faces | Lower |
| Speed (CPU) | ~200 ms per frame at 640 px | ~20 ms per frame |
| Licence | **Non-commercial research only.** Commercial use needs a licence from InsightFace (contact@insightface.ai) | MIT / Apache 2.0, free for commercial use |

Company attendance is a commercial use: either get an InsightFace licence or set `backend: opencv` in `config.yaml`. Run `evaluate` with both first to see the difference on your photos.

Other design decisions:
- **Faces come from a face detector, not YOLO.** A generic YOLO model gives no facial landmarks (needed for alignment). A person detector is only used, optionally, to follow people in groups (below).
- **No `insightface` package.** The ONNX models run directly on onnxruntime, so nothing needs compiling.
- **No cloud.** Recognition stays on the office computer and images never leave it.
- **Speed on a Mac: CoreML, not a smaller model.** With `use_gpu: true` the recogniser runs through CoreML: embeddings ~40 % faster on an Intel i5 with the same results. MobileFaceNet (branch `codex/mobilefacenet-trial`) was ~3.7x faster with 8 faces but recognised fewer hard camera snapshots than R50 (0/16 vs 2/16 at the default thresholds).
- **No synthetic masks.** Drawing a mask on each reference photo raised the score of the one masked test face, but made 2.5x more reference photos match someone else at 0.45. Real masked photos, learned from faces named on the review page, are the way.

## Camera placement

Placement matters more than any setting. Then draw a **detection area** on the Camera page around the walkway.
- Mount it where everyone **must walk towards it**.
- Face height or slightly above, tilted down less than 15°.
- Faces at least 80 px wide (check on the Camera page).
- The detector looks at the picture shrunk to `det_size` (640 px by default). A 1080p camera is shrunk three times, so an 80 px face becomes 27 px for the detector; if faces are small or far away, set `det_size: 960` in `config.yaml` (slower, more accurate).
- **Light** should come from in front of people. Never point the camera at a glass door or window (backlight).
- **Wi-Fi cameras on a desk** drop frames. A wired PoE camera is more reliable.
- **Masks and sunglasses:** enrol people at the door with and without a mask (below); anything still uncertain goes to review rather than being guessed.

## Reference photos: enrol at the door

An ID photo (3x4, studio light, years old) is a poor reference for a door camera that sees faces from above, in backlight, half-covered by a mask and blurred by walking. The best references are pictures from that camera. Two ways to get them, both on the admin page:

- **Enrol at the door** (Camera page): pick the person and turn the screen towards them. The live picture then tells them what to do, step by step, in big letters (and read aloud, if "Read the steps aloud" is ticked): *look at the camera · come close · turn your head to one side · the other side · put a mask on*. Each step is ticked by what the camera sees (head pose, face size, a covered lower face), so the person knows what is still missing and sees "Enough, thank you!" when every view is covered. Meanwhile the clearest faces that add a new view are kept as reference photos, up to 8, for 90 seconds. A step they cannot do (no mask at hand) is skipped with **Skip step**; if the time runs out first, **Continue 1 min** keeps the progress. Nothing is kept while more than one face is in view, or when the face clearly matches another employee, and the walk is not recorded as an arrival. Do this for every new employee on their first day; two minutes each.
- **Review page**: faces the camera could not name are grouped; naming a group keeps a few of its faces as references. This keeps teaching the system during normal use.

Upload ID photos as well if you have them (People page): they help on the first day, before the camera has seen the person. A photo almost identical to one already kept is refused, so the references stay varied.

**Camera angle.** Head turn and tilt are estimated from 5 points on the face, and a camera on a desk pointing up (or above a door pointing down) makes every face look tilted. The system therefore **learns how this camera normally sees faces** from the first ~30 clear matches, and judges turn and tilt relative to that (Settings → Recognition → Advanced shows what it learned; the Camera page too). After moving the camera, click *Learn again* there.

## Tune recognition with your photos

With labelled photos (e.g. from earlier tests), find the threshold at which nobody is identified as someone else:

```bash
# Folders: faces/<email>/*.jpg; optionally eval/<email>/*.jpg and eval/_unknown/*.jpg (non-employees)
python -m attendance evaluate --gallery faces --test eval --backends insightface opencv
```

Enter the recommended values in **Settings → Recognition**, adding about 0.02 to the match threshold as a safety margin. Include hard photos (dark, blurred, side views, masks); they are what separates the backends.

- **Visits:** frames from one walk past the camera can go in a subfolder, `faces/<email>/<visit>/*.jpg`. Without `--test`, a whole visit is left out of the references while it is tested (frames of one walk are nearly identical, so testing them against each other flatters the result), and each visit is also scored the way the live system decides, by several agreeing frames.
- **Centering:** each backend is also tested with `center_embeddings` (needs photos of at least 8 people). It removes the direction that all faces from one camera share, which makes some people look similar to everyone. Scores are lower with it, so it comes with its own threshold. If `evaluate` says it is more accurate, set in `config.yaml`:
  ```yaml
  recognition:
    center_embeddings: true
    accept_score: <the value evaluate recommends for "centered">
  ```

**From the camera's own data (after a week or two of use):** every correction you make is remembered, so the system can test settings on real camera snapshots without extra labelling:

```bash
python -m attendance evaluate --from-data --days 30
```

It uses faces moved to the right person, faces named on the review page, faces removed as "not them" and ignored visitors (today is left out until it has been reviewed), and recommends `accept_score` / `min_margin`, raw and centered. Run it again every few weeks.

`python -m attendance enroll --faces faces` imports the same folders as reference photos, as an alternative to uploading on the web page.

**Review & learning settings** (Settings → Recognition → Advanced): whether a sighting moved to the right person on the Today page is learned as a reference photo (on by default), how many photos are learned per confirmed review group (5), how far back to compare photos of faces skipped today (14 days), and the *already-seen score* (0.45) above which an unclear face resembling someone checked in today is taken to be them and not sent for review (never below the accept score, so a weak resemblance to an early arrival cannot hide someone else's arrival; 0 = always review). Each unidentified face track creates its own Review card, except that a track beginning within 5 s of the end of an unidentified track whose face it resembles (at least 0.3) adds its photo to that track's card while the card is pending: one walk broken into several tracks is one card. Pending cards are otherwise not merged by appearance, and two faces in the picture at the same time are never one card, so different people in the same frame stay separate. Instead, after a card is confirmed, the other pending cards of that day whose faces are at least the *same face score* (0.55) similar to the confirmed faces are offered as the same person, compared face to face rather than against the gallery; the admin confirms them all or looks first (0 = off).

Review suggestions are recalculated from saved face embeddings against the current reference gallery when the page opens, so new reference photos can change an old suggestion. Group scores are averaged over usable faces from the current model; a suggestion must meet `accept_score`, `min_margin`, and have clear supporting votes from more than half those faces. Ambiguous groups show “No clear suggestion” for manual selection. Similarity is not a probability of identity. Opening Review does not assign anyone or learn a face; confirmation is still required.

Still-photo imports and uploads retry small photos at detector sizes 320 and 160 when the default pass finds no usable face and the photo is smaller than `det_size`. They stop at the first successful pass. Large photos and live camera frames keep their existing detection path; the camera does not run these extra passes.

Portable seed samples imported for review are marked **Seed sample**. Labelling one records the chosen employee and can learn a usable single-face embedding after confirmation, but creates no attendance or Chat announcement. Samples without a detectable face or with multiple detected faces can still be labelled without learning an arbitrary face, including when a second face is too small to learn. They remain separate from live camera groups, daily unknown counts, snapshot retention and camera evaluation data. `seed/import_seed.py --config config.yaml --review-only` adds missing review samples and refreshes missing vectors only for pending seed samples. Existing vectors, handled labels and camera samples are preserved; a refreshed vector still requires manual confirmation before learning.

## Groups of people (person tracking)

When several people walk in together, faces turn away and cover each other for a moment. In the default **face** mode each face is followed on its own, so a face that disappears for more than `track_timeout_s` starts a new track and its earlier votes are lost.

**Person** mode (Settings → Camera → Groups of people, or `tracking.mode: person`) also detects and follows whole bodies:
- [YOLOX](https://github.com/Megvii-BaseDetection/YOLOX) person detector (Apache-2.0) and [ByteTrack](https://github.com/ifzhang/ByteTrack) (MIT, re-implemented in numpy). A half-hidden person is kept by low-confidence detections instead of being dropped.
- A face track stays attached to its person while the face is out of sight, so votes from every frame where the face was visible add up, and faces of two people are never merged.
- Faces are also searched in each person's head region at full resolution, which finds the small faces at the back of a group.
- The arrival time is when the person first appeared, not when their face was first sharp.

Install the model once:
```bash
python -m attendance download-models --person            # yolox_tiny (fast, recommended)
python -m attendance download-models --person yolox_s    # more accurate, about 3x slower
```
Then set `tracking.person_model: models/yolox_s.onnx` if you chose `yolox_s`. If the model file is missing the camera keeps working in face mode and the Camera page says why. A YOLOv8 ONNX export also works, but Ultralytics models are AGPL-3.0: check the licence first.

**Is this computer fast enough?** Record a minute of people walking in (any video file) and run `python -m attendance benchmark entrance.mp4`. It runs the video through both modes and prints the frames per second and who was recognised, without touching the attendance data.

Measured on a 4-core server CPU, 1280x720, one person plus a group in view:

| | face mode | person mode (yolox_tiny) |
|---|---|---|
| `insightface` | ~360 ms per frame | ~375 ms per frame |
| `opencv` | ~40 ms per frame | ~115 ms per frame |

Person mode only searches the whole frame for faces every `full_frame_every` frames (4 by default) and the head regions on every frame, so it costs little extra with `insightface`.

**Tested on a crowded doorway video** (below), person mode did **not** recognise more people than face mode, and sent more faces to review, so face mode stays the default. Try person mode with `benchmark` on your own entrance video before switching.

## Slow computer (e.g. a 2017 Mac)

An Intel Mac from 2017 is roughly half as fast as the numbers above. In order of effect:
1. `recognition.backend: opencv`: about 10x faster (also free for commercial use). Check the accuracy difference on your photos with `evaluate` first.
2. *Frames per second* in Settings: 2-3 is enough for people walking past. When the computer is slower than this it simply skips frames; nothing breaks, but fewer frames vote.
3. In person mode: keep `yolox_tiny` and raise `full_frame_every`.
4. Keep the Camera page closed when not needed (drawing the picture costs CPU).
5. `recognition.cpu_threads` (default: one per physical core, at most 6) leaves cores for video decoding and the page; set it lower if the picture stutters, higher if nothing else runs on the computer.

Run `benchmark` on the real machine and pick the combination that reaches the frame rate you set.

## Tested on public video

The system was run, as if the video were the camera, on the [ChokePoint dataset](https://arma.sourceforge.net/chokepoint/) (NICTA; Wong et al., CVPR Workshops 2011; CC BY-NC 4.0, research use, not included here): an office doorway filmed by a camera above the door. Reference photos were cut from one recording session and another session was played back.

| | insightface | opencv |
|---|---|---|
| One person at a time: 20 enrolled people + 5 strangers | 20/20 recognised, 0 wrong, 5/5 strangers sent to review | 20/20, 0 wrong, 5/5 |
| Time from appearing to being recognised (3-5 frames/s) | mostly 0.5-1 s | mostly 0.5-1 s |
| Crowded entry (people close together, backlight, glass door) | 18 people named, all correct; the rest sent to review | 15 named, all correct |
| Time per frame on a 4-core server CPU, crowded scene | ~670 ms | ~65 ms |

Lessons built into the settings:
- **A camera above head height** makes every face look tilted down. A face that fails the pose or sharpness check therefore still votes when it matches one person very clearly (at least `accept_score + 0.15`, with twice the usual margin); before this, 4 of the 20 people were missed.
- **Glass doors reflect faces**, which then show up as unknown faces to review. Set a detection area that leaves the glass out.

## How wrong identifications are avoided

1. **5-point alignment:** every face is aligned before its embedding is computed.
2. **Quality gate:** small, blurred, turned-away or weakly detected faces **do not vote**, unless a big, clearly detected face matches one person far above the threshold. Turn and tilt are measured from the camera's usual view of faces, learned automatically.
3. **Several references per person:** a person's score is the mean of their two closest reference photos.
4. **Clear margin:** the best match must beat the runner-up by a margin.
5. **Several frames must agree:** at least twice as many votes as everyone else combined. A track that ends one vote short is still accepted when all its votes agree and the match is very strong (fast walkers).
6. **No guessing:** everything else goes to review, and each confirmation teaches the system that face; so does a sighting moved to the right person on the Today page.
7. **Look-alike check:** employees whose photos match each other are flagged before they cause mix-ups.
8. **Varied references:** reference photos are enrolled together with their mirror image, and a confirmed face that is almost identical (≥ 97%) to one already kept is not stored again.
9. **Sharpest frame:** of the frames that arrived since the last one analysed, the one where the faces are sharpest is used (Wi-Fi cameras and people walking give many blurred frames).
10. **Camera health:** a black, blank or frozen picture counts as an outage instead of "nobody came"; days without any camera data are never counted as absences.


Seed Review shows **Source label** separately from the current model suggestion; the source email alone never establishes identity. Explicit `auto_resolve_email` / `auto_resolve_model` metadata is rechecked against the actual single-face photo and current active gallery during import. A match must score at least 0.60 with a margin of 0.12, honoring stricter configured thresholds. Automatic labelling adds no reference or attendance. **Auto-labelled seed samples → Review again** returns a sample to pending and prevents later imports from overriding that choice. Label, evidence and audit changes commit together. Live camera thresholds and inference are unchanged.
