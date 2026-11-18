# Đăng nhập Google: tài liệu tích hợp cho bên ngoài

Tài liệu này dành cho bên khác (web, app mobile, script, service) cần **đăng nhập bằng tài khoản Google công ty** rồi gọi API của OffspringInternal.
Mã nguồn tham chiếu: `backend/auth.js`, `backend/Code.js` (`doPost`), `backend/devices.js` (`handleMe_`), `web/src/auth/*`, `web/src/api/client.ts`.

> Không dùng Firebase Auth. Web dùng **Google Identity Services (GIS)** để lấy **Google ID token** (JWT). Server kiểm tra token đó, rồi cấp **session riêng của app** (dài 30 ngày). Mọi request sau dùng session này.

---

## 1. Tổng quan luồng

```
Client                          Google                       OffspringInternal API (Apps Script)
  |  1. Sign in with Google ----->|                               |
  |<---- 2. ID token (JWT, ~1h) --|                               |
  |  3. POST { action:"me", idToken:<Google ID token> } --------->|  kiểm tra token qua oauth2.googleapis.com/tokeninfo
  |<---- 4. { ok:true, data:{ email,name,picture,role, session:"s1...." } }
  |  5. Lưu session, gọi mọi action khác với idToken = session -->|  kiểm tra chữ ký HMAC tại chỗ (không gọi Google)
```

1. Client lấy **Google ID token** bằng SDK của Google, với **cùng OAuth Client ID** của app (xem §3).
2. Gọi action `me` với ID token đó. Server trả về hồ sơ người dùng và trường `session`.
3. Từ đó dùng `session` (chuỗi bắt đầu bằng `s1.`) thay cho Google ID token trong **mọi** request. Google ID token chỉ cần cho lần đăng nhập đầu (và khi đăng nhập lại).

---

## 2. Endpoint và định dạng request

| | |
|---|---|
| URL | URL Web App của Apps Script (`https://script.google.com/macros/s/<DEPLOYMENT_ID>/exec`). Xin URL từ team Offspring. |
| Method | `POST` |
| Content-Type | `text/plain;charset=utf-8` (**không** dùng `application/json`, để trình duyệt không gửi CORS preflight mà Apps Script không trả lời) |
| Redirect | Apps Script trả `302` sang `script.googleusercontent.com`. Client **phải đi theo redirect** (`fetch` mặc định đã theo; `curl` cần `-L`). |

Body (JSON dạng text):

```json
{ "action": "me", "idToken": "<Google ID token hoặc session s1.…>", "payload": {} }
```

Response luôn là HTTP 200, phân biệt bằng `ok`:

```json
{ "ok": true,  "data": { ... } }
{ "ok": false, "error": { "code": "UNAUTHENTICATED", "message": "..." } }
```

> Ghi chú: tên trường là `idToken` vì lý do lịch sử; nó nhận cả Google ID token lẫn session của app. Server phân biệt bằng tiền tố `s1.`.

---

## 3. Yêu cầu với Google ID token

Server kiểm tra (`verifyIdToken_` trong `backend/auth.js`):

| Kiểm tra | Điều kiện | Lỗi nếu sai |
|---|---|---|
| Dạng token | JWT 3 phần base64url, ≤ 4096 ký tự | `UNAUTHENTICATED` |
| Chữ ký / hợp lệ | `https://oauth2.googleapis.com/tokeninfo?id_token=…` trả 200 | `UNAUTHENTICATED` |
| `aud` | **bằng đúng** `GOOGLE_CLIENT_ID` của app (Script Property) | `UNAUTHENTICATED` |
| `email_verified` | `true` | `UNAUTHENTICATED` |
| `hd` | bằng `ALLOWED_DOMAIN` (`offspringdigital.com`): chỉ tài khoản Google Workspace công ty | `FORBIDDEN` "Tài khoản này không thuộc Offspring Digital" |
| `exp` | chưa hết hạn | `UNAUTHENTICATED` |

**Quan trọng về `aud`:** server chỉ chấp nhận token phát cho **Web Client ID** của OffspringInternal. Vì vậy:

- **Web của bên khác:** dùng chính Client ID đó, và nhờ team Offspring thêm origin của bạn (ví dụ `https://your-app.example.com`) vào *Authorized JavaScript origins* của OAuth client trong Google Cloud Console. Nếu không, Google sẽ từ chối hiện nút đăng nhập.
- **Android / iOS:** tạo OAuth client riêng cho nền tảng (Android/iOS) trong **cùng Google Cloud project**, nhưng khi xin ID token phải đặt `serverClientId` / `serverClientID` = **Web Client ID** của OffspringInternal. Khi đó `aud` của token sẽ là Web Client ID và server chấp nhận.
- Token phát cho client ID khác sẽ bị trả `UNAUTHENTICATED`.

Client ID không phải bí mật, nhưng xin từ team Offspring, không tự tạo project mới.

---

## 4. Session của app (`s1.…`)

