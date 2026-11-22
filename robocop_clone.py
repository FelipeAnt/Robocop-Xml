"""
robocop_clone.py (v7.1 - Cloudflare R2, Resiliência e Integridade Fiscal)
-----------------------------------------------------------------------
Monitora N pastas locais (NFC-e, NF-e, ou qualquer outra fonte de XML fiscal),
cada uma com sua própria data de início, identifica CNPJ / ano / mês pelo
conteúdo do XML (NF-e/NFC-e autorizadas, cancelamentos e inutilizações homologados).

Recursos de Resiliência:
- Memória local em SQLite com cache de stat (file_stat_cache) para varreduras rápidas.
- Verificação robusta de integridade de arquivo antes de processar (stability).
- Rotação de logs com RotatingFileHandler (10MB, 3 backups).
- Retry com backoff exponencial para falhas temporárias de rede.
- Varredura periódica de reconciliação (daemon) a cada 30 minutos.
- Heartbeat periódico (_status/<cliente>.json no R2) para monitorar PCs remotamente.
- Watchdog capturando eventos de criação, renomeação e modificação (on_created, on_moved, on_modified).
"""

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import re
import shutil
import socket
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import db_manager
from db_manager import (
    compute_file_hash,
    check_stat_cache,
    update_stat_cache,
    is_ignored,
    register_ignored,
    MOTIVO_DATA,
    MOTIVO_NAO_RECONHECIDO,
)
import fiscal_parser
import segredo
from fiscal_parser import extract_fiscal_info, extract_documento, FiscalDocInfo
import stability
from stability import esperar_arquivo_estavel

# ------------------------------------------------------------------
# CONFIGURAÇÃO (carregada de config.json em tempo de execução)
# ------------------------------------------------------------------

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = str(BASE_DIR / "config.json")
DB_PATH = str(BASE_DIR / "controle_upload.sqlite3")
LOG_FILE = str(BASE_DIR / "robocop_clone.log")
QUEUE_MAXSIZE = 50000
# Limites de retry independentes: instabilidade do arquivo (ERP ainda gravando)
# e falha de rede/R2 são problemas diferentes e não devem dividir a mesma conta.
MAX_TENTATIVAS_ESTABILIDADE = 5
MAX_TENTATIVAS_REDE = 5
VERSAO = "7.1"

# Preenchidos em main() a partir do config.json
R2_ACCOUNT_ID = ""
R2_ACCESS_KEY_ID = ""
R2_SECRET_ACCESS_KEY = ""
R2_BUCKET = ""
NUM_WORKERS = 4
NOME_CLIENTE = ""
EXIGIR_PROTOCOLO = True  # só envia nota com protocolo cStat 100/150 (config: exigir_protocolo)
HEARTBEAT_MIN = 15  # 0 desliga o heartbeat
FONTES = []  # lista de dicts {"pasta": ..., "data_inicio": datetime, "nome": ..., "tipo": ...}

