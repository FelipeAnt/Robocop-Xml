"""
robocop_clone.py (v6 - Cloudflare R2, sem OAuth)
---------------------------------------------------
Monitora N pastas locais (NFC-e, NF-e, ou qualquer outra fonte de XML fiscal),
cada uma com sua própria data de início, identifica CNPJ / ano / mês pelo
conteúdo do XML, evita reenviar arquivo já processado (memória local em
SQLite) e sobe para um bucket do Cloudflare R2, organizando por
CNPJ/ano/mes/arquivo.xml (sem precisar criar "pastas" — R2/S3 não tem esse
conceito, o nome do objeto já inclui o caminho completo).

Diferente da versão com Google Drive, esta NÃO PRECISA de login interativo:
usa uma Access Key + Secret Key estáticas, geradas uma vez no painel do
Cloudflare. Isso elimina token expirando, modo de teste do Google, e a
necessidade de rodar o script manualmente antes de virar serviço.

Dependências:
    pip install watchdog boto3

Uso:
    1. Rode configurador.py primeiro para gerar o config.json (pastas
       monitoradas, credenciais do R2, nome do bucket).
    2. Rode este script manualmente pra validar que está tudo certo.
    3. Depois, instale como serviço do Windows (instalar_servico.bat).
"""

import json
import logging
import queue
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ------------------------------------------------------------------
# CONFIGURAÇÃO (carregada de config.json em tempo de execução)
# ------------------------------------------------------------------

if getattr(sys, "frozen", False):
    # Rodando como .exe empacotado (PyInstaller): a pasta base é onde o
    # executável está, não a pasta temporária de extração.
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = str(BASE_DIR / "config.json")
DB_PATH = str(BASE_DIR / "controle_upload.sqlite3")
LOG_FILE = str(BASE_DIR / "robocop_clone.log")
QUEUE_MAXSIZE = 50000

# Preenchidos em main() a partir do config.json
R2_ACCOUNT_ID = ""
R2_ACCESS_KEY_ID = ""
R2_SECRET_ACCESS_KEY = ""
R2_BUCKET = ""
NUM_WORKERS = 4
FONTES = []  # lista de dicts {"pasta": ..., "data_inicio": datetime, "nome": ...}

NFE_NS = {"nfe": "http://www.portalfiscal.inf.br/nfe"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("robocop_clone")


def carregar_config():
    global R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, NUM_WORKERS, FONTES

    cfg_path = Path(CONFIG_FILE)
    if not cfg_path.exists():
        log.error(
            f"'{CONFIG_FILE}' não encontrado. Rode 'configurador.py' primeiro "
            f"para criar a configuração (pastas monitoradas, credenciais do R2, etc)."
        )
        sys.exit(1)

    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    fontes_cfg = cfg.get("fontes", [])
    if not fontes_cfg:
        log.error("Nenhuma pasta configurada em 'fontes' no config.json. Use o configurador.py.")
        sys.exit(1)

    fontes = []
    for f_cfg in fontes_cfg:
        pasta = Path(f_cfg["pasta"])
        if not pasta.exists():
            log.error(f"Pasta configurada não existe, pulando: {pasta}")
            continue
        try:
            data_inicio = datetime.strptime(f_cfg["data_inicio"], "%Y-%m-%d")
        except (KeyError, ValueError):
            log.error(f"Data de início inválida para '{pasta}', use AAAA-MM-DD. Pulando essa fonte.")
            continue
        tipo = f_cfg.get("tipo", "saida")
        if tipo not in ("saida", "entrada"):
            log.warning(f"Tipo inválido '{tipo}' para '{pasta}', usando 'saida'.")
            tipo = "saida"
        fontes.append({
            "pasta": pasta,
            "data_inicio": data_inicio,
            "nome": f_cfg.get("nome", pasta.name),
            "tipo": tipo,
        })

    if not fontes:
        log.error("Nenhuma fonte válida para monitorar. Verifique o config.json.")
        sys.exit(1)

    for campo in ("r2_account_id", "r2_access_key_id", "r2_secret_access_key", "r2_bucket"):
        if not cfg.get(campo):
            log.error(f"Campo obrigatório ausente no config.json: {campo}. Use o configurador.py.")
            sys.exit(1)

    R2_ACCOUNT_ID = cfg["r2_account_id"]
    R2_ACCESS_KEY_ID = cfg["r2_access_key_id"]
    R2_SECRET_ACCESS_KEY = cfg["r2_secret_access_key"]
    R2_BUCKET = cfg["r2_bucket"]
    NUM_WORKERS = int(cfg.get("num_workers", 4))
    FONTES = fontes


# ------------------------------------------------------------------
# BANCO LOCAL (memória de uploads)
# ------------------------------------------------------------------

_thread_local = threading.local()


def get_conn() -> sqlite3.Connection:
    if not hasattr(_thread_local, "conn"):
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        _thread_local.conn = conn
    return _thread_local.conn


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
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
    conn.commit()
    conn.close()


def already_uploaded(file_hash: str) -> bool:
    conn = get_conn()
    cur = conn.execute("SELECT 1 FROM uploads WHERE file_hash = ?", (file_hash,))
    return cur.fetchone() is not None


def register_upload(file_hash, filename, cnpj, ano, mes, object_key):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO uploads VALUES (?, ?, ?, ?, ?, ?, ?)",
        (file_hash, filename, cnpj, ano, mes, object_key, datetime.now().isoformat()),
    )
    conn.commit()


