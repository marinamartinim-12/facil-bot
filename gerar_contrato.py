"""
Fácil Financiamentos — Geração de contratos PDF
Estilo DocuSign: assinaturas sobrepostas nas posições corretas do documento.
"""

import hashlib
import base64
import os
from datetime import datetime
from io import BytesIO
from pathlib import Path

from fpdf import FPDF
from PIL import Image


# ─── Diretório de contratos ───────────────────────────────────────────────────
def _resolver_contratos_dir():
    candidatos = [
        os.getenv("CONTRATOS_DIR"),
        "/data/contratos",
        "/app/contratos",
    ]
    for c in candidatos:
        if not c:
            continue
        p = Path(c)
        try:
            p.mkdir(parents=True, exist_ok=True)
            print(f"📁 CONTRATOS_DIR: {p}")
            return p
        except Exception as exc:
            print(f"⚠️ Não foi possível criar {c}: {exc}")
    import tempfile
    p = Path(tempfile.gettempdir()) / "contratos"
    p.mkdir(parents=True, exist_ok=True)
    print(f"📁 CONTRATOS_DIR (fallback tmp): {p}")
    return p


CONTRATOS_DIR = _resolver_contratos_dir()

NAVY = (13, 43, 78)
GOLD = (200, 155, 0)

# ─── Posições FIXAS das caixas de assinatura (página 3, índice 2) ─────────────
SIG_PAGE_IDX = 2       # 0-based
SIG_REQ_X    = 26.0    # Requerente
SIG_REQ_Y    = 76.0
SIG_PROP_X   = 110.0   # Proprietário
SIG_PROP_Y   = 76.0
SIG_W        = 74.0
SIG_H        = 44.0

LOGO_PATH = Path(__file__).parent / "static" / "logo.png"   # coloque aqui a logo


# ─────────────────────────────────────────────────────────────────────────────
class RequerimentoPDF(FPDF):

    def __init__(self, doc_id=""):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.doc_id = doc_id
        self.set_auto_page_break(auto=True, margin=28)
        self.set_margins(25, 15, 25)

    # ── Header ────────────────────────────────────────────────────────────────
    def header(self):
        if LOGO_PATH.exists():
            # Logo proporcional: imagem 2341x1094 → ratio ≈ 2.14
            logo_h = 14.0
            logo_w = round(logo_h * 2.14, 1)   # ≈ 30mm
            self.image(str(LOGO_PATH), x=25, y=5, w=logo_w, h=logo_h)
            txt_x = 25 + logo_w + 4
        else:
            self.set_fill_color(*NAVY)
            self.ellipse(18, 5, 14, 14, "F")
            self.set_font("Helvetica", "B", 10)
            self.set_text_color(255, 255, 255)
            self.set_xy(20, 9)
            self.cell(10, 5, "F", align="C")
            txt_x = 35.0

        # Endereço ao lado da logo
        self.set_font("Helvetica", "", 7)
        self.set_text_color(100, 110, 125)
        self.set_xy(txt_x, 10)
        self.cell(0, 4,
                  "Av. Vilarinho, 1560, sala 202 (Sobreloja do Varejao das Tintas)",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_x(txt_x)
        self.cell(0, 4, "Venda Nova - Belo Horizonte / MG",
                  new_x="LMARGIN", new_y="NEXT")

        # Linha dourada
        self.set_draw_color(*GOLD)
        self.set_line_width(0.8)
        self.line(25, 22, self.w - 25, 22)
        self.set_line_width(0.2)
        self.set_draw_color(180, 180, 180)
        self.ln(8)

    # ── Footer ────────────────────────────────────────────────────────────────
    def footer(self):
        self.set_y(-15)
        self.set_draw_color(200, 200, 200)
        self.set_line_width(0.3)
        self.line(25, self.get_y(), self.w - 25, self.get_y())
        self.ln(1.5)
        self.set_font("Helvetica", "", 6.5)
        self.set_text_color(130, 130, 130)
        self.cell(
            0, 4,
            f"Facil Financiamentos  |  Av. Vilarinho, 1560, sala 202 - Venda Nova - BH/MG  |  Doc: {self.doc_id}  |  Pag {self.page_no()}",
            align="C",
        )

    # ── Helpers ───────────────────────────────────────────────────────────────
    def titulo(self, texto):
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(*NAVY)
        self.cell(0, 8, texto, align="C", new_x="LMARGIN", new_y="NEXT")

    def subtitulo(self, texto):
        self.set_font("Helvetica", "I", 10)
        self.set_text_color(80, 80, 80)
        self.cell(0, 6, texto, align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

    def para(self, texto, bold=False, align="J", after=4.5):
        """Parágrafo justificado."""
        self.set_font("Helvetica", "B" if bold else "", 10)
        self.set_text_color(30, 30, 30)
        self.multi_cell(0, 6.5, str(texto), align=align)
        if after:
            self.ln(after)

    def destaque(self, texto):
        """Texto em destaque centralizado em caixa alta bold navy."""
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*NAVY)
        self.multi_cell(0, 7, texto, align="C")
        self.ln(3)

    def campo(self, rotulo, valor):
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(30, 30, 30)
        self.write(6.5, rotulo + ": ")
        self.set_font("Helvetica", "", 10)
        self.write(6.5, str(valor or "-"))
        self.ln(7)

    def campo_valor(self, rotulo, valor):
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(30, 30, 30)
        self.write(6.5, rotulo + ": ")
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*NAVY)
        self.write(6.5, str(valor or "-"))
        self.ln(8)

    def linha_sep(self, after=6):
        self.set_draw_color(220, 220, 220)
        self.set_line_width(0.3)
        self.line(25, self.get_y(), self.w - 25, self.get_y())
        self.ln(after)


