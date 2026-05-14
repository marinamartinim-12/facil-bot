"""
Fácil Financiamentos — Motor de conversa IA
Conduz o atendimento inicial via WhatsApp, qualifica o lead e coleta dados.
"""

import re
import json
import anthropic
from datetime import datetime
from sqlalchemy.orm import Session

from config import get_settings
from models import (
    Lead, MensagemConversa,
    EstadoConversaEnum, ModalidadeEnum, StatusLeadEnum,
)

settings = get_settings()
client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

# ─── Prompt base do atendente ──────────────────────────────────────────────────

SYSTEM_PROMPT = """Você é o assistente virtual da *Fácil Financiamentos*, especializada em financiamento e refinanciamento de veículos.

Seu nome é *Fácil* e você deve ser simpático, direto e profissional.

🎯 MISSÃO: Qualificar leads e coletar dados iniciais para a equipe de vendas.

📋 REGRAS IMPORTANTES:
- Responda SEMPRE em português brasileiro, de forma natural e amigável
- Use emojis com moderação para humanizar o atendimento
- Seja conciso — respostas curtas e objetivas
- Não invente informações sobre taxas, parcelas ou aprovações
- Nunca confirme aprovação de crédito — isso é feito pela equipe
- Se o cliente perguntar sobre EMPRÉSTIMO PESSOAL: explique gentilmente que a Fácil é especializada em financiamento/refinanciamento de veículos e não trabalha com empréstimo pessoal sem garantia

🚗 PRODUTOS DA FÁCIL FINANCIAMENTOS:

1. **FINANCIAMENTO DE VEÍCULO**
   - Para quem quer COMPRAR um veículo (novo ou usado)
   - Parcelamento em até 60x
   - Veículos novos e seminovos
   - Entrada facilitada

2. **REFINANCIAMENTO / CGI (Crédito com Garantia de Imóvel/Veículo)**
   - Para quem já TEM um veículo quitado ou semi-quitado
   - Usa o veículo como garantia para levantar capital
   - Taxas menores que empréstimo pessoal
   - O cliente continua usando o veículo normalmente
   - Ideal para capital de giro, reforma, quitar dívidas, etc.

📊 FLUXO DE ATENDIMENTO:
Siga exatamente este fluxo, um passo por vez.

ETAPA 1 — BOAS-VINDAS E MODALIDADE
Apresente-se e pergunte qual produto o cliente busca. Ofereça as opções numeradas.

ETAPA 2 — EXPLICAÇÃO
Explique brevemente o produto escolhido e confirme se é isso que o cliente precisa.

ETAPA 3 — COLETA DE DADOS (um campo por mensagem)
Colete nesta ordem: Nome completo → CPF → Data de nascimento → Veículo (pretende comprar OU veículo que possui)

ETAPA 4 — FINALIZAÇÃO
Agradeça, informe que a equipe entrará em contato e despeça-se.

⚠️ RETORNO JSON OBRIGATÓRIO:
Você DEVE retornar suas respostas neste formato JSON exato:
{
  "mensagem": "texto da resposta para o cliente",
  "proximo_estado": "nome_do_estado",
  "dados_coletados": {
    "nome": null,
    "cpf": null,
    "data_nascimento": null,
    "carro_interesse": null,
    "modalidade": null
  },
  "qualificado": true
}

Estados possíveis para "proximo_estado":
- "aguardando_modalidade" — após boas-vindas
- "explicando_modalidade" — após cliente escolher
- "coletando_nome" — após confirmar interesse
- "coletando_cpf" — após receber nome
- "coletando_data_nasc" — após receber CPF
- "coletando_carro" — após receber data nascimento
- "finalizado" — após receber dados do carro
- "desqualificado" — se cliente não tem interesse ou pede produto fora do escopo

Em "dados_coletados", preencha apenas o campo coletado na mensagem atual (o resto null).
Em "qualificado": false apenas se desqualificado.
"""


