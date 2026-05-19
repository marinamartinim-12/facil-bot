"""
Fácil Financiamentos — Geração de contratos PDF
Estilo: documento jurídico limpo, fiel ao modelo original.
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
    """PDF estilo documento jurídico — idêntico ao modelo original."""

    def __init__(self, doc_id: str = ""):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.doc_id = doc_id
        self.set_auto_page_break(auto=True, margin=28)
        self.set_margins(25, 15, 25)

    # ── Cabeçalho ─────────────────────────────────────────────────────────────
    def header(self):
        # Bloco logo: círculo navy com "F" + nome da empresa
        cx, cy, r = 25, 12, 7
        self.set_fill_color(*NAVY)
        self.ellipse(cx - r, cy - r, r * 2, r * 2, "F")
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(255, 255, 255)
        self.set_xy(cx - 2.5, cy - 3.5)
        self.cell(5, 7, "F")

        # Nome da empresa ao lado do círculo
        self.set_font("Helvetica", "B", 15)
        self.set_text_color(*NAVY)
        self.set_xy(35, 7)
        self.cell(0, 7, "FÁCIL FINANCIAMENTOS", new_x="LMARGIN", new_y="NEXT")

        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(100, 110, 125)
        self.set_x(35)
        self.cell(0, 4.5,
            "Rua Lauro Ignácio Ponte, 08 – Sala 202 – Parque São Pedro – Venda Nova – BH/MG",
            new_x="LMARGIN", new_y="NEXT")

        # Linha separadora dourada
        self.set_draw_color(*GOLD)
        self.set_line_width(0.8)
        self.line(25, 22, self.w - 25, 22)
        self.set_line_width(0.2)
        self.set_draw_color(180, 180, 180)
        self.ln(8)

    # ── Rodapé ────────────────────────────────────────────────────────────────
    def footer(self):
        self.set_y(-16)
        self.set_draw_color(180, 180, 180)
        self.set_line_width(0.3)
        self.line(25, self.get_y(), self.w - 25, self.get_y())
        self.ln(1.5)
        self.set_font("Helvetica", "B", 7)
        self.set_text_color(80, 80, 80)
        self.cell(0, 4,
            "Fácil Financiamentos, rua Lauro Ignácio Ponte, 08 - sala 202 – Parq. São Pedro – Venda Nova",
            align="C")

    # ── Helpers de texto ──────────────────────────────────────────────────────
    def titulo(self, texto: str):
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(*NAVY)
        self.cell(0, 8, texto, align="C", new_x="LMARGIN", new_y="NEXT")

    def subtitulo(self, texto: str):
        self.set_font("Helvetica", "I", 10)
        self.set_text_color(80, 80, 80)
        self.cell(0, 6, texto, align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(4)

    def _write_mixed(self, parts: list[tuple[str, bool]], line_h: float = 6.5):
        """
        Escreve uma linha com partes normais e negrito misturadas.
        parts = [("texto normal", False), ("TEXTO BOLD", True), ...]
        """
        for texto, bold in parts:
            self.set_font("Helvetica", "B" if bold else "", 10)
            self.set_text_color(30, 30, 30)
            self.write(line_h, texto)

    def paragrafo(self, parts: list[tuple[str, bool]], after: float = 4.0):
        """Parágrafo com partes bold/normal misturadas, com quebra automática."""
        self._write_mixed(parts)
        self.ln()
        self.ln(after)

    def paragrafo_simples(self, texto: str, bold: bool = False, after: float = 4.0):
        self.set_font("Helvetica", "B" if bold else "", 10)
        self.set_text_color(30, 30, 30)
        self.multi_cell(0, 6.5, texto, align="J")
        self.ln(after)

    def declaracao(self, texto: str):
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*NAVY)
        self.multi_cell(0, 6.5, texto, align="C")
        self.ln(4)

    def campo_lista(self, rotulo: str, valor: str):
        """Ex: Nome: FULANO DE TAL"""
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(30, 30, 30)
        self.write(6.5, rotulo + ": ")
        self.set_font("Helvetica", "", 10)
        self.write(6.5, valor or "—")
        self.ln(7)

    def campo_destaque(self, rotulo: str, valor: str):
        """Campo com valor em BOLD CAPS para destaque (ex: VALOR)"""
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(30, 30, 30)
        self.write(6.5, rotulo + ": ")
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*NAVY)
        self.write(6.5, valor or "—")
        self.ln(8)

    def linha_assinatura(self, x: float, largura: float, label: str, nome: str = ""):
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
# Página 1 — Requerimento de Intermediação
# ─────────────────────────────────────────────────────────────────────────────
def _pagina_requerimento(pdf: RequerimentoPDF, d: dict):
    pdf.add_page()

    pdf.titulo("REQUERIMENTO DE INTERMEDIAÇÃO")
    pdf.subtitulo("Prestação de Serviço")

    modalidade = (d.get("modalidade") or "refinanciamento").lower()
    verbo = "refinanciamento" if "refin" in modalidade else "financiamento da aquisição"

    # Parágrafo de identificação do requerente
    endereco = (
        f"{d.get('req_rua','')}, "
        f"N°{d.get('req_numero','')} "
        f"BAIRRO {d.get('req_bairro','').upper()} "
        f"CEP {d.get('req_cep','')} "
        f"{d.get('req_cidade','').upper()}"
    ).strip(" ,")

    pdf.paragrafo([
        ("Eu ", False),
        (f"{d.get('req_nome','').upper()} ", True),
        ("CPF ", False),
        (f"{d.get('req_cpf','')} ", True),
        ("RG ", False),
        (f"{d.get('req_rg','')} ", True),
        ("residente na ", False),
        (f"{endereco} ", True),
        ("- CELULAR ", False),
        (f"{d.get('req_celular','')}", True),
        (".", False),
    ], after=5)

    # Parágrafo principal do requerimento
    modelo  = d.get('vei_modelo','').upper()
    placa   = d.get('vei_placa','').upper()
    ano     = d.get('vei_ano','')
    cor     = d.get('vei_cor','').upper()
    renavam = d.get('vei_renavam','')
    chassi  = d.get('vei_chassi','').upper()

    pdf.paragrafo([
        ("Requeiro que seja ", False),
        ("INTERMEDIADO ", True),
        (f"o {verbo} do veículo de marca ", False),
        (f"{modelo} ", True),
        ("Placa ", False),
        (f"{placa} ", True),
        ("ano ", False),
        (f"{ano} ", True),
        ("cor ", False),
        (f"{cor} ", True),
        ("RENAVAM ", False),
        (f"{renavam} ", True),
        ("CHASSI ", False),
        (f"{chassi} ", True),
        ("adquirido fruto de negociação direta com o seu legítimo proprietário/representante:", False),
    ], after=5)

    # Proprietário / Vendedor
    pdf.paragrafo([
        ("O Sr. ", False),
        (f"{d.get('prop_nome','').upper()} ", True),
        ("CPF ", False),
        (f"{d.get('prop_cpf','')} ", True),
        ("telefone ", False),
        (f"{d.get('prop_telefone','')}", True),
        ("\nque se responsabiliza civil e criminalmente pelo mesmo, "
         "inclusive pela documentação apresentada.", False),
    ], after=5)

    # Condições financeiras
    pdf.paragrafo([
        ("O valor ", False),
        ("líquido ", True),
        ("liberado será de ", False),
        (f"R$ {d.get('fin_valor_liquido','')} ", True),
        ("já descontadas todas as despesas, consultoria, comissões, taxas, impostos, "
         "e intermediação, divididos em ", False),
        (f"{d.get('fin_parcelas','')}x de R$ {d.get('fin_valor_parcela','')} ", True),
        (f"1º vencimento em ", False),
        (f"{d.get('fin_vencimento','')}", True),
        (".", False),
    ], after=5)

    # Isenção de responsabilidade
    pdf.paragrafo([
        ("Neste ato o requerente que ", False),
        ("NÃO ", True),
        ("adquiriu o veículo junto a empresa, sendo que a mesma não se responsabiliza "
         "pela documentação e qualidade do mesmo.", False),
    ], after=6)

    # Declaração em destaque
    pdf.declaracao(
        "DECLARO AINDA, QUE NADA MAIS ME FOI PROMETIDO ALÉM DO QUE\n"
        "ESTÁ ESPECIFICADO NESTE REQUERIMENTO."
    )

    # Data
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(60, 60, 60)
    pdf.cell(0, 6,
        f"Belo Horizonte, {d.get('data_contrato', datetime.now().strftime('%d de %B de %Y'))}.",
        align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(14)

    # Assinaturas
    larg = (pdf.w - 50 - 50) / 3
    x1 = 25
    x2 = pdf.w - 25 - 80
    pdf.linha_assinatura(x1, 80, "Requerente", d.get('req_nome','').upper())
    pdf.set_xy(x2, pdf.get_y() - 11)
    pdf.linha_assinatura(x2, 80, "Proprietário / Vendedor", d.get('prop_nome','').upper())
    pdf.ln(14)


# ─────────────────────────────────────────────────────────────────────────────
# Página 2 — Dados da Conta para Pagamento
# ─────────────────────────────────────────────────────────────────────────────
def _pagina_pagamento(pdf: RequerimentoPDF, d: dict):
    pdf.add_page()

    pdf.titulo("DADOS DA CONTA PARA PAGAMENTO")
    pdf.ln(5)

    modelo = d.get('vei_modelo','').upper()
    placa  = d.get('vei_placa','').upper()
    ano    = d.get('vei_ano','')
    cor    = d.get('vei_cor','').upper()
    banco_neg = d.get('fin_banco','').upper()

    pdf.paragrafo([
        ("Eu ", False),
        (f"{d.get('req_nome','').upper()} ", True),
        ("CPF ", False),
        (f"{d.get('req_cpf','')} ", True),
        ("RG ", False),
        (f"{d.get('req_rg','')}\n", True),
        ("Autorizo o pagamento da importância de ", False),
        (f"R${d.get('pag_valor','')} ", True),
        ("referente ao refinanciamento do veículo de marca ", False),
        (f"{modelo} ", True),
        ("Placa ", False),
        (f"{placa} ", True),
        ("ano ", False),
        (f"{ano} ", True),
        ("cor ", False),
        (f"{cor} ", True),
        ("que foi negociado junto ao banco ", False),
        (f"{banco_neg} ", True),
        ("na conta abaixo discriminada.", False),
    ], after=8)

    # Dados bancários como lista
    pdf.set_x(30)
    pdf.campo_lista(" Nome",    d.get('pag_nome_beneficiario','').upper())
    pdf.set_x(30)
    pdf.campo_lista(" CPF",     d.get('pag_cpf_beneficiario',''))
    pdf.set_x(30)
    pdf.campo_lista(" Banco",   d.get('pag_banco','').upper())
    pdf.set_x(30)
    pdf.campo_lista(" Agência", d.get('pag_agencia',''))
    pdf.set_x(30)
    pdf.campo_lista(" Conta",   d.get('pag_conta',''))
    pdf.set_x(30)
    pdf.campo_lista(" PIX",     d.get('pag_pix',''))
    pdf.ln(2)
    pdf.set_x(30)
    pdf.campo_destaque(" VALOR", f"R$ {d.get('pag_valor','')}")
    pdf.ln(10)

    # Assinatura
    x1 = 25
    pdf.linha_assinatura(x1, pdf.w - 50, "Requerente — Autorizo o pagamento acima")
    pdf.ln(3)
    pdf.set_x(x1)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(30, 5, "Nome: ")
    pdf.set_draw_color(150, 150, 150)
    pdf.set_line_width(0.3)
    pdf.line(pdf.get_x(), pdf.get_y() + 4.5, pdf.w - 25, pdf.get_y() + 4.5)


# ─────────────────────────────────────────────────────────────────────────────
# Função principal exportada
# ─────────────────────────────────────────────────────────────────────────────
def gerar_pdf_contrato(dados: dict, doc_id: str) -> tuple[bytes, str]:
    dados["doc_id"] = doc_id
    pdf = RequerimentoPDF(doc_id=doc_id)
    _pagina_requerimento(pdf, dados)
    _pagina_pagamento(pdf, dados)
    raw = bytes(pdf.output())
    sha256 = hashlib.sha256(raw).hexdigest()
    return raw, sha256


# ─────────────────────────────────────────────────────────────────────────────
# Utilitários
# ─────────────────────────────────────────────────────────────────────────────
def salvar_pdf(conteudo: bytes, nome_arquivo: str) -> str:
    caminho = CONTRATOS_DIR / nome_arquivo
    caminho.write_bytes(conteudo)
    return str(caminho)


def base64_para_imagem(b64: str, caminho: Path) -> bool:
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


def gerar_pdf_assinado(
    pdf_original_bytes: bytes,
    selfie_path: str,
    assinatura_path: str,
    dados_auditoria: dict,
    doc_id: str = "",
) -> bytes:
    """Gera página de auditoria e faz merge com o contrato original."""
    from pypdf import PdfWriter, PdfReader

    # ── Página de auditoria ──────────────────────────────────────────────────
    audit = RequerimentoPDF(doc_id=doc_id)
    audit.set_margins(25, 15, 25)
    audit.add_page()

    audit.titulo("PÁGINA DE AUDITORIA")
    audit.subtitulo("Assinatura Eletrônica — Lei 14.063/2020 e MP 2.200-2/2001")

    audit.campo_lista("Documento nº",    doc_id)
    audit.campo_lista("Assinado em",     dados_auditoria.get("assinado_em", "—"))
    audit.campo_lista("IP do assinante", dados_auditoria.get("ip", "—"))
    audit.campo_lista("Geolocalização",  dados_auditoria.get("geo", "não fornecida"))
    audit.campo_lista("Nome",            dados_auditoria.get("nome", "—"))
    audit.campo_lista("CPF",             dados_auditoria.get("cpf", "—"))
    audit.ln(2)
    audit.set_font("Helvetica", "B", 8)
    audit.set_text_color(80, 80, 80)
    audit.cell(25, 5, "Hash SHA-256:")
    audit.set_font("Helvetica", "", 7)
    audit.set_text_color(60, 60, 60)
    audit.multi_cell(0, 5, dados_auditoria.get("hash_doc", "—"))
    audit.ln(5)

    if selfie_path and Path(selfie_path).exists():
        audit.set_font("Helvetica", "B", 9)
        audit.set_text_color(*NAVY)
        audit.cell(0, 6, "Selfie do assinante:", new_x="LMARGIN", new_y="NEXT")
        audit.ln(1)
        try:
            audit.image(selfie_path, x=25, w=55, h=70)
            audit.ln(3)
        except Exception:
            audit.paragrafo_simples("(selfie não disponível)")

    if assinatura_path and Path(assinatura_path).exists():
        audit.set_font("Helvetica", "B", 9)
        audit.set_text_color(*NAVY)
        audit.cell(0, 6, "Assinatura manuscrita digital:", new_x="LMARGIN", new_y="NEXT")
        audit.ln(1)
        try:
            audit.image(assinatura_path, x=25, w=110, h=44)
            audit.ln(3)
        except Exception:
            audit.paragrafo_simples("(assinatura não disponível)")

    audit.ln(4)
    audit.set_font("Helvetica", "I", 8.5)
    audit.set_text_color(80, 80, 80)
    audit.multi_cell(0, 5.5,
        "Documento assinado eletronicamente em conformidade com a MP 2.200-2/2001 e Lei 14.063/2020. "
        "O hash SHA-256 garante a integridade e inalterabilidade do documento original. "
        "IP, geolocalização, selfie e assinatura manuscrita constituem prova de autoria e consentimento.", align="J")

    audit_bytes = bytes(audit.output())

    # ── Merge pypdf ──────────────────────────────────────────────────────────
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