NFE_NS = {"nfe": "http://www.portalfiscal.inf.br/nfe"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("robocop_clone")


def migrar_segredo(cfg: dict, secret_puro: str):
    """Instalações antigas guardam o secret em texto puro: criptografa e regrava
    o config.json de forma atômica. Qualquer falha é ignorada (o robô continua
    funcionando com o config atual)."""
    try:
        protegido = segredo.proteger(secret_puro)
        if segredo.revelar(protegido) != secret_puro:
            raise RuntimeError("verificação da criptografia falhou")
        novo = dict(cfg)
        novo["r2_secret_access_key"] = protegido
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(novo, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)
        log.info("Secret do R2 no config.json agora está criptografado (DPAPI, só abre neste PC).")
    except Exception as e:
        log.warning(f"Não consegui criptografar o secret no config.json (segue em texto puro): {e}")


class ConfigError(Exception):
    """Config.json ausente, ilegível ou com valores inválidos. Carrega a lista
    de problemas encontrados (todos de uma vez, para corrigir de uma só vez)."""

    def __init__(self, problemas):
        self.problemas = [problemas] if isinstance(problemas, str) else list(problemas)
        super().__init__("; ".join(self.problemas))


ESPERA_ERRO_FATAL_SERVICO = 300  # s: evita loop de reinício do NSSM a cada 5s


def falha_fatal(titulo: str, problemas):
    """Registra o erro de configuração e encerra. Como serviço (sem terminal),
    espera antes de sair: o NSSM reinicia o processo em 5s, e sem a pausa o log
    encheria de repetições do mesmo erro."""
    log.error(f"=== {titulo} ===")
    for p in ([problemas] if isinstance(problemas, str) else problemas):
        log.error(f"  - {p}")
    log.error("Corrija com o configurador.py (ou editando o config.json) e reinicie o serviço.")
    interativo = bool(sys.stdin) and sys.stdin.isatty()
    if not interativo:
        log.error(f"Aguardando {ESPERA_ERRO_FATAL_SERVICO}s antes de encerrar (evita reinício em loop).")
        time.sleep(ESPERA_ERRO_FATAL_SERVICO)
    sys.exit(1)


def _ler_config_json(cfg_path: Path) -> dict:
    if not cfg_path.exists():
        raise ConfigError(
            f"'{cfg_path}' não encontrado. Rode o configurador.py primeiro para criar a "
            f"configuração (pastas monitoradas, credenciais do R2, etc)."
        )
    try:
        # utf-8-sig: tolera o BOM que o Bloco de Notas do Windows grava
        with open(cfg_path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"config.json com erro de sintaxe na linha {e.lineno}, coluna {e.colno}: {e.msg}. "
            f"Se editou à mão, confira vírgulas/aspas; ou refaça com o configurador.py."
        )
    except UnicodeDecodeError:
        raise ConfigError("config.json não está em UTF-8 (arquivo corrompido?). Refaça com o configurador.py.")
    except OSError as e:
        raise ConfigError(f"Não consegui ler o config.json: {e}")
    if not isinstance(cfg, dict):
        raise ConfigError("config.json inválido: o conteúdo deveria ser um objeto JSON ({ ... }).")
    return cfg


def _texto(cfg: dict, campo: str) -> str:
    v = cfg.get(campo, "")
    return v.strip() if isinstance(v, str) else ""


def carregar_config():
    """Lê e valida o config.json. Em caso de problema levanta ConfigError com
    TODOS os problemas encontrados (não só o primeiro)."""
    global R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, NUM_WORKERS, FONTES
    global NOME_CLIENTE, HEARTBEAT_MIN, EXIGIR_PROTOCOLO

    cfg = _ler_config_json(Path(CONFIG_FILE))
    problemas = []

    # ---- fontes ----
    fontes_cfg = cfg.get("fontes")
    fontes = []
    if not isinstance(fontes_cfg, list) or not fontes_cfg:
        problemas.append("Nenhuma pasta configurada em 'fontes'. Use o configurador.py.")
    else:
        vistas = set()
        for i, f_cfg in enumerate(fontes_cfg, 1):
            if not isinstance(f_cfg, dict) or not isinstance(f_cfg.get("pasta"), str) or not f_cfg["pasta"].strip():
                problemas.append(f"Fonte #{i}: campo 'pasta' ausente ou inválido.")
                continue
            pasta = Path(f_cfg["pasta"].strip())
            try:
                data_inicio = datetime.strptime(str(f_cfg.get("data_inicio", "")), "%Y-%m-%d")
            except ValueError:
                problemas.append(f"Fonte '{pasta}': data de início inválida (use AAAA-MM-DD).")
                continue
            tipo = f_cfg.get("tipo", "saida")
            if tipo not in ("saida", "entrada"):
                log.warning(f"Tipo inválido '{tipo}' para '{pasta}', usando 'saida'.")
                tipo = "saida"
            chave = os.path.normcase(os.path.normpath(str(pasta)))
            if chave in vistas:
                log.warning(f"Pasta repetida no config.json, ignorando a duplicata: {pasta}")
                continue
            vistas.add(chave)
            if not pasta.is_dir():
                # Pode ser um drive de rede/pen drive ainda não montado: não impede
                # as outras pastas. O heartbeat mostra 'existe: False'.
                log.error(f"Pasta configurada não existe, pulando: {pasta}")
                continue
            nome = f_cfg.get("nome")
            fontes.append({
                "pasta": pasta,
                "data_inicio": data_inicio,
                "nome": nome.strip() if isinstance(nome, str) and nome.strip() else (pasta.name or str(pasta)),
                "tipo": tipo,
            })
        if not fontes and not problemas:
            problemas.append("Nenhuma das pastas configuradas existe neste PC. Confira os caminhos no configurador.py.")

    # ---- R2 ----
    conta = _texto(cfg, "r2_account_id")
    chave_acesso = _texto(cfg, "r2_access_key_id")
    secret_cfg = _texto(cfg, "r2_secret_access_key")
    bucket = _texto(cfg, "r2_bucket")
    for campo, valor in (("r2_account_id", conta), ("r2_access_key_id", chave_acesso),
                         ("r2_secret_access_key", secret_cfg), ("r2_bucket", bucket)):
        if not valor:
            problemas.append(f"Campo obrigatório ausente/vazio: {campo}.")
    if conta and not re.fullmatch(r"[0-9a-fA-F]{32}", conta):
        problemas.append("r2_account_id deve ter 32 caracteres hexadecimais (copie só o Account ID, sem https:// nem .r2.cloudflarestorage.com).")
    if bucket and not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        problemas.append(f"Nome de bucket inválido: '{bucket}' (minúsculas, números, hífen; 3 a 63 caracteres).")

    secret_puro = ""
    if secret_cfg:
        try:
            secret_puro = segredo.revelar(secret_cfg)
        except segredo.SegredoIndisponivel as e:
            problemas.append(
                "Não consegui abrir o Secret Access Key. Isso acontece quando o config.json foi COPIADO "
                "de outro computador (o segredo só abre na máquina onde foi salvo) ou está corrompido. "
                f"Rode o configurador.py neste PC e digite o Secret de novo. Detalhe: {e}"
            )

    # ---- opções numéricas / booleanas (valor ruim = padrão + aviso, não derruba) ----
    try:
        workers = int(cfg.get("num_workers", 4))
    except (TypeError, ValueError):
        log.warning("num_workers inválido, usando 4.")
        workers = 4
    if not 1 <= workers <= 32:
        log.warning(f"num_workers={workers} fora de 1-32, ajustando.")
        workers = max(1, min(32, workers))

    try:
        heartbeat = int(cfg.get("heartbeat_minutos", 15))
        if heartbeat < 0:
            raise ValueError
    except (TypeError, ValueError):
        log.warning("heartbeat_minutos inválido, usando 15.")
        heartbeat = 15

    exigir = cfg.get("exigir_protocolo", True)
    if not isinstance(exigir, bool):
        log.warning("exigir_protocolo deve ser true ou false; usando true.")
        exigir = True

    if problemas:
        raise ConfigError(problemas)

    R2_ACCOUNT_ID = conta
    R2_ACCESS_KEY_ID = chave_acesso
    R2_SECRET_ACCESS_KEY = secret_puro
    R2_BUCKET = bucket
    NUM_WORKERS = workers
    HEARTBEAT_MIN = heartbeat
    EXIGIR_PROTOCOLO = exigir
    FONTES = fontes

    if segredo.disponivel() and not segredo.esta_protegido(secret_cfg):
        migrar_segredo(cfg, secret_puro)

    NOME_CLIENTE = _texto(cfg, "nome_cliente")
    if not NOME_CLIENTE:
        NOME_CLIENTE = socket.gethostname()
        log.warning(
            f"'nome_cliente' não definido no config.json; usando o nome do PC ({NOME_CLIENTE}) "
            f"no monitoramento. Defina o nome do cliente no configurador.py."
        )


# ------------------------------------------------------------------
# BANCO LOCAL & COMPATIBILIDADE RETROATIVA (delegado a db_manager)
# ------------------------------------------------------------------

def init_db(db_path: Optional[str] = None):
    target = db_path or DB_PATH
    db_manager.init_db(target)


def is_hash_uploaded(*args, **kwargs) -> bool:
    if len(args) == 1 and isinstance(args[0], str):
        return db_manager.is_hash_uploaded(DB_PATH, args[0])
    return db_manager.is_hash_uploaded(*args, **kwargs)


already_uploaded = is_hash_uploaded
file_hash = compute_file_hash


def register_upload(*args, **kwargs):
    if len(args) == 6:
        return db_manager.register_upload(DB_PATH, *args, **kwargs)
    return db_manager.register_upload(*args, **kwargs)


def get_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    target = db_path or DB_PATH
    return db_manager.get_conn(target)


def close_conn(db_path: Optional[str] = None):
    target = db_path or DB_PATH
    db_manager.close_conn(target)


def extract_nfce_info(xml_path: Path, usar_destinatario: bool = False):
    info = extract_fiscal_info(xml_path, usar_destinatario=usar_destinatario)
    if info is None:
        return None
    return info.cnpj, info.ano, info.mes, info.data_emissao


# ------------------------------------------------------------------
# CLOUDFLARE R2 (compatível com S3)
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


def upload_xml(client, local_path: Path, cnpj: str, ano: str, mes: str, tipo: str,
               nome_objeto: Optional[str] = None) -> str:
    object_key = f"{ano}/{mes}/{cnpj}/{tipo}/{nome_objeto or local_path.name}"
    client.upload_file(
        str(local_path),
        R2_BUCKET,
        object_key,
        ExtraArgs={"ContentType": "text/xml"},
    )
    return object_key


# ------------------------------------------------------------------
# MONITORAMENTO (HEARTBEAT) — o robô avisa periodicamente que está vivo
# ------------------------------------------------------------------

class Estatisticas:
    """Contadores thread-safe usados no heartbeat."""

    def __init__(self):
        self._lock = threading.Lock()
        self.iniciado_em = datetime.now()
        self.enviados = 0
        self.ultimo_envio_em = None
        self.erros_consecutivos = 0
        self.ultimo_erro = None
        self.ultimo_erro_em = None

    def registrar_envio(self):
        with self._lock:
            self.enviados += 1
            self.ultimo_envio_em = datetime.now()
            self.erros_consecutivos = 0

    def registrar_erro(self, msg: str):
        with self._lock:
            self.erros_consecutivos += 1
            self.ultimo_erro = msg[:300]
            self.ultimo_erro_em = datetime.now()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "iniciado_em": self.iniciado_em,
                "enviados_sessao": self.enviados,
                "ultimo_envio_em": self.ultimo_envio_em,
                "erros_consecutivos": self.erros_consecutivos,
                "ultimo_erro": self.ultimo_erro,
                "ultimo_erro_em": self.ultimo_erro_em,
            }