def _historico_para_messages(historico: list[MensagemConversa]) -> list[dict]:
    """Converte histórico do banco para formato da API Anthropic."""
    return [{"role": m.role, "content": m.conteudo} for m in historico[-20:]]


def _salvar_mensagem(db: Session, telefone: str, role: str, conteudo: str):
    msg = MensagemConversa(telefone=telefone, role=role, conteudo=conteudo)
    db.add(msg)
    db.commit()


def _atualizar_lead(db: Session, lead: Lead, dados: dict, proximo_estado: str, qualificado: bool):
    """Atualiza lead com dados coletados e novo estado."""
    if dados.get("nome"):
        lead.nome = dados["nome"]
    if dados.get("cpf"):
        lead.cpf = _formatar_cpf(dados["cpf"])
    if dados.get("data_nascimento"):
        lead.data_nascimento = dados["data_nascimento"]
    if dados.get("carro_interesse"):
        lead.carro_interesse = dados["carro_interesse"]
    if dados.get("modalidade"):
        modalidade = dados["modalidade"].lower()
        if "refin" in modalidade or "cgi" in modalidade or "capital" in modalidade:
            lead.modalidade = ModalidadeEnum.refinanciamento
        elif "financ" in modalidade:
            lead.modalidade = ModalidadeEnum.financiamento

    lead.estado_conversa = proximo_estado
    lead.atualizado_em = datetime.utcnow()

    if not qualificado:
        lead.status = StatusLeadEnum.desqualificado
    elif proximo_estado == EstadoConversaEnum.finalizado:
        lead.status = StatusLeadEnum.qualificado

    db.commit()
    db.refresh(lead)


def _formatar_cpf(cpf: str) -> str:
    """Remove formatação e reaplica padrão 000.000.000-00."""
    apenas_numeros = re.sub(r"\D", "", cpf)
    if len(apenas_numeros) == 11:
        return f"{apenas_numeros[:3]}.{apenas_numeros[3:6]}.{apenas_numeros[6:9]}-{apenas_numeros[9:]}"
    return cpf


def _contexto_por_estado(estado: str, lead: Lead) -> str:
    """Adiciona contexto extra ao prompt conforme estado atual."""
    contextos = {
        EstadoConversaEnum.inicio: (
            "O cliente acabou de entrar em contato. Faça as boas-vindas da Fácil Financiamentos "
            "e pergunte qual serviço ele busca. Apresente as opções:\n"
            "1️⃣ Financiamento de veículo (quero COMPRAR um carro)\n"
            "2️⃣ Refinanciamento / CGI (já TENHO um carro e preciso de crédito)"
        ),
        EstadoConversaEnum.aguardando_modalidade: (
            "O cliente está escolhendo a modalidade. "
            "Identifique a escolha dele e explique brevemente o produto. "
            "Se ele mencionar 'empréstimo pessoal' ou algo fora do escopo, desqualifique gentilmente."
        ),
        EstadoConversaEnum.explicando_modalidade: (
            f"A modalidade escolhida é: {lead.modalidade}. "
            "Confirme se o cliente quer prosseguir e peça o nome completo dele."
        ),
        EstadoConversaEnum.coletando_nome: (
            "Colete o nome completo do cliente. "
            "Após receber, agradeça pelo nome e peça o CPF."
        ),
        EstadoConversaEnum.coletando_cpf: (
            f"O nome do cliente é {lead.nome or 'não informado'}. "
            "Colete o CPF. Após receber, peça a data de nascimento."
        ),
        EstadoConversaEnum.coletando_data_nasc: (
            f"Nome: {lead.nome}, CPF: {lead.cpf}. "
            "Colete a data de nascimento (DD/MM/AAAA). Após receber, "
            f"peça {'qual veículo ele pretende comprar' if lead.modalidade == ModalidadeEnum.financiamento else 'qual veículo ele possui (modelo, ano)'}"
        ),
        EstadoConversaEnum.coletando_carro: (
            f"Modalidade: {lead.modalidade}. "
            f"Colete {'o veículo de interesse (ex: Fiat Cronos 2023)' if lead.modalidade == ModalidadeEnum.financiamento else 'o veículo que o cliente possui como garantia (modelo e ano)'}. "
            "Após receber, finalize o atendimento agradecendo e informando que a equipe entrará em contato em breve."
        ),
    }
    return contextos.get(estado, "Continue o atendimento conforme o fluxo.")


