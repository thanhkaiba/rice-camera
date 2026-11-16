# API cho hệ thống camera: báo người đến muộn và chưa thấy

Hệ thống camera quét xem ai đến muộn so với lịch nghỉ, rồi **gọi API này** để báo từng người. Mỗi báo cáo tạo ngay một khoản phạt **Đến muộn** trong Sổ phạt của app (mức phạt mặc định 20.000đ, admin chỉnh được ở Admin → Cài đặt → Phạt tự động).

Camera tự quyết định ai muộn (so với lịch nghỉ trên Base hay cách nào là việc của bên camera). App **không** kiểm tra lại lịch nghỉ, chỉ ghi nhận. Người trong email phải là nhân viên có trong app (tab `Users`, đang hoạt động).

## 1. Cài đặt phía app (làm một lần)

1. `npm run deploy:backend`.
2. Trong Apps Script editor, chọn hàm **`setupCamera`** rồi bấm **Run**. Hàm tạo `CAMERA_SECRET` trong Script Properties (cố ý không in ra log).
3. Lấy `CAMERA_SECRET` ở **Project Settings → Script Properties**, gửi riêng cho bên camera.
4. Gửi cho bên camera: URL web app (cùng link `VITE_API_URL`, dạng `https://script.google.com/macros/s/<DEPLOYMENT_ID>/exec`) và secret.

## 2. Gọi API

Một endpoint duy nhất, `POST`, body là JSON.

- Header `Content-Type: text/plain;charset=utf-8` (hoặc `application/json`; server không cần CORS vì gọi từ server).
- Apps Script trả `302` rồi mới có kết quả: **phải theo redirect** (curl thêm `-L`, thư viện HTTP thường tự theo). Request POST đã được xử lý ở lần gọi đầu, redirect chỉ để lấy kết quả (bằng GET), đừng POST lại tới URL được chuyển tới.
- Không cần đăng nhập Google. Xác thực bằng `secret` trong body.

### Kiểm tra kết nối

Trong ứng dụng camera, vào **Settings → HR system → Camera API · Late fines**:

- Nhập **Server URL** và **Camera key** (`CAMERA_SECRET`), bấm **Save** để lưu.
- **Include late arrivals** và **Include not seen employees**: chọn chỉ đến muộn, chỉ chưa thấy hoặc cả hai; bỏ cả hai để tắt gửi. Áp dụng cho cả lịch tự động và Send now, độc lập với Google Chat.
- **Previous days to retry**: chọn 0–3 ngày lịch trước đó, ngoài hôm nay; 0 = chỉ hôm nay.
  Áp dụng cho cả tự động và Send now. Ngày nghỉ/cuối tuần vẫn bị loại.
- **Sending mode = Once a day**: chạy đúng một lượt trong phút **Send at**, kể cả khi lượt đó lỗi,
  chưa có dữ liệu HR hoặc chưa có báo cáo để gửi. Không tự thử lại hôm đó, kể cả sau Save/khởi động lại.
  Để trống Send at = giờ **Camera stops**; giờ này phải sau **On time through**.
- **Sending mode = Every interval**: gửi/thử lại trong khoảng **Interval · Start** đến trước **Interval · Stop**.
  Có thể đặt giờ riêng, không cần trùng giờ camera. Để trống Start = phút bắt đầu muộn; Stop = Camera stops.
  Start phải sau On time through, Stop phải sau Start trong cùng ngày; không hỗ trợ qua đêm.
  **Interval · Seconds** là bội số 30, từ 30 đến 86400. Ví dụ Start = 10:30, Stop = 18:00,
  Seconds = 300: các lượt chạy vào 10:30, 10:35, 10:40…; từ 18:00 không bắt đầu request mới.
  Lượt interval đã qua bị bỏ qua; kiểm tra nền trong 30 giây đầu mỗi lượt. Save không kích hoạt gửi riêng.
  Lượt đã chạy được ghi nhớ qua khởi động lại. Request đang chạy có thể hoàn tất sau giờ Stop.
