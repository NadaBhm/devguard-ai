from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

load_dotenv() 

class Settings(BaseSettings):
    PROJECT_NAME: str = "DevGuard AI"
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str = "your-secret-key-change-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    DATABASE_URL: str = "sqlite:///./devguard.db"
    
    # Gemini
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-3.5-flash"
    GEMINI_TEMPERATURE: float = 0.3
    GEMINI_MAX_TOKENS: int = 4096
    GEMINI_TIMEOUT: int = 60
    
    # AWS
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None
    AWS_DEFAULT_REGION: str = "us-east-1"
    
    model_config = SettingsConfigDict(env_file='.env', extra='allow', case_sensitive=True)

settings = Settings()