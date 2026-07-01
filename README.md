# Tinh Hoa Sách — Backend (FastAPI)

Backend v1 cho app đọc sách tóm tắt (subscription-only freemium), dựng theo
`BACKEND_SPEC` + billing spine từ `BILLING_HANDOFF`. Stack: **FastAPI + SQLAlchemy
(SQLite local / Postgres prod) + Google Play Developer API + Pub/Sub RTDN**.

Entitlement do **server** sở hữu (`premium`), client chỉ đọc lại. Không tin client.

---

## Chạy nhanh (local, DEV stub mode)

Cần **Python 3.10+**. Nếu máy chưa có: cài từ https://www.python.org/downloads/ (nhớ tick
"Add Python to PATH") hoặc `winget install Python.Python.3.12`.

```powershell
# Windows PowerShell
cd C:\Users\ADMIN\tinhhoasach-backend
./run.ps1
```
```bash
# macOS / Linux / Git-Bash
cd tinhhoasach-backend
./run.sh
```

Script tự tạo `.venv`, cài deps, copy `.env` từ `.env.example`, rồi chạy uvicorn tại
http://127.0.0.1:8000 — Swagger UI ở **/docs**.

Ở DEV (chưa set `GOOGLE_SERVICE_ACCOUNT_JSON`): mọi `verify_*` chạy **stub** (token nào
cũng pass) để test luồng mua trước khi lên Play. RTDN chấp nhận không cần JWT khi
`ALLOW_UNSIGNED_RTDN=1`.

---

## Cấu trúc

```
app/
  config.py         # settings + assert_prod_ready() (die nếu prod config hở)
  db.py             # models (spec §7) + ledger Purchase + atomic_* helpers
  remote_config.py  # config server-driven: flags, iap_catalog, base_plans
  skus.py           # 1 product + 3 base plan; resolve tier ↔ base plan
  play_billing.py   # verify (stub/prod) + Pub/Sub JWT (fail-closed) + helpers
  entitlement.py    # get-or-create user, cohort tier, lazy-refill
  storage.py        # ký URL audio (scaffold → thay bằng Firebase/S3 khi prod)
  schemas.py        # Pydantic request models
  deps.py           # auth API-key
  seed.py           # seed sách/chương/category/free-daily
  main.py           # app, error envelope, lifespan
  routers/
    config.py   user.py   content.py   peruser.py   billing.py
```

## Endpoints

Đặt sẵn 2 biến shell cho gọn (server local, DEV stub mode), rồi paste curl ở cột bên phải:

```bash
H=http://127.0.0.1:8000
A='Authorization: dev-static-key-change-me'
```