# ─────────────────────────────────────────────────────────────────────────────
def _v(d, chave, padrao=""):
    val = d.get(chave)
    if val is None or str(val).strip() == "":
        return str(padrao)
    return str(val)


# ─── Página 1: Requerimento ───────────────────────────────────────────────────
def _pagina_requerimento(pdf, d):
    pdf.add_page()
    pdf.titulo("REQUERIMENTO DE INTERMEDIACAO")
    pdf.subtitulo("Prestacao de Servico")

    modalidade = _v(d, "modalidade", "refinanciamento").lower()
    verbo = "refinanciamento" if "refin" in modalidade else "financiamento da aquisicao"

    # Endereço do requerente
    partes = []
    for k, prefix in [("req_rua",""), ("req_numero","N."), ("req_bairro","Bairro "), ("req_cep","CEP "), ("req_cidade","")]:
        v = _v(d, k)
        if v:
            partes.append((prefix + (v.upper() if k in ("req_bairro","req_cidade") else v)))
    endereco = " ".join(partes).strip()

    # Parágrafo 1 — identificação
    cel = _v(d, "req_celular")
    p1 = (
        f"Eu {_v(d,'req_nome').upper()}, CPF {_v(d,'req_cpf')}, "
        f"RG {_v(d,'req_rg')}, residente na {endereco}"
        + (f", CELULAR {cel}." if cel else ".")
    )
    pdf.para(p1)

    # Parágrafo 2 — veículo
    p2 = (
        f"Requeiro que seja INTERMEDIADO o {verbo} do veiculo de marca "
        f"{_v(d,'vei_modelo').upper()}, Placa {_v(d,'vei_placa').upper()}, "
        f"Ano {_v(d,'vei_ano')}, Cor {_v(d,'vei_cor').upper()}, "
        f"RENAVAM {_v(d,'vei_renavam')}, CHASSI {_v(d,'vei_chassi').upper()}, "
        f"adquirido fruto de negociacao direta com o seu legitimo proprietario/representante."
    )
    pdf.para(p2)

    # Parágrafo 3 — proprietário
    p3 = (
        f"O Sr. {_v(d,'prop_nome').upper()}, CPF {_v(d,'prop_cpf')}, "
        f"Telefone {_v(d,'prop_telefone')}, que se responsabiliza civil e criminalmente "
        f"pelo mesmo, inclusive pela documentacao apresentada."
    )
    pdf.para(p3)

    # Parágrafo 4 — condições financeiras
    p4 = (
        f"O valor LIQUIDO liberado sera de R$ {_v(d,'fin_valor_liquido')}, "
        f"ja descontadas todas as despesas, consultoria, comissoes, taxas, impostos "
        f"e intermediacao, divididos em {_v(d,'fin_parcelas')} x de "
        f"R$ {_v(d,'fin_valor_parcela')}, 1o vencimento em {_v(d,'fin_vencimento')}."
    )
    pdf.para(p4)

    # Parágrafo 5 — isenção
    p5 = (
        f"Neste ato o requerente que NAO adquiriu o veiculo junto a empresa, "
        f"sendo que a mesma nao se responsabiliza pela documentacao e qualidade do mesmo."
    )
    pdf.para(p5)
    pdf.ln(3)

    # Declaração em destaque
    pdf.destaque(
        "DECLARO AINDA, QUE NADA MAIS ME FOI PROMETIDO ALEM DO QUE\n"
        "ESTA ESPECIFICADO NESTE REQUERIMENTO."
    )


