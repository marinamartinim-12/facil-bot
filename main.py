"""
Fácil Financiamentos — Servidor principal v2
FastAPI + Webhook Z-API + Dashboard com login
"""

import asyncio
import json
import os
import re
import httpx
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Depends, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, FileResponse
import secrets
from pathlib import Path
from sqlalchemy.orm import Session

from config import get_settings
from models import Lead, MensagemConversa, Usuario, Configuracao, Contrato, Parceiro, ContatoParceiro, SessaoUsuario, criar_tabelas, get_db, StatusLeadEnum, ModalidadeEnum, RoleEnum, EstadoConversaEnum
from bot import processar_mensagem, obter_resumo_lead
from auth import verificar_senha, hash_senha, criar_token, obter_usuario_atual, requer_admin

settings = get_settings()
app = FastAPI(title="Fácil Financiamentos", version="2.0.0")


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
        config_horas = db.query(Configuracao).filter(Configuracao.chave == "followup_horas").first()
        horas = int(config_horas.valor) if config_horas and config_horas.valor.isdigit() else 4
        limite = datetime.utcnow() - timedelta(hours=horas)

        config_msg = db.query(Configuracao).filter(Configuracao.chave == "mensagem_followup").first()
        texto_padrao = (
            "Oi! 😊 Vi que nossa conversa ficou parada...\n"
            "Quando quiser continuar, estou aqui! Gostaria de retomar?"
        )
        texto_base = config_msg.valor if config_msg else texto_padrao

        # ── 3. Leads ainda no fluxo do bot (dados incompletos) ───────────────
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

                # Nunca mandou mensagem, ou mandou recentemente → pula
                if not ultima_user or ultima_user.criado_em > limite:
                    continue

                # Já enviou follow-up depois da última mensagem do usuário → pula
                if lead.followup_em and lead.followup_em > ultima_user.criado_em:
                    continue

                # Personaliza com nome se disponível
                nome = f" {lead.nome}" if lead.nome else ""
                texto = texto_base.replace("{nome}", nome.strip()).replace("Oi!", f"Oi{nome}!")

                await enviar_zapi(lead.telefone, texto)
                _salvar_msg_webhook(db, lead.telefone, texto, role="assistant")
                lead.followup_em = datetime.utcnow()
                db.commit()
                enviados += 1
                print(f"📨 Follow-up enviado para {lead.telefone} (lead #{lead.id})")

            except Exception as e_lead:
                print(f"⚠️ Erro ao enviar follow-up para lead #{lead.id}: {e_lead}")
                db.rollback()   # garante que o próximo lead começa limpo

        if enviados:
            print(f"✅ Follow-ups enviados: {enviados}")
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
    """Atualiza último momento ativo do usuário (chamado a cada 60s pelo frontend)."""
    sid = request.cookies.get("sessao_id")
    if sid:
        try:
            sessao = db.query(SessaoUsuario).filter(SessaoUsuario.id == int(sid)).first()
            if sessao:
                sessao.ultimo_ativo_em = datetime.utcnow()
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