| Endpoint | Mô tả (cho app) | curl |
|---|---|---|
| `GET /v1/config` | Gọi mỗi lần mở app. Trả `premium`, `pricing_tier`, cờ tính năng, `iap_catalog`+`base_plans` (dựng paywall) và sách free hôm nay. | `curl "$H/v1/config?uid=u1&country=VN" -H "$A"` |
| `POST /v1/user` | Đăng ký/cập nhật hồ sơ user, idempotent theo `uid`. Gọi ngay sau khi có uid. | `curl -X POST "$H/v1/user" -H "$A" -H 'Content-Type: application/json' -d '{"user_id":"u1","device_region":"VN","operating_system":"android"}'` |
| `POST /v1/events` | Gửi log sự kiện theo lô cho analytics/funnel. | `curl -X POST "$H/v1/events" -H "$A" -H 'Content-Type: application/json' -d '{"events":[{"event_name":"paywall_view","user_id":"u1"}]}'` |
| `GET /v1/books` | Danh sách sách, phân trang. Query: `category`, `page`, `limit`. | `curl "$H/v1/books?page=1&limit=20" -H "$A"` |
| `GET /v1/categories` | Danh sách thể loại (tên, icon, số sách). | `curl "$H/v1/categories" -H "$A"` |
| `GET /v1/search` | Tìm sách theo tên/tác giả. Query: `q`, `limit`. | `curl "$H/v1/search?q=atomic" -H "$A"` |
| `GET /v1/books/{id}` | Chi tiết sách + chương. Server khoá chương/audio nếu `premium=false` và không phải sách free hôm nay. Cần `uid`. | `curl "$H/v1/books/b_001?uid=u1" -H "$A"` |
| `POST /v1/library` | Lưu hoặc bỏ 1 sách khỏi thư viện (`action`: save/remove). | `curl -X POST "$H/v1/library?uid=u1" -H "$A" -H 'Content-Type: application/json' -d '{"book_id":"b_001","action":"save"}'` |
| `GET /v1/library` | Danh sách sách đã lưu của user. | `curl "$H/v1/library?uid=u1" -H "$A"` |
| `POST /v1/highlights` | Lưu 1 đoạn highlight, trả về `id`. | `curl -X POST "$H/v1/highlights?uid=u1" -H "$A" -H 'Content-Type: application/json' -d '{"book_id":"b_001","chapter_index":2,"text":"...","color":"yellow"}'` |
| `GET /v1/highlights` | Danh sách highlight của user. | `curl "$H/v1/highlights?uid=u1" -H "$A"` |
| `DELETE /v1/highlights/{id}` | Xoá 1 highlight theo id. | `curl -X DELETE "$H/v1/highlights/HID?uid=u1" -H "$A"` |
| `POST /v1/progress` | Lưu vị trí đọc + cập nhật streak khi mở app. Trả `current_streak`. | `curl -X POST "$H/v1/progress?uid=u1" -H "$A" -H 'Content-Type: application/json' -d '{"book_id":"b_001","chapter_index":2,"position":0.42}'` |
| `GET /v1/streak` | Chuỗi ngày đọc: `current_streak`, `best_streak`, 7 ngày gần nhất. | `curl "$H/v1/streak?uid=u1" -H "$A"` |
| `POST /v1/purchase/verify` | Client gọi NGAY sau khi Play trả `purchase_token` → server verify với Google → bật `premium`. | `curl -X POST "$H/v1/purchase/verify" -H "$A" -H 'Content-Type: application/json' -d '{"user_id":"u1","platform":"android","product_id":"yearly_pro","purchase_token":"tok-1"}'` |
| `POST /v1/purchase/restore` | Màn Settings → "Khôi phục". Gửi các token active, server chọn tier cao nhất còn hiệu lực. | `curl -X POST "$H/v1/purchase/restore" -H "$A" -H 'Content-Type: application/json' -d '{"user_id":"u1","purchases":[{"product_id":"yearly_pro","purchase_token":"tok-1"}]}'` |
| `POST /webhook/play-billing` | **Google gọi, KHÔNG phải app.** Nguồn sự thật cho renew/cancel/refund. Không cần API key (xác thực bằng Pub/Sub JWT). | `curl -X POST "$H/webhook/play-billing" -H 'Content-Type: application/json' -d '{"message":{"data":"eyJwYWNrYWdlTmFtZSI6ImlvLnRpbmhob2FzYWNoLmFwcCIsInN1YnNjcmlwdGlvbk5vdGlmaWNhdGlvbiI6eyJub3RpZmljYXRpb25UeXBlIjoyLCJwdXJjaGFzZVRva2VuIjoidG9rLTEifX0="}}'` |

**Ghi chú cho app dev:**
- Header bắt buộc mọi call (trừ webhook): `Authorization: <APP_API_KEY>` (raw key, **không** phải Bearer). Trong curl là biến `$A`.
- Nhóm per-user (`library`/`highlights`/`progress`/`streak`): truyền `uid` qua `?uid=` **hoặc** header `X-Uid`.
- `{id}` / `HID`: thay bằng id thật (vd id trả về khi tạo highlight).
- Webhook curl trên **chỉ chạy ở DEV**: chuỗi `data` là base64 của 1 RTDN `RENEWED` cho `tok-1`. Ở PROD, Google tự gọi và ký JWT — bạn không tự curl.
- Mọi response bọc envelope `{"status":{"code","message"}, ...}` (xem §9 spec).

---

## SKU convention (quan trọng)

Theo yêu cầu: **1 subscription product** (`PREMIUM_PRODUCT_ID`) với **3 base plan mặc định**
lấy từ remote config:

```
release-weekly-plan   →  weekly_pro
release-monthly-plan  →  monthly_pro
release-yearly-plan   →  yearly_pro
```

