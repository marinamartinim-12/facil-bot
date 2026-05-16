"""
Fácil Financiamentos — Motor de conversa IA
Roteiro oficial de atendimento da Fácil Financiamentos.
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
O cliente escolheu 1 ou 2. Identifique:
- Opção 1 / FINANCIAMENTO: quer comprar um veículo → próximo estado: coletando_cidade
- Opção 2 / REFINANCIAMENTO: já tem veículo e quer crédito → próximo estado: coletando_cpf (pula cidade)

Se FINANCIAMENTO, pergunte a cidade:
"Ótimo! Para o financiamento, o fechamento do contrato é feito presencialmente aqui em Belo Horizonte (exigência do banco). 🏢

De qual cidade você é?"

Se REFINANCIAMENTO, vá direto para o CPF:
"Somos credenciados nas 9 melhores financeiras do Brasil, podemos te atender 100% online! 🧑🏽‍💼

Com apenas 3 dados, faremos uma pré análise e encontraremos as melhores taxas para você. 🚘

Digite seu CPF:"

ETAPA 4A — CIDADE (estado: coletando_cidade) — SOMENTE PARA FINANCIAMENTO
O cliente informou a cidade. Salve em "cidade".
- Se a cidade estiver dentro de ~200km de BH (Contagem, Betim, Sete Lagoas, Ipatinga, Coronel Fabriciano, Juiz de Fora, Divinópolis, Itabira, João Monlevade, Conselheiro Lafaiete, Ouro Preto, Barbacena, Viçosa, Muriaé, Uberlândia, Uberaba, Governador Valadares, Montes Claros, Pouso Alegre, Varginha, Lavras, Poços de Caldas, ou qualquer cidade da Grande BH):
  → Continue normalmente para o CPF:
  "Perfeito! Somos credenciados nas 9 melhores financeiras do Brasil. 🚘

  Com apenas 3 dados faremos uma pré análise. Vamos lá!

  Digite seu CPF:"
  → próximo estado: coletando_cpf

- Se a cidade estiver FORA desse raio:
  → Pergunte: "Para o financiamento, o fechamento é presencial em BH (exigência do banco). Você teria disponibilidade de vir até nós?"
  → próximo estado: coletando_cidade (aguarda resposta sobre disponibilidade)

- Se o cliente CONFIRMAR que pode vir a BH:
  → Continue para CPF normalmente
  → próximo estado: coletando_cpf

- Se o cliente NÃO puder vir:
  → Informe: "Entendemos! Para o financiamento precisamos do fechamento presencial. Caso você já possua um veículo, temos o refinanciamento que é 100% online. Se quiser, posso te atender por essa modalidade!"
  → Se ele aceitar refinanciamento: mude modalidade e vá para CPF
  → Se não: desqualifique gentilmente
  → próximo estado: desqualificado

ETAPA 5 — CPF (estado: coletando_cpf)
O cliente enviou o CPF. Confirme e peça:
"Obrigado! Agora digite sua data de nascimento:"

ETAPA 6 — DATA DE NASCIMENTO (estado: coletando_data_nasc)
O cliente enviou a data. Confirme e peça:
"Ótimo! Por último, qual o ano e modelo do veículo?"
(Financiamento: veículo que quer comprar. Refinanciamento: veículo que possui.)

ETAPA 7 — VEÍCULO E FINALIZAÇÃO (estado: coletando_carro → finalizado)
O cliente enviou o veículo. Encerre com:
"Obrigado pelas confirmações, em breve uma de nossas consultoras entrará em contato. 🤝"

⚠️ RETORNO JSON OBRIGATÓRIO:
Você DEVE retornar SEMPRE neste formato JSON exato — nada antes, nada depois:
{
  "mensagem": "texto exato para o cliente",
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

Estados possíveis para "proximo_estado":
- "aguardando_nome" — após enviar boas-vindas
- "aguardando_modalidade" — após receber o nome
- "coletando_cidade" — após identificar FINANCIAMENTO (pergunta cidade)
- "coletando_cpf" — após cidade OK ou após identificar REFINANCIAMENTO
- "coletando_data_nasc" — após receber CPF
- "coletando_carro" — após receber data de nascimento
- "finalizado" — após receber o veículo
- "transferido" — quando precisar transferir para consultora
- "desqualificado" — cliente fora do escopo ou não pode vir a BH

Em "dados_coletados": preencha apenas os campos recebidos nesta mensagem (o resto null).
Em "qualificado": false apenas se desqualificado.
Em "cidade": preencha quando o cliente informar a cidade dele.
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
    if dados.get("nome"):
        lead.nome = dados["nome"]
    if dados.get("cidade"):
        # Guarda cidade no campo carro_interesse temporariamente se não houver campo próprio
        # (usamos observacoes para não sobrescrever nada importante)
        pass  # cidade é usada apenas para qualificação pelo bot, não precisa persistir separado
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


def _proximo_horario_atendimento() -> str:
    """Retorna string com o próximo horário de atendimento, ou vazio se estiver dentro do horário."""
    from datetime import timedelta
    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    dia = agora.weekday()   # 0=seg ... 6=dom
    hora_dec = agora.hour + agora.minute / 60

    # Dentro do horário?
    if dia < 5 and 9 <= hora_dec < 18:
        return ""
    if dia == 5 and 9 <= hora_dec < 13:
        return ""

    # Calcula próximo turno
    if dia < 5:          # Segunda a sexta
        if hora_dec < 9:
            return "hoje às 09h"
        elif dia == 4:   # Sexta após 18h → sábado (trabalhamos!)
            return "amanhã (sábado) às 09h"
        else:            # Segunda a quinta após 18h → dia seguinte
            nomes = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado"]
            return f"{nomes[dia + 1]} às 09h"
    elif dia == 5:       # Sábado
        if hora_dec < 9:
            return "hoje (sábado) às 09h"
        else:            # Sábado após 13h → segunda
            return "segunda-feira às 09h"
    else:                # Domingo → segunda
        return "segunda-feira às 09h"


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
    if lead.estado_conversa in [EstadoConversaEnum.finalizado, EstadoConversaEnum.transferido]:
        nome = f" {lead.nome}" if lead.nome else ""
        return (
            f"Olá{nome}, não consigo tirar dúvidas ainda, "
            f"mas em breve uma atendente humana entrará em contato. 😊"
        )

    # (bot atende 24h — aviso de horário só na finalização)

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

    # Aviso de horário para incluir na mensagem de encerramento
    fora_do_horario = bool(_proximo_horario_atendimento())
    aviso_horario = ""
    if fora_do_horario:
        aviso_horario = (
            f"\n\nIMPORTANTE: Estamos FORA do horário de atendimento agora. "
            f"Na mensagem de finalização, após agradecer, adicione: "
            f"'No momento estamos fora do horário de atendimento. "
            f"Nosso horário de funcionamento é segunda a sexta das 09h às 18h e sábado das 09h às 13h. "
            f"Assim que houver alguém disponível, entraremos em contato! 🕘'"
        )

    system_com_contexto = (
        f"{SYSTEM_PROMPT}\n\n"
        f"--- ESTADO ATUAL DA CONVERSA ---\n"
        f"Estado atual: {lead.estado_conversa}\n"
        f"Nome do cliente: {lead.nome or 'não informado ainda'}\n"
        f"Modalidade escolhida: {lead.modalidade}\n"
        f"CPF: {lead.cpf or 'não informado ainda'}\n"
        f"Data de nascimento: {lead.data_nascimento or 'não informado ainda'}\n"
        f"Veículo: {lead.carro_interesse or 'não informado ainda'}\n\n"
        f"INSTRUÇÃO CRÍTICA: Você está no estado '{lead.estado_conversa}'. "
        f"Siga EXATAMENTE o roteiro a partir deste estado. "
        f"Não repita etapas já concluídas. Não pule etapas."
        + aviso_horario
        + (f"\n\n--- REGRAS DA MODALIDADE ---\n{regras_extra}" if regras_extra else "")
        + (f"\n\n--- MENSAGEM DE FINALIZAÇÃO PERSONALIZADA ---\n{msg_finalizacao}" if msg_finalizacao else "")
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
