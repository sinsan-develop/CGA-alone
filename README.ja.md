# CGA Studio

言語：[한국어](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [Tiếng Việt](README.vi.md) | [Français](README.fr.md) | [Deutsch](README.de.md)

CGA Studioは、対話型AIボットを設計・学習・テストし、複数チャネルで運用するWebベースのプラットフォームです。ボットとバージョン、インテント、エンティティ、辞書、対話フロー、回答、チャネル、テスト、運用状態を一つのワークスペースで管理できます。

## 主な機能

- 韓国語、英語、中国語（簡体字）、日本語、ベトナム語、フランス語、ドイツ語
- ML、Semantic Vector、外部Embedding、LLMベースのNLU
- 固定回答、Semantic RAG、LLM RAG、LLM回答
- 専用NLU Training Workerによるバージョン単位の学習・インデックス作成
- Bot TestとRuntime・Variables・Trace分析
- Webchat、Kakao、Microsoft Teams連携
- Kakaoクイック返信、基本カード、リストカード、カルーセル変換
- ユーザー、グループ、ライセンス、Queue、ログ、稼働状態の管理
- CPU標準実行とオプションのNVIDIA GPUアクセラレーション

## 構成

| コンポーネント | 役割 |
|---|---|
| `studio` | Next.jsベースのCGA Studio Web画面 |
| `api` | FastAPIベースの認証・設計・実行・管理API |
| `nlu-training-worker` | ML・Semantic学習とインデックス処理 |
| `vector-worker` | Embeddingとベクトル検索 |
| `redis` | 参照キャッシュ |
| PostgreSQL | ユーザー、ボット、バージョン、運用データ用の外部DB |

## 必要条件

- Git
- Docker EngineとDocker Compose V2（`docker compose`）
- CGA用DBとユーザーを作成済みの到達可能なPostgreSQL
- 発行済みCGAライセンスと検証用公開鍵PEM
- GPUモードではNVIDIAドライバーとNVIDIA Container Toolkit

> このリポジトリにはPostgreSQLコンテナが含まれません。`CGA_DATABASE_URL`のDBホストは外部Dockerネットワーク`common_default`から到達できる必要があります。

## インストール

### 1. ソースと環境ファイル

```bash
git clone https://github.com/OWNER/CGA.git
cd CGA
cp .env.example .env
```
Windows PowerShellでは`Copy-Item .env.example .env`を実行します。

### 2. 必須環境変数

```dotenv
CGA_DATABASE_URL=postgresql+psycopg://cga_user:replace-with-password@shared-db:5432/cga
CGA_JWT_SECRET=replace-with-a-random-secret-of-at-least-32-characters
CGA_INITIAL_ADMIN_PASSWORD=replace-with-a-strong-password
CGA_LICENSE_PUBLIC_KEY=base64-encoded-public-pem
CORS_ORIGINS=http://localhost:5173
```
`CGA_LICENSE_PUBLIC_KEY`にはSHA-256フィンガープリントではなく、**公開鍵PEMファイル全体をBase64化した値**を設定します。秘密鍵と実際の`.env`はコミットしないでください。

### 3. ネットワークとPostgreSQL

存在しない場合に一度だけ作成します。

```bash
docker network create proxy-network
docker network create common_default
```
PostgreSQLコンテナを`common_default`へ接続し、`CGA_DATABASE_URL`のホスト、DB、ユーザー、パスワードを実環境に合わせます。`proxy-network`はリバースプロキシ接続に使用します。

### 4. CPUモード

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```
### 5. NVIDIA GPUモード（任意）

`.env`に`CGA_TORCH_INDEX_URL`を設定し、GPU overlayを追加します。

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml up -d --build
```
GPU overlayは`api`、`nlu-training-worker`、`vector-worker`へGPUを割り当てます。CPU環境では含めません。

### 6. 確認と初回ログイン

```bash
docker compose ps
curl http://localhost:5173/health/ready
```
`http://localhost:5173`を開きます。初期管理者IDは`master`、パスワードは`CGA_INITIAL_ADMIN_PASSWORD`の値です。ログイン後、**Admin > ライセンス**から発行済みライセンスをアップロードします。

## 更新と停止

`git pull --ff-only`の後、CPUまたはGPUの起動コマンドを再実行します。永続ボリュームを残して停止するには：

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
```
永続データを削除する意図がなければ`down -v`を使用しないでください。

## トラブルシューティング

- `CGA_DATABASE_URL is required`：`.env`のDB接続文字列を確認します。
- DB接続失敗：PostgreSQLと`common_default`ネットワークを確認します。
- ライセンス未適用：公開鍵PEMのBase64値とライセンスファイルの組み合わせを確認します。
- GPUが使えない：`nvidia-smi`とNVIDIA Container Toolkitを確認します。
- Studioがreadyにならない：`docker compose ps`と`docker compose logs api studio`を確認します。

## ドキュメント

- [CGA Getting Started](docs/manual/cga-getting-started/README.ja.md)
- [CGAユーザーマニュアル](docs/manual/cga-user-manual/README.ja.md)
- [CGA NLUガイド](docs/manual/cga-nlu-guide/README.ja.md)

## ライセンスとセキュリティ

ソース公開とCGA製品ライセンスは別です。利用には発行済みライセンスが必要な場合があります。`.env`、DBパスワード、JWT Secret、ライセンス秘密鍵、Provider API Key、運用認証情報を公開しないでください。
