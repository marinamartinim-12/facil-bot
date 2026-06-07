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
# timeout: nenhuma chamada à IA pode travar o servidor indefinidamente.
# max_retries=0: o SDK NÃO faz re-tentativas próprias (com backoff longo) —
# nós controlamos as tentativas abaixo, sem bloquear o app por dezenas de segundos.
client = anthropic.Anthropic(
    api_key=settings.ANTHROPIC_API_KEY,
    timeout=15.0,
    max_retries=0,
)

MODELO_IA = "claude-sonnet-4-6"


def diagnostico_ia() -> dict:
    """Faz uma chamada mínima à IA e devolve o resultado real (ok ou erro exato).
    Usado pelo painel admin para descobrir por que a Maria pode estar falhando."""
    chave = settings.ANTHROPIC_API_KEY or ""
    info = {
        "modelo": MODELO_IA,
        "tem_chave": bool(chave),
        "chave_prefixo": (chave[:14] + "…") if chave else "(vazia)",
    }
    try:
        resp = client.messages.create(
            model=MODELO_IA,
            max_tokens=10,
            messages=[{"role": "user", "content": "responda apenas: ok"}],
        )
        info["ok"] = True
        info["resposta"] = resp.content[0].text.strip()
    except Exception as e:
        info["ok"] = False
        info["tipo_erro"] = type(e).__name__
        info["erro"] = str(e)
    return info


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
- qualificado: false somente quando desqualificado.

══════════════════════════════════════════════════
REGRAS INVIOLÁVEIS
══════════════════════════════════════════════════
1. NUNCA invente taxas, valores, prazos ou documentos. Se perguntarem → TRANSFIRA.
2. Se o cliente pedir para falar com humano/atendente/consultor → TRANSFIRA imediatamente.
3. Para transferir: mensagens: ["Claro! Vou te conectar agora com um de nossos especialistas. Em breve ele entrará em contato! 😊"]  proximo_estado: "transferido"

══════════════════════════════════════════════════
ROTEIRO — execute EXATAMENTE conforme o ESTADO ATUAL
══════════════════════════════════════════════════

━━━ ESTADO "inicio" ━━━
Envie estas 3 mensagens e salve proximo_estado: "aguardando_nome"
  [0] "Olá ! Seja bem-vindo à Fácil Financiamentos, eleita a melhor plataforma de financiamentos de MG, há 23 anos no mercado."
  [1] "Meu nome é Maria, sou assistente virtual da Fácil Financiamentos. 🧑‍💻"
  [2] "Qual o seu nome ?"

━━━ ESTADO "aguardando_nome" ━━━
Salve o nome. Envie estas 2 mensagens e salve proximo_estado: "aguardando_modalidade"
  [0] "Nós somos especialistas em financiamento de particular para particular, credenciados nas 9 melhores financeiras do Brasil ! Encontraremos as melhores taxas e condições para você."
  [1] "Qual serviço você procura?\n1 - Financiamento de veículo (quero comprar um carro, novo ou usado).\n2 - Empréstimo com garantia do seu veículo (tenho um carro e preciso de crédito).\n3 - Outros assuntos.\n4 - Parceiro."

━━━ ESTADO "aguardando_modalidade" ━━━
Identifique a escolha do cliente:
  • "1" / "financiamento" / "comprar" / "carro novo" / "carro usado" = FINANCIAMENTO → modalidade: "financiamento"
  • "2" / "refinanciamento" / "já tenho" / "crédito" / "garantia" = REFINANCIAMENTO → modalidade: "refinanciamento"
  • "3" / "outros" / "outro assunto" / "outros assuntos" / qualquer assunto que claramente não seja financiamento nem refinanciamento = OUTROS ASSUNTOS → proximo_estado: "transferido"
  • "4" / "parceiro" / "sou parceiro" = PARCEIRO → proximo_estado: "transferido" (trate exatamente como OUTROS ASSUNTOS, mesma resposta)

Se FINANCIAMENTO → proximo_estado: "coletando_cidade"
  Envie: "Estamos em Belo Horizonte, MG, de que cidade você é ?"

Se REFINANCIAMENTO → proximo_estado: "coletando_cpf"
  (Refinanciamento é 100% online, atendemos todo o Brasil. NÃO pergunte cidade.)
  Envie estas 2 mensagens:
  [0] "Com apenas 3 dados, faremos uma pré análise e encontraremos as melhores taxas e condições para você. 🚘🛵🚚"
  [1] "Qual o seu CPF ?"

