from __future__ import annotations

import logging
import sys
import tempfile
import types
import unittest
import json
import zipfile
from unittest import mock

import yaml
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


# Stubs suficientes para testar a lógica sem instalar ou chamar o SDK Dropbox.
class ApiError(Exception):
    pass


class AuthError(Exception):
    pass


class FileMetadata:
    def __init__(self, name: str, path_display: str, server_modified: datetime, size: int = 0):
        self.name = name
        self.path_display = path_display
        self.path_lower = path_display.lower()
        self.server_modified = server_modified
        self.size = size


class FolderMetadata:
    def __init__(self, name: str, path_display: str):
        self.name = name
        self.path_display = path_display
        self.path_lower = path_display.lower()


class DeletedMetadata:
    def __init__(self, name: str, path_display: str):
        self.name = name
        self.path_display = path_display
        self.path_lower = path_display.lower()


class DummyOAuth:
    def __init__(self, *args, **kwargs):
        pass


class DummyDropbox:
    def __init__(self, *args, **kwargs):
        pass


dropbox_mod = types.ModuleType("dropbox")
dropbox_mod.Dropbox = DummyDropbox
exceptions_mod = types.ModuleType("dropbox.exceptions")
for name, cls in {
    "ApiError": ApiError,
    "AuthError": AuthError,
    "HttpError": ApiError,
    "InternalServerError": ApiError,
    "RateLimitError": ApiError,
}.items():
    setattr(exceptions_mod, name, cls)
