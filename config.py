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

    # Cloudflare R2 (armazenamento de mídia fora do banco) — vazio = usa o banco (comportamento antigo)
    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET: str = ""

    # Segurança — troque em produção
    SECRET_KEY: str = "facil-financiamentos-chave-secreta-mude-em-producao-2026"
    # Segredo do webhook (Z-API/Meta) — VAZIO = não valida (transição). Setado = exige ?s=<segredo> na URL.
    WEBHOOK_SECRET: str = ""

    # Admin padrão criado automaticamente no primeiro uso
    ADMIN_NOME: str = "Administrador"
    ADMIN_EMAIL: str = "admin@facilfinancamentos.com.br"
    ADMIN_PASSWORD: str = "Admin@123"

    class Config:
        env_file = ".env"


# Chaves fracas/padrão que NUNCA devem assinar tokens em produção
_SECRET_KEYS_FRACAS = {
    "facil-financiamentos-chave-secreta-mude-em-producao-2026",
    "facil-financiamentos-chave-secreta-2026-mude-em-producao",
    "", "changeme", "secret", "mude-em-producao", "sua-chave-secreta",
}


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # Fail-closed: a SECRET_KEY assina o "crachá" de login (JWT). Se estiver ausente ou for
    # uma chave fraca/padrão conhecida, o app RECUSA subir — evita token de admin forjável.
    _sk = (s.SECRET_KEY or "").strip()
    if _sk in _SECRET_KEYS_FRACAS or len(_sk) < 16:
        raise RuntimeError(
            "SECRET_KEY ausente ou fraca — configure uma chave forte e aleatória na "
            "variável de ambiente SECRET_KEY (Render → Environment)."
        )
    return s
