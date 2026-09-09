from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from dropbox.exceptions import ApiError
from dropbox.files import DeletedMetadata, FileMetadata

from . import auditor
from .version import VERSION

CACHE_VERSION = 3


def _now(tzinfo: ZoneInfo) -> datetime:
    return datetime.now(tzinfo)


def _iso(dt: datetime | None) -> str:
    return auditor.formatar_dt(dt)


def _parse_dt(value: Any, tzinfo: ZoneInfo) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tzinfo)
    return dt.astimezone(tzinfo)


def _novo_cache() -> dict[str, Any]:
    return {"versao": CACHE_VERSION, "vms": {}, "cursores": {}}


def _normalizar_cache_chunks(value: dict[str, Any]) -> None:
    """Consolida eventos antigos de .rclone_chunk como um unico backup logico."""
    for vm in (value.get("vms", {}) or {}).values():
        if not isinstance(vm, dict):
            continue
        agrupados: dict[str, dict[str, Any]] = {}
        partes: dict[str, set[str]] = {}
        for item in list(vm.get("uploads", []) or []):
            if not isinstance(item, dict):
                continue
            original = str(item.get("arquivo") or "")
            logico = auditor.nome_backup_logico(original)
            if not logico:
                continue
            candidato = dict(item)
            candidato["arquivo"] = logico
            caminho = str(candidato.get("caminho") or "")
            if caminho and auditor.eh_chunk_rclone(original):
                candidato["caminho"] = caminho.rsplit("/", 1)[0] + "/" + logico
            partes.setdefault(logico, set()).add(original)
            atual = agrupados.get(logico)
            dt_candidato = _parse_dt(
                candidato.get("referencia_status") or candidato.get("server_modified") or candidato.get("backup_datetime"),
                ZoneInfo("America/Sao_Paulo"),
            )
            dt_atual = _parse_dt(
                (atual or {}).get("referencia_status") or (atual or {}).get("server_modified") or (atual or {}).get("backup_datetime"),
                ZoneInfo("America/Sao_Paulo"),
            ) if atual else None
            if atual is None or (dt_candidato and (dt_atual is None or dt_candidato >= dt_atual)):
                agrupados[logico] = candidato
        for logico, item in agrupados.items():
            item["partes_detectadas"] = sorted(partes.get(logico, {logico}))
            item["quantidade_partes"] = len(item["partes_detectadas"])
        vm["uploads"] = list(agrupados.values())
        vm["arquivos_atuais"] = sorted({auditor.nome_backup_logico(str(nome)) for nome in vm.get("arquivos_atuais", []) or [] if nome})


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _novo_cache()
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        corrompido = path.with_name(f"{path.stem}.corrompido_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
        try:
            shutil.move(str(path), str(corrompido))
        except OSError:
            pass
        return _novo_cache()
    if not isinstance(value, dict):
        value = {}
    value.setdefault("vms", {})
    value.setdefault("cursores", {})
    _normalizar_cache_chunks(value)
    value["versao"] = CACHE_VERSION
    return value


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _vm_key(empresa: str, vmid: str, caminho: str = "") -> str:
    identificador = vmid or auditor.nome_path(caminho)
    return f"{empresa.lower()}|{identificador.lower()}"


def _cursor_key(empresa: str, raiz_vms: str) -> str:
    return f"{empresa.lower()}|{auditor.normalizar_path(raiz_vms).lower()}"


def _upsert_upload(
    cache: dict[str, Any],
    empresa: str,
    vmid: str,
    arquivo: str,
    backup_datetime: str,
    server_modified: str,
    referencia_status: str,
    caminho: str,
    detectado_em: str,
    extras: dict[str, Any] | None = None,
) -> None:
    arquivo_original = arquivo
    arquivo = auditor.nome_backup_logico(arquivo)
    if caminho and auditor.eh_chunk_rclone(arquivo_original):
        caminho = caminho.rsplit("/", 1)[0] + "/" + arquivo
    key = _vm_key(empresa, vmid, caminho)
    vm = cache.setdefault("vms", {}).setdefault(
        key,
        {"empresa": empresa, "vmid": vmid, "uploads": [], "exclusoes": [], "arquivos_atuais": []},
    )
    vm["empresa"] = empresa
    if vmid:
        vm["vmid"] = vmid
    if caminho:
        vm["ultimo_caminho"] = caminho
    uploads = vm.setdefault("uploads", [])
    existente = next((item for item in uploads if str(item.get("arquivo")) == arquivo), None)
    dados = {
        "arquivo": arquivo,
        "backup_datetime": backup_datetime,
        "server_modified": server_modified,
        "referencia_status": referencia_status or server_modified or backup_datetime,
        "caminho": caminho,
        "ultimo_avistamento": detectado_em,
    }
    if extras:
        dados.update({k: v for k, v in extras.items() if v not in (None, "")})
    partes_anteriores = set((existente or {}).get("partes_detectadas", []) or [])
    parte_original = str((extras or {}).get("parte_origem") or arquivo_original)
    partes_anteriores.add(parte_original)
    dados["partes_detectadas"] = sorted(partes_anteriores)
    dados["quantidade_partes"] = len(partes_anteriores)
    if existente is None:
        dados["primeiro_avistamento"] = detectado_em
        uploads.append(dados)
    else:
        primeiro = existente.get("primeiro_avistamento") or detectado_em
        existente.update(dados)
        existente["primeiro_avistamento"] = primeiro


def _registrar_exclusao(
    cache: dict[str, Any],
    empresa: str,
    vmid: str,
    arquivo: str,
    caminho: str,
    detectado_em: str,
    origem: str,
) -> bool:
    arquivo = auditor.nome_backup_logico(arquivo)
    key = _vm_key(empresa, vmid, caminho)
    vm = cache.setdefault("vms", {}).setdefault(
        key,
        {"empresa": empresa, "vmid": vmid, "uploads": [], "exclusoes": [], "arquivos_atuais": []},
    )
    exclusoes = vm.setdefault("exclusoes", [])
    # O mesmo arquivo pode ser observado pela comparacao do inventario e pelo cursor.
    # O nome logico dentro da mesma VM e a identidade suficiente para deduplicar.
    existente = next(
        (item for item in reversed(exclusoes[-200:]) if auditor.nome_backup_logico(str(item.get("arquivo"))) == arquivo),
        None,
    )
    if existente is not None:
        origens = set(existente.get("origens", []) or [])
        if existente.get("origem"):
            origens.add(str(existente.get("origem")))
        origens.add(origem)
        existente["origens"] = sorted(origens)
        existente["ultimo_avistamento_exclusao"] = detectado_em
        if caminho and not existente.get("caminho"):
            existente["caminho"] = caminho
        return False
    exclusoes.append({
        "arquivo": arquivo,
        "caminho": caminho,
        "detectado_em": detectado_em,
        "origem": origem,
        "origens": [origem],
    })
    vm["exclusoes"] = exclusoes[-500:]
    return True

def _processar_eventos_cursor(
    dbx: Any,
    empresa: str,
    raiz_vms: str,
    cache: dict[str, Any],
    tzinfo: ZoneInfo,
    logger: logging.Logger,
    max_paginas: int,
    tempo_maximo_segundos: int = 0,
) -> dict[str, int]:
    resultado_contagem = {
        "uploads": 0,
        "exclusoes": 0,
        "paginas": 0,
        "baseline": 0,
        "cursor_recriado": 0,
        "erro": 0,
        "incompleto": 0,
    }
    key = _cursor_key(empresa, raiz_vms)
    cursores = cache.setdefault("cursores", {})
    estado = cursores.setdefault(key, {"empresa": empresa, "path": auditor.normalizar_path(raiz_vms)})
    cursor = str(estado.get("cursor") or "")
    detectado_em = _iso(_now(tzinfo))
    if not cursor:
        try:
            base = auditor.chamada_com_retry(
                dbx.files_list_folder_get_latest_cursor,
                raiz_vms,
                recursive=True,
                include_deleted=True,
                logger=logger,
            )
            estado.update(cursor=str(base.cursor), baseline_criada_em=detectado_em, inclui_exclusoes=True)
            estado.pop("ultimo_erro", None)
            resultado_contagem["baseline"] = 1
        except Exception as exc:  # noqa: BLE001
            estado["ultimo_erro"] = str(exc)
            resultado_contagem["erro"] = 1
        return resultado_contagem

    paginas = 0
    cursor_atual = cursor
    inicio = time.monotonic()
    try:
        while True:
            resultado = auditor.chamada_com_retry(dbx.files_list_folder_continue, cursor_atual, logger=logger)
            paginas += 1
            for entrada in resultado.entries:
                caminho = auditor.normalizar_path(
                    getattr(entrada, "path_display", "") or getattr(entrada, "path_lower", "") or ""
                )
                nome_original = str(getattr(entrada, "name", "") or auditor.nome_path(caminho))
                nome = auditor.nome_backup_logico(nome_original)
                parsed = auditor.parse_vzdump_datetime(nome, tzinfo)
                if parsed is None:
                    continue
                if isinstance(entrada, FileMetadata):
                    server_modified = auditor.converter_dropbox_datetime(entrada.server_modified, tzinfo)
                    _upsert_upload(
                        cache,
                        empresa,
                        parsed.vmid,
                        nome,
                        _iso(parsed.data_hora),
                        _iso(server_modified),
                        _iso(server_modified or parsed.data_hora),
                        caminho,
                        detectado_em,
                        extras={"parte_origem": nome_original},
                    )
                    resultado_contagem["uploads"] += 1
                elif isinstance(entrada, DeletedMetadata):
                    # Excluir uma parte nao prova que o backup logico inteiro foi removido.
                    # A confirmacao segura ocorre pela comparacao do inventario completo da pasta.
                    if auditor.eh_chunk_rclone(nome_original):
                        continue
                    if _registrar_exclusao(
                        cache, empresa, parsed.vmid, nome, caminho, detectado_em, "cursor_dropbox"
                    ):
                        resultado_contagem["exclusoes"] += 1
            cursor_atual = str(resultado.cursor)
            estado.update(
                cursor=cursor_atual,
                ultima_execucao_em=detectado_em,
                paginas_ultima_execucao=paginas,
                incompleto=False,
            )
            if not resultado.has_more:
                break
            if max_paginas > 0 and paginas >= max_paginas:
                resultado_contagem["incompleto"] = 1
                estado["incompleto"] = True
                break
            if tempo_maximo_segundos > 0 and time.monotonic() - inicio >= tempo_maximo_segundos:
                resultado_contagem["incompleto"] = 1
                estado["incompleto"] = True
                break
        estado.pop("ultimo_erro", None)
    except ApiError as exc:
        texto = str(exc).lower()
        if "reset" in texto or "cursor" in texto:
            try:
                base = auditor.chamada_com_retry(
                    dbx.files_list_folder_get_latest_cursor,
                    raiz_vms,
                    recursive=True,
                    include_deleted=True,
                    logger=logger,
                )
                estado.update(
                    cursor=str(base.cursor),
                    baseline_recriada_em=detectado_em,
                    aviso="Cursor invalidado pelo Dropbox; histórico anterior preservado.",
                    incompleto=False,
                )
                estado.pop("ultimo_erro", None)
                resultado_contagem["cursor_recriado"] = 1
            except Exception as reset_exc:  # noqa: BLE001
                estado["ultimo_erro"] = str(reset_exc)
                resultado_contagem["erro"] = 1
        else:
            estado["ultimo_erro"] = str(exc)
            resultado_contagem["erro"] = 1
    except Exception as exc:  # noqa: BLE001
        estado["ultimo_erro"] = str(exc)
        resultado_contagem["erro"] = 1
    resultado_contagem["paginas"] = paginas
    return resultado_contagem


def _politicas(config: dict[str, Any]) -> dict[str, Any]:
    valor = config.get("politicas_vms") or config.get("auditoria_vms", {}).get("politicas") or {}
    return valor if isinstance(valor, dict) else {}


def _politica(config: dict[str, Any], empresa: str, vmid: str) -> dict[str, Any] | None:
    empresa_cfg = next((v for k, v in _politicas(config).items() if str(k).lower() == empresa.lower()), None)
    if not isinstance(empresa_cfg, dict):
        return None
    valor = next(
        (v for k, v in empresa_cfg.items() if str(k).lower() in {vmid.lower(), f"vm-{vmid}".lower()}),
        None,
    )
    return valor if isinstance(valor, dict) else None


def _politica_isenta(politica: dict[str, Any] | None) -> bool:
    if not isinstance(politica, dict):
        return False
    estado = str(politica.get("estado") or "").strip().lower()
    return bool(
        politica.get("isento") is True
        or politica.get("obrigatorio") is False
        or politica.get("auditar") is False
        or estado in {"isento", "nao_aplicavel", "não_aplicável", "excluido", "excluído"}
    )


def _recalcular_registro(
    registro: dict[str, Any],
    vm_cache: dict[str, Any],
    config: dict[str, Any],
    tzinfo: ZoneInfo,
    novas_exclusoes: int,
) -> None:
    uploads = list(vm_cache.get("uploads", []))
    eventos: list[tuple[datetime, dict[str, Any]]] = []
    for item in uploads:
        dt = _parse_dt(
            item.get("referencia_status") or item.get("server_modified") or item.get("backup_datetime"),
            tzinfo,
        )
        if dt is not None:
            eventos.append((dt, item))
    eventos.sort(key=lambda x: x[0])
    quantidade_atual_prevista = int(registro.get("quantidade_backups_atuais", registro.get("quantidade_backups") or 0) or 0)
    limite_historico = max(
        int(config.get("auditoria_vms", {}).get("max_eventos_historico_por_vm", 365) or 365),
        quantidade_atual_prevista,
    )
    eventos = eventos[-limite_historico:]
    vm_cache["uploads"] = [item for _, item in eventos]

    quantidade_atual = int(registro.get("quantidade_backups_atuais", registro.get("quantidade_backups") or 0) or 0)
    registro["quantidade_backups_atuais"] = quantidade_atual
    registro["quantidade_backups_historico"] = len(eventos)
    registro["exclusoes_detectadas_ultima_execucao"] = novas_exclusoes
    empresa = str(registro.get("empresa", ""))
    vmid = str(registro.get("vmid", ""))
    politica = _politica(config, empresa, vmid)
    isenta = _politica_isenta(politica)
    if not eventos:
        if isenta:
            motivo = str((politica or {}).get("motivo") or "VM/CT isenta conforme política configurada.")
            registro.update(
                status="NAO_APLICAVEL",
                periodicidade_detectada="nao_aplicavel",
                confianca="POLITICA",
                origem_periodicidade="configuracao_manual",
                observacao=f"{str(registro.get('observacao', '')).strip()} {motivo}".strip(),
            )
        return

    datas = [dt for dt, _ in eventos]
    ultimo_dt, ultimo_evento = eventos[-1]
    penultimo_dt = eventos[-2][0] if len(eventos) >= 2 else None
    origem = "historico_detectado"
    mediana = None

    if isenta:
        periodicidade = "nao_aplicavel"
        confianca = "POLITICA"
        origem = "configuracao_manual"
        mediana = None
        observacao_periodo = str((politica or {}).get("motivo") or "VM/CT isenta conforme política configurada.")
    elif politica:
        periodicidade = str(politica.get("frequencia") or politica.get("periodicidade") or "").lower().strip()
        if periodicidade not in {"diario", "semanal", "quinzenal", "mensal"}:
            periodicidade = "irregular"
        confianca = "CONFIGURADA"
        origem = "configuracao_manual"
        mediana = {
            "diario": timedelta(days=1),
            "semanal": timedelta(days=7),
            "quinzenal": timedelta(days=14),
            "mensal": relativedelta(months=1),
        }.get(periodicidade)
        observacao_periodo = "Periodicidade definida manualmente no config.yaml."
    else:
        minimo = int(config.get("auditoria_vms", {}).get("minimo_eventos_aprender", 4))
        if len(datas) < minimo:
            origem = "coleta_historico"
            periodicidade = "amostra_insuficiente"
            confianca = "EM_APRENDIZADO"
            observacao_periodo = (
                f"Histórico acumulado com {len(datas)} evento(s); são necessários {minimo} para confirmar automaticamente o padrão."
            )
        else:
            detectado = auditor.detectar_periodicidade(datas)
            periodicidade = detectado.periodicidade
            confianca = detectado.confianca
            mediana = detectado.mediana
            observacao_periodo = detectado.observacao

    if periodicidade == "mensal" and isinstance(mediana, relativedelta):
        proximo = ultimo_dt + mediana
    else:
        proximo = auditor.prever_proximo_backup(
            ultimo_dt, periodicidade, mediana if isinstance(mediana, timedelta) else None
        )
    tolerancia_horas = int((politica or {}).get("tolerancia_horas") or auditor.tolerancia_para(periodicidade, config))

    status_original = str(registro.get("status", ""))
    if isenta:
        status = "NAO_APLICAVEL"
    elif status_original == "VM_NAO_ENCONTRADA":
        status = status_original
    elif quantidade_atual <= 0:
        status = "SEM_BACKUP"
    elif periodicidade == "amostra_insuficiente":
        idade_maxima_dias = int(config.get("auditoria_vms", {}).get("idade_maxima_sem_historico_dias", 45))
        idade_dias = max((_now(tzinfo) - ultimo_dt).total_seconds() / 86400, 0)
        status = "SEM_UPLOAD_RECENTE" if idade_dias > idade_maxima_dias else "EM_APRENDIZADO"
        if status == "SEM_UPLOAD_RECENTE":
            observacao_periodo += f" Ultimo upload observado ha {idade_dias:.1f} dias; limite provisório: {idade_maxima_dias} dias."
    elif periodicidade == "irregular" or confianca == "IRREGULAR":
        status = "IRREGULAR"
    else:
        status = auditor.calcular_status(_now(tzinfo), proximo, timedelta(hours=tolerancia_horas))

    atraso: float | str = ""
    if proximo and status == "ATRASADO":
        atraso = round((_now(tzinfo) - (proximo + timedelta(hours=tolerancia_horas))).total_seconds() / 3600, 2)
    elif proximo:
        atraso = 0

    observacao_original = str(registro.get("observacao", "")).strip()
    descricao_origem = {
        "configuracao_manual": "Periodicidade definida manualmente.",
        "coleta_historico": "Periodicidade ainda não confirmada; o auditor está coletando histórico suficiente.",
        "historico_detectado": "Periodicidade detectada automaticamente a partir do histórico de uploads.",
    }.get(origem, f"Origem da periodicidade: {origem}.")
    complemento = (
        f"Historico persistente v{VERSION}: {len(eventos)} upload(s), "
        f"{len(vm_cache.get('exclusoes', []))} exclusao(oes) detectada(s). "
        f"{descricao_origem} {observacao_periodo}"
    )
    registro.update(
        status=status,
        periodicidade_detectada=periodicidade,
        confianca=confianca,
        quantidade_backups=len(eventos),
        ultimo_backup=_iso(ultimo_dt),
        ultima_atualizacao_dropbox=_iso(ultimo_dt),
        penultimo_backup=_iso(penultimo_dt),
        proximo_backup_previsto=_iso(proximo),
        tolerancia_horas=tolerancia_horas,
        atraso_horas=atraso,
        ultimo_arquivo=str(ultimo_evento.get("arquivo", registro.get("ultimo_arquivo", ""))),
        arquivo_mais_recente=str(ultimo_evento.get("arquivo", registro.get("arquivo_mais_recente", ""))),
        caminho_ultimo_arquivo=str(ultimo_evento.get("caminho", registro.get("caminho_ultimo_arquivo", ""))),
        origem_periodicidade=origem,
        observacao=f"{observacao_original} {complemento}".strip(),
    )
    max_relatorio = int(config.get("analise", {}).get("max_backups_por_vm_para_relatorio", 10))
    registro["ultimos_backups"] = [
        {
            "arquivo": item.get("arquivo", ""),
            "backup_datetime": item.get("backup_datetime", ""),
            "server_modified": item.get("server_modified", ""),
            "referencia_status": item.get("referencia_status", ""),
            "tamanho_gb": item.get("tamanho_gb", ""),
            "extensao_backup": item.get("extensao_backup", ""),
            "compactacao": item.get("compactacao", ""),
            "arquivo_zst": item.get("arquivo_zst", ""),
        }
        for _, item in reversed(eventos[-max_relatorio:])
    ]


def _registro_vm_ausente(empresa: str, vmid: str, vm_cache: dict[str, Any], origem: str) -> dict[str, Any]:
    caminho = str(vm_cache.get("ultimo_caminho") or "")
    return {
        "tipo_registro": "VM",
        "empresa": empresa,
        "caminho_dropbox": caminho,
        "vm": f"vm-{vmid}" if vmid else "VM desconhecida",
        "vmid": vmid,
        "status": "VM_NAO_ENCONTRADA",
        "periodicidade_detectada": "",
        "confianca": "",
        "quantidade_backups": 0,
        "quantidade_backups_atuais": 0,
        "quantidade_backups_historico": len(vm_cache.get("uploads", []) or []),
        "exclusoes_detectadas_ultima_execucao": 0,
        "origem_periodicidade": "",
        "ultimo_backup": "",
        "data_backup_arquivo": "",
        "referencia_status": "atividade_dropbox",
        "penultimo_backup": "",
        "proximo_backup_previsto": "",
        "tolerancia_horas": "",
        "atraso_horas": "",
        "ultimo_arquivo": "",
        "arquivo_mais_recente": "",
        "caminho_ultimo_arquivo": caminho,
        "caminho_arquivo_mais_recente": caminho,
        "tamanho_ultimo_arquivo_gb": "",
        "server_modified_ultimo_arquivo": "",
        "ultima_atualizacao_dropbox": "",
        "quantidade_arquivos": "",
        "tamanho_total_gb": "",
        "caminho_vms_usado": caminho,
        "pasta_vms_origem": caminho,
        "pasta_vms_alternativa": "",
        "extensao_backup": "",
        "compactacao": "",
        "arquivo_zst": "",
        "observacao": (
            "A VM existia no histórico ou possui política configurada, mas não foi encontrada no inventário atual. "
            f"Origem da expectativa: {origem}. Verifique remoção, renomeação, caminho e permissões."
        ),
        "ultimos_backups": [],
    }


def _registros_eventos_empresa(avisos: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    saida: list[dict[str, Any]] = []
    for empresa_lower, info in avisos.items():
        empresa = str(info.get("empresa") or empresa_lower)
        if info.get("erro"):
            status = "ERRO_EVENTOS_VMS"
            obs = "Falha ao atualizar o cursor incremental de eventos de VMs. O inventário atual foi mantido, mas o histórico pode estar incompleto."
        elif info.get("incompleto"):
            status = "EVENTOS_VMS_INCOMPLETOS"
            obs = "A leitura incremental de eventos de VMs atingiu limite de páginas/tempo e continuará na próxima execução."
        elif info.get("cursor_recriado"):
            status = "EVENTOS_VMS_INCOMPLETOS"
            obs = "O cursor de eventos de VMs foi invalidado e recriado. O histórico anterior foi preservado; revise o intervalo perdido."
        else:
            continue
        saida.append(
            {
                "tipo_registro": "EMPRESA",
                "empresa": empresa,
                "caminho_dropbox": str(info.get("raiz") or ""),
                "vm": "",
                "vmid": "",
                "status": status,
                "periodicidade_detectada": "",
                "confianca": "",
                "quantidade_backups": "",
                "ultimo_backup": "",
                "proximo_backup_previsto": "",
                "atraso_horas": "",
                "observacao": obs,
                "ultimos_backups": [],
            }
        )
    return saida


def aplicar_historico_vms(
    registros: list[dict[str, Any]],
    dbx: Any,
    config: dict[str, Any],
    tzinfo: ZoneInfo,
    base_dir: Path,
    logger: logging.Logger,
    detectar_ausentes: bool = True,
) -> list[dict[str, Any]]:
    caminho_cache = base_dir / "cache" / "auditoria_vms_historico.json"
    cache = _load(caminho_cache)
    detectado_em = _iso(_now(tzinfo))
    novas_exclusoes_por_vm: dict[str, int] = {}
    empresas_processadas = {
        str(registro.get("empresa", "")).lower()
        for registro in registros
        if str(registro.get("empresa", "")).strip()
    }
    empresas_inventario_invalido = {
        str(registro.get("empresa", "")).lower()
        for registro in registros
        if registro.get("tipo_registro") == "EMPRESA" and registro.get("status") == "ERRO"
    }
    empresas_fora_escopo = {
        str(registro.get("empresa", "")).lower()
        for registro in registros
        if registro.get("tipo_registro") == "EMPRESA"
        and registro.get("status") in {"MIGRADO_PARA_DRIVE", "NAO_APLICAVEL", "NAO_AUDITADO"}
    }

    roots: dict[tuple[str, str], None] = {}
    chaves_atuais: set[str] = set()
    for registro in registros:
        if registro.get("tipo_registro") != "VM":
            continue
        empresa = str(registro.get("empresa", ""))
        vmid = str(registro.get("vmid", ""))
        caminho_vm = str(registro.get("caminho_dropbox", ""))
        raiz_vms = str(registro.get("caminho_vms_usado", "") or registro.get("pasta_vms_origem", ""))
        if raiz_vms:
            roots[(empresa, auditor.normalizar_path(raiz_vms))] = None
        key = _vm_key(empresa, vmid, caminho_vm)
        chaves_atuais.add(key)
        vm_cache = cache.setdefault("vms", {}).setdefault(
            key,
            {"empresa": empresa, "vmid": vmid, "uploads": [], "exclusoes": [], "arquivos_atuais": []},
        )
        vm_cache.update(
            empresa=empresa,
            vmid=vmid,
            ultimo_caminho=caminho_vm,
            ultima_presenca_em=detectado_em,
            ausente_desde="",
        )

        inventario = registro.pop("backups_atuais_inventario", None)
        inventario_completo = isinstance(inventario, list)
        itens_inventario = inventario if inventario_completo else (registro.get("ultimos_backups", []) or [])
        atuais: list[str] = []
        for item in itens_inventario:
            if not isinstance(item, dict):
                continue
            arquivo = str(item.get("arquivo", ""))
            if not arquivo:
                continue
            atuais.append(arquivo)
            _upsert_upload(
                cache,
                empresa,
                vmid,
                arquivo,
                str(item.get("backup_datetime", "")),
                str(item.get("server_modified", "")),
                str(item.get("referencia_status", "")),
                str(item.get("caminho", "") or (registro.get("caminho_ultimo_arquivo", "") if arquivo == registro.get("ultimo_arquivo") else caminho_vm)),
                detectado_em,
                extras={
                    "tamanho_gb": item.get("tamanho_gb", ""),
                    "extensao_backup": item.get("extensao_backup", ""),
                    "compactacao": item.get("compactacao", ""),
                    "arquivo_zst": item.get("arquivo_zst", ""),
                },
            )
        anteriores = set(str(v) for v in vm_cache.get("arquivos_atuais", []) or [])
        atuais_set = set(atuais)
        # A comparação só é segura quando o inventário completo da pasta foi fornecido.
        # Listas limitadas para relatório jamais são usadas como prova de exclusão.
        if inventario_completo:
            for removido in sorted(anteriores - atuais_set):
                if _registrar_exclusao(
                    cache, empresa, vmid, removido, caminho_vm, detectado_em, "comparacao_inventario_completo"
                ):
                    novas_exclusoes_por_vm[key] = novas_exclusoes_por_vm.get(key, 0) + 1
            vm_cache["arquivos_atuais"] = sorted(atuais_set)
            vm_cache["inventario_completo_em"] = detectado_em
        vm_cache["ultima_execucao_em"] = detectado_em
        registro["quantidade_backups_atuais"] = len(atuais_set) if inventario_completo else int(
            registro.get("quantidade_backups") or 0
        )

    exclusoes_antes_cursor = {
        key: len((vm or {}).get("exclusoes", []) or []) for key, vm in cache.get("vms", {}).items()
    }
    cfg_vms = config.get("auditoria_vms", {}) if isinstance(config.get("auditoria_vms"), dict) else {}
    max_paginas = int(cfg_vms.get("max_paginas_eventos_por_raiz", 100))
    tempo_eventos = int(cfg_vms.get("tempo_maximo_eventos_por_raiz_minutos", 10)) * 60
    avisos_por_empresa: dict[str, dict[str, Any]] = {}
    for empresa, raiz_vms in roots:
        contagem = _processar_eventos_cursor(
            dbx, empresa, raiz_vms, cache, tzinfo, logger, max_paginas, tempo_eventos
        )
        aviso = avisos_por_empresa.setdefault(
            empresa.lower(), {"empresa": empresa, "raiz": raiz_vms, "mensagens": []}
        )
        if contagem.get("baseline"):
            aviso["mensagens"].append(
                "Linha de base de eventos VMS criada; exclusões passam a ser rastreadas a partir desta execução."
            )
        if contagem.get("cursor_recriado"):
            aviso["cursor_recriado"] = True
            aviso["mensagens"].append("Cursor de eventos VMS recriado; revise o intervalo perdido.")
        if contagem.get("incompleto"):
            aviso["incompleto"] = True
            aviso["mensagens"].append(
                "A leitura de eventos VMS atingiu o limite de páginas/tempo e continuará na próxima execução."
            )
        if contagem.get("erro"):
            aviso["erro"] = True
            aviso["mensagens"].append(
                "Falha ao atualizar o cursor de eventos VMS; o inventário atual foi mantido."
            )
        logger.info(
            "Eventos VMS: empresa=%s raiz=%s uploads=%s exclusoes=%s paginas=%s baseline=%s incompleto=%s erro=%s",
            empresa,
            raiz_vms,
            contagem["uploads"],
            contagem["exclusoes"],
            contagem["paginas"],
            contagem["baseline"],
            contagem["incompleto"],
            contagem["erro"],
        )
        _save(caminho_cache, cache)

    for key, vm in cache.get("vms", {}).items():
        depois = len((vm or {}).get("exclusoes", []) or [])
        novas = max(depois - exclusoes_antes_cursor.get(key, 0), 0)
        if novas:
            novas_exclusoes_por_vm[key] = novas_exclusoes_por_vm.get(key, 0) + novas

    if detectar_ausentes:
        # VMs vistas em execuções anteriores que sumiram do inventário atual.
        for key, vm_cache in list(cache.get("vms", {}).items()):
            empresa = str((vm_cache or {}).get("empresa", ""))
            vmid = str((vm_cache or {}).get("vmid", ""))
            if (
                not empresa
                or empresa.lower() not in empresas_processadas
                or empresa.lower() in empresas_inventario_invalido
                or empresa.lower() in empresas_fora_escopo
                or _politica_isenta(_politica(config, empresa, vmid))
                or key in chaves_atuais
            ):
                continue
            vm_cache.setdefault("ausente_desde", detectado_em)
            registros.append(_registro_vm_ausente(empresa, vmid, vm_cache, "histórico persistente"))
            chaves_atuais.add(key)

        # VMs declaradas em política manual também são esperadas, mesmo sem histórico anterior.
        for empresa_cfg_nome, vms_cfg in _politicas(config).items():
            empresa = str(empresa_cfg_nome)
            if (
                empresa.lower() not in empresas_processadas
                or empresa.lower() in empresas_inventario_invalido
                or empresa.lower() in empresas_fora_escopo
                or not isinstance(vms_cfg, dict)
            ):
                continue
            for vmid_cfg, vm_cfg in vms_cfg.items():
                vmid = str(vmid_cfg).lower().removeprefix("vm-")
                if _politica_isenta(vm_cfg if isinstance(vm_cfg, dict) else None):
                    continue
                key = _vm_key(empresa, vmid)
                if key in chaves_atuais:
                    continue
                vm_cache = cache.setdefault("vms", {}).setdefault(
                    key,
                    {
                        "empresa": empresa,
                        "vmid": vmid,
                        "uploads": [],
                        "exclusoes": [],
                        "arquivos_atuais": [],
                        "ausente_desde": detectado_em,
                    },
                )
                registros.append(_registro_vm_ausente(empresa, vmid, vm_cache, "política manual"))
                chaves_atuais.add(key)

    for registro in registros:
        if registro.get("tipo_registro") != "VM":
            continue
        empresa = str(registro.get("empresa", ""))
        vmid = str(registro.get("vmid", ""))
        key = _vm_key(empresa, vmid, str(registro.get("caminho_dropbox", "")))
        vm_cache = cache.get("vms", {}).get(key, {})
        _recalcular_registro(registro, vm_cache, config, tzinfo, novas_exclusoes_por_vm.get(key, 0))
        aviso = avisos_por_empresa.get(empresa.lower(), {})
        mensagens = list(dict.fromkeys(aviso.get("mensagens", []) or []))
        if mensagens:
            registro["observacao"] = f"{str(registro.get('observacao', '')).strip()} {' '.join(mensagens)}".strip()
        registro.pop("backups_atuais_inventario", None)

    registros.extend(_registros_eventos_empresa(avisos_por_empresa))
    _save(caminho_cache, cache)
    return registros
