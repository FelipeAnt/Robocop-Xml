import time
from pathlib import Path
from typing import Union


def esperar_arquivo_estavel(
    path: Union[Path, str],
    tentativas: int = 3,
    intervalo: float = 0.5,
) -> bool:
    """
    Retorna True quando o arquivo está pronto para ser lido: existe, não está
    vazio, abre em leitura (sem lock exclusivo do ERP) e mantém tamanho E data
    de modificação idênticos em duas leituras consecutivas.

    Problemas transitórios (arquivo travado pelo ERP/antivírus, ainda vazio) não
    encerram a verificação: gastam uma tentativa e a checagem segue. Só devolve
    False de imediato se o arquivo sumiu ou não é um arquivo.

    Retorna False se esgotar as tentativas (o chamador reagenda com backoff).
    """
    path = Path(path)
    anterior = None  # (tamanho, mtime_ns) da última leitura boa

    for i in range(tentativas):
        atual = None
        try:
            if not path.is_file():
                return False
            # Testa abertura (detecta lock exclusivo do Windows)
            with open(path, "rb") as f:
                f.read(10)
            st = path.stat()
            if st.st_size > 0:
                atual = (st.st_size, st.st_mtime_ns)
        except FileNotFoundError:
            return False  # sumiu (renomeado/removido): não adianta esperar
        except OSError:
            pass  # PermissionError e afins: lock transitório, tenta de novo

        if atual is not None and atual == anterior:
            return True
        anterior = atual  # None (lock/vazio) zera a comparação

        if i < tentativas - 1:  # não dorme depois da última tentativa
            time.sleep(intervalo)

    return False
