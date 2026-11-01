# Base Timeoff: loại trừ đi muộn có phép

Đã triển khai theo CSV thành viên do người dùng cung cấp. Hướng dẫn và hành vi hiện tại: [base-timeoff-setup.md](base-timeoff-setup.md). Phần dưới ghi nhận quá trình khảo sát; nguồn mapping hiện là CSV ghép theo email, không phụ thuộc quyền Account API.

## Phạm vi

Tích hợp theo `Base-Timeoff-API-Integration.md`, trước mắt phục vụ kiểm tra đi muộn theo yêu cầu. Giữ lịch sử giờ đến và vẫn yêu cầu check-in đối với đơn xin đi muộn. Không bổ sung chấm công về sớm.

## Cách tích hợp đề xuất

Mở rộng cơ chế `LeaveSync` hiện có với nguồn Base; tái sử dụng lịch refresh, retry và trạng thái trên trang quản trị. Một job độc lập sẽ lặp lại các cơ chế này nên không đề xuất. Không gọi API trên mỗi frame camera.

- Đọc token từ `BASE_ACCESS_TOKEN`, không lưu token vào repo.
- Đối chiếu nhân viên bằng email, tái sử dụng `normalize_email` và `employee_by_email` hiện có; không yêu cầu mapping tên hoặc employee ID → Base username. Response mẫu hiện chỉ có `username`, chưa có email: cần xác minh trường email trong response thật hoặc nguồn tra cứu email theo Base username. Không tự ghép username với domain email.
- POST form với bộ lọc khoảng ngày chồng lấn, đọc đủ các trang; xác thực cấu trúc phản hồi trước khi thay dữ liệu cũ.
- Lọc từng ngày thực tế trong `shifts` theo Asia/Ho_Chi_Minh. Chỉ miễn đi muộn khi đơn bao phủ buổi sáng hoặc là đơn xin đi muộn; đơn về sớm/nghỉ buổi chiều không miễn đi muộn.
- Đơn Pending tạm miễn và giữ nhãn chờ duyệt để đối soát. Loại đơn lạ tạm miễn theo tài liệu và cảnh báo quản trị.
- Lưu miễn đi muộn riêng với nghỉ phép: người có đơn đi muộn nhưng chưa check-in vẫn nằm trong danh sách chưa đến. Không sửa thời điểm camera ghi nhận.
- Áp dụng cùng kết quả cho báo cáo, thông báo tức thời và tin nhắn/danh sách/ảnh đi muộn. Khi Base lỗi hoặc dữ liệu chưa đủ mới, giữ dữ liệu cũ, retry và hoãn thông báo vi phạm phụ thuộc Base.
- Không tự động gọi Base vào cuối tuần.

## Phần cần thay đổi và kiểm tra

Nguồn API, cấu hình, lưu dữ liệu miễn đi muộn, logic phân loại/thông báo, hướng dẫn cấu hình và test. Kiểm tra timezone, ngày nghỉ không liên tục, nửa ngày, Pending, mapping, phân trang, lỗi API, huỷ đơn và check-in vẫn bắt buộc. Chạy test liên quan và bộ test hiện có nếu môi trường cho phép.

## Giới hạn

Tài liệu chưa quy định metadata phân trang và các chuỗi giá trị ca thực tế ngoài `Full day`; cần xác minh bằng phản hồi Base đã ẩn thông tin nhạy cảm trước khi đưa vào vận hành. Không gọi API thật khi chưa có token.

## Kiểm tra tài liệu API chính thức

Nguồn: [Base Public API](https://help.base.vn/support/solutions/articles/63000258324-base-public-api), dẫn đến [Account](https://documenter.getpostman.com/view/1623302/T1Dv6u6m) và [Timeoff](https://documenter.getpostman.com/view/1345096/UyrHftXj). Đã đọc JSON collection công khai của hai tài liệu.

- Account có `POST https://account.base.vn/extapi/v1/user/search.by.email`, form gồm `access_token_v2` và `email`. Đây là hướng tra cứu từ email nhân viên sang tài khoản Base, không cần người quản trị tạo mapping thủ công.
- Account cũng có `POST https://account.base.vn/extapi/v1/users` và `POST https://account.base.vn/extapi/v1/user/search.by.username`.
- Collection Timeoff còn ghi field cũ `access_token`. Theo [thông báo nâng cấp V2 chính thức](https://help.base.vn/support/solutions/articles/63000288090-th%C3%B4ng-b%C3%A1o-n%C3%A2ng-c%E1%BA%A5p-access-token-phi%C3%AAn-b%E1%BA%A3n-v2), token V2 phải gửi qua `access_token_v2` cho cả Timeoff và Account. Token V1 chỉ được thông báo hỗ trợ đến hết quý II/2026. Token cần scope tương ứng API đang gọi; không mặc định có quyền toàn hệ thống.
- Timeoff list nhận `users` là username, nhiều username cách nhau bằng dấu phẩy; `page` bắt đầu từ **0**.
- `s` là bộ lọc **trạng thái**, không phải sắp xếp: hỗ trợ pending, approved, rejected, canceled, confirmed, unconfirmed, overdue, warning. Phải loại đơn từ chối/huỷ; không chỉ dựa vào `approvals` rỗng để coi mọi đơn là Pending.
- Hai collection không có response mẫu để xác nhận cấu trúc JSON trả về, trường email/username trong kết quả Account và metadata kết thúc phân trang. Cần xác minh bằng request đọc dữ liệu thật trước khi hoàn thiện parser.

Trạng thái hiện tại: đã kiểm tra API Timeoff thật thành công, có metadata page/items_per_page/total_items và ca dạng HH:MM-HH:MM. Đã nhập CSV theo email, đồng bộ permissions hôm nay; Account API bị từ chối token nên không dùng để map. Xem hướng dẫn triển khai ở đầu tài liệu.
