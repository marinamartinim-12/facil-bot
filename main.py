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
from models import Lead, MensagemConversa, Usuario, Configuracao, Contrato, criar_tabelas, get_db, StatusLeadEnum, RoleEnum, EstadoConversaEnum
from bot import processar_mensagem, obter_resumo_lead
from auth import verificar_senha, hash_senha, criar_token, obter_usuario_atual, requer_admin

settings = get_settings()
app = FastAPI(title="Fácil Financiamentos", version="2.0.0")


# ─── Follow-up automático ───────────────────────────────────────────────────────

# Estados do bot onde o lead ainda não completou os dados para o consultor
_ESTADOS_BOT_ATIVO = [
    EstadoConversaEnum.aguardando_nome,
    EstadoConversaEnum.coletando_cidade,
    EstadoConversaEnum.aguardando_modalidade,
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

        for lead in leads:
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
            print(f"📨 Follow-up enviado para {lead.telefone}")

    except Exception as e:
        print(f"❌ Erro no follow-up: {e}")
    finally:
        db.close()


async def _loop_followup():
    """Roda a cada 30 minutos verificando leads parados."""
    await asyncio.sleep(60)  # aguarda 1 min após startup
    while True:
        print("🔍 Verificando leads para follow-up…")
        await _enviar_followups()
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

    # Migração: adiciona coluna followup_em se não existir
    try:
        from sqlalchemy import text
        with db_startup.bind.connect() as conn:
            conn.execute(text("ALTER TABLE leads ADD COLUMN followup_em DATETIME"))
            conn.commit()
        print("✅ Coluna followup_em adicionada")
    except Exception:
        pass  # Coluna já existe

    # Configurações padrão do bot
    _criar_config_padrao(db_startup)
    db_startup.close()

    # Inicia tarefa de follow-up automático
    asyncio.create_task(_loop_followup())

    print("✅ Fácil Financiamentos Bot v2 iniciado!")
    print(f"🗄️  Banco: {settings.DATABASE_URL}")
    print("📊 Dashboard: http://localhost:8000/dashboard")


# ─── Autenticação ────────────────────────────────────────────────────────────────

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
    return {"id": usuario.id, "nome": usuario.nome, "email": usuario.email, "role": usuario.role}


@app.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token")
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


# ─── API Leads ───────────────────────────────────────────────────────────────────

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
    from gerar_contrato import gerar_pdf_contrato, salvar_pdf, CONTRATOS_DIR
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, "Lead não encontrado")

    lead_data = {
        "nome": lead.nome, "cpf": lead.cpf,
        "data_nascimento": lead.data_nascimento,
        "telefone": lead.telefone,
        "carro_interesse": lead.carro_interesse,
        "modalidade": lead.modalidade,
    }
    pdf_bytes, hash_doc = gerar_pdf_contrato(lead_data)
    token = secrets.token_hex(32)
    nome_arquivo = f"contrato_{lead_id}_{token[:8]}.pdf"
    caminho = salvar_pdf(pdf_bytes, nome_arquivo)

    contrato = Contrato(
        lead_id=lead_id,
        criado_por_id=usuario.id,
        token=token,
        hash_doc=hash_doc,
        pdf_original=caminho,
        status="pendente",
    )
    db.add(contrato)
    db.commit()
    db.refresh(contrato)

    base_url = str(request.base_url).rstrip("/")
    link = f"{base_url}/assinar/{token}"
    return {"contrato_id": contrato.id, "link": link, "hash": hash_doc}


@app.get("/assinar/{token}", response_class=HTMLResponse)
async def pagina_assinar(token: str):
    caminho = Path("templates/assinar.html")
    return HTMLResponse(caminho.read_text(encoding="utf-8"))


