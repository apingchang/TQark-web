"""
TQark-web 設定模組

從 .env 載入所有環境變數,用 pydantic-settings 驗證。
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # === Google OAuth ===
    google_client_id: str
    google_client_secret: str

    # === Admin ===
    admin_emails: str  # 逗號分隔

    # === JWT ===
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expire_days: int = 14

    # === File paths ===
    cookies_path: Path
    db_path: Path
    pdf_cache_dir: Path
    log_dir: Path

    # === Server ===
    port: int = 8000
    env: str = "development"

    # === Public URL ===
    public_base_url: str

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[2] / "credentials" / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def admin_email_list(self) -> list[str]:
        return [e.strip() for e in self.admin_emails.split(",") if e.strip()]

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"


settings = Settings()  # type: ignore[call-arg]