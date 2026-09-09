"""Catálogo central de status para interface humana e integração."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class StatusInfo:
    codigo: str
    rotulo: str
    nivel: str
    descricao: str
    acao_recomendada: str = ""


_STATUS: dict[str, StatusInfo] = {}


def _add(codigo: str, rotulo: str, nivel: str, descricao: str, acao: str = "") -> None:
    _STATUS[codigo] = StatusInfo(codigo, rotulo, nivel, descricao, acao)


_add("OK", "Sem pendências", "ok", "Verificação concluída sem indício de problema.")
_add("ATIVIDADE_CONFIRMADA", "Atividade confirmada", "ok", "Foram detectadas criações ou alterações compatíveis com atividade de backup.")
_add("BACKUP_CONFIRMADO", "Backup confirmado", "ok", "Foi localizado backup válido dentro do comportamento esperado.")
_add("ATENCAO", "Atenção", "atencao", "Há uma condição que deve ser conferida pelo técnico.")
_add("ERRO", "Erro de auditoria", "erro", "A auditoria encontrou uma falha que impede uma conclusão confiável.")
_add("INFORMATIVO", "Informativo", "informativo", "Condição informativa que não representa falha.")
_add("INCOMPLETO", "Execução incompleta", "erro", "A consulta terminou antes de processar todo o escopo.", "Consultar o log e repetir a auditoria após corrigir a causa.")
_add("NAO_EXISTE", "Pasta não encontrada", "erro", "A pasta esperada não foi localizada no Dropbox.", "Confirmar estrutura, nome da empresa e escopo configurado.")
_add("CURSOR_INVALIDO", "Referência incremental inválida", "erro", "A referência usada para continuar a leitura incremental não é mais válida.", "Recriar a referência somente após validar o escopo.")
_add("HEARTBEAT_ERRO", "Falha ao verificar marcador de atividade", "erro", "O marcador de atividade não pôde ser lido ou validado.")
_add("SEM_BACKUP", "Sem backup válido", "erro", "Nenhum backup válido foi encontrado para a VM.", "Verificar a rotina de backup e o envio ao Dropbox.")
_add("SEM_PASTA_VMS", "Pasta de VMs não encontrada", "erro", "A árvore esperada de backups de VMs não foi localizada.", "Confirmar se a empresa possui VMs no escopo e revisar a estrutura de pastas.")
_add("VM_NAO_ENCONTRADA", "VM não encontrada", "erro", "Uma VM conhecida no histórico não foi encontrada no inventário atual.", "Confirmar remoção, renomeação ou falha de envio.")
_add("ERRO_EVENTOS_VMS", "Falha ao consultar histórico da VM", "erro", "Os eventos usados para avaliar a VM não puderam ser consultados.")
_add("SEM_MUDANCAS", "Sem alterações no período", "informativo", "Não foram detectadas alterações de arquivos desde a referência anterior.", "Confirmar se era esperado haver atividade no período.")
_add("SOMENTE_EXCLUSOES", "Somente exclusões no período", "informativo", "Foram observadas exclusões, sem criação ou alteração de arquivos.", "Confirmar se a atividade corresponde ao comportamento esperado.")
_add("CURSOR_RECRIADO", "Referência incremental recriada", "informativo", "A referência incremental foi recriada e a comparação anterior foi interrompida.")
_add("ATIVIDADE_ESPERADA_AUSENTE", "Atividade esperada não observada", "atencao", "A política da empresa exige atividade no período, mas nenhuma criação/alteração foi observada.", "Confirmar a execução do job e a origem dos dados.")
_add("HEARTBEAT_ATRASADO", "Marcador de atividade atrasado", "atencao", "O marcador de atividade está mais antigo que a janela permitida.")
_add("HEARTBEAT_AUSENTE", "Marcador de atividade ausente", "atencao", "O marcador de atividade configurado não foi localizado.")
_add("ATRASADO", "Backup atrasado", "atencao", "O backup ultrapassou a previsão e a tolerância configuradas.", "Verificar a rotina da VM e o último upload.")
_add("IRREGULAR", "Periodicidade irregular", "informativo", "O histórico não apresenta um intervalo consistente entre backups.", "Revisar a política da VM ou coletar mais histórico.")
_add("EVENTOS_VMS_INCOMPLETOS", "Histórico de VM incompleto", "informativo", "A leitura do histórico da VM foi interrompida antes do fim.")
_add("BASELINE_NAO_CRIADA", "Referência inicial não criada", "atencao", "Não foi possível criar uma referência inicial segura para a próxima comparação.")
_add("SEM_UPLOAD_RECENTE", "Sem upload recente", "atencao", "Ainda há pouco histórico para definir a periodicidade e o último upload é antigo demais.", "Confirmar se a rotina continua ativa.")
_add("VAZIA", "Pasta vazia", "atencao", "A pasta esperada existe, porém não contém item válido.")
_add("AMOSTRAGEM_INCONCLUSIVA", "Amostragem inconclusiva", "atencao", "A amostragem limitada não encontrou evidência suficiente para concluir a atividade.")
_add("BASELINE_CRIADA", "Referência inicial criada", "informativo", "Foi criada a referência inicial; a próxima execução poderá comparar somente eventos novos.")
_add("EM_APRENDIZADO", "Coletando histórico de periodicidade", "informativo", "Ainda não há eventos suficientes para confirmar automaticamente a frequência da VM.")
_add("AMOSTRA_INSUFICIENTE", "Histórico insuficiente", "informativo", "Há poucos backups observados para estimar a periodicidade com segurança.")
_add("MIGRADO_PARA_DRIVE", "Fora do escopo — migrado para Google Drive", "informativo", "Os backups ativos foram migrados para outro destino e não devem gerar alerta no Dropbox.")
_add("NAO_APLICAVEL", "Não aplicável", "informativo", "A verificação não se aplica ao escopo configurado.")
_add("NAO_AUDITADO", "Não auditado", "informativo", "O item está configurado para não ser auditado neste módulo.")
_add("NAO_EXECUTADO", "Não executado", "informativo", "O módulo não foi executado nesta rotina.")
_add("BASELINE_RECRIADA_POLITICA", "Referência recriada por política", "informativo", "A referência foi recriada conforme política para ambiente de alto volume.")
_add("BASELINE_RECRIADA_MANUAL", "Referência recriada manualmente", "informativo", "A referência foi recriada por ação explícita do operador.")
_add("INVENTARIO_BLOQUEADO", "Inventário completo bloqueado", "informativo", "A política da empresa impede inventário completo para evitar uma operação excessivamente pesada.")


def info(codigo: str | None) -> StatusInfo:
    chave = str(codigo or "").strip().upper()
    if chave in _STATUS:
        return _STATUS[chave]
    return StatusInfo(
        chave or "DESCONHECIDO",
        f"Status não catalogado ({chave or 'vazio'})",
        "atencao",
        "O auditor recebeu um código de status que não existe no catálogo da versão atual.",
        "Validar compatibilidade entre o produtor do relatório e o sistema consumidor.",
    )


def rotulo(codigo: str | None) -> str:
    return info(codigo).rotulo


def nivel(codigo: str | None) -> str:
    return info(codigo).nivel


def codigo_saida(codigos: list[str] | tuple[str, ...] | set[str]) -> int:
    """Converte códigos técnicos em código de saída coerente com o catálogo.

    0 = somente OK/informativo; 2 = ao menos uma atenção; 3 = ao menos um erro.
    """
    niveis = {info(codigo).nivel for codigo in codigos if str(codigo or "").strip()}
    if "erro" in niveis:
        return 3
    if "atencao" in niveis:
        return 2
    return 0


def catalogo() -> list[dict[str, str]]:
    return [asdict(item) for item in _STATUS.values()]


def enriquecer_registro(registro: dict[str, object], campo: str = "status") -> dict[str, object]:
    """Acrescenta metadados humanos sem alterar o código técnico do status."""
    resultado = dict(registro)
    detalhe = info(str(registro.get(campo) or ""))
    resultado[f"{campo}_rotulo"] = detalhe.rotulo
    resultado[f"{campo}_nivel"] = detalhe.nivel
    resultado[f"{campo}_descricao"] = detalhe.descricao
    if detalhe.acao_recomendada:
        resultado[f"{campo}_acao_recomendada"] = detalhe.acao_recomendada
    return resultado
