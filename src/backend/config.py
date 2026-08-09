
from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()

class Settings(BaseSettings):
    PROJECT_NAME: str = "DevGuard AI"
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str
    ALGORITHM: str = "HS256"

    CORS_ORIGINS: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    @field_validator("SECRET_KEY")
    @classmethod
    def _secret_key_not_placeholder(cls, v: str) -> str:
        if not v or v in {"SECRET_KEY_CHANGE_LATER", "change-me", "changeme"}:
            raise ValueError(
                "SECRET_KEY is required: generate one with `openssl rand -hex 32` "
                "and set it in your environment / .env"
            )
        return v
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    DATABASE_URL: str = "sqlite:///./devguard.db"

    # Gemini
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GEMINI_TEMPERATURE: float = 0.3
    GEMINI_MAX_TOKENS: int = 4096
    GEMINI_TIMEOUT: int = 60

    # AWS
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None
    AWS_DEFAULT_REGION: str = "us-east-1"

    # Redis role: app cache / progress pub-sub.
    REDIS_URL: str = "redis://localhost:6379/2"

    # Qdrant
    QDRANT_URL: str = "http://localhost:6333"


    model_config = SettingsConfigDict(env_file='.env', extra='allow', case_sensitive=True)

settings = Settings()
