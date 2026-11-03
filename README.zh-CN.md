# CGA Studio

语言：[한국어](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [Tiếng Việt](README.vi.md) | [Français](README.fr.md) | [Deutsch](README.de.md)

CGA Studio 是一个基于 Web 的对话式 AI 机器人设计、训练、测试与运营平台。它可在一个工作区中管理机器人及版本、意图、实体、词典、对话流程、回复、渠道、测试和运行状态。

## 主要功能

- 支持韩语、英语、简体中文、日语、越南语、法语和德语
- 支持 ML、语义向量、外部嵌入和基于 LLM 的 NLU
- 支持固定回复、Semantic RAG、LLM RAG 和 LLM 直接回复
- 通过独立 NLU Training Worker 执行版本化训练与索引
- Bot Test 以及 Runtime、Variables、Trace 分析
- Webchat、Kakao 和 Microsoft Teams 渠道连接
- Kakao 快速回复、基本卡片、列表卡片和轮播转换
- 用户、群组、许可证、队列、日志和运行状态管理
- 默认 CPU 运行，可选 NVIDIA GPU 加速

## 系统组成

| 组件 | 作用 |
|---|---|
| `studio` | 基于 Next.js 的 CGA Studio Web 界面 |
| `api` | 基于 FastAPI 的认证、设计、运行与管理 API |
| `nlu-training-worker` | ML 与语义训练、索引任务 |
| `vector-worker` | 嵌入与向量检索 |
| `redis` | 查询缓存 |
| PostgreSQL | 外部数据库，用于用户、机器人、版本和运营数据 |

## 安装要求

- Git
- Docker Engine 与 Docker Compose V2（`docker compose`）
- 已创建 CGA 数据库和用户且可访问的 PostgreSQL
- 已签发的 CGA 许可证及用于验证它的公钥 PEM
- GPU 模式需要 NVIDIA 驱动和 NVIDIA Container Toolkit

> 本仓库不包含 PostgreSQL 容器。`CGA_DATABASE_URL` 中的数据库主机必须能通过外部 Docker 网络 `common_default` 访问。

## 安装

### 1. 获取代码并创建环境文件

```bash
git clone https://github.com/OWNER/CGA.git
cd CGA
cp .env.example .env
```
Windows PowerShell 请运行 `Copy-Item .env.example .env`。

### 2. 设置必需环境变量

```dotenv
CGA_DATABASE_URL=postgresql+psycopg://cga_user:replace-with-password@shared-db:5432/cga
CGA_JWT_SECRET=replace-with-a-random-secret-of-at-least-32-characters
CGA_INITIAL_ADMIN_PASSWORD=replace-with-a-strong-password
CGA_LICENSE_PUBLIC_KEY=base64-encoded-public-pem
CORS_ORIGINS=http://localhost:5173
```
`CGA_LICENSE_PUBLIC_KEY` 必须是**完整公钥 PEM 文件**的 Base64 编码，而不是 SHA-256 指纹。切勿提交私钥或真实 `.env`。

### 3. 准备网络和 PostgreSQL

如果网络尚不存在，仅创建一次：

```bash
docker network create proxy-network
docker network create common_default
```
将 PostgreSQL 容器连接到 `common_default`，并确认 `CGA_DATABASE_URL` 的主机名、数据库、用户和密码与服务器一致。`proxy-network` 用于连接反向代理。

### 4. CPU 模式启动

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```
### 5. NVIDIA GPU 模式（可选）

在 `.env` 中设置 `CGA_TORCH_INDEX_URL`，并加载 GPU overlay：

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml up -d --build
```
GPU overlay 会为 `api`、`nlu-training-worker` 和 `vector-worker` 分配 GPU。仅使用 CPU 时不要加载该文件。

### 6. 检查并登录

```bash
docker compose ps
curl http://localhost:5173/health/ready
```
打开 `http://localhost:5173`。初始管理员 ID 为 `master`，密码是 `CGA_INITIAL_ADMIN_PASSWORD` 的值。登录后在 **Admin > 许可证** 上传已签发的许可证文件。

## 更新与停止

执行 `git pull --ff-only` 后，再次运行相应的 CPU 或 GPU 构建命令。保留数据卷并停止服务：

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
```
除非确定要删除持久数据，否则不要使用 `down -v`。

## 故障排除

- `CGA_DATABASE_URL is required`：检查 `.env` 中的数据库连接字符串。
- 数据库连接失败：检查 PostgreSQL 与 `common_default` 网络。
- 许可证未应用：确认公钥 PEM 的 Base64 值，并上传匹配的许可证。
- GPU 不可用：先检查 `nvidia-smi` 和 NVIDIA Container Toolkit。
- Studio 未就绪：检查 `docker compose ps` 和 `docker compose logs api studio`。

## 用户文档

- [CGA Getting Started](docs/manual/cga-getting-started/README.zh-CN.md)
- [CGA 用户手册](docs/manual/cga-user-manual/README.zh-CN.md)
- [CGA NLU 指南](docs/manual/cga-nlu-guide/README.zh-CN.md)

## 许可证与安全

公开源代码不等于授予 CGA 产品许可证，实际使用可能需要已签发的许可证。请勿公开 `.env`、数据库密码、JWT Secret、许可证私钥、Provider API Key 或生产凭据。
