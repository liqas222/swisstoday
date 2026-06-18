import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    anthropic_api_key: str
    x_api_key: str
    x_api_secret: str
    x_access_token: str
    x_access_token_secret: str
    x_bearer_token: str
    dry_run: bool
    check_interval_minutes: int
    claude_model: str
    db_path: str
    log_level: str


def load_config() -> Config:
    return Config(
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        x_api_key=os.getenv("X_API_KEY", ""),
        x_api_secret=os.getenv("X_API_SECRET", ""),
        x_access_token=os.getenv("X_ACCESS_TOKEN", ""),
        x_access_token_secret=os.getenv("X_ACCESS_TOKEN_SECRET", ""),
        x_bearer_token=os.getenv("X_BEARER_TOKEN", ""),
        dry_run=os.getenv("DRY_RUN", "true").lower() == "true",
        check_interval_minutes=int(os.getenv("CHECK_INTERVAL_MINUTES", "15")),
        claude_model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        db_path=os.getenv("DB_PATH", "swissintel.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
