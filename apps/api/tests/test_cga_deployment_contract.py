from __future__ import annotations

from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[3]

WEB_INTERNAL_API_FILES = (
    "apps/web/lib/api.ts",
    "apps/web/app/api/v1/[...path]/route.ts",
    "apps/web/app/api/studio/rag/_shared.ts",
    "apps/web/app/assets/[...segments]/route.ts",
    "apps/web/app/files/[category]/[...segments]/route.ts",
    "apps/web/app/health/ready/route.ts",
)


def _read(relative_path: str) -> str:
    return (ROOT_DIR / relative_path).read_text(encoding="utf-8")


def test_web_server_uses_cga_internal_api_variable_only() -> None:
    for relative_path in WEB_INTERNAL_API_FILES:
        source = _read(relative_path)
        assert "CGA_INTERNAL_API_BASE_URL" in source, relative_path
        assert "AIDOT_INTERNAL_API_BASE_URL" not in source, relative_path
        assert "NEXT_PUBLIC_API_BASE_URL" not in source, relative_path


def test_compose_exposes_cga_api_only_through_proxy_network() -> None:
    compose = _read("docker-compose.yml")
    assert "container_name: cga-alone-studio" in compose
    assert "container_name: cga-alone-api" in compose
    assert "CGA_INTERNAL_API_BASE_URL: http://cga-alone-api:8000" in compose
    assert '"5172:5173"' in compose
    assert '"4173:4173"' not in compose
    assert "http://127.0.0.1:5173/health/ready" in compose
    dockerfile = _read("apps/web/Dockerfile")
    assert "EXPOSE 5173" in dockerfile
    assert '"--port", "5173"' in dockerfile
    assert '"8000:8000"' not in compose
    assert "- .:/workspace" not in compose
    private_checkout = "/home/" + "ubuntu/deploy/cga:/workspace"
    assert private_checkout not in compose
    assert "name: proxy-network" in compose


def test_compose_does_not_embed_operating_domains() -> None:
    compose = _read("docker-compose.yml")
    private_domain = "sinsan" + ".kr"

    assert private_domain not in compose
    assert "CORS_ORIGINS: ${CORS_ORIGINS:-http://localhost:4173}" in compose
    assert compose.count("CGA_LEGACY_ASSET_ROOT: ${CGA_LEGACY_ASSET_ROOT:-}") == 2


def test_compose_runs_an_isolated_cga_vector_worker() -> None:
    compose = _read("docker-compose.yml")
    env_example = _read(".env.example")

    assert "container_name: cga-alone-vector-worker" in compose
    assert "image: cga-alone-vector-worker:latest" in compose
    assert "AIDOT_VECTOR_WORKER_BASE_URL: http://cga-alone-vector-worker:8350" in compose
    assert "AIDOT_VECTOR_STORAGE_DIR: /workspace/apps/vector-worker/data" in compose
    assert "cga_vector_data:/workspace/apps/vector-worker/data" in compose
    assert "vector-worker:\n        condition: service_healthy" in compose
    assert "http://127.0.0.1:8350/health" in compose
    assert "cga_internal:" in compose
    assert "driver: bridge" in compose
    assert '"8350:8350"' not in compose
    assert "CGA_VECTOR_EMBEDDING_PROVIDER" in env_example
    assert "CGA_VECTOR_EMBEDDING_MODEL" in env_example
    assert "CGA_OLLAMA_BASE_URL" in env_example


def test_compose_runs_cga_nlu_training_worker_with_shared_model_storage() -> None:
    compose = _read("docker-compose.yml")

    assert "nlu-training-worker:" in compose
    assert "container_name: cga-alone-nlu-training-worker" in compose
    assert 'command: ["python", "-m", "app.workers.nlu_training_worker"]' in compose
    assert "image: cga-alone-api:latest" in compose
    assert compose.count("cga_nlu_models:/workspace/data/nlu_models") == 2
    assert compose.count("cga_api_storage:/workspace/storage") == 2
    assert "AIDOT_VECTOR_WORKER_BASE_URL: http://cga-alone-vector-worker:8350" in compose
    assert "REDIS_URL: ${REDIS_URL:-redis://cga-alone-redis:6379/0}" in compose
    assert "db_net" in compose
    assert "cga_internal" in compose


