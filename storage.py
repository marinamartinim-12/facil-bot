"""
Armazenamento de mídia em object storage (Cloudflare R2 — S3-compatível).

Feature-flag: só entra em ação se as 4 variáveis R2_* estiverem configuradas.
Sem elas, o app continua guardando/servindo do banco (comportamento antigo) —
então este arquivo pode subir "dormente" sem mudar nada em produção.

Convenção: a CHAVE do objeto no R2 é o próprio `filename` (uuidhex.ext), o
mesmo já usado em MidiaArquivo — então não precisa de coluna nova.
"""
import threading
from config import get_settings

_settings = get_settings()
_lock = threading.Lock()
_cliente = None


def r2_ativo() -> bool:
    s = _settings
    return bool(s.R2_ACCOUNT_ID and s.R2_ACCESS_KEY_ID and s.R2_SECRET_ACCESS_KEY and s.R2_BUCKET)


def _client():
    global _cliente
    if _cliente is None:
        with _lock:
            if _cliente is None:
                import boto3
                from botocore.config import Config
                s = _settings
                _cliente = boto3.client(
                    "s3",
                    endpoint_url=f"https://{s.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
                    aws_access_key_id=s.R2_ACCESS_KEY_ID,
                    aws_secret_access_key=s.R2_SECRET_ACCESS_KEY,
                    region_name="auto",
                    config=Config(signature_version="s3v4",
                                  retries={"max_attempts": 3, "mode": "standard"}),
                )
    return _cliente


def subir(key: str, dados: bytes, mime: str = None) -> bool:
    """Sobe os bytes para o R2. True se ok. (chamada de rede — bloqueante)"""
    try:
        extra = {"ContentType": mime} if mime else {}
        _client().put_object(Bucket=_settings.R2_BUCKET, Key=key, Body=dados, **extra)
        return True
    except Exception as e:
        print(f"⚠️ R2 subir({key}) falhou: {e}")
        return False


def baixar(key: str):
    """Baixa os bytes do R2 (ou None se falhar). (chamada de rede — bloqueante)"""
    try:
        r = _client().get_object(Bucket=_settings.R2_BUCKET, Key=key)
        return r["Body"].read()
    except Exception as e:
        print(f"⚠️ R2 baixar({key}) falhou: {e}")
        return None


def url_assinada(key: str, mime: str = None, nome_download: str = None, expira: int = 900) -> str:
    """Gera uma URL temporária (presigned) de leitura direta do R2.
    Assinatura é LOCAL (não faz chamada de rede) — barato, não bloqueia o loop."""
    try:
        params = {"Bucket": _settings.R2_BUCKET, "Key": key}
        if mime:
            params["ResponseContentType"] = mime
        if nome_download:
            from urllib.parse import quote
            params["ResponseContentDisposition"] = (
                f"attachment; filename=\"{key}\"; filename*=UTF-8''{quote(nome_download)}"
            )
        return _client().generate_presigned_url("get_object", Params=params, ExpiresIn=expira)
    except Exception as e:
        print(f"⚠️ R2 url_assinada({key}) falhou: {e}")
        return None
