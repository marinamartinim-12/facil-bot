from datetime import datetime, timedelta
from typing import Optional
import bcrypt
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, Cookie, Request
from sqlalchemy.orm import Session
from models import get_db, Usuario, RoleEnum
from config import get_settings

settings = get_settings()

ALGORITHM = "HS256"
TOKEN_EXPIRE_HORAS = 24 * 7  # 7 dias


def verificar_senha(senha_plana: str, senha_hash: str) -> bool:
    return bcrypt.checkpw(senha_plana.encode("utf-8"), senha_hash.encode("utf-8"))


def hash_senha(senha: str) -> str:
    return bcrypt.hashpw(senha.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def criar_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(hours=TOKEN_EXPIRE_HORAS))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)


def obter_usuario_atual(
    access_token: Optional[str] = Cookie(None),
    db: Session = Depends(get_db),
) -> Usuario:
    if not access_token:
        raise HTTPException(status_code=401, detail="Não autenticado")
    try:
        payload = jwt.decode(access_token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        usuario_id = payload.get("sub")
        if not usuario_id:
            raise HTTPException(status_code=401, detail="Token inválido")
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido")

    usuario = db.query(Usuario).filter(Usuario.id == int(usuario_id)).first()
    if not usuario or not usuario.ativo:
        raise HTTPException(status_code=401, detail="Usuário não encontrado ou inativo")
    return usuario


def requer_admin(request: Request, usuario: Usuario = Depends(obter_usuario_atual)) -> Usuario:
    if usuario.role == RoleEnum.admin:
        return usuario
    # O Dono pode LER (GET) as telas de gestão. Escrita e telas perigosas (/api/admin/*)
    # são bloqueadas pelo guarda global no main.py — aqui é só a leitura.
    if usuario.role == RoleEnum.dono and request.method in ("GET", "HEAD"):
        return usuario
    raise HTTPException(status_code=403, detail="Acesso restrito ao administrador")


def requer_gestao(usuario: Usuario = Depends(obter_usuario_atual)) -> Usuario:
    """Admin OU Dono (perfil de visualização). Para telas de gestão só-leitura.
    O Dono chega aqui apenas em GET — escrita é bloqueada pelo guarda global."""
    if usuario.role not in (RoleEnum.admin, RoleEnum.dono):
        raise HTTPException(status_code=403, detail="Acesso restrito à gestão")
    return usuario


def role_do_token(access_token: Optional[str]) -> Optional[str]:
    """Lê o perfil (role) direto do JWT, sem tocar no banco — usado pelo guarda de escrita."""
    if not access_token:
        return None
    try:
        payload = jwt.decode(access_token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("role")
    except JWTError:
        return None
