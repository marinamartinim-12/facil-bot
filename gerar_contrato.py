"""
Fácil Financiamentos — Geração de contratos PDF
Estilo fiel ao modelo original: documento jurídico limpo.
"""

import hashlib
import base64
import os
from datetime import datetime
from io import BytesIO
from pathlib import Path

from fpdf import FPDF
from PIL import Image

CONTRATOS_DIR = Path(os.getenv("CONTRATOS_DIR", "/data/contratos"))
CONTRATOS_DIR.mkdir(parents=True, exist_ok=True)

NAVY = (13, 43, 78)
GOLD = (200, 155, 0)


# ─────────────────────────────────────────────────────────────────────────────
class RequerimentoPDF(FPDF):

    def __init__(self, doc_id=""):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.doc_id = doc_id
        self.set_auto_page_break(auto=True, margin=28)
        self.set_margins(25, 15, 25)

    def header(self):
        # Círculo navy com "F"
        self.set_fill_color(*NAVY)
        self.ellipse(25 - 7, 12 - 7, 14, 14, "F")
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(255, 255, 255)
        self.set_xy(22.5, 8.5)
        self.cell(5, 7, "F")

        # Nome da empresa
        self.set_font("Helvetica", "B", 15)
        self.set_text_color(*NAVY)
        self.set_xy(35, 7)
        self.cell(0, 7, "FACIL FINANCIAMENTOS", new_x="LMARGIN", new_y="NEXT")

        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(100, 110, 125)
        self.set_x(35)
        self.cell(0, 4.5,
            "Rua Lauro Ignacio Ponte, 08 - Sala 202 - Parque Sao Pedro - Venda Nova - BH/MG",
            new_x="LMARGIN", new_y="NEXT")

        # Linha dourada
        self.set_draw_color(*GOLD)
        self.set_line_width(0.8)
        self.line(25, 22, self.w - 25, 22)
        self.set_line_width(0.2)
        self.set_draw_color(180, 180, 180)
        self.ln(8)

    def footer(self):
        self.set_y(-16)
        self.set_draw_color(180, 180, 180)
        self.set_line_width(0.3)
        self.line(25, self.get_y(), self.w - 25, self.get_y())
        self.ln(1.5)
        self.set_font("Helvetica", "B", 7)
        self.set_text_color(80, 80, 80)
        self.cell(0, 4,
            "Facil Financiamentos, rua Lauro Ignacio Ponte, 08 - sala 202 - Parq. Sao Pedro - Venda Nova",
            align="C")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def titulo(self, texto):
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(*NAVY)
        self.cell(0, 8, texto, align="C", new_x="LMARGIN", new_y="NEXT")

    def subtitulo(self, texto):
        self.set_font("Helvetica", "I", 10)
        self.set_text_color(80, 80, 80)
        self.cell(0, 6, texto, align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(4)

    def escrever(self, partes):
        """
        Escreve inline misturando normal e negrito.
        partes = [("texto normal", False), ("TEXTO BOLD", True), ...]
        Trata quebras de linha manualmente — não usa \\n dentro de write().
        """
        for texto, bold in partes:
            self.set_font("Helvetica", "B" if bold else "", 10)
            self.set_text_color(30, 30, 30)
            # Quebras de linha explícitas
            linhas = str(texto).split("\n")
            for i, linha in enumerate(linhas):
                if i > 0:
                    self.ln(6.5)
                    self.set_x(self.l_margin)
                if linha:
                    self.write(6.5, linha)
        self.ln(6.5)

    def para(self, texto, bold=False, align="J", after=4):
        """Parágrafo simples sem mistura de estilos."""
        self.set_font("Helvetica", "B" if bold else "", 10)
        self.set_text_color(30, 30, 30)
        self.multi_cell(0, 6.5, str(texto), align=align)
        if after:
            self.ln(after)

    def declaracao(self, texto):
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*NAVY)
        self.multi_cell(0, 6.5, texto, align="C")
        self.ln(4)

    def campo(self, rotulo, valor):
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(30, 30, 30)
        self.write(6.5, rotulo + ": ")
        self.set_font("Helvetica", "", 10)
        self.write(6.5, str(valor or "—"))
        self.ln(7)

    def campo_valor(self, rotulo, valor):
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(30, 30, 30)
        self.write(6.5, rotulo + ": ")
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*NAVY)
        self.write(6.5, str(valor or "—"))
        self.ln(8)

    def assinatura(self, x, largura, label, nome=""):
        y = self.get_y()
        self.set_draw_color(80, 80, 80)
        self.set_line_width(0.4)
        self.line(x, y, x + largura, y)
        self.set_font("Helvetica", "", 8.5)
        self.set_text_color(80, 80, 80)
        self.set_xy(x, y + 2)
        self.cell(largura, 5, label, align="C")
        if nome:
            self.set_font("Helvetica", "I", 7.5)
            self.set_xy(x, y + 7)
            self.cell(largura, 4, nome, align="C")


# ─────────────────────────────────────────────────────────────────────────────
def _v(d, chave, padrao=""):
    """Retorna o valor do dict ou padrão — nunca None."""
    val = d.get(chave)
    if val is None or str(val).strip() == "":
        return str(padrao)
    return str(val)


