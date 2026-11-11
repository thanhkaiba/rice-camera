# API cho hệ thống camera: báo người đến muộn

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
- **Send at**: giờ bắt đầu gửi hằng ngày, theo múi giờ ứng dụng. Để trống = giờ **Camera stops**.
  Có thể đặt sớm hoặc muộn hơn giờ camera dừng, nhưng phải sau **On time through**.
  Ví dụ: đúng giờ đến hết 09:30, camera chạy tới 18:00, đặt **Send at = 10:30** để bắt đầu gửi lúc 10:30.
  Sau mốc này, người đến muộn mới được gửi ở lượt kiểm tra tiếp theo (khoảng mỗi 30 giây).
- **Test connection** chỉ gọi `camera.ping`, không tạo phạt; dùng được với URL/key vừa nhập trước khi lưu.
- **Send now** gửi ngay các báo cáo đủ điều kiện bằng URL/key đã lưu, gồm hôm nay và 7 ngày trước.
  Vẫn chờ tới giờ **Send at** của ngày báo cáo, kiểm tra dữ liệu nghỉ phép và không gửi lại sự kiện đã xử lý.
  Kết quả hiển thị số sự kiện đã xử lý/bị từ chối hoặc lý do chưa gửi; nút này có thể tạo khoản phạt thật.

Cũng có thể cấu hình trong `config.yaml` rồi khởi động lại ứng dụng:

```yaml
camera_api:
  url: "https://script.google.com/macros/s/DEPLOYMENT_ID/exec"
  key: "CAMERA_SECRET_DO_BEN_NHAN_CUNG_CAP"
  send_at: ""  # ví dụ "10:30"; để trống dùng giờ Camera stops
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
| `date` | không | Ngày bị muộn, `yyyy-mm-dd`. Bỏ trống = hôm nay (giờ Việt Nam). Chỉ nhận hôm nay hoặc trong 7 ngày gần nhất (để gửi bù khi camera mất kết nối), không nhận ngày tương lai hay cuối tuần. |
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
