"""Camada de interpretação gerencial dos estados técnicos do Auditor Dropbox.

Os módulos de coleta preservam seus códigos técnicos para diagnóstico e
compatibilidade. Este módulo decide apenas como cada evidência deve impactar o
relatório consolidado. O objetivo é não confundir limitação da auditoria,
aprendizado de periodicidade ou irregularidade ainda dentro da janela esperada
com falha comprovada de backup.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from dateutil.relativedelta import relativedelta


@dataclass(frozen=True)
class AvaliacaoGerencial:
    nivel: int
    status: str
    acao: str
    motivo: str


_NIVEL_STATUS = {0: "OK", 1: "INFORMATIVO", 2: "ATENCAO", 3: "ERRO"}


def _resultado(nivel: int, acao: str, motivo: str) -> AvaliacaoGerencial:
    nivel = max(0, min(int(nivel), 3))
    return AvaliacaoGerencial(nivel, _NIVEL_STATUS[nivel], acao, motivo)


def _casefold_mapping(mapping: Any, chave: str) -> dict[str, Any]:
    if not isinstance(mapping, dict):
        return {}
    alvo = str(chave or "").casefold()
    for nome, valor in mapping.items():
        if str(nome).casefold() == alvo and isinstance(valor, dict):
            return valor
    return {}


def politica_empresa(config: dict[str, Any], empresa: str) -> dict[str, Any]:
    return _casefold_mapping(config.get("empresas"), empresa)


def politica_vm(config: dict[str, Any], empresa: str, vmid: str) -> dict[str, Any]:
    empresa_cfg = _casefold_mapping(config.get("politicas_vms"), empresa)
    if not empresa_cfg:
        return {}
    alvo = str(vmid or "").lower().removeprefix("vm-")
    for chave, valor in empresa_cfg.items():
        atual = str(chave).lower().removeprefix("vm-")
        if atual == alvo and isinstance(valor, dict):
            return valor
    return {}


def empresa_area_fora_escopo(config: dict[str, Any], empresa: str, area: str) -> bool:
    politica = politica_empresa(config, empresa)
    if not politica:
        return False
    chave = "auditar_arquivos" if str(area).lower() == "arquivos" else "auditar_vms"
    estado = str(politica.get("estado") or "").strip().casefold()
    return politica.get(chave) is False or estado in {
        "migrado_drive", "migrado_para_drive", "drive", "fora_escopo", "fora_do_escopo"
    }


def vm_isenta(config: dict[str, Any], empresa: str, vmid: str) -> bool:
    politica = politica_vm(config, empresa, vmid)
    estado = str(politica.get("estado") or "").strip().casefold()
    return bool(
        politica.get("isento") is True
        or politica.get("obrigatorio") is False
        or politica.get("auditar") is False
        or estado in {"isento", "nao_aplicavel", "não_aplicável", "excluido", "excluído"}
    )


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _agora_compativel(registro: dict[str, Any], now: datetime | None) -> datetime:
    if now is not None:
        return now
    for campo in ("proximo_backup_previsto", "ultima_atualizacao_dropbox", "ultimo_backup"):
        dt = _parse_dt(registro.get(campo))
        if dt is not None and dt.tzinfo is not None:
            return datetime.now(dt.tzinfo)
    return datetime.now().astimezone()


def _atividade_confirmada(registro: dict[str, Any]) -> bool:
    try:
        if int(registro.get("arquivos_criados_ou_alterados") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    heartbeat = registro.get("heartbeat")
    return bool(isinstance(heartbeat, dict) and heartbeat.get("ok"))


def _irregular_esta_atrasado(registro: dict[str, Any], config: dict[str, Any], now: datetime | None) -> bool:
    agora = _agora_compativel(registro, now)
    proximo = _parse_dt(registro.get("proximo_backup_previsto"))
    if proximo is not None:
        if proximo.tzinfo is None and agora.tzinfo is not None:
            proximo = proximo.replace(tzinfo=agora.tzinfo)
        try:
            tolerancia = float(registro.get("tolerancia_horas") or 0)
        except (TypeError, ValueError):
            tolerancia = 0
        return agora > proximo + timedelta(hours=max(tolerancia, 0))

    ultimo = _parse_dt(registro.get("ultima_atualizacao_dropbox") or registro.get("ultimo_backup"))
    if ultimo is None:
        return False
    if ultimo.tzinfo is None and agora.tzinfo is not None:
        ultimo = ultimo.replace(tzinfo=agora.tzinfo)
    limite_dias = int((config.get("auditoria_vms") or {}).get("idade_maxima_sem_historico_dias", 45) or 45)
    return agora - ultimo > timedelta(days=max(limite_dias, 1))


def _avaliar_frequencia_explicita_vm(
    registro: dict[str, Any],
    config: dict[str, Any],
    empresa: str,
    vmid: str,
    now: datetime | None,
) -> AvaliacaoGerencial | None:
    """Aplica a política declarada mesmo se o registro técnico vier de cache/versão anterior.

    Isso torna o consolidado resiliente a um JSON técnico gerado antes da atualização da
    política e evita depender de inferência quando a frequência real é conhecida.
    """
    politica = politica_vm(config, empresa, vmid)
    frequencia = str(politica.get("frequencia") or politica.get("periodicidade") or "").strip().lower()
    if frequencia not in {"diario", "semanal", "quinzenal", "mensal"}:
        return None

    ultimo = _parse_dt(registro.get("ultima_atualizacao_dropbox") or registro.get("ultimo_backup"))
    if ultimo is None:
        return None
    agora = _agora_compativel(registro, now)
    if ultimo.tzinfo is None and agora.tzinfo is not None:
        ultimo = ultimo.replace(tzinfo=agora.tzinfo)

    if frequencia == "mensal":
        previsto = ultimo + relativedelta(months=1)
    else:
        dias = {"diario": 1, "semanal": 7, "quinzenal": 14}[frequencia]
        previsto = ultimo + timedelta(days=dias)

    tolerancias = config.get("tolerancias") if isinstance(config.get("tolerancias"), dict) else {}
    try:
        tolerancia = float(
            politica.get("tolerancia_horas")
            if politica.get("tolerancia_horas") is not None
            else tolerancias.get(f"{frequencia}_horas", 0)
        )
    except (TypeError, ValueError):
        tolerancia = 0.0
    limite = previsto + timedelta(hours=max(tolerancia, 0.0))

    if agora > limite:
        atraso = max((agora - limite).total_seconds() / 3600, 0)
        return _resultado(
            2,
            "Verificar a última execução e confirmar o envio ao Dropbox.",
            f"Política explícita {frequencia}: última atividade excedeu a janela em {atraso:.1f} hora(s).",
        )
    return _resultado(
        0,
        "Nenhuma ação necessária.",
        f"Política explícita {frequencia}: última atividade ainda está dentro da janela configurada.",
    )


def _vms_explicitamente_esperadas(config: dict[str, Any], empresa: str) -> bool:
    empresa_cfg = politica_empresa(config, empresa)
    if "auditar_vms" in empresa_cfg:
        return bool(empresa_cfg.get("auditar_vms"))
    vms_cfg = _casefold_mapping(config.get("politicas_vms"), empresa)
    if not vms_cfg:
        return False
    for chave, valor in vms_cfg.items():
        vmid = str(chave).lower().removeprefix("vm-")
        if not vm_isenta(config, empresa, vmid) and isinstance(valor, dict):
            return True
    return False


def avaliar_registro(
    registro: dict[str, Any],
    area: str,
    config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> AvaliacaoGerencial:
    """Avalia impacto gerencial sem alterar ``registro['status']``.

    ``area`` deve ser ``arquivos`` ou ``vms``. O código técnico permanece no
    JSON/CSV/PDF para investigação; somente o consolidado usa este impacto.
    """
    status = str(registro.get("status") or "NAO_EXECUTADO").upper()
    empresa = str(registro.get("empresa") or "")
    vmid = str(registro.get("vmid") or registro.get("vm") or "").lower().removeprefix("vm-")
    area = str(area or "").lower()

    if empresa_area_fora_escopo(config, empresa, area):
        empresa_cfg = politica_empresa(config, empresa)
        estado = str(empresa_cfg.get("estado") or "").strip().casefold()
        if estado in {"migrado_drive", "migrado_para_drive", "drive"}:
            return _resultado(1, "Nenhuma ação no Dropbox; destino ativo configurado fora deste escopo.", "Empresa migrada para outro destino.")
        return _resultado(1, "Nenhuma ação; empresa/área fora do escopo operacional configurado.", "A política desativa esta área para a empresa.")

    if area == "vms" and registro.get("tipo_registro") == "VM" and vmid and vm_isenta(config, empresa, vmid):
        motivo = str(politica_vm(config, empresa, vmid).get("motivo") or "VM/CT marcada como isenta na política.")
        return _resultado(1, "Nenhuma ação; item isento conforme política documentada.", motivo)

    # Política explícita vence inferência/aprendizado para estados que representam
    # periodicidade, mas não vence falhas estruturais como SEM_BACKUP/VM_NAO_ENCONTRADA.
    if area == "vms" and registro.get("tipo_registro") == "VM" and vmid and status in {
        "OK", "IRREGULAR", "ATRASADO", "SEM_UPLOAD_RECENTE", "EM_APRENDIZADO", "AMOSTRA_INSUFICIENTE"
    }:
        por_politica = _avaliar_frequencia_explicita_vm(registro, config, empresa, vmid, now)
        if por_politica is not None:
            return por_politica

    # Estados que já são, por definição, neutros ou apenas informativos.
    if status in {
        "OK", "ATIVIDADE_CONFIRMADA", "BACKUP_CONFIRMADO",
    }:
        return _resultado(0, "Nenhuma ação necessária.", "Evidência de backup/atividade compatível com o esperado.")

    if status in {
        "EM_APRENDIZADO", "AMOSTRA_INSUFICIENTE", "MIGRADO_PARA_DRIVE", "NAO_APLICAVEL",
        "NAO_AUDITADO", "NAO_EXECUTADO", "BASELINE_CRIADA", "BASELINE_RECRIADA_POLITICA",
        "BASELINE_RECRIADA_MANUAL", "INVENTARIO_BLOQUEADO",
    }:
        if status == "MIGRADO_PARA_DRIVE":
            return _resultado(1, "Nenhuma ação no Dropbox; destino ativo configurado fora deste escopo.", "Empresa migrada para outro destino.")
        if status in {"BASELINE_CRIADA", "BASELINE_RECRIADA_POLITICA", "BASELINE_RECRIADA_MANUAL"}:
            return _resultado(1, "Aguardar a próxima execução para consolidar a comparação incremental.", "Referência incremental criada/recriada.")
        return _resultado(1, "Nenhuma ação imediata; acompanhar a coleta técnica.", "Estado informativo sem falha comprovada.")

    # Ausência de mudanças não prova falha. Só vira atenção quando a empresa
    # explicitamente exige atividade de Arquivos em toda janela de auditoria.
    if status in {"SEM_MUDANCAS", "SOMENTE_EXCLUSOES"}:
        empresa_cfg = politica_empresa(config, empresa)
        exigir = bool(
            empresa_cfg.get("exigir_atividade_arquivos") is True
            or empresa_cfg.get("arquivos_exigem_atividade") is True
        )
        if exigir:
            return _resultado(2, "Confirmar se a rotina de Arquivos executou no período esperado.", "A política da empresa exige atividade na janela auditada.")
        return _resultado(1, "Sem ação imediata; ausência de mudanças, isoladamente, não comprova falha de backup.", "Não houve criação/alteração de arquivos na janela.")

    # Consulta parcial: preserva a evidência coletada e não vira erro de backup.
    if status == "INCOMPLETO":
        if _atividade_confirmada(registro):
            return _resultado(1, "A auditoria pode continuar na próxima execução; a atividade já foi confirmada.", "Consulta parcial com evidência positiva de atividade.")
        return _resultado(2, "Repetir/continuar a auditoria para concluir o escopo antes de afirmar falha de backup.", "Consulta parcial sem evidência suficiente para concluir.")

    if status in {"EVENTOS_VMS_INCOMPLETOS", "CURSOR_RECRIADO"}:
        return _resultado(1, "Acompanhar a próxima coleta; o histórico técnico está incompleto, sem falha de backup comprovada.", "Limitação do histórico/cursor, não do backup em si.")

    if status in {"CURSOR_INVALIDO", "BASELINE_NAO_CRIADA", "HEARTBEAT_ERRO", "ERRO_EVENTOS_VMS", "ERRO"}:
        return _resultado(2, "Revisar a auditoria e repetir a coleta; o backup não deve ser declarado como falho apenas por este erro técnico.", "Falha/limitação da auditoria impede conclusão confiável.")

    if status == "IRREGULAR":
        # IRREGULAR significa que o histórico não permitiu inferir uma periodicidade
        # confiável. Uma previsão produzida a partir desse estado não deve, sozinha,
        # escalar o consolidado. Quando a periodicidade real for conhecida, declare-a
        # em politicas_vms; o histórico então recalcula o registro como OK/ATRASADO.
        return _resultado(
            1,
            "Acompanhar a periodicidade; ainda não há atraso confiável calculado.",
            "Histórico irregular sem periodicidade confiável para cobrar atraso automaticamente.",
        )

    if status in {"ATRASADO", "SEM_UPLOAD_RECENTE", "HEARTBEAT_ATRASADO", "HEARTBEAT_AUSENTE", "VAZIA", "AMOSTRAGEM_INCONCLUSIVA", "ATIVIDADE_ESPERADA_AUSENTE"}:
        return _resultado(2, "Verificar a última execução e confirmar se a rotina continua ativa.", "Há indício objetivo que requer conferência técnica.")

    if status == "SEM_PASTA_VMS":
        if _vms_explicitamente_esperadas(config, empresa):
            return _resultado(3, "Verificar a estrutura de VMs e restaurar a cobertura esperada no Dropbox.", "A empresa está explicitamente configurada para possuir backups de VMs.")
        return _resultado(2, "Confirmar se a empresa realmente possui VMs no escopo antes de tratar a ausência da pasta como falha.", "Pasta de VMs ausente sem expectativa explícita configurada.")

    if status == "VM_NAO_ENCONTRADA":
        if politica_vm(config, empresa, vmid):
            return _resultado(3, "Verificar remoção, renomeação ou falha de envio da VM/CT prevista na política.", "VM/CT explicitamente esperada não foi encontrada.")
        return _resultado(2, "Confirmar se a VM/CT foi desativada ou renomeada antes de classificar como falha.", "VM histórica ausente sem política obrigatória explícita.")

    if status in {"SEM_BACKUP", "NAO_EXISTE"}:
        return _resultado(3, "Verificar imediatamente a rotina e a existência do backup esperado.", "Backup/pasta obrigatória não localizado no escopo auditado.")

    # Código desconhecido: nunca converte silenciosamente em OK.
    return _resultado(2, "Validar o código técnico e a compatibilidade da versão antes de concluir.", f"Status técnico não tratado pela política gerencial: {status}.")


def consolidar_avaliacoes(avaliacoes: list[AvaliacaoGerencial]) -> AvaliacaoGerencial:
    if not avaliacoes:
        return _resultado(0, "Nenhuma ação necessária.", "Sem evidências que exijam ação.")
    maior = max(item.nivel for item in avaliacoes)
    candidatas = [item for item in avaliacoes if item.nivel == maior]
    principal = candidatas[0]
    return _resultado(maior, principal.acao, principal.motivo)
