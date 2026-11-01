# Review rice-camera và hướng phát triển Person Re-ID

Ngày: 2026-10-27. Baseline commit: `4be3781`; working tree sạch trước khi thêm tài liệu. Review chỉ thêm docs và liên kết README, không sửa logic ứng dụng hay dữ liệu vận hành.

Nghiên cứu/model/kiến trúc mục tiêu: [person-reid-design.md](person-reid-design.md).

## Project hiện làm gì?

Đây là ứng dụng chấm công theo lần nhìn thấy mặt: một process Python đọc một camera, nhận diện nhân viên, lưu sighting/giờ đến đầu ngày trong SQLite, cung cấp Flask UI để review/correct và gửi thông báo đi muộn qua Google Chat. Có person detection/tracking tùy chọn để giữ các face observations trên cùng person track.

Tải giả định của bản hiện tại: một máy văn phòng, một camera, vài người quản trị trên LAN; không giả định nhiều writer độc lập hoặc hàng trăm stream. Review đi sâu đường camera → tracking → identity decision → persistence/evidence và đánh giá khoảng cách tới ENTRY/EXIT. Không phải penetration test hay audit từng dòng toàn repository.

**Kết luận: nên phát triển tiếp từ project này, không viết lại.** Phần nền inference, UI review, dữ liệu nhân viên và vận hành có thể tận dụng; semantics chấm công không thể dùng trực tiếp làm occupancy.

## 1. Bản đồ phần có thể tận dụng

| Phần hiện có | Vị trí | Tận dụng và điều chỉnh |
|---|---|---|
| RTSP thread, buffer 6 frame, chọn frame rõ, retry | [camera.py](../attendance/vision/camera.py) | Giữ reader; thêm frame ID/stream epoch, gap và frame-age metrics |
| ONNX session, CUDA/CoreML/CPU | [recognizer.py](../attendance/vision/recognizer.py) | Giữ session helper; body preprocessing riêng, provider thực chạy và model version |
| SCRFD/ArcFace, YuNet/SFace, alignment/quality | [recognizer.py](../attendance/vision/recognizer.py) | Giữ face anchor; không lấy threshold mặt áp cho body |
| YOLOX/YOLOv8 ONNX detector | [person.py](../attendance/vision/person.py) | Giữ baseline trước khi thay detector |
| Kalman + hai vòng IoU association | [person.py](../attendance/vision/person.py) | Baseline tự triển khai kiểu ByteTrack; dùng greedy matching, không mặc định tương đương upstream benchmark |
| FrameProcessor dùng chung live/replay | [processing.py](../attendance/vision/processing.py) | Điểm tích hợp person observations, body encoder, crossing |
| Multi-reference cosine gallery | [gallery.py](../attendance/vision/gallery.py) | Tái dùng normalization/matrix matching; body gallery cần visit/view/provenance riêng |
| Face votes, quality, margin, timeout | [tracker.py](../attendance/vision/tracker.py) | Giữ đường attendance hiện tại; củng cố association trước khi dùng identity cho EXIT |
| Enrollment, review và corrections | [faces.py](../attendance/vision/faces.py), [service.py](../attendance/core/service.py) | Giữ human review; thêm crossing review, không auto-learn body probable |
| SQLite WAL, RLock, transaction | [store](../attendance/store/__init__.py) | Phù hợp single-process MVP; bổ sung event/visit transaction |
| Evidence ảnh/scene, audit và backup | [evidence.py](../attendance/core/evidence.py), [backup.py](../attendance/core/backup.py) | Giữ quy trình; sửa phạm vi digest, bổ sung event evidence |
| Photo evaluation theo visit | [evaluate.py](../attendance/cli/evaluate.py) | Giữ cách tránh leakage; mở rộng ground truth video |
| Replay và timing | [benchmark.py](../attendance/cli/benchmark.py) | Giữ harness; thêm event metrics và latency end-to-end |
| Flask UI và packaging đa hệ điều hành | [web](../attendance/web/__init__.py), [build workflow](../.github/workflows/build.yml) | Giữ UI/backend; không cần thêm Node.js/Go cho MVP |

## 2. Những gì chưa có — không phải lỗi của mục tiêu chấm công cũ

