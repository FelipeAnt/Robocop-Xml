import time
from pathlib import Path
from typing import Union


def esperar_arquivo_estavel(
    path: Union[Path, str],
    tentativas: int = 3,
    intervalo: float = 0.5,
) -> bool:
    """
    Verifica se o arquivo existe, tem tamanho > 0, permite abertura em leitura
    (sem locks exclusivos pelo ERP) e mantém tamanho estável.
    Retorna True se estiver 100% pronto. Retorna False caso contrário (para reenfileiramento).
    """
    path = Path(path)
    if not path.exists() or not path.is_file():
        return False

    tamanho_anterior = -1
    for _ in range(tentativas):
        try:
            # Testa abertura e lock exclusivo do Windows
            with open(path, "rb") as f:
                f.read(10)
            tamanho_atual = path.stat().st_size
        except (PermissionError, FileNotFoundError, OSError):
            return False

        if tamanho_atual == 0:
            return False

        if tamanho_atual == tamanho_anterior:
            return True

        tamanho_anterior = tamanho_atual
        time.sleep(intervalo)

    return False
