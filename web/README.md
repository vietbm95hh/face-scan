# UI Vue 3 (web/)

Giao diện web để xem và **xử lý dữ liệu** bài viết Facebook group mà backend ở thư mục cha
(`fbscan` + `api/main.py`) đã quét vào PostgreSQL.

```
Browser ──▶ Vite (web/, :5173) ──proxy /api, /media──▶ FastAPI (:8000) ──▶ PostgreSQL fbscan
```

## 1. Chạy

Terminal 1 — API (thư mục `fb_group_scanner/`):

```bash
python scripts/run_api.py            # http://127.0.0.1:8000
```

Terminal 2 — UI (thư mục `fb_group_scanner/web/`):

```bash
npm install        # lần đầu
npm run dev        # http://localhost:5173
```

Vite proxy `/api` và `/media` sang `http://127.0.0.1:8000` nên không cần cấu hình CORS.
Đổi địa chỉ API (vd API ở máy khác) bằng `web/.env.local`:

```dotenv
VITE_API_TARGET=http://100.69.140.68:8000   # dùng cho dev server proxy
# VITE_API_BASE=http://100.69.140.68:8000   # dùng khi build tĩnh, gọi thẳng API
```

Build bản tĩnh: `npm run build` → `web/dist/` (phục vụ bằng bất kỳ static server nào).

> Worker quét vẫn là Python: bật `python scripts/run_scan.py --pending --watch 60` thì các
> yêu cầu đặt từ UI (trang **Quét dữ liệu**) mới được thực thi.

## 2. Các trang

| Trang | Chức năng |
|---|---|
| **Bài viết** (`/posts`) | Lọc theo nhóm / từ khoá / tác giả / khoảng ngày / có ảnh / đã đọc; sắp xếp; phân trang. Panel chi tiết: nội dung, gallery ảnh + lightbox, mở bài gốc trên Facebook, copy link, **đánh dấu đã đọc**, **ghi chú nội bộ**, **xoá bài**. Chọn nhiều bài để đánh dấu hàng loạt hoặc xoá hàng loạt. |
| **Nhóm** (`/groups`) | Danh sách nhóm + số bài, số bài chưa đọc, lần quét gần nhất. Đổi tên nhóm, bật/tắt nhóm (`is_active`), đặt yêu cầu quét nhanh 1 nhóm. |
| **Quét dữ liệu** (`/scan`) | Form đặt yêu cầu quét (nhóm / từ khóa, lọc thời gian **N ngày / theo năm / theo ngày**, tối đa bài, reset cursor, tải ảnh), quét tất cả nhóm đang bật, hàng đợi yêu cầu (tự cập nhật 5s, huỷ yêu cầu `queued`, hiện khoảng ngày + số lần review), lịch sử jobs. |
| **Đối chiếu** (`/verify`) | Nhập từ khóa + khoảng năm/ngày → **Kiểm tra trước** xem Facebook trả gì (đúng khoảng, đã có trong DB, bị bỏ vì sao, có link FB để mở đối chiếu tay) → **Review & lưu** (chạy nền, tự chờ kết quả) → báo cáo bài mới/đã có/bị bỏ. Nút **Loại trừ** để lần sau không lấy lại bài đó; lọc cụm từ / nguồn bị loại. |
| **Thống kê** (`/dashboard`) | Tổng bài / like / comment theo N ngày (hoặc 1 nhóm), biểu đồ bài theo ngày, top tác giả, top bài nhiều like. |

## 3. Endpoint API dùng thêm (đã thêm vào `api/main.py`)

| Endpoint | Mô tả |
|---|---|
| `PATCH /api/groups/{id}` | `{name, slug, is_active, privacy, member_count_txt, group_url}` — field nào gửi thì cập nhật field đó |
| `GET /api/posts?...&is_read=true\|false` | lọc bài theo trạng thái đã đọc (mỗi item có `is_read`, `note`, `updated_by`) |
| `PATCH /api/posts/{id}` | `{is_read, note}` — đánh dấu đã đọc / ghi chú |
| `POST /api/posts/bulk-read` | `{ids: [...]}` — đánh dấu nhiều bài đã đọc |
| `GET /api/posts/{id}/delete-preview` | hỏi trước khi xoá: số file ảnh sẽ bị xoá |
| `DELETE /api/posts/{id}` | xoá bài + file ảnh trong `media/` (FK cascade xoá `scan_post_media`) |
| `DELETE /api/posts?confirm=true` | `{ids: [...]}` — xoá nhiều bài |
| `POST /api/search/preview` | kiểm tra trước: FB trả gì với từ khóa + khoảng năm/ngày (không ghi DB) |
| `POST /api/search/review` | review chạy nền: lưu bài mới + ghi `scan_reviews` để đối chiếu |
| `GET /api/search/reviews` | lịch sử review |
| `GET /api/search/reviews/{id}` | báo cáo 1 lần review (bài mới / đã có / bị bỏ kèm lý do) |

Cột mới nằm ở `migrations/003_ui_ops.sql` (`is_read`, `note`, `updated_by` trên `scan_posts`),
chạy một lần: `python -m fbscan.cli init`.

> Xoá bài chỉ xoá dữ liệu đã quét. Lần quét sau Facebook còn trả về bài đó thì nó sẽ được lưu lại.

## 4. Cấu trúc

```
web/
├─ index.html
├─ vite.config.js        # proxy /api, /media → FastAPI
├─ src/
│  ├─ main.js            # Vue + Pinia + Router
│  ├─ App.vue            # layout, sidebar, toast, hộp thoại xác nhận
│  ├─ router.js
│  ├─ api.js             # client gọi FastAPI + mediaUrl()
│  ├─ format.js          # format ngày/số/avatar
│  ├─ style.css          # theme tối, không cần thư viện UI
│  ├─ stores/            # pinia: ui (toast/confirm), groups
│  ├─ components/PostDetail.vue
│  └─ views/             # PostsView, GroupsView, ScanView, VerifyView, DashboardView
└─ dist/                 # kết quả npm run build
```