def test_vector_worker_uses_cpu_only_torch_by_default() -> None:
    dockerfile = _read("apps/vector-worker/Dockerfile")

    assert "https://download.pytorch.org/whl/cpu" in dockerfile
    assert 'if [ "$AIDOT_GPU" = "true" ]' in dockerfile
    assert 'test -n "$AIDOT_TORCH_INDEX_URL"' in dockerfile
    assert dockerfile.index("pip install --no-cache-dir --index-url") < dockerfile.index(
        "pip install --no-cache-dir -r requirements.txt"
    )


def test_gpu_compose_overlay_enables_cuda_build_and_runtime_for_cga_engines() -> None:
    gpu_compose = _read("docker-compose.gpu.yml")

    assert "nlu-training-worker:" in gpu_compose
    assert 'CGA_GPU: "true"' in gpu_compose
    assert "CGA_TORCH_INDEX_URL: ${CGA_TORCH_INDEX_URL:?" in gpu_compose
    assert "AIDOT_ML_ACCELERATOR: auto" in gpu_compose
    assert 'AIDOT_GPU: "true"' in gpu_compose
    assert "AIDOT_TORCH_INDEX_URL: ${CGA_TORCH_INDEX_URL:?" in gpu_compose
    assert "AIDOT_EMBEDDING_DEVICE: auto" in gpu_compose
    assert gpu_compose.count("gpus: all") == 3


def test_default_compose_remains_cpu_compatible_without_gpu_device_requests() -> None:
    compose = _read("docker-compose.yml")

    assert "gpus: all" not in compose
    assert 'CGA_GPU: "true"' not in compose
    assert 'AIDOT_GPU: "true"' not in compose


def test_compose_runs_an_isolated_redis_cache_for_cga_api() -> None:
    compose = _read("docker-compose.yml")
    env_example = _read(".env.example")

    assert "container_name: cga-alone-redis" in compose
    assert "image: redis:7-alpine" in compose
    assert "CACHE_ENABLED: ${CACHE_ENABLED:-true}" in compose
    assert "REDIS_URL: ${REDIS_URL:-redis://cga-alone-redis:6379/0}" in compose
    assert "CACHE_DEFAULT_TTL_SECONDS: ${CACHE_DEFAULT_TTL_SECONDS:-60}" in compose
    assert "CACHE_VERSION_SECTION_TTL_SECONDS: ${CACHE_VERSION_SECTION_TTL_SECONDS:-60}" in compose
    assert "redis:\n        condition: service_healthy" in compose
    assert "redis-cli" in compose
    assert '"6379:6379"' not in compose

    for expected in (
        "CACHE_ENABLED=true",
        "REDIS_URL=redis://cga-alone-redis:6379/0",
        "CACHE_DEFAULT_TTL_SECONDS=60",
        "CACHE_VERSION_SECTION_TTL_SECONDS=60",
    ):
        assert expected in env_example


def test_docker_context_excludes_secrets_and_host_dependencies() -> None:
    dockerignore = _read(".dockerignore")
    for required_pattern in (".env", "**/node_modules", "**/.next", ".cga-data"):
        assert required_pattern in dockerignore


def test_web_container_preserves_shared_package_layout() -> None:
    dockerfile = _read("apps/web/Dockerfile")
    assert "WORKDIR /workspace/apps/web" in dockerfile
    assert "COPY packages/shared /workspace/packages/shared" in dockerfile


def test_api_uses_cga_service_branding() -> None:
    main_source = _read("apps/api/app/main.py")
    config_source = _read("apps/api/app/core/config.py")
    assert 'title="CGA API"' in main_source
    assert '"message": "CGA API is running."' in main_source
    assert 'app_name: str = "CGA API"' in config_source
    assert "/cga" in config_source


def test_api_container_runs_migrations_and_safe_bootstrap() -> None:
    dockerfile = _read("apps/api/Dockerfile")
    start_script = _read("apps/api/scripts/start-cga-api.sh")
    assert 'CMD ["/workspace/apps/api/scripts/start-cga-api.sh"]' in dockerfile
    assert "alembic upgrade head" in start_script
    assert "python -m app.db.bootstrap" in start_script
    assert "exec python -m uvicorn" in start_script


def test_alembic_escapes_percent_encoded_database_urls() -> None:
    env_source = _read("apps/api/alembic/env.py")
    assert 'settings.sqlalchemy_database_url.replace("%", "%%")' in env_source
