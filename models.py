from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, ForeignKey, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import enum
import os

from config import get_settings

settings = get_settings()

def _resolver_database_url() -> str:
    url = settings.DATABASE_URL
    if "sqlite" in url:
        # Garante que o diretório existe
        caminho = url.replace("sqlite:///", "").lstrip("/")
        if url.startswith("sqlite:////"):
            caminho = "/" + url[len("sqlite:////"):]
        diretorio = os.path.dirname(caminho)
        if diretorio:
            try:
                os.makedirs(diretorio, exist_ok=True)
            except Exception:
                # Fallback: usa /app se não conseguir criar o diretório
                print(f"⚠️ Não foi possível usar {url} — fallback para /app/facil_leads.db")
                return "sqlite:////app/facil_leads.db"
    return url

_DATABASE_URL = _resolver_database_url()
print(f"🗄️  DATABASE_URL resolvida: {_DATABASE_URL}")

engine = create_engine(
    _DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in _DATABASE_URL else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class RoleEnum(str, enum.Enum):
    admin = "admin"
    funcionario = "funcionario"


class ModalidadeEnum(str, enum.Enum):
    financiamento = "financiamento"
    refinanciamento = "refinanciamento"
    indefinido = "indefinido"


class StatusLeadEnum(str, enum.Enum):
    em_atendimento = "em_atendimento"
    qualificado = "qualificado"
    assumido = "assumido"
    proposta_enviada = "proposta_enviada"
    fechado = "fechado"
    perdido = "perdido"
    desqualificado = "desqualificado"


class EstadoConversaEnum(str, enum.Enum):
    inicio = "inicio"
    aguardando_nome = "aguardando_nome"
    aguardando_modalidade = "aguardando_modalidade"
    coletando_cidade = "coletando_cidade"
    coletando_cpf = "coletando_cpf"
    coletando_data_nasc = "coletando_data_nasc"
    coletando_carro = "coletando_carro"
    finalizado = "finalizado"
    transferido = "transferido"
    desqualificado = "desqualificado"


class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(150), nullable=False)
    email = Column(String(150), unique=True, index=True, nullable=False)
    senha_hash = Column(String(200), nullable=False)
    role = Column(String(20), default=RoleEnum.funcionario)
    ativo = Column(Boolean, default=True)
    criado_em = Column(DateTime, default=datetime.utcnow)

    leads_assumidos = relationship("Lead", back_populates="responsavel")


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    telefone = Column(String(20), unique=True, index=True, nullable=False)
    nome = Column(String(150), nullable=True)
    cpf = Column(String(14), nullable=True)
    data_nascimento = Column(String(10), nullable=True)
    carro_interesse = Column(String(200), nullable=True)
    modalidade = Column(String(20), default=ModalidadeEnum.indefinido)
    status = Column(String(30), default=StatusLeadEnum.em_atendimento)
    estado_conversa = Column(String(40), default=EstadoConversaEnum.inicio)
    atribuido_para = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    assumido_em = Column(DateTime, nullable=True)
    followup_em = Column(DateTime, nullable=True)
    observacoes = Column(Text, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    responsavel = relationship("Usuario", back_populates="leads_assumidos")


class MensagemConversa(Base):
    __tablename__ = "mensagens"

    id = Column(Integer, primary_key=True, index=True)
    telefone = Column(String(20), index=True, nullable=False)
    role = Column(String(10), nullable=False)
    conteudo = Column(Text, nullable=False)
    criado_em = Column(DateTime, default=datetime.utcnow)


class Contrato(Base):
    """Contrato gerado para assinatura digital do lead."""
    __tablename__ = "contratos"

    id              = Column(Integer, primary_key=True, index=True)
    lead_id         = Column(Integer, ForeignKey("leads.id"), nullable=False)
    criado_por_id   = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    token           = Column(String(64), unique=True, nullable=False)
    status          = Column(String(20), default="pendente")   # pendente | assinado | expirado
    hash_doc        = Column(String(64), nullable=False)
    pdf_original    = Column(String(300), nullable=True)
    pdf_assinado    = Column(String(300), nullable=True)
    selfie_path     = Column(String(300), nullable=True)
    assinatura_path = Column(String(300), nullable=True)
    ip_cliente      = Column(String(50), nullable=True)
    geolocalizacao  = Column(String(200), nullable=True)
    criado_em       = Column(DateTime, default=datetime.utcnow)
    assinado_em     = Column(DateTime, nullable=True)

    lead    = relationship("Lead")
    criador = relationship("Usuario")


class Configuracao(Base):
    """Configurações editáveis do bot pelo painel admin."""
    __tablename__ = "configuracoes"

    id = Column(Integer, primary_key=True)
    chave = Column(String(100), unique=True, nullable=False)
    valor = Column(Text, nullable=False)
    descricao = Column(String(300), nullable=True)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def criar_tabelas():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
