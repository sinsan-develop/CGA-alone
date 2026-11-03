# CGA Studio

언어: [한국어](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [Tiếng Việt](README.vi.md) | [Français](README.fr.md) | [Deutsch](README.de.md)

CGA Studio는 대화형 AI 봇을 설계하고 학습하며 여러 채널에 연결해 운영하는 웹 기반 제작·관리 플랫폼입니다. 하나의 화면에서 봇과 버전, 의도·개체·사전, 대화 흐름, 답변, 채널, 테스트 및 운영 상태를 관리할 수 있습니다.

## 주요 기능

- 한국어, 영어, 중국어(간체), 일본어, 베트남어, 프랑스어, 독일어 지원
- ML, Semantic Vector, 외부 임베딩, LLM 기반 NLU 구성
- 정해진 답변, Semantic RAG, LLM RAG, LLM 답변 방식
- 봇 버전별 학습·인덱싱과 별도 NLU Training Worker
- 봇 테스트 화면과 Runtime·Variables·Trace 분석 데이터
- Webchat, Kakao, Microsoft Teams 채널 연계
- Kakao 간편 응답·기본 카드·리스트 카드·캐러셀 변환
- 사용자·그룹·라이선스·Queue·로그·운영 상태 관리
- CPU 기본 실행과 NVIDIA GPU 선택 실행

## 구성

| 구성 요소 | 역할 |
|---|---|
| `studio` | Next.js 기반 CGA Studio 웹 화면 |
| `api` | FastAPI 기반 인증·설계·실행·관리 API |
| `nlu-training-worker` | ML·Semantic 학습 및 인덱싱 작업 처리 |
| `vector-worker` | 임베딩과 벡터 검색 처리 |
| `redis` | 조회 캐시 |
| PostgreSQL | 사용자·봇·버전·운영 데이터 저장. 외부 DB를 사용합니다. |

## 설치 전 준비

- Git
- Docker Engine 및 Docker Compose V2(`docker compose`)
- CGA 데이터베이스와 사용자가 생성된 PostgreSQL 서버
- 발급받은 CGA 라이선스와 그 라이선스를 검증하는 공개키 PEM
- GPU 실행 시 NVIDIA 드라이버와 NVIDIA Container Toolkit

> 이 저장소는 PostgreSQL 컨테이너를 포함하지 않습니다. `CGA_DATABASE_URL`의 DB 호스트가 `common_default` Docker 네트워크에서 접근 가능해야 합니다.

## 설치

### 1. 저장소와 환경 파일 준비

```bash
git clone https://github.com/OWNER/CGA.git
cd CGA
cp .env.example .env
```

Windows PowerShell에서는 `Copy-Item .env.example .env`를 사용합니다.

### 2. 필수 환경 변수 설정

`.env`에서 최소한 다음 값을 운영 환경에 맞게 지정합니다.

```dotenv
CGA_DATABASE_URL=postgresql+psycopg://cga_user:replace-with-password@shared-db:5432/cga
CGA_JWT_SECRET=replace-with-a-random-secret-of-at-least-32-characters
CGA_INITIAL_ADMIN_PASSWORD=replace-with-a-strong-password
CGA_LICENSE_PUBLIC_KEY=base64-encoded-public-pem
CORS_ORIGINS=http://localhost:5173
```

`CGA_LICENSE_PUBLIC_KEY`에는 공개키 SHA-256 지문이 아니라 **공개키 PEM 파일 전체를 Base64로 인코딩한 값**을 넣습니다. 개인키와 실제 `.env`는 절대로 Git에 커밋하지 마십시오.

### 3. 외부 네트워크와 PostgreSQL 연결

다음 네트워크가 없을 때 한 번만 생성합니다.

```bash
docker network create proxy-network
docker network create common_default
```

PostgreSQL 컨테이너를 `common_default`에 연결하고, `CGA_DATABASE_URL`의 호스트 이름·DB·사용자·암호가 실제 PostgreSQL 설정과 일치하는지 확인합니다. `proxy-network`는 Studio를 리버스 프록시에 연결할 때 사용합니다.

### 4. CPU 환경 실행

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

### 5. NVIDIA GPU 환경 실행(선택)

`.env`에 `CGA_TORCH_INDEX_URL`을 지정한 뒤 GPU 오버레이를 함께 사용합니다.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml up -d --build
```

GPU 오버레이는 `api`, `nlu-training-worker`, `vector-worker`에 GPU 장치를 할당합니다. CPU 환경에서는 `docker-compose.gpu.yml`을 포함하지 않습니다.

### 6. 상태 확인과 최초 로그인

```bash
docker compose ps
curl http://localhost:5173/health/ready
```

브라우저에서 `http://localhost:5173`을 엽니다. 최초 관리자 ID는 `master`, 암호는 `CGA_INITIAL_ADMIN_PASSWORD` 값입니다. 로그인 후 **Admin > 라이선스**에서 발급받은 라이선스 파일을 업로드합니다.

## 업데이트와 중지

업데이트할 때는 `git pull --ff-only` 후 사용 중인 CPU 또는 GPU 명령으로 다시 빌드·실행합니다. 서비스를 중지하되 데이터 볼륨을 보존하려면 다음 명령을 사용합니다.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
```

`down -v`는 영구 볼륨을 삭제하므로 데이터 삭제 의도가 없다면 사용하지 마십시오.

## 문제 해결

- `CGA_DATABASE_URL is required`: `.env`의 필수 DB 연결 문자열을 확인합니다.
- DB 연결 실패: PostgreSQL 컨테이너와 `common_default` 네트워크 연결을 확인합니다.
- 라이선스가 적용되지 않음: 공개키 PEM의 Base64 값인지 확인한 뒤 올바른 라이선스 파일을 업로드합니다.
- GPU가 보이지 않음: `nvidia-smi`와 `docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu22.04 nvidia-smi`를 먼저 확인합니다.
- Studio 준비 상태 실패: `docker compose ps`와 `docker compose logs api studio`를 확인합니다.

## 사용자 문서

- [CGA Getting Started](docs/manual/cga-getting-started/README.md)
- [CGA 사용자 설명서](docs/manual/cga-user-manual/README.md)
- [CGA NLU 활용 가이드](docs/manual/cga-nlu-guide/README.md)

## 라이선스와 보안

저장소가 공개되어 있는 것과 CGA 제품 사용 권한은 별개입니다. 실제 사용에는 발급된 CGA 라이선스가 필요할 수 있습니다. `.env`, DB 암호, JWT Secret, 라이선스 개인키, Provider API Key 및 운영 인증정보를 저장소에 올리지 마십시오.
