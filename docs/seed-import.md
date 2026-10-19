# Nhập employee và ảnh sau khi pull code

## Cách nhanh: ngay trong app (bản cài đặt hoặc từ source)

Không cần Python hay mã nguồn. Nén thư mục `seed/` thành `.zip` (hoặc dùng file zip nhận được), rồi:

- **Trang People → Add → Seed (.zip)**: chọn file, bấm **Import seed**. Tick **Review samples only** nếu chỉ cập nhật Review (tương đương `--review-only`). Camera tạm dừng trong vài phút import; trang hiện tiến độ và kết quả, kèm đường dẫn backup.
- **Dòng lệnh** (dừng chương trình trước; lệnh từ chối chạy nếu chương trình đang chạy):

```bash
FaceAttendance import-seed seed.zip               # bản cài đặt (macOS: /Applications/FaceAttendance.app/Contents/MacOS/FaceAttendance)
python -m attendance --config config.yaml import-seed seed/            # từ source; thư mục hoặc .zip
python -m attendance --config config.yaml import-seed seed.zip --review-only
```

Kiểm tra SHA-256 theo manifest, backup `before-seed-*`, quy tắc ghép email và báo cáo `seed-import-*.json` giống hệt script bên dưới. `seed/import_seed.py` cũ vẫn chạy được; bộ seed mới có thể thay nó bằng wrapper mỏng:

```python
"""Import this seed: python seed/import_seed.py --config config.yaml [--review-only] (program stopped)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from attendance.cli.seed import script_main

script_main(Path(__file__).resolve().parent)
```

Các mục dưới đây là quy trình chi tiết khi chạy từ source.

## Từ source bằng script

Dùng **toàn bộ thư mục `seed/`**, đặt cạnh `attendance/` trên máy đích. Sau khi chuẩn bị môi trường và dừng dịch vụ, chạy tại gốc dự án:

```bash
.venv/bin/python seed/import_seed.py --config config.yaml
```

**Một lệnh này nhập cả 47 employee, 539 ảnh tham chiếu và 299 ảnh Review. Không cần nhập employee riêng trước.**

Hướng dẫn dành cho app chạy từ source bằng Python, ví dụ repo nằm tại `/opt/rice-camera`. Thay đường dẫn, tài khoản SSH và tên dịch vụ ví dụ bằng giá trị máy đích.

## 1. Cần mang theo thư mục nào?

| Thành phần | Có khi pull Git? | Vai trò |
| --- | --- | --- |
| `attendance/`, requirements, `docs/` | Có, sau khi commit/push | Mã ứng dụng và hướng dẫn |
| Toàn bộ `seed/` | Không | Bộ đầu vào cần chuyển riêng |
| `models/` | Không | Chuyển model đang dùng hoặc tải bằng lệnh của app |
| `config.yaml` | Không | Cấu hình riêng của máy đích |
| `data/` | Không | Dữ liệu vận hành của từng hệ thống |

Bộ seed hiện tại:

```text
seed/
├── employees.csv       47 employee, cột email,name
├── faces/              539 ảnh tham chiếu, chia theo email/visit
├── review.csv          Metadata của 299 ảnh Review
├── review/             299 ảnh chính + 7 ảnh body/scene
├── manifest.json       SHA-256 của CSV và các ảnh
├── import_seed.py      Script import
├── README.md
└── verified-local.json Kết quả kiểm chứng local
```

Giữ nguyên cấu trúc thư mục con và tên file. Manifest phải đi cùng đúng phiên bản CSV/ảnh; sửa CSV hoặc bỏ ảnh mà không cập nhật manifest sẽ làm kiểm tra thất bại.

Để nhập seed, không cần chuyển `data/import-doan-20261017/`, `data/faces/`, database local, `.venv/` hoặc `build/`. App sẽ tạo dữ liệu vận hành trong `data_dir` của máy đích.

