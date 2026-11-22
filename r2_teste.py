"""
r2_teste.py
-----------
Teste de conexão com o Cloudflare R2 usado pelo botão "Testar conexão" do
configurador. Só biblioteca padrão (assinatura AWS SigV4 feita à mão), para o
configurador.exe não precisar empacotar o boto3.

Faz três passos e diz exatamente qual falhou:
  1. HEAD no bucket            -> credencial e bucket corretos
  2. PUT de um arquivinho      -> o token tem permissão de ESCRITA
  3. DELETE do mesmo arquivo   -> não deixa lixo no bucket
O arquivo de teste fica em _status/ e é apagado em seguida.
"""

import hashlib
import hmac
import re
import socket
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple
from urllib.parse import quote

REGIAO = "auto"
SERVICO = "s3"


def _hmac(chave: bytes, msg: str) -> bytes:
    return hmac.new(chave, msg.encode("utf-8"), hashlib.sha256).digest()


def assinar(metodo: str, host: str, caminho: str, headers: Dict[str, str], payload_hash: str,
            access_key: str, secret_key: str, amz_date: str,
            regiao: str = REGIAO, servico: str = SERVICO) -> str:
    """Devolve o valor do header Authorization (AWS Signature V4).
    `headers` deve conter todos os headers que serão assinados (minúsculos),
    incluindo host, x-amz-content-sha256 e x-amz-date."""
    data = amz_date[:8]
    nomes = sorted(headers)
    canon_headers = "".join(f"{n}:{headers[n].strip()}\n" for n in nomes)
    assinados = ";".join(nomes)
    requisicao = "\n".join([
        metodo, quote(caminho, safe="/-_.~"), "", canon_headers, assinados, payload_hash,
    ])
    escopo = f"{data}/{regiao}/{servico}/aws4_request"
    a_assinar = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, escopo, hashlib.sha256(requisicao.encode("utf-8")).hexdigest(),
    ])
    k = _hmac(("AWS4" + secret_key).encode("utf-8"), data)
    for parte in (regiao, servico, "aws4_request"):
        k = _hmac(k, parte)
    assinatura = hmac.new(k, a_assinar.encode("utf-8"), hashlib.sha256).hexdigest()
    return (f"AWS4-HMAC-SHA256 Credential={access_key}/{escopo}, "
            f"SignedHeaders={assinados}, Signature={assinatura}")


def _requisicao(metodo: str, account_id: str, bucket: str, chave: Optional[str],
                access_key: str, secret_key: str, corpo: bytes = b"", timeout: float = 15.0
                ) -> Tuple[int, str]:
    """Devolve (status_http, código_de_erro_S3). Levanta URLError/OSError em falha de rede."""
    host = f"{account_id}.r2.cloudflarestorage.com"
    caminho = f"/{bucket}" + (f"/{chave}" if chave else "")
    amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload_hash = hashlib.sha256(corpo).hexdigest()
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    auth = assinar(metodo, host, caminho, headers, payload_hash, access_key, secret_key, amz_date)

    req = urllib.request.Request(
        f"https://{host}{quote(caminho, safe='/-_.~')}", data=corpo if corpo else None, method=metodo,
    )
    for n, v in headers.items():
        if n != "host":
            req.add_header(n, v)
    req.add_header("Authorization", auth)
    if corpo:
        req.add_header("Content-Type", "text/plain")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, ""
    except urllib.error.HTTPError as e:
        try:
            m = re.search(r"<Code>([^<]+)</Code>", e.read().decode("utf-8", "replace"))
        except Exception:
            m = None
        return e.code, (m.group(1) if m else "")