def file_hash(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------
# PARSER DO XML — funciona igual para NFC-e (modelo 65) e NF-e (modelo 55).
# Para notas de SAÍDA (emitidas pelo cliente), organiza pelo CNPJ do emit.
# Para notas de ENTRADA (compras/bonificação recebidas), organiza pelo CNPJ
# do dest — é o cliente quem recebe, não quem emitiu.
# ------------------------------------------------------------------

def extract_nfce_info(xml_path: Path, usar_destinatario: bool = False):
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        tag_pessoa = "dest" if usar_destinatario else "emit"
        pessoa = root.find(f".//nfe:{tag_pessoa}/nfe:CNPJ", NFE_NS)
        if pessoa is None:
            pessoa = root.find(f".//nfe:{tag_pessoa}/nfe:CPF", NFE_NS)
        dh_emi = root.find(".//nfe:ide/nfe:dhEmi", NFE_NS)
        if dh_emi is None:
            dh_emi = root.find(".//nfe:ide/nfe:dEmi", NFE_NS)

        if pessoa is None or dh_emi is None:
            return None

        cnpj = pessoa.text.strip()
        dh_texto = dh_emi.text.strip()[:10]
        data_emissao = datetime.strptime(dh_texto, "%Y-%m-%d")

        return cnpj, f"{data_emissao.year:04d}", f"{data_emissao.month:02d}", data_emissao
    except Exception as e:
        log.warning(f"Não consegui interpretar {xml_path.name}: {e}")
        return None


# ------------------------------------------------------------------
# CLOUDFLARE R2 (compatível com S3) — sem OAuth, credenciais estáticas
# ------------------------------------------------------------------

def build_r2_client():
    endpoint_url = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=BotoConfig(retries={"max_attempts": 8, "mode": "adaptive"}),
    )


def upload_xml(client, local_path: Path, cnpj: str, ano: str, mes: str, tipo: str) -> str:
    # Sem "criar pasta": o objeto já nasce com o caminho completo como nome.
    # ano/mes/cnpj (não cnpj/ano/mes): assim "baixar o mês passado de todos
    # os clientes" vira um único prefixo (ex: rclone copy r2:bucket/2026/08),
    # em vez de precisar listar CNPJ por CNPJ.
    # .../cnpj/saida/ ou .../cnpj/entrada/: separa visualmente nota própria
    # de nota de compra/bonificação recebida, pra conferência.
    object_key = f"{ano}/{mes}/{cnpj}/{tipo}/{local_path.name}"
    client.upload_file(
        str(local_path),
        R2_BUCKET,
        object_key,
        ExtraArgs={"ContentType": "text/xml"},
    )
    return object_key


# ------------------------------------------------------------------
# FILA DE PROCESSAMENTO
# ------------------------------------------------------------------

@dataclass
class ItemFila:
    path: Path
    data_inicio: datetime
    fonte_nome: str
    tipo: str = "saida"


fila_arquivos: "queue.Queue[ItemFila]" = queue.Queue(maxsize=QUEUE_MAXSIZE)