# ─── Página 2: Dados da Conta ─────────────────────────────────────────────────
def _pagina_pagamento(pdf, d):
    pdf.add_page()
    pdf.titulo("DADOS DA CONTA PARA PAGAMENTO")
    pdf.subtitulo("Autorizacao de Pagamento")

    modelo    = _v(d, "vei_modelo").upper()
    placa     = _v(d, "vei_placa").upper()
    ano       = _v(d, "vei_ano")
    cor       = _v(d, "vei_cor").upper()
    banco_neg = _v(d, "fin_banco").upper()

    p1 = (
        f"Eu {_v(d,'req_nome').upper()}, CPF {_v(d,'req_cpf')}, "
        f"RG {_v(d,'req_rg')}."
    )
    pdf.para(p1)

    p2 = (
        f"Autorizo o pagamento da importancia de R$ {_v(d,'pag_valor')}, "
        f"referente ao refinanciamento do veiculo de marca {modelo}, "
        f"Placa {placa}, Ano {ano}, Cor {cor}, "
        f"negociado junto ao banco {banco_neg}, na conta abaixo discriminada."
    )
    pdf.para(p2)
    pdf.ln(4)

    pdf.linha_sep(after=5)

    for rotulo, chave in [
        (" Nome",    "pag_nome_beneficiario"),
        (" CPF",     "pag_cpf_beneficiario"),
        (" Banco",   "pag_banco"),
        (" Agencia", "pag_agencia"),
        (" Conta",   "pag_conta"),
        (" PIX",     "pag_pix"),
    ]:
        pdf.set_x(30)
        val = _v(d, chave)
        pdf.campo(rotulo, val.upper() if chave in ("pag_nome_beneficiario","pag_banco") else val)

    pdf.set_x(30)
    pdf.campo_valor(" VALOR TOTAL", "R$ " + _v(d, "pag_valor"))