def processar_mensagem(telefone: str, mensagem_cliente: str, db: Session) -> str:
    """
    Ponto de entrada principal.
    Recebe mensagem do cliente, processa com Claude e retorna resposta.
    """
    # Busca ou cria lead
    lead = db.query(Lead).filter(Lead.telefone == telefone).first()
    if not lead:
        lead = Lead(telefone=telefone)
        db.add(lead)
        db.commit()
        db.refresh(lead)

    # Se já finalizado ou desqualificado, resposta simples
    if lead.estado_conversa == EstadoConversaEnum.finalizado:
        return (
            "✅ Seus dados já foram registrados! Nossa equipe entrará em contato em breve. "
            "Caso precise de algo mais, entre em contato pelo nosso site. Obrigado! 😊"
        )

    # Busca histórico de conversa
    historico = (
        db.query(MensagemConversa)
        .filter(MensagemConversa.telefone == telefone)
        .order_by(MensagemConversa.id)
        .all()
    )

    # Salva mensagem do cliente
    _salvar_mensagem(db, telefone, "user", mensagem_cliente)

    # Monta contexto extra para o estado atual
    contexto_estado = _contexto_por_estado(lead.estado_conversa, lead)

    system_com_contexto = (
        f"{SYSTEM_PROMPT}\n\n"
        f"--- CONTEXTO ATUAL ---\n"
        f"Estado: {lead.estado_conversa}\n"
        f"Instrução: {contexto_estado}\n"
        f"Dados já coletados: nome={lead.nome}, cpf={lead.cpf}, "
        f"data_nasc={lead.data_nascimento}, carro={lead.carro_interesse}, "
        f"modalidade={lead.modalidade}"
    )

    messages = _historico_para_messages(historico)
    messages.append({"role": "user", "content": mensagem_cliente})

    # Chama Claude
    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=system_com_contexto,
            messages=messages,
        )
        resposta_raw = response.content[0].text
    except Exception as e:
        return "Desculpe, ocorreu um problema técnico. Tente novamente em instantes. 🙏"

    # Tenta parsear JSON
    try:
        # Extrai JSON mesmo se vier com texto ao redor
        json_match = re.search(r"\{[\s\S]*\}", resposta_raw)
        if json_match:
            dados_resposta = json.loads(json_match.group())
        else:
            raise ValueError("JSON não encontrado")

        mensagem_bot = dados_resposta.get("mensagem", resposta_raw)
        proximo_estado = dados_resposta.get("proximo_estado", lead.estado_conversa)
        dados_coletados = dados_resposta.get("dados_coletados", {})
        qualificado = dados_resposta.get("qualificado", True)

        # Remove Nones do dict
        dados_coletados = {k: v for k, v in (dados_coletados or {}).items() if v}

        # Atualiza lead
        _atualizar_lead(db, lead, dados_coletados, proximo_estado, qualificado)

    except (json.JSONDecodeError, ValueError):
        # Se Claude não retornou JSON válido, usa resposta bruta e mantém estado
        mensagem_bot = resposta_raw

    # Salva resposta do bot
    _salvar_mensagem(db, telefone, "assistant", mensagem_bot)

    return mensagem_bot


def obter_resumo_lead(telefone: str, db: Session) -> dict | None:
    """Retorna resumo do lead para notificação interna."""
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
