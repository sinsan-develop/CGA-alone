# CGA Studio

Ngôn ngữ: [한국어](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [Tiếng Việt](README.vi.md) | [Français](README.fr.md) | [Deutsch](README.de.md)

CGA Studio là nền tảng web để thiết kế, huấn luyện, kiểm thử và vận hành bot AI hội thoại trên nhiều kênh. Hệ thống quản lý bot và phiên bản, ý định, thực thể, từ điển, luồng hội thoại, câu trả lời, kênh, kiểm thử và trạng thái vận hành trong một không gian làm việc.

## Tính năng chính

- Hỗ trợ tiếng Hàn, tiếng Anh, tiếng Trung giản thể, tiếng Nhật, tiếng Việt, tiếng Pháp và tiếng Đức
- NLU dựa trên ML, Semantic Vector, embedding bên ngoài và LLM
- Câu trả lời cố định, Semantic RAG, LLM RAG và câu trả lời trực tiếp từ LLM
- Huấn luyện và lập chỉ mục theo phiên bản bằng NLU Training Worker riêng
- Bot Test với dữ liệu phân tích Runtime, Variables và Trace
- Kết nối Webchat, Kakao và Microsoft Teams
- Chuyển đổi quick reply, basic card, list card và carousel của Kakao
- Quản trị người dùng, nhóm, giấy phép, Queue, log và trạng thái hệ thống
- Chạy CPU mặc định, hỗ trợ tăng tốc NVIDIA GPU tùy chọn

## Kiến trúc

| Thành phần | Vai trò |
|---|---|
| `studio` | Giao diện CGA Studio bằng Next.js |
| `api` | API FastAPI cho xác thực, thiết kế, runtime và quản trị |
| `nlu-training-worker` | Xử lý huấn luyện và lập chỉ mục ML/Semantic |
| `vector-worker` | Embedding và tìm kiếm vector |
| `redis` | Bộ nhớ đệm truy vấn |
| PostgreSQL | CSDL ngoài cho người dùng, bot, phiên bản và dữ liệu vận hành |

## Yêu cầu

- Git
- Docker Engine và Docker Compose V2 (`docker compose`)
- PostgreSQL có thể truy cập, đã tạo CSDL và người dùng CGA
- Giấy phép CGA đã cấp và public-key PEM để xác minh
- NVIDIA driver và NVIDIA Container Toolkit nếu dùng GPU

> Kho mã không chứa container PostgreSQL. Máy chủ DB trong `CGA_DATABASE_URL` phải truy cập được qua mạng Docker ngoài `common_default`.

## Cài đặt

### 1. Lấy mã nguồn và tạo tệp môi trường

```bash
git clone https://github.com/OWNER/CGA.git
cd CGA
cp .env.example .env
```
Trên Windows PowerShell, dùng `Copy-Item .env.example .env`.

### 2. Cấu hình biến bắt buộc

```dotenv
CGA_DATABASE_URL=postgresql+psycopg://cga_user:replace-with-password@shared-db:5432/cga
CGA_JWT_SECRET=replace-with-a-random-secret-of-at-least-32-characters
CGA_INITIAL_ADMIN_PASSWORD=replace-with-a-strong-password
CGA_LICENSE_PUBLIC_KEY=base64-encoded-public-pem
CORS_ORIGINS=http://localhost:5172
```
`CGA_LICENSE_PUBLIC_KEY` phải là Base64 của **toàn bộ tệp public-key PEM**, không phải dấu vân tay SHA-256. Không commit private key hoặc tệp `.env` thật.

### 3. Chuẩn bị mạng và PostgreSQL

Chỉ tạo các mạng sau một lần nếu chưa có:

```bash
docker network create proxy-network
docker network create common_default
```
Kết nối container PostgreSQL với `common_default`, rồi kiểm tra host, database, user và password trong `CGA_DATABASE_URL`. `proxy-network` dùng cho reverse proxy.

### 4. Chạy ở chế độ CPU

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```
### 5. Chạy với NVIDIA GPU (tùy chọn)

Đặt `CGA_TORCH_INDEX_URL` trong `.env`, sau đó thêm GPU overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml up -d --build
```
GPU overlay gán GPU cho `api`, `nlu-training-worker` và `vector-worker`. Không thêm tệp này khi chỉ dùng CPU.

### 6. Kiểm tra và đăng nhập

```bash
docker compose ps
curl http://localhost:5172/health/ready
```
Mở `http://localhost:5172`. ID quản trị ban đầu là `master`; mật khẩu là giá trị `CGA_INITIAL_ADMIN_PASSWORD`. Sau khi đăng nhập, tải giấy phép đã cấp tại **Admin > Giấy phép**.

## Cập nhật và dừng

Chạy `git pull --ff-only`, sau đó chạy lại lệnh build CPU hoặc GPU. Để dừng dịch vụ nhưng giữ volume:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
```
Không dùng `down -v` nếu không chủ ý xóa dữ liệu lưu trữ.

## Xử lý sự cố

- `CGA_DATABASE_URL is required`: kiểm tra chuỗi kết nối DB trong `.env`.
- Không kết nối DB: kiểm tra PostgreSQL và mạng `common_default`.
- Giấy phép chưa áp dụng: kiểm tra Base64 của public PEM và tải đúng tệp giấy phép.
- Không thấy GPU: kiểm tra `nvidia-smi` và NVIDIA Container Toolkit.
- Studio chưa ready: kiểm tra `docker compose ps` và `docker compose logs api studio`.

## Tài liệu

- [CGA Getting Started](docs/manual/cga-getting-started/README.vi.md)
- [Hướng dẫn sử dụng CGA](docs/manual/cga-user-manual/README.vi.md)
- [Hướng dẫn CGA NLU](docs/manual/cga-nlu-guide/README.vi.md)

## Giấy phép và bảo mật

Mã nguồn công khai không đồng nghĩa với giấy phép sử dụng sản phẩm CGA. Việc sử dụng có thể cần giấy phép đã cấp. Không công khai `.env`, mật khẩu DB, JWT Secret, private key giấy phép, Provider API Key hoặc thông tin xác thực vận hành.
