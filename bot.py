"""
Fácil Financiamentos — Motor de conversa IA
Roteiro oficial de atendimento da Fácil Financiamentos.
"""

import re
import json
import anthropic
from datetime import datetime
from sqlalchemy.orm import Session

from config import get_settings
from models import (
    Lead, MensagemConversa, Configuracao,
    EstadoConversaEnum, ModalidadeEnum, StatusLeadEnum,
)

settings = get_settings()
client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

# ─── Prompt base ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Você é Maria, atendente virtual da *Fácil Financiamentos*, especializada em financiamento e crédito com garantia de veículo, localizada em Belo Horizonte, MG, há 23 anos no mercado.

📋 REGRAS OBRIGATÓRIAS:
- Responda SEMPRE em português brasileiro, de forma calorosa, próxima e profissional
- Siga o roteiro à risca, uma etapa por vez
- Se o cliente perguntar sobre empréstimo pessoal sem garantia, explique gentilmente que trabalhamos apenas com financiamento de veículos e crédito com garantia de veículo

🚫 REGRAS INVIOLÁVEIS — NUNCA QUEBRE ESTAS REGRAS:

1. JAMAIS invente ou estime qualquer dado que não sabe, incluindo:
   - Taxas de juros
   - Valor de parcelas
   - Prazo de aprovação
   - Documentos necessários
   - Condições específicas
   - Valor máximo de crédito
   - Qualquer número ou percentual
   Se o cliente perguntar qualquer uma dessas coisas, transfira IMEDIATAMENTE para um consultor.

2. Se o cliente pedir para falar com um humano/atendente/consultor/pessoa real:
   Transfira IMEDIATAMENTE. Não tente convencer o cliente a continuar com o bot.

3. Se receber qualquer pergunta fora do seu roteiro que você não sabe responder com certeza:
   Transfira IMEDIATAMENTE para um consultor.

⚡ COMO TRANSFERIR:
Quando precisar transferir, responda com esta mensagem e use proximo_estado: "transferido":
"Claro! Vou te conectar agora com uma de nossas consultoras. Um momento! 😊
Em breve ela entrará em contato com você."

🏢 SOBRE A FÁCIL FINANCIAMENTOS:
- 23 anos no mercado, eleita melhor plataforma de financiamentos de MG
- De particular para particular — o cliente escolhe o veículo
- Credenciados nas 9 melhores financeiras do Brasil
- Atendimento online ou presencial em BH
- Veículos: carros, motos, caminhões 🚘 🛵 🚚

📋 ROTEIRO DE ATENDIMENTO (siga exatamente esta ordem):

ETAPA 1 — BOAS-VINDAS (estado: inicio)
Quando o cliente entrar em contato, envie EXATAMENTE esta mensagem:

"Olá, seja bem vindo a Fácil Financiamentos. Meu nome é Maria e sou sua atendente virtual, estou aqui para ajudá-lo!

Qual o seu nome?"

ETAPA 2 — NOME (estado: aguardando_nome)
O cliente informou o nome. Salve o nome e envie EXATAMENTE:

"A gente oferece soluções rápidas e fáceis para você!
Qual serviço você procura?
1 - Financiamento de veículo (quero COMPRAR um carro);
2 - Refinanciamento (já tenho um carro e preciso de crédito);"

ETAPA 3 — MODALIDADE (estado: aguardando_modalidade)
O cliente escolheu 1 ou 2 (ou descreveu o que precisa). Identifique:
- Opção 1 / FINANCIAMENTO: quer comprar um veículo
- Opção 2 / REFINANCIAMENTO/CGI: já tem veículo e quer crédito com garantia

Responda com entusiasmo e envie:
"Somos credenciados nas 9 melhores financeiras do Brasil, podemos te atender online, ou presencialmente. 🧑🏽‍💼

Com apenas 3 dados, faremos uma pré análise, e encontraremos as melhores taxas e condições para você 🚘 🛵 🚚

Vamos lá!

Digite seu CPF:"

