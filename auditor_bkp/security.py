"""Controles de segurança compartilhados pelo Auditor Dropbox v2.5.

O objetivo deste módulo é reduzir exposição acidental de credenciais e dados
interpretados por ferramentas externas. Ele não substitui as ACLs, o cofre de
segredos ou as políticas de segurança do sistema operacional onde o auditor
for instalado.
"""
from __future__ import annotations

import copy
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping

# Chaves cujo valor deve ser tratado como segredo. A App Key identifica o app,
# mas não é segredo; App Secret, refresh token e access token são confidenciais.
SECRET_KEYS = {
    "refresh_token",
    "oauth2_refresh_token",
    "app_secret",
    "secret",
    "access_token",
    "oauth2_access_token",
    "authorization",
    "password",
    "senha",
    "DROPBOX_REFRESH_TOKEN",
    "DROPBOX_APP_SECRET",
    "DROPBOX_ACCESS_TOKEN",
}
_SECRET_KEYS_LOWER = {key.lower() for key in SECRET_KEYS}

_PATTERNS = [
    re.compile(r"(?i)(DROPBOX_(?:REFRESH_TOKEN|APP_SECRET|ACCESS_TOKEN)\s*[=:]\s*[\"']?)([^\s\"']+)"),
    re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)([^\s]+)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+\-/=]{16,})"),
]

# Excel/LibreOffice podem interpretar células iniciadas por esses caracteres
# como fórmula. Como nomes/caminhos vêm do Dropbox, tratamos isso antes do CSV.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def is_secret_key(key: Any) -> bool:
    return str(key).strip().lower() in _SECRET_KEYS_LOWER


def redact_text(value: Any) -> str:
    """Mascara credenciais conhecidas sem alterar o restante do texto."""
    text = str(value)
    for pattern in _PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    return text


def redact_mapping(value: Any) -> Any:
    """Cria uma cópia de uma estrutura removendo valores de chaves sensíveis."""
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            result[key] = "[REDACTED]" if is_secret_key(key) else redact_mapping(item)
        return result
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_mapping(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return copy.deepcopy(value)


def sanitize_csv_cell(value: Any) -> Any:
    """Neutraliza fórmulas em texto não confiável antes de gravar CSV.

    Valores numéricos permanecem numéricos. Strings que poderiam ser tratadas
    como fórmulas por planilhas recebem um apóstrofo literal no início.
    """
    if not isinstance(value, str):
        return value
    candidate = value.lstrip(" \t\r\n")
    if candidate.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def sanitize_csv_mapping(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): sanitize_csv_cell(value) for key, value in row.items()}


class RedactingFormatter(logging.Formatter):
    """Formatter que remove padrões sensíveis inclusive de exceções."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def secure_file_permissions(path: Path) -> None:
    """Aplica permissão restritiva em POSIX.

    No Windows, o auditor depende da ACL NTFS herdada da pasta de instalação.
    A documentação orienta instalar em diretório acessível apenas à equipe que
    deve operar o serviço.
    """
    try:
        if os.name == "posix" and path.exists():
            path.chmod(0o600)
    except OSError:
        # Não derrubar a auditoria por falha de chmod; registrar/administrar ACL
        # é responsabilidade do ambiente de implantação.
        pass


def is_safe_child(path: Path, root: Path) -> bool:
    """Confirma que o caminho resolve dentro da raiz e não é link simbólico."""
    try:
        if path.is_symlink():
            return False
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False