- Chưa có body embedding/Re-ID model. `Observation.body` là ảnh review; `crop_body()` suy ra vùng quanh mặt theo tỷ lệ, không phải crop từ detector toàn thân. Người chỉ thấy lưng không tạo face observation để đi vào luồng decision hiện tại.
- `Decision.kind` chỉ có identified/unknown, không có crossing, direction, identity status, event status hoặc track UID.
- `FaceTracker._strong_exit()` là quyết định khi face track hết hạn, **không phải PERSON_EXIT**.
- `on_identified()` ghi sighting/checkin; `checkins` giữ earliest sighting/ngày, không có visit vào-ra.
- `sightings.camera_id` hiện là **employee ID ban đầu mà camera đoán**, không phải ID thiết bị. Không tái dùng cột này cho multi-camera; thêm `source_camera_id` hoặc migration rõ nghĩa.
- Cấu hình chỉ có một `camera.url`; pipeline chỉ có một processor. Chưa có topology hoặc cross-camera association.
- Camera chạy theo lịch chấm công, mặc định 08:00–11:00. Occupancy cần lịch quan sát bao phủ cả EXIT, tách khỏi deadline đi muộn.
- `seen_again_score`/split suppression phục vụ giảm card review người đã chấm công. Không được mang logic suppression này sang bỏ crossing chưa biết identity.
- Benchmark hiện đếm quyết định và thời gian `processor.process()`, chưa có ground truth tracking/crossing, Rank-1/mAP, wrong-person EXIT hoặc decode-to-event latency.
- SQLite và Flask hiện tại không phải vấn đề cần thay ngay. Multi-process/multi-node mới là điểm đánh giá chuyển PostgreSQL và service deployment.

## 3. Must fix: lỗi cần sửa trước khi dùng identity cho EXIT

### 1. Track có thể sống lại sau gap dài

