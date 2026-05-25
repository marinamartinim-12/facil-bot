"""
Fácil Financiamentos — Servidor principal v2
FastAPI + Webhook Z-API + Dashboard com login
"""

import asyncio
import base64
import json
import os
import re
import uuid
import httpx
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Fuso horário de Brasília (UTC-3)
_TZ_BR = ZoneInfo("America/Sao_Paulo")

def _fmt_br(dt: datetime | None, fmt: str = "%d/%m/%Y %H:%M") -> str | None:
    """Converte datetime UTC para horário de Brasília e formata."""
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_TZ_BR).strftime(fmt)

def _agora_br() -> datetime:
    """Retorna o datetime atual no fuso de Brasília."""
    return datetime.now(_TZ_BR)
from fastapi import FastAPI, Request, Depends, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, FileResponse
import secrets
from pathlib import Path
from sqlalchemy.orm import Session

from config import get_settings
from models import Lead, MensagemConversa, Usuario, Configuracao, Contrato, Parceiro, ContatoParceiro, SessaoUsuario, criar_tabelas, get_db, StatusLeadEnum, ModalidadeEnum, RoleEnum, EstadoConversaEnum
from bot import processar_mensagem, obter_resumo_lead, _proximo_horario_atendimento
from auth import verificar_senha, hash_senha, criar_token, obter_usuario_atual, requer_admin

settings = get_settings()
app = FastAPI(title="Fácil Financiamentos", version="2.0.0")

# Cache para deduplicar mensagens enviadas pelo painel vs. webhook fromMe
# Chave: (telefone, texto_normalizado) → timestamp do envio
_msgs_painel_recentes: dict[tuple, float] = {}
_TTL_DEDUP = 30  # segundos

def _registrar_msg_painel(telefone: str, texto: str):
    """Registra mensagem enviada pelo painel para evitar duplicata do webhook fromMe."""
    import time
    chave = (telefone, texto.strip().lower()[:100])
    _msgs_painel_recentes[chave] = time.time()
    # Limpa entradas antigas
    agora = time.time()
    expiradas = [k for k, t in _msgs_painel_recentes.items() if agora - t > _TTL_DEDUP]
    for k in expiradas:
        del _msgs_painel_recentes[k]

def _e_duplicata_painel(telefone: str, texto: str) -> bool:
    """Retorna True se essa mensagem foi enviada recentemente pelo painel."""
    import time
    chave = (telefone, texto.strip().lower()[:100])
    ts = _msgs_painel_recentes.get(chave)
    if ts and time.time() - ts < _TTL_DEDUP:
        del _msgs_painel_recentes[chave]  # consome a entrada
        return True
    return False


# ─── Follow-up automático ───────────────────────────────────────────────────────

# Estados do bot onde o lead ainda não completou os dados para o consultor
_ESTADOS_BOT_ATIVO = [
    EstadoConversaEnum.inicio,
    EstadoConversaEnum.aguardando_nome,
    EstadoConversaEnum.aguardando_modalidade,
    EstadoConversaEnum.coletando_cidade,
    EstadoConversaEnum.coletando_cpf,
    EstadoConversaEnum.coletando_data_nasc,
    EstadoConversaEnum.coletando_carro,
]


def _dentro_horario_atendimento() -> bool:
    """Retorna True se estiver dentro do horário de funcionamento (horário de Brasília)."""
    from zoneinfo import ZoneInfo
    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    dia = agora.weekday()       # 0=seg … 6=dom
    hora_dec = agora.hour + agora.minute / 60
    if dia < 5 and 9 <= hora_dec < 18:   # segunda a sexta
        return True
    if dia == 5 and 9 <= hora_dec < 13:  # sábado
        return True
    return False


async def _enviar_followups():
    from models import SessionLocal
    db = SessionLocal()
    try:
        # ── 1. Só envia dentro do horário de atendimento ──────────────────────
        if not _dentro_horario_atendimento():
            print("⏰ Follow-up ignorado: fora do horário de atendimento")
            return

        # ── 2. Carrega configurações ──────────────────────────────────────────
        def _cfg(chave, padrao=""):
            c = db.query(Configuracao).filter(Configuracao.chave == chave).first()
            return c.valor if c else padrao

        horas = int(h) if (h := _cfg("followup_horas", "4")).isdigit() else 4
        limite_1 = datetime.utcnow() - timedelta(hours=horas)   # 1º: X h sem resposta

        msgs = {
            0: _cfg("mensagem_followup",
                    "Oi! 😊 Vi que nossa conversa ficou parada...\n"
                    "Quando quiser continuar, estou aqui! Gostaria de retomar?"),
            1: _cfg("mensagem_followup_2",
                    "Olá! 👋 Passando para saber se ainda tem interesse em financiar ou refinanciar seu veículo.\n"
                    "Estamos com ótimas condições! Ficou alguma dúvida?"),
            2: _cfg("mensagem_followup_3",
                    "Oi! Última tentativa de contato por aqui. 😊\n"
                    "Se mudar de ideia, pode nos chamar a qualquer momento! Ficamos à disposição. 🤝"),
        }

        # ── 3. Leads ainda no fluxo do bot ───────────────────────────────────
        leads = db.query(Lead).filter(
            Lead.estado_conversa.in_([e.value for e in _ESTADOS_BOT_ATIVO]),
            Lead.status.in_([
                StatusLeadEnum.em_atendimento.value,
                StatusLeadEnum.qualificado.value,
            ]),
        ).all()

        enviados = 0
        for lead in leads:
            try:
                # Pula leads manuais sem telefone real
                if lead.telefone.startswith("_manual_"):
                    continue

                # Última mensagem do usuário
                ultima_user = (
                    db.query(MensagemConversa)
                    .filter(
                        MensagemConversa.telefone == lead.telefone,
                        MensagemConversa.role == "user",
                    )
                    .order_by(MensagemConversa.id.desc())
                    .first()
                )

                if not ultima_user:
                    continue

                tentativa = lead.followup_tentativa or 0
                agora = datetime.utcnow()

                # Se o cliente respondeu após o último follow-up → zera a sequência
                if tentativa > 0 and lead.followup_em and ultima_user.criado_em > lead.followup_em:
                    lead.followup_tentativa = 0
                    db.commit()
                    tentativa = 0

                # ── Decide se é hora de disparar ─────────────────────────────
                if tentativa == 0:
                    # 1º: cliente ficou X horas sem responder
                    if ultima_user.criado_em > limite_1:
                        continue   # ainda recente
                elif tentativa == 1:
                    # 2º: 24h após o 1º sem resposta
                    if not lead.followup_em or (agora - lead.followup_em) < timedelta(hours=24):
                        continue
                elif tentativa == 2:
                    # 3º: 48h após o 2º sem resposta
                    if not lead.followup_em or (agora - lead.followup_em) < timedelta(hours=48):
                        continue
                else:
                    continue   # já esgotou as 3 tentativas

                # ── Monta e envia ─────────────────────────────────────────────
                nome = f" {lead.nome}" if lead.nome else ""
                texto_base = msgs[tentativa]
                texto = texto_base.replace("{nome}", nome.strip()).replace("Oi!", f"Oi{nome}!")

                await enviar_zapi(lead.telefone, texto)
                _salvar_msg_webhook(db, lead.telefone, texto, role="assistant")

                lead.followup_em = agora
                lead.followup_tentativa = tentativa + 1

                # 3º follow-up enviado → marca como Perdido automaticamente
                if tentativa == 2:
                    lead.status = StatusLeadEnum.perdido
                    print(f"🔴 Lead #{lead.id} marcado como Perdido após 3 follow-ups sem resposta")

                db.commit()
                enviados += 1
                print(f"📨 Follow-up #{tentativa + 1} enviado para {lead.telefone} (lead #{lead.id})")

            except Exception as e_lead:
                print(f"⚠️ Erro ao enviar follow-up para lead #{lead.id}: {e_lead}")
                db.rollback()

        if enviados:
            print(f"✅ Follow-ups enviados nesta rodada: {enviados}")
        else:
            print("ℹ️ Nenhum lead precisava de follow-up agora")

    except Exception as e:
        print(f"❌ Erro geral no follow-up: {e}")
    finally:
        db.close()


async def _loop_followup():
    """Roda a cada 30 minutos verificando leads parados."""
    await asyncio.sleep(60)  # aguarda 1 min após startup
    while True:
        try:
            print("🔍 Verificando leads para follow-up…")
            await _enviar_followups()
        except Exception as e:
            print(f"❌ Erro inesperado no loop de follow-up: {e}")
        await asyncio.sleep(30 * 60)  # a cada 30 minutos


async def _loop_ocultar_inativos():
    """Roda 1x por dia: oculta do funil leads sem atividade há 30+ dias."""
    await asyncio.sleep(120)  # aguarda 2 min após startup
    while True:
        try:
            from models import SessionLocal
            db = SessionLocal()
            limite = datetime.utcnow() - timedelta(days=30)
            inativos = db.query(Lead).filter(
                Lead.atualizado_em < limite,
                Lead.oculto_funil == False,
                Lead.status.notin_([
                    StatusLeadEnum.desqualificado.value,
                ]),
            ).all()
            total = 0
            for lead in inativos:
                lead.oculto_funil = True
                total += 1
            if total:
                db.commit()
                print(f"📦 {total} lead(s) ocultados do funil por inatividade (30 dias)")
            db.close()
        except Exception as e:
            print(f"❌ Erro ao ocultar leads inativos: {e}")
        await asyncio.sleep(24 * 60 * 60)  # 1x por dia


# ─── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    # Garante que o diretório do banco existe (volume /data ou local)
    db_url = settings.DATABASE_URL
    if "sqlite" in db_url:
        db_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
        db_dir = os.path.dirname(db_path)
        if db_dir:
            try:
                os.makedirs(db_dir, exist_ok=True)
                print(f"📁 Diretório do banco: {db_dir}")
            except Exception as e:
                print(f"⚠️ Não foi possível criar diretório {db_dir}: {e}")

    try:
        criar_tabelas()
    except Exception as e:
        print(f"⚠️ Erro ao criar tabelas: {e}")
        raise

    # Cria admin padrão se não existir nenhum usuário
    db_startup = next(get_db())
    db = db_startup
    try:
        if db.query(Usuario).count() == 0:
            admin = Usuario(
                nome=settings.ADMIN_NOME,
                email=settings.ADMIN_EMAIL,
                senha_hash=hash_senha(settings.ADMIN_PASSWORD),
                role=RoleEnum.admin,
                ativo=True,
            )
            db.add(admin)
            db.commit()
            print(f"✅ Admin criado: {settings.ADMIN_EMAIL} / {settings.ADMIN_PASSWORD}")
    finally:
        pass

    # Migrações de schema (colunas novas adicionadas após criação inicial)
    from sqlalchemy import text
    _migracoes = [
        ("leads",     "followup_em",           "DATETIME"),
        ("contratos", "dados_contrato",        "TEXT"),
        ("contratos", "selfie_path",           "VARCHAR(300)"),
        ("contratos", "assinatura_path",       "VARCHAR(300)"),
        ("contratos", "ip_cliente",            "VARCHAR(50)"),
        ("contratos", "geolocalizacao",        "VARCHAR(200)"),
        ("contratos", "pdf_assinado",          "VARCHAR(300)"),
        ("contratos", "assinado_em",           "DATETIME"),
        ("contratos", "doc_frente_req_path",   "VARCHAR(300)"),
        ("contratos", "doc_verso_req_path",    "VARCHAR(300)"),
        ("contratos", "token_prop",            "VARCHAR(64)"),
        ("contratos", "status_prop",           "VARCHAR(20)"),
        ("contratos", "selfie_prop_path",      "VARCHAR(300)"),
        ("contratos", "assinatura_prop_path",  "VARCHAR(300)"),
        ("contratos", "doc_frente_prop_path",  "VARCHAR(300)"),
        ("contratos", "doc_verso_prop_path",   "VARCHAR(300)"),
        ("contratos", "ip_prop",               "VARCHAR(50)"),
        ("contratos", "geo_prop",              "VARCHAR(200)"),
        ("contratos", "assinado_prop_em",      "DATETIME"),
        ("contratos", "codigo_req",            "VARCHAR(10)"),
        ("contratos", "codigo_req_expira",     "DATETIME"),
        ("contratos", "codigo_prop",           "VARCHAR(10)"),
        ("contratos", "codigo_prop_expira",    "DATETIME"),
        ("leads",     "origem",                "VARCHAR(50)"),
        ("leads",     "origem_detalhe",          "VARCHAR(100)"),
        ("leads",     "parceiro_id",             "INTEGER"),
        ("parceiros", "telefones_extras",         "TEXT"),
        ("sessoes_usuario", "tempo_ativo_s",      "INTEGER DEFAULT 0"),
        ("leads",           "followup_tentativa", "INTEGER DEFAULT 0"),
        ("leads",           "deal_data",          "VARCHAR(10)"),
        ("leads",           "deal_veiculo",       "VARCHAR(200)"),
        ("leads",           "deal_retorno",       "VARCHAR(5)"),
        ("leads",           "deal_valor",         "VARCHAR(20)"),
        ("leads",           "deal_comissao",      "VARCHAR(20)"),
        ("leads",           "deal_banco",         "VARCHAR(30)"),
        ("leads",           "deal_conta_pg",      "VARCHAR(50)"),
        ("leads",           "deal_operadora",     "VARCHAR(150)"),
        ("leads",           "dados_contrato",     "TEXT"),
        ("leads",           "cidade",             "VARCHAR(100)"),
        ("leads",           "renda",              "VARCHAR(30)"),
        ("leads",           "profissao",          "VARCHAR(100)"),
        ("leads",           "tem_cnh",            "BOOLEAN"),
        ("leads",           "oculto_funil",       "BOOLEAN DEFAULT 0"),
        ("parceiros",       "nome_agenda",        "VARCHAR(200)"),
        ("parceiros",       "operadora_id",       "INTEGER"),
    ]
    for tabela, coluna, tipo in _migracoes:
        try:
            with db_startup.bind.connect() as conn:
                conn.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}"))
                conn.commit()
            print(f"✅ Migração: {tabela}.{coluna} adicionada")
        except Exception:
            pass  # coluna já existe — ignorar

    # Configurações padrão do bot
    _criar_config_padrao(db_startup)
    db_startup.close()

    # Inicia tarefa de follow-up automático
    asyncio.create_task(_loop_followup())
    asyncio.create_task(_loop_ocultar_inativos())

    print("✅ Fácil Financiamentos Bot v2 iniciado!")
    print(f"🗄️  Banco: {settings.DATABASE_URL}")
    print("📊 Dashboard: http://localhost:8000/dashboard")


# ─── Autenticação ────────────────────────────────────────────────────────────────

