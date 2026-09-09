from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .status_catalog import rotulo as status_rotulo
from .version import VERSION
PAGE_SIZE = landscape(A4)

NAVY = colors.HexColor("#17365D")
BLUE = colors.HexColor("#2F75B5")
LIGHT_BLUE = colors.HexColor("#D9EAF7")
LIGHT_GRAY = colors.HexColor("#F2F4F7")
MID_GRAY = colors.HexColor("#D0D5DD")
DARK = colors.HexColor("#101828")
MUTED = colors.HexColor("#475467")
GREEN = colors.HexColor("#067647")
GREEN_BG = colors.HexColor("#DCFAE6")
ORANGE = colors.HexColor("#B54708")
ORANGE_BG = colors.HexColor("#FFEAD5")
RED = colors.HexColor("#B42318")
RED_BG = colors.HexColor("#FEE4E2")
PURPLE = colors.HexColor("#6941C6")
PURPLE_BG = colors.HexColor("#F4EBFF")

CRITICAL_STATUSES = {
    "ERRO", "INCOMPLETO", "NAO_EXISTE", "CURSOR_INVALIDO", "HEARTBEAT_ERRO",
    "SEM_BACKUP", "SEM_PASTA_VMS", "VM_NAO_ENCONTRADA", "ERRO_EVENTOS_VMS",
}
WARNING_STATUSES = {
    "SEM_MUDANCAS", "SOMENTE_EXCLUSOES", "BASELINE_CRIADA", "CURSOR_RECRIADO",
    "HEARTBEAT_ATRASADO", "HEARTBEAT_AUSENTE", "ATRASADO", "IRREGULAR",
    "EVENTOS_VMS_INCOMPLETOS", "BASELINE_NAO_CRIADA", "SEM_UPLOAD_RECENTE", "VAZIA", "AMOSTRAGEM_INCONCLUSIVA",
}
INFO_STATUSES = {
    "EM_APRENDIZADO", "AMOSTRA_INSUFICIENTE", "MIGRADO_PARA_DRIVE", "NAO_APLICAVEL", "NAO_AUDITADO",
    "BASELINE_RECRIADA_POLITICA", "BASELINE_RECRIADA_MANUAL", "INVENTARIO_BLOQUEADO",
}
OK_STATUSES = {"OK", "ATIVIDADE_CONFIRMADA", "BACKUP_CONFIRMADO"}


def _safe(value: Any) -> str:
    if value in (None, ""):
        return "-"
    text = str(value)
    # ReportLab Paragraph uses a small XML subset.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _short(value: Any, limit: int = 160) -> str:
    text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _dt_br(value: Any) -> str:
    if not value:
        return "-"
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return str(value)
    return dt.strftime("%d/%m/%Y %H:%M:%S")


def _status_level(status: str) -> int:
    status = str(status or "").upper()
    if status in CRITICAL_STATUSES or status == "ERRO":
        return 3
    if status in WARNING_STATUSES or status == "ATENCAO":
        return 2
    if status in INFO_STATUSES:
        return 1
    return 0


def _status_colors(status: str) -> tuple[colors.Color, colors.Color]:
    level = _status_level(status)
    if level == 3:
        return RED_BG, RED
    if level == 2:
        return ORANGE_BG, ORANGE
    if level == 1:
        return PURPLE_BG, PURPLE
    return GREEN_BG, GREEN


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle", parent=base["Title"], fontName="Helvetica-Bold", fontSize=19,
            leading=22, textColor=NAVY, spaceAfter=4,
        ),
        "subtitle": ParagraphStyle(
            "ReportSubtitle", parent=base["Normal"], fontName="Helvetica", fontSize=9,
            leading=12, textColor=MUTED, spaceAfter=10,
        ),
        "h1": ParagraphStyle(
            "ReportH1", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=13,
            leading=16, textColor=NAVY, spaceBefore=5, spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "ReportH2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=10,
            leading=13, textColor=BLUE, spaceBefore=4, spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "ReportBody", parent=base["BodyText"], fontName="Helvetica", fontSize=8,
            leading=10, textColor=DARK,
        ),
        "small": ParagraphStyle(
            "ReportSmall", parent=base["BodyText"], fontName="Helvetica", fontSize=6.7,
            leading=8.3, textColor=DARK,
        ),
        "tiny": ParagraphStyle(
            "ReportTiny", parent=base["BodyText"], fontName="Helvetica", fontSize=5.8,
            leading=7.1, textColor=DARK,
        ),
        "center": ParagraphStyle(
            "ReportCenter", parent=base["BodyText"], fontName="Helvetica", fontSize=7,
            leading=8.5, alignment=TA_CENTER, textColor=DARK,
        ),
        "card_label": ParagraphStyle(
            "CardLabel", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=7,
            leading=8, alignment=TA_CENTER, textColor=MUTED,
        ),
        "card_value": ParagraphStyle(
            "CardValue", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=15,
            leading=17, alignment=TA_CENTER, textColor=NAVY,
        ),
    }