Client đọc `base_plans` trong `/v1/config`, map bucket → base plan, mua product đó với base
plan tương ứng. Server verify đọc **base plan THẬT** từ Google và match lại (chống spoof
weekly→yearly). Đổi tên plan / giá chỉ cần sửa remote config (env `BASE_PLAN_*` hoặc bảng
`remote_config`), **không cần update app**. `lifetime` là in-app product một lần.

---

## Billing — các bất biến đã port (đừng đổi)

1. **Verify TRƯỚC khi claim ledger** — token giả không bao giờ chiếm slot UNIQUE.
2. **Ledger keyed `purchase_token` UNIQUE** — grant đúng 1 lần (chống replay + race).
3. **Tier re-verification** — đọc `basePlanId` thật, `409 Tier mismatch` nếu lệch.
4. **Leaked-token gate** — chỉ migrate cross-device khi email verified khớp
   `email_at_grant`. v1 ẩn danh → từ chối; chủ thật khôi phục bằng đăng nhập (v1.1).
5. **RTDN fail-closed** — verify Pub/Sub JWT (`aud`/`iss`) TRƯỚC; audience chưa set ⇒ từ
   chối hết. Body cap 64KB (đếm stream). **Luôn trả 200** để Pub/Sub không retry-loop.
6. **`linkedPurchaseToken` revoke** khi upgrade/downgrade + `expire_all()` chống ORM ghi đè.
7. **Clawback `UPDATE … WHERE revoked=0`** — 2 VOIDED redelivery không claw back 2 lần.
8. **Lazy-refill** mỗi `/v1/config` & `/v1/user` — an toàn khi lỡ 1 RTDN.

Map RTDN: RENEWED/RECOVERED/RESTARTED→gia hạn (skip nếu grant trong 90% chu kỳ THẬT theo
`duration_days`), EXPIRED(13)→drop (không lùi expiry), REVOKED(12)/VOIDED→clawback,
IN_GRACE(6)→giữ Pro tới expiry, ON_HOLD(5)→drop Pro giữ sub_type.

---

## Lên PROD

1. `DATABASE_URL=postgresql+psycopg://…` (bỏ comment `psycopg` trong requirements).
2. Set `GOOGLE_SERVICE_ACCOUNT_JSON` (Play Developer API) → tắt stub, verify thật.
3. Tạo Pub/Sub topic + push subscription → `/webhook/play-billing`; set
   `GOOGLE_PUBSUB_AUDIENCE` = URL webhook.
4. `REQUIRE_PROD=1` + bỏ `ALLOW_UNSIGNED_RTDN` → `assert_prod_ready()` chặn boot nếu hở.
5. Thay `storage.sign_audio_url` bằng Firebase Storage / S3 signed URL thật.

Chi tiết wiring GCP/Play Console: xem §8 của `BILLING_HANDOFF.md`.

---

## Smoke test (curl, DEV stub)

```bash
KEY="dev-static-key-change-me"; H="http://127.0.0.1:8000"

# config (tự tạo user, tier theo cohort)
curl -s "$H/v1/config?uid=u_test&country=VN" -H "Authorization: $KEY"

# mua yearly (stub pass) → premium=true
curl -s -X POST "$H/v1/purchase/verify" -H "Authorization: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u_test","platform":"android","product_id":"yearly_pro","purchase_token":"tok-1"}'

# spoof: mua weekly nhưng khai yearly → 409 (chỉ chặn ở PROD; DEV stub trust client)

# replay cùng token → không double-grant
curl -s -X POST "$H/v1/purchase/verify" -H "Authorization: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u_test","platform":"android","product_id":"yearly_pro","purchase_token":"tok-1"}'

# RTDN EXPIRED (DEV: không cần JWT). data = base64 RTDN JSON.
DATA=$(printf '{"packageName":"io.tinhhoasach.app","subscriptionNotification":{"notificationType":13,"purchaseToken":"tok-1"}}' | base64 -w0)
curl -s -X POST "$H/webhook/play-billing" -H "Content-Type: application/json" \
  -d "{\"message\":{\"data\":\"$DATA\"}}"
```

> Lưu ý: `base64 -w0` (Linux). macOS dùng `base64`. Streak dùng ngày UTC của server (scaffold);
> chỉnh theo `time_zone_offset_seconds` nếu cần chính xác theo múi giờ user.