ETAPA 4 — CPF (estado: coletando_cpf)
O cliente enviou o CPF. Confirme e peça:
"Obrigado! Agora digite sua data de nascimento:"

ETAPA 5 — DATA DE NASCIMENTO (estado: coletando_data_nasc)
O cliente enviou a data. Confirme e peça:
"Ótimo! Por último, digite o ano e modelo do veículo:"
(Se for financiamento: veículo que quer comprar. Se for refinanciamento: veículo que possui.)

ETAPA 6 — VEÍCULO E FINALIZAÇÃO (estado: coletando_carro → finalizado)
O cliente enviou o veículo. Encerre com:
"Obrigado pelas confirmações, em breve uma de nossas consultoras entrará em contato. 🤝"

⚠️ RETORNO JSON OBRIGATÓRIO:
Você DEVE retornar SEMPRE neste formato JSON exato — nada antes, nada depois:
{
  "mensagem": "texto exato para o cliente",
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
- "aguardando_nome" — após enviar boas-vindas
- "aguardando_modalidade" — após receber o nome
- "coletando_cpf" — após identificar modalidade
- "coletando_data_nasc" — após receber CPF
- "coletando_carro" — após receber data de nascimento
- "finalizado" — após receber o veículo
- "transferido" — quando precisar transferir para consultora
- "desqualificado" — se cliente pedir produto fora do escopo

Em "dados_coletados": preencha apenas o campo recebido nesta mensagem (o resto null).
Em "qualificado": false apenas se desqualificado.
Em "nome": preencha quando o cliente informar o nome dele.
"""


def _historico_para_messages(historico: list) -> list[dict]:
    return [{"role": m.role, "content": m.conteudo} for m in historico[-20:]]


def _salvar_mensagem(db: Session, telefone: str, role: str, conteudo: str):
    msg = MensagemConversa(telefone=telefone, role=role, conteudo=conteudo)
    db.add(msg)
    db.commit()


def _atualizar_lead(db: Session, lead: Lead, dados: dict, proximo_estado: str, qualificado: bool):
    if dados.get("cpf"):
        lead.cpf = _formatar_cpf(dados["cpf"])
    if dados.get("data_nascimento"):
        lead.data_nascimento = dados["data_nascimento"]
    if dados.get("carro_interesse"):
        lead.carro_interesse = dados["carro_interesse"]
    if dados.get("cidade"):
        # Salva cidade no campo nome por enquanto (até adicionar campo específico)
        lead.nome = dados["cidade"]
    if dados.get("modalidade"):
        modalidade = dados["modalidade"].lower()
        if "refin" in modalidade or "garantia" in modalidade or "cgi" in modalidade:
            lead.modalidade = ModalidadeEnum.refinanciamento
        elif "financ" in modalidade or "comprar" in modalidade:
            lead.modalidade = ModalidadeEnum.financiamento

    lead.estado_conversa = proximo_estado
    lead.atualizado_em = datetime.utcnow()

    if not qualificado:
        lead.status = StatusLeadEnum.desqualificado
    elif proximo_estado in [EstadoConversaEnum.finalizado, EstadoConversaEnum.transferido]:
        lead.status = StatusLeadEnum.qualificado

    db.commit()
    db.refresh(lead)


def _formatar_cpf(cpf: str) -> str:
    apenas_numeros = re.sub(r"\D", "", cpf)
    if len(apenas_numeros) == 11:
        return f"{apenas_numeros[:3]}.{apenas_numeros[3:6]}.{apenas_numeros[6:9]}-{apenas_numeros[9:]}"
    return cpf


def _carregar_config(db: Session) -> dict:
    """Carrega configurações editáveis do banco de dados."""
    configs = db.query(Configuracao).all()
    return {c.chave: c.valor for c in configs}


def processar_mensagem(telefone: str, mensagem_cliente: str, db: Session) -> str:
    # Busca ou cria lead
    lead = db.query(Lead).filter(Lead.telefone == telefone).first()
    if not lead:
        lead = Lead(telefone=telefone)
        db.add(lead)
        db.commit()
        db.refresh(lead)

    # Se já finalizado ou transferido, não processa mais
    if lead.estado_conversa == EstadoConversaEnum.finalizado:
        nome = f" {lead.nome}," if lead.nome else ""
        return f"Olá{nome} seus dados já estão registrados! Em breve uma de nossas consultoras entrará em contato. 🤝"

    if lead.estado_conversa == EstadoConversaEnum.transferido:
        nome = f" {lead.nome}," if lead.nome else ""
        return f"Olá{nome} sua solicitação já foi registrada! Uma de nossas consultoras entrará em contato em breve. 😊"

    # Busca histórico
    historico = (
        db.query(MensagemConversa)
        .filter(MensagemConversa.telefone == telefone)
        .order_by(MensagemConversa.id)
        .all()
    )

    # Salva mensagem do cliente
    _salvar_mensagem(db, telefone, "user", mensagem_cliente)

    # Carrega configurações editáveis do admin
    config = _carregar_config(db)
    regras_extra = ""
    if lead.modalidade == ModalidadeEnum.financiamento:
        regras_extra = config.get("regras_financiamento", "")
    elif lead.modalidade == ModalidadeEnum.refinanciamento:
        regras_extra = config.get("regras_refinanciamento", "")

    msg_boas_vindas = config.get("mensagem_boas_vindas", "")
    msg_finalizacao = config.get("mensagem_finalizacao", "")

    system_com_contexto = (
        f"{SYSTEM_PROMPT}\n\n"
        f"--- CONFIGURAÇÕES EDITADAS PELO ADMIN ---\n"
        f"Mensagem de boas-vindas: {msg_boas_vindas}\n"
        f"Mensagem de finalização: {msg_finalizacao}\n"
        f"Regras específicas da modalidade: {regras_extra}\n\n"
        f"--- ESTADO ATUAL DA CONVERSA ---\n"
        f"Estado: {lead.estado_conversa}\n"
        f"Nome do cliente: {lead.nome or 'não informado'}\n"
        f"Modalidade: {lead.modalidade}\n"
        f"CPF: {lead.cpf or 'não informado'}\n"
        f"Nascimento: {lead.data_nascimento or 'não informado'}\n"
        f"Veículo: {lead.carro_interesse or 'não informado'}"
    )

    messages = _historico_para_messages(historico)
    messages.append({"role": "user", "content": mensagem_cliente})

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=system_com_contexto,
            messages=messages,
        )
        resposta_raw = response.content[0].text
    except Exception as e:
        print(f"Erro Claude: {e}")
        return "Desculpe, ocorreu um problema técnico. Tente novamente em instantes. 🙏"

    # Parse JSON
    try:
        json_match = re.search(r"\{[\s\S]*\}", resposta_raw)
        if json_match:
            dados_resposta = json.loads(json_match.group())
        else:
            raise ValueError("JSON não encontrado")

        mensagem_bot = dados_resposta.get("mensagem", resposta_raw)
        proximo_estado = dados_resposta.get("proximo_estado", lead.estado_conversa)
        dados_coletados = {k: v for k, v in (dados_resposta.get("dados_coletados") or {}).items() if v}
        qualificado = dados_resposta.get("qualificado", True)

        _atualizar_lead(db, lead, dados_coletados, proximo_estado, qualificado)

    except (json.JSONDecodeError, ValueError):
        mensagem_bot = resposta_raw

    _salvar_mensagem(db, telefone, "assistant", mensagem_bot)
    return mensagem_bot


def obter_resumo_lead(telefone: str, db: Session) -> dict | None:
    lead = db.query(Lead).filter(Lead.telefone == telefone).first()
    if not lead:
        return None
    return {
        "telefone": lead.telefone,
        "cidade": lead.nome,
        "cpf": lead.cpf,
        "data_nascimento": lead.data_nascimento,
        "carro_interesse": lead.carro_interesse,
        "modalidade": lead.modalidade,
        "status": lead.status,
        "criado_em": lead.criado_em.strftime("%d/%m/%Y %H:%M") if lead.criado_em else None,
    }
