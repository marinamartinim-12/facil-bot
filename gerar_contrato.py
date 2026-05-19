"""
Geração de PDF para contratos da Fácil Financiamentos.
Usa fpdf2 para criar o documento original e o documento final assinado.
"""

import hashlib
import base64
import os
from datetime import datetime
from io import BytesIO
from pathlib import Path

from fpdf import FPDF
from PIL import Image


# Diretório de contratos
CONTRATOS_DIR = Path(os.getenv("CONTRATOS_DIR", "/data/contratos"))
CONTRATOS_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
class PDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(10, 40, 80)
        self.cell(0, 10, "FÁCIL FINANCIAMENTOS", align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(100, 100, 100)
        self.cell(0, 5, "Belo Horizonte, MG  |  23 anos no mercado", align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(3)
        self.set_draw_color(10, 40, 80)
        self.set_line_width(0.5)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"Página {self.page_no()} — Documento gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}", align="C")

    def section_title(self, title: str):
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(10, 40, 80)
        self.set_fill_color(235, 240, 250)
        self.cell(0, 7, f"  {title}", fill=True, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)
        self.set_text_color(30, 30, 30)

    def field_row(self, label: str, value: str):
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(80, 80, 80)
        self.cell(50, 6, label + ":", new_x="RIGHT", new_y="TOP")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(20, 20, 20)
        self.multi_cell(0, 6, value or "—")

    def body_text(self, text: str):
        self.set_font("Helvetica", "", 9)
        self.set_text_color(40, 40, 40)
        self.multi_cell(0, 5.5, text)
        self.ln(2)


# ─────────────────────────────────────────────────────────────────────────────
def gerar_pdf_contrato(lead_data: dict) -> tuple[bytes, str]:
    """
    Gera o PDF do contrato com os dados do lead.
    Retorna (bytes_do_pdf, hash_sha256).
    """
    pdf = PDF()
    pdf.set_margins(20, 20, 20)
    pdf.add_page()

    modalidade = (lead_data.get("modalidade") or "indefinido").lower()
    tipo = "REFINANCIAMENTO DE VEÍCULO" if "refin" in modalidade else "FINANCIAMENTO DE VEÍCULO"

    # Título
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(10, 40, 80)
    pdf.cell(0, 10, f"TERMO DE PRESTAÇÃO DE SERVIÇOS — {tipo}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Dados do cliente
    pdf.section_title("DADOS DO CLIENTE")
    pdf.field_row("Nome completo", lead_data.get("nome") or "—")
    pdf.field_row("CPF", lead_data.get("cpf") or "—")
    pdf.field_row("Data de nascimento", lead_data.get("data_nascimento") or "—")
    pdf.field_row("Telefone/WhatsApp", lead_data.get("telefone") or "—")
    pdf.ln(3)

    # Dados do serviço
    pdf.section_title("SERVIÇO CONTRATADO")
    pdf.field_row("Modalidade", tipo)
    pdf.field_row("Veículo de interesse", lead_data.get("carro_interesse") or "—")
    pdf.field_row("Data do contrato", datetime.now().strftime("%d/%m/%Y"))
    pdf.field_row("Local", "Belo Horizonte, MG")
    pdf.ln(3)

    # Objeto
    pdf.section_title("OBJETO DO CONTRATO")
    if "refin" in modalidade:
        objeto = (
            "A FÁCIL FINANCIAMENTOS, doravante denominada CONTRATADA, obriga-se a prestar serviços de "
            "intermediação para obtenção de crédito mediante refinanciamento de veículo automotor de propriedade "
            "do CONTRATANTE, junto às instituições financeiras credenciadas, nas melhores condições de taxas e prazos "
            "disponíveis no mercado. O processo ocorre 100% de forma digital, sem necessidade de deslocamento."
        )
    else:
        objeto = (
            "A FÁCIL FINANCIAMENTOS, doravante denominada CONTRATADA, obriga-se a prestar serviços de "
            "intermediação para aquisição de veículo automotor novo ou usado de terceiros (particular para particular), "
            "junto às 9 melhores instituições financeiras credenciadas do Brasil, buscando as melhores taxas e "
            "condições de parcelamento disponíveis para o perfil do CONTRATANTE."
        )
    pdf.body_text(objeto)

    # Obrigações
    pdf.section_title("OBRIGAÇÕES DAS PARTES")
    pdf.body_text(
        "DA CONTRATADA: Realizar análise de crédito junto às financeiras parceiras; apresentar as melhores "
        "propostas disponíveis; orientar o CONTRATANTE durante todo o processo; zelar pela confidencialidade "
        "dos dados pessoais fornecidos, em conformidade com a LGPD (Lei 13.709/2018).\n\n"
        "DO CONTRATANTE: Fornecer informações verdadeiras e documentação necessária; manter contato disponível "
        "durante o processo de análise; comparecer para assinatura do contrato com a instituição financeira "
        "quando exigido presencialmente."
    )

    # LGPD
    pdf.section_title("PROTEÇÃO DE DADOS — LGPD")
    pdf.body_text(
        "Os dados pessoais fornecidos pelo CONTRATANTE serão utilizados exclusivamente para fins de análise "
        "de crédito e intermediação contratual, sendo compartilhados apenas com as instituições financeiras "
        "parceiras mediante consentimento. O CONTRATANTE poderá solicitar exclusão de seus dados a qualquer "
        "momento, conforme art. 18 da Lei 13.709/2018."
    )

    # Assinatura eletrônica
    pdf.section_title("ASSINATURA ELETRÔNICA")
    pdf.body_text(
        "Este documento será assinado eletronicamente, com validade jurídica nos termos da Lei 14.063/2020 "
        "e do art. 10, §2º da MP 2.200-2/2001. A assinatura eletrônica simples é suficiente para contratos "
        "entre particulares. O registro de IP, geolocalização, horário e selfie do assinante constituem "
        "prova da autenticidade e do aceite."
    )
    pdf.ln(8)

    # Espaço para assinatura
    pdf.set_draw_color(150, 150, 150)
    pdf.set_line_width(0.3)
    mid = pdf.w / 2
    pdf.line(pdf.l_margin, pdf.get_y(), mid - 10, pdf.get_y())
    pdf.line(mid + 10, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(mid - pdf.l_margin - 10, 5, "CONTRATANTE", align="C")
    pdf.cell(20, 5, "")
    pdf.cell(0, 5, "FÁCIL FINANCIAMENTOS", align="C", new_x="LMARGIN", new_y="NEXT")

    raw = bytes(pdf.output())
    sha256 = hashlib.sha256(raw).hexdigest()
    return raw, sha256


# ─────────────────────────────────────────────────────────────────────────────
def salvar_pdf(conteudo: bytes, nome_arquivo: str) -> str:
    caminho = CONTRATOS_DIR / nome_arquivo
    caminho.write_bytes(conteudo)
    return str(caminho)


def base64_para_imagem(b64: str, caminho: Path) -> bool:
    """Salva uma imagem base64 em disco. Retorna True se OK."""
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


# ─────────────────────────────────────────────────────────────────────────────
def gerar_pdf_assinado(
    pdf_original_bytes: bytes,
    selfie_path: str,
    assinatura_path: str,
    dados_auditoria: dict,
) -> bytes:
    """
    Gera o PDF final com página de auditoria anexada ao contrato original.
    """
    pdf = PDF()
    pdf.set_margins(20, 20, 20)

    # ── Página de auditoria ──
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(10, 40, 80)
    pdf.cell(0, 10, "PÁGINA DE AUDITORIA — ASSINATURA ELETRÔNICA", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.section_title("REGISTRO DE ASSINATURA")
    pdf.field_row("Assinado em", dados_auditoria.get("assinado_em", "—"))
    pdf.field_row("IP do assinante", dados_auditoria.get("ip", "—"))
    pdf.field_row("Geolocalização", dados_auditoria.get("geo", "—"))
    pdf.field_row("Hash do documento original", dados_auditoria.get("hash_doc", "—"))
    pdf.field_row("Nome do assinante", dados_auditoria.get("nome", "—"))
    pdf.field_row("CPF do assinante", dados_auditoria.get("cpf", "—"))
    pdf.ln(4)

    # Selfie
    if selfie_path and Path(selfie_path).exists():
        pdf.section_title("SELFIE DO ASSINANTE")
        pdf.ln(2)
        try:
            pdf.image(selfie_path, x=pdf.l_margin, w=60, h=80)
            pdf.ln(4)
        except Exception:
            pdf.body_text("(selfie não disponível)")

    # Assinatura
    if assinatura_path and Path(assinatura_path).exists():
        pdf.section_title("ASSINATURA DIGITAL")
        pdf.ln(2)
        try:
            pdf.image(assinatura_path, x=pdf.l_margin, w=100, h=40)
            pdf.ln(4)
        except Exception:
            pdf.body_text("(assinatura não disponível)")

    pdf.section_title("VALIDADE JURÍDICA")
    pdf.body_text(
        "Este documento foi assinado eletronicamente com validade jurídica nos termos da Lei 14.063/2020. "
        "Os dados de auditoria acima constituem prova inequívoca da autenticidade da assinatura e do "
        "consentimento do assinante. O hash SHA-256 do documento original garante sua integridade."
    )

    audit_bytes = bytes(pdf.output())

    # Concatena: original + auditoria usando bytes diretos
    # (fpdf2 não suporta merge nativo, então retornamos só a página de auditoria
    #  e no endpoint fazemos o merge com PyPDF2 se disponível, senão só auditoria)
    return audit_bytes
