"""
Fácil Financiamentos — Motor de conversa IA
"""

import re
import json
import anthropic
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy.orm import Session

from config import get_settings
from models import (
    Lead, MensagemConversa, Configuracao,
    EstadoConversaEnum, ModalidadeEnum, StatusLeadEnum,
)

settings = get_settings()
client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
Você é Maria, atendente virtual da Fácil Financiamentos (Belo Horizonte, MG, 23 anos no mercado).

══════════════════════════════════════════════════
REGRA ABSOLUTA DE FORMATO
══════════════════════════════════════════════════
Responda SOMENTE com JSON válido. Zero texto fora do JSON.

{
  "mensagens": ["texto da mensagem 1", "texto da mensagem 2"],
  "proximo_estado": "nome_do_estado",
  "dados_coletados": {
    "nome": null,
    "cidade": null,
    "cpf": null,
    "data_nascimento": null,
    "carro_interesse": null,
    "modalidade": null
  },
  "qualificado": true
}

- "mensagens" é sempre um array, mesmo com 1 item: ["texto"].
- Preencha dados_coletados apenas com o que o cliente enviou NESTA mensagem.
- qualificado: false somente se desqualificado.

══════════════════════════════════════════════════
REGRAS INVIOLÁVEIS
══════════════════════════════════════════════════
1. NUNCA invente taxas, valores, prazos ou documentos. Se o cliente perguntar qualquer dado numérico → TRANSFIRA.
2. Se o cliente pedir para falar com humano/atendente/consultor → TRANSFIRA imediatamente.
3. Para transferir:
   mensagens: ["Claro! Vou te conectar agora com uma de nossas consultoras. Em breve ela entrará em contato! 😊"]
   proximo_estado: "transferido"

══════════════════════════════════════════════════
ROTEIRO (execute conforme o ESTADO ATUAL informado abaixo)
══════════════════════════════════════════════════

ESTADO "inicio"  →  proximo_estado: "aguardando_nome"
Envie exatamente estas 3 mensagens:
  "Olá ! Seja bem-vindo à Fácil Financiamentos, eleita a melhor plataforma de financiamentos de MG, há 23 anos no mercado."
  "Meu nome é Maria, sou assistente virtual da Fácil Financiamentos. 🧕"
  "Estamos em Belo Horizonte, MG. Qual o seu nome ?"

ESTADO "aguardando_nome"  →  proximo_estado: "coletando_cidade"
Salve o nome. Envie:
  "Estamos em Belo Horizonte, MG, de que cidade você é ?"

ESTADO "coletando_cidade"  →  proximo_estado: "aguardando_modalidade"
Salve a cidade. Envie exatamente estas 2 mensagens:
  "Você esta procurando um financiamento, ou empréstimo com garantia do seu veículo ?"
  "Somos especialistas em financiamento de particular para particular, credenciados nas 9 melhores financeiras do Brasil ! Encontraremos as melhores taxas e condições para você"

ESTADO "aguardando_modalidade"  →  proximo_estado: "coletando_cpf"
Identifique a escolha:
  • "2" / "empréstimo" / "garantia" / "refinanciamento" / "já tenho" = EMPRÉSTIMO COM GARANTIA
  • "1" / "financiamento" / "comprar" = FINANCIAMENTO

Se EMPRÉSTIMO COM GARANTIA → modalidade: "refinanciamento"
Envie exatamente estas 2 mensagens:
  "O empréstimo com garantia de veículo funciona assim: você usa seu carro como garantia e, por isso, as taxas ficam bem mais baixas do que as de empréstimo pessoal ou cartão.\nOutro ponto importante: você continua usando seu carro normalmente, sem nenhuma mudança na rotina.\nQuer que eu veja o valor que você consegue liberar hoje?"
  "Com apenas 3 dados, faremos uma pré análise, e encontraremos as melhores taxas e condições para você. 🚗🛵🚛\n\nDigite seu CPF:"

Se FINANCIAMENTO → modalidade: "financiamento"
  - Cidade próxima de BH (até ~200 km: Grande BH, Contagem, Betim, Sete Lagoas, Ipatinga, Juiz de Fora, Divinópolis, Itabira, Conselheiro Lafaiete, Ouro Preto, Barbacena, Viçosa, Muriaé, Uberlândia, Uberaba, Gov. Valadares, Montes Claros, Pouso Alegre, Varginha, Lavras, Poços de Caldas):
    Envie: "Ótimo! Para o financiamento, o fechamento do contrato é feito presencialmente aqui em Belo Horizonte (exigência do banco). 🏢\n\nCom apenas 3 dados, faremos uma pré análise e encontraremos as melhores taxas e condições para você. 🚗🛵🚛\n\nDigite seu CPF:"
  - Cidade longe de BH:
    Envie: "Para o financiamento, o fechamento é presencial em BH (exigência do banco). Você teria disponibilidade de vir até nós?"
    proximo_estado: "aguardando_modalidade" (aguarda resposta)
    Se confirmar que pode vir → vá para CPF.
    Se não puder → ofereça empréstimo com garantia (100% online). Se aceitar → trate como EMPRÉSTIMO COM GARANTIA. Se recusar → desqualifique (qualificado: false, proximo_estado: "desqualificado").

