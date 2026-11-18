# Offspring – Logo & Animation

Tài liệu mô tả logo (SVG) và 3 kiểu animation đang dùng trong app Offspring HR, để bên khác dựng lại cho nền tảng của họ (web, mobile, video…).

## 1. Logo

Logo là một dải ruy-băng gấp khúc (hình chữ "S" gấp), gồm:

- **1 đường viền** khép kín, nét dày 40.
- **12 chấm** (nét đứt) chia thành 2 hàng, mô phỏng 2 nếp gấp. Mỗi chấm là một đoạn ngang dài 7, nét dày 15, bo tròn hai đầu.

Toàn bộ vẽ bằng **stroke, không fill**, không dùng ảnh bitmap. Màu lấy từ màu chữ hiện tại (`currentColor`) nên tự đổi theo light/dark.

| Thuộc tính | Giá trị |
|---|---|
| viewBox | `0 0 693 698` (tỉ lệ ≈ 693 : 698, gần vuông) |
| fill | `none` |
| stroke | `currentColor` |
| stroke-linejoin / linecap | `round` / `round` |
| Nét viền | `stroke-width: 40` |
| Nét chấm | `stroke-width: 15` |

**Đường viền (path):**

```
M210 30 L662 30 L482 243 L662 455 L482 668 L30 668 L210 455 L30 243 Z
```

**Tâm 12 chấm** (mỗi chấm vẽ `M(x-3.5) y h7`):

| Hàng | y | x |
|---|---|---|
| Trên | 243 | 100, 165, 230, 295, 359, 424 |
| Dưới | 455 | 266, 330, 394, 459, 523, 588 |

File gốc tĩnh: `web/public/brand/logo.svg` (màu `#2B2B2B`).

### Màu dùng trong app

| Ngữ cảnh | Logo | Nền |
|---|---|---|
| Light | `#2B2B2B` | `#F5F6F8` (nền app), thẻ logo trắng |
| Dark | màu chữ sáng của theme | nền tối của theme |
| Favicon | trắng | hình tròn `#0B0B0B` |

## 2. Animation

Có 3 kiểu. Tất cả chỉ dùng `stroke-dashoffset` và `opacity`, không dùng transform nên rất nhẹ và dễ chuyển sang Lottie / After Effects / SwiftUI / Compose.

Đặt `pathLength="1"` cho đường viền để dash chạy theo tỉ lệ 0 → 1, không cần tính độ dài thật.

### 2.1 `draw` – vẽ một lần (dùng khi cần logo xuất hiện rồi đứng yên)

1. **0 → 1.1s:** đường viền tự vẽ từ đầu đến cuối (`stroke-dashoffset: 1 → 0`, `stroke-dasharray: 1`), easing `cubic-bezier(.65, 0, .35, 1)`.
2. **Từ 0.9s:** 12 chấm hiện dần (opacity 0 → 1, 0.35s, easing `cubic-bezier(.2, .9, .3, 1.4)`), mỗi chấm trễ thêm **60ms** theo thứ tự (chấm 0 → 11). Hai hàng chấm đi liền nhau theo thứ tự trong bảng trên.
3. Kết thúc, logo đứng yên.

### 2.2 `loop` – màn hình khởi động (splash)

Giống `draw`, nhưng sau khi các chấm hiện ra thì chúng **nhấp nháy mãi**:

- Mỗi chấm bắt đầu nhấp nháy sau `1.9s + i × 90ms`.
- Chu kỳ 1.8s, ease-in-out, opacity `1 → .25 → 1`.

Dùng trong thẻ logo: ô vuông bo góc 36px, kích thước 132px, logo chiếm 56% chiều rộng thẻ.

### 2.3 `load` – đang tải

- Viền hiện mờ cố định: `opacity: .14`.
- Một **nét sáng chạy vòng quanh viền**: `stroke-dasharray: .2 .8` (đoạn sáng dài 20% chu vi), `stroke-dashoffset: 0 → -1`, chu kỳ **1.5s**, linear, lặp vô hạn.
- 12 chấm nhấp nháy lần lượt: opacity `1 → .25 → 1`, chu kỳ **1.2s**, mỗi chấm trễ `i × 80ms`, lặp vô hạn.

Dùng trong thẻ logo nhỏ: 84px, bo góc 24px.

### 2.4 Thẻ logo (khung bao quanh)

Logo được đặt trong một thẻ vuông bo góc, hiện lên bằng animation trượt nhẹ:

- Kích thước: 116px (mặc định), 132px (splash), 84px (loading). Bo góc 32 / 36 / 24px.
- Nền trắng (`--surface`), viền 1px nhạt, bóng mềm.
- Animation xuất hiện: `opacity 0 → 1` và `translateY(14px → 0)`, 0.7s, `cubic-bezier(.2, .8, .2, 1)`.

### 2.5 Giảm chuyển động

