"""Fachada estável para integração do Auditor Dropbox v2.5.

O consumidor não deve analisar mensagens do terminal. Esta fachada executa o
CLI em processo isolado, sem shell, e devolve um contrato JSON simples. Os
códigos internos dos relatórios continuam disponíveis nos arquivos JSON
produzidos pela auditoria.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .security import redact_text
from .version import VERSION

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = "1.0"


@dataclass
class IntegrationResult:
    schema_version: str
    versao: str
    operacao: str
    empresa: str
    iniciado_em: str
    concluido_em: str
    duracao_segundos: float
    codigo_saida: int
    resultado: str
    relatorios: list[str]
    logs: list[str]
    mensagem: str
    saida_resumida: str = ""
    codigo_tecnico: int = 0
    codigo_gerencial: int = 0
    resultado_gerencial: str = "sucesso"

    @property
    def sucesso(self) -> bool:
        return self.codigo_saida == 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _classificar_codigo(code: int) -> str:
    if code == 0:
        return "sucesso"
    if code == 2:
        return "atencao"
    return "erro"


def _novos_arquivos(folder: Path, since_epoch: float) -> list[str]:
    if not folder.exists():
        return []
    result: list[str] = []
    for item in folder.iterdir():
        try:
            if item.is_file() and item.stat().st_mtime >= since_epoch - 2:
                result.append(str(item.resolve()))
        except OSError:
            continue
    return sorted(result)


def _codigos_consolidado(relatorios: list[str], fallback: int) -> tuple[int, int]:
    """Lê códigos técnico/gerencial do consolidado sem depender do texto do terminal."""
    candidatos = [
        Path(item) for item in relatorios
        if Path(item).name.startswith("auditoria_semanal_consolidada_") and Path(item).suffix.lower() == ".json"
    ]
    if not candidatos:
        return fallback, fallback
    try:
        path = max(candidatos, key=lambda item: item.stat().st_mtime)
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        tecnico = int(payload.get("codigo_final", fallback))
        gerencial = int(payload.get("codigo_gerencial", tecnico))
        tecnico = 0 if tecnico == 0 else (2 if tecnico == 2 else 3)
        gerencial = 0 if gerencial == 0 else (2 if gerencial == 2 else 3)
        return tecnico, gerencial
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return fallback, fallback


def _resumir_saida(stdout: str, stderr: str, limite: int = 4000) -> str:
    combinado = "\n".join(parte.strip() for parte in (stdout, stderr) if parte and parte.strip())
    combinado = redact_text(combinado)
    if len(combinado) <= limite:
        return combinado
    return "..." + combinado[-limite:]


def executar_auditoria(
    operacao: str = "rotina-semanal",
    *,
    empresa: str = "",
    config_path: str | Path | None = None,
    modo_arquivos: str = "semanal",
    timeout_segundos: int | None = None,
) -> IntegrationResult:
    """Executa uma auditoria e devolve resultado estruturado.

    ``codigo_saida`` segue o contrato público:
    - 0: execução normal ou somente informativa;
    - 2: há ponto de atenção;
    - 3: erro ou resultado incompleto.
    """
    operacao = str(operacao).strip().lower()
    if operacao not in {"rotina-semanal", "arquivos", "vms"}:
        raise ValueError("operacao deve ser 'rotina-semanal', 'arquivos' ou 'vms'.")
    if modo_arquivos not in {"semanal", "cursor", "diagnostico", "completo"}:
        raise ValueError("modo_arquivos inválido.")

    cfg = Path(config_path).resolve() if config_path else (ROOT / "config.yaml").resolve()
    if not cfg.exists():
        raise FileNotFoundError(f"Configuração não encontrada: {cfg}")

    command = [sys.executable, str(ROOT / "auditoria.py"), operacao, "--config", str(cfg)]
    if empresa:
        command += ["--empresa", empresa]
    if operacao == "arquivos":
        command += ["--modo", modo_arquivos]

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env["AUDITOR_CONFIG_PATH"] = str(cfg)
    started = datetime.now().astimezone()
    started_epoch = time.time()
    t0 = time.monotonic()
    stdout = ""
    stderr = ""

    try:
        proc = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_segundos if timeout_segundos and timeout_segundos > 0 else None,
            shell=False,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        code = 0 if proc.returncode == 0 else (2 if proc.returncode == 2 else 3)
        if code == 0:
            message = "Auditoria concluída."
        elif code == 2:
            message = "Auditoria concluída com pontos de atenção."
        else:
            message = "Auditoria concluída com erro ou resultado incompleto."
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        code = 3
        message = "O tempo limite da integração foi excedido. Consulte os logs e checkpoints antes de repetir."

    finished = datetime.now().astimezone()
    novos_relatorios = _novos_arquivos(ROOT / "relatorios", started_epoch)
    novos_logs = _novos_arquivos(ROOT / "logs", started_epoch)
    codigo_tecnico, codigo_gerencial = (
        _codigos_consolidado(novos_relatorios, code)
        if operacao == "rotina-semanal"
        else (code, code)
    )
    return IntegrationResult(
        schema_version=SCHEMA_VERSION,
        versao=VERSION,
        operacao=operacao,
        empresa=empresa,
        iniciado_em=started.isoformat(timespec="seconds"),
        concluido_em=finished.isoformat(timespec="seconds"),
        duracao_segundos=round(time.monotonic() - t0, 3),
        codigo_saida=code,
        resultado=_classificar_codigo(code),
        relatorios=novos_relatorios,
        logs=novos_logs,
        mensagem=message,
        saida_resumida=_resumir_saida(stdout, stderr),
        codigo_tecnico=codigo_tecnico,
        codigo_gerencial=codigo_gerencial,
        resultado_gerencial=_classificar_codigo(codigo_gerencial),
    )
