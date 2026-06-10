from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, ForeignKey, Boolean, LargeBinary
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
    pre_analise = "pre_analise"
    proposta_enviada = "proposta_enviada"
    proposta_aprovada = "proposta_aprovada"
    fechado = "fechado"
    perdido = "perdido"
    desqualificado = "desqualificado"
    parceiro = "parceiro"


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
    followup_tentativa = Column(Integer, default=0)   # 0=nenhum 1=1º enviado 2=2º enviado 3=3º enviado
    # ── Dados do contrato fechado ─────────────────────────────────────────
    fechado_em     = Column(DateTime, nullable=True)      # quando o lead virou Fechado (estável p/ relatórios)
    deal_data      = Column(String(10),  nullable=True)   # DD/MM/YYYY
    deal_veiculo   = Column(String(200), nullable=True)
    deal_placa     = Column(String(10),  nullable=True)   # placa do veículo
    deal_retorno   = Column(String(5),   nullable=True)   # número do retorno (admin only)
    deal_valor     = Column(String(20),  nullable=True)   # valor financiado (admin only)
    deal_comissao  = Column(String(20),  nullable=True)   # comissão recebida (admin only)
    deal_banco     = Column(String(30),  nullable=True)
    deal_conta_pg  = Column(String(50),  nullable=True)
    deal_operadora = Column(String(150), nullable=True)   # nome da operadora responsável
    dados_contrato = Column(Text, nullable=True)           # JSON com dados extras p/ requerimento
    cidade     = Column(String(100), nullable=True)        # coletado pelo bot
    renda      = Column(String(30),  nullable=True)        # faixa de renda (preenchido pela atendente)
    profissao  = Column(String(100), nullable=True)        # profissão (preenchido pela atendente)
    email      = Column(String(150), nullable=True)
    tem_cnh    = Column(Boolean, nullable=True)            # True=tem CNH | False=não tem | None=não informado
    oculto_funil = Column(Boolean, default=False)   # True = oculto do kanban por inatividade
    descadastrado = Column(Boolean, default=False)  # True = pediu p/ não receber mensagens (opt-out)
    ignorar_relatorios = Column(Boolean, default=False)  # True = conversa interna/teste, fora dos relatórios
    carros_proposta = Column(Text, nullable=True)   # JSON: lista de carros em proposta [{placa,modelo,ano,chassi,escolhido}]
    lido_em = Column(DateTime, nullable=True)        # quando alguém abriu a conversa (leitura compartilhada)
    observacoes = Column(Text, nullable=True)
    origem = Column(String(50), nullable=True)        # rede_social | parceiro | ex_cliente | indicacao | whatsapp
    origem_detalhe = Column(String(100), nullable=True) # google | instagram | nome livre (rede_social); ou nome do parceiro
    parceiro_id = Column(Integer, ForeignKey("parceiros.id"), nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    responsavel = relationship("Usuario", back_populates="leads_assumidos")
    parceiro    = relationship("Parceiro", back_populates="leads")


class Parceiro(Base):
    """Parceiro de negócios (imobiliária, correspondente, etc.)."""
    __tablename__ = "parceiros"

    id              = Column(Integer, primary_key=True, index=True)
    nome            = Column(String(200), nullable=False)
    data_nascimento = Column(String(10), nullable=True)
    cpf             = Column(String(14), nullable=True, unique=True, index=True)
    telefone          = Column(String(20), nullable=False)
    telefones_extras  = Column(Text, nullable=True)   # JSON array de strings
    email             = Column(String(150), nullable=True)
    observacoes       = Column(Text, nullable=True)
    nome_agenda       = Column(String(200), nullable=True)                          # como está salvo na agenda
    operadora_id      = Column(Integer, ForeignKey("usuarios.id"), nullable=True)  # operadora responsável
    ativo             = Column(Boolean, default=True)
    criado_em       = Column(DateTime, default=datetime.utcnow)

    contatos  = relationship("ContatoParceiro", back_populates="parceiro",
                             cascade="all, delete-orphan", order_by="ContatoParceiro.id")
    leads     = relationship("Lead", back_populates="parceiro")
    operadora = relationship("Usuario", foreign_keys=[operadora_id])


class ContatoParceiro(Base):
    """Contatos adicionais de um parceiro."""
    __tablename__ = "contatos_parceiro"

    id          = Column(Integer, primary_key=True, index=True)
    parceiro_id = Column(Integer, ForeignKey("parceiros.id"), nullable=False)
    nome        = Column(String(200), nullable=False)
    telefone    = Column(String(20), nullable=True)
    email       = Column(String(150), nullable=True)
    cargo       = Column(String(100), nullable=True)

    parceiro = relationship("Parceiro", back_populates="contatos")


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
    hash_doc        = Column(String(64), nullable=False)
    pdf_original    = Column(String(300), nullable=True)
    pdf_assinado    = Column(String(300), nullable=True)
    dados_contrato  = Column(Text, nullable=True)
    criado_em       = Column(DateTime, default=datetime.utcnow)

    # ── Requerente (cliente) ──────────────────────────────────────────────
    token           = Column(String(64), unique=True, nullable=False)
    status          = Column(String(20), default="pendente")   # pendente | assinado
    selfie_path         = Column(String(300), nullable=True)
    assinatura_path     = Column(String(300), nullable=True)
    doc_frente_req_path = Column(String(300), nullable=True)
    doc_verso_req_path  = Column(String(300), nullable=True)
    ip_cliente      = Column(String(50), nullable=True)
    geolocalizacao  = Column(String(200), nullable=True)
    assinado_em     = Column(DateTime, nullable=True)

    # ── Proprietário / Vendedor ───────────────────────────────────────────
    token_prop           = Column(String(64), unique=True, nullable=True)
    status_prop          = Column(String(20), default="pendente")
    selfie_prop_path     = Column(String(300), nullable=True)
    assinatura_prop_path = Column(String(300), nullable=True)
    doc_frente_prop_path = Column(String(300), nullable=True)
    doc_verso_prop_path  = Column(String(300), nullable=True)
    ip_prop              = Column(String(50), nullable=True)
    geo_prop             = Column(String(200), nullable=True)
    assinado_prop_em     = Column(DateTime, nullable=True)

    # ── OTP de confirmação ────────────────────────────────────────────────────
    codigo_req         = Column(String(10), nullable=True)
    codigo_req_expira  = Column(DateTime, nullable=True)
    codigo_prop        = Column(String(10), nullable=True)
    codigo_prop_expira = Column(DateTime, nullable=True)

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


class SessaoUsuario(Base):
    """Registro de cada sessão de login para relatório de acesso e atividade."""
    __tablename__ = "sessoes_usuario"

    id              = Column(Integer, primary_key=True, index=True)
    usuario_id      = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    ip              = Column(String(50), nullable=True)
    localizacao     = Column(String(200), nullable=True)   # "Cidade, Estado, País"
    login_em        = Column(DateTime, default=datetime.utcnow)
    ultimo_ativo_em = Column(DateTime, default=datetime.utcnow)
    logout_em       = Column(DateTime, nullable=True)
    tempo_ativo_s   = Column(Integer, default=0)           # segundos realmente ativos

    usuario = relationship("Usuario")


class AusenciaFuncionaria(Base):
    """Folgas, férias e afastamentos programados — apenas controle visual."""
    __tablename__ = "ausencias_funcionaria"

    id          = Column(Integer, primary_key=True, index=True)
    usuario_id  = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    tipo        = Column(String(20), nullable=False)   # folga | ferias | afastamento
    data_inicio = Column(String(10), nullable=False)   # YYYY-MM-DD
    data_fim    = Column(String(10), nullable=False)   # YYYY-MM-DD
    observacao  = Column(String(300), nullable=True)
    criado_em   = Column(DateTime, default=datetime.utcnow)

    usuario = relationship("Usuario")


class RegistroPonto(Base):
    """Marcação de ponto da funcionária (entrada, almoço, volta, saída)."""
    __tablename__ = "registros_ponto"

    id          = Column(Integer, primary_key=True, index=True)
    usuario_id  = Column(Integer, ForeignKey("usuarios.id"), nullable=False, index=True)
    tipo        = Column(String(20), nullable=False)   # entrada | saida_almoco | volta_almoco | saida
    timestamp   = Column(DateTime, default=datetime.utcnow, index=True)
    ip          = Column(String(50), nullable=True)
    foto_filename = Column(String(64), nullable=True)  # selfie tirada na hora de bater o ponto

    usuario = relationship("Usuario")


class JustificativaPonto(Base):
    """Justificativa de horário da funcionária (ex.: foi ao médico) com atestado anexado.
    Fica como 'pendente' até o admin aprovar ou rejeitar."""
    __tablename__ = "justificativas_ponto"

    id            = Column(Integer, primary_key=True, index=True)
    usuario_id    = Column(Integer, ForeignKey("usuarios.id"), nullable=False, index=True)
    data          = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD do dia justificado
    texto         = Column(Text, nullable=True)                     # motivo
    filename      = Column(String(64), nullable=True)               # atestado anexado (no banco)
    nome_arquivo  = Column(String(200), nullable=True)              # nome original do atestado
    status        = Column(String(20), default="pendente")         # pendente | aprovada | rejeitada
    obs_admin     = Column(String(400), nullable=True)             # observação/motivo da rejeição
    aprovado_por  = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    aprovado_em   = Column(DateTime, nullable=True)
    criado_em     = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    usuario   = relationship("Usuario", foreign_keys=[usuario_id])
    aprovador = relationship("Usuario", foreign_keys=[aprovado_por])


class CorrecaoPonto(Base):
    """Correção de ponto — solicitação E trilha de auditoria (conformidade trabalhista).
    A funcionária SOLICITA (status 'pendente'); o admin APLICA (status 'aplicada') ou REJEITA.
    Toda correção feita direto pelo admin também é gravada aqui (origem 'admin', já 'aplicada').
    Nada é editado de forma silenciosa: o antes/depois fica sempre registrado."""
    __tablename__ = "correcoes_ponto"

    id            = Column(Integer, primary_key=True, index=True)
    usuario_id    = Column(Integer, ForeignKey("usuarios.id"), nullable=False, index=True)  # dono do ponto
    solicitante_id= Column(Integer, ForeignKey("usuarios.id"), nullable=True)               # quem pediu/fez
    data          = Column(String(10), nullable=False, index=True)   # YYYY-MM-DD do ponto
    acao          = Column(String(20), nullable=False)               # adicionar | editar | remover
    tipo_ponto    = Column(String(20), nullable=True)                # entrada | saida_almoco | volta_almoco | saida
    hora_anterior = Column(String(5), nullable=True)                 # HH:MM antes da mudança
    hora_nova     = Column(String(5), nullable=True)                 # HH:MM depois da mudança
    registro_id   = Column(Integer, nullable=True)                   # id do RegistroPonto afetado
    motivo        = Column(Text, nullable=True)                      # justificativa da correção
    status        = Column(String(20), default="pendente")           # pendente | aplicada | rejeitada
    origem        = Column(String(20), default="funcionaria")        # funcionaria | admin
    obs_admin     = Column(String(400), nullable=True)               # observação/motivo da rejeição
    resolvido_por = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    resolvido_em  = Column(DateTime, nullable=True)
    criado_em     = Column(DateTime, default=datetime.utcnow)

    usuario     = relationship("Usuario", foreign_keys=[usuario_id])
    solicitante = relationship("Usuario", foreign_keys=[solicitante_id])
    resolvedor  = relationship("Usuario", foreign_keys=[resolvido_por])


class AtividadePing(Base):
    """Registro com horário de cada minuto realmente ativo no painel (admin only).
    Permite calcular o tempo ativo dentro de cada janela de ponto."""
    __tablename__ = "atividade_pings"

    id         = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False, index=True)
    timestamp  = Column(DateTime, default=datetime.utcnow, index=True)