- Định dạng: `s1.<payload base64url>.<HMAC-SHA256 base64url>`. Payload gồm `e` (email), `n` (tên), `p` (ảnh), `g` (Google ID), `iat`, `exp`. Client **không cần và không nên** tự đọc nó; coi như chuỗi mờ.
- Thời hạn: **30 ngày**.
- Gia hạn: gọi `me` bằng session còn **dưới 7 ngày** thì response có trường `session` mới. Client luôn ghi đè session đã lưu khi thấy `data.session`. Gợi ý: gọi `me` mỗi lần mở app.
- Mỗi request server vẫn đọc lại tab `Users`: người bị đặt `active = FALSE` bị chặn ngay (`FORBIDDEN` "Tài khoản đã bị khoá"), dù session còn hạn.
- Không có endpoint thu hồi từng session. Đăng xuất = xoá session ở client (và gọi `google.accounts.id.disableAutoSelect()` trên web). Admin có thể đăng xuất **tất cả mọi người** bằng cách đổi `SESSION_SECRET` trong Script Properties.
- Session là thông tin đăng nhập: lưu ở nơi an toàn (Keychain / EncryptedSharedPreferences trên mobile), không log, không gửi cho bên thứ ba.

---

## 5. Action `me`

Request:

```json
{ "action": "me", "idToken": "<token>", "payload": {} }
```

Response `data`:

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `email` | string | Email (chữ thường), lấy từ token đã xác thực |
| `name` | string | Tên trong tab `Users`, nếu chưa có thì tên Google |
| `picture` | string | URL ảnh Google |
| `role` | `"admin"` \| `"staff"` | Quyền trong app |
| `session` | string, tuỳ chọn | Session mới: có khi đăng nhập bằng Google ID token, hoặc khi session cũ còn dưới 7 ngày |

Lần đầu một email đăng nhập, server tự thêm người đó vào tab `Users` với `role = staff`.

---

## 6. Mã lỗi liên quan đến đăng nhập

| `error.code` | Khi nào | Client nên làm |
|---|---|---|
| `UNAUTHENTICATED` | Thiếu token, token sai / hết hạn, sai `aud`, session hết hạn hoặc bị đổi secret | Xoá session, cho đăng nhập Google lại (thử im lặng một lần trước nếu có, xem §7) |
| `FORBIDDEN` | Tài khoản ngoài domain công ty, hoặc bị khoá (`active = FALSE`), hoặc action cần quyền admin | Hiện `error.message`, không thử lại |
| `UPSTREAM` | Quá nhiều token sai trong 1 phút (≥ 30), server tạm ngừng gọi Google | Thử lại sau ít phút |
| `VALIDATION` | `action` không tồn tại, body không phải JSON | Sửa request |
| `INTERNAL` | Lỗi server | Thử lại sau |

Lưu ý: token không đúng dạng JWT bị từ chối ngay mà không gọi Google; token đúng dạng nhưng sai thì tính vào giới hạn 30 lần/phút dùng chung cho cả hệ thống. Đừng gửi lặp lại một token đã bị từ chối.

---

## 7. Logic phía client nên có (giống web hiện tại)

1. Mở app: có session đã lưu → gọi `me`. Có `data.session` → ghi đè.
2. Gọi action bất kỳ nhận `UNAUTHENTICATED` → thử lấy Google ID token **im lặng** (web: One Tap `auto_select`; mobile: đăng nhập không UI), gọi `me` để đổi lấy session mới, rồi **thử lại request đúng một lần**. Vẫn lỗi → về màn đăng nhập.
3. Có thể kiểm tra nhanh `hd` trong JWT ở client để báo sớm "không thuộc công ty", nhưng server mới là nơi quyết định.

---

## 8. Ví dụ

### 8.1 Web (Google Identity Services)

```html
<script src="https://accounts.google.com/gsi/client" async defer></script>
<div id="g-btn"></div>
<script>
  const API_URL = 'https://script.google.com/macros/s/<DEPLOYMENT_ID>/exec';
  const CLIENT_ID = '<WEB_CLIENT_ID>.apps.googleusercontent.com';

  async function call(action, idToken, payload = {}) {
    const res = await fetch(API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain;charset=utf-8' },
      body: JSON.stringify({ action, idToken, payload }),
      redirect: 'follow',
    });
    const body = await res.json();
    if (!body.ok) throw Object.assign(new Error(body.error.message), { code: body.error.code });
    return body.data;
  }

  window.onload = () => {
    google.accounts.id.initialize({
      client_id: CLIENT_ID,
      hd: 'offspringdigital.com',      // chỉ gợi ý tài khoản công ty
      auto_select: true,
      use_fedcm_for_prompt: true,
      callback: async ({ credential }) => {
        const me = await call('me', credential);   // credential = Google ID token
        localStorage.setItem('session', me.session);
        console.log('Xin chào', me.name, me.role);
      },
    });
    google.accounts.id.renderButton(document.getElementById('g-btn'), { theme: 'outline', size: 'large' });
  };

  // Các lần gọi sau:
  // const data = await call('leave.balance', localStorage.getItem('session'));
</script>
```