Se OUTROS ASSUNTOS ou PARCEIRO → proximo_estado: "transferido"
  Envie: "Claro! Já estou te transferindo para uma de nossas atendentes. 😊 Se quiser, pode nos contar aqui qual o assunto para direcionarmos melhor!"

━━━ ESTADO "coletando_cidade" ━━━  (somente para Financiamento)
Salve a cidade. Avalie a distância até BH:

CIDADES PRÓXIMAS (até ~200km — atenda normalmente):
Grande BH, Contagem, Betim, Sete Lagoas, Ipatinga, Coronel Fabriciano, Juiz de Fora, Divinópolis, Itabira, João Monlevade, Conselheiro Lafaiete, Ouro Preto, Barbacena, Viçosa, Muriaé, Gov. Valadares, Montes Claros, Pouso Alegre, Varginha, Lavras, Poços de Caldas, Uberlândia, Uberaba, e qualquer cidade de Minas Gerais não mencionada abaixo.

Se cidade PRÓXIMA → proximo_estado: "coletando_cpf"
  Envie estas 2 mensagens:
  [0] "Com apenas 3 dados, faremos uma pré análise e encontraremos as melhores taxas e condições para você. 🚘🛵🚚"
  [1] "Qual o seu CPF ?"

Se cidade FORA DE MINAS GERAIS ou muito distante (ex: São Paulo capital, Rio de Janeiro, Salvador, Brasília, Fortaleza, Manaus, etc.) → proximo_estado: "coletando_cidade" (aguarda resposta)
  Envie: "Olha, vejo que você mora longe da nossa sede. Por uma exigência do banco, os fechamentos de contratos de financiamento devem ser feitos de maneira presencial aqui em BH. Você consegue se deslocar até a gente ?"

  • Se o cliente CONFIRMAR que pode vir → proximo_estado: "coletando_cpf"
    Envie estas 2 mensagens:
    [0] "Com apenas 3 dados, faremos uma pré análise e encontraremos as melhores taxas e condições para você. 🚘🛵🚚"
    [1] "Qual o seu CPF ?"

  • Se o cliente NÃO puder vir → ofereça o Refinanciamento:
    Envie: "Entendemos! Temos também o Refinanciamento, que realizamos 100% online em todo o Brasil. Se você já possui um veículo, conseguimos liberar crédito usando ele como garantia. Tem interesse ?"
    • Se aceitar → modalidade: "refinanciamento", proximo_estado: "coletando_cpf"
      Envie estas 2 mensagens:
      [0] "Com apenas 3 dados, faremos uma pré análise e encontraremos as melhores taxas e condições para você. 🚘🛵🚚"
      [1] "Qual o seu CPF ?"
    • Se NÃO aceitar → qualificado: false, proximo_estado: "desqualificado"
      Envie: "Agradecemos o contato e permanecemos à disposição ! Qualquer coisa, estamos aqui. 😊"

━━━ ESTADO "coletando_cpf" ━━━
Salve o CPF. Envie e salve proximo_estado: "coletando_data_nasc"
  "Qual a sua data de nascimento ?"

━━━ ESTADO "coletando_data_nasc" ━━━
Salve a data de nascimento no campo data_nascimento, sempre no formato DD/MM/YYYY.
Reconheça qualquer formato que o cliente usar, por exemplo:
  "121290" → "12/12/1990"
  "12121990" → "12/12/1990"
  "12/12/90" → "12/12/1990"
  "12-12-1990" → "12/12/1990"
  "12.12.1990" → "12/12/1990"
  "12 12 1990" → "12/12/1990"
  "meu aniversário é em 12/12/1990" → "12/12/1990"
  "12 de dezembro de 1990" → "12/12/1990"
  Anos com 2 dígitos: 90→1990, 85→1985, 01→2001, 10→2010 (≤24 = 2000s)
Envie e salve proximo_estado: "coletando_carro"
  Para Financiamento: "Qual veículo você está procurando ?"
  Para Refinanciamento: "Qual o modelo e ano do seu veículo ?"

━━━ ESTADO "coletando_carro" ━━━
Salve o veículo. Envie e salve proximo_estado: "transferido"
  "Ótimo ! Já tenho os dados que preciso, aguarde um momento que um dos nossos especialistas irá seguir com você. 😊"

