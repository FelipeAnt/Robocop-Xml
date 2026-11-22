"""
verificar_status.py
-------------------
Roda no SEU computador (não no cliente). Lê os heartbeats que cada robô grava
em _status/ no R2 e mostra uma tabela com a situação de cada PC.

Uso:
    python verificar_status.py              # tabela de todos os PCs
    python verificar_status.py --horas 12   # limite para "SEM SINAL" (padrão 24h)
    python verificar_status.py --so-problemas
    python verificar_status.py --json       # saída em JSON (para automação)

Credenciais: monitor_config.json (na mesma pasta), criado como modelo na
primeira execução. Use uma chave de API de LEITURA (Object Read) — diferente da
chave de escrita que fica nos clientes.

A "idade" do último sinal vem do relógio do R2 (LastModified), então um PC com
data/hora errada não engana o monitor.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig

BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG = BASE_DIR / "monitor_config.json"
PREFIXO = "_status/"

MODELO = {
    "r2_account_id": "",
    "r2_access_key_id": "",
    "r2_secret_access_key": "",
    "r2_bucket": "",
}


def carregar_config() -> dict:
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(MODELO, indent=2), encoding="utf-8")
        print(f"Criei o modelo {CONFIG}. Preencha as credenciais de LEITURA e rode de novo.")
        sys.exit(1)
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))  # tolera BOM do Bloco de Notas
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"{CONFIG} ilegível ({e}). Corrija o JSON ou apague o arquivo para gerar um modelo novo.")
        sys.exit(1)
    if not isinstance(cfg, dict):
        print(f"{CONFIG} inválido: esperado um objeto JSON.")
        sys.exit(1)
    faltando = [k for k in MODELO if not cfg.get(k)]
    if faltando:
        print(f"Faltam campos em {CONFIG}: {', '.join(faltando)}")
        sys.exit(1)
    return cfg


def cliente_r2(cfg: dict):
    return boto3.client(
        "s3",
        endpoint_url=f"https://{cfg['r2_account_id']}.r2.cloudflarestorage.com",
        aws_access_key_id=cfg["r2_access_key_id"],
        aws_secret_access_key=cfg["r2_secret_access_key"],
        region_name="auto",
        config=BotoConfig(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def listar_heartbeats(client, bucket: str):
    """Retorna lista de (chave, LastModified, payload_dict_ou_None)."""
    resultado = []
    paginas = client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIXO)
    for pagina in paginas:
        for obj in pagina.get("Contents", []):
            chave = obj["Key"]
            if not chave.endswith(".json"):
                continue
            try:
                corpo = client.get_object(Bucket=bucket, Key=chave)["Body"].read()
                payload = json.loads(corpo.decode("utf-8"))
            except Exception:
                payload = None
            resultado.append((chave, obj["LastModified"], payload))
    return resultado


def classificar(payload, last_modified, agora, limite_horas: float):
    """Devolve (status, [alertas], idade_em_segundos)."""
    idade = (agora - last_modified).total_seconds()
    p = payload or {}
    intervalo = p.get("intervalo_s") or 900

    if idade > limite_horas * 3600:
        status = "SEM SINAL"
    elif idade > intervalo * 2.5:
        status = "ATRASADO"
    else:
        status = "OK"

    alertas = []
    if payload is None:
        alertas.append("heartbeat ilegível")
    else:
        if (p.get("erros_consecutivos") or 0) >= 3:
            alertas.append(f"{p['erros_consecutivos']} erros seguidos")
        if (p.get("workers_vivos") or 0) < (p.get("workers_esperados") or 0):
            alertas.append(f"workers {p.get('workers_vivos')}/{p.get('workers_esperados')}")
        ausentes = [f["nome"] for f in p.get("fontes", []) if not f.get("existe", True)]
        if ausentes:
            alertas.append("pasta ausente: " + ", ".join(ausentes))
        disco = p.get("disco_livre_mb")
        if disco is not None and disco < 500:
            alertas.append(f"disco baixo ({disco} MB)")
    return status, alertas, idade


def fmt_idade(seg: float) -> str:
    seg = int(seg)
    if seg < 90:
        return f"{seg}s"
    if seg < 5400:
        return f"{seg // 60}min"
    if seg < 172800:
        return f"{seg // 3600}h"
    return f"{seg // 86400}d"


def fmt_dt(iso):
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m %H:%M")
    except ValueError:
        return iso


def main():
    ap = argparse.ArgumentParser(description="Monitor dos robôs Robocop Clone")
    ap.add_argument("--horas", type=float, default=24, help="horas sem sinal para marcar SEM SINAL (padrão 24)")
    ap.add_argument("--so-problemas", action="store_true", help="mostra só PCs fora do OK ou com alertas")
    ap.add_argument("--json", action="store_true", help="saída em JSON")
    args = ap.parse_args()

    cfg = carregar_config()
    client = cliente_r2(cfg)
    agora = datetime.now(timezone.utc)

    linhas = []
    for chave, lm, payload in listar_heartbeats(client, cfg["r2_bucket"]):
        status, alertas, idade = classificar(payload, lm, agora, args.horas)
        p = payload or {}
        linhas.append({
            "chave": chave,
            "cliente": p.get("cliente") or chave[len(PREFIXO):-5],
            "host": p.get("host", "-"),
            "versao": p.get("versao", "-"),
            "status": status,
            "ultimo_sinal": fmt_idade(idade),
            "idade_s": int(idade),
            "ultimo_envio": fmt_dt(p.get("ultimo_envio_em")),
            "fila": p.get("fila_pendente", "-"),
            "enviados_sessao": p.get("enviados_sessao", "-"),
            "alertas": alertas,
        })

    if args.so_problemas:
        linhas = [l for l in linhas if l["status"] != "OK" or l["alertas"]]

    ordem = {"SEM SINAL": 0, "ATRASADO": 1, "OK": 2}
    linhas.sort(key=lambda l: (ordem[l["status"]], -bool(l["alertas"]), l["cliente"].lower()))

    if args.json:
        print(json.dumps(linhas, ensure_ascii=False, indent=2))
    else:
        if not linhas:
            print("Nenhum heartbeat encontrado." if not args.so_problemas else "Tudo certo: nenhum problema.")
        else:
            cab = f"{'CLIENTE':<26}{'STATUS':<11}{'SINAL':<8}{'ÚLT.ENVIO':<13}{'FILA':<7}{'VER':<6}ALERTAS"
            print(cab)
            print("-" * len(cab))
            for l in linhas:
                print(f"{l['cliente'][:25]:<26}{l['status']:<11}{l['ultimo_sinal']:<8}"
                      f"{l['ultimo_envio']:<13}{str(l['fila']):<7}{l['versao']:<6}{'; '.join(l['alertas'])}")
            ok = sum(1 for l in linhas if l["status"] == "OK" and not l["alertas"])
            print(f"\n{len(linhas)} PC(s) listado(s); {ok} sem problemas.")

    # código de saída 1 se houver problema (útil para agendar/automatizar)
    sys.exit(1 if any(l["status"] != "OK" or l["alertas"] for l in linhas) else 0)


if __name__ == "__main__":
    main()