### 8.2 curl

```bash
# Đổi Google ID token lấy session
curl -sL "$API_URL" -H 'Content-Type: text/plain;charset=utf-8' \
  -d '{"action":"me","idToken":"'"$GOOGLE_ID_TOKEN"'","payload":{}}'

# Gọi action khác bằng session
curl -sL "$API_URL" -H 'Content-Type: text/plain;charset=utf-8' \
  -d '{"action":"me","idToken":"'"$SESSION"'","payload":{}}'
```

### 8.3 Android (Credential Manager)

```kotlin
val option = GetGoogleIdOption.Builder()
    .setServerClientId(WEB_CLIENT_ID)        // Web Client ID của OffspringInternal, không phải Android client ID
    .setFilterByAuthorizedAccounts(false)
    .setAutoSelectEnabled(true)
    .build()
val request = GetCredentialRequest.Builder().addCredentialOption(option).build()
val result = CredentialManager.create(context).getCredential(context, request)
val idToken = GoogleIdTokenCredential.createFrom(result.credential.data).idToken
// POST { action: "me", idToken, payload: {} } → lưu data.session
```

### 8.4 iOS (GoogleSignIn)

```swift
GIDSignIn.sharedInstance.configuration = GIDConfiguration(
  clientID: IOS_CLIENT_ID,
  serverClientID: WEB_CLIENT_ID            // để aud = Web Client ID
)
GIDSignIn.sharedInstance.signIn(withPresenting: vc, hint: nil, additionalScopes: nil) { result, _ in
  guard let idToken = result?.user.idToken?.tokenString else { return }
  // POST { action: "me", idToken, payload: {} } → lưu data.session
}
```

---

## 9. Những thứ cần xin từ team Offspring

- [ ] URL Web App (`…/exec`) của môi trường cần dùng
- [ ] Web Client ID (`GOOGLE_CLIENT_ID`)
- [ ] Thêm origin web của bạn vào *Authorized JavaScript origins* (nếu tích hợp trên web)
- [ ] Tạo OAuth client Android (package + SHA-1) / iOS (bundle ID) trong cùng Google Cloud project (nếu là app mobile)
- [ ] Danh sách action được phép gọi và tài liệu payload (ví dụ `LEAVE-API.md` cho nghỉ phép)

## 10. Cấp quyền cho bên tích hợp (việc của team Offspring)

**Không có API key hay client secret nào phải cấp.** Chỉ cần:

**Đưa cho bên tích hợp (không phải bí mật):**

1. URL Web App (`…/exec`).
2. Web Client ID: giá trị `GOOGLE_CLIENT_ID` trong Script Properties, cũng là `VITE_GOOGLE_CLIENT_ID` của web.

**Làm trong Google Cloud Console** (cùng project với OAuth client của app):

| Bên tích hợp làm | Việc cần làm | Cần họ gửi |
|---|---|---|
| Web | Thêm origin vào *Authorized JavaScript origins* của Web OAuth client | Origin, ví dụ `https://abc.example.com` |
| Android | Tạo OAuth client loại Android | Package name + SHA-1 của chứng chỉ ký (debug và release) |
| iOS | Tạo OAuth client loại iOS | Bundle ID |

App mobile vẫn phải đặt `serverClientId` = Web Client ID (xem §3).

**Tuyệt đối không đưa:** `SESSION_SECRET` (ai có nó thì giả được session của bất kỳ ai), `WFH_SECRET`, `CAMERA_SECRET`, `BASE_ACCESS_TOKEN`, hay bất kỳ Script Property nào khác.

**Giới hạn hiện tại:**

- Cách này là **đăng nhập theo từng người**: người dùng phải có tài khoản `@offspringdigital.com`. Một service gọi từ server sang server (không có người bấm đăng nhập) không dùng được cách này, mà cần một secret riêng như bot WFH / camera (phải viết thêm ở backend).
- **Không giới hạn được theo bên tích hợp.** Người đăng nhập qua app của bên khác gọi được mọi action mà tài khoản đó có quyền, y như dùng app chính. Muốn chỉ cho một bên gọi một nhóm action (ví dụ chỉ `leave.*`) thì phải sửa backend.
- Muốn cắt quyền: xoá origin / OAuth client của họ trong Cloud Console (chặn đăng nhập mới). Session đã cấp vẫn còn hạn tới 30 ngày; muốn cắt ngay thì đổi `SESSION_SECRET`, nhưng việc đó đăng xuất **tất cả** mọi người.

## 11. Không áp dụng

Bot check-in WFH và camera văn phòng **không** dùng đăng nhập Google; chúng xác thực bằng secret riêng (`WFH_SECRET`, `CAMERA_SECRET`, xem `WFH-EXTENSION.md`, `CAMERA-API.md`).