- **Test connection** chỉ gọi `camera.ping`, không tạo phạt; dùng được với URL/key vừa nhập trước khi lưu.
- **Send now** gửi ngay các báo cáo đủ điều kiện bằng URL/key và nhóm đã lưu, gồm hôm nay và số ngày trước đã chọn (tối đa 3).
  Gửi theo lựa chọn đã lưu ở bất kỳ giờ nào, vẫn kiểm tra dữ liệu nghỉ phép và không gửi lại sự kiện đã xử lý.
  Kết quả hiển thị số sự kiện đã xử lý/bị từ chối hoặc lý do chưa gửi; nút này có thể tạo khoản phạt thật.

Cũng có thể cấu hình trong `config.yaml` rồi khởi động lại ứng dụng:

```yaml
camera_api:
  url: "https://script.google.com/macros/s/DEPLOYMENT_ID/exec"
  key: "CAMERA_SECRET_DO_BEN_NHAN_CUNG_CAP"
  send_at: ""  # ví dụ "10:30"; để trống dùng giờ Camera stops
  include_late: true
  include_not_seen: true
  mode: daily  # hoặc interval
  days_back: 3  # 0–3 ngày lịch trước, ngoài hôm nay
  interval_seconds: 300  # bội số 30; chỉ dùng trong chế độ interval
  interval_start: "10:30"
  interval_end: "18:00"
```

Giá trị đã lưu trong Settings ưu tiên hơn `config.yaml` theo từng trường.

```bash
curl -L "$URL" -H 'Content-Type: text/plain' \
  -d '{"action":"camera.ping","secret":"<CAMERA_SECRET>"}'
```

```json
{ "ok": true, "message": "Kết nối OffspringInternal OK" }
```

### Báo người đến muộn: `camera.late`

```bash
curl -L "$URL" -H 'Content-Type: text/plain' -d '{
  "action": "camera.late",
  "secret": "<CAMERA_SECRET>",
  "events": [
    { "email": "an.nguyen@offspringdigital.com", "date": "2026-11-09", "arrivedAt": "09:42", "lateMinutes": 12 },
    { "email": "binh.tran@offspringdigital.com" }
  ]
}'
```

| Trường | Bắt buộc | Ý nghĩa |
|---|---|---|
| `email` | có | Email công ty của nhân viên (không phân biệt hoa thường). |
| `date` | không | Ngày bị muộn, `yyyy-mm-dd`. Bỏ trống = hôm nay (giờ Việt Nam). Backend nhận hỗ trợ hôm nay và 7 ngày trước; ứng dụng gửi giới hạn theo days_back, tối đa 3 ngày trước. Không nhận ngày tương lai hay cuối tuần. |
| `arrivedAt` | không | Giờ camera thấy người đó, `HH:MM`. Chỉ để ghi vào lý do phạt. |
| `lateMinutes` | không | Số phút muộn, số nguyên 0 đến 720. Chỉ để ghi vào lý do phạt. |

Mỗi lần gửi tối đa 200 `events`.

Lý do phạt hiện trong app: `Đến muộn (camera) lúc 09:42, trễ 12 phút` (bỏ phần thiếu nếu không gửi `arrivedAt` / `lateMinutes`).

### Kết quả

```json
{
  "ok": true,
  "created": 1,
  "results": [
    { "email": "an.nguyen@offspringdigital.com", "date": "2026-11-09", "status": "created", "fineId": "FN-1A2B3C4D", "amount": 20000 },
    { "email": "binh.tran@offspringdigital.com", "date": "2026-11-09", "status": "duplicate", "fineId": "FN-9Z8Y7X6W" }
  ]
}
```

`results` cùng thứ tự với `events`. `status` của từng event:

