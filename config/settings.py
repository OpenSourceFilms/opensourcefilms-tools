from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="/workspace/pipeline/.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    postgres_url: str = "postgresql+asyncpg://media:media@localhost:5432/media_evidence"

    # Queue
    redis_url: str = "redis://localhost:6379/0"

    # Object store
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_use_ssl: bool = False

    # Model paths
    transnetv2_weights: str = "/workspace/weights/transnetv2"
    parakeet_model: str = "nvidia/parakeet-tdt-1.1b"
    paddleocr_lang: str = "en"
    qwen3_vl_model: str = "/workspace/weights/qwen3/Qwen3-VL-Embedding-2B"
    qwen3_text_model: str = "/workspace/weights/qwen3/Qwen3-Embedding-8B"
    whisper_fallback_model: str = "large-v3"
    whisper_fallback_dir: str = "/workspace/pipeline/models/whispercpp"

    # HuggingFace
    hf_token: str = ""

    # Ingest tuning
    keyframes_per_shot: int = 3
    ocr_confidence_threshold: float = 0.7
    asr_language_sample_s: int = 30
    diarize: bool = True
    min_speaker_pause_s: float = 1.5

    # GPU flags
    require_gpu_for_asr: bool = True
    require_gpu_for_embed: bool = True
    runpod_api_key: str = ""
    runpod_gpu_pod_template_id: str = ""

    # Qdrant
    qdrant_url: str = "http://localhost:6333"

    # Embedding
    embed_batch_size: int = 8

    # Embedding dims (verified against actual model output)
    qwen3_vl_dim: int = 2048   # Qwen3-VL-Embedding-2B (fallback: siglip=1152)
    qwen3_text_dim: int = 4096  # Qwen3-Embedding-8B (fallback: bge-m3=1024)

    # Service ports
    api_port: int = 8000
    api_host: str = "0.0.0.0"

    # Logging
    log_level: str = "INFO"
    log_dir: str = "/workspace/logs"


settings = Settings()