- **Thành phần:** `ByteTracker.update()`, [person.py:187](../attendance/vision/person.py#L187); processor được giữ xuyên các lần mở camera trong [pipeline.py:262](../attendance/core/pipeline.py#L262).
- **Vấn đề:** matching diễn ra trước khi loại track hết hạn; matched track được cập nhật `last_ts` rồi thoát điều kiện lost. Với cùng box tại t=0 và t=60s, `lost_s=2`, không có update ở giữa, cả hai quan sát vẫn mang ID 1. Đã tái hiện bằng assert; pipeline không gọi tracker update khi không có frame.
- **Sửa nhỏ nhất:** loại track quá hạn trước association, thêm regression test gap không có intermediate updates. Reset/epoch khi reconnect, đổi nguồn camera hoặc hình học ROI; nếu mở grace cho inference chậm phải có giới hạn và test riêng.
- **Nếu bỏ qua:** người mới có thể kế thừa thời điểm xuất hiện, votes và identity người cũ; occupancy sai sau outage.

### 2. Quyết định A có thể mang ảnh và embedding của B

- **Thành phần:** `_observe()` và `_decide()`, [tracker.py:164](../attendance/vision/tracker.py#L164).
- **Vấn đề:** `t.best` chọn quality cao nhất trên toàn track, không theo người thắng vote. Đã tái hiện: cùng person ID, B có quality 1,0 trước, sau đó ba vote A quality 0,8; decision là A nhưng crop/vector thuộc B. Đây là tình huống sau face-person association sai hoặc ID switch, không phải khẳng định mọi track bình thường đều bị lỗi.
- **Sửa nhỏ nhất:** lưu best supporting observation theo identity hoặc chọn lại evidence từ observations thực sự hỗ trợ người thắng. Score/crop/vector phải cùng provenance; thêm test votes lẫn nhau.
- **Nếu bỏ qua:** bằng chứng hiển thị sai người, correction có thể học sai vector, và body gallery mới sẽ bị nhiễm nếu lấy decision làm anchor.

### 3. Person ID được tin tuyệt đối; danh tính đã quyết định không bị xét lại

- **Thành phần:** `_link()` tại [tracker.py:147](../attendance/vision/tracker.py#L147), `_observe()` tại [tracker.py:164](../attendance/vision/tracker.py#L164), `assign_faces()` tại [person.py:253](../attendance/vision/person.py#L253).
- **Vấn đề:** cùng person ID nối track trước mọi kiểm tra face similarity; sau `t.decided`, quan sát khác không thu hồi quyết định. Đã tái hiện sáu face observations B rõ ràng sau quyết định A vẫn giữ `decided=A`. Face ownership hiện chọn box nhỏ nhất chứa tâm mặt, chưa có ambiguity reject khi nhiều box cùng hợp lệ.
- **Sửa nhỏ nhất:** phát hiện face contradiction đáng tin, tách segment/đánh dấu association ambiguous và yêu cầu anchor lại. Cho phép từ chối ownership mơ hồ. Test overlap và ID switch, không chỉ hai người có person ID đúng sẵn.
- **Nếu bỏ qua:** hiện tại có thể bỏ lỡ chấm công B; khi mở rộng EXIT có thể đóng visit A. Không được coi dòng mô tả “faces ... never merged” là bảo đảm hình học thực tế.

### 4. Digest bằng chứng không bao gồm ảnh body

- **Thành phần:** `evidence_digest()`, [evidence.py:31](../attendance/core/evidence.py#L31); body vẫn được lưu và dùng làm bằng chứng review.
- **Vấn đề:** hash chỉ đọc face và scene. Đã tái hiện thay bytes file body nhưng digest giữ nguyên. Điều này không xác thực integrity của body dù body có thể được hiển thị/xuất cùng evidence.
- **Sửa nhỏ nhất:** định nghĩa digest version mới gồm danh sách file/kind và bytes/hash từng file; giữ verifier legacy cho dữ liệu cũ. Không đổi thuật toán rồi đánh dấu mọi hash cũ là corrupted. Thêm test body bị đổi hoặc thiếu.
- **Nếu bỏ qua:** body evidence mới dùng cho Re-ID có thể thay đổi mà integrity check hiện tại không phát hiện. Digest cũng không phải chữ ký chống người có quyền sửa cả DB lẫn file.

## 4. Should fix: bảo vệ chất lượng khi mở rộng

### 5. Model identity chỉ là tên file

- **Thành phần:** `RecognitionConfig.model_id()`, [config.py:136](../attendance/core/config.py#L136), và [faces.py:169](../attendance/vision/faces.py#L169).
- **Vấn đề:** hai checkpoint khác nhau cùng basename có cùng model ID; `reembed_missing()` thấy image đã có vector cho ID đó và bỏ qua. Đổi weights giữ tên có thể so vector từ hai không gian khác nhau.
- **Sửa nhỏ nhất:** model fingerprint/version và preprocessing version bền vững; re-embed khi version đổi. Thực hiện migration cho references, saved observations và evaluation; không chỉ đổi một trường cấu hình.
- **Nếu bỏ qua:** benchmark và quyết định sau nâng model có thể dùng gallery cũ mà không báo lỗi, nhất là khi thêm body model thử nghiệm.

### 6. Release workflow chưa chạy pytest suite

- **Thành phần:** [build.yml](../.github/workflows/build.yml), các bước Build và Test the package.
- **Vấn đề:** workflow có packaged self-test (download/load models/render pages) nhưng không cài pytest/chạy test suite trước phát hành. Lỗi voting, transaction hoặc restart có thể không bị smoke test phát hiện.
- **Sửa nhỏ nhất:** một test job chạy suite hiện có trên PR/push và làm điều kiện release; giữ package smoke tests. Không cần nhân toàn bộ ML matrix lên mọi job.
- **Nếu bỏ qua:** regression nghiệp vụ có thể được đóng gói/phân phối dù ứng dụng vẫn khởi động được.

## 5. Hướng cải tiến cụ thể, theo thứ tự

### Bước A — củng cố nền và dữ liệu đánh giá

Sửa findings 1–4 với regression tests, bổ sung model version ở finding 5 và test gate ở finding 6. Thu video cửa có ground truth; ghi track/crossing/identity, ngày/visit và unknowns. Chưa gắn OSNet vào quyết định chấm công.

Điểm chạm: `vision/person.py`, `vision/tracker.py`, `vision/processing.py`, `core/pipeline.py`, `core/evidence.py`, config/store migration; tests person/tracker/evidence. Cần rà callers/fixtures và các module alias trong `attendance/__init__.py` khi triển khai.

### Bước B — crossing độc lập danh tính, shadow mode

Thêm module nhỏ `attendance/vision/crossing.py` (đề xuất, chưa tồn tại) nhận các person observations thật, timestamp, track UID, gate config và trả crossing candidates. Dùng hai vùng phía cửa + transition/hysteresis, có unknown/degraded khi gap lớn. Person detection bắt buộc cho mode này; lỗi detector không được im lặng chạy face-only rồi vẫn báo occupancy hợp lệ.

Lưu bảng `crossing_events` mới với idempotency. Không viết vào `checkins` từ crossing thử nghiệm. Bổ sung replay labels và metric false crossing/duplicates/reversal trước identity association.

### Bước C — body Re-ID với trusted anchors

Thêm `attendance/vision/reid.py` (đề xuất) chạy một OSNet ONNX session. Crop bằng person bbox, quality/occlusion gates, tuyệt đối không dùng heuristic `crop_body(face_bbox)` làm pipeline Re-ID mặc định. Giữ helper crop cũ cho UI nếu cần.

Thêm bảng body templates có employee, visit/appearance session, camera, timestamp, viewpoint, quality, model/preprocess version và anchor provenance. Thu đa góc khi face đã xác nhận và track sạch; không học từ probable. Exact cosine + bounded exemplars trước, chưa thêm vector DB.

### Bước D — resolver và occupancy

Thêm `attendance/core/identity.py` và `attendance/core/occupancy.py` theo hai trách nhiệm (đề xuất). Resolver trả candidate/status/evidence; occupancy nhận crossing+association và ghi event+visit trong một transaction. Không nhét tất cả vào `AttendanceService` đang xử lý lịch đi muộn, HR và Chat.

Giữ `employees` và face embeddings hiện tại; mở rộng tối thiểu:

| Bảng mới | Trường cốt lõi |
|---|---|
| crossing_events | event ID, camera/gate, track UID, occurred_at, direction, crossing_status, person nullable, candidate nullable, identity_status, evidence, versions, idempotency key |
| visits | visit ID, employee nullable, zone, entry/exit event, status |
| body_templates | appearance session, visit/employee, vector, model/preprocess version, view, quality, provenance |
| event_revisions | event ID, actor, quyết định trước/sau, lý do, timestamp |

Có thể lưu tracklet/association tables riêng khi cần replay/history nhiều camera; không lưu mọi frame vô hạn trong MVP. Cột device camera mới không trùng nghĩa `sightings.camera_id` hiện có.

Attendance tiếp tục có semantics cũ cho tới khi migration nghiệp vụ được duyệt. Sau đó có thể derive checkin từ confirmed ENTRY phù hợp; không chuyển toàn bộ sighting lịch sử thành ENTRY giả. Review crossing có thể dùng UI pattern hiện có nhưng phải tách khỏi thao tác “confirm face → record arrival”.

### Bước E — hai camera và production

Một processor/stream với camera ID + epoch; một occupancy writer sở hữu gate events. Tách capture schedule khỏi schedule chấm công. Bổ sung đồng bộ timestamp/topology và dedup cùng crossing. PostgreSQL khi nhiều process/node cần cùng cập nhật, không chuyển DB chỉ để thử OSNet.

Pin runtime/model versions, đo queue/frame age/VRAM/p95 latency, cảnh báo detector/provider failure. Dùng service manager/WSGI phù hợp triển khai server thay vì coi Flask dev server hiện tại là hạ tầng multi-node. Kiểm tra deployment/auth riêng trước khi mở ngoài LAN; review này không xác nhận an toàn internet-facing.

## 6. Test bổ sung cần có khi triển khai

1. Gap không có frame vượt lost timeout → không nối track cũ.
2. Cùng track ID nhưng face mâu thuẫn → không giữ identity chắc chắn.
3. Decision A → evidence phải hỗ trợ A, không lấy best-quality B.
4. Person không có mặt vẫn phát sinh anonymous crossing hợp lệ.
5. Oscillation ở line không double count; vào rồi ra vẫn có hai event.
6. Track spawn trong vùng/prediction-only/stream outage không sinh EXIT giả.
7. Body top-1 cao nhưng margin nhỏ hoặc quality thấp → không confirm.
8. PROBABLE EXIT không đóng visit của candidate và không học gallery.
9. Hai camera hoặc retry cùng crossing → chỉ một thay đổi occupancy.
10. Restart giữa event/visit write → commit cả hai hoặc không bên nào.
11. Đổi checkpoint cùng tên → không dùng vector space cũ.
12. Replay không dùng gallery từ tương lai; test ngày/visit giữ lại.

## 7. Kiểm chứng và phạm vi

- Đọc README, dependency/build config, entry point và sâu đường frame/person/face tracking → decisions → checkins/evidence; đọc gallery, benchmark/evaluation, cấu hình và test liên quan; xem web guard và persistence transaction pattern.
- Bốn kiểm tra assert chạy trực tiếp bằng `.venv/bin/python`, không model/camera/network: gap 60s giữ track ID; A decision mang evidence B; sáu observations B không thu hồi A; thay body file không đổi digest. Tất cả tái hiện hành vi mô tả. Script chỉ dùng arrays giả và thư mục tạm; không sửa dữ liệu thật.
- Chạy `.venv/bin/python -m pytest -q`: **438 passed in 473.40s (0:07:53)**. Suite hiện tại pass không phủ các counterexample mới nêu trên; bốn assert tái hiện chạy riêng, chưa thêm regression tests vì phạm vi lần này chỉ review/docs.
- Không chạy ứng dụng/live RTSP, không gửi Google Chat, không đọc nội dung DB/ảnh nhân viên hay secrets trong config thực tế, không download/fine-tune model.
- Chưa đo accuracy/latency/VRAM GPU, chưa tái lập public-video benchmark trong docs cũ, chưa build installer; không audit sâu toàn bộ updater, HR integration, backup/restore hay mọi web route.

**Verdict:** tận dụng project hiện tại; ưu tiên sửa lifecycle/association/evidence, thêm crossing shadow mode, rồi mới body Re-ID và occupancy. Không cần viết lại backend hoặc thêm hạ tầng phân tán để bắt đầu.

## 8. Lệnh tái hiện các findings để tiếp tục công việc

Chạy từ thư mục gốc project. Đoạn dưới dùng dữ liệu giả và thư mục tạm, không mở camera, không gọi Chat, không sửa database vận hành. Các assert xác nhận **hành vi lỗi ở baseline**, không phải tiêu chí đúng sau khi sửa; khi sửa cần chuyển thành regression tests với kỳ vọng ngược lại thích hợp.

```bash
.venv/bin/python - <<'PY'
import datetime as dt
import tempfile
from pathlib import Path

import numpy as np

from attendance.core.config import RecognitionConfig
from attendance.core.evidence import evidence_digest
from attendance.store import body_photo
from attendance.vision.person import ByteTracker
from attendance.vision.tracker import FaceTracker, Observation

now = dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc)
box = np.array([[100, 100, 180, 300]], dtype=np.float32)
tracker = ByteTracker(lost_s=2)
first = tracker.update(box, np.array([0.9]), now)[0].id
later = tracker.update(box, np.array([0.9]), now + dt.timedelta(seconds=60))[0].id
assert first == later
print('Finding 1: track ID survives a 60-second observation gap')

faces = FaceTracker(RecognitionConfig())

def observation(who, quality):
    vector = np.array([1., 0.]) if who == 'A' else np.array([0., 1.])
    return Observation(
        (100, 100, 160, 160), vector, [(who, 0.9)], True, quality,
        crop=who, person_id=1,
    )

faces.update([observation('B', 1.)], now, {1: now})
decisions = []
for i in range(1, 4):
    decisions += faces.update(
        [observation('A', 0.8)], now + dt.timedelta(seconds=i / 5), {1: now},
    )
assert decisions[0].employee_id == 'A'
assert decisions[0].crop == 'B'
assert np.array_equal(decisions[0].embedding, np.array([0., 1.]))
print('Finding 2: identity A carries evidence of B')

later_decisions = []
for i in range(4, 10):
    later_decisions += faces.update(
        [observation('B', 1.)], now + dt.timedelta(seconds=i / 5), {1: now},
    )
assert not later_decisions and faces._tracks[0].decided == 'A'
print('Finding 3: six conflicting B observations do not revoke A')

with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    relative = 'crops/sample.jpg'
    (root / 'crops').mkdir()
    (root / relative).write_bytes(b'face')
    body = root / body_photo(relative)
    body.write_bytes(b'original body')
    before = evidence_digest(root, relative)
    body.write_bytes(b'changed body')
    assert before == evidence_digest(root, relative)
print('Finding 4: modified body evidence does not change the digest')
PY
```

Để kiểm tra suite hiện có: `.venv/bin/python -m pytest -q`. Kết quả 438 passed ở mục 7 là kết quả tại thời điểm review, không phải cam kết cho những thay đổi sau này.

## 9. Điểm bắt đầu cho phiên triển khai tiếp theo

1. Đọc findings 1–4 và tái hiện trên revision đang làm việc; ưu tiên gap/ID switch và evidence trước khi tạo trusted body gallery.
2. Triển khai Bước A với regression tests; giữ semantics chấm công đang hoạt động.
3. Làm Bước B ở shadow mode, có ground truth cửa thực tế rồi mới Bước C/D.
4. Chốt camera layout, GPU, số người đồng thời, false EXIT budget và coverage yêu cầu bằng dữ liệu site; hiện chưa có các số đo này.

Toàn bộ lựa chọn model, schema và module mới trong hai tài liệu là đề xuất. Chưa triển khai OSNet, crossing, resolver, occupancy, multi-camera hoặc sửa các findings; chưa có bằng chứng để cam kết accuracy/latency/VRAM production.