| `status` | Nghĩa | Camera nên |
|---|---|---|
| `created` | Đã tạo khoản phạt. | Coi như xong. |
| `duplicate` | Người này đã có khoản phạt đến muộn của ngày đó (kể cả khi admin đã huỷ khoản đó). Giữ khoản đầu, không tạo thêm, không sửa giờ. | Coi như xong, **không gửi lại**. |
| `skipped` | Admin đang tắt hoặc để mức phạt đến muộn = 0. | Coi như xong. |
| `rejected` | Event sai, có thêm trường `error` (không tìm thấy email, tài khoản bị khoá, ngày hoặc giờ sai, cuối tuần…). | Gửi lại cũng vẫn lỗi: sửa dữ liệu hoặc báo lại cho HR/admin. |

Gửi lại cùng một event nhiều lần là an toàn (idempotent theo email + ngày), nên cứ gửi lại khi không chắc lần trước có thành công.

### Báo người chưa thấy: `camera.missing`

Ứng dụng gửi riêng một request cùng URL và secret:

```json
{
  "action": "camera.missing",
  "secret": "<CAMERA_SECRET>",
  "events": [
    { "email": "an.nguyen@offspringdigital.com", "date": "2026-11-09" }
  ]
}
```

Mỗi event chỉ có `email` và `date`. Danh sách lấy từ **Not seen** của báo cáo Today,
loại người nghỉ phép/WFH đã được duyệt và người miễn chấm công. Không phụ thuộc
công tắc **Include not seen employees** của thông báo Google Chat. Không gửi ngày
không có dữ liệu camera, cuối tuần hoặc ngày không làm việc.

Dùng cùng chế độ lịch gửi, kiểm tra dữ liệu HR, giới hạn số ngày gửi bù (tối đa 3) và tối đa 200 event/request như `camera.late`.
Daily chạy một lượt/ngày; interval chỉ thử trong khung giờ đã chọn. Ngoài lịch, dùng **Send now** để gửi tay.
Phản hồi phải theo cùng cấu trúc `ok` và `results`
ở trên; mỗi kết quả có `email`, `date`, `status` (`created`, `duplicate`, `skipped`,
`rejected`), cùng thứ tự với events. Lỗi request dùng cùng quy tắc `permanent`.

Chống trùng riêng theo **action + email + ngày**. Người đã báo missing vẫn được
gửi `camera.late` nếu sau đó xuất hiện muộn; không tự thu hồi báo cáo missing đã gửi.
Nếu đặt Send at trước giờ camera dừng, missing là trạng thái tại thời điểm gửi.

**Triển khai:** backend nhận phải hỗ trợ `camera.missing` và quy tắc chống trùng riêng
trước khi cập nhật ứng dụng gửi. Repo này chỉ chứa phía gửi và tài liệu giao thức;
cần cập nhật/kiểm tra backend Apps Script trong repo của bên nhận. Loại xử lý hoặc
khoản phạt cho missing do backend nhận quyết định.

### Lỗi cả request

```json
{ "ok": false, "error": "Sai secret của camera." }
```

- Có thêm `"permanent": true` khi request sai hẳn (thiếu `events`, quá 200 event): gửi lại y nguyên sẽ vẫn lỗi.
- Không có `permanent` (sai secret, backend bận `Hệ thống bận, thử lại`, lỗi hệ thống): thử lại sau, giữ lại các event chưa gửi được và gửi bù.

## 3. Việc còn lại phía app

- Admin → Cài đặt → **Phạt tự động**: chỉnh mức phạt, bật/tắt loại **Đến muộn (camera)**.
- Admin → Cài đặt → **Báo cáo sáng**: mục "Đến muộn (camera)" trong báo cáo gửi vào Google Chat cũng lấy từ các khoản phạt này.
- Phạt được hiện trong **Sổ phạt** (loại "Đến muộn"); admin vẫn huỷ / đánh dấu đã thu như các khoản khác.