# ─── Página 3: Assinaturas (posição FIXA) ────────────────────────────────────
def _pagina_assinaturas(pdf, d):
    pdf.add_page()

    # Título e data
    pdf.titulo("ASSINATURAS")
    data_str = _v(d, "data_contrato", datetime.now().strftime("%d de %B de %Y"))
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(60, 60, 60)
    pdf.cell(0, 6, f"Belo Horizonte, {data_str}.", align="C",
             new_x="LMARGIN", new_y="NEXT")

    # Linha dourada separadora
    pdf.ln(5)
    pdf.set_draw_color(*GOLD)
    pdf.set_line_width(0.6)
    pdf.line(25, pdf.get_y(), pdf.w - 25, pdf.get_y())
    pdf.ln(6)

    # Correspondente (se houver)
    corr = _v(d, "correspondente_nome")
    if corr:
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(*NAVY)
        pdf.cell(0, 7, "CORRESPONDENTE", align="C",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    # ── Caixas de assinatura em posições FIXAS ────────────────────────────────
    # Forçamos o Y absoluto para garantir que o overlay bata certeiro
    pdf.set_y(SIG_REQ_Y)

    req_nome  = _v(d, "req_nome").upper()
    prop_nome = _v(d, "prop_nome").upper()

    for (sx, nome_sig, label) in [
        (SIG_REQ_X,  req_nome,  "Requerente"),
        (SIG_PROP_X, prop_nome, "Proprietario / Vendedor"),
    ]:
        # Caixa
        pdf.set_fill_color(248, 251, 255)
        pdf.set_draw_color(180, 195, 215)
        pdf.set_line_width(0.35)
        pdf.rect(sx, SIG_REQ_Y, SIG_W, SIG_H, "DF")

        # Placeholder "Assine aqui"
        pdf.set_xy(sx, SIG_REQ_Y + SIG_H / 2 - 4)
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(185, 195, 210)
        pdf.cell(SIG_W, 8, "Assine aqui", align="C")

        # Linha horizontal abaixo da caixa
        y_line = SIG_REQ_Y + SIG_H + 3
        pdf.set_draw_color(80, 80, 80)
        pdf.set_line_width(0.35)
        pdf.line(sx, y_line, sx + SIG_W, y_line)

        # Label
        pdf.set_xy(sx, y_line + 1.5)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(60, 60, 60)
        pdf.cell(SIG_W, 5, label, align="C")

        # Nome
        pdf.set_xy(sx, y_line + 7)
        pdf.set_font("Helvetica", "I", 7.5)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(SIG_W, 4, nome_sig[:35], align="C")


# ─────────────────────────────────────────────────────────────────────────────
def gerar_pdf_contrato(dados, doc_id):
    dados["doc_id"] = doc_id
    pdf = RequerimentoPDF(doc_id=doc_id)
    _pagina_requerimento(pdf, dados)
    _pagina_pagamento(pdf, dados)
    _pagina_assinaturas(pdf, dados)
    raw = bytes(pdf.output())
    sha256 = hashlib.sha256(raw).hexdigest()
    return raw, sha256


def salvar_pdf(conteudo, nome_arquivo):
    caminho = CONTRATOS_DIR / nome_arquivo
    caminho.write_bytes(conteudo)
    return str(caminho)


def base64_para_imagem(b64, caminho):
    try:
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        data = base64.b64decode(b64)
        img = Image.open(BytesIO(data)).convert("RGB")
        img.save(str(caminho), "JPEG", quality=85)
        return True
    except Exception as e:
        print(f"Erro ao salvar imagem: {e}")
        return False


# ─── Overlay DocuSign-style ───────────────────────────────────────────────────
def aplicar_assinatura_no_pdf(pdf_bytes: bytes, sig_img_path: str,
                               sx: float, sy: float, sw: float, sh: float,
                               nome: str = "", ts: str = "") -> bytes:
    """
    Stampa a imagem de assinatura sobre a caixa na página de assinaturas.
    Cobre o placeholder com fundo branco e borda navy (estilo DocuSign).
    """
    from pypdf import PdfReader, PdfWriter

    ovl = FPDF(format="A4")
    ovl.set_margins(0, 0, 0)
    ovl.set_auto_page_break(False)
    ovl.add_page()

    # Fundo branco — cobre placeholder
    ovl.set_fill_color(255, 255, 255)
    ovl.set_draw_color(*NAVY)
    ovl.set_line_width(0.6)
    ovl.rect(sx, sy, sw, sh, "DF")

    # "DocuSigned by:" canto superior esquerdo
    ovl.set_xy(sx + 1.5, sy + 1.5)
    ovl.set_font("Helvetica", "", 5.5)
    ovl.set_text_color(*NAVY)
    ovl.cell(sw / 2, 4, "DocuSigned by:", align="L")

    # Imagem da assinatura
    sig_path = Path(sig_img_path)
    if sig_path.exists():
        img_x = sx + 2
        img_y = sy + 6
        img_w = sw - 4
        img_h = sh - 12
        ovl.image(str(sig_path), x=img_x, y=img_y, w=img_w, h=img_h)

    # Linha e nome/data no rodapé da caixa
    footer_y = sy + sh - 7
    ovl.set_draw_color(180, 195, 215)
    ovl.set_line_width(0.2)
    ovl.line(sx + 1, footer_y, sx + sw - 1, footer_y)

    if nome:
        label = (nome[:24] + "..." if len(nome) > 24 else nome)
        if ts:
            label += f"  {ts[:10]}"
        ovl.set_xy(sx + 2, footer_y + 0.5)
        ovl.set_font("Helvetica", "", 5.5)
        ovl.set_text_color(80, 80, 80)
        ovl.cell(sw - 4, 4, label, align="L")

    ovl_bytes = bytes(ovl.output())

    reader = PdfReader(BytesIO(pdf_bytes))
    ovl_reader = PdfReader(BytesIO(ovl_bytes))
    writer = PdfWriter()

    for i, page in enumerate(reader.pages):
        if i == SIG_PAGE_IDX:
            page.merge_page(ovl_reader.pages[0])
        writer.add_page(page)

    out = BytesIO()
    writer.write(out)
    return out.getvalue()


# ─── Página de auditoria ─────────────────────────────────────────────────────
def _gerar_pagina_audit(selfie_path, assin_path, dados, doc_id, url, role_label,
                        doc_frente_path=None, doc_verso_path=None):
    """Gera uma página PDF de auditoria para um assinante."""
    audit = RequerimentoPDF(doc_id=doc_id)
    audit.add_page()

    audit.titulo(f"AUDITORIA - {role_label.upper()}")
    audit.subtitulo("Assinatura Eletronica - Lei 14.063/2020 e MP 2.200-2/2001")

    y_inicio = audit.get_y()

    audit.campo("Documento",       doc_id)
    audit.campo("Papel",           role_label)
    audit.campo("Assinado em",     dados.get("assinado_em", "-"))
    audit.campo("IP do assinante", dados.get("ip", "-"))
    audit.campo("Geolocalizacao",  dados.get("geo", "nao fornecida"))
    audit.campo("Nome",            dados.get("nome", "-"))
    audit.campo("CPF",             dados.get("cpf", "-"))

    # QR code
    if url:
        qr_buf = _gerar_qr_png(url)
        if qr_buf:
            try:
                qr_size = 40
                qr_x = audit.w - 25 - qr_size
                audit.image(qr_buf, x=qr_x, y=y_inicio, w=qr_size, h=qr_size)
                audit.set_y(y_inicio + qr_size + 1)
                audit.set_font("Helvetica", "", 6.5)
                audit.set_text_color(100, 100, 100)
                audit.set_x(qr_x)
                audit.cell(qr_size, 4, "Verificar assinatura", align="C")
            except Exception as e:
                print(f"Erro ao inserir QR: {e}")

    audit.ln(2)
    audit.set_font("Helvetica", "B", 8)
    audit.set_text_color(80, 80, 80)
    audit.write(5, "Hash SHA-256: ")
    audit.set_font("Helvetica", "", 7)
    audit.set_text_color(60, 60, 60)
    audit.multi_cell(0, 5, dados.get("hash_doc", "-"))
    audit.ln(5)

    tem_selfie = selfie_path and Path(selfie_path).exists()
    tem_assin  = assin_path  and Path(assin_path).exists()

    if tem_selfie or tem_assin:
        audit.set_draw_color(*GOLD)
        audit.set_line_width(0.5)
        audit.line(25, audit.get_y(), audit.w - 25, audit.get_y())
        audit.ln(4)

    # Selfie e assinatura lado a lado
    y_imgs = audit.get_y()
    if tem_selfie:
        audit.set_font("Helvetica", "B", 8.5)
        audit.set_text_color(*NAVY)
        audit.cell(0, 5, "Selfie do assinante com documento",
                   new_x="LMARGIN", new_y="NEXT")
        audit.ln(1)
        try:
            audit.set_draw_color(200, 200, 200)
            audit.set_line_width(0.3)
            audit.rect(25, audit.get_y(), 58, 73)
            audit.image(selfie_path, x=25.5, y=audit.get_y() + 0.5, w=57, h=72)
            audit.set_y(audit.get_y() + 76)
        except Exception:
            audit.para("(selfie nao disponivel)", after=2)

    if tem_assin:
        audit.set_font("Helvetica", "B", 8.5)
        audit.set_text_color(*NAVY)
        audit.cell(0, 5, "Assinatura digital",
                   new_x="LMARGIN", new_y="NEXT")
        audit.ln(1)
        try:
            sig_x, sig_y, sig_w, sig_h = 25, audit.get_y(), 120, 48
            audit.set_fill_color(255, 255, 255)
            audit.set_draw_color(*NAVY)
            audit.set_line_width(0.4)
            audit.rect(sig_x, sig_y, sig_w, sig_h, "DF")
            audit.image(assin_path, x=sig_x + 2, y=sig_y + 2,
                        w=sig_w - 4, h=sig_h - 4)
            audit.set_y(sig_y + sig_h + 3)
        except Exception:
            audit.para("(assinatura nao disponivel)", after=2)

    # Documentos de identidade (frente e verso)
    tem_frente = doc_frente_path and Path(doc_frente_path).exists()
    tem_verso  = doc_verso_path  and Path(doc_verso_path).exists()

    if tem_frente or tem_verso:
        audit.ln(4)
        audit.set_draw_color(*GOLD)
        audit.set_line_width(0.5)
        audit.line(25, audit.get_y(), audit.w - 25, audit.get_y())
        audit.ln(4)

        audit.set_font("Helvetica", "B", 9)
        audit.set_text_color(*NAVY)
        audit.cell(0, 6, "Documentos de identificacao",
                   new_x="LMARGIN", new_y="NEXT")
        audit.ln(2)

        doc_w = 78   # largura de cada foto de documento
        doc_h = 50   # altura

        if tem_frente:
            audit.set_font("Helvetica", "B", 8)
            audit.set_text_color(80, 80, 80)
            audit.cell(doc_w, 5, "Frente", align="C",
                       new_x="LMARGIN", new_y="NEXT")
            audit.ln(1)
            try:
                fx, fy = 25, audit.get_y()
                audit.set_draw_color(200, 200, 200)
                audit.set_line_width(0.3)
                audit.rect(fx, fy, doc_w, doc_h)
                audit.image(doc_frente_path, x=fx + 0.5, y=fy + 0.5,
                            w=doc_w - 1, h=doc_h - 1)
                if tem_verso:
                    vx, vy = fx + doc_w + 6, fy
                    audit.set_font("Helvetica", "B", 8)
                    audit.set_text_color(80, 80, 80)
                    audit.set_xy(vx, fy - 6)
                    audit.cell(doc_w, 5, "Verso", align="C")
                    audit.rect(vx, vy, doc_w, doc_h)
                    audit.image(doc_verso_path, x=vx + 0.5, y=vy + 0.5,
                                w=doc_w - 1, h=doc_h - 1)
                audit.set_y(fy + doc_h + 4)
            except Exception as e:
                print(f"Erro ao inserir doc frente/verso: {e}")
                audit.para("(documento nao disponivel)", after=2)
        elif tem_verso:
            audit.set_font("Helvetica", "B", 8)
            audit.set_text_color(80, 80, 80)
            audit.cell(doc_w, 5, "Verso", align="C",
                       new_x="LMARGIN", new_y="NEXT")
            audit.ln(1)
            try:
                vx, vy = 25, audit.get_y()
                audit.set_draw_color(200, 200, 200)
                audit.set_line_width(0.3)
                audit.rect(vx, vy, doc_w, doc_h)
                audit.image(doc_verso_path, x=vx + 0.5, y=vy + 0.5,
                            w=doc_w - 1, h=doc_h - 1)
                audit.set_y(vy + doc_h + 4)
            except Exception as e:
                print(f"Erro ao inserir doc verso: {e}")
                audit.para("(documento nao disponivel)", after=2)

    audit.ln(3)
    audit.set_draw_color(220, 220, 220)
    audit.set_line_width(0.3)
    audit.line(25, audit.get_y(), audit.w - 25, audit.get_y())
    audit.ln(3)
    audit.set_font("Helvetica", "I", 8)
    audit.set_text_color(100, 100, 100)
    audit.multi_cell(0, 5,
        "Documento assinado eletronicamente em conformidade com a MP 2.200-2/2001 "
        "e Lei 14.063/2020. O hash SHA-256 garante a integridade do documento original. "
        "IP, geolocalizacao, selfie e assinatura constituem prova de autoria e consentimento.",
        align="J")

    return bytes(audit.output())


# ─── PDF final unificado (ambas as assinaturas) ───────────────────────────────
def gerar_pdf_final_completo(
    pdf_original_path: str,
    *,
    assin_req_path=None, selfie_req_path=None, dados_req=None,
    doc_frente_req_path=None, doc_verso_req_path=None,
    assin_prop_path=None, selfie_prop_path=None, dados_prop=None,
    doc_frente_prop_path=None, doc_verso_prop_path=None,
    doc_id="",
    verificacao_url_req="", verificacao_url_prop="",
) -> bytes:
    """
    Monta o PDF final:
    1. Páginas originais do contrato
    2. Overlay das assinaturas na página 3 (DocuSign-style)
    3. Página(s) de auditoria ao final
    """
    from pypdf import PdfReader, PdfWriter

    working = Path(pdf_original_path).read_bytes()

    # Overlay requerente
    if assin_req_path and Path(assin_req_path).exists() and dados_req:
        working = aplicar_assinatura_no_pdf(
            working, assin_req_path,
            sx=SIG_REQ_X, sy=SIG_REQ_Y, sw=SIG_W, sh=SIG_H,
            nome=dados_req.get("nome", ""),
            ts=dados_req.get("assinado_em", "")[:10],
        )

    # Overlay proprietário
    if assin_prop_path and Path(assin_prop_path).exists() and dados_prop:
        working = aplicar_assinatura_no_pdf(
            working, assin_prop_path,
            sx=SIG_PROP_X, sy=SIG_PROP_Y, sw=SIG_W, sh=SIG_H,
            nome=dados_prop.get("nome", ""),
            ts=dados_prop.get("assinado_em", "")[:10],
        )

    # Monta PDF final
    writer = PdfWriter()
    for page in PdfReader(BytesIO(working)).pages:
        writer.add_page(page)

    # Páginas de auditoria
    for selfie_p, assin_p, dados, role_label, url, frente_p, verso_p in [
        (selfie_req_path,  assin_req_path,  dados_req,  "Requerente",             verificacao_url_req,
         doc_frente_req_path,  doc_verso_req_path),
        (selfie_prop_path, assin_prop_path, dados_prop, "Proprietario / Vendedor", verificacao_url_prop,
         doc_frente_prop_path, doc_verso_prop_path),
    ]:
        if dados:
            audit = _gerar_pagina_audit(selfie_p, assin_p, dados, doc_id, url, role_label,
                                        doc_frente_path=frente_p, doc_verso_path=verso_p)
            for page in PdfReader(BytesIO(audit)).pages:
                writer.add_page(page)

    out = BytesIO()
    writer.write(out)
    return out.getvalue()


# ─── Compatibilidade retroativa ───────────────────────────────────────────────
def gerar_pdf_assinado(pdf_original_bytes, selfie_path, assinatura_path,
                       dados_auditoria, doc_id="", verificacao_url=""):
    """Mantido para compatibilidade — usa o novo sistema internamente."""
    from pypdf import PdfReader, PdfWriter

    # Salva bytes originais em temp para poder passar o path
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_original_bytes)
        tmp_path = tmp.name

    role_label = dados_auditoria.get("role", "Assinante")
    is_req = "req" in role_label.lower() or "requerente" in role_label.lower()

    result = gerar_pdf_final_completo(
        tmp_path,
        assin_req_path=assinatura_path   if is_req else None,
        selfie_req_path=selfie_path      if is_req else None,
        dados_req=dados_auditoria        if is_req else None,
        assin_prop_path=assinatura_path  if not is_req else None,
        selfie_prop_path=selfie_path     if not is_req else None,
        dados_prop=dados_auditoria       if not is_req else None,
        doc_id=doc_id,
        verificacao_url_req=verificacao_url   if is_req else "",
        verificacao_url_prop=verificacao_url  if not is_req else "",
    )
    Path(tmp_path).unlink(missing_ok=True)
    return result


# ─── QR code ─────────────────────────────────────────────────────────────────
def _gerar_qr_png(url):
    try:
        import qrcode
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=5, border=3,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color=(13, 43, 78), back_color=(255, 255, 255))
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"Erro ao gerar QR: {e}")
        return None