def _explicar(passo: str, status: int, codigo: str) -> str:
    c = codigo or str(status)
    if c == "InvalidAccessKeyId":
        return ("O Access Key ID não existe ou o token foi revogado. Confira o Access Key ID "
                "ou gere um token novo no Cloudflare.")
    if c == "SignatureDoesNotMatch":
        return "O Secret Access Key não confere com o Access Key ID. Digite o Secret de novo (sem espaços)."
    if c == "RequestTimeTooSkewed":
        return "A data/hora deste PC está errada. Acerte o relógio do Windows e teste de novo."
    if c in ("NoSuchBucket", "404") and passo == "bucket":
        return "O bucket não existe nesta conta. Confira o nome do bucket e o Account ID."
    if c == "AccessDenied" or status == 403:
        if passo == "bucket":
            return ("O R2 recusou o acesso (403). Pode ser Access Key ID ou Secret errados, token "
                    "revogado, ou token sem acesso a este bucket. Confira os três e, se preciso, gere um token novo.")
        return ("O token só tem permissão de LEITURA. Crie um token 'Object Read & Write' "
                "restrito a este bucket.")
    return f"Resposta inesperada do R2 (HTTP {status}, código {c or 'sem código'})."


def _explicar_rede(e: BaseException) -> str:
    causa = getattr(e, "reason", e)
    if isinstance(causa, ssl.SSLError) or "CERTIFICATE" in str(causa).upper():
        return ("Falha de certificado (SSL). Em Windows antigo (7/8), atualize os certificados "
                "raiz ou verifique data/hora; antivírus/proxy também podem interceptar.")
    if isinstance(causa, socket.gaierror):
        return "Não consegui resolver o endereço (DNS). Confira o Account ID e a internet do PC."
    if isinstance(causa, (socket.timeout, TimeoutError)):
        return "Tempo esgotado ao falar com o R2. Internet lenta/instável ou bloqueio de firewall."
    return f"Sem conexão com o R2: {causa}. Confira internet, firewall e proxy."


def testar_conexao(account_id: str, access_key: str, secret_key: str, bucket: str,
                   timeout: float = 15.0) -> Tuple[bool, str]:
    """Devolve (ok, mensagem em português para mostrar ao usuário)."""
    account_id, access_key, secret_key, bucket = (
        account_id.strip(), access_key.strip(), secret_key.strip(), bucket.strip())
    if not (account_id and access_key and secret_key and bucket):
        return False, "Preencha Account ID, Access Key ID, Secret Access Key e bucket antes de testar."
    if not re.fullmatch(r"[0-9a-fA-F]{32}", account_id):
        return False, "O Account ID deve ter 32 caracteres (só o ID, sem https:// nem .r2.cloudflarestorage.com)."

    host_pc = re.sub(r"[^A-Za-z0-9_-]+", "_", socket.gethostname()) or "pc"
    chave_teste = f"_status/_teste_conexao_{host_pc}.txt"
    feitos = []
    try:
        s, c = _requisicao("HEAD", account_id, bucket, None, access_key, secret_key, timeout=timeout)
        if s != 200:
            # HEAD não traz corpo (sem código de erro): repete como GET para a
            # resposta dizer se é Access Key, Secret, permissão ou bucket.
            _, c2 = _requisicao("GET", account_id, bucket, chave_teste, access_key, secret_key, timeout=timeout)
            return False, "Falhou ao acessar o bucket.\n\n" + _explicar("bucket", s, c2 or c)
        feitos.append("acesso ao bucket")

        s, c = _requisicao("PUT", account_id, bucket, chave_teste, access_key, secret_key,
                           corpo=b"teste de conexao do configurador", timeout=timeout)
        if s not in (200, 201):
            return False, ("O bucket abriu, mas a ESCRITA falhou.\n\n" + _explicar("escrita", s, c))
        feitos.append("gravação de arquivo")

        s, c = _requisicao("DELETE", account_id, bucket, chave_teste, access_key, secret_key, timeout=timeout)
        if s not in (200, 204):
            return True, ("Conexão OK (acesso e gravação funcionam), mas não consegui APAGAR o arquivo de teste "
                          f"'{chave_teste}'.\n\nO token talvez não tenha permissão de exclusão; o robô não "
                          "precisa apagar nada. Pode apagar o arquivo manualmente no painel.")
        feitos.append("exclusão do arquivo de teste")
    except (urllib.error.URLError, OSError) as e:
        return False, _explicar_rede(e)
    return True, "Conexão OK: " + ", ".join(feitos) + ".\n\nAs credenciais servem para o robô."
