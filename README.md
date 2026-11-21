# fb_group_scanner

Tool Python quét bài viết trong các Facebook Group (group bạn đã tham gia), lưu vào
PostgreSQL **kèm ảnh tải về máy**, và phục vụ dữ liệu cho UI qua REST API chỉ đọc.

- Engine lấy dữ liệu: [`facebook-gql-scraper`](https://pypi.org/project/facebook-gql-scraper/) (GraphQL nội bộ Facebook, không cần API key).
- DB: PostgreSQL (dùng chung server Laragon của bạn, **database riêng `fbscan`**, không đụng `bantin_mxh`).
- API: FastAPI (Swagger tại `/docs`).

```
Facebook (cookies)  →  fbscan.scanner  →  PostgreSQL fbscan  →  FastAPI  →  UI
                                    ↘  media/<post_id>/*.jpg
```

---

## 1. Trạng thái máy này (đã kiểm tra)

| Hạng mục | Kết quả |
|---|---|
| Python | 3.13 (Laragon) |
| Thư viện | `facebook-gql-scraper 0.8.4`, `psycopg 3`, `fastapi`, `uvicorn`, `playwright` — đã cài |
| PostgreSQL | **server dev** `100.69.209.48:5432`, DB `fbscan` (PostgreSQL 16.15) — cấu hình trong `.env` (`PG_HOST`, `PG_PORT`, ...) |
| Schema | đã tạo 9 bảng `scan_*` trong DB `fbscan` |
| API | đã test `GET /api/health`, `/docs` trả 200; `smoke_test.py` 13/13 PASS |

`scripts\start_db.cmd` **không cần chạy** khi DB ở server dev: script tự đọc `.env`, thấy
`PG_HOST` khác `127.0.0.1` thì kiểm tra kết nối tới server dev rồi thoát (báo lỗi nếu không tới
được). Nếu chuyển về DB local thì script sẽ tự bật cluster `D:\laragon\data\postgresql2` (port 2024).

**Cấu hình DB (`.env`)** — mọi module (`discover`, `scanner`, `cli`, `api`, `scripts/*`) đều dùng
chung một nguồn này, không hard-code ở đâu khác:

```dotenv
PG_HOST=100.69.209.48     # IP/hostname của PostgreSQL trên server dev
PG_PORT=5432
PG_DB=fbscan
PG_USER=postgres
PG_PASSWORD="Cts@12345"   # bọc nháy nếu có # hoặc ký tự đặc biệt
PG_CONNECT_TIMEOUT=10     # giây; mạng tới server dev rớt thì fail nhanh, không treo
PG_CONNECT_RETRIES=2      # thử lại kết nối khi Tailscale/DERP chập chờn
PG_CONNECT_BACKOFF=1.0    # giây, tăng dần giữa các lần thử
```

Server dev cần bật `listen_addresses`, mở firewall cổng 5432 và cho user trong `.env` kết nối
(`pg_hba.conf`). Kiểm tra nhanh: `python scripts/check_install.py` — dòng *"nơi đặt DB"* phải là
*server dev*, dòng *"kết nối PostgreSQL"* in ra IP server đang phục vụ.

> **Ghi chú về PostgreSQL local của máy này:** cluster gốc `D:\laragon\data\postgresql`
> (port 5432) bị crash `0xC0000142 (DLL init failed)` ngay khi có kết nối, nên đã tạo cluster
> sạch `D:\laragon\data\postgresql2` chạy port 2024 để dùng khi cần chạy offline.

> **Nhớ bọc giá trị đặc biệt trong `.env` bằng dấu nháy:** `.env` là plain text nên dấu `#`
> (và giá trị có `"`) phải bọc: `PG_PASSWORD="Cts@12345"`. Tool tự bỏ nháy và URL-encode khi
> dựng chuỗi kết nối.

Kiểm tra nhanh:

```bash
python scripts/check_install.py
```

---

## 2. Cài đặt (máy mới)

```bash
pip install -r requirements.txt
python -m playwright install chromium      # chỉ cần cho discover.py
```

Tạo DB và bảng:

```bash
# 1) tạo database (1 lần)
psql -h 127.0.0.1 -p 2024 -U postgres -c "CREATE DATABASE fbscan ENCODING 'UTF8';"

# 2) tạo bảng
python -m fbscan.cli init
```

Cấu hình: copy `.env.example` → `.env` rồi sửa `PG_*`, hoặc dùng nguyên `.env` hiện có.

---

## 3. Đăng nhập Facebook (cookies)

Cần **file cookies** để scraper gọi GraphQL bằng phiên của bạn:

1. Cài extension **Cookie-Editor** (Chrome/Edge/Firefox).
2. Mở `https://www.facebook.com`, bấm Cookie-Editor → **Export** → **Export as JSON**.
3. Lưu nội dung đó thành `facebook_cookies.json` trong thư mục dự án.

Cookies hết hạn → tool báo `SessionExpiredError` / `LoginRequired`. Chỉ cần export lại.
Nên dùng tài khoản phụ: FB có thể cảnh báo khi quét nhiều.

**Lỗi `error 1357004`** ("Rất tiếc, đã xảy ra lỗi") khi tìm kiếm: Facebook **chặn** truy vấn
GraphQL của tool (`/api/graphql/` trả payload lỗi cho mọi từ khóa) — không phải hết kết quả.
Trang HTML vẫn mở bình thường, nên tool **tự chuyển sang đọc trang tìm kiếm bằng Chromium**:

- `POST /api/search/preview` / `review` mặc định `engine="auto"`: thử GraphQL, bị chặn thì mở
  `facebook.com/search/posts/?q=…` bằng Playwright, cuộn, rồi bóc dữ liệu bài nhúng trong HTML
  (link, nội dung, thời gian, nguồn). Kết quả trả `engine: "browser"`.
- Chuẩn bị một lần: `pip install -r requirements.txt` và `python -m playwright install chromium`.
- CLI: `python scripts/run_scan.py --keyword "..." --engine auto|browser|graphql`.
- Bài lấy bằng browser có `scan_posts.fetched_by = 'browser:…'`, dedupe theo cột `source_path`
  (`<chủ thể>/posts/<id>`); không có only-members/like count như GraphQL.
- Nếu browser cũng không đọc được (cookies hết hạn), sửa bằng cách export lại cookies. Đôi khi
  chỉ cần chờ vài giờ rồi export lại `facebook_cookies.json` (đủ `c_user`, `xs`).

**Quét bản tin của một Page** (lấy trọn bài trong ngày):

```bash
python scripts/run_scan.py --page thanhnien --days 1 --no-media
python scripts/run_scan.py --page https://facebook.com/thanhnien --from 2026-11-10 --to 2026-11-10
```

- UI **Đối chiếu** → chọn tab **Theo Page** → dán link/tên Page → **Kiểm tra trước** rồi
  **Review & lưu kết quả**. Không cần từ khóa.
- Feed Page không nằm trong HTML: tool mở Page bằng Chromium và bắt response GraphQL
  (`ProfileCometTimelineFeedRefetchQuery`) — không bị lỗi `1357004` như gọi GraphQL trực tiếp.
- Tự dừng khi đã cuộn qua bài cũ hơn mốc đầu khoảng, nên chọn **Chỉ hôm nay** là lấy đủ bài
  trong ngày mà không cuộn hết lịch sử Page.
- Mốc ngày tính theo **giờ Việt Nam** (00:00–23:59 VN), không phải UTC.
- Bài Page lưu với `source_type='page'`, `group_id=NULL`, dedupe theo `source_path` và
  `fb_post_id`; bài chỉ có ảnh/video vẫn được lưu (có thể không có nội dung chữ).
- Page đổi tên/xóa hoặc giới hạn người xem → tool báo lỗi rõ thay vì trả 0 bài.

---

## 4. Lấy danh sách group đã tham gia

**Cách A — tự động bằng trình duyệt (mặc định):**

```bash
python -m fbscan.discover --source playwright
```

- Mở cửa sổ Chromium với profile lưu ở `.fb-browser-profile/`.
- Nếu chưa đăng nhập (kể cả khi FB chuyển thẳng sang trang **xác thực 2 lớp**):
  **đăng nhập trực tiếp trong cửa sổ đó** — mật khẩu → mã 2FA → xác nhận “thiết bị lạ” nếu có.
  Tool chờ tối đa 10 phút (`--login-timeout`), tự phát hiện khi vào được và tiếp tục.
- Tool mở `facebook.com` trước (nếu phiên hỏng/checkpoint thì bạn đăng nhập ngay ở đó), sau đó mới
  vào `facebook.com/groups/joins/?ordering=viewer_added` (tab **Nhóm của bạn**), cuộn và đọc mọi
  link group, lưu vào `scan_groups`.
- Lần sau chạy lại lệnh này để cập nhật group mới tham gia.
- Phiên đăng nhập được lưu trong `.fb-browser-profile/` nên các lần sau **không phải đăng nhập lại**
  (trừ khi FB logout). Không chia sẻ/commit thư mục này.

Tùy chọn: `--max-scroll 80` cuộn nhiều hơn khi có rất nhiều group, `--login-timeout 1200` chờ lâu hơn.

**Cách B — thêm tay:**

```bash
python -m fbscan.discover --source manual --group 2781022932139267 --group kynangbanhang
python -m fbscan.discover --source manual --file groups.txt
```

`groups.txt` mỗi dòng một mục, hỗ trợ `id`, `slug`, link đầy đủ, có thể kèm tên:

```
2781022932139267 | Chợ tốt Quảng Ninh
https://www.facebook.com/groups/kynangbanhang
viectimquangninh
```

Sau khi lưu, tool tự gọi GraphQL để điền tên/privacy/số thành viên (bỏ qua bằng `--no-resolve`).

Xem danh sách:

```bash
python -m fbscan.cli groups
```

---

## 5. Quét bài viết

```bash
python scripts/run_scan.py --all                       # tất cả group đang bật
python scripts/run_scan.py --group 2781022932139267    # một group
python scripts/run_scan.py --db-group 3 --max 50       # group id=3 trong DB, tối đa 50 bài
python scripts/run_scan.py --all --days 3              # chỉ bài trong 3 ngày gần nhất
python scripts/run_scan.py --all --reset-cursor        # quét lại từ đầu
python scripts/run_scan.py --all --no-media            # không tải ảnh
```

Cơ chế chống quét trùng:
- Mỗi group lưu **cursor phân trang** trong `scan_cursors` → lần sau chạy tiếp, không quét lại
  lịch sử (dùng `freeze()` / `thaw()` của thư viện).
- Gặp `STOP_AFTER_SEEN` (mặc định 10) bài đã có liên tiếp → dừng group đó.
- Bài cũ hơn `--days`/`MAX_AGE_DAYS` → dừng.
- Cùng một bài: `UPSERT` theo `(group_id, fb_post_id)` — bài cũ chỉ cập nhật lại like/comment/share.
- Nghỉ ngẫu nhiên `GROUP_DELAY_MIN..MAX` (5–15s) giữa các group, `REQUEST_DELAY_SEC` giữa các post.

Mỗi lần quét ghi một dòng `scan_jobs` (found/new/updated/error):

```bash
python -m fbscan.cli jobs
```

### Quét theo group + khoảng thời gian từ API

**Bước 1 — đặt yêu cầu** (`python scripts/run_api.py`, mở http://127.0.0.1:8000/docs):

```bash
curl -X POST http://127.0.0.1:8000/api/scan ^
  -H "Content-Type: application/json" ^
  -d "{\"group_id\": 3, \"days\": 3, \"max_posts\": 100, \"reset_cursor\": true}"
```

| Field | Ý nghĩa |
|---|---|
| `group_id` | id trong bảng `scan_groups` (xem `python -m fbscan.cli groups`), hoặc |
| `group` | `fb_group_id` / slug / link FB nếu chưa có trong DB (số lạ sẽ được thêm mới) |
| `days` | chỉ lấy bài trong N ngày gần nhất; `0` = dùng `MAX_AGE_DAYS` |
| `max_posts` | tối đa số bài; `0` = không giới hạn |
| `reset_cursor` | `true` (mặc định) = bỏ cursor để quét lại cả khoảng thời gian yêu cầu |
| `download_media` | tải ảnh hay không |
| `note` | ghi chú tuỳ ý |

Yêu cầu được ghi vào hàng đợi `scan_requests` (status `queued`). Xem bài đã có trong khoảng thời
gian (không cần quét lại) bằng API đọc:

```bash
curl "http://127.0.0.1:8000/api/posts?group_id=3&date_from=2026-11-01&date_to=2026-11-09&size=50"
curl "http://127.0.0.1:8000/api/stats?group_id=3&days=7"
```

**Bước 2 — worker thực thi** (cần `facebook_cookies.json`):

```bash
python scripts/run_scan.py --pending              # làm hết hàng đợi rồi thoát
python scripts/run_scan.py --pending --watch 60   # chạy mãi, 60s kiểm tra hàng đợi 1 lần
```

Worker nhận việc bằng `FOR UPDATE SKIP LOCKED` (đặt nhiều worker cũng không trùng),
tự đưa lại hàng đợi các yêu cầu bị bỏ dở quá 30 phút. Theo dõi:

```bash
python -m fbscan.cli requests                 # hàng đợi (id, status, group, days, max)
python -m fbscan.cli requests --status queued
curl http://127.0.0.1:8000/api/scan/status
curl -X DELETE http://127.0.0.1:8000/api/scan/5   # huỷ yêu cầu còn 'queued'
```

> Giới hạn: scraper luôn đọc feed từ bài **mới nhất** và dừng khi bài cũ hơn `days`, nên `days`
> càng lớn càng quét lâu, và **không lấy được bài cũ vượt quá những gì Facebook còn trả về**
> trong feed (thường chỉ vài ngày gần đây). Muốn xem dữ liệu cũ đã lưu thì dùng `/api/posts`
> với `date_from`/`date_to`.

### Quét theo từ khóa (ô tìm kiếm Facebook)

Ngoài quét feed của group, tool còn quét **ô tìm kiếm** trên Facebook
(`facebook.com/search/posts/?q=...`) — dùng khi muốn tìm bài ở cả những group chưa
nằm trong `scan_groups`.

```bash
python scripts/run_scan.py --keyword "tuyển dụng kế toán" --days 7 --max 100
python scripts/run_scan.py --keyword "cellphones" --keyword-all       # lấy MỌI bài
python scripts/run_scan.py --keyword "bán xe" --keyword "thuê nhà" --no-media
python -m fbscan.cli keywords                 # từ khóa + số bài đã lưu
```

| Cờ | Ý nghĩa |
|---|---|
| *(mặc định)* | chỉ lưu bài **thuộc group** (bài Page/trang cá nhân bị bỏ qua) |
| `--keyword-all` | lưu **mọi bài ô tìm kiếm trả về**: group + Page + trang cá nhân |

Hoặc đặt qua API (cùng hàng đợi `scan_requests`, worker `--pending` xử lý như bình thường):

```bash
curl -X POST http://127.0.0.1:8000/api/scan ^
  -H "Content-Type: application/json" ^
  -d "{\"keyword\": \"tuyển dụng kế toán\", \"days\": 7, \"max_posts\": 100, \"search_all\": true}"
```

Cách hoạt động:

- `SearchEngine.search(term, filter_type="post")` trả danh sách bài khớp từ khóa
  (mới nhất trước) — **giống hệt** những gì ô tìm kiếm Facebook hiển thị, gồm bài trong
  group, bài của Page và bài trên trang cá nhân. Mỗi bài được lưu vào `scan_posts` +
  `scan_post_media`; nội dung/ảnh/chỉ số tương tác lấy ngay từ kết quả tìm kiếm.
- `search_all` (API) / `--keyword-all` (CLI):
  - `false` (mặc định): chỉ lưu bài thuộc group; group lạ tự được thêm vào `scan_groups`,
    `group_id` được set. Bài Page/trang cá nhân bị bỏ qua (đếm vào "bỏ qua").
  - `true`: lưu tất cả. Bài không thuộc group có `group_id = NULL` và được phân loại qua
    `source_type` = `page` (Page/trang cá nhân) hoặc `user`, kèm `source_fb_id`,
    `source_name`, `source_url` (URL gốc của bài).
- Bài đã có trong DB (do quét group hoặc từ khóa khác) → chỉ cập nhật chỉ số tương tác,
  không lấy lại chi tiết, không tải lại ảnh.
- Bài mới gắn `keyword_id`; xem lại bằng `GET /api/posts?keyword_id=<id>` (thêm
  `&source_type=page` nếu chỉ muốn bài Page/trang cá nhân).
- `days` dừng khi gặp bài cũ hơn mốc (0 = `MAX_AGE_DAYS`); `max_posts` giới hạn số bài xử
  lý (0 = không giới hạn).
- Từ khóa giống nhau (không phân biệt hoa thường, khoảng trắng thừa) dùng chung 1 dòng
  trong `scan_keywords`; không có cursor nên mỗi lần quét đọc lại từ đầu trang kết quả
  (bài cũ được bỏ qua nhanh).

> Kết quả phụ thuộc tài khoản đang đăng nhập: chỉ thấy bài công khai/mà tài khoản xem được.

### Lọc theo ngày / năm (bỏ bài cũ, chỉ lấy đúng khoảng)

| Cách | Field API | CLI | Ý nghĩa |
|---|---|---|---|
| **Chỉ hôm nay** | `date: ""` (trống = hôm nay, giờ VN) | `--from <hôm nay> --to <hôm nay>` | chỉ bài đăng trong ngày hôm nay |
| 1 ngày bất kỳ | `date: "2025-11-09"` | `--from 2025-11-09 --to 2025-11-09` | chỉ bài đăng đúng ngày đó |
| N ngày gần nhất | `days: 7` | `--days 7` | từ `now - 7 ngày` đến nay |
| Theo năm | `from_year: 2025`, `to_year` (tuỳ chọn) | `--year 2025` | **cả năm** 2025; có `to_year` thì từ năm A đến hết năm B |
| Theo khoảng ngày | `from_date: "2025-06-01"`, `to_date: "2025-06-30"` | `--from ... --to ...` | tính **cả** ngày đầu và ngày cuối |
| Không lọc | (bỏ trống) | — | theo `sort`, tới `lookback` bài |

- **Thứ tự kết quả:** mặc định `sort: "recent"` = bài **mới nhất trước** (dùng filter
  "Bài viết mới đây" của Facebook). Đây là điều kiện để lọc theo ngày/năm chính xác: gặp
  bài cũ hơn mốc đầu khoảng là dừng. `sort: "relevance"` = thứ tự liên quan mặc định của
  FB (bài cũ/mới chen lẫn — chỉ nên dùng khi không lọc thời gian).
- Không trộn `days` / `date` / `from_year` / `from_date` với nhau (API trả 422).
- `to_date` / `to_year` **bao gồm** ngày/năm đó (lưu dạng nửa mở `[date_from, date_to)`).
- `lookback` giới hạn số bài đọc qua (mặc định `1000`). API trả `stopped_reason` cho biết
  dừng vì hết trang / đạt `max_posts` / đã tới bài cũ hơn mốc đầu khoảng / đạt `lookback`.
- Chỉ **quét từ khóa** lọc được theo ngày/năm (feed group chỉ đi từ bài mới về cũ).
- Facebook (qua ô tìm kiếm) chỉ có dữ liệu tới vài tháng gần đây: lọc năm cũ thường ra
  0 bài — tool báo rõ bằng `note` chứ không im lặng trả rỗng.
- **Thời gian chạy:** kết quả "mới trước" của Facebook chỉ *xấp xỉ* (15:31, 15:18, 15:23…,
  bài cũ cách vài tháng vẫn lẫn vào), nên để lọc đúng một ngày tool phải đọc sâu tới khi
  bài cũ nhất đã lùi hẳn qua ngày cần lấy. Từ khóa phổ biến mất **1–3 phút**; đặt
  `lookback` nhỏ hơn để nhanh hơn (đánh đổi: có thể sót bài). UI hiện đồng hồ đếm giây
  trong lúc chờ. Mẹo nhanh: muốn lấy "hôm nay" của 1 từ khóa phổ biến, đặt `lookback` ~40
  và xem bài mẫu; kết quả có ít bài trong ngày là bình thường vì FB trả lẫn bài cũ.
- **Phiên Facebook phải còn sống:** gọi `/api/search/preview|review` (và worker quét) trực
  tiếp trên máy chạy API; cookies hết hạn sẽ báo `SessionExpiredError` / HTTP 503 — export
  lại `facebook_cookies.json`. Chỉ `POST /api/scan` (xếp hàng) + xem bài đã lưu mới chạy
  được khi không có phiên.

### Chỉ lưu bài có từ khóa trong tiêu đề/nội dung

Mặc định (`keyword_match: "strict"`) chỉ lưu bài mà **tiêu đề hoặc nội dung** có chứa
từ khóa — so khớp **bỏ dấu tiếng Việt** và **không phân biệt hoa thường**:

| Mức (`keyword_match`) | Nhận những bài nào | Ví dụ với từ khóa "tuyển dụng kế toán" |
|---|---|---|
| `strict` (mặc định) | đúng cụm từ khóa; **hoặc** đủ mọi từ; **hoặc** chứa nguyên một cụm của từ khóa | "TUYỂN DỤNG KẾ TOÁN - BÁN HÀNG" ✅, "Tuyển dụng nhân viên kinh doanh" ✅ (có cụm "tuyển dụng") |
| `normal` | như strict + bài chỉ trùng **1 từ chính** | "Kế toán cần người gấp" ✅ |
| `loose` | có bất kỳ từ nào trùng (kể cả từ phụ) | "Tuyển nhân viên bán hàng" ✅ |
| `off` | không kiểm tra (lưu mọi bài FB trả về) | — |

Từ phụ (từ dừng) như *cần, và, của, cho…* không được tính là "có từ khóa". Bài không khớp
bị bỏ với lý do `not_match` và vẫn hiện trong báo cáo review để đối chiếu. Chọn mức ở UI
(Quét dữ liệu / Đối chiếu) hoặc CLI `--match strict|normal|loose|off`.

### Kiểm tra trước + đối chiếu (verify) kết quả với Facebook

Đặt yêu cầu quét xong vẫn có thể không chắc "đúng tin". Dùng **Đối chiếu** (UI `/verify`)
hoặc API để xem Facebook thật sự trả gì rồi mới lưu:

```bash
# 1) KIỂM TRA TRƯỚC (không ghi DB, không tải ảnh): FB trả bao nhiêu, đã có bao nhiêu
curl -X POST http://127.0.0.1:8000/api/search/preview ^
  -H "Content-Type: application/json" ^
  -d "{\"keyword\": \"tuyển dụng kế toán\", \"from_year\": 2025, \"search_all\": true}"

# 2) REVIEW: chạy như quét (lưu bài mới) + ghi lại báo cáo để đối chiếu
curl -X POST http://127.0.0.1:8000/api/search/review ^
  -H "Content-Type: application/json" ^
  -d "{\"keyword\": \"tuyển dụng kế toán\", \"from_year\": 2025, \"search_all\": true}"

# 3) Xem báo cáo (chạy nền, thử sau ít phút)
curl "http://127.0.0.1:8000/api/search/reviews?limit=20"
curl http://127.0.0.1:8000/api/search/reviews/1
```

`preview` trả: `found` (FB trả về), `in_range` (đúng khoảng thời gian), `would_save`,
`existing` (đã có trong DB), `skipped`, `counts` (số bài theo từng lý do), `items` (tối đa
`sample` bài mẫu có link Facebook để mở kiểm tra tay). `review` lưu kết quả vào bảng
`scan_reviews` (`new_ids` / `existing_ids` / `skipped[]`) và UI hiện danh sách **bài mới lưu**
+ **bài bị bỏ kèm lý do**, mỗi bài có link FB.

Lý do bỏ qua (`skipped[].reason`):

| reason | Nghĩa |
|---|---|
| `already_in_db` | bài đã có trong DB — **không tải lại chi tiết/ảnh** |
| `duplicate` | trùng trong cùng lượt |
| `out_of_range` | ngoài khoảng năm/ngày |
| `not_match` | nội dung không chứa từ khóa (so khớp bỏ dấu, không phân biệt hoa thường) |
| `excluded_id` | fb_post_id đã bị loại trừ |
| `excluded_term` | chứa cụm từ loại trừ |
| `excluded_source` | nguồn (Page/trang cá nhân) bị loại trừ |
| `not_group` | không phải bài group khi `search_all=false` |

**Loại bỏ những gì đã xử lý:** dùng `exclusions` (danh sách `fb_post_id`), `excluded_terms`
(cụm từ) và `exclude_sources` (id Page/trang cá nhân) trong `POST /api/scan`,
`/api/search/preview`, `/api/search/review`. Các field này cũng được lưu trong
`scan_requests` để worker thực thi yêu cầu quét theo đúng bộ lọc đó. Trong UI Đối chiếu, nút
**Loại trừ** ở mỗi bài preview sẽ thêm bài đó vào danh sách loại trừ.

### Chạy định kỳ on Windows

Tạo task trong **Task Scheduler**:

| Trường | Giá trị |
|---|---|
| Program | `C:\Users\Viet\Documents\Default Project\fb_group_scanner\scripts\scan_task.cmd` |
| Start in | `C:\Users\Viet\Documents\Default Project\fb_group_scanner` |
| Trigger | mỗi 30 phút |

Nếu muốn bỏ qua bước bật DB local (vd khi DB ở xa), đặt biến `FBSCAN_SKIP_DB_START=1`.

`scan_task.cmd` đã có sẵn: tự kiểm tra DB đang chạy, chạy `run_scan.py --all`, ghi log vào `logs/`.

---

## 6. API cho UI

```bash
python scripts/run_api.py           # http://127.0.0.1:8000  (Swagger: /docs)
python scripts/run_api.py --port 8010 --reload
```

| Endpoint | Mô tả |
|---|---|
| `GET /api/health` | kiểm tra API + DB |
| `GET /api/groups` | danh sách group + `post_count` + `latest_post_at` |
| `GET /api/groups/{id}` | chi tiết group |
| `GET /api/posts` | danh sách bài; query: `group_id, q, author, date_from, date_to, has_media, keyword_id, source_type, sort, order, page, size` |
| `GET /api/posts/{id}` | chi tiết bài + `media[]` + `raw` đã lược |
| `GET /api/stats` | tổng bài, `by_day`, `top_authors`, `top_posts` (`group_id`, `days`) |
| `GET /api/jobs` | lịch sử quét (`status`, `limit`) |
| `GET /api/keywords` | từ khóa đã quét qua ô tìm kiếm + số bài mỗi từ khóa |
| `DELETE /api/keywords/{id}` | xoá từ khóa (bài đã lưu vẫn giữ) |
| `GET /api/scan` | hàng đợi yêu cầu quét (`status`, `group_id`, `keyword_id`, `limit`) |
| `GET /api/scan/status` | số yêu cầu `queued`/`running` + yêu cầu gần nhất |
| `GET /api/scan/{id}` | chi tiết 1 yêu cầu |
| `POST /api/scan` | đặt yêu cầu quét `{group_id/group}` **hoặc** `{keyword}` + `{days, max_posts, reset_cursor, download_media, search_all, keyword_match, sort}` + lọc thời gian `{date, from_year, to_year, from_date, to_date, lookback}` + loại trừ `{exclusions, excluded_terms, exclude_sources}` |
| `DELETE /api/scan/{id}` | huỷ yêu cầu còn `queued` |
| `POST /api/search/preview` | **kiểm tra trước** (không ghi DB): `{keyword, from_year/from_date..., search_all, exclusions...}` → FB trả gì, sẽ lưu/bỏ bao nhiêu bài. Quét Page: gửi `{page: "link/tên Page"}` thay cho `keyword` (tự dùng `engine="browser"`, lấy mọi bài trong khoảng) |
| `POST /api/search/review` | **review** (chạy nền): chạy như quét + ghi `scan_reviews`. Quét Page: gửi `{page}` thay cho `keyword` |
| `GET /api/search/reviews` | lịch sử review (`keyword_id`, `limit`) |
| `GET /api/search/reviews/{id}` | báo cáo 1 lần review: bài mới, đã có, bị bỏ kèm lý do |
| `GET /media/{file_path}` | file ảnh đã tải (có chặn path traversal) |

Ví dụ phản hồi `GET /api/posts?group_id=1&size=2`:

```json
{
  "items": [
    {
      "id": 12,
      "fb_post_id": "1234567890",
      "group_id": 1,
      "group_name": "Chợ tốt Quảng Ninh",
      "author_fb_id": "1000...",
      "author_name": "Nguyễn Văn A",
      "title": "Tuyển dụng lập trình viên Python",
      "content": "Công ty ABC tuyển dụng...",
      "permalink": "https://www.facebook.com/groups/.../posts/1234567890",
      "posted_at": "2026-11-09T04:30:00+00:00",
      "like_count": 42, "comment_count": 7, "share_count": 1,
      "has_media": true,
      "scraped_at": "2026-11-09T05:00:00+00:00",
      "summary": "Tuyển dụng lập trình viên Python",
      "media": [
        { "id": 5, "kind": "photo", "position": 0, "url": "/media/1234567890/00.jpg", "width": null, "height": null }
      ],
      "cover_url": "/media/1234567890/00.jpg"
    }
  ],
  "total": 128, "page": 1, "size": 2, "pages": 64
}
```

UI ghép base URL (vd `http://127.0.0.1:8000` + `/media/...`) để hiển thị ảnh.
`posted_at` là UTC ISO-8601, có timezone.

### Endpoint ghi (dùng bởi UI `web/`)

| Endpoint | Mô tả |
|---|---|
| `PATCH /api/groups/{id}` | đổi tên / bật tắt group (`{name, is_active, ...}`) |
| `PATCH /api/posts/{id}` | `{is_read, note}` cho 1 bài |
| `POST /api/posts/bulk-read` | `{ids}` đánh dấu nhiều bài đã đọc |
| `GET /api/posts/{id}/delete-preview` | số file ảnh sẽ bị xoá |
| `DELETE /api/posts/{id}` | xoá bài + ảnh tải về |
| `DELETE /api/posts?confirm=true` | `{ids}` xoá nhiều bài |

`GET /api/posts` có thêm filter `is_read`, mỗi bài có `is_read`, `note`, `updated_by`.

### Giao diện web (Vue 3)

```bash
cd web
npm install
npm run dev        # http://localhost:5173  (proxy /api + /media sang API :8000)
```

Chi tiết các trang, cấu hình `VITE_API_TARGET`, xem [`web/README.md`](web/README.md).


---

## 7. Sơ đồ database

```
scan_groups (id, fb_group_id UQ, slug, name, privacy, member_count_txt,
             group_url, avatar_path, is_active, discovered_at, last_scanned_at)
      │ 1
      ├──────────── N  scan_posts   (id, fb_post_id, group_id FK, author_fb_id, author_name,
      │                               title, content, permalink, posted_at, like_count,
      │                               comment_count, share_count, reaction_breakdown jsonb,
      │                               has_media, scraped_at, updated_at, raw jsonb,
      │                               UNIQUE(group_id, fb_post_id))
      │                     │ 1
      │                     └── N  scan_post_media (id, post_id FK, kind, position,
      │                                              source_url, file_path, thumb_url,
      │                                              width, height, bytes, downloaded_at)
      ├──────────── N  scan_jobs    (id, group_id FK, status, trigger_source, started_at,
      │                               finished_at, posts_found, posts_new, posts_updated, error)
      ├──────────── N  scan_requests(id, group_id FK NULL, keyword_id FK NULL, status, max_posts,
      │                               days, reset_cursor, download_media, requested_by, note, job_id,
      │                               created_at, started_at, finished_at, error) — hàng đợi từ
      │                               POST /api/scan (group hoặc keyword)
      ├──────────── N  scan_keywords(id, term, term_key UQ, created_at) — từ khóa ô tìm kiếm FB
      ├──────────── N  scan_reviews (id, request_id FK, keyword_id FK, group_id FK, term,
      │                               date_from, date_to, found, in_range, new_ids[],
      │                               existing_ids[], skipped jsonb, search_all, created_by,
      │                               created_at) — báo cáo đối chiếu / review
      └──────────── 1  scan_cursors (group_id PK FK, cursor, has_next, updated_at)
```

`scan_requests` có thêm: `date_from`, `date_to` (lọc năm/ngày, nửa mở), `exclusions`,
`excluded_terms`, `exclude_sources` (loại trừ), `keyword_match` (mức khớp từ khóa),
`sort` (`recent`/`relevance`), `scope_key` (đã quét những gì).

Index: `(group_id, posted_at DESC)`, `posted_at DESC`, `author_fb_id`,
GIN trigram trên `content`, `(post_id, position)`, `(status, started_at DESC)`.

**Vì sao title tách khỏi content:** bài group của FB thường không có tiêu đề; scraper đặt
`title` = 120 ký tự đầu của bài để UI hiển thị danh sách, còn `content` giữ nguyên văn.

---

## 8. Cấu trúc mã

```
fb_group_scanner/
├─ .env / .env.example          # cấu hình
├─ migrations/001_init.sql      # schema
├─ migrations/002_scan_requests.sql  # hàng đợi quét từ API
├─ migrations/003_ui_ops.sql    # cột is_read/note cho UI
├─ migrations/004_keyword_search.sql  # quét từ khóa: scan_keywords + keyword_id
├─ migrations/005_search_all_sources.sql  # nguồn bài group/page/user + search_all
├─ migrations/006_date_range_review.sql   # lọc năm/ngày + bảng scan_reviews
├─ fbscan/
│  ├─ config.py                 # đọc .env
│  ├─ console.py                # in tiếng Việt trên console Windows
│  ├─ context.py                # FacebookContext + resolve group id/slug/link
│  ├─ discover.py               # Playwright / manual → scan_groups
│  ├─ scanner.py                # vòng quét group + cursor + stop-early
│  ├─ keywords.py               # quét ô tìm kiếm FB theo từ khóa → scan_posts
│  ├─ search_scan.py            # lõi: preview / review / quét từ khóa + lọc năm/ngày, loại trừ
│  ├─ dtrange.py                # khoảng thời gian theo năm/ngày (DateRange)
│  ├─ normalize.py              # Post model → row DB
│  ├─ media.py                  # tải ảnh
│  ├─ db.py                     # psycopg + SQL
│  └─ cli.py                    # init / groups / jobs / keywords / toggle / reset
├─ api/main.py                  # FastAPI (đọc + thao tác cho UI)
├─ web/                         # UI Vue 3 + Vite (xem web/README.md)
├─ scripts/
│  ├─ run_scan.py               # quét 1 lần / --pending worker từ API
│  ├─ run_api.py                # chạy API
│  ├─ check_install.py          # tự kiểm tra môi trường
│  ├─ smoke_test.py             # test API với dữ liệu giả
│  ├─ test_filters.py           # test lọc năm/ngày + so khớp từ khóa
│  ├─ test_search_api.py        # test preview/review qua HTTP (scraper giả)
│  ├─ start_db.cmd              # bật PostgreSQL 2024
│  └─ scan_task.cmd             # wrapper cho Task Scheduler
│  └─ start_db.py               # bật PostgreSQL local (chỉ khi PG_HOST=127.0.0.1)
└─ media/<fb_post_id>/00.jpg
```

---

## 9. Xử lý sự cố

| Hiện tượng | Cách xử lý |
|---|---|
| `connection refused` / `could not translate host name` | kiểm tra `PG_HOST/PG_PORT` trong `.env`; server dev có bật PostgreSQL + mở firewall 5432 chưa (mạng/VPN/Tailscale đã thông chưa); DB local thì chạy `scripts\start_db.cmd` |
| `connection timed out` khi mở tool | `PG_CONNECT_TIMEOUT` trong `.env` (mặc định 10s) — giảm để fail nhanh hơn khi mạng tới server dev chập chờn |
| `psycopg.errors.ConnectionTimeout` / API trả 500 | Không phải sai user/password — đây là timeout tầng mạng (thường do Tailscale/DERP chưa sẵn sau khi máy wake). Kiểm tra `tailscale status` + `tailscale ping <PG_HOST>` rồi chạy lại; tool đã tự thử lại `PG_CONNECT_RETRIES` lần và trả **503** thay vì 500 |
| `GET /health` trả 404 | Endpoint đúng là `GET /api/health` |
| Mật khẩu `.env` có `#` hoặc ký tự đặc biệt | bọc bằng `"..."`, tool tự bỏ nháy và URL-encode |
| `LoginRequired` / `SessionExpiredError` | export lại `facebook_cookies.json` |
| `discover` treo ở trang 2FA hoặc báo chưa đăng nhập dù đã nhập mã | để nguyên cửa sổ Chromium, chờ tool tự chuyển trang; nếu quá 10 phút, chạy lại với `--login-timeout 1200`. Nếu vẫn lỗi, xoá thư mục `.fb-browser-profile/` và đăng nhập lại |
| Đăng nhập xong nhưng 0 group | kiểm tra đúng tài khoản; thử `--max-scroll 80`; hoặc thêm tay bằng `--source manual` |
| `SchemaChangedError` | thư viện lỗi thời → `pip install -U facebook-gql-scraper` |
| `DocIdStillInvalidError` | xoá `facebook_state.json` rồi chạy lại |
| Ảnh không hiện trong UI | kiểm tra `media/` có file; UI dùng `/media/...` của API |
| Group quét nhầm/thừa | `python -m fbscan.cli toggle <id> --on false` |
| Muốn quét lại lịch sử | `python -m fbscan.cli reset --id <id>` hoặc `--reset-cursor` |

Lưu ý pháp lý/ToS: tool dùng phiên đăng nhập của chính bạn để đọc dữ liệu bạn có quyền xem.
Quét dồn dập có thể khiến Facebook tạm khóa tài khoản — hãy giữ các delay mặc định.