Khi người dùng bật *reduce motion* (`prefers-reduced-motion: reduce`) thì **tắt hết animation** và hiện logo tĩnh. Bản mới cũng nên giữ điều này.

## 3. Code mẫu (HTML thuần, chạy độc lập)

Lưu thành file `.html` và mở bằng trình duyệt. Có cả hai kiểu `loop` và `load`.

```html
<!doctype html>
<meta charset="utf-8">
<title>Offspring logo</title>
<style>
  body { margin: 0; min-height: 100vh; display: flex; gap: 48px; align-items: center; justify-content: center; background: #F5F6F8; color: #2B2B2B; }
  svg { width: 160px; height: auto; overflow: visible; }

  /* draw / loop: viền tự vẽ, chấm hiện dần */
  .outline { stroke-dasharray: 1; animation: draw 1.1s cubic-bezier(.65, 0, .35, 1) both; }
  .dot { animation: pop .35s cubic-bezier(.2, .9, .3, 1.4) calc(.9s + var(--i) * 60ms) both; }
  /* loop: sau khi hiện thì chấm nhấp nháy mãi */
  .dot.loop { animation: pop .35s cubic-bezier(.2, .9, .3, 1.4) calc(.9s + var(--i) * 60ms) both,
                         pulse 1.8s ease-in-out calc(1.9s + var(--i) * 90ms) infinite; }
  /* load: viền mờ + một nét sáng chạy vòng quanh, chấm nhấp nháy lần lượt */
  .base { opacity: .14; }
  .trace { stroke-dasharray: .2 .8; animation: trace 1.5s linear infinite; }
  .dot.load { animation: pulse 1.2s ease-in-out calc(var(--i) * 80ms) infinite; }

  @keyframes draw  { from { stroke-dashoffset: 1; } to { stroke-dashoffset: 0; } }
  @keyframes pop   { from { opacity: 0; } to { opacity: 1; } }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .25; } }
  @keyframes trace { from { stroke-dashoffset: 0; } to { stroke-dashoffset: -1; } }
  @media (prefers-reduced-motion: reduce) { * { animation: none !important; } }
</style>

<svg id="a" viewBox="0 0 693 698" fill="none" stroke="currentColor" stroke-linejoin="round" stroke-linecap="round"></svg>
<svg id="b" viewBox="0 0 693 698" fill="none" stroke="currentColor" stroke-linejoin="round" stroke-linecap="round"></svg>

<script>
  const OUTLINE = 'M210 30L662 30L482 243L662 455L482 668L30 668L210 455L30 243Z';
  const DOTS = [[100,243],[165,243],[230,243],[295,243],[359,243],[424,243],
                [266,455],[330,455],[394,455],[459,455],[523,455],[588,455]];

  function mark(svg, mode) {            // mode: 'loop' (splash) | 'load' (loading)
    const load = mode === 'load';
    svg.innerHTML =
      `<path class="${load ? 'base' : 'outline'}" d="${OUTLINE}" stroke-width="40" pathLength="1"/>` +
      (load ? `<path class="trace" d="${OUTLINE}" stroke-width="40" pathLength="1"/>` : '') +
      DOTS.map(([x, y], i) =>
        `<path class="dot ${mode}" style="--i:${i}" d="M${x - 3.5} ${y}h7" stroke-width="15"/>`).join('');
  }
  mark(document.getElementById('a'), 'loop');
  mark(document.getElementById('b'), 'load');
</script>
```

## 4. Gợi ý khi dựng lại trên nền tảng khác

- **Lottie / After Effects:** dùng *Trim Paths* cho đường viền (End 0% → 100%, 1.1s) và nhân bản 12 shape chấm với *Opacity* và độ trễ tăng dần. Kiểu `load` dùng Trim Paths với Start/End cùng chạy (đoạn dài 20%) và Offset quay 1 vòng / 1.5s.
- **SwiftUI / Compose:** vẽ bằng `Path` với `trim(from:to:)` (SwiftUI) hoặc `PathMeasure` (Compose); chấm là 12 đường ngang nhỏ có `lineCap = round`.
- **Xuất tĩnh:** dùng `web/public/brand/logo.svg`. Nếu cần bản trắng trên nền đen thì đổi `stroke` sang `#fff`.

## 5. Nguồn trong repo

| Nội dung | Vị trí |
|---|---|
| Hằng số và component logo | `web/src/ui/index.tsx` (`LOGO_OUTLINE`, `LOGO_DOTS`, `LogoMarkPaths`, `LogoSvg`) |
| Splash / Loading | `web/src/ui/index.tsx` (`Splash`, `PageLoading`) |
| CSS animation | `web/src/styles/app.css` (khối `.splash-*`, `.logo-*`, `@keyframes`) |
| Thẻ logo | `web/src/styles/app.css` (`.logo-tile`) |
| Logo tĩnh | `web/public/brand/logo.svg` |
| Favicon tròn nền đen | `web/public/icons/favicon.svg` |