══════════════════════════════════════════════════
Estados válidos para proximo_estado:
aguardando_nome | aguardando_modalidade | coletando_cidade |
coletando_cpf | coletando_data_nasc | coletando_carro |
transferido | desqualificado
══════════════════════════════════════════════════
"""


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

# ── Opção 3: Mensagens do estado "inicio" 100% fixas (sem Claude) ──
MENSAGENS_INICIO = [
    "Olá ! Seja bem-vindo à Fácil Financiamentos, eleita a melhor plataforma de financiamentos de MG, há 23 anos no mercado.",
    "Meu nome é Maria, sou assistente virtual da Fácil Financiamentos. 🧑‍💻",
    "Qual o seu nome ?",
]

_ESTADOS_TERMINAIS = {
    EstadoConversaEnum.transferido,
    EstadoConversaEnum.desqualificado,
    EstadoConversaEnum.finalizado,
}

# ── Opção 2: Transições válidas por estado ──
# Se Claude sugerir algo fora desta lista, o código corrige automaticamente.
_TRANSICOES_VALIDAS: dict[str, list[str]] = {
    EstadoConversaEnum.inicio:                ["aguardando_nome"],
    EstadoConversaEnum.aguardando_nome:       ["aguardando_modalidade"],
    EstadoConversaEnum.aguardando_modalidade: ["coletando_cidade", "coletando_cpf", "transferido"],
    EstadoConversaEnum.coletando_cidade:      ["coletando_cpf", "coletando_cidade", "desqualificado"],
    EstadoConversaEnum.coletando_cpf:         ["coletando_data_nasc"],
    EstadoConversaEnum.coletando_data_nasc:   ["coletando_carro"],
    EstadoConversaEnum.coletando_carro:       ["transferido"],
}


def _validar_proximo_estado(estado_atual: str, proximo_estado: str) -> str:
    """Garante que a transição de estado é válida. Corrige automaticamente se Claude errar."""
    # Estados terminais sempre são permitidos
    if proximo_estado in _ESTADOS_TERMINAIS:
        return proximo_estado

    validos = _TRANSICOES_VALIDAS.get(estado_atual)
    if validos is None:
        return estado_atual  # estado não mapeado, mantém

    if proximo_estado in validos:
        return proximo_estado

    # Claude sugeriu transição inválida — corrige
    if len(validos) == 1:
        correto = validos[0]
        print(f"⚠️  Transição inválida {estado_atual} → {proximo_estado}, forçando {correto}")
        return correto

    # Múltiplas opções possíveis e nenhuma bateu — mantém estado atual
    print(f"⚠️  Transição inválida {estado_atual} → {proximo_estado}, mantendo {estado_atual}")
    return estado_atual


def _formatar_cpf(cpf: str) -> str:
    n = re.sub(r"\D", "", cpf)
    if len(n) == 11:
        return f"{n[:3]}.{n[3:6]}.{n[6:9]}-{n[9:]}"
    return cpf


_MESES_PT = {
    "janeiro": "01", "fevereiro": "02", "março": "03", "marco": "03",
    "abril": "04", "maio": "05", "junho": "06", "julho": "07",
    "agosto": "08", "setembro": "09", "outubro": "10",
    "novembro": "11", "dezembro": "12",
    "jan": "01", "fev": "02", "mar": "03", "abr": "04",
    "mai": "05", "jun": "06", "jul": "07", "ago": "08",
    "set": "09", "out": "10", "nov": "11", "dez": "12",
}


def _data_valida(d: str, m: str, a: str) -> bool:
    try:
        return 1 <= int(d) <= 31 and 1 <= int(m) <= 12 and 1900 <= int(a) <= 2099
    except (ValueError, TypeError):
        return False


def _ano_dois_digitos(y: str) -> str:
    """Converte ano de 2 dígitos para 4: ≤24 → 2000s, caso contrário → 1900s."""
    n = int(y)
    return str(2000 + n if n <= 24 else 1900 + n)


def _normalizar_data_nascimento(texto: str) -> str | None:
    """
    Converte qualquer formato de data de nascimento para DD/MM/YYYY.
    Aceita: 12121990, 121290, 12/12/1990, 12-12-1990, 12.12.1990,
            12 12 1990, 1990-12-12, '12 de dezembro de 1990', etc.
    Retorna None se não conseguir interpretar.
    """
    if not texto:
        return None

    t = texto.strip()

    # ── 1. Mês por extenso ─────────────────────────────────────────────────────
    t_lower = t.lower()
    for nome, num in _MESES_PT.items():
        if nome in t_lower:
            nums = re.findall(r"\d+", t)
            dias  = [n for n in nums if len(n) <= 2 and 1 <= int(n) <= 31]
            anos4 = [n for n in nums if len(n) == 4 and 1900 <= int(n) <= 2099]
            anos2 = [n for n in nums if len(n) == 2]
            if dias:
                d = dias[0].zfill(2)
                if anos4:
                    return f"{d}/{num}/{anos4[0]}"
                if anos2:
                    return f"{d}/{num}/{_ano_dois_digitos(anos2[0])}"

    # ── 2. Só dígitos (sem separadores) ────────────────────────────────────────
    apenas = re.sub(r"\D", "", t)
    if len(apenas) == 8:            # DDMMYYYY
        d, m, a = apenas[:2], apenas[2:4], apenas[4:]
        if _data_valida(d, m, a):
            return f"{d}/{m}/{a}"
    if len(apenas) == 6:            # DDMMYY
        d, m, y = apenas[:2], apenas[2:4], apenas[4:]
        a = _ano_dois_digitos(y)
        if _data_valida(d, m, a):
            return f"{d}/{m}/{a}"

    # ── 3. Com separadores (/, -, ., espaço) ───────────────────────────────────
    for sep in ["/", "-", ".", " "]:
        partes = [re.sub(r"\D", "", p) for p in t.split(sep) if p.strip()]
        if len(partes) != 3 or not all(partes):
            continue
        p0, p1, p2 = partes

        # ISO: YYYY-MM-DD
        if len(p0) == 4 and 1900 <= int(p0) <= 2099:
            if _data_valida(p2, p1, p0):
                return f"{p2.zfill(2)}/{p1.zfill(2)}/{p0}"

        # DD/MM/YYYY
        elif len(p2) == 4:
            if _data_valida(p0, p1, p2):
                return f"{p0.zfill(2)}/{p1.zfill(2)}/{p2}"

        # DD/MM/YY
        elif len(p2) == 2:
            a = _ano_dois_digitos(p2)
            if _data_valida(p0, p1, a):
                return f"{p0.zfill(2)}/{p1.zfill(2)}/{a}"

    return None  # não conseguiu interpretar


def _salvar_mensagem(db: Session, telefone: str, role: str, conteudo: str):
    db.add(MensagemConversa(telefone=telefone, role=role, conteudo=conteudo))
    db.commit()


def _atualizar_lead(db: Session, lead: Lead, dados: dict, proximo_estado: str, qualificado: bool):
    if dados.get("nome"):
        lead.nome = dados["nome"]
    if dados.get("cpf"):
        lead.cpf = _formatar_cpf(dados["cpf"])
    if dados.get("data_nascimento"):
        normalizada = _normalizar_data_nascimento(dados["data_nascimento"])
        lead.data_nascimento = normalizada if normalizada else dados["data_nascimento"]
    if dados.get("cidade"):
        lead.cidade = dados["cidade"]
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
        conteudo = m.conteudo or ""
        if m.role == "assistant":
            # Tenta extrair o texto real das mensagens do bot
            try:
                parsed = json.loads(conteudo)
                textos = parsed.get("mensagens") or [parsed.get("mensagem", conteudo)]
                conteudo = " | ".join(t for t in textos if t)
            except Exception:
                pass  # Não era JSON, usa texto puro
        conteudo = (conteudo or "").strip()
        # A API da Claude rejeita mensagens com conteúdo vazio (erro 400).
        # Mensagens de áudio/mídia sem texto entravam aqui e quebravam TODA a conversa.
        if not conteudo:
            continue
        msgs.append({"role": m.role, "content": conteudo})

    # A primeira mensagem precisa ser do usuário (regra da API).
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
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

    # Conversa já encerrada — resposta ciente do horário (não promete contato fora do expediente)
    if lead.estado_conversa in (EstadoConversaEnum.finalizado, EstadoConversaEnum.transferido):
        nome = f" {lead.nome}" if lead.nome else ""
        if _proximo_horario_atendimento():   # != "" → estamos FORA do horário
            resposta = (
                f"Olá{nome}! No momento estamos fora do horário de atendimento. "
                "Funcionamos seg–sex das 09h às 18h e sábado das 09h às 13h. "
                "Retornaremos seu contato no primeiro horário disponível! 🕘"
            )
        else:
            resposta = f"Olá{nome}! Em breve uma atendente entrará em contato. 😊"
        _salvar_mensagem(db, telefone, "user", mensagem_cliente)
        _salvar_mensagem(db, telefone, "assistant", resposta)
        return [resposta]

    # Salva mensagem do cliente
    _salvar_mensagem(db, telefone, "user", mensagem_cliente)

    # ── Opção 3: ESTADO "inicio" 100% determinístico — zero Claude ──
    if lead.estado_conversa == EstadoConversaEnum.inicio:
        _atualizar_lead(db, lead, {}, EstadoConversaEnum.aguardando_nome, True)
        for msg in MENSAGENS_INICIO:
            _salvar_mensagem(db, telefone, "assistant", msg)
        return MENSAGENS_INICIO

    # Histórico (usado pelos demais estados)
    historico = (
        db.query(MensagemConversa)
        .filter(MensagemConversa.telefone == telefone)
        .order_by(MensagemConversa.id)
        .all()
    )

    # O aviso de "fora do horário" NÃO é mais responsabilidade da IA — ele é
    # anexado pelo próprio código ao transferir (determinístico, sempre igual).
    aviso_horario = ""

    # System com contexto do estado atual
    # Estado do lead — muda a cada mensagem, fica FORA do cache
    estado_dinamico = (
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

    # CACHE DE PROMPT: o roteiro fixo (grande) é marcado p/ a IA "lembrar" por
    # alguns minutos — nas mensagens seguintes da mesma conversa ele custa ~10%.
    # Reduz bastante o gasto de créditos por lead.
    system = [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": estado_dinamico},
    ]

    messages = _historico_limpo(historico)
    # Mensagem atual do cliente (áudio/mídia podem vir vazios → evita erro 400)
    msg_atual = (mensagem_cliente or "").strip() or "(mensagem sem texto)"
    messages.append({"role": "user", "content": msg_atual})

    # Chama o Claude — 2 tentativas para falhas transitórias (cada uma com timeout de 15s)
    import time as _time
    resposta_raw = None
    ultimo_erro = None
    for _tent in range(2):
        try:
            response = client.messages.create(
                model=MODELO_IA,
                max_tokens=1024,
                system=system,
                messages=messages,
            )
            resposta_raw = response.content[0].text.strip()
            try:
                u = response.usage
                print(f"💾 IA cache — criado:{getattr(u,'cache_creation_input_tokens',0)} "
                      f"lido:{getattr(u,'cache_read_input_tokens',0)} "
                      f"entrada:{u.input_tokens} saída:{u.output_tokens}")
            except Exception:
                pass
            break
        except Exception as e:
            ultimo_erro = e
            print(f"❌ Erro Claude (tentativa {_tent+1}/2): {type(e).__name__}: {e}")
            _time.sleep(0.5)

    if resposta_raw is None:
        # IA indisponível de vez → NÃO perde o lead: transfere para uma atendente.
        print(f"🚨 IA indisponível — transferindo {telefone} para atendimento humano. "
              f"Último erro: {repr(ultimo_erro)}")
        try:
            lead.estado_conversa = EstadoConversaEnum.transferido
            if lead.status == StatusLeadEnum.em_atendimento:
                lead.status = StatusLeadEnum.qualificado
            lead.atualizado_em = datetime.utcnow()
            db.commit()
        except Exception:
            db.rollback()
        msg = ("Obrigada pelas informações! 😊 Vou te transferir agora para uma de "
               "nossas especialistas, que vai continuar seu atendimento. Só um instante!")
        _salvar_mensagem(db, telefone, "assistant", msg)
        return [msg]

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
        estado_antes    = lead.estado_conversa  # Guarda antes de atualizar

        _atualizar_lead(db, lead, dados_coletados, proximo_estado, qualificado)

        # Ao TRANSFERIR para o atendente (qualquer caminho), se estiver fora do
        # horário, o código acrescenta a mensagem padrão — com o nome do cliente.
        if (proximo_estado == "transferido"
                and estado_antes != EstadoConversaEnum.transferido):
            prox = _proximo_horario_atendimento()
            if prox:
                nome = f" {lead.nome}" if lead.nome else ""
                mensagens_bot.append(
                    f"Olá{nome}! 😊 No momento estamos fora do horário de atendimento. "
                    "Funcionamos seg–sex das 09h às 18h e sábado das 09h às 13h. "
                    "Retornaremos seu contato no primeiro horário disponível! 🕘"
                )

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