def _p(value: Any, style: ParagraphStyle, limit: int | None = None) -> Paragraph:
    text = _short(value, limit) if limit else str(value if value not in (None, "") else "-")
    return Paragraph(_safe(text), style)


def _page_header_footer(canvas: Any, doc: Any) -> None:
    canvas.saveState()
    width, height = PAGE_SIZE
    canvas.setStrokeColor(MID_GRAY)
    canvas.setLineWidth(0.4)
    canvas.line(14 * mm, 12 * mm, width - 14 * mm, 12 * mm)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(14 * mm, 7.5 * mm, f"Auditor Dropbox v{VERSION} - uso interno")
    canvas.drawRightString(width - 14 * mm, 7.5 * mm, f"Pagina {doc.page}")
    canvas.restoreState()


def _doc(path: Path, title: str) -> SimpleDocTemplate:
    path.parent.mkdir(parents=True, exist_ok=True)
    return SimpleDocTemplate(
        str(path), pagesize=PAGE_SIZE, rightMargin=12 * mm, leftMargin=12 * mm,
        topMargin=12 * mm, bottomMargin=16 * mm, title=title,
        author="Auditor Dropbox", subject="Auditoria de backups no Dropbox",
    )


def _header(story: list[Any], title: str, subtitle: str, executado_em: Any) -> None:
    st = _styles()
    story.append(_p(title, st["title"]))
    story.append(_p(f"{subtitle} | Executado em {_dt_br(executado_em)}", st["subtitle"]))
    story.append(Spacer(1, 2 * mm))


def _cards(items: Iterable[tuple[str, Any, str]]) -> Table:
    st = _styles()
    cells = []
    backgrounds: list[colors.Color] = []
    for label, value, kind in items:
        bg, fg = {
            "ok": (GREEN_BG, GREEN), "warning": (ORANGE_BG, ORANGE),
            "error": (RED_BG, RED), "info": (PURPLE_BG, PURPLE),
        }.get(kind, (LIGHT_BLUE, NAVY))
        value_style = ParagraphStyle(
            f"CardValue{len(cells)}", parent=st["card_value"], textColor=fg,
        )
        cells.append([_p(label, st["card_label"]), _p(value, value_style)])
        backgrounds.append(bg)
    if not cells:
        cells = [[_p("Sem dados", st["card_label"]), _p("0", st["card_value"])]]
        backgrounds = [LIGHT_GRAY]
    table = Table([cells], colWidths=[(PAGE_SIZE[0] - 24 * mm) / len(cells)] * len(cells))
    style = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), 0.5, MID_GRAY),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.white),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    for idx, bg in enumerate(backgrounds):
        style.append(("BACKGROUND", (idx, 0), (idx, 0), bg))
    table.setStyle(TableStyle(style))
    return table