def esperar_arquivo_estavel(path: Path, tentativas=10, intervalo=0.5):
    tamanho_anterior = -1
    for _ in range(tentativas):
        try:
            tamanho_atual = path.stat().st_size
        except FileNotFoundError:
            return False
        if tamanho_atual == tamanho_anterior and tamanho_atual > 0:
            return True
        tamanho_anterior = tamanho_atual
        time.sleep(intervalo)
    return True


def worker_loop(client):
    while True:
        item = fila_arquivos.get()
        try:
            processar_arquivo(item, client)
        except Exception as e:
            log.error(f"Erro inesperado processando {item.path}: {e}")
        finally:
            fila_arquivos.task_done()


def processar_arquivo(item: ItemFila, client):
    path = item.path
    if path.suffix.lower() != ".xml":
        return
    if not esperar_arquivo_estavel(path):
        log.warning(f"Arquivo sumiu antes de estabilizar: {path}")
        return

    h = file_hash(path)
    if already_uploaded(h):
        return

    info = extract_nfce_info(path, usar_destinatario=(item.tipo == "entrada"))
    if info is None:
        log.info(f"[{item.fonte_nome}] Ignorado (XML não reconhecido): {path.name}")
        return

    cnpj, ano, mes, data_emissao = info
    if data_emissao < item.data_inicio:
        return

    try:
        object_key = upload_xml(client, path, cnpj, ano, mes, item.tipo)
        register_upload(h, path.name, cnpj, ano, mes, object_key)
        log.info(f"[{item.fonte_nome}] Enviado: {path.name} -> {object_key}")
    except ClientError as e:
        log.error(f"[{item.fonte_nome}] Falha ao enviar {path.name}: {e}")
    except Exception as e:
        log.error(f"[{item.fonte_nome}] Falha inesperada ao enviar {path.name}: {e}")


# ------------------------------------------------------------------
# WATCHDOG — um handler por fonte, cada um sabendo sua própria data_inicio
# ------------------------------------------------------------------

class FonteXMLHandler(FileSystemEventHandler):
    def __init__(self, data_inicio: datetime, fonte_nome: str, tipo: str):
        self.data_inicio = data_inicio
        self.fonte_nome = fonte_nome
        self.tipo = tipo

    def on_created(self, event):
        if not event.is_directory:
            fila_arquivos.put(ItemFila(Path(event.src_path), self.data_inicio, self.fonte_nome, self.tipo))

    def on_moved(self, event):
        if not event.is_directory:
            fila_arquivos.put(ItemFila(Path(event.dest_path), self.data_inicio, self.fonte_nome, self.tipo))


def varredura_inicial():
    log.info("Fazendo varredura inicial das pastas configuradas...")
    total = 0
    for fonte in FONTES:
        for xml_path in fonte["pasta"].rglob("*.xml"):
            fila_arquivos.put(ItemFila(xml_path, fonte["data_inicio"], fonte["nome"], fonte["tipo"]))
            total += 1
    log.info(f"{total} arquivos enfileirados na varredura inicial.")


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

def main():
    carregar_config()
    init_db()

    log.info("Conectando ao Cloudflare R2...")
    client = build_r2_client()
    try:
        client.head_bucket(Bucket=R2_BUCKET)
        log.info(f"Conectado ao bucket '{R2_BUCKET}' com sucesso.")
    except ClientError as e:
        log.error(f"Não consegui acessar o bucket '{R2_BUCKET}': {e}")
        log.error("Confira r2_account_id, r2_access_key_id, r2_secret_access_key e r2_bucket no config.json.")
        sys.exit(1)

    for i in range(NUM_WORKERS):
        t = threading.Thread(target=worker_loop, args=(client,), name=f"worker-{i}", daemon=True)
        t.start()

    varredura_inicial()

    observer = Observer()
    for fonte in FONTES:
        handler = FonteXMLHandler(fonte["data_inicio"], fonte["nome"], fonte["tipo"])
        observer.schedule(handler, str(fonte["pasta"]), recursive=True)
        tipo_label = "entrada (organiza por destinatário)" if fonte["tipo"] == "entrada" else "saída (organiza por emitente)"
        log.info(f"Monitorando [{fonte['nome']}] {fonte['pasta']} "
                 f"(desde {fonte['data_inicio'].strftime('%d/%m/%Y')}, tipo: {tipo_label})")

    observer.start()
    log.info(f"Robocop rodando com {NUM_WORKERS} workers, {len(FONTES)} pasta(s) monitorada(s).")

    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