@app.post("/webhook/zapi")
async def receber_webhook_zapi(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    try:
        if body.get("fromMe"):
            return JSONResponse({"status": "ignored"})
        telefone = body.get("phone", "").replace("+", "").replace(" ", "")
        texto = body.get("text", {}).get("message", "").strip()
        if not telefone or not texto:
            return JSONResponse({"status": "ignored"})

        lead = db.query(Lead).filter(Lead.telefone == telefone).first()

        # Se já foi assumido por funcionário, só salva a mensagem — humano responde
        if lead and lead.status in [
            StatusLeadEnum.assumido,
            StatusLeadEnum.proposta_enviada,
            StatusLeadEnum.fechado,
            StatusLeadEnum.perdido,
        ]:
            _salvar_msg_webhook(db, telefone, texto)
            return JSONResponse({"status": "aguardando_humano"})

        # Bot ainda está qualificando
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
        "ativo": p.ativo,
        "criado_em": p.criado_em.strftime("%d/%m/%Y") if p.criado_em else "",
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
    p = Parceiro(
        nome=nome,
        data_nascimento=body.get("data_nascimento", "").strip() or None,
        cpf=cpf,
        telefone=tel_norm or telefone,
        telefones_extras=json.dumps(extras, ensure_ascii=False) if extras else None,
        email=body.get("email", "").strip() or None,
        observacoes=body.get("observacoes", "").strip() or None,
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
    p.nome             = nome
    p.data_nascimento  = body.get("data_nascimento", "").strip() or None
    p.cpf              = cpf
    p.telefone         = tel_norm
    p.telefones_extras = json.dumps(extras, ensure_ascii=False) if extras else None
    p.email            = body.get("email", "").strip() or None
    p.observacoes      = body.get("observacoes", "").strip() or None

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
            "em": datetime.utcnow().strftime("%d/%m/%Y %H:%M"),
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
                "horario": m.criado_em.strftime("%d/%m/%Y %H:%M") if m.criado_em else "",
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
        StatusLeadEnum.fechado,
        StatusLeadEnum.perdido,
    ]
    if novo_status not in [s.value for s in estagios_validos]:
        raise HTTPException(status_code=400, detail="Estágio inválido")

    # Apenas administradores podem marcar como perdido ou desqualificado
    restritos = [StatusLeadEnum.perdido.value, StatusLeadEnum.desqualificado.value]
    if novo_status in restritos and usuario.role != RoleEnum.admin:
        raise HTTPException(status_code=403, detail="Apenas administradores podem remover leads do funil.")

    lead.status = novo_status
    if novo_status == StatusLeadEnum.assumido and not lead.atribuido_para:
        lead.atribuido_para = usuario.id
        lead.assumido_em = datetime.utcnow()
    lead.atualizado_em = datetime.utcnow()
    db.commit()
    db.refresh(lead)
    return _serial_lead(lead, db)


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

    # Salva e envia
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

    # Salva no histórico como mensagem do atendente
    _salvar_msg_webhook(db, lead.telefone, f"[{usuario.nome}]: {texto}", role="assistant")

    # Envia pelo WhatsApp
    await enviar_zapi(lead.telefone, texto)

    return {"status": "enviado"}


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
        # Limpa prefixo [Nome]: para exibição
        import re
        conteudo_limpo = re.sub(r'^\[[^\]]+\]:\s*', '', ultima.conteudo)
        resultado.append({
            **_serial_lead(l, db),
            "ultima_mensagem": conteudo_limpo[:60],
            "ultima_hora": ultima.criado_em.strftime("%H:%M") if ultima and ultima.criado_em else "",
        })
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
    campos_editaveis = ["nome", "cpf", "data_nascimento", "carro_interesse", "modalidade", "observacoes"]
    for campo in campos_editaveis:
        if campo in body:
            valor = body[campo]
            if campo == "cpf" and valor:
                valor = re.sub(r"[^\d]", "", valor)
                if len(valor) == 11:
                    valor = f"{valor[:3]}.{valor[3:6]}.{valor[6:9]}-{valor[9:]}"
            setattr(lead, campo, valor if valor != "" else None)
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


@app.get("/api/relatorio/sessoes")
async def relatorio_sessoes(
    db: Session = Depends(get_db),
    admin: Usuario = Depends(requer_admin),
):
    """Retorna histórico de sessões de login de todos os usuários (admin only)."""
    sessoes = (
        db.query(SessaoUsuario)
        .order_by(SessaoUsuario.login_em.desc())
        .limit(500)
        .all()
    )
    resultado = []
    for s in sessoes:
        # Tempo ativo = diferença entre último heartbeat e login
        fim = s.logout_em or s.ultimo_ativo_em
        tempo_s = max(0, int((fim - s.login_em).total_seconds())) if fim and s.login_em else 0
        resultado.append({
            "id": s.id,
            "usuario": s.usuario.nome if s.usuario else "—",
            "role": s.usuario.role if s.usuario else "—",
            "ip": s.ip or "—",
            "localizacao": s.localizacao or "—",
            "login_em": s.login_em.strftime("%d/%m/%Y %H:%M") if s.login_em else "—",
            "ultimo_ativo_em": s.ultimo_ativo_em.strftime("%d/%m/%Y %H:%M") if s.ultimo_ativo_em else "—",
            "logout_em": s.logout_em.strftime("%d/%m/%Y %H:%M") if s.logout_em else None,
            "tempo_ativo": _duracao_str(tempo_s),
            "ativa": s.logout_em is None,
        })
    return resultado


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
            datetime.now().strftime("%d de %B de %Y")
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
        agora          = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

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
    assinado_em_br = c.assinado_em.strftime("%d/%m/%Y %H:%M:%S") if c.assinado_em else "-"
    criado_em_br   = c.criado_em.strftime("%d/%m/%Y %H:%M") if c.criado_em else "-"

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
            "criado_em":       c.criado_em.strftime("%d/%m/%Y %H:%M") if c.criado_em else "-",
            # Requerente
            "status_req":      c.status or "pendente",
            "assinado_req_em": c.assinado_em.strftime("%d/%m/%Y %H:%M") if c.assinado_em else None,
            "link_req":        f"{base_url}/assinar/{c.token}",
            # Proprietário
            "status_prop":     c.status_prop or "pendente",
            "assinado_prop_em": c.assinado_prop_em.strftime("%d/%m/%Y %H:%M") if c.assinado_prop_em else None,
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
        "assumido_em": l.assumido_em.strftime("%d/%m/%Y %H:%M") if l.assumido_em else None,
        "criado_em": l.criado_em.strftime("%d/%m/%Y %H:%M") if l.criado_em else "—",
        "atualizado_em": l.atualizado_em.strftime("%d/%m/%Y %H:%M") if l.atualizado_em else "—",
        "observacoes": _parse_observacoes(l.observacoes),
        "origem": l.origem or "whatsapp",
        "origem_detalhe": l.origem_detalhe or "",
        "parceiro_id": l.parceiro_id,
        "parceiro_nome": l.parceiro.nome if l.parceiro else "",
    }


def _serial_usuario(u: Usuario) -> dict:
    return {
        "id": u.id, "nome": u.nome, "email": u.email,
        "role": u.role, "ativo": u.ativo,
        "criado_em": u.criado_em.strftime("%d/%m/%Y") if u.criado_em else "—",
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
            "descricao": "Mensagem enviada automaticamente após 4h sem resposta do cliente",
            "valor": "Oi! 😊 Vi que nossa conversa ficou parada...\nQuando quiser continuar, estou aqui! Gostaria de retomar?",
        },
        {
            "chave": "followup_horas",
            "descricao": "Horas de inatividade antes de enviar o follow-up (padrão: 4)",
            "valor": "4",
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