def _table(
    headers: list[str],
    rows: list[list[Any]],
    widths_mm: list[float],
    status_col: int | None = None,
    small: bool = True,
) -> Table:
    st = _styles()
    body_style = st["small"] if small else st["body"]
    header_style = ParagraphStyle(
        "TableHeader", parent=st["center"], fontName="Helvetica-Bold", textColor=colors.white,
    )
    data: list[list[Any]] = [[_p(h, header_style) for h in headers]]
    statuses: list[str] = []
    for row_index, row in enumerate(rows):
        status = str(row[status_col]) if status_col is not None and status_col < len(row) else ""
        statuses.append(status)
        formatted: list[Any] = []
        for col_index, cell in enumerate(row):
            if isinstance(cell, Paragraph):
                formatted.append(cell)
            elif status_col is not None and col_index == status_col:
                _, fg = _status_colors(status)
                status_style = ParagraphStyle(
                    f"StatusCell{row_index}", parent=body_style, fontName="Helvetica-Bold", textColor=fg,
                )
                formatted.append(_p(status_rotulo(status) if status else cell, status_style))
            else:
                formatted.append(_p(cell, body_style))
        data.append(formatted)
    table = Table(data, colWidths=[w * mm for w in widths_mm], repeatRows=1, hAlign="LEFT")
    style: list[tuple[Any, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.35, MID_GRAY),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for idx in range(1, len(data)):
        if idx % 2 == 0:
            style.append(("BACKGROUND", (0, idx), (-1, idx), LIGHT_GRAY))
        if status_col is not None:
            bg, fg = _status_colors(statuses[idx - 1])
            style.extend([
                ("BACKGROUND", (status_col, idx), (status_col, idx), bg),
                ("TEXTCOLOR", (status_col, idx), (status_col, idx), fg),
                ("FONTNAME", (status_col, idx), (status_col, idx), "Helvetica-Bold"),
            ])
    table.setStyle(TableStyle(style))
    return table


def _section_title(text: str) -> Paragraph:
    return _p(text, _styles()["h1"])


def _body(text: str) -> Paragraph:
    return _p(text, _styles()["body"])


def gerar_pdf_arquivos(
    registros: list[dict[str, Any]],
    modo: str,
    executado_em: Any,
    caminho: Path,
) -> None:
    story: list[Any] = []
    _header(story, "Auditoria de Arquivos no Dropbox", f"Modo {modo} - resumo operacional por empresa", executado_em)
    counts: dict[str, int] = {}
    for reg in registros:
        status = str(reg.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    ok = sum(v for k, v in counts.items() if _status_level(k) == 0)
    info = sum(v for k, v in counts.items() if _status_level(k) == 1)
    warning = sum(v for k, v in counts.items() if _status_level(k) == 2)
    error = sum(v for k, v in counts.items() if _status_level(k) == 3)
    story.append(_cards([
        ("Empresas", len(registros), "neutral"), ("OK", ok, "ok"),
        ("Informativo", info, "info"), ("Atencao", warning, "warning"), ("Erro", error, "error"),
    ]))
    story.append(Spacer(1, 4 * mm))

    priorities = [r for r in registros if _status_level(str(r.get("status"))) >= 2]
    story.append(_section_title("1. Itens que exigem verificacao"))
    if priorities:
        rows = [[
            r.get("empresa"), r.get("status"),
            r.get("arquivos_criados_ou_alterados") or r.get("arquivos_total") or 0,
            r.get("arquivos_excluidos") or 0, _dt_br(r.get("ultima_modificacao")),
            r.get("paginas_lidas") or 0, f"{r.get('tempo_segundos') or 0}s",
            _short(r.get("observacao"), 210),
        ] for r in sorted(priorities, key=lambda x: (-_status_level(str(x.get("status"))), str(x.get("empresa", "")).lower()))]
        story.append(_table(
            ["Empresa", "Status", "Atividade", "Exclusoes", "Ultima atividade", "Paginas", "Tempo", "Acao / observacao"],
            rows, [42, 31, 20, 20, 35, 17, 18, 85], status_col=1,
        ))
    else:
        story.append(_body("Nenhuma empresa exige verificacao imediata nesta execucao."))

    story.append(Spacer(1, 4 * mm))
    story.append(_section_title("2. Resultado completo por empresa"))
    all_rows = [[
        r.get("empresa"), r.get("status"),
        r.get("arquivos_criados_ou_alterados") or r.get("arquivos_total") or 0,
        r.get("arquivos_excluidos") or 0, _dt_br(r.get("ultima_modificacao")),
        r.get("paginas_lidas") or 0, f"{r.get('tempo_segundos') or 0}s",
        _short(r.get("observacao"), 170),
    ] for r in sorted(registros, key=lambda x: (-_status_level(str(x.get("status"))), str(x.get("empresa", "")).lower()))]
    story.append(_table(
        ["Empresa", "Status", "Atividade", "Exclusoes", "Ultima atividade", "Paginas", "Tempo", "Observacao"],
        all_rows, [42, 31, 20, 20, 35, 17, 18, 85], status_col=1,
    ))
    story.append(Spacer(1, 4 * mm))
    story.append(_body("Os detalhes completos, caminhos e amostras permanecem disponiveis nos arquivos CSV e JSON da mesma execucao."))
    doc = _doc(caminho, f"Auditoria de Arquivos Dropbox v{VERSION}")
    doc.build(story, onFirstPage=_page_header_footer, onLaterPages=_page_header_footer)


def gerar_pdf_vms(
    registros: list[dict[str, Any]],
    resumo: dict[str, Any],
    executado_em: Any,
    caminho: Path,
    backups_fora_do_lugar: list[dict[str, Any]] | None = None,
    meta_backups_fora: dict[str, Any] | None = None,
) -> None:
    story: list[Any] = []
    _header(story, "Auditoria de Backups de VMs no Dropbox", "Historico de uploads e verificacao por VM", executado_em)
    vm_regs = [r for r in registros if r.get("tipo_registro") == "VM"]
    empresa_regs = [r for r in registros if r.get("tipo_registro") == "EMPRESA"]
    vm_counts = {"ok": 0, "info": 0, "warning": 0, "error": 0}
    for reg in vm_regs:
        level = _status_level(str(reg.get("status")))
        vm_counts[{0: "ok", 1: "info", 2: "warning", 3: "error"}[level]] += 1
    fora_escopo = sum(1 for r in empresa_regs if str(r.get("status")) in {"MIGRADO_PARA_DRIVE", "NAO_APLICAVEL", "NAO_AUDITADO"})
    story.append(_cards([
        ("Empresas", resumo.get("total_empresas", 0), "neutral"),
        ("VMs", len(vm_regs), "neutral"), ("OK", vm_counts["ok"], "ok"),
        ("Coletando histórico", vm_counts["info"], "info"),
        ("Atencao", vm_counts["warning"], "warning"), ("Erro", vm_counts["error"], "error"),
        ("Fora do escopo", fora_escopo, "neutral"),
    ]))
    story.append(Spacer(1, 4 * mm))

    priorities = [r for r in registros if _status_level(str(r.get("status"))) >= 2]
    story.append(_section_title("1. Itens que exigem verificacao"))
    if priorities:
        rows = [[
            r.get("empresa"), r.get("vm") or "-", r.get("status"),
            _dt_br(r.get("ultima_atualizacao_dropbox") or r.get("ultimo_backup")),
            r.get("periodicidade_detectada") or "-", r.get("confianca") or "-",
            r.get("atraso_horas") if r.get("atraso_horas") not in (None, "") else "-",
            _short(r.get("observacao"), 220),
        ] for r in sorted(priorities, key=lambda x: (-_status_level(str(x.get("status"))), str(x.get("empresa", "")).lower(), str(x.get("vm", ""))))]
        story.append(_table(
            ["Empresa", "VM", "Status", "Ultima atividade", "Frequencia", "Confianca", "Atraso h", "Acao / observacao"],
            rows, [35, 18, 28, 32, 22, 21, 15, 95], status_col=2,
        ))
    else:
        story.append(_body("Nenhuma VM exige verificacao imediata nesta execucao."))

    story.append(Spacer(1, 4 * mm))
    story.append(_section_title("2. Resumo por empresa"))
    grouped: dict[str, dict[str, Any]] = {}
    for reg in registros:
        empresa = str(reg.get("empresa") or "")
        if not empresa:
            continue
        item = grouped.setdefault(empresa, {"total": 0, "ok": 0, "info": 0, "warning": 0, "error": 0, "empresa_info": 0})
        level = _status_level(str(reg.get("status")))
        if reg.get("tipo_registro") == "VM":
            item["total"] += 1
            item[{0: "ok", 1: "info", 2: "warning", 3: "error"}[level]] += 1
        elif level == 1:
            item["empresa_info"] += 1
        elif level >= 2:
            item[{2: "warning", 3: "error"}[level]] += 1
    company_rows = []
    for empresa, item in sorted(grouped.items(), key=lambda x: (-x[1]["error"], -x[1]["warning"], x[0].lower())):
        status = "ERRO" if item["error"] else ("ATENCAO" if item["warning"] else ("INFORMATIVO" if item["info"] or item["empresa_info"] else "OK"))
        company_rows.append([empresa, status, item["total"], item["ok"], item["info"], item["warning"], item["error"]])
    story.append(_table(
        ["Empresa", "Status geral", "VMs", "OK", "Aprendendo", "Atencao", "Erro"],
        company_rows, [75, 35, 22, 22, 30, 25, 22], status_col=1, small=False,
    ))

    ok_regs = [r for r in vm_regs if _status_level(str(r.get("status"))) == 0]
    story.append(Spacer(1, 4 * mm))
    story.append(_section_title("3. VMs com periodicidade confirmada"))
    if ok_regs:
        rows = [[
            r.get("empresa"), r.get("vm") or "-", r.get("status"),
            _dt_br(r.get("ultima_atualizacao_dropbox") or r.get("ultimo_backup")),
            r.get("periodicidade_detectada") or "-", r.get("confianca") or "-",
            r.get("quantidade_backups_atuais") if r.get("quantidade_backups_atuais") not in (None, "") else r.get("quantidade_backups", 0),
            r.get("quantidade_backups_historico") or 0,
        ] for r in sorted(ok_regs, key=lambda x: (str(x.get("empresa", "")).lower(), str(x.get("vm", ""))))]
        story.append(_table(
            ["Empresa", "VM", "Status", "Ultima atividade", "Frequencia", "Confianca", "Atuais", "Historico"],
            rows, [45, 20, 28, 38, 26, 25, 20, 22], status_col=2, small=False,
        ))
    else:
        story.append(_body("Nenhuma VM possui periodicidade confirmada nesta execucao."))

    learning = [r for r in vm_regs if _status_level(str(r.get("status"))) == 1]
    story.append(Spacer(1, 4 * mm))
    story.append(_section_title("4. VMs coletando histórico - resumo por empresa"))
    if learning:
        learning_group: dict[str, dict[str, Any]] = {}
        for r in learning:
            empresa = str(r.get("empresa") or "")
            item = learning_group.setdefault(empresa, {"qtd": 0, "mais_recente": "", "historico": 0})
            item["qtd"] += 1
            item["historico"] += int(r.get("quantidade_backups_historico") or 0)
            valor = str(r.get("ultima_atualizacao_dropbox") or r.get("ultimo_backup") or "")
            if valor > item["mais_recente"]:
                item["mais_recente"] = valor
        rows = [[empresa, item["qtd"], _dt_br(item["mais_recente"]), item["historico"]] for empresa, item in sorted(learning_group.items())]
        story.append(_table(["Empresa", "VMs em coleta", "Atividade mais recente", "Eventos no histórico"], rows, [90, 40, 65, 45], small=False))
        story.append(_body("Os detalhes individuais das VMs que ainda estão coletando histórico permanecem no CSV e JSON."))
    else:
        story.append(_body("Nenhuma VM está aguardando histórico adicional."))

    if empresa_regs:
        story.append(Spacer(1, 4 * mm))
        story.append(_section_title("5. Situacoes informativas por empresa"))
        rows = [[r.get("empresa"), r.get("status"), _short(r.get("observacao"), 260)] for r in empresa_regs]
        story.append(_table(["Empresa", "Status", "Observacao"], rows, [65, 40, 150], status_col=1, small=False))

    if backups_fora_do_lugar is not None:
        story.append(Spacer(1, 4 * mm))
        story.append(_section_title("6. Backups encontrados fora da arvore VMS"))
        meta = meta_backups_fora or {}
        story.append(_body(
            f"Total encontrado: {meta.get('total_encontrado', len(backups_fora_do_lugar))}. "
            f"Paginas lidas: {meta.get('paginas', 0)}. "
            f"Resultado incompleto: {'sim' if meta.get('incompleto') else 'nao'}."
        ))
        rows = [[
            r.get("empresa"), r.get("vmid"), r.get("arquivo"),
            _dt_br(r.get("ultima_atividade_dropbox")), _short(r.get("caminho_dropbox"), 130),
        ] for r in backups_fora_do_lugar]
        if rows:
            story.append(_table(["Empresa", "VMID", "Arquivo", "Ultima atividade", "Caminho"], rows, [38, 18, 75, 35, 100]))
        else:
            story.append(_body("Nenhum backup fora da arvore VMS foi encontrado."))

    story.append(Spacer(1, 4 * mm))
    story.append(_body("Os caminhos completos e o historico tecnico permanecem disponiveis nos arquivos CSV e JSON da mesma execucao."))
    doc = _doc(caminho, f"Auditoria de VMs Dropbox v{VERSION}")
    doc.build(story, onFirstPage=_page_header_footer, onLaterPages=_page_header_footer)

def gerar_pdf_consolidado(
    linhas: list[dict[str, Any]],
    executado_em: Any,
    caminho: Path,
    codigo_arquivos: int,
    codigo_vms: int,
) -> None:
    story: list[Any] = []
    _header(story, "Relatorio Semanal Consolidado", "Arquivos e backups de VMs no Dropbox", executado_em)
    ok = sum(1 for r in linhas if r.get("status_geral") == "OK")
    info = sum(1 for r in linhas if r.get("status_geral") == "INFORMATIVO")
    warning = sum(1 for r in linhas if r.get("status_geral") == "ATENCAO")
    error = sum(1 for r in linhas if r.get("status_geral") == "ERRO")
    story.append(_cards([
        ("Empresas", len(linhas), "neutral"), ("OK", ok, "ok"),
        ("Informativo", info, "info"), ("Atencao", warning, "warning"),
        ("Erro", error, "error"), ("Codigo final", max(codigo_arquivos, codigo_vms), "neutral"),
    ]))
    story.append(Spacer(1, 4 * mm))
    priorities = [r for r in linhas if r.get("status_geral") in {"ERRO", "ATENCAO"}]
    story.append(_section_title("1. Prioridades da semana"))
    if priorities:
        rows = [[
            r.get("empresa"), r.get("status_geral"), r.get("status_arquivos"),
            r.get("vms_total"), r.get("vms_criticas"), r.get("vms_atencao"),
            _short(r.get("status_vms"), 100), _short(r.get("acao"), 150),
        ] for r in sorted(priorities, key=lambda x: ({"ERRO": 0, "ATENCAO": 1}.get(str(x.get("status_geral")), 2), str(x.get("empresa", "")).lower()))]
        story.append(_table(
            ["Empresa", "Geral", "Arquivos", "VMs", "VMs erro", "VMs atencao", "Status VMs", "Acao"],
            rows, [38, 22, 31, 14, 18, 21, 60, 65], status_col=1,
        ))
    else:
        story.append(_body("Nenhuma prioridade foi identificada nesta execucao."))

    story.append(Spacer(1, 4 * mm))
    story.append(_section_title("2. Visao completa por empresa"))
    rows = [[
        r.get("empresa"), r.get("status_geral"), r.get("status_arquivos"),
        r.get("atividade_arquivos"), _dt_br(r.get("ultima_atividade_arquivos")),
        r.get("vms_total"), r.get("vms_criticas"), r.get("vms_atencao"),
        _short(r.get("acao"), 120),
    ] for r in sorted(linhas, key=lambda x: ({"ERRO": 0, "ATENCAO": 1, "INFORMATIVO": 2, "OK": 3}.get(str(x.get("status_geral")), 9), str(x.get("empresa", "")).lower()))]
    story.append(_table(
        ["Empresa", "Geral", "Arquivos", "Atividade", "Ultima atividade", "VMs", "VMs erro", "VMs atencao", "Acao"],
        rows, [38, 22, 31, 18, 31, 14, 18, 21, 70], status_col=1,
    ))
    story.append(Spacer(1, 4 * mm))
    story.append(_body(
        "Use este PDF como resumo principal. Para investigacao, consulte os PDFs de Arquivos e de VMs, "
        "os CSVs para filtros e os logs tecnicos da mesma execucao."
    ))
    doc = _doc(caminho, f"Relatorio semanal consolidado v{VERSION}")
    doc.build(story, onFirstPage=_page_header_footer, onLaterPages=_page_header_footer)