class Agendamento(Base):
    """Tarefa/agendamento de uma ação a fazer com o cliente, dentro de um lead."""
    __tablename__ = "agendamentos"

    id          = Column(Integer, primary_key=True, index=True)
    lead_id     = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)
    criado_por  = Column(Integer, ForeignKey("usuarios.id"), nullable=False, index=True)
    titulo      = Column(String(200), nullable=False)        # ação a fazer
    descricao   = Column(Text, nullable=True)                # detalhes opcionais
    quando      = Column(DateTime, nullable=False, index=True)  # data/hora (UTC naive)
    concluido   = Column(Boolean, default=False, index=True)
    concluido_em = Column(DateTime, nullable=True)
    resultado   = Column(Text, nullable=True)                # o que foi feito/aconteceu ao concluir
    criado_em   = Column(DateTime, default=datetime.utcnow)

    lead   = relationship("Lead")
    criador = relationship("Usuario")


class MidiaArquivo(Base):
    """Arquivos de mídia (imagem, documento, áudio) recebidos/enviados no WhatsApp,
    guardados no próprio banco para não se perderem quando o disco do container é reciclado."""
    __tablename__ = "midia_arquivos"

    id            = Column(Integer, primary_key=True, index=True)
    filename      = Column(String(64), unique=True, index=True, nullable=False)  # uuidhex.ext
    tipo          = Column(String(20), nullable=False)   # imagem | documento | audio
    nome_original = Column(String(200), nullable=True)
    mime          = Column(String(120), nullable=True)
    dados         = Column(LargeBinary, nullable=False)
    tamanho       = Column(Integer, default=0)
    criado_em     = Column(DateTime, default=datetime.utcnow)


class DocumentoCliente(Base):
    """Documento anexado a um contrato fechado (pasta do cliente).
    Os BYTES ficam em MidiaArquivo (banco = durável); aqui guardamos a referência e metadados."""
    __tablename__ = "documentos_cliente"

    id          = Column(Integer, primary_key=True, index=True)
    lead_id     = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)
    nome        = Column(String(250), nullable=False)        # nome original do arquivo
    filename    = Column(String(64), nullable=False)         # uuidhex.ext (chave em MidiaArquivo)
    mime        = Column(String(120), nullable=True)
    tamanho     = Column(Integer, default=0)
    enviado_por = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    criado_em   = Column(DateTime, default=datetime.utcnow)

    lead    = relationship("Lead")
    usuario = relationship("Usuario")


def criar_tabelas():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