def _pagina_requerimento(pdf, d):
    pdf.add_page()

    pdf.titulo("REQUERIMENTO DE INTERMEDIACAO")
    pdf.subtitulo("Prestacao de Servico")

    modalidade = _v(d, "modalidade", "refinanciamento").lower()
    verbo = "refinanciamento" if "refin" in modalidade else "financiamento da aquisicao"

    endereco = (
        _v(d, "req_rua") + ", "
        + "N." + _v(d, "req_numero") + " "
        + "BAIRRO " + _v(d, "req_bairro").upper() + " "
        + "CEP " + _v(d, "req_cep") + " "
        + _v(d, "req_cidade").upper()
    ).strip(" ,")

    # Parágrafo 1 — identificação do requerente
    pdf.escrever([
        ("Eu ", False),
        (_v(d, "req_nome").upper() + " ", True),
        ("CPF ", False),
        (_v(d, "req_cpf") + " ", True),
        ("RG ", False),
        (_v(d, "req_rg") + " ", True),
        ("residente na ", False),
        (endereco + " ", True),
        ("- CELULAR ", False),
        (_v(d, "req_celular"), True),
        (".", False),
    ])
    pdf.ln(2)

    # Parágrafo 2 — requerimento do veículo
    pdf.escrever([
        ("Requeiro que seja ", False),
        ("INTERMEDIADO ", True),
        ("o " + verbo + " do veiculo de marca ", False),
        (_v(d, "vei_modelo").upper() + " ", True),
        ("Placa ", False),
        (_v(d, "vei_placa").upper() + " ", True),
        ("ano ", False),
        (_v(d, "vei_ano") + " ", True),
        ("cor ", False),
        (_v(d, "vei_cor").upper() + " ", True),
        ("RENAVAM ", False),
        (_v(d, "vei_renavam") + " ", True),
        ("CHASSI ", False),
        (_v(d, "vei_chassi").upper() + " ", True),
        ("adquirido fruto de negociacao direta com o seu legitimo proprietario/representante:", False),
    ])
    pdf.ln(2)

    # Parágrafo 3 — proprietário
    pdf.escrever([
        ("O Sr. ", False),
        (_v(d, "prop_nome").upper() + " ", True),
        ("CPF ", False),
        (_v(d, "prop_cpf") + " ", True),
        ("telefone ", False),
        (_v(d, "prop_telefone"), True),
    ])
    pdf.para(
        "que se responsabiliza civil e criminalmente pelo mesmo, "
        "inclusive pela documentacao apresentada.",
        after=5,
    )

    # Parágrafo 4 — condições financeiras
    pdf.escrever([
        ("O valor ", False),
        ("liquido ", True),
        ("liberado sera de ", False),
        ("R$ " + _v(d, "fin_valor_liquido") + " ", True),
        ("ja descontadas todas as despesas, consultoria, comissoes, taxas, impostos, "
         "e intermediacao, divididos em ", False),
        (_v(d, "fin_parcelas") + "x de R$ " + _v(d, "fin_valor_parcela") + " ", True),
        ("1o vencimento em ", False),
        (_v(d, "fin_vencimento") + ".", True),
    ])
    pdf.ln(2)

    # Parágrafo 5 — isenção
    pdf.escrever([
        ("Neste ato o requerente que ", False),
        ("NAO ", True),
        ("adquiriu o veiculo junto a empresa, sendo que a mesma nao se responsabiliza "
         "pela documentacao e qualidade do mesmo.", False),
    ])
    pdf.ln(3)

    # Declaração em destaque
    pdf.declaracao(
        "DECLARO AINDA, QUE NADA MAIS ME FOI PROMETIDO ALEM DO QUE\n"
        "ESTA ESPECIFICADO NESTE REQUERIMENTO."
    )

    # Data
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(60, 60, 60)
    data_str = _v(d, "data_contrato", datetime.now().strftime("%d de %B de %Y"))
    pdf.cell(0, 6, "Belo Horizonte, " + data_str + ".",
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(14)

    # Linhas de assinatura
    largura_pag = pdf.w - 50
    x1 = 25
    x2 = pdf.w - 25 - 80
    pdf.assinatura(x1, 80, "Requerente", _v(d, "req_nome").upper())
    pdf.set_xy(x2, pdf.get_y() - 11)
    pdf.assinatura(x2, 80, "Proprietario / Vendedor", _v(d, "prop_nome").upper())
    pdf.ln(14)


def _pagina_pagamento(pdf, d):
    pdf.add_page()

    pdf.titulo("DADOS DA CONTA PARA PAGAMENTO")
    pdf.ln(5)

    modelo    = _v(d, "vei_modelo").upper()
    placa     = _v(d, "vei_placa").upper()
    ano       = _v(d, "vei_ano")
    cor       = _v(d, "vei_cor").upper()
    banco_neg = _v(d, "fin_banco").upper()

    # Parágrafo de autorização
    pdf.escrever([
        ("Eu ", False),
        (_v(d, "req_nome").upper() + " ", True),
        ("CPF ", False),
        (_v(d, "req_cpf") + " ", True),
        ("RG ", False),
        (_v(d, "req_rg"), True),
    ])
    pdf.escrever([
        ("Autorizo o pagamento da importancia de ", False),
        ("R$" + _v(d, "pag_valor") + " ", True),
        ("referente ao refinanciamento do veiculo de marca ", False),
        (modelo + " ", True),
        ("Placa ", False),
        (placa + " ", True),
        ("ano ", False),
        (ano + " ", True),
        ("cor ", False),
        (cor + " ", True),
        ("que foi negociado junto ao banco ", False),
        (banco_neg + " ", True),
        ("na conta abaixo discriminada.", False),
    ])
    pdf.ln(5)

    # Dados bancários — lista simples
    pdf.set_x(30)
    pdf.campo(" Nome",    _v(d, "pag_nome_beneficiario").upper())
    pdf.set_x(30)
    pdf.campo(" CPF",     _v(d, "pag_cpf_beneficiario"))
    pdf.set_x(30)
    pdf.campo(" Banco",   _v(d, "pag_banco").upper())
    pdf.set_x(30)
    pdf.campo(" Agencia", _v(d, "pag_agencia"))
    pdf.set_x(30)
    pdf.campo(" Conta",   _v(d, "pag_conta"))
    pdf.set_x(30)
    pdf.campo(" PIX",     _v(d, "pag_pix"))
    pdf.ln(2)
    pdf.set_x(30)
    pdf.campo_valor(" VALOR", "R$ " + _v(d, "pag_valor"))
    pdf.ln(10)

    # Assinatura
    pdf.assinatura(25, pdf.w - 50, "Requerente - Autorizo o pagamento acima")
    pdf.ln(4)
    pdf.set_x(25)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(80, 80, 80)
    pdf.write(6, "Nome: ")
    pdf.set_draw_color(150, 150, 150)
    pdf.set_line_width(0.3)
    lx = pdf.get_x()
    ly = pdf.get_y() + 4.5
    pdf.line(lx, ly, pdf.w - 25, ly)


# ─────────────────────────────────────────────────────────────────────────────
def gerar_pdf_contrato(dados, doc_id):
    dados["doc_id"] = doc_id
    pdf = RequerimentoPDF(doc_id=doc_id)
    _pagina_requerimento(pdf, dados)
    _pagina_pagamento(pdf, dados)
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


def gerar_pdf_assinado(pdf_original_bytes, selfie_path, assinatura_path,
                       dados_auditoria, doc_id=""):
    from pypdf import PdfWriter, PdfReader

    # Página de auditoria
    audit = RequerimentoPDF(doc_id=doc_id)
    audit.add_page()

    audit.titulo("PAGINA DE AUDITORIA")
    audit.subtitulo("Assinatura Eletronica - Lei 14.063/2020 e MP 2.200-2/2001")

    audit.campo("Documento n.", doc_id)
    audit.campo("Assinado em",     dados_auditoria.get("assinado_em", "—"))
    audit.campo("IP do assinante", dados_auditoria.get("ip", "—"))
    audit.campo("Geolocalizacao",  dados_auditoria.get("geo", "nao fornecida"))
    audit.campo("Nome",            dados_auditoria.get("nome", "—"))
    audit.campo("CPF",             dados_auditoria.get("cpf", "—"))
    audit.ln(2)
    audit.set_font("Helvetica", "B", 8)
    audit.set_text_color(80, 80, 80)
    audit.write(5, "Hash SHA-256: ")
    audit.set_font("Helvetica", "", 7)
    audit.set_text_color(60, 60, 60)
    audit.multi_cell(0, 5, dados_auditoria.get("hash_doc", "—"))
    audit.ln(5)

    if selfie_path and Path(selfie_path).exists():
        audit.para("Selfie do assinante:", bold=True, after=2)
        try:
            audit.image(selfie_path, x=25, w=55, h=70)
            audit.ln(3)
        except Exception:
            audit.para("(selfie nao disponivel)")

    if assinatura_path and Path(assinatura_path).exists():
        audit.para("Assinatura manuscrita digital:", bold=True, after=2)
        try:
            audit.image(assinatura_path, x=25, w=110, h=44)
            audit.ln(3)
        except Exception:
            audit.para("(assinatura nao disponivel)")

    audit.ln(4)
    audit.set_font("Helvetica", "I", 8.5)
    audit.set_text_color(80, 80, 80)
    audit.multi_cell(0, 5.5,
        "Documento assinado eletronicamente em conformidade com a MP 2.200-2/2001 "
        "e Lei 14.063/2020. O hash SHA-256 garante a integridade do documento original. "
        "IP, geolocalizacao, selfie e assinatura constituem prova de autoria e consentimento.",
        align="J")

    audit_bytes = bytes(audit.output())

    # Merge
    try:
        writer = PdfWriter()
        for src in [pdf_original_bytes, audit_bytes]:
            reader = PdfReader(BytesIO(src))
            for page in reader.pages:
                writer.add_page(page)
        out = BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception as e:
        print(f"Erro no merge: {e}")
        return audit_bytes