Seed không chứa lịch sử chấm công, ID nội bộ hoặc bản sao toàn bộ trạng thái Review local. Bộ hiện tại có 16 dòng chỉ định tự gán bằng email, được kiểm tra lại với model/tham chiếu khi import; các dòng còn lại chờ xác nhận. Nếu muốn chuyển nguyên hệ thống cùng lịch sử và nhãn, dùng quy trình [backup/restore](install.md#backup-and-moving-to-another-computer).

## 2. Pull đúng bản code

Nếu máy đích đang chạy app, dừng dịch vụ theo bước 5 trước khi cập nhật code/dependencies.

```bash
cd /opt/rice-camera
git status --short
git pull --ff-only
ls attendance/cli/seed.py attendance/vision/recognizer.py
```

Branch/tag được pull phải đã chứa bản sửa import seed và xử lý crop. Thay đổi chưa commit/push ở máy local sẽ không xuất hiện trên máy đích. Nếu Git báo xung đột hoặc không thể fast-forward, xử lý tình trạng đó trước; không reset để bỏ thay đổi đang có.

Nếu app được cài bằng `Install.command`/installer, thư mục app thực tế có thể nằm trong Application Support hoặc thư mục dữ liệu người dùng, khác repo vừa pull. Phải cập nhật đúng code dịch vụ sử dụng và dùng đúng `config.yaml`/`data_dir` của hệ thống đó. Các lệnh ở đây giả định app chạy từ checkout `/opt/rice-camera`.

## 3. Chuyển seed sang máy đích

Đóng gói trên máy local đang giữ dữ liệu:

```bash
cd /Users/vinhdq2312/Documents/personal/rice-camera
tar --exclude='__pycache__' -czf /tmp/rice-camera-seed.tar.gz seed
scp /tmp/rice-camera-seed.tar.gz user@server:/tmp/
```

Trên máy đích, nếu có seed cũ thì chuyển sang tên lưu riêng trước khi giải nén, tránh trộn hai phiên bản:

```bash
cd /opt/rice-camera
if [ -d seed ]; then
  mv seed "seed.previous-$(date +%Y%m%d-%H%M%S)"
fi
tar -xzf /tmp/rice-camera-seed.tar.gz
ls seed/employees.csv seed/review.csv seed/manifest.json seed/import_seed.py
```

Cũng có thể chép cả `seed/` bằng công cụ truyền file. Sau khi chuẩn bị, cấu trúc phải là:

```text
/opt/rice-camera/
├── attendance/
├── seed/
├── config.yaml
├── models/
└── .venv/
```

## 4. Chuẩn bị Python, cấu hình và model

Máy đã chạy app: dùng lại môi trường Python và cấu hình của hệ thống đó, cập nhật dependencies:

```bash
cd /opt/rice-camera
.venv/bin/python -m pip install -r requirements.txt
```

Máy mới chưa có môi trường: ví dụ dùng Python 3.12:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp -n config.example.yaml config.yaml
```

Chỉnh cấu hình máy đích bằng trình soạn thảo; `cp -n` giữ nguyên file đã tồn tại. Đường dẫn tương đối của `data_dir` được tính từ thư mục chứa `config.yaml`. Ví dụ `data_dir: data` với config trong `/opt/rice-camera` sẽ dùng `/opt/rice-camera/data/attendance.db`.

Các settings đã chỉnh qua UI được lưu trong database và nạp đè những mục được hỗ trợ trong config. Dùng đúng config của dịch vụ đang chạy để tránh nhập vào một database mới ngoài ý muốn.

Bộ hiện tại đã kiểm chứng với backend `insightface`, hai model `models/det_10g.onnx` và `models/w600k_r50.onnx`. Có thể chép hai file model sang `models/`, hoặc tải theo backend cấu hình:

```bash
.venv/bin/python -m attendance --config config.yaml download-models
```

Nếu đã có đúng model, bỏ qua lệnh tải. Vector được tạo bằng model máy đích, không chuyển vector local. Kết quả 539/539 đã kiểm chứng với bộ model nói trên; backend/model khác cần kiểm tra kết quả riêng.

Kiểm tra script chạy được, chưa nhập dữ liệu:

```bash
.venv/bin/python seed/import_seed.py --help
```

## 5. Dừng dịch vụ trước khi import

Chạy import bằng cùng tài khoản hệ điều hành có quyền ghi dữ liệu của app.

- Chạy thủ công trong terminal: dừng bằng `Ctrl+C`.
- Chạy qua service/launcher: dừng bằng trình quản lý tương ứng để nó không tự khởi động lại trong lúc import.

Ví dụ Linux nếu dịch vụ thực sự có tên `attendance.service`:

```bash
sudo systemctl status attendance.service
sudo systemctl stop attendance.service
```

Xác nhận dịch vụ đã dừng và dùng đúng code/config. Không chạy đồng thời nhiều lệnh import. Script tự backup database cùng `faces/` và `review-seed/` trước khi ghi dữ liệu.

## 6. Import đầy đủ employee và ảnh

```bash
cd /opt/rice-camera
.venv/bin/python seed/import_seed.py --config config.yaml
```

Lệnh sẽ:

1. Kiểm tra SHA-256 trong manifest, danh sách email và bộ ảnh tham chiếu.
2. Backup vào `<data_dir>/backups/before-seed-<thời-gian>-<mã>/`.
3. Nhập 47 employee từ `seed/employees.csv` theo email.
4. Nhập 539 ảnh từ `seed/faces/`, tạo vector bằng model máy đích.
5. Thêm ảnh Review chưa có hoặc bổ sung vector thiếu cho mẫu seed pending.
6. Ghi báo cáo vào `<data_dir>/seed-import-<thời-gian>-<mã>.json`.

Quy tắc với employee:

| Trạng thái máy đích | Kết quả |
| --- | --- |
| Đã có đúng email | Giữ ID và liên kết lịch sử, cập nhật tên từ CSV, bật active |
| Chưa có email đó | Tạo employee mới với ID nội bộ mới |
| Employee ngoài seed | Giữ nguyên |
| Bản ghi cũ chưa có email | Chỉ gắn khi tên khớp duy nhất; tên mơ hồ khiến import dừng |
| Đã có Chat user ID, CSV không cung cấp | Giữ Chat user ID hiện có |

Email là khóa ghép. Tên giống nhau nhưng email khác không tự được coi là cùng employee.

Quy tắc với ảnh và Review:

- Chạy đầy đủ thay thế ảnh nguồn `enroll` của những employee trong seed; giữ ảnh upload và ảnh đã học từ xác nhận Review.
- Giữ nhãn assigned/ignored trên máy đích, không đưa lại về pending.
- Chỉ bổ sung vector cho mẫu `source=seed`, `status=pending`, `vector=NULL`; giữ vector đã có và mẫu camera.
- `expected_email` chỉ là nhãn nguồn để hiển thị **Source label** trên Review. Chỉ dòng có cả `auto_resolve_email` và `auto_resolve_model` mới được xét tự gán; email phải khớp nhãn nguồn, model phải khớp, ảnh được phân tích lại có đúng một mặt, điểm so khớp ít nhất 0.60 và hơn người thứ hai ít nhất 0.12 (đồng thời giữ ngưỡng cấu hình nếu chặt hơn). Không tự học thêm ảnh tham chiếu hoặc tạo chấm công khi tự gán.
- Import không ghi check-in/sighting hoặc gửi Chat. Cảnh báo chất lượng/giống người khác được ghi trong báo cáo.

## 7. Kiểm tra kết quả và khởi động lại

Với bộ seed/model đã kiểm chứng, kết quả mong đợi:

```text
Imported 539/539 photos; failures: 0
Backup: .../backups/before-seed-...
```

Mở báo cáo vừa tạo, kiểm tra `employees=47`, `imported=539`, `failed=[]` và mục `review`. Có thể đọc báo cáo mới nhất mà không in cấu hình:

```bash
.venv/bin/python - <<'PY'
import json
from attendance.core.config import load

cfg = load('config.yaml')
reports = list(cfg.path(cfg.data_dir).glob('seed-import-*.json'))
if not reports:
    raise SystemExit('Chưa có báo cáo seed-import trong data_dir này')
latest = max(reports, key=lambda p: p.stat().st_mtime)
report = json.loads(latest.read_text(encoding='utf-8'))
print('Report:', latest)
print('Employees trong seed:', report['employees'])
print('Ảnh nhập:', report['imported'], '/', report['expected_photos'])
print('Lỗi ảnh:', len(report['failed']))
print('Review:', report.get('review', 'Chưa hoàn tất bước Review'))
PY
```

| | Hệ thống mới trống | Hệ thống đã có dữ liệu |
| --- | --- | --- |
| Employee | 47 | Ghép/cập nhật 47 email, giữ employee khác |
| Tham chiếu | 539 | 539 ảnh enroll, cộng ảnh upload/admin được giữ |
| Review | 299 mẫu: dự kiến 16 tự gán, 283 pending với model/tham chiếu đã kiểm chứng | Chỉ thêm mẫu chưa có, kiểm tra quyết định tự gán, giữ nhãn cũ |

Các con số local như 49 employee, 550 tham chiếu hoặc 295 mẫu seed không phải số bắt buộc trên server mới. Toàn bộ trạng thái Review local không đi theo seed; chỉ các quyết định tự gán rõ ràng trong CSV được mang theo và kiểm tra lại.

Sau khi lệnh thành công, khởi động lại bằng cách đang quản lý dịch vụ. Ví dụ systemd:

```bash
sudo systemctl start attendance.service
sudo systemctl status attendance.service
```

Hoặc chạy thủ công tại gốc repo:

```bash
.venv/bin/python -m attendance --config config.yaml run
```

Mở web máy đích, mặc định `http://<server-ip>:8000`:

1. **People**: kiểm tra email/tên và ảnh tham chiếu.
2. **Review**: kiểm tra **Source label** (nhãn nguồn) riêng với gợi ý model. Bấm **Confirm source label** sau khi xem ảnh nếu nhãn nguồn đúng; khi model và nguồn khác nhau, giao diện báo xung đột. Mở **Auto-labelled seed samples** để xem mẫu tự gán và bấm **Review again** nếu cần; lần import sau giữ lựa chọn xem lại này.
3. **Camera/Settings**: kiểm tra đúng cấu hình và dữ liệu hệ thống.

App chạy bình thường dùng cấu hình Chat của máy đích. File `data/local-server-no-chat.py` trong lượt bảo trì local không thuộc seed và không cần chuyển.

## 8. Chỉ cập nhật Review trên hệ thống đã import

Nếu employee và ảnh tham chiếu đã đúng, dừng dịch vụ rồi chạy:

```bash
.venv/bin/python seed/import_seed.py --config config.yaml --review-only
```

Luồng này backup, thêm mẫu chưa có, bổ sung vector NULL cho seed pending, lưu nhãn nguồn và xét tự gán các dòng đã chỉ định trong seed. Không chạy bước nhập `seed/employees.csv` hoặc thay thế ảnh enroll. Trên hệ thống mới, dùng lệnh đầy đủ ở bước 6 trước.

Báo cáo: `<data_dir>/seed-review-import-....json`. `skipped` có thể bao gồm mẫu vừa được bổ sung vector; xem thêm `refreshed`, `resolved` (vừa tự gán) và `auto_pending` (có chỉ định nhưng còn cần xem thủ công). Khi đã cập nhật hết mẫu dùng được, chạy lại mong đợi `imported=0`, `refreshed=0`, `resolved=0`. Ảnh không có mặt đủ điều kiện vẫn giữ để gán nhãn thủ công. Khởi động lại dịch vụ sau khi lệnh hoàn tất.

## 9. Chỉ nhập danh sách employee

Nếu chỉ nhập người, chưa nhập ảnh, mở **People → Import CSV** và chọn `seed/employees.csv`. CSV có dạng:

```csv
email,name
alice@example.com,Alice
bob@example.com,Bob
```

App ghép theo email với quy tắc ở bước 6. Thao tác này không nhập ảnh tham chiếu hoặc Review. Không cần chép đè `employees.csv` gốc: thay file đó không phải cách cập nhật danh sách trên database đã nhập trước đó.

Giữ nguyên CSV trong seed để manifest còn khớp. Muốn nhập danh sách chỉnh riêng, tạo CSV khác để upload trên People.

## 10. Windows

Dùng PowerShell tại gốc repo, thay Python thực thi bằng `.venv\Scripts\python.exe`. Máy mới tạo môi trường bằng `py -3.12 -m venv .venv`, rồi cài dependencies bằng `.venv\Scripts\python.exe -m pip install -r requirements.txt`.

Lệnh import đầy đủ:

```powershell
.venv\Scripts\python.exe seed\import_seed.py --config config.yaml
```

Chỉ cập nhật Review:

```powershell
.venv\Scripts\python.exe seed\import_seed.py --config config.yaml --review-only
```

Khởi động thủ công:

```powershell
.venv\Scripts\python.exe -m attendance --config config.yaml run
```

Các bước chuyển seed, chọn config/model, dừng và kiểm tra dữ liệu giữ cùng nguyên tắc. Lệnh đóng gói/SSH ở bước 3 chạy trên máy local macOS đang giữ dữ liệu.

## 11. Nếu import báo lỗi

| Lỗi/kết quả | Cách kiểm tra |
| --- | --- |
| Không thấy `seed/import_seed.py` | Chưa chuyển seed hoặc sai thư mục |
| Không import được `attendance.cli.seed` | Code chưa có bản sửa cần thiết |
| `Seed file missing or changed` | Chuyển lại đúng bộ seed; giữ kiểm tra SHA-256 |
| CSV và folder email không khớp | Kiểm tra chép thiếu/trộn seed cũ |
| `ambiguous legacy employee name` | Gán email đúng trên People, dừng dịch vụ rồi chạy lại |
| Không tìm thấy model | Kiểm tra backend/config/model hoặc tải model |
| Có `warnings`, nhưng `failed=[]` | Ảnh đã nhập; đọc cảnh báo chất lượng/nhãn |
| Có `failed`, hoặc lệnh kết thúc lỗi | Giữ dịch vụ dừng, xem báo cáo và backup trước khi xử lý tiếp |

Import đầy đủ không phải một transaction cho cả bộ ảnh: nếu lỗi giữa chừng, có thể đã thay một phần ảnh enroll. Không xem lần chạy lỗi là import hoàn tất. Backup `before-seed-*` có database, `faces/`, `review-seed/`; cấu trúc khác backup định kỳ có thư mục `db/`, nên không đưa thẳng vào lệnh `attendance restore` dành cho backup định kỳ.

Xem thêm: [README đi cùng seed](../seed/README.md), [cài đặt và backup/restore](install.md), [nhận diện và Review](recognition.md).