async def _geo_por_ip(ip: str) -> str:
    """Retorna 'Cidade, Estado, País' via ip-api.com (grátis, sem chave)."""
    if not ip or ip in ("127.0.0.1", "::1", "testclient"):
        return "Local"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,city,regionName,country"},
            )
            d = r.json()
            if d.get("status") == "success":
                partes = [d.get("city",""), d.get("regionName",""), d.get("country","")]
                return ", ".join(p for p in partes if p)
    except Exception:
        pass
    return ip


def _ip_da_requisicao(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "desconhecido"


@app.post("/auth/login")
async def login(request: Request, response: Response, db: Session = Depends(get_db)):
    body = await request.json()
    email = body.get("email", "").strip().lower()
    senha = body.get("senha", "")

    usuario = db.query(Usuario).filter(Usuario.email == email).first()
    if not usuario or not verificar_senha(senha, usuario.senha_hash):
        raise HTTPException(status_code=401, detail="E-mail ou senha incorretos")
    if not usuario.ativo:
        raise HTTPException(status_code=403, detail="Usuário desativado")

    token = criar_token({"sub": str(usuario.id)})
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
    )

    # ── Registrar sessão ──────────────────────────────────────────────────────
    ip = _ip_da_requisicao(request)
    geo = await _geo_por_ip(ip)
    sessao = SessaoUsuario(usuario_id=usuario.id, ip=ip, localizacao=geo)
    db.add(sessao)
    db.commit()
    db.refresh(sessao)
    response.set_cookie(
        key="sessao_id",
        value=str(sessao.id),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
    )

    return {"id": usuario.id, "nome": usuario.nome, "email": usuario.email, "role": usuario.role}


@app.post("/auth/logout")
async def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    # Fecha sessão aberta
    sid = request.cookies.get("sessao_id")
    if sid:
        try:
            sessao = db.query(SessaoUsuario).filter(SessaoUsuario.id == int(sid)).first()
            if sessao and not sessao.logout_em:
                sessao.logout_em = datetime.utcnow()
                db.commit()
        except Exception:
            pass
    response.delete_cookie("access_token")
    response.delete_cookie("sessao_id")
    return {"status": "ok"}


@app.post("/api/heartbeat")
async def heartbeat(request: Request, db: Session = Depends(get_db),
                    usuario: Usuario = Depends(obter_usuario_atual)):
    """Atualiza último momento ativo. Se ativo=true, incrementa tempo_ativo_s."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    realmente_ativo = body.get("ativo", False)

    sid = request.cookies.get("sessao_id")
    if sid:
        try:
            sessao = db.query(SessaoUsuario).filter(SessaoUsuario.id == int(sid)).first()
            if sessao:
                sessao.ultimo_ativo_em = datetime.utcnow()
                if realmente_ativo:
                    sessao.tempo_ativo_s = (sessao.tempo_ativo_s or 0) + 60
                db.commit()
        except Exception:
            pass
    return {"status": "ok"}


@app.get("/auth/me")
async def me(usuario: Usuario = Depends(obter_usuario_atual)):
    return {"id": usuario.id, "nome": usuario.nome, "email": usuario.email, "role": usuario.role}



# ─── WhatsApp: envio ─────────────────────────────────────────────────────────────

async def enviar_zapi(telefone: str, mensagem: str):
    if not settings.ZAPI_INSTANCE or not settings.ZAPI_TOKEN:
        print(f"[Z-API SIMULADO] {telefone}: {mensagem}")
        return
    url = f"https://api.z-api.io/instances/{settings.ZAPI_INSTANCE}/token/{settings.ZAPI_TOKEN}/send-text"
    headers = {"Client-Token": settings.ZAPI_CLIENT_TOKEN}
    payload = {"phone": telefone, "message": mensagem}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"⚠️ Erro Z-API: {resp.text}")


async def enviar_meta(telefone: str, mensagem: str):
    if not settings.WHATSAPP_TOKEN or not settings.WHATSAPP_PHONE_ID:
        print(f"[META SIMULADO] {telefone}: {mensagem}")
        return
    url = f"https://graph.facebook.com/v19.0/{settings.WHATSAPP_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": telefone, "type": "text", "text": {"body": mensagem}}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"⚠️ Erro Meta: {resp.text}")


# ─── Webhooks ────────────────────────────────────────────────────────────────────

def _montar_msg_recontato(lead) -> list[str]:
    """Monta mensagem de boas-vindas personalizada para lead que voltou após ser marcado como perdido."""
    nome = lead.nome or ""
    saudacao = f"Olá{' ' + nome if nome else ''}! Que bom ter você de volta! 😊"

    # Resumo dos dados já coletados
    linhas = []
    mod_map = {"financiamento": "Financiamento", "refinanciamento": "Refinanciamento"}
    if lead.modalidade and lead.modalidade != "indefinido":
        linhas.append(f"📋 Modalidade: {mod_map.get(lead.modalidade, lead.modalidade)}")
    if lead.carro_interesse:
        linhas.append(f"🚗 Veículo de interesse: {lead.carro_interesse}")

    if linhas:
        resumo = "Encontrei seus dados cadastrados aqui:\n" + "\n".join(linhas)
        msg1 = f"{saudacao}\n\n{resumo}"
    else:
        msg1 = f"{saudacao}\nSeus dados estão registrados no nosso sistema."

    msg2 = "Em breve uma de nossas consultoras entrará em contato. Tem alguma informação que mudou desde nossa última conversa? 😊"
    return [msg1, msg2]


async def _reativar_lead_perdido(lead, texto: str, db, enviar_fn) -> bool:
    """
    Trata lead 'perdido' que voltou a enviar mensagem.
    - Com dados: reativa como qualificado, manda boas-vindas personalizadas.
    - Sem dados: reinicia o fluxo do bot do zero.
    Retorna True se tratou como recontato (chamador não precisa fazer mais nada).
    """
    _salvar_msg_webhook(db, lead.telefone, texto, role="user")

    if lead.nome:
        # ── Tem dados: boas-vindas + passa para equipe ─────────────────────
        msgs = _montar_msg_recontato(lead)
        lead.status = StatusLeadEnum.qualificado
        lead.estado_conversa = EstadoConversaEnum.finalizado
        lead.atribuido_para = None      # libera para qualquer atendente assumir
        lead.assumido_em = None
        lead.followup_em = None
        lead.atualizado_em = datetime.utcnow()
        db.commit()
        for i, msg in enumerate(msgs):
            if i > 0:
                await asyncio.sleep(0.8)
            await enviar_fn(lead.telefone, msg)
            _salvar_msg_webhook(db, lead.telefone, msg, role="assistant")
        print(f"🔄 Lead #{lead.id} ({lead.nome}) reativado — tinha dados, voltou como qualificado")
        return True
    else:
        # ── Sem dados: reinicia o bot do zero ─────────────────────────────
        lead.status = StatusLeadEnum.em_atendimento
        lead.estado_conversa = EstadoConversaEnum.inicio
        lead.followup_em = None
        lead.atualizado_em = datetime.utcnow()
        db.commit()
        print(f"🔄 Lead #{lead.id} reativado — sem dados, reiniciando fluxo")
        return False   # deixa o fluxo normal do bot processar


def _extrair_texto_zapi(body: dict) -> str:
    """Extrai o texto de mensagem de qualquer tipo de payload Z-API."""
    return (
        (body.get("text") or {}).get("message", "")
        or (body.get("extendedTextMessage") or {}).get("text", "")
        or (body.get("listResponseMessage") or {}).get("title", "")
        or (body.get("buttonsResponseMessage") or {}).get("selectedDisplayText", "")
        or body.get("body", "")
        or ""
    ).strip()


def _buscar_parceiro_por_telefone(telefone: str, db) -> "Parceiro | None":
    """Retorna o Parceiro ativo cujo telefone principal ou extra bate com o número."""
    from models import Parceiro as _Parceiro
    p = db.query(_Parceiro).filter(_Parceiro.telefone == telefone, _Parceiro.ativo == True).first()
    if p:
        return p
    todos = db.query(_Parceiro).filter(_Parceiro.ativo == True, _Parceiro.telefones_extras != None).all()
    for p in todos:
        try:
            extras = json.loads(p.telefones_extras or "[]")
            if any(str(e).replace("+", "").replace(" ", "") == telefone for e in extras):
                return p
        except Exception:
            pass
    return None


def _extrair_audio_url_zapi(body: dict) -> str | None:
    """Extrai a URL do áudio de um webhook Z-API, se houver."""
    audio = body.get("audio") or {}
    if audio.get("audioUrl"):
        return audio["audioUrl"]
    ptt = body.get("ptt") or {}
    if ptt.get("audioUrl"):
        return ptt["audioUrl"]
    return None


def _extrair_imagem_zapi(body: dict) -> dict | None:
    """Extrai URL e legenda de imagem do webhook Z-API."""
    img = body.get("image") or {}
    if img.get("imageUrl"):
        return {"url": img["imageUrl"], "caption": img.get("caption", "")}
    return None


def _extrair_documento_zapi(body: dict) -> dict | None:
    """Extrai URL, nome e mime de documento/PDF do webhook Z-API."""
    doc = body.get("document") or {}
    if doc.get("documentUrl"):
        return {
            "url": doc["documentUrl"],
            "nome": doc.get("fileName", "documento"),
            "mime": doc.get("mimeType", "application/octet-stream"),
        }
    return None


async def _salvar_imagem(url: str) -> str | None:
    """Baixa imagem, salva em /app/imagens/ e retorna marcador [IMAGE:filename]."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return None
        ct = resp.headers.get("content-type", "image/jpeg")
        if "png" in ct:
            ext = "png"
        elif "webp" in ct:
            ext = "webp"
        elif "gif" in ct:
            ext = "gif"
        else:
            ext = "jpg"
        img_id = uuid.uuid4().hex
        filename = f"{img_id}.{ext}"
        os.makedirs("/app/imagens", exist_ok=True)
        with open(f"/app/imagens/{filename}", "wb") as f:
            f.write(resp.content)
        return f"[IMAGE:{filename}]"
    except Exception as e:
        print(f"⚠️ Erro ao salvar imagem: {e}")
        return None


async def _salvar_documento(url: str, nome_original: str) -> str | None:
    """Baixa documento/PDF, salva em /app/documentos/ e retorna marcador [DOC:filename|nome]."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return None
        ct = resp.headers.get("content-type", "")
        nome_lower = nome_original.lower()
        if "pdf" in ct or nome_lower.endswith(".pdf"):
            ext = "pdf"
        elif "." in nome_original:
            ext = nome_original.rsplit(".", 1)[-1][:5]
        else:
            ext = "bin"
        doc_id = uuid.uuid4().hex
        filename = f"{doc_id}.{ext}"
        nome_display = re.sub(r"[|]", "_", nome_original)[:100] if nome_original else f"documento.{ext}"
        os.makedirs("/app/documentos", exist_ok=True)
        with open(f"/app/documentos/{filename}", "wb") as f:
            f.write(resp.content)
        return f"[DOC:{filename}|{nome_display}]"
    except Exception as e:
        print(f"⚠️ Erro ao salvar documento: {e}")
        return None


async def _salvar_audio_cliente(telefone: str, audio_url: str, db) -> str | None:
    """Baixa o áudio do cliente, salva em /app/audios/ e retorna o conteúdo [AUDIO:filename]."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(audio_url)
        if resp.status_code != 200:
            print(f"⚠️ Não foi possível baixar áudio do cliente: {resp.status_code}")
            return None
        # Detecta extensão pelo content-type
        ct = resp.headers.get("content-type", "audio/ogg")
        ext = "ogg" if "ogg" in ct else ("mp3" if "mp3" in ct or "mpeg" in ct else "webm")
        audio_id = uuid.uuid4().hex
        filename = f"{audio_id}.{ext}"
        os.makedirs("/app/audios", exist_ok=True)
        with open(f"/app/audios/{filename}", "wb") as f:
            f.write(resp.content)
        return f"[AUDIO:{filename}]"
    except Exception as e:
        print(f"⚠️ Erro ao salvar áudio do cliente: {e}")
        return None


