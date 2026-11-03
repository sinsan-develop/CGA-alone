# CGA Studio

Language: [한국어](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [Tiếng Việt](README.vi.md) | [Français](README.fr.md) | [Deutsch](README.de.md)

CGA Studio is a web-based platform for designing, training, testing, and operating conversational AI bots across multiple channels. It manages bots and versions, intents, entities, dictionaries, dialog flows, responses, channels, tests, and operational status in one workspace.

## Key features

- Korean, English, Simplified Chinese, Japanese, Vietnamese, French, and German
- ML, semantic vector, external embedding, and LLM-based NLU
- Fixed responses, Semantic RAG, LLM RAG, and direct LLM responses
- Versioned training and indexing through a dedicated NLU Training Worker
- Bot Test with Runtime, Variables, and Trace analysis
- Webchat, Kakao, and Microsoft Teams channel integration
- Kakao quick replies, basic cards, list cards, and carousel rendering
- Administration for users, groups, licenses, queues, logs, and runtime health
- CPU by default with optional NVIDIA GPU acceleration

## Architecture

| Component | Purpose |
|---|---|
| `studio` | Next.js CGA Studio web application |
| `api` | FastAPI authentication, authoring, runtime, and administration API |
| `nlu-training-worker` | ML and semantic training/indexing jobs |
| `vector-worker` | Embedding and vector search |
| `redis` | Read cache |
| PostgreSQL | External database for users, bots, versions, and operational data |

## Prerequisites

- Git
- Docker Engine and Docker Compose V2 (`docker compose`)
- A reachable PostgreSQL server with a CGA database and user
- An issued CGA license and the public-key PEM used to verify it
- NVIDIA driver and NVIDIA Container Toolkit for GPU mode

> This repository does not include a PostgreSQL container. The database host in `CGA_DATABASE_URL` must be reachable through the external `common_default` Docker network.

## Installation

### 1. Clone and create the environment file

```bash
git clone https://github.com/OWNER/CGA.git
cd CGA
cp .env.example .env
```
On Windows PowerShell, run `Copy-Item .env.example .env`.

### 2. Configure required variables

```dotenv
CGA_DATABASE_URL=postgresql+psycopg://cga_user:replace-with-password@shared-db:5432/cga
CGA_JWT_SECRET=replace-with-a-random-secret-of-at-least-32-characters
CGA_INITIAL_ADMIN_PASSWORD=replace-with-a-strong-password
CGA_LICENSE_PUBLIC_KEY=base64-encoded-public-pem
CORS_ORIGINS=http://localhost:5172
```
`CGA_LICENSE_PUBLIC_KEY` must contain the Base64 encoding of the **complete public-key PEM file**, not its SHA-256 fingerprint. Never commit the private key or the real `.env` file.

### 3. Prepare networks and PostgreSQL

Create these networks once if they do not already exist:

```bash
docker network create proxy-network
docker network create common_default
```
Connect the PostgreSQL container to `common_default`, then verify that the host, database, user, and password in `CGA_DATABASE_URL` match the server. `proxy-network` connects Studio to a reverse proxy.

### 4. Start in CPU mode

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```
### 5. Start with NVIDIA GPU support (optional)

Set `CGA_TORCH_INDEX_URL` in `.env`, then include the GPU overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml up -d --build
```
The GPU overlay assigns GPU devices to `api`, `nlu-training-worker`, and `vector-worker`. Do not include it for CPU-only deployments.

### 6. Verify and sign in

```bash
docker compose ps
curl http://localhost:5172/health/ready
```
Open `http://localhost:5172`. The initial administrator ID is `master`; its password is the value of `CGA_INITIAL_ADMIN_PASSWORD`. After signing in, upload the issued license file under **Admin > License**.

## Update and stop

Run `git pull --ff-only`, then repeat the CPU or GPU build command. To stop services while keeping persistent volumes:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
```
Do not use `down -v` unless you intend to delete persistent data.

## Troubleshooting

- `CGA_DATABASE_URL is required`: set the required database URL in `.env`.
- Database connection failure: verify PostgreSQL and the `common_default` network.
- License not applied: verify that the variable is the Base64 form of the public PEM and upload the matching license file.
- GPU unavailable: verify `nvidia-smi` and the NVIDIA Container Toolkit first.
- Studio not ready: inspect `docker compose ps` and `docker compose logs api studio`.

## Documentation

- [CGA Getting Started](docs/manual/cga-getting-started/README.en.md)
- [CGA User Manual](docs/manual/cga-user-manual/README.en.md)
- [CGA NLU Guide](docs/manual/cga-nlu-guide/README.en.md)

## License and security

Public source visibility does not grant a CGA product license. An issued CGA license may be required for use. Never publish `.env`, database passwords, JWT secrets, license private keys, provider API keys, or production credentials.