files_mod = types.ModuleType("dropbox.files")
files_mod.FileMetadata = FileMetadata
files_mod.FolderMetadata = FolderMetadata
files_mod.DeletedMetadata = DeletedMetadata
oauth_mod = types.ModuleType("dropbox.oauth")
oauth_mod.DropboxOAuth2FlowNoRedirect = DummyOAuth
sys.modules.update(
    {
        "dropbox": dropbox_mod,
        "dropbox.exceptions": exceptions_mod,
        "dropbox.files": files_mod,
        "dropbox.oauth": oauth_mod,
    }
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from auditor_bkp import auditor  # noqa: E402
from auditor_bkp import auditor_arquivos  # noqa: E402
from auditor_bkp import auditor_vms  # noqa: E402
from auditor_bkp import vms_history  # noqa: E402
from auditor_bkp import pdf_reports  # noqa: E402
from auditor_bkp.config_validation import ConfigurationError, validate_config  # noqa: E402
from auditor_bkp.security import redact_text, sanitize_csv_cell  # noqa: E402
from auditor_bkp.status_catalog import enriquecer_registro, info as status_info  # noqa: E402
from auditor_bkp.version import VERSION  # noqa: E402
from auditor_bkp import integration  # noqa: E402
from auditor_bkp import managerial_policy  # noqa: E402
from auditor_bkp.release_guard import ReleaseGuardError, validar_politicas_criticas
from auditor_bkp.version import BUILD
import auditoria as launcher  # noqa: E402
import validar_dropbox as live_validator  # noqa: E402


class Result:
    def __init__(self, entries, cursor, has_more):
        self.entries = entries
        self.cursor = cursor
        self.has_more = has_more


class FakeDbx:
    def __init__(self, continue_results=None, list_results=None):
        self.continue_results = dict(continue_results or {})
        self.list_results = list(list_results or [])
        self.latest_counter = 0

    def files_list_folder_get_latest_cursor(self, *args, **kwargs):
        self.latest_counter += 1
        return types.SimpleNamespace(cursor=f"baseline-{self.latest_counter}")

    def files_list_folder_continue(self, cursor):
        result = self.continue_results.get(cursor)
        if isinstance(result, Exception):
            raise result
        if result is None:
            return Result([], cursor, False)
        return result

    def files_list_folder(self, *args, **kwargs):
        if not self.list_results:
            return Result([], "full-final", False)
        return self.list_results.pop(0)

    def files_get_metadata(self, path):
        raise ApiError("not_found")


class V25Tests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("America/Sao_Paulo")
        self.logger = logging.getLogger("test-v25")
        self.config = {
            "estrutura": {"pasta_arquivos": "arquivos"},
            "analise": {"arquivos_max_idade_horas": 168, "max_backups_por_vm_para_relatorio": 10},
            "tolerancias": {
                "diario_horas": 12,
                "semanal_horas": 24,
                "quinzenal_horas": 36,
                "mensal_horas": 48,
                "irregular_horas": 24,
            },
            "auditoria_vms": {"minimo_eventos_aprender": 4},
        }
        self.cfg = auditor_arquivos.config_arquivos(self.config)

    def test_delta_streaming_e_exclusoes(self):
        now = datetime.now(timezone.utc)
        dbx = FakeDbx(
            {
                "old": Result(
                    [FileMetadata("a.txt", "/Aplicativos/EMP/arquivos/a.txt", now, 10)],
                    "c1",
                    True,
                ),
                "c1": Result(
                    [DeletedMetadata("b.txt", "/Aplicativos/EMP/arquivos/b.txt")],
                    "c2",
                    False,
                ),
            }
        )
        cache = {
            "versao": 2,
            "empresas": {
                "emp|/aplicativos/emp/arquivos": {
                    "empresa": "EMP",
                    "path": "/Aplicativos/EMP/arquivos",
                    "cursor_confirmado": "old",
                    "cursor_inclui_exclusoes": True,
                }
            },
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            registro = auditor_arquivos.auditar_semanal_empresa(
                dbx, "EMP", "/Aplicativos/EMP", self.config, self.cfg, cache, path,
                self.tz, self.logger, 10, 60, 1, False, True,
            )
        self.assertEqual(registro["status"], "ATIVIDADE_CONFIRMADA")
        self.assertEqual(registro["arquivos_criados_ou_alterados"], 1)
        self.assertEqual(registro["arquivos_excluidos"], 1)
        estado = cache["empresas"]["emp|/aplicativos/emp/arquivos"]
        self.assertNotIn("pendente", estado)
        self.assertEqual(estado["cursor_confirmado"], "c2")

    def test_delta_parcial_retorna_sem_perder_resumo(self):
        now = datetime.now(timezone.utc)
        dbx1 = FakeDbx(
            {
                "old": Result([FileMetadata("a.txt", "/Aplicativos/EMP/arquivos/a.txt", now, 10)], "c1", True),
            }
        )
        cache = {
            "versao": 2,
            "empresas": {
                "emp|/aplicativos/emp/arquivos": {
                    "empresa": "EMP", "path": "/Aplicativos/EMP/arquivos",
                    "cursor_confirmado": "old", "cursor_inclui_exclusoes": True,
                }
            },
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            r1 = auditor_arquivos.auditar_semanal_empresa(
                dbx1, "EMP", "/Aplicativos/EMP", self.config, self.cfg, cache, path,
                self.tz, self.logger, 1, 60, 1, False, True,
            )
            self.assertEqual(r1["status"], "INCOMPLETO")
            dbx2 = FakeDbx(
                {"c1": Result([FileMetadata("b.txt", "/Aplicativos/EMP/arquivos/b.txt", now, 20)], "c2", False)}
            )
            r2 = auditor_arquivos.auditar_semanal_empresa(
                dbx2, "EMP", "/Aplicativos/EMP", self.config, self.cfg, cache, path,
                self.tz, self.logger, 1, 60, 1, False, True,
            )
        self.assertEqual(r2["status"], "ATIVIDADE_CONFIRMADA")
        self.assertEqual(r2["arquivos_criados_ou_alterados"], 2)

    def test_completo_retomado_preserva_totais(self):
        now = datetime.now(timezone.utc)
        dbx1 = FakeDbx(
            list_results=[Result([FileMetadata("a", "/Aplicativos/EMP/arquivos/a", now, 10)], "f1", True)]
        )
        cache = {"versao": 2, "empresas": {}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            r1 = auditor_arquivos.auditar_completo_empresa(
                dbx1, "EMP", "/Aplicativos/EMP", self.config, cache, path,
                self.tz, self.logger, 1, 60, 1, True, True,
            )
            self.assertEqual(r1["status"], "INCOMPLETO")
            dbx2 = FakeDbx({"f1": Result([FileMetadata("b", "/Aplicativos/EMP/arquivos/b", now, 20)], "f2", False)})
            r2 = auditor_arquivos.auditar_completo_empresa(
                dbx2, "EMP", "/Aplicativos/EMP", self.config, cache, path,
                self.tz, self.logger, 1, 60, 1, True, True,
            )
        self.assertEqual(r2["status"], "OK")
        self.assertEqual(r2["arquivos_total"], 2)
        self.assertEqual(r2["tamanho_total_gb"], round(30 / (1024**3), 3))

    def test_historico_vm_aprende_semanal(self):
        now = datetime.now(self.tz)
        uploads = []
        for i in range(4):
            dt = now - timedelta(days=7 * (3 - i))
            uploads.append(
                {
                    "arquivo": f"vzdump-qemu-100-{dt:%Y_%m_%d-%H_%M_%S}.vma.zst",
                    "backup_datetime": dt.isoformat(timespec="seconds"),
                    "server_modified": dt.isoformat(timespec="seconds"),
                    "referencia_status": dt.isoformat(timespec="seconds"),
                    "caminho": "/Aplicativos/EMP/VMS/pve/vm-100",
                }
            )
        registro = {
            "empresa": "EMP", "vmid": "100", "caminho_dropbox": "/Aplicativos/EMP/VMS/pve/vm-100",
            "quantidade_backups": 2, "status": "AMOSTRA_INSUFICIENTE", "observacao": "",
        }
        vm_cache = {"uploads": uploads, "exclusoes": []}
        vms_history._recalcular_registro(registro, vm_cache, self.config, self.tz, 0)
        self.assertEqual(registro["periodicidade_detectada"], "semanal")
        self.assertEqual(registro["quantidade_backups_historico"], 4)


    def test_cursor_vms_registra_upload_e_exclusao(self):
        now = datetime.now(timezone.utc)
        nome_upload = "vzdump-qemu-100-2026_07_11-19_00_06.vma.zst"
        nome_delete = "vzdump-qemu-100-2026_07_04-19_00_06.vma.zst"
        dbx = FakeDbx(
            {
                "vm-old": Result(
                    [
                        FileMetadata(nome_upload, f"/Aplicativos/EMP/VMS/pve/vm-100/{nome_upload}", now, 100),
                        DeletedMetadata(nome_delete, f"/Aplicativos/EMP/VMS/pve/vm-100/{nome_delete}"),
                    ],
                    "vm-new",
                    False,
                )
            }
        )
        cache = {
            "versao": 1,
            "vms": {},
            "cursores": {
                "emp|/aplicativos/emp/vms/pve": {
                    "empresa": "EMP", "path": "/Aplicativos/EMP/VMS/pve", "cursor": "vm-old"
                }
            },
        }
        contagem = vms_history._processar_eventos_cursor(
            dbx, "EMP", "/Aplicativos/EMP/VMS/pve", cache, self.tz, self.logger, 10
        )
        self.assertEqual(contagem["uploads"], 1)
        self.assertEqual(contagem["exclusoes"], 1)
        vm = cache["vms"]["emp|100"]
        self.assertEqual(len(vm["uploads"]), 1)
        self.assertEqual(len(vm["exclusoes"]), 1)

    def test_backup_fora_exige_arvore_vms(self):
        self.assertTrue(auditor_vms.caminho_em_pasta_permitida_para_backup("/Aplicativos/EMP/VMS/pve/vm-100/a.vma.zst"))
        self.assertFalse(auditor_vms.caminho_em_pasta_permitida_para_backup("/Aplicativos/EMP/arquivos/pve/a.vma.zst"))


    def test_regressao_inventario_completo_nao_gera_exclusao_falsa(self):
        now = datetime.now(self.tz)
        nomes = [f"vzdump-qemu-100-2026_07_{i:02d}-19_00_00.vma.zst" for i in range(1, 12)]
        inventario = [
            {
                "arquivo": nome,
                "backup_datetime": now.isoformat(timespec="seconds"),
                "server_modified": now.isoformat(timespec="seconds"),
                "referencia_status": now.isoformat(timespec="seconds"),
                "caminho": f"/Aplicativos/EMP/VMS/pve/vm-100/{nome}",
            }
            for nome in nomes
        ]
        registro = {
            "tipo_registro": "VM", "empresa": "EMP", "vmid": "100", "vm": "vm-100",
            "caminho_dropbox": "/Aplicativos/EMP/VMS/pve/vm-100",
            "caminho_vms_usado": "/Aplicativos/EMP/VMS/pve",
            "pasta_vms_origem": "/Aplicativos/EMP/VMS/pve",
            "quantidade_backups": len(nomes), "status": "OK", "observacao": "",
            "ultimo_arquivo": nomes[-1], "caminho_ultimo_arquivo": f"/Aplicativos/EMP/VMS/pve/vm-100/{nomes[-1]}",
            "ultimos_backups": inventario[-10:],
            "backups_atuais_inventario": inventario,
        }
        cache = {
            "versao": 2,
            "vms": {
                "emp|100": {
                    "empresa": "EMP", "vmid": "100", "uploads": [], "exclusoes": [],
                    "arquivos_atuais": nomes,
                }
            },
            "cursores": {},
        }
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "cache").mkdir()
            path = base / "cache" / "auditoria_vms_historico.json"
            path.write_text(__import__("json").dumps(cache), encoding="utf-8")
            vms_history.aplicar_historico_vms(
                [registro], FakeDbx(), self.config, self.tz, base, self.logger
            )
            salvo = __import__("json").loads(path.read_text(encoding="utf-8"))
        self.assertEqual(salvo["vms"]["emp|100"]["exclusoes"], [])
        self.assertEqual(len(salvo["vms"]["emp|100"]["arquivos_atuais"]), 11)

    def test_regressao_vm_historica_ausente_aparece_no_relatorio(self):
        cache = {
            "versao": 2,
            "vms": {
                "emp|100": {
                    "empresa": "EMP", "vmid": "100", "uploads": [], "exclusoes": [],
                    "arquivos_atuais": ["antigo.vma.zst"],
                    "ultimo_caminho": "/Aplicativos/EMP/VMS/pve/vm-100",
                }
            },
            "cursores": {},
        }
        registros = [{
            "tipo_registro": "EMPRESA", "empresa": "EMP", "status": "SEM_PASTA_VMS",
            "caminho_dropbox": "/Aplicativos/EMP", "observacao": "",
        }]
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "cache").mkdir()
            path = base / "cache" / "auditoria_vms_historico.json"
            path.write_text(__import__("json").dumps(cache), encoding="utf-8")
            saida = vms_history.aplicar_historico_vms(
                registros, FakeDbx(), self.config, self.tz, base, self.logger
            )
        ausentes = [r for r in saida if r.get("status") == "VM_NAO_ENCONTRADA"]
        self.assertEqual(len(ausentes), 1)
        self.assertEqual(ausentes[0]["vmid"], "100")

    def test_regressao_heartbeat_atrasado_tem_status_proprio(self):
        class DbxHeartbeat(FakeDbx):
            def files_get_metadata(self, path):
                antigo = datetime.now(timezone.utc) - timedelta(hours=240)
                return FileMetadata("ultimo_backup.json", path, antigo, 10)

        cfg = dict(self.cfg)
        cfg["heartbeat_arquivo"] = ".auditoria/ultimo_backup.json"
        cfg["heartbeat_max_idade_horas"] = 168
        cache = {
            "versao": 2,
            "empresas": {
                "emp|/aplicativos/emp/arquivos": {
                    "empresa": "EMP", "path": "/Aplicativos/EMP/arquivos",
                    "cursor_confirmado": "old", "cursor_inclui_exclusoes": True,
                }
            },
        }
        with tempfile.TemporaryDirectory() as td:
            registro = auditor_arquivos.auditar_semanal_empresa(
                DbxHeartbeat({"old": Result([], "new", False)}),
                "EMP", "/Aplicativos/EMP", self.config, cfg, cache, Path(td) / "cache.json",
                self.tz, self.logger, 10, 60, 1, False, True,
            )
        self.assertEqual(registro["status"], "HEARTBEAT_ATRASADO")
        self.assertEqual(registro["heartbeat"]["estado"], "ATRASADO")

    def test_regressao_busca_fora_do_lugar_processa_paginas_em_streaming(self):
        now = datetime.now(timezone.utc)
        a = "vzdump-qemu-100-2026_07_10-19_00_00.vma.zst"
        b = "vzdump-qemu-101-2026_07_11-19_00_00.vma.zst"
        dbx = FakeDbx(
            continue_results={
                "p1": Result([FileMetadata(b, f"/Aplicativos/EMP/Arquivos/{b}", now, 100)], "p2", False)
            },
            list_results=[Result([FileMetadata(a, f"/Aplicativos/EMP/Arquivos/{a}", now, 100)], "p1", True)],
        )
        achados, meta = auditor_vms.encontrar_backups_fora_do_lugar(
            dbx, "/Aplicativos", self.tz, self.logger, auditor.EstatisticasExecucao(), True, limite=1
        )
        self.assertEqual(meta["paginas"], 2)
        self.assertEqual(meta["total_encontrado"], 2)
        self.assertEqual(meta["resultados_omitidos"], 1)
        self.assertEqual(len(achados), 1)


    def test_v25_chunks_rclone_sao_um_backup_logico(self):
        dt = datetime.now(timezone.utc)
        base = "vzdump-lxc-107-2026_07_09-20_26_30.tar.zst"
        arquivos = [
            FileMetadata(base, f"/Aplicativos/EMP/VMS/pve/vm-107/{base}", dt, 10),
            FileMetadata(base + ".rclone_chunk.001", f"/Aplicativos/EMP/VMS/pve/vm-107/{base}.rclone_chunk.001", dt + timedelta(minutes=1), 20),
            FileMetadata(base + ".rclone_chunk.002", f"/Aplicativos/EMP/VMS/pve/vm-107/{base}.rclone_chunk.002", dt + timedelta(minutes=2), 30),
        ]
        registro = auditor.analisar_vm_por_arquivos(
            "EMP", FolderMetadata("vm-107", "/Aplicativos/EMP/VMS/pve/vm-107"),
            arquivos, self.config, self.tz, datetime.now(self.tz), self.logger,
        )
        self.assertEqual(registro["quantidade_backups"], 1)
        self.assertEqual(len(registro["backups_atuais_inventario"]), 1)
        self.assertEqual(registro["backups_atuais_inventario"][0]["quantidade_partes"], 3)
        self.assertEqual(registro["ultimo_arquivo"], base)

    def test_v25_periodicidade_aceita_lacuna_multipla_do_ciclo(self):
        inicio = datetime(2026, 6, 1, 20, 0, tzinfo=self.tz)
        datas = [inicio, inicio + timedelta(days=7), inicio + timedelta(days=21), inicio + timedelta(days=28)]
        resultado = auditor.detectar_periodicidade(datas)
        self.assertEqual(resultado.periodicidade, "semanal")
        self.assertNotEqual(resultado.confianca, "IRREGULAR")
        self.assertIn("multiplos", resultado.observacao)

    def test_v25_reconhece_pastas_numericas_de_vm(self):
        class DbxPorPasta(FakeDbx):
            def files_list_folder(self, path, **kwargs):
                dados = {
                    "/Aplicativos/AZUL": [FolderMetadata("VMS", "/Aplicativos/AZUL/VMS")],
                    "/Aplicativos/AZUL/VMS": [
                        FolderMetadata("100", "/Aplicativos/AZUL/VMS/100"),
                        FolderMetadata("101", "/Aplicativos/AZUL/VMS/101"),
                    ],
                }
                return Result(dados.get(path, []), f"c-{path}", False)

        cfg = dict(self.config)
        cfg["estrutura"] = {"pasta_vms": "VMS/pve", "prefixo_vm": "vm-"}
        encontrados = auditor.listar_vm_folders_empresa(
            DbxPorPasta(), "/Aplicativos/AZUL", cfg, self.logger,
            auditor.EstatisticasExecucao(), True,
        )
        self.assertEqual({Path(v.folder.path_display).name for v in encontrados}, {"100", "101"})

    def test_v25_aprendizado_recente_e_informativo(self):
        agora = datetime.now(self.tz)
        registro = {
            "empresa": "EMP", "vmid": "100", "status": "AMOSTRA_INSUFICIENTE",
            "quantidade_backups": 1, "quantidade_backups_atuais": 1, "observacao": "",
        }
        vm_cache = {"uploads": [{
            "arquivo": "vzdump-qemu-100-2026_07_14-10_00_00.vma.zst",
            "referencia_status": (agora - timedelta(days=2)).isoformat(timespec="seconds"),
            "server_modified": (agora - timedelta(days=2)).isoformat(timespec="seconds"),
            "backup_datetime": (agora - timedelta(days=2)).isoformat(timespec="seconds"),
        }], "exclusoes": []}
        cfg = dict(self.config)
        cfg["auditoria_vms"] = {"minimo_eventos_aprender": 4, "idade_maxima_sem_historico_dias": 45}
        vms_history._recalcular_registro(registro, vm_cache, cfg, self.tz, 0)
        self.assertEqual(registro["status"], "EM_APRENDIZADO")
        self.assertEqual(registro["confianca"], "EM_APRENDIZADO")

    def test_v25_aprendizado_com_upload_antigo_da_atencao(self):
        agora = datetime.now(self.tz)
        registro = {
            "empresa": "EMP", "vmid": "100", "status": "AMOSTRA_INSUFICIENTE",
            "quantidade_backups": 1, "quantidade_backups_atuais": 1, "observacao": "",
        }
        antigo = agora - timedelta(days=60)
        vm_cache = {"uploads": [{
            "arquivo": "vzdump-qemu-100-2026_05_14-10_00_00.vma.zst",
            "referencia_status": antigo.isoformat(timespec="seconds"),
            "server_modified": antigo.isoformat(timespec="seconds"),
            "backup_datetime": antigo.isoformat(timespec="seconds"),
        }], "exclusoes": []}
        cfg = dict(self.config)
        cfg["auditoria_vms"] = {"minimo_eventos_aprender": 4, "idade_maxima_sem_historico_dias": 45}
        vms_history._recalcular_registro(registro, vm_cache, cfg, self.tz, 0)
        self.assertEqual(registro["status"], "SEM_UPLOAD_RECENTE")

    def test_v25_empresa_migrada_fica_fora_do_escopo(self):
        cfg = {"empresas": {"EMP": {
            "estado": "migrado_drive", "auditar_arquivos": False, "auditar_vms": False,
            "observacao": "Migrada para o Drive",
        }}}
        self.assertFalse(auditor.empresa_audita(cfg, "emp", "arquivos"))
        self.assertFalse(auditor.empresa_audita(cfg, "EMP", "vms"))
        self.assertEqual(auditor.status_empresa_fora_escopo(cfg, "EMP"), "MIGRADO_PARA_DRIVE")

    def test_v25_gera_pdfs_validos(self):
        agora = datetime.now(self.tz)
        arquivos = [{
            "empresa": "EMP", "status": "ATIVIDADE_CONFIRMADA", "arquivos_criados_ou_alterados": 2,
            "arquivos_excluidos": 0, "ultima_modificacao": agora.isoformat(timespec="seconds"),
            "paginas_lidas": 1, "tempo_segundos": 2, "observacao": "Atividade recente.",
        }]
        vms = [{
            "tipo_registro": "VM", "empresa": "EMP", "vm": "vm-100", "status": "EM_APRENDIZADO",
            "ultima_atualizacao_dropbox": agora.isoformat(timespec="seconds"),
            "periodicidade_detectada": "amostra_insuficiente", "confianca": "EM_APRENDIZADO",
            "quantidade_backups": 1, "quantidade_backups_atuais": 1,
            "quantidade_backups_historico": 1, "observacao": "Historico inicial.",
        }]
        consolidado = [{
            "empresa": "EMP", "status_geral": "INFORMATIVO", "status_arquivos": "ATIVIDADE_CONFIRMADA",
            "atividade_arquivos": 2, "ultima_atividade_arquivos": agora.isoformat(timespec="seconds"),
            "vms_total": 1, "vms_criticas": 0, "vms_atencao": 0,
            "status_vms": "EM_APRENDIZADO", "acao": "Acompanhar aprendizado",
        }]
        with tempfile.TemporaryDirectory() as td:
            paths = [Path(td) / nome for nome in ("arquivos.pdf", "vms.pdf", "consolidado.pdf")]
            pdf_reports.gerar_pdf_arquivos(arquivos, "semanal", agora, paths[0])
            pdf_reports.gerar_pdf_vms(vms, {"total_empresas": 1}, agora, paths[1])
            pdf_reports.gerar_pdf_consolidado(consolidado, agora, paths[2], 0, 0)
            for path in paths:
                self.assertTrue(path.read_bytes().startswith(b"%PDF"))
                self.assertGreater(path.stat().st_size, 1500)

    def test_v25_migracao_cache_deduplica_chunks(self):
        base = "vzdump-lxc-107-2026_07_09-20_26_30.tar.zst"
        cache = {"versao": 2, "vms": {"emp|107": {"uploads": [
            {"arquivo": base, "server_modified": "2026-07-09T20:30:00-03:00"},
            {"arquivo": base + ".rclone_chunk.001", "server_modified": "2026-07-09T20:31:00-03:00"},
        ], "exclusoes": [], "arquivos_atuais": [base, base + ".rclone_chunk.001"]}}, "cursores": {}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            path.write_text(__import__("json").dumps(cache), encoding="utf-8")
            migrado = vms_history._load(path)
        vm = migrado["vms"]["emp|107"]
        self.assertEqual(migrado["versao"], 3)
        self.assertEqual(len(vm["uploads"]), 1)
        self.assertEqual(vm["uploads"][0]["arquivo"], base)
        self.assertEqual(vm["arquivos_atuais"], [base])


    def test_v25_alto_volume_arquiva_checkpoint_e_recria_baseline(self):
        config = dict(self.config)
        config["empresas"] = {
            "EMPRESA-ALTO-VOLUME-BACKUP": {
                "auditar_arquivos": True,
                "auditoria_arquivos": {
                    "estrategia": "atividade_com_rebaseline",
                    "abandonar_checkpoint_existente": True,
                    "max_paginas_amostragem": 3,
                },
            }
        }
        cache = {
            "versao": 2,
            "empresas": {
                "empresa-alto-volume-backup|/aplicativos/empresa-alto-volume-backup/arquivos": {
                    "empresa": "EMPRESA-ALTO-VOLUME-BACKUP",
                    "path": "/Aplicativos/EMPRESA-ALTO-VOLUME-BACKUP/arquivos",
                    "cursor_confirmado": "old",
                    "pendente": {
                        "cursor": "c160",
                        "paginas": 160,
                        "resumo": {
                            "arquivos_criados_ou_alterados": 79052,
                            "arquivos_excluidos": 604,
                        },
                    },
                }
            },
        }
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "cache" / "auditoria.json"
            registro = auditor_arquivos.auditar_semanal_empresa(
                FakeDbx(), "EMPRESA-ALTO-VOLUME-BACKUP", "/Aplicativos/EMPRESA-ALTO-VOLUME-BACKUP",
                config, auditor_arquivos.config_arquivos(config), cache, cache_path,
                self.tz, self.logger, 80, 900, 1, False, True,
            )
            arquivos = list((cache_path.parent / "backlogs_arquivados").glob("*.json"))
        self.assertEqual(registro["status"], "BASELINE_RECRIADA_POLITICA")
        self.assertEqual(registro["paginas_backlog_arquivadas"], 160)
        self.assertTrue(registro["backlog_descartado"])
        estado = cache["empresas"]["empresa-alto-volume-backup|/aplicativos/empresa-alto-volume-backup/arquivos"]
        self.assertNotIn("pendente", estado)
        self.assertEqual(estado["cursor_confirmado"], "baseline-1")
        self.assertEqual(len(arquivos), 1)

    def test_v25_amostragem_confirma_atividade_e_corta_backlog(self):
        now = datetime.now(timezone.utc)
        config = dict(self.config)
        config["empresas"] = {
            "EMP": {
                "auditoria_arquivos": {
                    "estrategia": "atividade_com_rebaseline",
                    "max_paginas_amostragem": 3,
                    "rebaseline_apos_confirmacao": True,
                }
            }
        }
        dbx = FakeDbx({
            "old": Result([FileMetadata("novo.txt", "/Aplicativos/EMP/arquivos/novo.txt", now, 10)], "c1", True)
        })
        cache = {"versao": 3, "empresas": {
            "emp|/aplicativos/emp/arquivos": {
                "empresa": "EMP", "path": "/Aplicativos/EMP/arquivos",
                "cursor_confirmado": "old", "cursor_inclui_exclusoes": True,
            }
        }}
        with tempfile.TemporaryDirectory() as td:
            registro = auditor_arquivos.auditar_semanal_empresa(
                dbx, "EMP", "/Aplicativos/EMP", config,
                auditor_arquivos.config_arquivos(config), cache,
                Path(td) / "cache" / "auditoria.json", self.tz, self.logger,
                80, 900, 1, False, True,
            )
        self.assertEqual(registro["status"], "ATIVIDADE_CONFIRMADA")
        self.assertEqual(registro["paginas_nesta_execucao"], 1)
        self.assertTrue(registro["backlog_descartado"])
        self.assertEqual(cache["empresas"]["emp|/aplicativos/emp/arquivos"]["cursor_confirmado"], "baseline-1")

    def test_v25_exclusao_vm_e_deduplicada_entre_origens(self):
        cache = {"versao": 3, "vms": {}, "cursores": {}}
        primeiro = vms_history._registrar_exclusao(
            cache, "EMP", "100", "backup.vma.zst", "/VMS/vm-100", "2026-07-14T10:00:00-03:00", "comparacao_inventario_completo"
        )
        segundo = vms_history._registrar_exclusao(
            cache, "EMP", "100", "backup.vma.zst", "/VMS/vm-100/backup.vma.zst", "2026-07-14T10:01:00-03:00", "cursor_dropbox"
        )
        self.assertTrue(primeiro)
        self.assertFalse(segundo)
        exclusoes = cache["vms"]["emp|100"]["exclusoes"]
        self.assertEqual(len(exclusoes), 1)
        self.assertEqual(set(exclusoes[0]["origens"]), {"comparacao_inventario_completo", "cursor_dropbox"})

    def test_v25_historico_nao_fica_menor_que_inventario_atual(self):
        agora = datetime.now(self.tz)
        uploads = []
        for i in range(61):
            dt = agora - timedelta(days=60-i)
            uploads.append({
                "arquivo": f"vzdump-qemu-100-2026_05_{(i%28)+1:02d}-10_00_{i%60:02d}.vma.zst",
                "referencia_status": dt.isoformat(timespec="seconds"),
                "server_modified": dt.isoformat(timespec="seconds"),
                "backup_datetime": dt.isoformat(timespec="seconds"),
            })
        registro = {
            "empresa": "EMP", "vmid": "100", "status": "OK",
            "quantidade_backups": 61, "quantidade_backups_atuais": 61, "observacao": "",
        }
        vm_cache = {"uploads": uploads, "exclusoes": []}
        cfg = dict(self.config)
        cfg["auditoria_vms"] = {"minimo_eventos_aprender": 4, "max_eventos_historico_por_vm": 60}
        vms_history._recalcular_registro(registro, vm_cache, cfg, self.tz, 0)
        self.assertEqual(registro["quantidade_backups_historico"], 61)
        self.assertEqual(len(vm_cache["uploads"]), 61)


    def test_v25_versao_publica_e_25(self):
        self.assertEqual(VERSION, "2.5")

    def test_v25_config_exemplo_e_valido(self):
        config_path = ROOT / "config.example.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8-sig"))
        validado = validate_config(config, config_path)
        self.assertIn("EMPRESA-EXEMPLO-BACKUP", validado["empresas"])
        self.assertTrue(validado["empresas"]["EMPRESA-EXEMPLO-BACKUP"]["auditar_arquivos"])

    def test_v25_config_rejeita_segredo_no_yaml(self):
        config = {
            "raiz_dropbox": "/Aplicativos",
            "timezone": "America/Sao_Paulo",
            "dropbox": {"app_secret": "nao-deve-ficar-aqui"},
        }
        with self.assertRaises(ConfigurationError):
            validate_config(config)

    def test_v25_config_detecta_erro_de_indentacao_de_empresa(self):
        config = {
            "raiz_dropbox": "/Aplicativos",
            "timezone": "America/Sao_Paulo",
            "empresas": {"EMP": {}},
            "auditar_arquivos": True,
        }
        with self.assertRaises(ConfigurationError):
            validate_config(config)

    def test_v25_csv_neutraliza_formula(self):
        self.assertEqual(sanitize_csv_cell("=HYPERLINK(\"x\")"), "'=HYPERLINK(\"x\")")
        self.assertEqual(sanitize_csv_cell("@SUM(A1:A2)"), "'@SUM(A1:A2)")
        self.assertEqual(sanitize_csv_cell("arquivo-normal.zst"), "arquivo-normal.zst")

    def test_v25_redacao_mascara_token_conhecido(self):
        texto = redact_text("DROPBOX_REFRESH_TOKEN=segredo123456")
        self.assertNotIn("segredo123456", texto)
        self.assertIn("[REDACTED]", texto)

    def test_v25_status_expoe_codigo_e_texto_humano(self):
        registro = enriquecer_registro({"status": "SEM_BACKUP", "empresa": "EMP"})
        self.assertEqual(registro["status"], "SEM_BACKUP")
        self.assertEqual(registro["status_rotulo"], "Sem backup válido")
        self.assertEqual(registro["status_nivel"], "erro")
        self.assertEqual(status_info("DESCONHECIDO_X").nivel, "atencao")

    def test_v25_launcher_executa_modulo_sem_shell(self):
        fake = types.SimpleNamespace(returncode=0)
        with mock.patch.object(launcher.subprocess, "run", return_value=fake) as run:
            codigo = launcher.executar_modulo("auditor_vms.py", ["--listar-empresas"])
        self.assertEqual(codigo, 0)
        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["-m", "auditor_bkp.auditor_vms"])
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_v25_integracao_inclui_config_e_sem_shell(self):
        fake = types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(integration.subprocess, "run", return_value=fake) as run:
            resultado = integration.executar_auditoria("vms", config_path=ROOT / "config.example.yaml")
        self.assertEqual(resultado.codigo_saida, 0)
        command = run.call_args.args[0]
        self.assertIn("--config", command)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(resultado.schema_version, "1.0")

    def test_v25_validacao_online_e_nao_destrutiva(self):
        class Cliente:
            def __init__(self):
                self.calls = []
                self.closed = False

            def files_list_folder_get_latest_cursor(self, **kwargs):
                self.calls.append(kwargs)
                return types.SimpleNamespace(cursor="nao-exibir")

            def close(self):
                self.closed = True

        cliente = Cliente()
        cfg = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8-sig"))
        with mock.patch.object(live_validator, "parse_args", return_value=types.SimpleNamespace(config="config.yaml")), \
             mock.patch.object(live_validator, "carregar_config", return_value=cfg), \
             mock.patch.object(live_validator, "criar_cliente_dropbox", return_value=cliente):
            codigo = live_validator.main()
        self.assertEqual(codigo, 0)
        self.assertEqual(cliente.calls, [{"path": "/Aplicativos", "recursive": False}])
        self.assertTrue(cliente.closed)

    def test_v25_pacote_suporte_mascara_segredo_e_cache_e_opt_in(self):
        root_antigo, lock_antigo = launcher.ROOT, launcher.LOCK_PATH
        try:
            with tempfile.TemporaryDirectory() as td:
                temp_root = Path(td)
                launcher.ROOT = temp_root
                launcher.LOCK_PATH = temp_root / "cache" / "auditoria_em_execucao.lock"
                for nome in ("logs", "relatorios", "cache", "suporte"):
                    (temp_root / nome).mkdir(parents=True, exist_ok=True)
                (temp_root / "VERSAO.txt").write_text("Auditor v2.5", encoding="utf-8")
                (temp_root / "config.yaml").write_text(
                    "raiz_dropbox: /Aplicativos\ndropbox:\n  app_secret: SUPERSEGREDO\n", encoding="utf-8"
                )
                (temp_root / "logs" / "teste.log").write_text(
                    "DROPBOX_REFRESH_TOKEN=TOKENULTRASSECRETO", encoding="utf-8"
                )
                (temp_root / "cache" / "estado.json").write_text(
                    json.dumps({"refresh_token": "TOKENCACHE", "empresa": "EMP"}), encoding="utf-8"
                )
                sem_cache = launcher.gerar_pacote_suporte(incluir_cache=False)
                with zipfile.ZipFile(sem_cache) as zf:
                    nomes = zf.namelist()
                    corpo = "\n".join(
                        zf.read(nome).decode("utf-8", "ignore") for nome in nomes if not nome.endswith(".pdf")
                    )
                    self.assertFalse(any(nome.startswith("cache/") for nome in nomes))
                    self.assertNotIn("SUPERSEGREDO", corpo)
                    self.assertNotIn("TOKENULTRASSECRETO", corpo)
                com_cache = launcher.gerar_pacote_suporte(incluir_cache=True)
                with zipfile.ZipFile(com_cache) as zf:
                    corpo = "\n".join(
                        zf.read(nome).decode("utf-8", "ignore") for nome in zf.namelist() if not nome.endswith(".pdf")
                    )
                    self.assertIn("cache/estado.json", zf.namelist())
                    self.assertNotIn("TOKENCACHE", corpo)
        finally:
            launcher.ROOT, launcher.LOCK_PATH = root_antigo, lock_antigo



    def test_v25_gerencial_incompleto_com_atividade_nao_vira_erro(self):
        registro = {
            "empresa": "EMP", "status": "INCOMPLETO",
            "arquivos_criados_ou_alterados": 12,
        }
        av = managerial_policy.avaliar_registro(registro, "arquivos", self.config)
        self.assertEqual(av.status, "INFORMATIVO")
        self.assertEqual(av.nivel, 1)

    def test_v25_gerencial_incompleto_sem_evidencia_vira_atencao(self):
        registro = {"empresa": "EMP", "status": "INCOMPLETO", "arquivos_criados_ou_alterados": 0}
        av = managerial_policy.avaliar_registro(registro, "arquivos", self.config)
        self.assertEqual(av.status, "ATENCAO")

    def test_v25_gerencial_irregular_recente_nao_vira_atencao(self):
        agora = datetime.now(self.tz)
        registro = {
            "tipo_registro": "VM", "empresa": "EMP", "vmid": "100", "status": "IRREGULAR",
            "ultimo_backup": (agora - timedelta(days=2)).isoformat(timespec="seconds"),
            "proximo_backup_previsto": (agora + timedelta(days=5)).isoformat(timespec="seconds"),
            "tolerancia_horas": 24,
        }
        av = managerial_policy.avaliar_registro(registro, "vms", self.config, now=agora)
        self.assertEqual(av.status, "INFORMATIVO")

    def test_v25_gerencial_irregular_nao_usa_previsao_inconfiavel_para_alertar(self):
        agora = datetime.now(self.tz)
        registro = {
            "tipo_registro": "VM", "empresa": "EMP", "vmid": "100", "status": "IRREGULAR",
            "ultimo_backup": (agora - timedelta(days=20)).isoformat(timespec="seconds"),
            "proximo_backup_previsto": (agora - timedelta(days=3)).isoformat(timespec="seconds"),
            "tolerancia_horas": 24,
        }
        av = managerial_policy.avaliar_registro(registro, "vms", self.config, now=agora)
        self.assertEqual(av.status, "INFORMATIVO")

    def test_v25_politica_manual_converte_vm_irregular_em_atraso_objetivo(self):
        agora = datetime.now(self.tz)
        registro = {
            "tipo_registro": "VM", "empresa": "EMP", "vmid": "100", "vm": "vm-100",
            "status": "IRREGULAR", "quantidade_backups": 1, "quantidade_backups_atuais": 1,
            "observacao": "",
        }
        cache_vm = {
            "uploads": [{
                "arquivo": "vzdump-lxc-100-2026_08_01-21_00_00.tar.zst",
                "backup_datetime": (agora - timedelta(days=10)).isoformat(timespec="seconds"),
                "server_modified": (agora - timedelta(days=10)).isoformat(timespec="seconds"),
                "referencia_status": (agora - timedelta(days=10)).isoformat(timespec="seconds"),
                "caminho": "/Aplicativos/EMP/VMS/pve/vm-100/x.tar.zst",
            }],
            "exclusoes": [],
        }
        cfg = dict(self.config)
        cfg["politicas_vms"] = {"EMP": {"100": {"frequencia": "diario", "tolerancia_horas": 24}}}
        vms_history._recalcular_registro(registro, cache_vm, cfg, self.tz, 0)
        self.assertEqual(registro["status"], "ATRASADO")
        av = managerial_policy.avaliar_registro(registro, "vms", cfg, now=agora)
        self.assertEqual(av.status, "ATENCAO")

    def test_v25_sem_pasta_vms_sem_expectativa_explicita_nao_vira_erro(self):
        registro = {"tipo_registro": "EMPRESA", "empresa": "EMP", "status": "SEM_PASTA_VMS"}
        av = managerial_policy.avaliar_registro(registro, "vms", self.config)
        self.assertEqual(av.status, "ATENCAO")

    def test_v25_sem_pasta_vms_com_expectativa_explicita_permanece_erro(self):
        cfg = dict(self.config)
        cfg["empresas"] = {"EMP": {"auditar_vms": True}}
        registro = {"tipo_registro": "EMPRESA", "empresa": "EMP", "status": "SEM_PASTA_VMS"}
        av = managerial_policy.avaliar_registro(registro, "vms", cfg)
        self.assertEqual(av.status, "ERRO")

    def test_v25_vm_isenta_nao_gera_sem_backup(self):
        agora = datetime.now(self.tz)
        registro = {
            "tipo_registro": "VM", "empresa": "EMP", "vmid": "101", "vm": "vm-101",
            "status": "SEM_BACKUP", "quantidade_backups": 0, "quantidade_backups_atuais": 0,
            "observacao": "",
        }
        cfg = dict(self.config)
        cfg["politicas_vms"] = {"EMP": {"101": {"isento": True, "motivo": "Fora da política de backup."}}}
        vms_history._recalcular_registro(registro, {"uploads": [], "exclusoes": []}, cfg, self.tz, 0)
        self.assertEqual(registro["status"], "NAO_APLICAVEL")
        av = managerial_policy.avaliar_registro(registro, "vms", cfg, now=agora)
        self.assertEqual(av.status, "INFORMATIVO")

    def test_v25_config_aceita_politica_vm_isenta(self):
        cfg = {
            "raiz_dropbox": "/Aplicativos",
            "timezone": "America/Sao_Paulo",
            "politicas_vms": {"EMP": {"101": {"isento": True, "motivo": "Exceção documentada"}}},
        }
        self.assertIs(validate_config(cfg), cfg)

    def test_v25_consolidado_aplica_politica_gerencial_sem_mudar_template(self):
        agora = datetime.now(self.tz)
        root_antigo, lock_antigo = launcher.ROOT, launcher.LOCK_PATH
        try:
            with tempfile.TemporaryDirectory() as td:
                temp_root = Path(td)
                rel = temp_root / "relatorios"
                rel.mkdir(parents=True)
                launcher.ROOT = temp_root
                launcher.LOCK_PATH = temp_root / "cache" / "auditoria_em_execucao.lock"
                arq = {
                    "registros": [{
                        "empresa": "EMP", "status": "INCOMPLETO",
                        "arquivos_criados_ou_alterados": 3,
                        "arquivos_excluidos": 0,
                        "ultima_modificacao": agora.isoformat(timespec="seconds"),
                    }]
                }
                vm = {
                    "registros": [{
                        "tipo_registro": "VM", "empresa": "emp", "vmid": "100", "vm": "vm-100",
                        "status": "IRREGULAR",
                        "ultimo_backup": agora.isoformat(timespec="seconds"),
                        "proximo_backup_previsto": (agora + timedelta(days=3)).isoformat(timespec="seconds"),
                        "tolerancia_horas": 24,
                    }]
                }
                a = rel / "auditoria_arquivos_teste.json"
                v = rel / "auditoria_vms_teste.json"
                a.write_text(json.dumps(arq), encoding="utf-8")
                v.write_text(json.dumps(vm), encoding="utf-8")
                inicio = min(a.stat().st_mtime, v.stat().st_mtime) - 1
                gerados = launcher.gerar_relatorio_consolidado(inicio, 3, 2, self.config)
                self.assertIsNotNone(gerados)
                payload = json.loads(Path(gerados["json"]).read_text(encoding="utf-8"))
                self.assertEqual(len(payload["empresas"]), 1)  # EMP/emp deduplicados por casefold
                linha = payload["empresas"][0]
                self.assertEqual(linha["status_geral"], "INFORMATIVO")
                self.assertEqual(linha["vms_atencao"], 0)
                self.assertEqual(payload["codigo_final"], 3)  # contrato técnico preservado
                self.assertEqual(payload["codigo_gerencial"], 0)
                self.assertTrue(Path(gerados["pdf"]).read_bytes().startswith(b"%PDF"))
        finally:
            launcher.ROOT, launcher.LOCK_PATH = root_antigo, lock_antigo

    def test_v25_sem_mudancas_e_informativo_por_padrao(self):
        registro = {"empresa": "EMP", "status": "SEM_MUDANCAS"}
        av = managerial_policy.avaliar_registro(registro, "arquivos", self.config)
        self.assertEqual(av.status, "INFORMATIVO")
        cfg = dict(self.config)
        cfg["empresas"] = {"EMP": {"exigir_atividade_arquivos": True}}
        av2 = managerial_policy.avaliar_registro(registro, "arquivos", cfg)
        self.assertEqual(av2.status, "ATENCAO")

    def test_v25_requirements_inclui_tzdata_para_windows(self):
        requirements = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertIn("tzdata", requirements)



    def test_v25_irregular_e_informativo_no_relatorio_tecnico(self):
        self.assertEqual(status_info("IRREGULAR").nivel, "informativo")

    def test_v25_sem_mudancas_e_informativo_no_relatorio_tecnico(self):
        self.assertEqual(status_info("SEM_MUDANCAS").nivel, "informativo")
        self.assertEqual(status_info("SOMENTE_EXCLUSOES").nivel, "informativo")

    def test_v25_codigo_saida_ignora_estados_apenas_informativos(self):
        from auditor_bkp.status_catalog import codigo_saida
        self.assertEqual(codigo_saida({"OK", "IRREGULAR", "EM_APRENDIZADO", "SEM_MUDANCAS"}), 0)
        self.assertEqual(codigo_saida({"OK", "ATRASADO"}), 2)
        self.assertEqual(codigo_saida({"OK", "SEM_BACKUP"}), 3)

    def test_v25_empresa_fora_escopo_anula_alerta_stale_no_consolidado(self):
        cfg = dict(self.config)
        cfg["empresas"] = {
            "EMPRESA-FORA-ESCOPO-BACKUP": {
                "estado": "fora_escopo", "auditar_arquivos": False, "auditar_vms": False
            }
        }
        registro = {
            "tipo_registro": "VM", "empresa": "EMPRESA-FORA-ESCOPO-BACKUP", "vmid": "100",
            "status": "SEM_UPLOAD_RECENTE",
            "ultimo_backup": (datetime.now(self.tz) - timedelta(days=90)).isoformat(timespec="seconds"),
        }
        av = managerial_policy.avaliar_registro(registro, "vms", cfg)
        self.assertEqual(av.status, "INFORMATIVO")
        self.assertEqual(av.nivel, 1)

    def test_v25_politica_explicita_reavalia_json_antigo_irregular(self):
        agora = datetime.now(self.tz)
        cfg = dict(self.config)
        cfg["politicas_vms"] = {
            "EMPRESA-DIARIA-BACKUP": {"100": {"frequencia": "diario", "tolerancia_horas": 36}}
        }
        registro = {
            "tipo_registro": "VM", "empresa": "EMPRESA-DIARIA-BACKUP", "vmid": "100",
            "status": "IRREGULAR",
            "ultimo_backup": (agora - timedelta(days=5)).isoformat(timespec="seconds"),
        }
        av = managerial_policy.avaliar_registro(registro, "vms", cfg, now=agora)
        self.assertEqual(av.status, "ATENCAO")
        self.assertEqual(av.nivel, 2)

    def test_v25_politica_explicita_pode_corrigir_falso_atraso_antigo(self):
        agora = datetime.now(self.tz)
        cfg = dict(self.config)
        cfg["politicas_vms"] = {
            "EMP": {"100": {"frequencia": "semanal", "tolerancia_horas": 24}}
        }
        registro = {
            "tipo_registro": "VM", "empresa": "EMP", "vmid": "100",
            "status": "ATRASADO",
            "ultimo_backup": (agora - timedelta(days=2)).isoformat(timespec="seconds"),
        }
        av = managerial_policy.avaliar_registro(registro, "vms", cfg, now=agora)
        self.assertEqual(av.status, "OK")
        self.assertEqual(av.nivel, 0)

    def test_v25_config_rejeita_fora_escopo_com_auditoria_ativa(self):
        cfg = {
            "raiz_dropbox": "/Aplicativos",
            "timezone": "America/Sao_Paulo",
            "empresas": {"EMP": {"estado": "fora_escopo", "auditar_arquivos": True, "auditar_vms": False}},
        }
        with self.assertRaises(ConfigurationError):
            validate_config(cfg)

    def test_v25_config_exemplo_contem_somente_casos_genericos(self):
        cfg = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8-sig"))
        self.assertTrue(cfg["empresas"]["EMPRESA-EXEMPLO-BACKUP"]["auditar_vms"])
        self.assertFalse(cfg["empresas"]["EMPRESA-FORA-ESCOPO-BACKUP"]["auditar_vms"])
        self.assertEqual(cfg["empresas"]["EMPRESA-FORA-ESCOPO-BACKUP"]["estado"], "fora_escopo")
        self.assertEqual(cfg["politicas_vms"]["EMPRESA-EXEMPLO-BACKUP"]["100"]["frequencia"], "diario")
        self.assertTrue(cfg["politicas_vms"]["EMPRESA-EXEMPLO-BACKUP"]["199"]["isento"])

    def test_v25_instalador_usa_venv_local(self):
        instalar = (ROOT / "instalar.bat").read_text(encoding="utf-8").lower()
        executar = (ROOT / "executar.bat").read_text(encoding="utf-8").lower()
        self.assertIn(".venv", instalar)
        self.assertIn("-m venv", instalar)
        self.assertIn(".venv\\scripts\\python.exe", executar)


    def test_v25_template_pdf_permanece_byte_identico(self):
        import hashlib
        esperado = "f54ef7952f1218965945ddde8b4c5495a30b647c8ce0287dbc9d220ee1f2124e"
        atual = hashlib.sha256((ROOT / "auditor_bkp" / "pdf_reports.py").read_bytes()).hexdigest()
        self.assertEqual(atual, esperado)

    def test_v25_integracao_expoe_codigo_tecnico_e_gerencial(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "auditoria_semanal_consolidada_teste.json"
            p.write_text(json.dumps({"codigo_final": 3, "codigo_gerencial": 2}), encoding="utf-8")
            tecnico, gerencial = integration._codigos_consolidado([str(p)], 0)
            self.assertEqual(tecnico, 3)
            self.assertEqual(gerencial, 2)

    def test_v25_consolidado_envia_codigo_gerencial_ao_template(self):
        agora = datetime.now(self.tz)
        root_antigo, lock_antigo = launcher.ROOT, launcher.LOCK_PATH
        try:
            with tempfile.TemporaryDirectory() as td:
                temp_root = Path(td)
                rel = temp_root / "relatorios"
                rel.mkdir(parents=True)
                launcher.ROOT = temp_root
                launcher.LOCK_PATH = temp_root / "cache" / "auditoria_em_execucao.lock"
                (rel / "auditoria_arquivos_teste.json").write_text(json.dumps({
                    "registros": [{"empresa": "EMP", "status": "ATIVIDADE_CONFIRMADA", "arquivos_criados_ou_alterados": 1}]
                }), encoding="utf-8")
                (rel / "auditoria_vms_teste.json").write_text(json.dumps({
                    "registros": [{"tipo_registro": "VM", "empresa": "EMP", "vmid": "100", "status": "ATRASADO",
                                   "ultimo_backup": (agora - timedelta(days=10)).isoformat(timespec="seconds")}]
                }), encoding="utf-8")
                inicio = min(x.stat().st_mtime for x in rel.glob("*.json")) - 1
                with mock.patch.object(launcher.pdf_reports, "gerar_pdf_consolidado") as gerar:
                    out = launcher.gerar_relatorio_consolidado(inicio, 3, 3, self.config)
                self.assertIsNotNone(out)
                args = gerar.call_args.args
                self.assertEqual(args[-2:], (2, 0))
                payload = json.loads(Path(out["json"]).read_text(encoding="utf-8"))
                self.assertEqual(payload["codigo_final"], 3)
                self.assertEqual(payload["codigo_gerencial"], 2)
        finally:
            launcher.ROOT, launcher.LOCK_PATH = root_antigo, lock_antigo

    def test_v25_build_revisao_operacional(self):
        self.assertEqual(BUILD, "20260909-r4")

    def test_v25_release_guard_aprova_build_publico(self):
        cfg = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8-sig"))
        validar_politicas_criticas(cfg)

    def test_v25_release_guard_rejeita_build_inesperado(self):
        cfg = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8-sig"))
        with mock.patch("auditor_bkp.release_guard.BUILD", "build-invalido"):
            with self.assertRaises(ReleaseGuardError):
                validar_politicas_criticas(cfg)


if __name__ == "__main__":
    unittest.main()