@app.post("/webhook/zapi")
async def receber_webhook_zapi(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    try:
        telefone = body.get("phone", "").replace("+", "").replace(" ", "")
        if not telefone:
            return JSONResponse({"status": "ignored"})

        # ── Mensagem editada pelo cliente ou pelo atendente ──────────────────
        if body.get("isEdit") or body.get("editedMessage"):
            print(f"📝 EDIT webhook recebido: {json.dumps(body, ensure_ascii=False)[:500]}")
            edited = body.get("editedMessage") or {}
            # Z-API aninha o conteúdo em editedMessage.message.*
            edited_msg = edited.get("message") or {}
            texto_editado = (
                # estrutura aninhada: editedMessage.message.conversation
                edited_msg.get("conversation", "")
                or (edited_msg.get("extendedTextMessage") or {}).get("text", "")
                # estrutura plana dentro de editedMessage
                or (edited.get("text") or {}).get("message", "")
                or (edited.get("extendedTextMessage") or {}).get("text", "")
                or edited.get("conversation", "")
                # fallback: texto direto no body (alguns formatos Z-API)
                or _extrair_texto_zapi(body)
            ).strip()
            print(f"📝 EDIT texto extraído: '{texto_editado}'")
            if telefone and texto_editado:
                lead = db.query(Lead).filter(Lead.telefone == telefone).first()
                if lead:
                    role = "assistant" if body.get("fromMe") else "user"
                    _salvar_msg_webhook(db, telefone, f"✏️ {texto_editado}", role=role)
                    print(f"📝 EDIT salvo para {telefone} ({role})")
            return JSONResponse({"status": "edit_saved"})

        # ── Mensagem enviada do próprio aparelho pelo atendente ──────────────
        if body.get("fromMe"):
            texto = _extrair_texto_zapi(body)
            if texto:
                # Ignora eco de mensagem já enviada pelo painel (evita texto duplicado/errado)
                if _e_duplicata_painel(telefone, texto):
                    return JSONResponse({"status": "fromme_ignored_duplicate"})
                lead = db.query(Lead).filter(Lead.telefone == telefone).first()
                if lead:
                    _salvar_msg_webhook(db, telefone, texto, role="assistant")
            else:
                # Imagem ou documento enviado do celular pelo atendente
                lead = db.query(Lead).filter(Lead.telefone == telefone).first()
                if lead:
                    img_info = _extrair_imagem_zapi(body)
                    if img_info:
                        conteudo = await _salvar_imagem(img_info["url"])
                        if conteudo:
                            _salvar_msg_webhook(db, telefone, conteudo, role="assistant")
                    else:
                        doc_info = _extrair_documento_zapi(body)
                        if doc_info:
                            conteudo = await _salvar_documento(doc_info["url"], doc_info["nome"])
                            if conteudo:
                                _salvar_msg_webhook(db, telefone, conteudo, role="assistant")
            return JSONResponse({"status": "fromme_saved"})

        # ── Mensagem recebida do cliente ─────────────────────────────────────
        texto = _extrair_texto_zapi(body)
        if not texto:
            lead_midia = db.query(Lead).filter(Lead.telefone == telefone).first()

            # Verifica áudio do cliente
            audio_url = _extrair_audio_url_zapi(body)
            if audio_url and lead_midia:
                conteudo_audio = await _salvar_audio_cliente(telefone, audio_url, db)
                if conteudo_audio:
                    _salvar_msg_webhook(db, telefone, conteudo_audio, role="user")
                    lead_midia.atualizado_em = datetime.utcnow()
                    if lead_midia.oculto_funil:
                        lead_midia.oculto_funil = False
                    db.commit()
                    return JSONResponse({"status": "audio_salvo"})

            # Verifica imagem do cliente
            img_info = _extrair_imagem_zapi(body)
            if img_info and lead_midia:
                conteudo_img = await _salvar_imagem(img_info["url"])
                if conteudo_img:
                    # Se tem legenda, salva junto
                    if img_info.get("caption"):
                        conteudo_img += f"\n{img_info['caption']}"
                    _salvar_msg_webhook(db, telefone, conteudo_img, role="user")
                    lead_midia.atualizado_em = datetime.utcnow()
                    if lead_midia.oculto_funil:
                        lead_midia.oculto_funil = False
                    db.commit()
                    return JSONResponse({"status": "imagem_salva"})

            # Verifica documento/PDF do cliente
            doc_info = _extrair_documento_zapi(body)
            if doc_info and lead_midia:
                conteudo_doc = await _salvar_documento(doc_info["url"], doc_info["nome"])
                if conteudo_doc:
                    _salvar_msg_webhook(db, telefone, conteudo_doc, role="user")
                    lead_midia.atualizado_em = datetime.utcnow()
                    if lead_midia.oculto_funil:
                        lead_midia.oculto_funil = False
                    db.commit()
                    return JSONResponse({"status": "documento_salvo"})

            return JSONResponse({"status": "ignored"})

        # ── Verifica se é número de parceiro ─────────────────────────────────
        parceiro = _buscar_parceiro_por_telefone(telefone, db)
        if parceiro:
            lead = db.query(Lead).filter(Lead.telefone == telefone).first()
            ja_em_atendimento = lead and lead.status in [
                StatusLeadEnum.assumido,
                StatusLeadEnum.proposta_enviada,
                StatusLeadEnum.proposta_aprovada,
                StatusLeadEnum.fechado,
                StatusLeadEnum.qualificado,
            ]
            if ja_em_atendimento:
                # Parceiro já transferido — salva mensagem e avisa fora do horário
                _salvar_msg_webhook(db, telefone, texto, role="user")
                if lead.oculto_funil:
                    lead.oculto_funil = False
                    db.commit()
                prox = _proximo_horario_atendimento()
                if prox:
                    primeiro_nome = parceiro.nome.split()[0]
                    aviso = (
                        f"Olá {primeiro_nome}! 😊 No momento estamos fora do horário de atendimento. "
                        f"Funcionamos seg–sex das 09h às 18h e sábado das 09h às 13h. "
                        f"Retornaremos seu contato no primeiro horário disponível! 🕘"
                    )
                    await enviar_zapi(telefone, aviso)
                    _salvar_msg_webhook(db, telefone, aviso, role="assistant")
                return JSONResponse({"status": "parceiro_aguardando_humano"})

            # Primeiro contato do parceiro → boas-vindas e transferência direta
            primeiro_nome = parceiro.nome.split()[0]
            msg_boas_vindas = f"Olá {primeiro_nome}! Um momento, já vou te conectar com nossa equipe. 😊"

            if not lead:
                lead = Lead(telefone=telefone, nome=parceiro.nome, parceiro_id=parceiro.id)
                db.add(lead)
                db.commit()
                db.refresh(lead)

            _salvar_msg_webhook(db, telefone, texto, role="user")
            lead.estado_conversa = EstadoConversaEnum.transferido
            lead.status = StatusLeadEnum.qualificado
            lead.parceiro_id = parceiro.id
            if not lead.nome:
                lead.nome = parceiro.nome
            lead.atualizado_em = datetime.utcnow()
            lead.oculto_funil = False
            db.commit()

            await enviar_zapi(telefone, msg_boas_vindas)
            _salvar_msg_webhook(db, telefone, msg_boas_vindas, role="assistant")

            prox = _proximo_horario_atendimento()
            if prox:
                aviso = (
                    "No momento estamos fora do horário de atendimento. "
                    "Funcionamos seg–sex das 09h às 18h e sábado das 09h às 13h. "
                    "Retornaremos seu contato no primeiro horário disponível! 🕘"
                )
                await enviar_zapi(telefone, aviso)
                _salvar_msg_webhook(db, telefone, aviso, role="assistant")

            await _notificar_equipe(telefone, db)
            return JSONResponse({"status": "parceiro_transferido"})

        lead = db.query(Lead).filter(Lead.telefone == telefone).first()

        # Lead perdido voltou a entrar em contato → reativa inteligentemente
        if lead and lead.status == StatusLeadEnum.perdido:
            tratado = await _reativar_lead_perdido(lead, texto, db, enviar_zapi)
            if tratado:
                await _notificar_equipe(telefone, db)
                return JSONResponse({"status": "reativado"})
            # tratado=False → reiniciou do zero, cai no fluxo normal abaixo

        # Se está sendo atendido por humano, salva a mensagem e avisa se fora do horário
        if lead and lead.status in [
            StatusLeadEnum.assumido,
            StatusLeadEnum.proposta_enviada,
            StatusLeadEnum.proposta_aprovada,
            StatusLeadEnum.fechado,
        ]:
            # Cliente voltou → reexibe no funil se estava oculto
            if lead.oculto_funil:
                lead.oculto_funil = False
                db.commit()
            _salvar_msg_webhook(db, telefone, texto)
            prox = _proximo_horario_atendimento()
            if prox:
                nome = f" {lead.nome}" if lead.nome else ""
                aviso = (
                    f"Olá{nome}! 😊 No momento estamos fora do horário de atendimento. "
                    f"Funcionamos seg–sex das 09h às 18h e sábado das 09h às 13h. "
                    f"Retornaremos seu contato no primeiro horário disponível! 🕘"
                )
                await enviar_zapi(telefone, aviso)
                _salvar_msg_webhook(db, telefone, aviso, role="assistant")
            return JSONResponse({"status": "aguardando_humano"})

        # Bot qualificando
        respostas = processar_mensagem(telefone, texto, db)
        for i, msg in enumerate(respostas):
            if i > 0:
                await asyncio.sleep(0.8)
            await enviar_zapi(telefone, msg)

        lead = db.query(Lead).filter(Lead.telefone == telefone).first()
        if lead and lead.status == StatusLeadEnum.qualificado:
            await _notificar_equipe(telefone, db)

    except Exception as e:
        print(f"⚠️ Erro webhook Z-API: {e}")
    return JSONResponse({"status": "ok"})


@app.get("/webhook/meta")
async def verificar_webhook_meta(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge)
    raise HTTPException(status_code=403, detail="Token inválido")


@app.post("/webhook/meta")
async def receber_webhook_meta(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    try:
        entry = body["entry"][0]["changes"][0]["value"]
        for msg in entry.get("messages", []):
            if msg.get("type") != "text":
                continue
            telefone = msg["from"]
            texto = msg["text"]["body"].strip()
            if not texto:
                continue

            lead = db.query(Lead).filter(Lead.telefone == telefone).first()

            # Lead perdido voltou → reativa inteligentemente
            if lead and lead.status == StatusLeadEnum.perdido:
                tratado = await _reativar_lead_perdido(lead, texto, db, enviar_meta)
                if tratado:
                    await _notificar_equipe(telefone, db)
                    continue
                # tratado=False → reiniciou do zero, cai no fluxo normal

            # Humano atendendo → salva e avisa se fora do horário
            if lead and lead.status in [
                StatusLeadEnum.assumido,
                StatusLeadEnum.proposta_enviada,
                StatusLeadEnum.proposta_aprovada,
                StatusLeadEnum.fechado,
            ]:
                _salvar_msg_webhook(db, telefone, texto)
                prox = _proximo_horario_atendimento()
                if prox:
                    nome = f" {lead.nome}" if lead.nome else ""
                    aviso = (
                        f"Olá{nome}! 😊 No momento estamos fora do horário de atendimento. "
                        f"Funcionamos seg–sex das 09h às 18h e sábado das 09h às 13h. "
                        f"Retornaremos seu contato {prox}! 🕘"
                    )
                    await enviar_meta(telefone, aviso)
                    _salvar_msg_webhook(db, telefone, aviso, role="assistant")
                continue

            respostas = processar_mensagem(telefone, texto, db)
            for i, msg in enumerate(respostas):
                if i > 0:
                    await asyncio.sleep(0.8)
                await enviar_meta(telefone, msg)
            lead = db.query(Lead).filter(Lead.telefone == telefone).first()
            if lead and lead.status == StatusLeadEnum.qualificado:
                await _notificar_equipe(telefone, db)
    except (KeyError, IndexError):
        pass
    return JSONResponse({"status": "ok"})


# ─── Teste ───────────────────────────────────────────────────────────────────────

@app.post("/testar")
async def testar_bot(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    telefone = body.get("telefone", "5500000000000")
    mensagem = body.get("mensagem", "")
    if not mensagem:
        raise HTTPException(status_code=400, detail="Campo 'mensagem' obrigatório")
    respostas = processar_mensagem(telefone, mensagem, db)
    return JSONResponse({"respostas": respostas, "telefone": telefone})


# ─── API Parceiros ───────────────────────────────────────────────────────────────

def _serial_parceiro(p: Parceiro) -> dict:
    try:
        extras = json.loads(p.telefones_extras or "[]")
        if not isinstance(extras, list):
            extras = []
    except Exception:
        extras = []
    return {
        "id": p.id,
        "nome": p.nome,
        "data_nascimento": p.data_nascimento or "",
        "cpf": p.cpf or "",
        "telefone": p.telefone,
        "telefones_extras": extras,
        "email": p.email or "",
        "observacoes": p.observacoes or "",
        "nome_agenda":    p.nome_agenda or "",
        "operadora_id":   p.operadora_id,
        "operadora_nome": p.operadora.nome if p.operadora else "",
        "ativo": p.ativo,
        "criado_em": _fmt_br(p.criado_em, "%d/%m/%Y") or "",
        "contatos": [
            {"id": c.id, "nome": c.nome, "telefone": c.telefone or "",
             "email": c.email or "", "cargo": c.cargo or ""}
            for c in p.contatos
        ],
    }


@app.get("/api/parceiros")
async def listar_parceiros(
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    parceiros = db.query(Parceiro).filter(Parceiro.ativo == True)\
                  .order_by(Parceiro.nome).all()
    return [_serial_parceiro(p) for p in parceiros]


@app.post("/api/parceiros")
async def criar_parceiro(
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    body = await request.json()
    nome     = body.get("nome", "").strip()
    telefone = body.get("telefone", "").strip()
    cpf      = body.get("cpf", "").strip() or None

    if not nome:
        raise HTTPException(status_code=400, detail="O nome do parceiro é obrigatório")
    if not telefone:
        raise HTTPException(status_code=400, detail="O telefone é obrigatório")

    # Verificar duplicidade por CPF
    if cpf and db.query(Parceiro).filter(Parceiro.cpf == cpf).first():
        raise HTTPException(status_code=400, detail="Já existe um parceiro com este CPF")
    # Verificar duplicidade por telefone
    tel_norm = "".join(c for c in telefone if c.isdigit())
    existente = db.query(Parceiro).filter(Parceiro.telefone == tel_norm).first()
    if existente:
        raise HTTPException(status_code=400, detail=f"Já existe um parceiro com este telefone: {existente.nome}")

    extras = [t.strip() for t in body.get("telefones_extras", []) if t.strip()]
    operadora_id = body.get("operadora_id") or None
    if operadora_id:
        operadora_id = int(operadora_id)
    p = Parceiro(
        nome=nome,
        data_nascimento=body.get("data_nascimento", "").strip() or None,
        cpf=cpf,
        telefone=tel_norm or telefone,
        telefones_extras=json.dumps(extras, ensure_ascii=False) if extras else None,
        email=body.get("email", "").strip() or None,
        observacoes=body.get("observacoes", "").strip() or None,
        nome_agenda=body.get("nome_agenda", "").strip() or None,
        operadora_id=operadora_id,
    )
    db.add(p)
    db.flush()

    # Contatos adicionais
    for c in body.get("contatos", []):
        nome_c = c.get("nome", "").strip()
        if nome_c:
            db.add(ContatoParceiro(
                parceiro_id=p.id,
                nome=nome_c,
                telefone=c.get("telefone", "").strip() or None,
                email=c.get("email", "").strip() or None,
                cargo=c.get("cargo", "").strip() or None,
            ))

    db.commit()
    db.refresh(p)
    return _serial_parceiro(p)


@app.put("/api/parceiros/{pid}")
async def atualizar_parceiro(
    pid: int,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    p = db.query(Parceiro).filter(Parceiro.id == pid).first()
    if not p:
        raise HTTPException(status_code=404, detail="Parceiro não encontrado")

    body = await request.json()
    nome     = body.get("nome", "").strip()
    telefone = body.get("telefone", "").strip()
    cpf      = body.get("cpf", "").strip() or None

    if not nome:
        raise HTTPException(status_code=400, detail="O nome é obrigatório")
    if not telefone:
        raise HTTPException(status_code=400, detail="O telefone é obrigatório")

    # Duplicidade CPF (outro parceiro)
    if cpf and cpf != p.cpf:
        if db.query(Parceiro).filter(Parceiro.cpf == cpf, Parceiro.id != pid).first():
            raise HTTPException(status_code=400, detail="Já existe um parceiro com este CPF")

    tel_norm = "".join(c for c in telefone if c.isdigit()) or telefone
    if tel_norm != p.telefone:
        if db.query(Parceiro).filter(Parceiro.telefone == tel_norm, Parceiro.id != pid).first():
            raise HTTPException(status_code=400, detail="Já existe um parceiro com este telefone")

    extras = [t.strip() for t in body.get("telefones_extras", []) if t.strip()]
    op_id = body.get("operadora_id") or None
    p.nome             = nome
    p.data_nascimento  = body.get("data_nascimento", "").strip() or None
    p.cpf              = cpf
    p.telefone         = tel_norm
    p.telefones_extras = json.dumps(extras, ensure_ascii=False) if extras else None
    p.email            = body.get("email", "").strip() or None
    p.observacoes      = body.get("observacoes", "").strip() or None
    p.nome_agenda      = body.get("nome_agenda", "").strip() or None
    # Só admin pode alterar a operadora responsável
    if usuario.role == RoleEnum.admin:
        p.operadora_id = int(op_id) if op_id else None

    db.commit()
    db.refresh(p)
    return _serial_parceiro(p)


@app.delete("/api/parceiros/{pid}")
async def desativar_parceiro(
    pid: int,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(requer_admin),
):
    p = db.query(Parceiro).filter(Parceiro.id == pid).first()
    if not p:
        raise HTTPException(status_code=404, detail="Parceiro não encontrado")
    p.ativo = False
    db.commit()
    return {"status": "ok"}


@app.post("/api/parceiros/{pid}/contatos")
async def adicionar_contato(
    pid: int,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    p = db.query(Parceiro).filter(Parceiro.id == pid).first()
    if not p:
        raise HTTPException(status_code=404, detail="Parceiro não encontrado")
    body = await request.json()
    nome_c = body.get("nome", "").strip()
    if not nome_c:
        raise HTTPException(status_code=400, detail="Nome do contato é obrigatório")
    c = ContatoParceiro(
        parceiro_id=pid,
        nome=nome_c,
        telefone=body.get("telefone", "").strip() or None,
        email=body.get("email", "").strip() or None,
        cargo=body.get("cargo", "").strip() or None,
    )
    db.add(c)
    db.commit()
    db.refresh(p)
    return _serial_parceiro(p)


@app.delete("/api/parceiros/{pid}/contatos/{cid}")
async def remover_contato(
    pid: int,
    cid: int,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    c = db.query(ContatoParceiro).filter(
        ContatoParceiro.id == cid,
        ContatoParceiro.parceiro_id == pid,
    ).first()
    if not c:
        raise HTTPException(status_code=404, detail="Contato não encontrado")
    db.delete(c)
    db.commit()
    return {"status": "ok"}


# ─── API Leads ───────────────────────────────────────────────────────────────────

@app.post("/api/leads")
async def criar_lead_manual(
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    """Cria um lead manualmente (sem passar pelo bot do WhatsApp)."""
    import time
    body = await request.json()

    nome            = body.get("nome", "").strip()
    telefone        = body.get("telefone", "").strip()
    origem          = body.get("origem", "whatsapp").strip() or "whatsapp"
    origem_detalhe  = body.get("origem_detalhe", "").strip() or None
    parceiro_id     = body.get("parceiro_id") or None
    modalidade      = body.get("modalidade", ModalidadeEnum.indefinido)
    obs             = body.get("observacao", "").strip()
    atrib_id        = body.get("atribuido_para")

    if not nome:
        raise HTTPException(status_code=400, detail="O nome do lead é obrigatório")

    # Telefone é único — se não informado, gera placeholder
    if not telefone:
        telefone = f"_manual_{int(time.time() * 1000)}"
    else:
        telefone_norm = "".join(c for c in telefone if c.isdigit() or c == "+")
        if not telefone_norm:
            telefone_norm = telefone
        telefone = telefone_norm
        if db.query(Lead).filter(Lead.telefone == telefone).first():
            raise HTTPException(status_code=400, detail="Já existe um lead com este número de telefone")

    # Responsável
    responsavel = None
    if atrib_id:
        responsavel = db.query(Usuario).filter(Usuario.id == atrib_id, Usuario.ativo == True).first()

    # Parceiro
    if parceiro_id:
        p = db.query(Parceiro).filter(Parceiro.id == parceiro_id, Parceiro.ativo == True).first()
        if not p:
            parceiro_id = None

    lead = Lead(
        nome=nome,
        telefone=telefone,
        origem=origem,
        origem_detalhe=origem_detalhe,
        parceiro_id=int(parceiro_id) if parceiro_id else None,
        modalidade=modalidade,
        status=StatusLeadEnum.assumido,
        estado_conversa=EstadoConversaEnum.transferido,
        atribuido_para=responsavel.id if responsavel else usuario.id,
        assumido_em=datetime.utcnow(),
    )
    db.add(lead)
    db.flush()

    if obs:
        lista = []
        try:
            lista = json.loads(lead.observacoes or "[]")
        except Exception:
            pass
        lista.append({
            "texto": obs,
            "usuario": usuario.nome,
            "em": _agora_br().strftime("%d/%m/%Y %H:%M"),
        })
        lead.observacoes = json.dumps(lista, ensure_ascii=False)

    db.commit()
    db.refresh(lead)
    return _serial_lead(lead, db)


@app.get("/api/leads")
async def listar_leads(
    status: str = None,
    modalidade: str = None,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    query = db.query(Lead)
    if status:
        query = query.filter(Lead.status == status)
    if modalidade:
        query = query.filter(Lead.modalidade == modalidade)
    leads = query.order_by(Lead.criado_em.desc()).all()
    return [_serial_lead(l, db) for l in leads]


@app.get("/api/leads/{lead_id}/conversa")
async def obter_conversa(
    lead_id: int,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    msgs = db.query(MensagemConversa).filter(
        MensagemConversa.telefone == lead.telefone
    ).order_by(MensagemConversa.id).all()
    return {
        "lead": _serial_lead(lead, db),
        "mensagens": [
            {
                "role": m.role,
                "conteudo": m.conteudo,
                "horario": _fmt_br(m.criado_em) or "",
            }
            for m in msgs
        ],
    }


@app.post("/api/leads/{lead_id}/assumir")
async def assumir_lead(
    lead_id: int,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    if lead.status not in [StatusLeadEnum.qualificado, StatusLeadEnum.assumido]:
        raise HTTPException(status_code=400, detail="Lead não pode ser assumido neste status")
    lead.atribuido_para = usuario.id
    lead.status = StatusLeadEnum.assumido
    lead.assumido_em = datetime.utcnow()
    db.commit()
    db.refresh(lead)
    return _serial_lead(lead, db)


@app.post("/api/leads/{lead_id}/mover")
async def mover_lead(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    """Move um lead para outro estágio do funil."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    body = await request.json()
    novo_status = body.get("status", "")

    estagios_validos = [
        StatusLeadEnum.em_atendimento,
        StatusLeadEnum.qualificado,
        StatusLeadEnum.assumido,
        StatusLeadEnum.proposta_enviada,
        StatusLeadEnum.proposta_aprovada,
        StatusLeadEnum.fechado,
        StatusLeadEnum.perdido,
    ]
    if novo_status not in [s.value for s in estagios_validos]:
        raise HTTPException(status_code=400, detail="Estágio inválido")

    # Apenas administradores podem desqualificar ou devolver para Atendimento IA
    if usuario.role != RoleEnum.admin and novo_status in (
        StatusLeadEnum.desqualificado.value, StatusLeadEnum.em_atendimento.value
    ):
        raise HTTPException(status_code=403, detail="Apenas administradores podem realizar esta ação.")

    lead.status = novo_status
    if novo_status == StatusLeadEnum.assumido and not lead.atribuido_para:
        lead.atribuido_para = usuario.id
        lead.assumido_em = datetime.utcnow()
    lead.atualizado_em = datetime.utcnow()
    db.commit()
    db.refresh(lead)
    return _serial_lead(lead, db)


@app.post("/api/leads/{lead_id}/fechar-contrato")
async def fechar_contrato_lead(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    """Salva os dados financeiros do contrato fechado e marca o lead como Fechado."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    body = await request.json()
    lead.deal_data     = body.get("deal_data", "").strip() or None
    lead.deal_veiculo  = body.get("deal_veiculo", "").strip() or None
    lead.deal_retorno  = body.get("deal_retorno", "").strip() or None
    lead.deal_valor    = body.get("deal_valor", "").strip() or None
    lead.deal_comissao = body.get("deal_comissao", "").strip() or None
    lead.deal_banco     = body.get("deal_banco", "").strip() or None
    lead.deal_conta_pg  = body.get("deal_conta_pg", "").strip() or None
    lead.deal_operadora = body.get("deal_operadora", "").strip() or None
    lead.status        = StatusLeadEnum.fechado
    lead.atualizado_em = datetime.utcnow()
    if not lead.atribuido_para:
        lead.atribuido_para = usuario.id
        lead.assumido_em    = datetime.utcnow()
    db.commit()
    db.refresh(lead)
    return _serial_lead(lead, db)


def _calc_idade(data_nasc_str: str | None) -> str:
    """Calcula idade a partir de DD/MM/YYYY."""
    if not data_nasc_str or data_nasc_str == "—":
        return "—"
    try:
        from datetime import date
        partes = data_nasc_str.strip().split("/")
        if len(partes) != 3:
            return "—"
        nasc = date(int(partes[2]), int(partes[1]), int(partes[0]))
        hoje = date.today()
        idade = hoje.year - nasc.year - ((hoje.month, hoje.day) < (nasc.month, nasc.day))
        return str(idade)
    except Exception:
        return "—"


def _origem_label(origem: str | None, detalhe: str | None) -> str:
    mapa = {
        "rede_social": "Rede Social",
        "parceiro": "Parceiro",
        "ex_cliente": "Ex-cliente",
        "indicacao": "Indicação",
        "whatsapp": "WhatsApp",
    }
    base = mapa.get(origem or "", origem or "WhatsApp")
    if detalhe:
        return f"{base} ({detalhe})"
    return base


def _contratos_periodo(db, periodo: str = "mes") -> list:
    """Retorna leads fechados do período. periodo = 'semana' | 'mes' | 'tudo'."""
    hoje = _agora_br()
    if periodo == "semana":
        dia_semana = hoje.weekday()  # 0=seg
        inicio = datetime(hoje.year, hoje.month, hoje.day, tzinfo=_TZ_BR) - timedelta(days=dia_semana)
    elif periodo == "mes":
        inicio = datetime(hoje.year, hoje.month, 1, tzinfo=_TZ_BR)
    else:
        inicio = None

    q = db.query(Lead).filter(Lead.status == StatusLeadEnum.fechado)
    if inicio:
        q = q.filter(Lead.atualizado_em >= inicio)
    return q.order_by(Lead.atualizado_em.desc()).all()


@app.get("/api/relatorio/contratos-mes")
async def relatorio_contratos_mes(
    periodo: str = "mes",
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    """Retorna contagem e metas do período. Totais financeiros só para admin."""
    leads_fechados = _contratos_periodo(db, periodo)

    hoje = datetime.utcnow()
    total = len(leads_fechados)
    cfg_meta = db.query(Configuracao).filter(Configuracao.chave == "meta_contratos").first()
    meta = int(cfg_meta.valor) if cfg_meta and cfg_meta.valor.isdigit() else 20
    percentual = round(total / meta * 100) if meta > 0 else 0

    resultado: dict = {
        "total":      total,
        "meta":       meta,
        "percentual": min(percentual, 100),
        "mes":        _agora_br().strftime("%B %Y"),
        "periodo":    periodo,
    }

    if usuario.role == RoleEnum.admin:
        def _to_float(v):
            try:
                return float(str(v).replace("R$","").replace(".","").replace(",",".").strip())
            except Exception:
                return 0.0

        total_valor    = sum(_to_float(l.deal_valor)    for l in leads_fechados if l.deal_valor)
        total_comissao = sum(_to_float(l.deal_comissao) for l in leads_fechados if l.deal_comissao)

        resultado["total_valor"]    = total_valor
        resultado["total_comissao"] = total_comissao
        resultado["contratos"] = [
            {
                "id":            l.id,
                "nome":          l.nome or "—",
                "data_nascimento": l.data_nascimento or "—",
                "idade":         _calc_idade(l.data_nascimento),
                "telefone":      l.telefone,
                "deal_data":     l.deal_data or "—",
                "deal_veiculo":  l.deal_veiculo or "—",
                "deal_retorno":  l.deal_retorno or "—",
                "deal_valor":    l.deal_valor or "—",
                "deal_comissao": l.deal_comissao or "—",
                "deal_banco":    l.deal_banco or "—",
                "deal_conta_pg": l.deal_conta_pg or "—",
                "deal_operadora": l.deal_operadora or (l.responsavel.nome if l.responsavel else "—"),
                "responsavel":   l.responsavel.nome if l.responsavel else "—",
                "modalidade":    l.modalidade,
                "origem":        _origem_label(l.origem, l.origem_detalhe),
            }
            for l in leads_fechados
        ]

    return resultado


@app.get("/api/relatorio/contratos/csv")
async def relatorio_contratos_csv(
    periodo: str = "mes",
    db: Session = Depends(get_db),
    admin: Usuario = Depends(requer_admin),
):
    """Exporta contratos fechados do período como CSV (admin only)."""
    from fastapi.responses import StreamingResponse
    import csv, io
    leads = _contratos_periodo(db, periodo)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Cliente", "Idade", "Data", "Veículo", "Retorno",
                     "Valor Financiado", "Comissão", "Banco", "Conta PG",
                     "Origem", "Operadora Responsável"])
    for l in leads:
        writer.writerow([
            l.nome or "—",
            _calc_idade(l.data_nascimento),
            l.deal_data or "—",
            l.deal_veiculo or "—",
            l.deal_retorno or "—",
            l.deal_valor or "—",
            l.deal_comissao or "—",
            l.deal_banco or "—",
            l.deal_conta_pg or "—",
            _origem_label(l.origem, l.origem_detalhe),
            l.deal_operadora or (l.responsavel.nome if l.responsavel else "—"),
        ])
    output.seek(0)
    nome_arquivo = f"contratos_{periodo}.csv"
    headers = {"Content-Disposition": f"attachment; filename={nome_arquivo}"}
    return StreamingResponse(iter([output.getvalue()]),
                             media_type="text/csv; charset=utf-8-sig", headers=headers)


# ─── Analytics / Perfil para Anúncios ────────────────────────────────────────────

@app.get("/api/relatorio/perfil")
async def relatorio_perfil(
    db: Session = Depends(get_db),
    admin: Usuario = Depends(requer_admin),
):
    """Painel de perfil de clientes para direcionar anúncios (admin only)."""
    from collections import Counter
    import re as _re

    fechados  = db.query(Lead).filter(Lead.status == StatusLeadEnum.fechado).all()
    todos     = db.query(Lead).all()

    # ── Faixa etária ─────────────────────────────────────────────────────────
    faixas = {"Até 25": 0, "26-35": 0, "36-45": 0, "46-55": 0, "56+": 0, "N/I": 0}
    for l in fechados:
        idade_str = _calc_idade(l.data_nascimento)
        if idade_str == "—" or not idade_str.isdigit():
            faixas["N/I"] += 1
        else:
            i = int(idade_str)
            if   i <= 25: faixas["Até 25"] += 1
            elif i <= 35: faixas["26-35"]  += 1
            elif i <= 45: faixas["36-45"]  += 1
            elif i <= 55: faixas["46-55"]  += 1
            else:         faixas["56+"]    += 1

    # ── Renda ─────────────────────────────────────────────────────────────────
    rendas = Counter(l.renda for l in fechados if l.renda)

    # ── Profissão (top 8) ─────────────────────────────────────────────────────
    profissoes = Counter(l.profissao.strip().title() for l in fechados if l.profissao).most_common(8)

    # ── Modalidade ────────────────────────────────────────────────────────────
    modalidades = Counter(l.modalidade for l in fechados)

    # ── Origem com taxa de conversão ──────────────────────────────────────────
    origem_total   = Counter(l.origem or "whatsapp" for l in todos)
    origem_fechado = Counter(l.origem or "whatsapp" for l in fechados)
    origens_conv = []
    for orig, total in sorted(origem_total.items(), key=lambda x: -x[1]):
        fechou = origem_fechado.get(orig, 0)
        taxa = round(fechou / total * 100) if total else 0
        origens_conv.append({"origem": orig, "total": total, "fechados": fechou, "taxa": taxa})

    # ── Top veículos (top 8) ──────────────────────────────────────────────────
    veiculos_raw = [l.deal_veiculo or l.carro_interesse for l in fechados if l.deal_veiculo or l.carro_interesse]
    # Extrai marca (primeira palavra)
    marcas = Counter()
    for v in veiculos_raw:
        marca = v.strip().split()[0].upper() if v.strip() else "?"
        marcas[marca] += 1
    top_veiculos = marcas.most_common(8)

    # ── Cidade (top 8) ────────────────────────────────────────────────────────
    cidades = Counter(l.cidade.strip().title() for l in fechados if l.cidade).most_common(8)

    # ── Dia da semana (todos os leads — horário de entrada) ───────────────────
    dias = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
    dia_count = Counter()
    hora_count = Counter()
    for l in todos:
        if l.criado_em:
            dia_count[dias[l.criado_em.weekday()]] += 1
            hora_count[l.criado_em.hour] += 1

    # ── Valor médio financiado ────────────────────────────────────────────────
    def _to_float(v):
        try:
            return float(str(v).replace("R$","").replace(".","").replace(",",".").strip())
        except Exception:
            return 0.0
    valores = [_to_float(l.deal_valor) for l in fechados if l.deal_valor]
    valor_medio = round(sum(valores) / len(valores)) if valores else 0

    # ── Tempo médio até fechar (dias) ─────────────────────────────────────────
    tempos = []
    for l in fechados:
        if l.criado_em and l.atualizado_em:
            dias_delta = (l.atualizado_em - l.criado_em).days
            if 0 <= dias_delta <= 365:
                tempos.append(dias_delta)
    tempo_medio = round(sum(tempos) / len(tempos), 1) if tempos else 0

    return {
        "total_fechados": len(fechados),
        "total_leads":    len(todos),
        "valor_medio":    valor_medio,
        "tempo_medio_dias": tempo_medio,
        "faixas_etarias": faixas,
        "rendas":         dict(rendas),
        "profissoes":     profissoes,
        "modalidades":    dict(modalidades),
        "origens":        origens_conv,
        "top_veiculos":   top_veiculos,
        "cidades":        cidades,
        "dias_semana":    {d: dia_count.get(d, 0) for d in dias},
        "horas":          {str(h): hora_count.get(h, 0) for h in range(0, 24)},
    }


@app.post("/api/conversa/iniciar")
async def iniciar_conversa(
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    """Funcionário inicia uma nova conversa com um número de WhatsApp."""
    body = await request.json()
    telefone = re.sub(r"\D", "", body.get("telefone", ""))
    texto = body.get("mensagem", "").strip()

    if not telefone or len(telefone) < 10:
        raise HTTPException(status_code=400, detail="Número de telefone inválido")
    if not texto:
        raise HTTPException(status_code=400, detail="Mensagem não pode ser vazia")

    # Busca ou cria o lead
    lead = db.query(Lead).filter(Lead.telefone == telefone).first()
    if not lead:
        lead = Lead(
            telefone=telefone,
            status=StatusLeadEnum.assumido,
            atribuido_para=usuario.id,
            assumido_em=datetime.utcnow(),
            estado_conversa="transferido",
        )
        db.add(lead)
        db.commit()
        db.refresh(lead)
    elif lead.status not in [StatusLeadEnum.assumido, StatusLeadEnum.proposta_enviada]:
        lead.status = StatusLeadEnum.assumido
        lead.atribuido_para = usuario.id
        lead.assumido_em = datetime.utcnow()
        lead.atualizado_em = datetime.utcnow()
        db.commit()

    # Salva e envia (registra no cache para ignorar o eco fromMe do Z-API)
    _registrar_msg_painel(telefone, texto)
    _salvar_msg_webhook(db, telefone, f"[{usuario.nome}]: {texto}", role="assistant")
    await enviar_zapi(telefone, texto)

    return _serial_lead(lead, db)


@app.post("/api/leads/{lead_id}/mensagem")
async def enviar_mensagem_funcionario(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    """Funcionário envia mensagem para o cliente pelo dashboard."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    body = await request.json()
    texto = body.get("mensagem", "").strip()
    if not texto:
        raise HTTPException(status_code=400, detail="Mensagem não pode ser vazia")

    # Se funcionário responde, assume o lead automaticamente
    if lead.status in [StatusLeadEnum.em_atendimento, StatusLeadEnum.qualificado]:
        lead.status = StatusLeadEnum.assumido
        lead.atribuido_para = usuario.id
        lead.assumido_em = datetime.utcnow()
        lead.atualizado_em = datetime.utcnow()
        db.commit()

    # Salva no histórico como mensagem do atendente (registra no cache para ignorar eco fromMe)
    _registrar_msg_painel(lead.telefone, texto)
    _salvar_msg_webhook(db, lead.telefone, f"[{usuario.nome}]: {texto}", role="assistant")

    # Envia pelo WhatsApp
    await enviar_zapi(lead.telefone, texto)

    return {"status": "enviado"}


@app.post("/api/leads/{lead_id}/reativar-funil")
async def reativar_funil(
    lead_id: int,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    """Traz o lead de volta ao funil (chamado ao abrir o lead na lista)."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404)
    if lead.oculto_funil:
        lead.oculto_funil = False
        lead.atualizado_em = datetime.utcnow()
        db.commit()
    return {"status": "ok"}


@app.post("/api/leads/{lead_id}/enviar-audio")
async def enviar_audio_gravado(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    """Atendente envia áudio gravado no navegador para o cliente."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    body = await request.json()
    audio_base64_raw = body.get("audio_base64", "")
    if not audio_base64_raw:
        raise HTTPException(status_code=400, detail="Áudio não recebido")

    # Extrai MIME e bytes
    mime = "audio/webm"
    raw_b64 = audio_base64_raw
    if "," in audio_base64_raw:
        header, raw_b64 = audio_base64_raw.split(",", 1)
        mime = header.split(":")[1].split(";")[0] if ":" in header else mime

    ext = "ogg" if "ogg" in mime else "webm"
    audio_bytes = base64.b64decode(raw_b64)
    print(f"🎤 Áudio para {lead.telefone} | mime={mime} | bytes={len(audio_bytes)}")

    # Salva áudio permanentemente para reprodução no painel
    audio_id = uuid.uuid4().hex
    audios_dir = "/app/audios"
    os.makedirs(audios_dir, exist_ok=True)
    audio_filename = f"{audio_id}.{ext}"
    audio_path = f"{audios_dir}/{audio_filename}"
    with open(audio_path, "wb") as f:
        f.write(audio_bytes)

    # Envia pelo Z-API usando URL pública do próprio servidor
    if settings.ZAPI_INSTANCE and settings.ZAPI_TOKEN:
        base_url = str(request.base_url).rstrip("/")
        audio_url = f"{base_url}/api/audio/{audio_filename}"
        print(f"🎤 URL: {audio_url}")
        zapi_url = f"https://api.z-api.io/instances/{settings.ZAPI_INSTANCE}/token/{settings.ZAPI_TOKEN}/send-audio"
        headers_zapi = {"Client-Token": settings.ZAPI_CLIENT_TOKEN}
        payload = {"phone": lead.telefone, "audio": audio_url}
        async with httpx.AsyncClient() as client:
            resp = await client.post(zapi_url, headers=headers_zapi, json=payload, timeout=30)
        print(f"🎤 Z-API resposta: {resp.status_code} — {resp.text[:300]}")
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Z-API erro: {resp.text[:200]}")
    else:
        print(f"[Z-API SIMULADO] Áudio para {lead.telefone}")

    # Salva no histórico com referência ao arquivo (para reprodução no painel)
    _salvar_msg_webhook(db, lead.telefone, f"[{usuario.nome}]: [AUDIO:{audio_filename}]", role="assistant")

    return {"status": "enviado"}


@app.get("/api/audio/{filename}")
async def servir_audio(filename: str):
    """Serve arquivos de áudio para o Z-API baixar e para reprodução no painel."""
    if not re.match(r'^[a-f0-9]{32}\.(webm|ogg|mp3)$', filename):
        raise HTTPException(status_code=404)
    path = f"/app/audios/{filename}"
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    ext = filename.rsplit(".", 1)[-1]
    media_type = "audio/ogg" if ext == "ogg" else "audio/webm"
    return FileResponse(path, media_type=media_type)


@app.get("/api/imagem/{filename}")
async def servir_imagem(filename: str):
    """Serve arquivos de imagem salvos do WhatsApp."""
    if not re.match(r'^[a-f0-9]{32}\.(jpg|jpeg|png|webp|gif)$', filename):
        raise HTTPException(status_code=404)
    path = f"/app/imagens/{filename}"
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    ext = filename.rsplit(".", 1)[-1].lower()
    tipos = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
             "webp": "image/webp", "gif": "image/gif"}
    return FileResponse(path, media_type=tipos.get(ext, "image/jpeg"))


@app.get("/api/documento/{filename}")
async def servir_documento(filename: str):
    """Serve documentos/PDFs salvos do WhatsApp com header de download."""
    if not re.match(r'^[a-f0-9]{32}\.\w{2,5}$', filename):
        raise HTTPException(status_code=404)
    path = f"/app/documentos/{filename}"
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    ext = filename.rsplit(".", 1)[-1].lower()
    media_type = "application/pdf" if ext == "pdf" else "application/octet-stream"
    return FileResponse(path, media_type=media_type,
                        headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/api/inbox")
async def inbox(
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    """Retorna TODAS as conversas com mensagens, ordenadas pela mais recente."""
    # Busca todos os leads que têm pelo menos uma mensagem
    leads_com_msg = (
        db.query(Lead)
        .filter(Lead.status != StatusLeadEnum.desqualificado)
        .order_by(Lead.atualizado_em.desc())
        .all()
    )

    resultado = []
    for l in leads_com_msg:
        ultima = (
            db.query(MensagemConversa)
            .filter(MensagemConversa.telefone == l.telefone)
            .order_by(MensagemConversa.id.desc())
            .first()
        )
        if not ultima:
            continue  # ignora leads sem nenhuma mensagem
        import re
        conteudo_limpo = re.sub(r'^\[[^\]]+\]:\s*', '', ultima.conteudo)
        resultado.append({
            **_serial_lead(l, db),
            "ultima_mensagem": conteudo_limpo[:60],
            "ultima_hora": _fmt_br(ultima.criado_em, "%H:%M") if ultima and ultima.criado_em else "",
            "ultima_msg_ts": ultima.criado_em.timestamp() if ultima and ultima.criado_em else 0,
        })

    # Ordena pelo timestamp da última mensagem (mais recente primeiro)
    resultado.sort(key=lambda x: x.get("ultima_msg_ts", 0), reverse=True)
    return resultado


@app.patch("/api/leads/{lead_id}")
async def editar_lead(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    """Edita dados do lead — disponível para admin e funcionários."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    body = await request.json()
    campos_editaveis = ["nome", "cpf", "data_nascimento", "carro_interesse", "modalidade", "observacoes", "cidade", "renda", "profissao", "tem_cnh"]
    for campo in campos_editaveis:
        if campo in body:
            valor = body[campo]
            if campo == "cpf" and valor:
                valor = re.sub(r"[^\d]", "", valor)
                if len(valor) == 11:
                    valor = f"{valor[:3]}.{valor[3:6]}.{valor[6:9]}-{valor[9:]}"
            setattr(lead, campo, valor if valor != "" else None)
    # Dados extras para o requerimento (JSON blob)
    if "dados_contrato" in body:
        import json as _json
        dc = body["dados_contrato"]
        lead.dados_contrato = _json.dumps(dc, ensure_ascii=False) if isinstance(dc, dict) else None
    lead.atualizado_em = datetime.utcnow()
    db.commit()
    db.refresh(lead)
    return _serial_lead(lead, db)


@app.post("/api/leads/{lead_id}/observacoes")
async def adicionar_observacao(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    body = await request.json()
    texto = (body.get("texto") or "").strip()
    if not texto:
        raise HTTPException(status_code=400, detail="Texto vazio")

    lista = _parse_observacoes(lead.observacoes)
    lista.append({
        "texto": texto,
        "usuario": usuario.nome,
        "em": datetime.utcnow().strftime("%d/%m/%Y %H:%M"),
    })
    lead.observacoes = json.dumps(lista, ensure_ascii=False)
    lead.atualizado_em = datetime.utcnow()
    db.commit()
    return {"status": "ok", "observacoes": lista}


@app.delete("/api/leads/{lead_id}/observacoes/{idx}")
async def deletar_observacao(
    lead_id: int,
    idx: int,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(requer_admin),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    lista = _parse_observacoes(lead.observacoes)
    if idx < 0 or idx >= len(lista):
        raise HTTPException(status_code=400, detail="Índice inválido")
    lista.pop(idx)
    lead.observacoes = json.dumps(lista, ensure_ascii=False)
    lead.atualizado_em = datetime.utcnow()
    db.commit()
    return {"status": "ok", "observacoes": lista}


@app.post("/api/leads/{lead_id}/fechar")
async def fechar_lead(
    lead_id: int,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    lead.status = StatusLeadEnum.fechado
    db.commit()
    return _serial_lead(lead, db)


@app.post("/api/sincronizar-chats")
async def sincronizar_chats(
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    """Importa todos os chats existentes do WhatsApp para o painel."""
    if not settings.ZAPI_INSTANCE or not settings.ZAPI_TOKEN:
        raise HTTPException(status_code=400, detail="Z-API não configurada.")

    base = f"https://api.z-api.io/instances/{settings.ZAPI_INSTANCE}/token/{settings.ZAPI_TOKEN}"
    headers = {"Client-Token": settings.ZAPI_CLIENT_TOKEN}

    criados = 0
    ignorados = 0
    page = 1

    async with httpx.AsyncClient() as http:
        while True:
            resp = await http.get(
                f"{base}/chats",
                headers=headers,
                params={"page": page, "pageSize": 100},
                timeout=30,
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"Erro Z-API: {resp.text}")

            chats = resp.json()
            if not chats:
                break

            for chat in chats:
                # Ignora grupos, broadcasts e spam
                if chat.get("isGroup") or chat.get("isMarkedSpam"):
                    ignorados += 1
                    continue

                telefone = re.sub(r"\D", "", chat.get("phone", ""))
                if not telefone or len(telefone) < 10:
                    ignorados += 1
                    continue

                # Só cria se ainda não existe
                existente = db.query(Lead).filter(Lead.telefone == telefone).first()
                if not existente:
                    nome = chat.get("name") or None
                    # Ignora se o nome for igual ao telefone (sem nome real)
                    if nome and re.sub(r"\D", "", nome) == telefone:
                        nome = None
                    lead = Lead(
                        telefone=telefone,
                        nome=nome,
                        status=StatusLeadEnum.em_atendimento,
                        estado_conversa=EstadoConversaEnum.transferido,
                    )
                    db.add(lead)
                    criados += 1
                else:
                    # Atualiza nome se ainda não tinha
                    nome = chat.get("name") or None
                    if nome and re.sub(r"\D", "", nome) == telefone:
                        nome = None
                    if nome and not existente.nome:
                        existente.nome = nome
                    ignorados += 1

            db.commit()

            if len(chats) < 100:
                break
            page += 1

    return {"criados": criados, "ignorados": ignorados, "total_paginas": page}


@app.delete("/api/leads/{lead_id}")
async def excluir_lead(
    lead_id: int,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(requer_admin),
):
    """Exclui permanentemente um lead e todas as suas mensagens. Apenas admin."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    db.query(MensagemConversa).filter(MensagemConversa.telefone == lead.telefone).delete()
    db.delete(lead)
    db.commit()
    return {"status": "excluido"}


@app.put("/api/leads/{lead_id}/atribuir")
async def atribuir_lead(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(requer_admin),
):
    """Troca o atendente responsável por um lead. Apenas admin."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    body = await request.json()
    usuario_id = body.get("usuario_id")
    if usuario_id:
        u = db.query(Usuario).filter(Usuario.id == usuario_id, Usuario.ativo == True).first()
        if not u:
            raise HTTPException(status_code=404, detail="Usuário não encontrado")
        lead.atribuido_para = u.id
        lead.assumido_em = lead.assumido_em or datetime.utcnow()
        if lead.status == StatusLeadEnum.em_atendimento or lead.status == StatusLeadEnum.qualificado:
            lead.status = StatusLeadEnum.assumido
    else:
        lead.atribuido_para = None
    lead.atualizado_em = datetime.utcnow()
    db.commit()
    return _serial_lead(lead, db)


@app.get("/api/stats")
async def estatisticas(
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    total = db.query(Lead).count()
    em_atendimento = db.query(Lead).filter(Lead.status == StatusLeadEnum.em_atendimento).count()
    qualificados = db.query(Lead).filter(Lead.status == StatusLeadEnum.qualificado).count()
    assumidos = db.query(Lead).filter(Lead.status == StatusLeadEnum.assumido).count()
    propostas = db.query(Lead).filter(Lead.status == StatusLeadEnum.proposta_enviada).count()
    fechados = db.query(Lead).filter(Lead.status == StatusLeadEnum.fechado).count()
    perdidos = db.query(Lead).filter(Lead.status == StatusLeadEnum.perdido).count()
    desqualificados = db.query(Lead).filter(Lead.status == StatusLeadEnum.desqualificado).count()
    conv = qualificados + assumidos + propostas + fechados
    return {
        "total": total,
        "em_atendimento": em_atendimento,
        "qualificados": qualificados,
        "assumidos": assumidos,
        "propostas": propostas,
        "fechados": fechados,
        "perdidos": perdidos,
        "desqualificados": desqualificados,
        "financiamento": db.query(Lead).filter(Lead.modalidade == "financiamento").count(),
        "refinanciamento": db.query(Lead).filter(Lead.modalidade == "refinanciamento").count(),
        "taxa_qualificacao": round((conv / total * 100), 1) if total > 0 else 0,
    }


# ─── API Usuários (admin) ─────────────────────────────────────────────────────────

@app.get("/api/usuarios")
async def listar_usuarios(db: Session = Depends(get_db), admin: Usuario = Depends(requer_admin)):
    return [_serial_usuario(u) for u in db.query(Usuario).order_by(Usuario.criado_em).all()]


@app.post("/api/usuarios")
async def criar_usuario(request: Request, db: Session = Depends(get_db), admin: Usuario = Depends(requer_admin)):
    body = await request.json()
    email = body.get("email", "").strip().lower()
    if db.query(Usuario).filter(Usuario.email == email).first():
        raise HTTPException(status_code=400, detail="E-mail já cadastrado")
    u = Usuario(
        nome=body.get("nome", "").strip(),
        email=email,
        senha_hash=hash_senha(body.get("senha", "Senha@123")),
        role=body.get("role", RoleEnum.funcionario),
        ativo=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return _serial_usuario(u)


@app.put("/api/usuarios/{uid}")
async def atualizar_usuario(uid: int, request: Request, db: Session = Depends(get_db), admin: Usuario = Depends(requer_admin)):
    u = db.query(Usuario).filter(Usuario.id == uid).first()
    if not u:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    body = await request.json()
    if "nome" in body:
        u.nome = body["nome"].strip()
    if "email" in body:
        novo_email = body["email"].strip().lower()
        if novo_email != u.email:
            if db.query(Usuario).filter(Usuario.email == novo_email, Usuario.id != uid).first():
                raise HTTPException(status_code=400, detail="E-mail já está em uso por outro usuário")
            u.email = novo_email
    if "role" in body:
        u.role = body["role"]
    if "ativo" in body:
        u.ativo = body["ativo"]
    if body.get("senha"):
        u.senha_hash = hash_senha(body["senha"])
    db.commit()
    db.refresh(u)
    return _serial_usuario(u)


@app.delete("/api/usuarios/{uid}")
async def desativar_usuario(uid: int, db: Session = Depends(get_db), admin: Usuario = Depends(requer_admin)):
    u = db.query(Usuario).filter(Usuario.id == uid).first()
    if not u:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    u.ativo = False
    db.commit()
    return {"status": "desativado"}


# ─── Perfil do próprio usuário ───────────────────────────────────────────────────

@app.put("/api/me")
async def atualizar_perfil(
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    """Permite que qualquer usuário logado atualize seu nome e/ou senha."""
    body = await request.json()
    nome       = body.get("nome", "").strip()
    senha_atual = body.get("senha_atual", "")
    nova_senha  = body.get("nova_senha", "")

    if nome:
        usuario.nome = nome

    if nova_senha:
        if not senha_atual:
            raise HTTPException(status_code=400, detail="Informe a senha atual para trocar a senha")
        if not verificar_senha(senha_atual, usuario.senha_hash):
            raise HTTPException(status_code=401, detail="Senha atual incorreta")
        if len(nova_senha) < 6:
            raise HTTPException(status_code=400, detail="A nova senha deve ter ao mínimo 6 caracteres")
        usuario.senha_hash = hash_senha(nova_senha)

    db.commit()
    db.refresh(usuario)
    return {"id": usuario.id, "nome": usuario.nome, "email": usuario.email, "role": usuario.role}


# ─── Configurações do bot (admin) ────────────────────────────────────────────────

@app.get("/api/config")
async def listar_config(db: Session = Depends(get_db), admin: Usuario = Depends(requer_admin)):
    configs = db.query(Configuracao).order_by(Configuracao.chave).all()
    return [{"chave": c.chave, "valor": c.valor, "descricao": c.descricao} for c in configs]


@app.put("/api/config/{chave}")
async def atualizar_config(
    chave: str, request: Request,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(requer_admin),
):
    body = await request.json()
    config = db.query(Configuracao).filter(Configuracao.chave == chave).first()
    if not config:
        raise HTTPException(status_code=404, detail="Configuração não encontrada")
    config.valor = body.get("valor", config.valor)
    config.atualizado_em = datetime.utcnow()
    db.commit()
    return {"status": "ok", "chave": chave}


# ─── Bancos (lista gerenciável) ───────────────────────────────────────────────────

def _get_bancos_lista(db: Session) -> list:
    import json as _json
    cfg = db.query(Configuracao).filter(Configuracao.chave == "bancos_lista").first()
    if not cfg:
        return []
    try:
        return _json.loads(cfg.valor)
    except Exception:
        return []

def _set_bancos_lista(db: Session, bancos: list):
    import json as _json
    cfg = db.query(Configuracao).filter(Configuracao.chave == "bancos_lista").first()
    if not cfg:
        cfg = Configuracao(chave="bancos_lista", descricao="Lista de bancos para o formulário de contrato")
        db.add(cfg)
    cfg.valor = _json.dumps(bancos, ensure_ascii=False)
    cfg.atualizado_em = datetime.utcnow()
    db.commit()

@app.get("/api/bancos")
async def listar_bancos(db: Session = Depends(get_db), usuario: Usuario = Depends(obter_usuario_atual)):
    return _get_bancos_lista(db)

@app.post("/api/bancos")
async def adicionar_banco(
    request: Request,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(requer_admin),
):
    body = await request.json()
    nome = (body.get("nome") or "").strip().upper()
    if not nome:
        raise HTTPException(status_code=400, detail="Nome do banco é obrigatório")
    bancos = _get_bancos_lista(db)
    if nome not in bancos:
        bancos.append(nome)
        bancos.sort()
        _set_bancos_lista(db, bancos)
    return bancos

@app.delete("/api/bancos/{nome}")
async def remover_banco(
    nome: str,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(requer_admin),
):
    bancos = _get_bancos_lista(db)
    nome_upper = nome.strip().upper()
    bancos = [b for b in bancos if b.upper() != nome_upper]
    _set_bancos_lista(db, bancos)
    return bancos


# ─── Relatórios (admin) ───────────────────────────────────────────────────────────

def _duracao_str(segundos: int) -> str:
    """Converte segundos em string legível ex: '2h 15min'."""
    if segundos < 60:
        return f"{segundos}s"
    m = segundos // 60
    if m < 60:
        return f"{m}min"
    h, rm = divmod(m, 60)
    return f"{h}h {rm}min" if rm else f"{h}h"


@app.get("/api/relatorios")
async def relatorios(db: Session = Depends(get_db), admin: Usuario = Depends(requer_admin)):
    usuarios = db.query(Usuario).filter(Usuario.ativo == True).all()
    por_funcionario = []
    for u in usuarios:
        total = db.query(Lead).filter(Lead.atribuido_para == u.id).count()
        fechados = db.query(Lead).filter(Lead.atribuido_para == u.id, Lead.status == StatusLeadEnum.fechado).count()
        por_funcionario.append({
            "nome": u.nome,
            "role": u.role,
            "total_assumidos": total,
            "fechados": fechados,
            "taxa": round((fechados / total * 100), 1) if total > 0 else 0,
        })
    return {
        "por_funcionario": por_funcionario,
        "por_modalidade": {
            "financiamento": db.query(Lead).filter(Lead.modalidade == "financiamento").count(),
            "refinanciamento": db.query(Lead).filter(Lead.modalidade == "refinanciamento").count(),
        },
    }


@app.get("/api/relatorio/parceiros")
async def relatorio_parceiros(
    db: Session = Depends(get_db),
    admin: Usuario = Depends(requer_admin),
):
    """Relatório de desempenho por parceiro (admin only)."""
    parceiros = db.query(Parceiro).order_by(Parceiro.nome).all()
    resultado = []
    for p in parceiros:
        leads = db.query(Lead).filter(Lead.parceiro_id == p.id).all()
        total        = len(leads)
        assumidos    = sum(1 for l in leads if l.status in (
            StatusLeadEnum.assumido, StatusLeadEnum.proposta_enviada,
            StatusLeadEnum.fechado, StatusLeadEnum.perdido))
        propostas    = sum(1 for l in leads if l.status in (
            StatusLeadEnum.proposta_enviada, StatusLeadEnum.fechado))
        fechados     = sum(1 for l in leads if l.status == StatusLeadEnum.fechado)
        # Contratos gerados para leads deste parceiro
        lead_ids     = [l.id for l in leads]
        contratos    = db.query(Contrato).filter(Contrato.lead_id.in_(lead_ids)).count() if lead_ids else 0
        resultado.append({
            "id":            p.id,
            "nome":          p.nome,
            "telefone":      p.telefone,
            "ativo":         p.ativo,
            "total_leads":   total,
            "assumidos":     assumidos,
            "propostas":     propostas,
            "fechados":      fechados,
            "contratos":     contratos,
            "taxa_fechamento": round(fechados / propostas * 100, 1) if propostas > 0 else 0,
        })
    # Ordena por mais leads enviados
    resultado.sort(key=lambda x: x["total_leads"], reverse=True)
    return resultado


def _sessoes_funcionarias(db: Session, limit: int = 1000) -> list:
    """Retorna sessões apenas de funcionárias (sem admin), ordenadas por login desc."""
    sessoes = (
        db.query(SessaoUsuario)
        .join(Usuario, Usuario.id == SessaoUsuario.usuario_id)
        .filter(Usuario.role == RoleEnum.funcionario)
        .order_by(SessaoUsuario.login_em.desc())
        .limit(limit)
        .all()
    )
    resultado = []
    for s in sessoes:
        fim = s.logout_em or s.ultimo_ativo_em
        tempo_s = max(0, int((fim - s.login_em).total_seconds())) if fim and s.login_em else 0
        resultado.append({
            "id": s.id,
            "usuario": s.usuario.nome if s.usuario else "—",
            "role": s.usuario.role if s.usuario else "—",
            "ip": s.ip or "—",
            "localizacao": s.localizacao or "—",
            "login_em": _fmt_br(s.login_em) or "—",
            "ultimo_ativo_em": _fmt_br(s.ultimo_ativo_em) or "—",
            "logout_em": _fmt_br(s.logout_em),
            "tempo_logado": _duracao_str(tempo_s),
            "tempo_ativo": _duracao_str(s.tempo_ativo_s or 0),
            "tempo_ativo_s": s.tempo_ativo_s or 0,
            "ativa": (
                s.logout_em is None
                and s.ultimo_ativo_em is not None
                and (datetime.utcnow() - s.ultimo_ativo_em).total_seconds() < 300
            ),
        })
    return resultado


@app.get("/api/relatorio/sessoes")
async def relatorio_sessoes(
    db: Session = Depends(get_db),
    admin: Usuario = Depends(requer_admin),
):
    """Retorna histórico de sessões somente de funcionárias (admin only)."""
    return _sessoes_funcionarias(db)


@app.get("/api/relatorio/sessoes/csv")
async def relatorio_sessoes_csv(
    db: Session = Depends(get_db),
    admin: Usuario = Depends(requer_admin),
):
    """Exporta histórico de sessões como CSV (admin only)."""
    from fastapi.responses import StreamingResponse
    import csv, io
    sessoes = _sessoes_funcionarias(db)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Funcionária", "Login em", "Último acesso", "Logout em",
                     "Tempo logado", "Tempo ativo", "Localização", "IP"])
    for s in sessoes:
        writer.writerow([
            s["usuario"], s["login_em"], s["ultimo_ativo_em"],
            s["logout_em"] or "—", s["tempo_logado"], s["tempo_ativo"],
            s["localizacao"], s["ip"],
        ])
    output.seek(0)
    headers = {"Content-Disposition": "attachment; filename=atividade_funcionarias.csv"}
    return StreamingResponse(iter([output.getvalue()]),
                             media_type="text/csv; charset=utf-8-sig", headers=headers)


# ─── Dashboard ────────────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent / "templates" / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>dashboard.html não encontrado</h1>")


@app.get("/")
async def root():
    return {"app": "Fácil Financiamentos Bot v2", "status": "online", "dashboard": "/dashboard"}


# ─── Contratos / Assinatura Digital ─────────────────────────────────────────────

@app.post("/api/leads/{lead_id}/contrato")
async def gerar_contrato_endpoint(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    import traceback as _tb
    try:
        # ── 1. importar módulo ────────────────────────────────────────────
        from gerar_contrato import gerar_pdf_contrato, salvar_pdf

        # ── 2. lead ──────────────────────────────────────────────────────
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            raise HTTPException(404, "Lead não encontrado")

        body = await request.json()
        dados = body.get("dados", {})

        dados.setdefault("req_nome",    lead.nome     or "")
        dados.setdefault("req_cpf",     lead.cpf      or "")
        dados.setdefault("req_celular", lead.telefone or "")
        dados.setdefault("modalidade",  lead.modalidade or "refinanciamento")
        dados.setdefault("data_contrato",
            _agora_br().strftime("%d de %B de %Y")
            .replace("January","Janeiro").replace("February","Fevereiro")
            .replace("March","Marco").replace("April","Abril")
            .replace("May","Maio").replace("June","Junho")
            .replace("July","Julho").replace("August","Agosto")
            .replace("September","Setembro").replace("October","Outubro")
            .replace("November","Novembro").replace("December","Dezembro"))

        # ── 3. gerar PDF ─────────────────────────────────────────────────
        doc_id = secrets.token_hex(8).upper()
        pdf_bytes, hash_doc = gerar_pdf_contrato(dados, doc_id)

        # ── 4. salvar arquivo ────────────────────────────────────────────
        tok = secrets.token_hex(32)
        nome_arquivo = f"contrato_{lead_id}_{tok[:8]}.pdf"
        caminho = salvar_pdf(pdf_bytes, nome_arquivo)

        # ── 5. persistir no banco ────────────────────────────────────────
        tok_prop = secrets.token_hex(32)
        contrato = Contrato(
            lead_id=lead_id,
            criado_por_id=usuario.id,
            token=tok,
            token_prop=tok_prop,
            hash_doc=hash_doc,
            pdf_original=caminho,
            dados_contrato=json.dumps(dados, ensure_ascii=False),
            status="pendente",
            status_prop="pendente",
        )
        db.add(contrato)
        db.commit()
        db.refresh(contrato)

        base_url = str(request.base_url).rstrip("/")
        link_req  = f"{base_url}/assinar/{tok}"
        link_prop = f"{base_url}/assinar/{tok_prop}"

        # ── 6. Enviar link de assinatura para o vendedor/proprietário via WhatsApp ──
        prop_tel = dados.get("prop_telefone", "").strip()
        prop_nome = dados.get("prop_nome", "Proprietário").strip() or "Proprietário"
        if prop_tel:
            msg_prop = (
                f"Olá, {prop_nome}! 👋\n\n"
                f"A Fácil Financiamentos gerou um contrato que requer sua assinatura digital.\n\n"
                f"🔗 Clique no link abaixo para assinar:\n{link_prop}\n\n"
                f"O processo é rápido e 100% online. Qualquer dúvida, entre em contato conosco."
            )
            try:
                await enviar_zapi(prop_tel, msg_prop)
            except Exception:
                pass  # não bloqueia se falhar o envio

        return {
            "contrato_id": contrato.id,
            "hash": hash_doc,
            "doc_id": doc_id,
            "link_requerente":   link_req,
            "link_proprietario": link_prop,
            # compat retroativa
            "link": link_req,
        }

    except HTTPException:
        raise  # re-lança 404 normalmente
    except Exception as exc:
        tb_str = _tb.format_exc()
        print(f"❌ Erro em gerar_contrato_endpoint:\n{tb_str}")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


@app.get("/assinar/{token}", response_class=HTMLResponse)
async def pagina_assinar(token: str):
    caminho = Path("templates/assinar.html")
    return HTMLResponse(caminho.read_text(encoding="utf-8"))


def _detectar_role_contrato(token: str, db):
    """Retorna (contrato, role) onde role = 'requerente' | 'proprietario'."""
    c = db.query(Contrato).filter(Contrato.token == token).first()
    if c:
        return c, "requerente"
    c = db.query(Contrato).filter(Contrato.token_prop == token).first()
    if c:
        return c, "proprietario"
    return None, None


@app.get("/assinar/{token}/pdf-original")
async def pdf_preview_contrato(token: str, db: Session = Depends(get_db)):
    c, _ = _detectar_role_contrato(token, db)
    if not c:
        raise HTTPException(404, "Contrato não encontrado")
    if not c.pdf_original:
        raise HTTPException(404, "PDF não disponível")
    p = Path(c.pdf_original)
    if not p.exists():
        raise HTTPException(404, "Arquivo não encontrado no servidor")
    return FileResponse(str(p), media_type="application/pdf",
                        headers={"Content-Disposition": "inline"})


@app.get("/assinar/{token}/conteudo")
async def conteudo_contrato(token: str, db: Session = Depends(get_db)):
    contrato, role = _detectar_role_contrato(token, db)
    if not contrato:
        raise HTTPException(404, "Contrato não encontrado")
    if role == "requerente"   and contrato.status      == "assinado":
        raise HTTPException(410, "Contrato ja assinado")
    if role == "proprietario" and contrato.status_prop == "assinado":
        raise HTTPException(410, "Contrato ja assinado")

    d = json.loads(contrato.dados_contrato or "{}") if contrato.dados_contrato else {}
    nome_req  = d.get("req_nome",  "-")
    nome_prop = d.get("prop_nome", "-")

    # Mascara o telefone do lead para exibir na tela de confirmação
    lead = db.query(Lead).filter(Lead.id == contrato.lead_id).first()
    tel_mascarado = ""
    if lead and lead.telefone:
        t = re.sub(r'\D', '', lead.telefone)
        if len(t) >= 8:
            tel_mascarado = f"(*****){t[-4:]}"

    return {
        "hash":              contrato.hash_doc,
        "role":              role,
        "nome_req":          nome_req,
        "nome_prop":         nome_prop,
        "doc_id":            d.get("doc_id", ""),
        "data":              d.get("data_contrato", ""),
        "telefone_mascarado": tel_mascarado,
    }


@app.post("/assinar/{token}/enviar-codigo")
async def enviar_codigo_otp(token: str, db: Session = Depends(get_db)):
    """Gera e envia código OTP de 6 dígitos via WhatsApp para confirmação de assinatura."""
    import random
    contrato, role = _detectar_role_contrato(token, db)
    if not contrato:
        raise HTTPException(404, "Contrato não encontrado")

    codigo = str(random.randint(100000, 999999))
    expira = datetime.utcnow() + timedelta(minutes=10)

    if role == "requerente":
        contrato.codigo_req        = codigo
        contrato.codigo_req_expira = expira
    else:
        contrato.codigo_prop        = codigo
        contrato.codigo_prop_expira = expira
    db.commit()

    lead = db.query(Lead).filter(Lead.id == contrato.lead_id).first()
    telefone = lead.telefone if lead else None

    if telefone:
        msg = (
            f"🔐 *Código de confirmação — Fácil Financiamentos*\n\n"
            f"Olá! Seu código para confirmar a assinatura do contrato é:\n\n"
            f"*{codigo}*\n\n"
            f"⏱ Válido por 10 minutos.\n"
            f"Não compartilhe este código com ninguém."
        )
        await enviar_zapi(telefone, msg)

    tel_mascarado = ""
    if telefone:
        t = re.sub(r'\D', '', telefone)
        if len(t) >= 4:
            tel_mascarado = f"(*****){t[-4:]}"

    return {"ok": True, "telefone_mascarado": tel_mascarado}


@app.post("/assinar/{token}/verificar-codigo")
async def verificar_codigo_otp(token: str, request: Request, db: Session = Depends(get_db)):
    """Valida o código OTP digitado pelo assinante."""
    body = await request.json()
    codigo_digitado = str(body.get("codigo", "")).strip()

    contrato, role = _detectar_role_contrato(token, db)
    if not contrato:
        raise HTTPException(404, "Contrato não encontrado")

    if role == "requerente":
        codigo_salvo = contrato.codigo_req
        expira       = contrato.codigo_req_expira
    else:
        codigo_salvo = contrato.codigo_prop
        expira       = contrato.codigo_prop_expira

    if not codigo_salvo:
        raise HTTPException(400, detail="Nenhum código foi enviado. Solicite um novo código.")
    if not expira or datetime.utcnow() > expira:
        raise HTTPException(400, detail="Código expirado. Solicite um novo código.")
    if codigo_digitado != codigo_salvo:
        raise HTTPException(400, detail="Código incorreto. Verifique e tente novamente.")

    # Invalida o código após uso bem-sucedido
    if role == "requerente":
        contrato.codigo_req        = None
        contrato.codigo_req_expira = None
    else:
        contrato.codigo_prop        = None
        contrato.codigo_prop_expira = None
    db.commit()

    return {"ok": True}


@app.post("/assinar/{token}")
async def submeter_assinatura(token: str, request: Request, db: Session = Depends(get_db)):
    import traceback as _tb
    try:
        from gerar_contrato import (
            base64_para_imagem, gerar_pdf_final_completo,
            salvar_pdf, CONTRATOS_DIR,
        )
        from pathlib import Path as Pt

        contrato, role = _detectar_role_contrato(token, db)
        if not contrato:
            raise HTTPException(404, "Contrato nao encontrado")
        if role == "requerente"   and contrato.status      == "assinado":
            raise HTTPException(410, "Contrato ja assinado")
        if role == "proprietario" and contrato.status_prop == "assinado":
            raise HTTPException(410, "Contrato ja assinado")

        body = await request.json()
        selfie_b64     = body.get("selfie", "")
        assin_b64      = body.get("assinatura", "")
        doc_frente_b64 = body.get("doc_frente", "")
        doc_verso_b64  = body.get("doc_verso", "")
        geo            = body.get("geo", "")
        ip             = request.client.host if request.client else "desconhecido"
        agora          = _agora_br().strftime("%d/%m/%Y %H:%M:%S")

        base = CONTRATOS_DIR / f"contrato_{contrato.lead_id}_{token[:8]}"
        d_contrato = json.loads(contrato.dados_contrato or "{}") if contrato.dados_contrato else {}
        base_url = str(request.base_url).rstrip("/")

        if role == "requerente":
            selfie_path = str(base) + "_selfie_req.jpg"
            assin_path  = str(base) + "_assin_req.png"
            frente_path = str(base) + "_doc_frente_req.jpg"
            verso_path  = str(base) + "_doc_verso_req.jpg"
            base64_para_imagem(selfie_b64,     Pt(selfie_path))
            base64_para_imagem(assin_b64,      Pt(assin_path))
            base64_para_imagem(doc_frente_b64, Pt(frente_path))
            base64_para_imagem(doc_verso_b64,  Pt(verso_path))

            contrato.status              = "assinado"
            contrato.selfie_path         = selfie_path
            contrato.assinatura_path     = assin_path
            contrato.doc_frente_req_path = frente_path
            contrato.doc_verso_req_path  = verso_path
            contrato.ip_cliente          = ip
            contrato.geolocalizacao      = geo
            contrato.assinado_em         = datetime.utcnow()

        else:  # proprietario
            selfie_path = str(base) + "_selfie_prop.jpg"
            assin_path  = str(base) + "_assin_prop.png"
            frente_path = str(base) + "_doc_frente_prop.jpg"
            verso_path  = str(base) + "_doc_verso_prop.jpg"
            base64_para_imagem(selfie_b64,     Pt(selfie_path))
            base64_para_imagem(assin_b64,      Pt(assin_path))
            base64_para_imagem(doc_frente_b64, Pt(frente_path))
            base64_para_imagem(doc_verso_b64,  Pt(verso_path))

            contrato.status_prop          = "assinado"
            contrato.selfie_prop_path     = selfie_path
            contrato.assinatura_prop_path = assin_path
            contrato.doc_frente_prop_path = frente_path
            contrato.doc_verso_prop_path  = verso_path
            contrato.ip_prop              = ip
            contrato.geo_prop             = geo
            contrato.assinado_prop_em     = datetime.utcnow()

        db.commit()
        db.refresh(contrato)

        # ── Gera PDF final unificado com todas as assinaturas disponíveis ──────
        if not contrato.pdf_original or not Pt(contrato.pdf_original).exists():
            raise HTTPException(500, "PDF original nao encontrado")

        dados_req  = None
        dados_prop = None

        if contrato.status == "assinado":
            dados_req = {
                "assinado_em": contrato.assinado_em.strftime("%d/%m/%Y %H:%M:%S") if contrato.assinado_em else agora,
                "ip": contrato.ip_cliente or ip,
                "geo": contrato.geolocalizacao or "nao fornecida",
                "hash_doc": contrato.hash_doc,
                "nome": d_contrato.get("req_nome") or (contrato.lead.nome if contrato.lead else "-") or "-",
                "cpf":  d_contrato.get("req_cpf")  or (contrato.lead.cpf  if contrato.lead else "-") or "-",
            }

        if contrato.status_prop == "assinado":
            dados_prop = {
                "assinado_em": contrato.assinado_prop_em.strftime("%d/%m/%Y %H:%M:%S") if contrato.assinado_prop_em else agora,
                "ip": contrato.ip_prop or ip,
                "geo": contrato.geo_prop or "nao fornecida",
                "hash_doc": contrato.hash_doc,
                "nome": d_contrato.get("prop_nome", "-"),
                "cpf":  d_contrato.get("prop_cpf",  "-"),
            }

        pdf_final = gerar_pdf_final_completo(
            contrato.pdf_original,
            assin_req_path=contrato.assinatura_path,
            selfie_req_path=contrato.selfie_path,
            dados_req=dados_req,
            doc_frente_req_path=contrato.doc_frente_req_path,
            doc_verso_req_path=contrato.doc_verso_req_path,
            assin_prop_path=contrato.assinatura_prop_path,
            selfie_prop_path=contrato.selfie_prop_path,
            dados_prop=dados_prop,
            doc_frente_prop_path=contrato.doc_frente_prop_path,
            doc_verso_prop_path=contrato.doc_verso_prop_path,
            doc_id=d_contrato.get("doc_id", ""),
            verificacao_url_req=f"{base_url}/verificar/{contrato.token}",
            verificacao_url_prop=f"{base_url}/verificar/{contrato.token_prop}" if contrato.token_prop else "",
        )

        pdf_final_path = str(base) + "_assinado_final.pdf"
        Pt(pdf_final_path).write_bytes(pdf_final)
        contrato.pdf_assinado = pdf_final_path
        db.commit()

        return {"status": "ok", "assinado_em": agora, "role": role}

    except HTTPException:
        raise
    except Exception as exc:
        print(f"Erro em submeter_assinatura: {_tb.format_exc()}")
        raise HTTPException(500, f"{type(exc).__name__}: {exc}")


# ── Verificação pública de assinatura ────────────────────────────────────────
@app.get("/verificar/{token}", response_class=HTMLResponse)
async def verificar_assinatura(token: str, db: Session = Depends(get_db)):
    c = db.query(Contrato).filter(Contrato.token == token).first()
    if not c:
        return HTMLResponse("<h1>Contrato não encontrado</h1>", status_code=404)

    d = json.loads(c.dados_contrato or "{}") if c.dados_contrato else {}
    nome = d.get("req_nome") or (c.lead.nome if c.lead else "-") or "-"
    cpf  = d.get("req_cpf")  or (c.lead.cpf  if c.lead else "-") or "-"
    assinado_em_br = _fmt_br(c.assinado_em, "%d/%m/%Y %H:%M:%S") or "-"
    criado_em_br   = _fmt_br(c.criado_em) or "-"

    status_badge = (
        '<span style="background:#16a34a;color:#fff;padding:.3rem .9rem;border-radius:99px;font-weight:700;font-size:.9rem">✅ ASSINADO</span>'
        if c.status == "assinado" else
        '<span style="background:#f59e0b;color:#fff;padding:.3rem .9rem;border-radius:99px;font-weight:700;font-size:.9rem">⏳ PENDENTE</span>'
    )

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Verificação de Assinatura — Fácil Financiamentos</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;background:#f4f6fb;color:#1a202c}}
  .header{{background:#0d2b4e;color:#fff;padding:1.25rem;text-align:center}}
  .header h1{{font-size:1.1rem;font-weight:700}}
  .header p{{font-size:.78rem;opacity:.65;margin-top:3px}}
  .container{{max-width:560px;margin:1.5rem auto;padding:0 1rem 2rem}}
  .card{{background:#fff;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.08);padding:1.5rem;margin-bottom:1rem}}
  .card h2{{font-size:.95rem;color:#0d2b4e;margin-bottom:1rem;display:flex;align-items:center;gap:.4rem}}
  .row{{display:flex;justify-content:space-between;align-items:flex-start;padding:.55rem 0;border-bottom:1px solid #f1f5f9;font-size:.85rem}}
  .row:last-child{{border-bottom:none}}
  .row .lbl{{color:#64748b;font-weight:500}}
  .row .val{{color:#1a202c;font-weight:600;text-align:right;max-width:60%;word-break:break-all}}
  .hash{{font-size:.68rem;color:#64748b;word-break:break-all;line-height:1.5;margin-top:.5rem;padding:.5rem;background:#f8fafc;border-radius:6px;border:1px solid #e2e8f0}}
  .status-area{{text-align:center;padding:.5rem 0 1rem}}
  .footer{{text-align:center;font-size:.72rem;color:#94a3b8;margin-top:1.5rem}}
</style>
</head>
<body>
<div class="header">
  <h1>Fácil Financiamentos</h1>
  <p>Verificação de Assinatura Eletrônica</p>
</div>
<div class="container">
  <div class="card">
    <div class="status-area">{status_badge}</div>
    <h2>📄 Dados do Documento</h2>
    <div class="row"><span class="lbl">Nº do documento</span><span class="val">{d.get("doc_id", c.id)}</span></div>
    <div class="row"><span class="lbl">Gerado em</span><span class="val">{criado_em_br}</span></div>
    <div class="row"><span class="lbl">Assinado em</span><span class="val">{assinado_em_br}</span></div>
    <div class="row"><span class="lbl">IP do assinante</span><span class="val">{c.ip_cliente or "-"}</span></div>
  </div>
  <div class="card">
    <h2>👤 Assinante</h2>
    <div class="row"><span class="lbl">Nome</span><span class="val">{nome.upper()}</span></div>
    <div class="row"><span class="lbl">CPF</span><span class="val">{cpf}</span></div>
  </div>
  <div class="card">
    <h2>🔒 Integridade</h2>
    <p style="font-size:.8rem;color:#64748b;margin-bottom:.5rem">Hash SHA-256 do documento original:</p>
    <div class="hash">{c.hash_doc}</div>
  </div>
  <div class="footer">
    Assinado eletronicamente nos termos da Lei 14.063/2020 e MP 2.200-2/2001.<br>
    Fácil Financiamentos · Rua Lauro Ignacio Ponte, 08, Sala 202 · BH/MG
  </div>
</div>
</body></html>"""
    return HTMLResponse(html)


@app.get("/api/leads/{lead_id}/contratos")
async def listar_contratos(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    contratos = db.query(Contrato).filter(Contrato.lead_id == lead_id).order_by(Contrato.criado_em.desc()).all()
    base_url = str(request.base_url).rstrip("/")
    return [
        {
            "id": c.id,
            "criado_em":       _fmt_br(c.criado_em) or "-",
            # Requerente
            "status_req":      c.status or "pendente",
            "assinado_req_em": _fmt_br(c.assinado_em),
            "link_req":        f"{base_url}/assinar/{c.token}",
            # Proprietário
            "status_prop":     c.status_prop or "pendente",
            "assinado_prop_em": _fmt_br(c.assinado_prop_em),
            "link_prop":       f"{base_url}/assinar/{c.token_prop}" if c.token_prop else None,
            # PDF disponível assim que qualquer parte assinar
            "pdf_id":          c.id if c.pdf_assinado else None,
            "ambos_assinaram": c.status == "assinado" and c.status_prop == "assinado",
        }
        for c in contratos
    ]


@app.get("/api/contratos/{contrato_id}/pdf")
async def baixar_pdf_assinado(
    contrato_id: int,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    c = db.query(Contrato).filter(Contrato.id == contrato_id).first()
    if not c:
        raise HTTPException(404, "Contrato não encontrado")
    if not c.pdf_assinado:
        raise HTTPException(400, "Contrato ainda não assinado")
    p = Path(c.pdf_assinado)
    if not p.exists():
        raise HTTPException(404, "Arquivo não encontrado")
    return FileResponse(str(p), media_type="application/pdf", filename=f"contrato_assinado_{contrato_id}.pdf")


# ─── Helpers ─────────────────────────────────────────────────────────────────────

def _parse_observacoes(raw: str | None) -> list:
    """Retorna observações como lista de dicts {texto, usuario, em}.
    Compatível com formato antigo (texto puro) e novo (JSON array)."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        # Era um objeto único, converte
        return [{"texto": str(parsed), "usuario": "—", "em": "—"}]
    except Exception:
        # Formato legado: texto puro → converte sem autor
        return [{"texto": raw, "usuario": "—", "em": "—"}]


def _serial_lead(l: Lead, db: Session) -> dict:
    responsavel = None
    if l.atribuido_para:
        u = db.query(Usuario).filter(Usuario.id == l.atribuido_para).first()
        if u:
            responsavel = {"id": u.id, "nome": u.nome}
    return {
        "id": l.id,
        "telefone": l.telefone,
        "nome": l.nome or "—",
        "cpf": l.cpf or "—",
        "data_nascimento": l.data_nascimento or "—",
        "carro_interesse": l.carro_interesse or "—",
        "modalidade": l.modalidade,
        "status": l.status,
        "estado_conversa": l.estado_conversa,
        "responsavel": responsavel,
        "assumido_em": _fmt_br(l.assumido_em),
        "criado_em": _fmt_br(l.criado_em) or "—",
        "atualizado_em": _fmt_br(l.atualizado_em) or "—",
        "observacoes": _parse_observacoes(l.observacoes),
        "origem": l.origem or "whatsapp",
        "origem_detalhe": l.origem_detalhe or "",
        "parceiro_id": l.parceiro_id,
        "parceiro_nome": l.parceiro.nome if l.parceiro else "",
        # Dados do contrato fechado
        "deal_data":     l.deal_data or "",
        "deal_veiculo":  l.deal_veiculo or "",
        "deal_retorno":  l.deal_retorno or "",
        "deal_valor":    l.deal_valor or "",
        "deal_comissao": l.deal_comissao or "",
        "deal_banco":      l.deal_banco or "",
        "deal_conta_pg":   l.deal_conta_pg or "",
        "deal_operadora":  l.deal_operadora or "",
        # Dados extras p/ requerimento
        "dados_contrato": json.loads(l.dados_contrato) if l.dados_contrato else {},
        # Perfil do cliente
        "cidade":   l.cidade   or "",
        "renda":    l.renda    or "",
        "profissao": l.profissao or "",
        "tem_cnh":  l.tem_cnh,   # None=não informado | True=sim | False=não
        "oculto_funil": bool(l.oculto_funil),
    }


def _serial_usuario(u: Usuario) -> dict:
    return {
        "id": u.id, "nome": u.nome, "email": u.email,
        "role": u.role, "ativo": u.ativo,
        "criado_em": _fmt_br(u.criado_em, "%d/%m/%Y") or "—",
    }


def _criar_config_padrao(db: Session):
    """Cria configurações padrão do bot se não existirem."""
    configs_padrao = [
        {
            "chave": "regras_financiamento",
            "descricao": "Regras específicas para leads de financiamento de veículo",
            "valor": (
                "REGRAS PARA FINANCIAMENTO:\n"
                "- O fechamento do contrato é PRESENCIAL em Belo Horizonte/MG (exigência do banco)\n"
                "- Após o cliente informar a cidade, verifique se está a até 200km de BH\n"
                "- Cidades dentro de ~200km de BH: Contagem, Betim, Sete Lagoas, Montes Claros, Governador Valadares, Ipatinga, Coronel Fabriciano, Juiz de Fora, Uberlândia, Uberaba, Divinópolis, Itabira, João Monlevade, Conselheiro Lafaiete, Ouro Preto, Poços de Caldas, Pouso Alegre, Varginha, Lavras, São João del-Rei, Barbacena, Viçosa, Muriaé\n"
                "- Se o cliente for de cidade FORA desse raio, pergunte: 'Para o financiamento, o fechamento do contrato é feito presencialmente aqui em BH (exigência do banco). Você teria disponibilidade de vir até nós?'\n"
                "- Se o cliente NÃO puder vir: informe gentilmente que para financiamento precisamos do fechamento presencial e sugira o refinanciamento caso ele já tenha um veículo. Marque como desqualificado.\n"
                "- Se o cliente PUDER vir: continue normalmente com a coleta de dados"
            ),
        },
        {
            "chave": "regras_refinanciamento",
            "descricao": "Regras específicas para leads de refinanciamento/CGI",
            "valor": (
                "REGRAS PARA REFINANCIAMENTO:\n"
                "- Atendimento pode ser 100% ONLINE, de qualquer lugar do Brasil\n"
                "- Não há restrição geográfica\n"
                "- O cliente pode ter o veículo quitado ou semi-quitado\n"
                "- Continue normalmente com a coleta de dados independente da cidade"
            ),
        },
        {
            "chave": "mensagem_boas_vindas",
            "descricao": "Mensagem inicial de boas-vindas (enviada quando o cliente entra em contato)",
            "valor": (
                "Olá, seja bem vindo a Fácil Financiamentos. Meu nome é Maria e sou sua atendente virtual, estou aqui para ajudá-lo!\n\n"
                "Qual o seu nome?"
            ),
        },
        {
            "chave": "mensagem_finalizacao",
            "descricao": "Mensagem de encerramento após coletar todos os dados",
            "valor": "Obrigado pelas confirmações, em breve uma de nossas consultoras, entrará em contato. 🤝",
        },
        {
            "chave": "mensagem_followup",
            "descricao": "1º follow-up: enviado após X horas sem resposta do cliente",
            "valor": "Oi! 😊 Vi que nossa conversa ficou parada...\nQuando quiser continuar, estou aqui! Gostaria de retomar?",
        },
        {
            "chave": "mensagem_followup_2",
            "descricao": "2º follow-up: enviado 24h após o 1º sem resposta",
            "valor": "Olá! 👋 Passando para saber se ainda tem interesse em financiar ou refinanciar seu veículo.\nEstamos com ótimas condições e podemos te ajudar! Me conta, ficou alguma dúvida?",
        },
        {
            "chave": "mensagem_followup_3",
            "descricao": "3º follow-up: última tentativa, enviado 48h após o 2º. Após isso o lead é marcado como Perdido.",
            "valor": "Oi! Última tentativa de contato por aqui. 😊\nSe mudar de ideia sobre o financiamento ou refinanciamento, pode nos chamar a qualquer momento!\nFicamos à disposição. 🤝",
        },
        {
            "chave": "followup_horas",
            "descricao": "Horas de inatividade antes de enviar o 1º follow-up (padrão: 4)",
            "valor": "4",
        },
        {
            "chave": "meta_contratos",
            "descricao": "Meta de contratos fechados no mês (número inteiro)",
            "valor": "20",
        },
    ]

    for c in configs_padrao:
        existe = db.query(Configuracao).filter(Configuracao.chave == c["chave"]).first()
        if not existe:
            db.add(Configuracao(chave=c["chave"], valor=c["valor"], descricao=c["descricao"]))
    db.commit()


def _salvar_msg_webhook(db: Session, telefone: str, texto: str, role: str = "user"):
    from models import MensagemConversa
    msg = MensagemConversa(telefone=telefone, role=role, conteudo=texto)
    db.add(msg)
    db.commit()


async def _notificar_equipe(telefone: str, db: Session):
    resumo = obter_resumo_lead(telefone, db)
    if resumo:
        print("\n🎉 NOVO LEAD QUALIFICADO!")
        print(json.dumps(resumo, ensure_ascii=False, indent=2))
        print("─" * 40)