@app.get("/assinar/{token}/conteudo")
async def conteudo_contrato(token: str, db: Session = Depends(get_db)):
    contrato = db.query(Contrato).filter(Contrato.token == token).first()
    if not contrato:
        raise HTTPException(404, "Contrato não encontrado")
    if contrato.status == "assinado":
        raise HTTPException(410, "Contrato já assinado")

    # Lê o PDF e extrai texto simples do lead para exibir na tela
    lead = contrato.lead
    modalidade = (lead.modalidade or "indefinido").lower()
    tipo = "Refinanciamento de Veículo" if "refin" in modalidade else "Financiamento de Veículo"

    texto = (
        f"TERMO DE PRESTAÇÃO DE SERVIÇOS — {tipo.upper()}\n"
        f"Fácil Financiamentos · Belo Horizonte, MG\n\n"
        f"DADOS DO CLIENTE\n"
        f"Nome: {lead.nome or '—'}\n"
        f"CPF: {lead.cpf or '—'}\n"
        f"Data de nascimento: {lead.data_nascimento or '—'}\n"
        f"Telefone: {lead.telefone}\n\n"
        f"SERVIÇO CONTRATADO\n"
        f"Modalidade: {tipo}\n"
        f"Veículo: {lead.carro_interesse or '—'}\n"
        f"Data: {datetime.now().strftime('%d/%m/%Y')}\n\n"
        f"OBJETO DO CONTRATO\n"
        + (
            "A Fácil Financiamentos obriga-se a prestar serviços de intermediação para obtenção de crédito "
            "mediante refinanciamento de veículo automotor de propriedade do contratante, junto às instituições "
            "financeiras credenciadas, nas melhores condições de taxas e prazos disponíveis. O processo ocorre "
            "100% de forma digital."
            if "refin" in modalidade else
            "A Fácil Financiamentos obriga-se a prestar serviços de intermediação para aquisição de veículo "
            "automotor novo ou usado de terceiros (particular para particular), junto às 9 melhores instituições "
            "financeiras credenciadas do Brasil, buscando as melhores taxas e condições de parcelamento."
        ) + "\n\n"
        f"PROTEÇÃO DE DADOS — LGPD\n"
        f"Os dados pessoais fornecidos serão utilizados exclusivamente para análise de crédito e intermediação "
        f"contratual, em conformidade com a Lei 13.709/2018 (LGPD).\n\n"
        f"ASSINATURA ELETRÔNICA\n"
        f"Este documento será assinado eletronicamente com validade jurídica nos termos da Lei 14.063/2020. "
        f"O registro de IP, geolocalização, horário e selfie constituem prova de autenticidade.\n\n"
        f"Hash do documento (SHA-256):\n{contrato.hash_doc}"
    )
    return {"texto": texto, "hash": contrato.hash_doc}


@app.post("/assinar/{token}")
async def submeter_assinatura(token: str, request: Request, db: Session = Depends(get_db)):
    from gerar_contrato import base64_para_imagem, gerar_pdf_assinado, salvar_pdf, CONTRATOS_DIR
    from pathlib import Path as Pt

    contrato = db.query(Contrato).filter(Contrato.token == token).first()
    if not contrato:
        raise HTTPException(404, "Contrato não encontrado")
    if contrato.status == "assinado":
        raise HTTPException(410, "Contrato já foi assinado")

    body = await request.json()
    selfie_b64  = body.get("selfie", "")
    assin_b64   = body.get("assinatura", "")
    geo         = body.get("geo", "")
    ip          = request.client.host if request.client else "desconhecido"

    base = CONTRATOS_DIR / f"contrato_{contrato.lead_id}_{token[:8]}"
    selfie_path  = str(base) + "_selfie.jpg"
    assin_path   = str(base) + "_assinatura.png"

    base64_para_imagem(selfie_b64, Pt(selfie_path))
    base64_para_imagem(assin_b64,  Pt(assin_path))

    # Gera PDF de auditoria
    agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    lead  = contrato.lead
    audit_bytes = gerar_pdf_assinado(
        pdf_original_bytes=Pt(contrato.pdf_original).read_bytes() if contrato.pdf_original else b"",
        selfie_path=selfie_path,
        assinatura_path=assin_path,
        dados_auditoria={
            "assinado_em": agora,
            "ip": ip,
            "geo": geo or "não fornecida",
            "hash_doc": contrato.hash_doc,
            "nome": lead.nome or "—",
            "cpf": lead.cpf or "—",
        },
    )
    audit_path = str(base) + "_auditoria.pdf"
    Pt(audit_path).write_bytes(audit_bytes)

    contrato.status          = "assinado"
    contrato.selfie_path     = selfie_path
    contrato.assinatura_path = assin_path
    contrato.pdf_assinado    = audit_path
    contrato.ip_cliente      = ip
    contrato.geolocalizacao  = geo
    contrato.assinado_em     = datetime.utcnow()
    db.commit()

    return {"status": "ok", "assinado_em": agora}


@app.get("/api/leads/{lead_id}/contratos")
async def listar_contratos(
    lead_id: int,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    contratos = db.query(Contrato).filter(Contrato.lead_id == lead_id).order_by(Contrato.criado_em.desc()).all()
    return [
        {
            "id": c.id,
            "status": c.status,
            "criado_em": c.criado_em.strftime("%d/%m/%Y %H:%M") if c.criado_em else "—",
            "assinado_em": c.assinado_em.strftime("%d/%m/%Y %H:%M") if c.assinado_em else None,
            "link": f"/assinar/{c.token}",
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
    if c.status != "assinado" or not c.pdf_assinado:
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