stats = Estatisticas()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


def _slug(texto: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", texto).strip("_") or "sem_nome"


def chave_heartbeat() -> str:
    host = socket.gethostname()
    nome = _slug(NOME_CLIENTE)
    base = nome if _slug(host).lower() == nome.lower() else f"{nome}__{_slug(host)}"
    return f"_status/{base}.json"


def montar_heartbeat() -> dict:
    snap = stats.snapshot()
    agora = datetime.now()
    workers_vivos = sum(
        1 for t in threading.enumerate() if t.name.startswith("worker-") and t.is_alive()
    )
    try:
        total_db = db_manager.contar_uploads(DB_PATH)
    except Exception:
        total_db = None
    try:
        disco_livre_mb = shutil.disk_usage(str(BASE_DIR)).free // (1024 * 1024)
    except Exception:
        disco_livre_mb = None

    return {
        "cliente": NOME_CLIENTE,
        "host": socket.gethostname(),
        "versao": VERSAO,
        "agora": _iso(agora),
        "iniciado_em": _iso(snap["iniciado_em"]),
        "intervalo_s": HEARTBEAT_MIN * 60,
        "fila_pendente": fila_arquivos.qsize(),
        "workers_esperados": NUM_WORKERS,
        "workers_vivos": workers_vivos,
        "enviados_sessao": snap["enviados_sessao"],
        "total_enviados_db": total_db,
        "ultimo_envio_em": _iso(snap["ultimo_envio_em"]),
        "erros_consecutivos": snap["erros_consecutivos"],
        "ultimo_erro": snap["ultimo_erro"],
        "ultimo_erro_em": _iso(snap["ultimo_erro_em"]),
        "disco_livre_mb": disco_livre_mb,
        "fontes": [
            {"nome": f["nome"], "pasta": str(f["pasta"]), "tipo": f["tipo"],
             "existe": Path(f["pasta"]).exists()}
            for f in FONTES
        ],
    }


def enviar_heartbeat(client):
    corpo = json.dumps(montar_heartbeat(), ensure_ascii=False).encode("utf-8")
    client.put_object(
        Bucket=R2_BUCKET,
        Key=chave_heartbeat(),
        Body=corpo,
        ContentType="application/json",
    )


def heartbeat_loop(client, stop_event: Optional[threading.Event] = None):
    intervalo = HEARTBEAT_MIN * 60
    log.info(f"Heartbeat iniciado (a cada {HEARTBEAT_MIN} min) -> {chave_heartbeat()}")
    falhando = False
    while True:
        try:
            enviar_heartbeat(client)
            if falhando:
                log.info("Heartbeat voltou a funcionar.")
            falhando = False
        except Exception as e:
            # Loga só na primeira falha da sequência para não poluir o log.
            if not falhando:
                log.warning(f"Falha ao enviar heartbeat (seguirá tentando): {e}")
            falhando = True
        if stop_event is not None:
            if stop_event.wait(intervalo):
                break
        else:
            time.sleep(intervalo)


# ------------------------------------------------------------------
# FILA DE PROCESSAMENTO COM RETRY & ESTABILIDADE
# ------------------------------------------------------------------

@dataclass
class ItemFila:
    path: Path
    data_inicio: datetime
    fonte_nome: str
    tipo: str = "saida"
    tentativas_estabilidade: int = 0
    tentativas_rede: int = 0


fila_arquivos: "queue.Queue[ItemFila]" = queue.Queue(maxsize=QUEUE_MAXSIZE)


def agendar_retry(item: "ItemFila", delay: float):
    """Agenda o reenfileiramento do item após 'delay' segundos, sem bloquear
    o worker nem segurar um get() em aberto — evita corromper a contagem
    interna de tarefas pendentes da Queue."""
    threading.Timer(delay, fila_arquivos.put, args=(item,)).start()


def reagendar(item: "ItemFila", campo: str, limite: int, motivo: str) -> bool:
    """Incrementa o contador 'campo' do item e agenda novo retry com backoff
    exponencial (teto de 60s). Retorna False quando o limite já foi atingido."""
    n = getattr(item, campo)
    if n >= limite:
        return False
    n += 1
    setattr(item, campo, n)
    delay = min(60, 2 ** n)
    log.warning(
        f"[{item.fonte_nome}] [Tentativa {n}/{limite}] {motivo}: {item.path.name}. "
        f"Reagendando em {delay}s..."
    )
    agendar_retry(item, delay)
    return True


def worker_loop(client):
    while True:
        item = fila_arquivos.get()
        try:
            processar_arquivo(item, client)
        except Exception as e:
            log.error(f"Erro inesperado processando {item.path}: {e}", exc_info=True)
        finally:
            fila_arquivos.task_done()


def processar_arquivo(item: ItemFila, client):
    path = item.path
    if not path.exists() or path.suffix.lower() != ".xml":
        return

    # 1. Fast-check no cache de stat (evita ler disco e calcular hash se já enviado)
    is_up, cached_hash = check_stat_cache(DB_PATH, path)
    if is_up:
        return

    # 1b. Já foi descartado antes (fora da data de início ou XML não reconhecido)?
    if is_ignored(DB_PATH, path, item.data_inicio):
        return

    # 2. Verificação de estabilidade do arquivo
    if not esperar_arquivo_estavel(path, tentativas=3, intervalo=0.5):
        if not reagendar(item, "tentativas_estabilidade", MAX_TENTATIVAS_ESTABILIDADE,
                         "Arquivo instável ou em gravação"):
            log.error(
                f"[{item.fonte_nome}] Arquivo instável após {MAX_TENTATIVAS_ESTABILIDADE} tentativas: "
                f"{path.name}. O arquivo será recuperado na próxima varredura periódica."
            )
        return

    # 3. Hash do arquivo (reutiliza cache se disponível)
    h = cached_hash if cached_hash else compute_file_hash(path)

    # 4. Verifica se hash já foi enviado anteriormente
    if is_hash_uploaded(DB_PATH, h):
        update_stat_cache(DB_PATH, path, h)
        return

    # 5. Extração polimórfica das informações fiscais
    # Nota autorizada, cancelamento ou inutilização homologados. Eventos só em
    # pastas de saída (o evento traz o CNPJ do emitente). O motivo de uma
    # rejeição (sem protocolo, cStat, sem homologação) é logado pelo parser.
    info = extract_documento(
        path,
        usar_destinatario=(item.tipo == "entrada"),
        exigir_protocolo=EXIGIR_PROTOCOLO,
        aceitar_eventos=(item.tipo == "saida"),
    )
    if info is None:
        log.info(f"[{item.fonte_nome}] Ignorado (não é nota autorizada nem evento aceito): {path.name}")
        register_ignored(DB_PATH, path, MOTIVO_NAO_RECONHECIDO)
        return

    # 6. Filtro por data de corte
    if info.data_emissao < item.data_inicio:
        register_ignored(DB_PATH, path, MOTIVO_DATA, info.data_emissao)
        return

    # 7. Upload para o Cloudflare R2 com retry exponencial em caso de falha de rede
    try:
        if info.pasta:
            # Eventos (cancelamento/inutilização): pasta própria e nome determinístico
            object_key = upload_xml(client, path, info.cnpj, info.ano, info.mes,
                                    info.pasta, info.nome_objeto)
        else:
            object_key = upload_xml(client, path, info.cnpj, info.ano, info.mes, item.tipo)
        register_upload(DB_PATH, h, path.name, info.cnpj, info.ano, info.mes, object_key)
        update_stat_cache(DB_PATH, path, h)
        stats.registrar_envio()
        log.info(f"[{item.fonte_nome}] Enviado: {path.name} -> {object_key}")
    except Exception as e:
        stats.registrar_erro(f"{path.name}: {e}")
        if not reagendar(item, "tentativas_rede", MAX_TENTATIVAS_REDE, f"Falha de rede ({e})"):
            log.error(
                f"[{item.fonte_nome}] Falha persistente de rede para {path.name} após "
                f"{MAX_TENTATIVAS_REDE} tentativas: {e}. O arquivo será recuperado na próxima varredura periódica."
            )


# ------------------------------------------------------------------
# WATCHDOG — Captura created, moved e modified por pasta monitorada
# ------------------------------------------------------------------

class FonteXMLHandler(FileSystemEventHandler):
    def __init__(self, data_inicio: datetime, fonte_nome: str, tipo: str):
        self.data_inicio = data_inicio
        self.fonte_nome = fonte_nome
        self.tipo = tipo

    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(".xml"):
            fila_arquivos.put(ItemFila(Path(event.src_path), self.data_inicio, self.fonte_nome, self.tipo))

    def on_moved(self, event):
        if not event.is_directory and event.dest_path.lower().endswith(".xml"):
            fila_arquivos.put(ItemFila(Path(event.dest_path), self.data_inicio, self.fonte_nome, self.tipo))

    def on_modified(self, event):
        if not event.is_directory and event.src_path.lower().endswith(".xml"):
            fila_arquivos.put(ItemFila(Path(event.src_path), self.data_inicio, self.fonte_nome, self.tipo))


# ------------------------------------------------------------------
# VARREDURA INICIAL & RECONCILIAÇÃO PERIÓDICA
# ------------------------------------------------------------------

def varredura_reconciliacao(fontes=None, db_path=None) -> int:
    target_fontes = FONTES if fontes is None else fontes
    target_db = DB_PATH if db_path is None else db_path
    total = 0
    for fonte in target_fontes:
        pasta = Path(fonte["pasta"])
        if not pasta.exists():
            continue
        for xml_path in pasta.rglob("*.xml"):
            is_up, _ = check_stat_cache(target_db, xml_path)
            if is_up:
                continue
            if is_ignored(target_db, xml_path, fonte["data_inicio"]):
                continue
            fila_arquivos.put(ItemFila(xml_path, fonte["data_inicio"], fonte["nome"], fonte.get("tipo", "saida")))
            total += 1
    return total


def varredura_inicial():
    log.info("Fazendo varredura inicial das pastas configuradas...")
    total = varredura_reconciliacao()
    log.info(f"{total} arquivos enfileirados na varredura inicial.")


def reconciliation_loop(intervalo: int = 1800, stop_event: Optional[threading.Event] = None):
    log.info(f"Loop de reconciliação periódico iniciado (intervalo={intervalo}s).")
    while stop_event is None or not stop_event.is_set():
        if stop_event:
            if stop_event.wait(intervalo):
                break
        else:
            time.sleep(intervalo)
        try:
            total = varredura_reconciliacao()
            if total > 0:
                log.info(f"[Reconciliação] {total} arquivo(s) não sincronizado(s) reenfileirado(s).")
        except Exception as e:
            log.error(f"[Reconciliação] Erro durante varredura: {e}")


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

# Erros do R2 que não se resolvem sozinhos: credencial/bucket errados.
CODIGOS_R2_FATAIS = {"403", "401", "404", "AccessDenied", "InvalidAccessKeyId",
                     "SignatureDoesNotMatch", "NoSuchBucket", "NoSuchKey", "InvalidBucketName"}


def conectar_r2():
    """Cria o client e valida o bucket. Credencial/bucket errados são fatais
    (config a corrigir); rede fora do ar/DNS/relógio é transitório: tenta de
    novo com espera crescente, pois o PC pode ter acabado de ligar."""
    espera = 5
    while True:
        try:
            client = build_r2_client()
            client.head_bucket(Bucket=R2_BUCKET)
            log.info(f"Conectado ao bucket '{R2_BUCKET}' com sucesso.")
            return client
        except ClientError as e:
            err = e.response.get("Error", {}) if hasattr(e, "response") else {}
            codigo = str(err.get("Code", ""))
            if codigo in CODIGOS_R2_FATAIS:
                falha_fatal(
                    "R2 recusou o acesso",
                    [f"Código {codigo} ao acessar o bucket '{R2_BUCKET}'.",
                     "Confira r2_access_key_id, r2_secret_access_key, r2_account_id e r2_bucket.",
                     "Se o token foi revogado/expirou, gere outro no Cloudflare e refaça no configurador.py."])
            log.warning(f"R2 respondeu com erro temporário ({codigo or e}); nova tentativa em {espera}s.")
        except Exception as e:
            log.warning(f"Sem conexão com o R2 ({type(e).__name__}: {e}); nova tentativa em {espera}s.")
        time.sleep(espera)
        espera = min(60, espera * 2)


def main():
    try:
        carregar_config()
    except ConfigError as e:
        falha_fatal("Erro de configuração", e.problemas)
    init_db(DB_PATH)

    log.info("Conectando ao Cloudflare R2...")
    client = conectar_r2()

    for i in range(NUM_WORKERS):
        t = threading.Thread(target=worker_loop, args=(client,), name=f"worker-{i}", daemon=True)
        t.start()

    if HEARTBEAT_MIN > 0:
        t_hb = threading.Thread(target=heartbeat_loop, args=(client,), name="heartbeat", daemon=True)
        t_hb.start()

    varredura_inicial()

    t_rec = threading.Thread(target=reconciliation_loop, name="reconciliation", daemon=True)
    t_rec.start()

    observer = Observer()
    for fonte in FONTES:
        handler = FonteXMLHandler(fonte["data_inicio"], fonte["nome"], fonte["tipo"])
        observer.schedule(handler, str(fonte["pasta"]), recursive=True)
        tipo_label = "entrada (organiza por destinatário)" if fonte["tipo"] == "entrada" else "saída (organiza por emitente)"
        log.info(f"Monitorando [{fonte['nome']}] {fonte['pasta']} "
                 f"(desde {fonte['data_inicio'].strftime('%d/%m/%Y')}, tipo: {tipo_label})")

    observer.start()
    log.info(f"Robocop v{VERSAO} [{NOME_CLIENTE}] rodando com {NUM_WORKERS} workers, {len(FONTES)} pasta(s) monitorada(s).")

    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
