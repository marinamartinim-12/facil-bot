from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    ANTHROPIC_API_KEY: str
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_VERIFY_TOKEN: str = "facil_financiamentos_bot"
    WHATSAPP_PHONE_ID: str = ""
    DATABASE_URL: str = "sqlite:///./facil_leads.db"
    ZAPI_INSTANCE: str = ""
    ZAPI_TOKEN: str = ""
    ZAPI_CLIENT_TOKEN: str = ""

    # Segurança — troque em produção
    SECRET_KEY: str = "facil-financiamentos-chave-secreta-mude-em-producao-2026"

    # Admin padrão criado automaticamente no primeiro uso
    ADMIN_NOME: str = "Administrador"
    ADMIN_EMAIL: str = "admin@facilfinancamentos.com.br"
    ADMIN_PASSWORD: str = "Admin@123"

    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()
