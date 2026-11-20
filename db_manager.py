import hashlib
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

_thread_local = threading.local()
_all_connections = []
_lock = threading.Lock()


def get_conn(db_path: str) -> sqlite3.Connection:
    """Retorna conexão SQLite thread-local configurada com WAL e timeout=60."""
    if not hasattr(_thread_local, "connections"):
        _thread_local.connections = {}

    conn = _thread_local.connections.get(db_path)
    if conn is not None:
        conn_morta = conn
        try:
            conn.execute("SELECT 1")
        except Exception:
            conn = None
            with _lock:
                for item in list(_all_connections):
                    p, c = item
                    if p == db_path and c is conn_morta:
                        try:
                            _all_connections.remove(item)
                        except ValueError:
                            pass

    if conn is None:
        conn = sqlite3.connect(db_path, timeout=60, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _thread_local.connections[db_path] = conn
        with _lock:
            _all_connections.append((db_path, conn))

    return conn


def close_conn(db_path: Optional[str] = None):
    """Fecha conexões abertas para o db_path especificado ou todas se db_path for None."""
    if hasattr(_thread_local, "connections"):
        if db_path is not None:
            conn = _thread_local.connections.pop(db_path, None)
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        else:
            for p, conn in list(_thread_local.connections.items()):
                try:
                    conn.close()
                except Exception:
                    pass
            _thread_local.connections.clear()

    with _lock:
        for item in list(_all_connections):
            p, conn = item
            if db_path is None or p == db_path:
                try:
                    conn.close()
                except Exception:
                    pass
                try:
                    _all_connections.remove(item)
                except ValueError:
                    pass


def init_db(db_path: str):
    """Inicializa tabelas do banco SQLite preservando dados existentes de forma não-destrutiva."""
    conn = sqlite3.connect(db_path, timeout=60, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS uploads (
            file_hash TEXT PRIMARY KEY,
            filename TEXT,
            cnpj TEXT,
            ano TEXT,
            mes TEXT,
            object_key TEXT,
            uploaded_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS file_stat_cache (
            file_path TEXT PRIMARY KEY,
            file_size INTEGER,
            mtime REAL,
            file_hash TEXT,
            last_checked TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stat_hash ON file_stat_cache(file_hash)"
    )
    conn.commit()
    conn.close()


def compute_file_hash(path: Path) -> str:
    """Calcula hash SHA-256 do arquivo em blocos de 64KB."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_hash_uploaded(db_path: str, file_hash: str) -> bool:
    """Verifica se o hash especificado já foi registrado na tabela uploads."""
    conn = get_conn(db_path)
    cur = conn.execute("SELECT 1 FROM uploads WHERE file_hash = ?", (file_hash,))
    return cur.fetchone() is not None


def check_stat_cache(db_path: str, path: Path) -> Tuple[bool, Optional[str]]:
    """Consulta cache de stat de arquivo.
    
    Retorna (is_uploaded, cached_hash) se tamanho e mtime baterem.
    Caso contrário, retorna (False, None).
    """
    try:
        resolved_path = Path(path).resolve()
        st = resolved_path.stat()
        file_size = st.st_size
        mtime = st.st_mtime
        path_str = str(resolved_path)
    except (FileNotFoundError, PermissionError, OSError):
        return False, None

    conn = get_conn(db_path)
    cur = conn.execute(
        "SELECT file_size, mtime, file_hash FROM file_stat_cache WHERE file_path = ?",
        (path_str,),
    )
    row = cur.fetchone()
    if row:
        cached_size, cached_mtime, cached_hash = row
        # Se tamanho e mtime forem idênticos, reutiliza o hash sem ler o disco
        if cached_size == file_size and abs(cached_mtime - mtime) < 0.001:
            return is_hash_uploaded(db_path, cached_hash), cached_hash

    return False, None


def update_stat_cache(db_path: str, path: Path, file_hash: str):
    """Atualiza ou insere metadados de stat e hash na tabela file_stat_cache."""
    try:
        resolved_path = Path(path).resolve()
        st = resolved_path.stat()
        file_size = st.st_size
        mtime = st.st_mtime
        path_str = str(resolved_path)
    except (FileNotFoundError, PermissionError, OSError):
        return

    conn = get_conn(db_path)
    conn.execute(
        """
        INSERT OR REPLACE INTO file_stat_cache (file_path, file_size, mtime, file_hash, last_checked)
        VALUES (?, ?, ?, ?, ?)
        """,
        (path_str, file_size, mtime, file_hash, datetime.now().isoformat()),
    )
    conn.commit()


def register_upload(
    db_path: str,
    file_hash: str,
    filename: str,
    cnpj: str,
    ano: str,
    mes: str,
    object_key: str,
):
    """Registra ou atualiza um upload concluído na tabela uploads."""
    conn = get_conn(db_path)
    conn.execute(
        """
        INSERT OR REPLACE INTO uploads (file_hash, filename, cnpj, ano, mes, object_key, uploaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (file_hash, filename, cnpj, ano, mes, object_key, datetime.now().isoformat()),
    )
    conn.commit()


def contar_uploads(db_path: str) -> int:
    """Total de arquivos já registrados como enviados."""
    conn = get_conn(db_path)
    return conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
