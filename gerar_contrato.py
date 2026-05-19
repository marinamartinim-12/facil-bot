"""
Fácil Financiamentos — Geração de contratos PDF profissionais
Duas páginas: Requerimento de Intermediação + Dados da Conta para Pagamento
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

# ── Paleta ────────────────────────────────────────────────────────────────────
NAVY   = (13,  43,  78)
NAVY2  = (22,  65, 115)
GOLD   = (200, 155,   0)
GOLD_L = (245, 196,   0)
WHITE  = (255, 255, 255)
GRAY   = (240, 243, 248)
TEXT   = ( 22,  22,  22)
MUTED  = (110, 120, 135)
LINE   = (210, 218, 228)


# ── Classe base ───────────────────────────────────────────────────────────────
class ContratoPDF(FPDF):
    def __init__(self, doc_id: str = "", titulo_doc: str = "REQUERIMENTO DE INTERMEDIAÇÃO"):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.doc_id    = doc_id
        self.titulo_doc = titulo_doc
        self.set_auto_page_break(auto=True, margin=28)
        self.set_margins(18, 42, 18)   # deixa espaço para header

    # ── Cabeçalho ─────────────────────────────────────────────────────────────
    def header(self):
        # Faixa navy
        self.set_fill_color(*NAVY)
        self.rect(0, 0, self.w, 35, "F")

        # Nome empresa
        self.set_font("Helvetica", "B", 20)
        self.set_text_color(*WHITE)
        self.set_xy(18, 6)
        self.cell(0, 10, "FÁCIL FINANCIAMENTOS", new_x="LMARGIN", new_y="NEXT")

        # Subtítulo
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(185, 205, 235)
        self.set_x(18)
        self.cell(0, 5,
            "Rua Lauro Ignácio Ponte, 08 – Sala 202 – Parque São Pedro – Venda Nova – Belo Horizonte / MG",
            new_x="LMARGIN", new_y="NEXT")

        # Linha dourada
        self.set_draw_color(*GOLD_L)
        self.set_line_width(1.2)
        self.line(0, 35, self.w, 35)
        self.set_line_width(0.2)
        self.set_draw_color(*LINE)

        # Título do documento (centralizado sob a faixa)
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(*NAVY)
        self.set_xy(18, 38)
        self.cell(0, 7, self.titulo_doc, align="C", new_x="LMARGIN", new_y="NEXT")

        self.set_font("Helvetica", "I", 8.5)
        self.set_text_color(*MUTED)
        self.set_x(18)
        self.cell(0, 5, "Prestação de Serviços", align="C", new_x="LMARGIN", new_y="NEXT")

        self.ln(3)

    # ── Rodapé ────────────────────────────────────────────────────────────────
    def footer(self):
        self.set_y(-18)
        self.set_draw_color(*LINE)
        self.set_line_width(0.3)
        self.line(18, self.get_y(), self.w - 18, self.get_y())
        self.ln(1.5)
        self.set_font("Helvetica", "", 6.5)
        self.set_text_color(*MUTED)
        self.cell(self.w / 2 - 18, 5, f"Doc. nº {self.doc_id}", align="L")
        self.cell(0, 5,
            f"Pág. {self.page_no()}  |  Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}",
            align="R")

    # ── Helpers ───────────────────────────────────────────────────────────────
    def secao(self, titulo: str):
        """Cabeçalho de seção com fundo navy."""
        self.ln(2)
        self.set_fill_color(*NAVY2)
        self.set_text_color(*WHITE)
        self.set_font("Helvetica", "B", 8.5)
        self.cell(0, 6.5, f"   {titulo}", fill=True, new_x="LMARGIN", new_y="NEXT")
        self.ln(1.5)
        self.set_text_color(*TEXT)

    def campo(self, rotulo: str, valor: str, w_rot: float = 38, w_val: float = 0, nl: bool = True):
        """Campo único: rótulo em cinza + valor em negrito."""
        w_val = w_val or (self.w - self.l_margin - self.r_margin - w_rot)
        self.set_font("Helvetica", "B", 7.5)
        self.set_text_color(*MUTED)
        self.cell(w_rot, 5.5, rotulo.upper() + ":", new_x="RIGHT", new_y="TOP")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*TEXT)
        if nl:
            self.multi_cell(w_val, 5.5, valor or "—")
        else:
            self.cell(w_val, 5.5, valor or "—", new_x="RIGHT", new_y="TOP")

    def linha_dupla(self, r1, v1, r2, v2, wr1=30, wv1=52, wr2=28):
        """Dois campos na mesma linha."""
        self.campo(r1, v1, w_rot=wr1, w_val=wv1, nl=False)
        wv2 = self.w - self.l_margin - self.r_margin - wr1 - wv1 - wr2
        self.campo(r2, v2, w_rot=wr2, w_val=wv2, nl=True)

    def linha_tripla(self, r1, v1, r2, v2, r3, v3, wr=22, wv=32):
        """Três campos na mesma linha."""
        self.campo(r1, v1, w_rot=wr, w_val=wv, nl=False)
        self.campo(r2, v2, w_rot=wr, w_val=wv, nl=False)
        wv3 = self.w - self.l_margin - self.r_margin - (wr + wv) * 2 - wr
        self.campo(r3, v3, w_rot=wr, w_val=wv3, nl=True)

    def paragrafo(self, texto: str):
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*TEXT)
        self.multi_cell(0, 5.2, texto)
        self.ln(1.5)

    def destaque(self, texto: str):
        """Parágrafo em caixa alta e negrito — para a declaração final."""
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*NAVY)
        self.multi_cell(0, 5.5, texto, align="C")
        self.ln(2)

    def linha_assinatura(self, x: float, label: str, w: float = 80):
        y = self.get_y()
        self.set_draw_color(*MUTED)
        self.set_line_width(0.5)
        self.line(x, y, x + w, y)
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(*MUTED)
        self.set_xy(x, y + 1.5)
        self.cell(w, 4, label, align="C")

    def separador(self):
        self.ln(2)
        self.set_draw_color(*LINE)
        self.set_line_width(0.2)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def caixa_info(self, rotulo: str, valor: str):
        """Linha de dado bancário com fundo alternado."""
        self.set_fill_color(*GRAY)
        self.set_font("Helvetica", "B", 8)
        self.set_text_color(*MUTED)
        self.cell(42, 6.5, "  " + rotulo.upper(), fill=True, new_x="RIGHT", new_y="TOP")
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*TEXT)
        self.cell(0, 6.5, "  " + (valor or "—"), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*LINE)
        y = self.get_y()
        self.line(self.l_margin, y, self.w - self.r_margin, y)


# ── Geração do PDF ────────────────────────────────────────────────────────────
def gerar_pdf_contrato(dados: dict, doc_id: str) -> tuple[bytes, str]:
    """
    Gera o PDF completo com 2 páginas.
    dados: dict com todos os campos do formulário.
    Retorna (bytes, hash_sha256).
    """
    pdf = ContratoPDF(doc_id=doc_id)

    # ══════════════════════════════════════════════════════════════════════════
    # PÁGINA 1 — REQUERIMENTO DE INTERMEDIAÇÃO
    # ══════════════════════════════════════════════════════════════════════════
    pdf.add_page()

    # Número do documento e data
    pdf.set_fill_color(*GRAY)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*MUTED)
    pdf.cell(0, 6,
        f"  Nº {doc_id}    |    Belo Horizonte, {dados.get('data_contrato', datetime.now().strftime('%d de %B de %Y'))}",
        fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # ── DADOS DO REQUERENTE ───────────────────────────────────────────────────
    pdf.secao("1. DADOS DO REQUERENTE")
    pdf.campo("Nome completo", dados.get("req_nome", "").upper())
    pdf.linha_dupla("CPF", dados.get("req_cpf", ""), "RG", dados.get("req_rg", ""),
                    wr1=10, wv1=50, wr2=10)
    pdf.campo("Endereço",
        f"{dados.get('req_rua','')}, Nº {dados.get('req_numero','')} – {dados.get('req_bairro','')} – CEP {dados.get('req_cep','')} – {dados.get('req_cidade','')}")
    pdf.campo("Celular / WhatsApp", dados.get("req_celular", ""), w_rot=38, w_val=60)
    pdf.separador()

    # ── DADOS DO VEÍCULO ──────────────────────────────────────────────────────
    pdf.secao("2. DADOS DO VEÍCULO")
    pdf.linha_dupla("Marca / Modelo", dados.get("vei_modelo", "").upper(),
                    "Placa", dados.get("vei_placa", "").upper(), wr1=26, wv1=65, wr2=12)
    pdf.linha_tripla("Ano", dados.get("vei_ano", ""),
                     "Cor", dados.get("vei_cor", "").upper(),
                     "RENAVAM", dados.get("vei_renavam", ""),
                     wr=14, wv=34)
    pdf.campo("Chassi", dados.get("vei_chassi", "").upper())
    pdf.separador()

    # ── OBJETO DO REQUERIMENTO ────────────────────────────────────────────────
    modalidade = (dados.get("modalidade") or "refinanciamento").lower()
    if "refin" in modalidade:
        verbo = "refinanciamento"
        acao  = "o refinanciamento"
    else:
        verbo = "financiamento"
        acao  = "o financiamento da aquisição"

    proprietario = dados.get("prop_nome", "").upper()
    prop_cpf     = dados.get("prop_cpf", "")
    prop_fone    = dados.get("prop_telefone", "")

    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*TEXT)
    pdf.multi_cell(0, 5.5,
        f"Eu {dados.get('req_nome','').upper()}, qualificado(a) acima, requeiro que seja INTERMEDIADO "
        f"{acao} do veículo de marca {dados.get('vei_modelo','').upper()}, placa {dados.get('vei_placa','').upper()}, "
        f"ano {dados.get('vei_ano','')}, cor {dados.get('vei_cor','').upper()}, RENAVAM {dados.get('vei_renavam','')}, "
        f"CHASSI {dados.get('vei_chassi','').upper()}, adquirido fruto de negociação direta com o seu legítimo proprietário/representante:"
    )
    pdf.ln(3)

    # ── DADOS DO PROPRIETÁRIO / VENDEDOR ─────────────────────────────────────
    pdf.secao("3. DADOS DO PROPRIETÁRIO / VENDEDOR")
    pdf.campo("Nome completo", proprietario)
    pdf.linha_dupla("CPF", prop_cpf, "Telefone", prop_fone, wr1=10, wv1=55, wr2=16)
    pdf.paragrafo(
        f"O(A) Sr(a). {proprietario} se responsabiliza civil e criminalmente pelo veículo, "
        "inclusive pela documentação apresentada."
    )
    pdf.separador()

    # ── CONDIÇÕES FINANCEIRAS ─────────────────────────────────────────────────
    pdf.secao("4. CONDIÇÕES FINANCEIRAS")
    pdf.linha_dupla("Valor líquido liberado",
                    f"R$ {dados.get('fin_valor_liquido','')}",
                    "Banco",
                    dados.get("fin_banco", "").upper(),
                    wr1=36, wv1=45, wr2=14)
    pdf.linha_dupla("Nº de parcelas",
                    f"{dados.get('fin_parcelas','')}x de R$ {dados.get('fin_valor_parcela','')}",
                    "1º vencimento",
                    dados.get("fin_vencimento", ""),
                    wr1=26, wv1=60, wr2=22)
    pdf.paragrafo(
        f"O valor líquido de R$ {dados.get('fin_valor_liquido','')} já está descontado de todas as despesas, "
        "consultoria, comissões, taxas, impostos e intermediação."
    )
    pdf.paragrafo(
        "Neste ato o requerente que NÃO adquiriu o veículo junto à empresa, sendo que a mesma "
        "não se responsabiliza pela documentação e qualidade do mesmo."
    )
    pdf.separador()

    # ── DECLARAÇÃO ────────────────────────────────────────────────────────────
    pdf.destaque(
        "DECLARO AINDA, QUE NADA MAIS ME FOI PROMETIDO ALÉM DO QUE\n"
        "ESTÁ ESPECIFICADO NESTE REQUERIMENTO."
    )

    # Data e local
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*MUTED)
    pdf.cell(0, 6, f"Belo Horizonte, {dados.get('data_contrato', datetime.now().strftime('%d de %B de %Y'))}.",
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(14)

    # Linhas de assinatura
    marg = pdf.l_margin
    largura_pag = pdf.w - pdf.l_margin - pdf.r_margin
    pdf.linha_assinatura(marg, "Requerente", w=largura_pag / 2 - 8)
    pdf.set_xy(pdf.l_margin + largura_pag / 2 + 8, pdf.get_y() - 5.5)
    pdf.linha_assinatura(pdf.l_margin + largura_pag / 2 + 8,
                          "Proprietário / Vendedor", w=largura_pag / 2 - 8)
    pdf.ln(12)

    # Faixa rodapé institucional
    pdf.set_fill_color(*GRAY)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(*MUTED)
    pdf.cell(0, 6,
        "Fácil Financiamentos  ·  Rua Lauro Ignácio Ponte, 08 – Sala 202 – Parque São Pedro – Venda Nova – BH/MG",
        fill=True, align="C", new_x="LMARGIN", new_y="NEXT")

    # ══════════════════════════════════════════════════════════════════════════
    # PÁGINA 2 — DADOS DA CONTA PARA PAGAMENTO
    # ══════════════════════════════════════════════════════════════════════════
    pdf.titulo_doc = "AUTORIZAÇÃO DE PAGAMENTO"
    pdf.add_page()

    # Nº e data
    pdf.set_fill_color(*GRAY)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*MUTED)
    pdf.cell(0, 6,
        f"  Nº {doc_id}    |    Belo Horizonte, {dados.get('data_contrato', datetime.now().strftime('%d de %B de %Y'))}",
        fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # Parágrafo de autorização
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*TEXT)
    pdf.multi_cell(0, 5.5,
        f"Eu {dados.get('req_nome','').upper()}, CPF {dados.get('req_cpf','')}, RG {dados.get('req_rg','')}, "
        f"autorizo o pagamento da importância de R$ {dados.get('pag_valor','')} referente ao {verbo} "
        f"do veículo de marca {dados.get('vei_modelo','').upper()}, placa {dados.get('vei_placa','').upper()}, "
        f"ano {dados.get('vei_ano','')}, cor {dados.get('vei_cor','').upper()}, "
        f"que foi negociado junto ao banco {dados.get('pag_banco_negociador','').upper()} "
        f"na conta abaixo discriminada:"
    )
    pdf.ln(4)

    # ── DADOS BANCÁRIOS ───────────────────────────────────────────────────────
    pdf.secao("DADOS BANCÁRIOS DO BENEFICIÁRIO")
    pdf.caixa_info("Nome",        dados.get("pag_nome_beneficiario", "").upper())
    pdf.caixa_info("CPF",         dados.get("pag_cpf_beneficiario", ""))
    pdf.caixa_info("Banco",       dados.get("pag_banco", "").upper())
    pdf.caixa_info("Agência",     dados.get("pag_agencia", ""))
    pdf.caixa_info("Conta",       dados.get("pag_conta", ""))
    pdf.caixa_info("PIX",         dados.get("pag_pix", ""))
    pdf.ln(1)

    # Valor em destaque
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(*WHITE)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 9, f"   VALOR:  R$ {dados.get('pag_valor','')}",
             fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(14)

    # Assinaturas
    pdf.linha_assinatura(pdf.l_margin, "Requerente — Autorizo o pagamento acima", w=largura_pag)
    pdf.ln(3)

    # Faixa rodapé
    pdf.set_fill_color(*GRAY)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(*MUTED)
    pdf.cell(0, 6,
        "Fácil Financiamentos  ·  Rua Lauro Ignácio Ponte, 08 – Sala 202 – Parque São Pedro – Venda Nova – BH/MG",
        fill=True, align="C", new_x="LMARGIN", new_y="NEXT")

    # ── Gera bytes e hash ─────────────────────────────────────────────────────
    raw = bytes(pdf.output())
    sha256 = hashlib.sha256(raw).hexdigest()
    return raw, sha256


# ── Utilitários de arquivo ────────────────────────────────────────────────────
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
    """
    Concatena o contrato original com uma página de auditoria.
    Usa pypdf para o merge.
    """
    from pypdf import PdfWriter, PdfReader
    from io import BytesIO

    # ── Gera página de auditoria ──────────────────────────────────────────────
    pdf_audit = ContratoPDF(doc_id=doc_id, titulo_doc="PÁGINA DE AUDITORIA — ASSINATURA ELETRÔNICA")
    pdf_audit.set_margins(18, 42, 18)
    pdf_audit.add_page()

    pdf_audit.secao("REGISTRO DA ASSINATURA ELETRÔNICA")
    pdf_audit.campo("Assinado em",       dados_auditoria.get("assinado_em", "—"))
    pdf_audit.campo("IP do assinante",   dados_auditoria.get("ip", "—"))
    pdf_audit.campo("Geolocalização",    dados_auditoria.get("geo", "não fornecida"))
    pdf_audit.campo("Nome do assinante", dados_auditoria.get("nome", "—"))
    pdf_audit.campo("CPF do assinante",  dados_auditoria.get("cpf", "—"))
    pdf_audit.campo("Hash SHA-256 (doc original)", dados_auditoria.get("hash_doc", "—"))
    pdf_audit.separador()

    if selfie_path and Path(selfie_path).exists():
        pdf_audit.secao("SELFIE DO ASSINANTE")
        pdf_audit.ln(2)
        try:
            pdf_audit.image(selfie_path, x=pdf_audit.l_margin, w=55, h=70)
            pdf_audit.ln(3)
        except Exception:
            pdf_audit.paragrafo("(selfie não disponível)")

    if assinatura_path and Path(assinatura_path).exists():
        pdf_audit.secao("ASSINATURA MANUSCRITA DIGITAL")
        pdf_audit.ln(2)
        try:
            pdf_audit.image(assinatura_path, x=pdf_audit.l_margin, w=110, h=44)
            pdf_audit.ln(3)
        except Exception:
            pdf_audit.paragrafo("(assinatura não disponível)")

    pdf_audit.secao("VALIDADE JURÍDICA")
    pdf_audit.paragrafo(
        "Documento assinado eletronicamente em conformidade com a MP 2.200-2/2001 e Lei 14.063/2020. "
        "O hash SHA-256 do documento original garante sua integridade e inalterabilidade. "
        "Os dados de auditoria acima (IP, geolocalização, selfie e assinatura manuscrita) "
        "constituem prova plena de autoria e consentimento do assinante."
    )

    audit_bytes = bytes(pdf_audit.output())

    # ── Merge com pypdf ───────────────────────────────────────────────────────
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
        print(f"Erro no merge PDF: {e} — retornando só auditoria")
        return audit_bytes