ESTADO "coletando_cpf"  →  proximo_estado: "coletando_data_nasc"
Salve o CPF. Envie:
  "Obrigado! Agora digite sua data de nascimento:"

ESTADO "coletando_data_nasc"  →  proximo_estado: "coletando_carro"
Salve a data. Envie:
  "Ótimo! Por último, qual o ano e modelo do veículo?"

ESTADO "coletando_carro"  →  proximo_estado: "finalizado"
Salve o veículo. Envie:
  "Obrigado pelas confirmações, em breve uma de nossas consultoras entrará em contato. 🤝"

══════════════════════════════════════════════════
Estados válidos para proximo_estado:
aguardando_nome | coletando_cidade | aguardando_modalidade |
coletando_cpf | coletando_data_nasc | coletando_carro |
finalizado | transferido | desqualificado
══════════════════════════════════════════════════
"""


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

# Ordem dos estados — protege contra regressão
_ORDEM_ESTADOS = [
    EstadoConversaEnum.inicio,
    EstadoConversaEnum.aguardando_nome,
    EstadoConversaEnum.coletando_cidade,
    EstadoConversaEnum.aguardando_modalidade,
    EstadoConversaEnum.coletando_cpf,
    EstadoConversaEnum.coletando_data_nasc,
    EstadoConversaEnum.coletando_carro,
    EstadoConversaEnum.finalizado,
]
_ESTADOS_TERMINAIS = {
    EstadoConversaEnum.transferido,
    EstadoConversaEnum.desqualificado,
    EstadoConversaEnum.finalizado,
}


def _validar_proximo_estado(estado_atual: str, proximo_estado: str) -> str:
    if proximo_estado in _ESTADOS_TERMINAIS:
        return proximo_estado
    try:
        idx_atual   = _ORDEM_ESTADOS.index(estado_atual)
        idx_proximo = _ORDEM_ESTADOS.index(proximo_estado)
    except ValueError:
        return estado_atual
    if idx_proximo < idx_atual:
        print(f"⚠️  Regressão bloqueada: {estado_atual} → {proximo_estado}")
        return estado_atual
    return proximo_estado


def _formatar_cpf(cpf: str) -> str:
    n = re.sub(r"\D", "", cpf)
    if len(n) == 11:
        return f"{n[:3]}.{n[3:6]}.{n[6:9]}-{n[9:]}"
    return cpf


def _salvar_mensagem(db: Session, telefone: str, role: str, conteudo: str):
    db.add(MensagemConversa(telefone=telefone, role=role, conteudo=conteudo))
    db.commit()


def _atualizar_lead(db: Session, lead: Lead, dados: dict, proximo_estado: str, qualificado: bool):
    if dados.get("nome"):
        lead.nome = dados["nome"]
    if dados.get("cpf"):
        lead.cpf = _formatar_cpf(dados["cpf"])
    if dados.get("data_nascimento"):
        lead.data_nascimento = dados["data_nascimento"]
    if dados.get("carro_interesse"):
        lead.carro_interesse = dados["carro_interesse"]
    if dados.get("modalidade"):
        mod = dados["modalidade"].lower()
        if "refin" in mod or "garantia" in mod:
            lead.modalidade = ModalidadeEnum.refinanciamento
        elif "financ" in mod or "comprar" in mod:
            lead.modalidade = ModalidadeEnum.financiamento

    proximo_estado = _validar_proximo_estado(lead.estado_conversa, proximo_estado)
    lead.estado_conversa = proximo_estado
    lead.atualizado_em   = datetime.utcnow()

    if not qualificado:
        lead.status = StatusLeadEnum.desqualificado
    elif proximo_estado in (EstadoConversaEnum.finalizado, EstadoConversaEnum.transferido):
        lead.status = StatusLeadEnum.qualificado

    db.commit()
    db.refresh(lead)


def _historico_limpo(historico: list) -> list[dict]:
    """
    Converte o histórico para o formato de messages do Claude.
    Mensagens do bot que sejam JSON são substituídas pelo texto real,
    evitando que o modelo se confunda com os JSONs anteriores.
    """
    msgs = []
    for m in historico[-20:]:
        conteudo = m.conteudo
        if m.role == "assistant":
            # Tenta extrair o texto real das mensagens do bot
            try:
                parsed = json.loads(conteudo)
                textos = parsed.get("mensagens") or [parsed.get("mensagem", conteudo)]
                conteudo = " | ".join(t for t in textos if t)
            except Exception:
                pass  # Não era JSON, usa texto puro
        msgs.append({"role": m.role, "content": conteudo})
    return msgs


def _proximo_horario_atendimento() -> str:
    agora    = datetime.now(ZoneInfo("America/Sao_Paulo"))
    dia      = agora.weekday()
    hora_dec = agora.hour + agora.minute / 60
    if dia < 5 and 9 <= hora_dec < 18:
        return ""
    if dia == 5 and 9 <= hora_dec < 13:
        return ""
    if dia < 5:
        if hora_dec < 9:
            return "hoje às 09h"
        nomes = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado"]
        return f"{nomes[min(dia+1,5)]} às 09h"
    if dia == 5:
        return "segunda-feira às 09h" if hora_dec >= 13 else "hoje (sábado) às 09h"
    return "segunda-feira às 09h"


# ─────────────────────────────────────────────────────────────────────────────
# FUNÇÃO PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

def processar_mensagem(telefone: str, mensagem_cliente: str, db: Session) -> list[str]:
    # Busca ou cria lead
    lead = db.query(Lead).filter(Lead.telefone == telefone).first()
    if not lead:
        lead = Lead(telefone=telefone)
        db.add(lead)
        db.commit()
        db.refresh(lead)

    # Conversa já encerrada
    if lead.estado_conversa in (EstadoConversaEnum.finalizado, EstadoConversaEnum.transferido):
        nome = f" {lead.nome}" if lead.nome else ""
        return [f"Olá{nome}! Em breve uma atendente entrará em contato. 😊"]

    # Histórico (antes de salvar a mensagem atual)
    historico = (
        db.query(MensagemConversa)
        .filter(MensagemConversa.telefone == telefone)
        .order_by(MensagemConversa.id)
        .all()
    )

    # Salva mensagem do cliente
    _salvar_mensagem(db, telefone, "user", mensagem_cliente)

    # Aviso de horário (só na finalização)
    aviso_horario = ""
    prox = _proximo_horario_atendimento()
    if prox:
        aviso_horario = (
            f"\n\nATENÇÃO: Estamos fora do horário agora. "
            f"Na mensagem de finalização, acrescente: "
            f"'No momento estamos fora do horário. Funcionamos seg-sex das 09h às 18h e sáb das 09h às 13h. "
            f"Entraremos em contato assim que possível! 🕘'"
        )

    # System com contexto do estado atual
    system = (
        f"{SYSTEM_PROMPT}"
        f"\n\n══════════════════════════════════════════════════"
        f"\nESTADO ATUAL: {lead.estado_conversa}"
        f"\nNome: {lead.nome or '(ainda não informado)'}"
        f"\nCidade: {getattr(lead, 'cidade', None) or '(ainda não informada)'}"
        f"\nModalidade: {lead.modalidade or '(ainda não definida)'}"
        f"\nCPF: {lead.cpf or '(ainda não informado)'}"
        f"\nData de nascimento: {lead.data_nascimento or '(ainda não informada)'}"
        f"\nVeículo: {lead.carro_interesse or '(ainda não informado)'}"
        f"\n══════════════════════════════════════════════════"
        f"\nExecute EXATAMENTE a etapa do estado '{lead.estado_conversa}'."
        f"{aviso_horario}"
    )

    messages = _historico_limpo(historico)
    messages.append({"role": "user", "content": mensagem_cliente})

    # Chama o Claude
    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=system,
            messages=messages,
        )
        resposta_raw = response.content[0].text.strip()
    except Exception as e:
        print(f"❌ Erro Claude: {e}")
        return ["Desculpe, ocorreu um problema técnico. Tente novamente em instantes. 🙏"]

    # Parse JSON
    try:
        match = re.search(r"\{[\s\S]*\}", resposta_raw)
        if not match:
            raise ValueError("JSON não encontrado")
        dados = json.loads(match.group())

        mensagens_bot = dados.get("mensagens")
        if not isinstance(mensagens_bot, list) or not mensagens_bot:
            # fallback para chave "mensagem" (singular)
            mensagens_bot = [dados.get("mensagem", resposta_raw)]
        mensagens_bot = [m for m in mensagens_bot if m]

        proximo_estado  = dados.get("proximo_estado", lead.estado_conversa)
        dados_coletados = {k: v for k, v in (dados.get("dados_coletados") or {}).items() if v}
        qualificado     = dados.get("qualificado", True)

        _atualizar_lead(db, lead, dados_coletados, proximo_estado, qualificado)

    except Exception as ex:
        print(f"⚠️  Falha no parse JSON: {ex}\nResposta raw: {resposta_raw}")
        mensagens_bot = [resposta_raw]

    for msg in mensagens_bot:
        _salvar_mensagem(db, telefone, "assistant", msg)

    return mensagens_bot


def obter_resumo_lead(telefone: str, db: Session) -> dict | None:
    lead = db.query(Lead).filter(Lead.telefone == telefone).first()
    if not lead:
        return None
    return {
        "telefone": lead.telefone,
        "nome": lead.nome,
        "cpf": lead.cpf,
        "data_nascimento": lead.data_nascimento,
        "carro_interesse": lead.carro_interesse,
        "modalidade": lead.modalidade,
        "status": lead.status,
        "criado_em": lead.criado_em.strftime("%d/%m/%Y %H:%M") if lead.criado_em else None,
    }
