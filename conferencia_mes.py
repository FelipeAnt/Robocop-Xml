"""
conferencia_mes.py
------------------
Roda no SEU computador (nao no cliente), sobre a pasta de XMLs que voce baixou
do R2 (rclone). Le notas (NFC-e/NF-e) e eventos de cancelamento, cruza pela
chave de acesso e mostra, por CNPJ, o que bater com o SIGA/PGDAS:

  - quantidade e valor das notas de saida autorizadas
  - quantas foram canceladas (evento homologado) e quanto valiam
  - total LIQUIDO (autorizadas menos canceladas)
  - quebras de numeracao por serie, ja descontando os numeros INUTILIZADOS
    (o que sobra sem explicacao sao possiveis XMLs faltando)
  - alertas: duplicadas, sem protocolo, nao autorizadas, cancelamento sem nota

Uso:
    python conferencia_mes.py C:\\Conferencia\\2026-10
    python conferencia_mes.py C:\\Conferencia\\2026-10 C:\\Conferencia\\2026-11   (varias pastas)
    python conferencia_mes.py C:\\Conferencia\\2026-10 --cnpj 17717530000158
    python conferencia_mes.py C:\\Conferencia --mes 2026-10
    python conferencia_mes.py C:\\Conferencia\\2026-10 --csv outubro.csv

Estrutura esperada (a que o robo grava):  <pasta>\\CNPJ\\saida|entrada|cancelamento|inutilizacao\\*.xml

Dica: a inutilizacao e arquivada pelo mes em que a SEFAZ a homologou. Uma
inutilizacao feita no mes seguinte (antes do PGDAS) fica na pasta do mes
seguinte: passe as duas pastas para o relatorio considerar tudo.
O CNPJ e o tipo sao lidos do caminho; se faltarem, deduz pelo conteudo do XML.
So usa a biblioteca padrao do Python.
"""

import argparse
import csv
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional

TIPOS_PASTA = {"saida", "entrada", "cancelamento", "inutilizacao"}
CSTAT_AUTORIZADA = {"100", "150"}
EVENTOS_CANCELAMENTO = {"110111", "110112"}
CSTAT_EVENTO_OK = {"135", "155"}
CSTAT_INUT_OK = {"102"}
MAX_FAIXAS_FALTANDO = 12


@dataclass
class Nota:
    chave: str
    cnpj: str
    tipo: str            # "saida" ou "entrada"
    modelo: str
    serie: str
    numero: int
    data: Optional[date]
    valor: Decimal
    cstat: Optional[str]
    arquivo: str


@dataclass
class Inutilizacao:
    cnpj: str
    modelo: str
    serie: str
    ini: int
    fin: int
    cstat: Optional[str]
    data: Optional[date]
    justificativa: str
    arquivo: str


@dataclass
class Evento:
    chave: str
    cnpj: str
    tp_evento: str
    cstat: Optional[str]
    data: Optional[date]
    justificativa: str
    arquivo: str


# ------------------------------------------------------------------
# LEITURA DOS XMLs
# ------------------------------------------------------------------

def _tag(e: ET.Element) -> str:
    t = e.tag
    return t.split("}")[-1] if isinstance(t, str) else ""


def _achar(raiz: Optional[ET.Element], nome: str) -> Optional[ET.Element]:
    if raiz is None:
        return None
    for e in raiz.iter():
        if _tag(e) == nome:
            return e
    return None


def _texto(raiz: Optional[ET.Element], nome: str) -> Optional[str]:
    e = _achar(raiz, nome)
    if e is not None and e.text and e.text.strip():
        return e.text.strip()
    return None


def _data(txt: Optional[str]) -> Optional[date]:
    if not txt:
        return None
    try:
        return date.fromisoformat(txt.strip()[:10])
    except ValueError:
        return None


def _decimal(txt: Optional[str]) -> Decimal:
    try:
        return Decimal(txt) if txt else Decimal("0")
    except InvalidOperation:
        return Decimal("0")


def _so_digitos(txt: str) -> str:
    return "".join(c for c in txt if c.isdigit())


def _tipo_e_cnpj_do_caminho(arquivo: Path, base: Path):
    """Deduz (tipo, cnpj) a partir de .../CNPJ/saida/arquivo.xml."""
    try:
        partes = arquivo.relative_to(base).parts[:-1]
    except ValueError:
        partes = arquivo.parts[:-1]
    tipo = cnpj = None
    for i, p in enumerate(partes):
        if p.lower() in TIPOS_PASTA:
            tipo = p.lower()
            if i > 0 and partes[i - 1].isdigit() and len(partes[i - 1]) in (11, 14):
                cnpj = partes[i - 1]
    return tipo, cnpj


def ler_arquivo(arquivo: Path, base: Path):
    """Devolve Nota, Evento ou None (se nao for um XML fiscal reconhecido)."""
    try:
        raiz = ET.parse(arquivo).getroot()
    except Exception:
        return None
    tipo_pasta, cnpj_pasta = _tipo_e_cnpj_do_caminho(arquivo, base)
    nome_raiz = _tag(raiz)

    if nome_raiz.lower() == "procinutnfe":
        inf = _achar(_achar(raiz, "inutNFe"), "infInut")
        if inf is None:
            return None
        ret = _achar(_achar(raiz, "retInutNFe"), "infInut")
        try:
            ini = int(_texto(inf, "nNFIni") or 0)
            fin = int(_texto(inf, "nNFFin") or 0)
        except ValueError:
            return None
        if ini <= 0 or fin < ini:
            return None
        return Inutilizacao(
            cnpj=cnpj_pasta or _so_digitos(_texto(inf, "CNPJ") or "") or "SEM_CNPJ",
            modelo=_texto(inf, "mod") or "",
            serie=_texto(inf, "serie") or "",
            ini=ini,
            fin=fin,
            cstat=_texto(ret, "cStat"),
            data=_data(_texto(ret, "dhRecbto")),
            justificativa=_texto(inf, "xJust") or "",
            arquivo=arquivo.name,
        )

    if nome_raiz == "procEventoNFe":
        evento = _achar(raiz, "evento")
        inf = _achar(evento, "infEvento")
        if inf is None:
            return None
        chave = _texto(inf, "chNFe") or ""
        if len(chave) != 44:
            return None
        ret = _achar(raiz, "retEvento")
        return Evento(
            chave=chave,
            cnpj=cnpj_pasta or chave[6:20],
            tp_evento=_texto(inf, "tpEvento") or "",
            cstat=_texto(ret, "cStat"),
            data=_data(_texto(inf, "dhEvento")),
            justificativa=_texto(inf, "xJust") or "",
            arquivo=arquivo.name,
        )

    if nome_raiz in ("nfeProc", "NFe"):
        inf = _achar(raiz, "infNFe")
        if inf is None:
            return None
        chave = (inf.get("Id") or "").replace("NFe", "")
        if len(chave) != 44:
            chave = _texto(_achar(raiz, "protNFe"), "chNFe") or ""
        if len(chave) != 44:
            return None
        ide = _achar(inf, "ide")
        emit = _achar(inf, "emit")
        dest = _achar(inf, "dest")
        tipo = "entrada" if tipo_pasta == "entrada" else "saida"
        if cnpj_pasta:
            cnpj = cnpj_pasta
        elif tipo == "entrada":
            cnpj = _texto(dest, "CNPJ") or _texto(dest, "CPF") or "SEM_CNPJ"
        else:
            cnpj = _texto(emit, "CNPJ") or _texto(emit, "CPF") or chave[6:20]
        try:
            numero = int(_texto(ide, "nNF") or 0)
        except ValueError:
            numero = 0
        prot = _achar(raiz, "protNFe")
        return Nota(
            chave=chave,
            cnpj=cnpj,
            tipo=tipo,
            modelo=_texto(ide, "mod") or chave[20:22],
            serie=_texto(ide, "serie") or chave[22:25].lstrip("0") or "0",
            numero=numero,
            data=_data(_texto(ide, "dhEmi") or _texto(ide, "dEmi")),
            valor=_decimal(_texto(_achar(inf, "ICMSTot"), "vNF")),
            cstat=_texto(prot, "cStat") if prot is not None else None,
            arquivo=arquivo.name,
        )
    return None


# ------------------------------------------------------------------
# FORMATACAO
# ------------------------------------------------------------------

def brl(v: Decimal) -> str:
    s = f"{v:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def milhar(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def faixas(numeros: List[int]) -> List[str]:
    """[5,6,7,10] -> ['5-7', '10']"""
    out, i = [], 0
    nums = sorted(numeros)
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return out


# ------------------------------------------------------------------
# ANALISE
# ------------------------------------------------------------------

@dataclass
class ResumoCnpj:
    cnpj: str
    saidas_bruto_qtd: int = 0
    saidas_bruto_valor: Decimal = Decimal("0")
    canceladas_qtd: int = 0
    canceladas_valor: Decimal = Decimal("0")
    entradas_qtd: int = 0
    entradas_valor: Decimal = Decimal("0")
    data_min: Optional[date] = None
    data_max: Optional[date] = None

    @property
    def liquido_qtd(self) -> int:
        return self.saidas_bruto_qtd - self.canceladas_qtd

    @property
    def liquido_valor(self) -> Decimal:
        return self.saidas_bruto_valor - self.canceladas_valor


def analisar(pastas: List[Path], mes: Optional[str], cnpj_filtro: Optional[str]):
    notas: Dict[tuple, Nota] = {}
    eventos: Dict[str, Evento] = {}
    inutilizacoes: Dict[tuple, Inutilizacao] = {}
    duplicadas: Dict[str, int] = defaultdict(int)
    ilegiveis = 0
    ignorados_fora_do_mes = 0

    arquivos = [(pasta, arq) for pasta in pastas for arq in sorted(pasta.rglob("*.xml"))]
    for pasta, arq in arquivos:
        item = ler_arquivo(arq, pasta)
        if item is None:
            ilegiveis += 1
            continue
        if cnpj_filtro and item.cnpj != cnpj_filtro:
            continue
        if isinstance(item, Inutilizacao):
            # Nao filtra por mes: a inutilizacao so serve para explicar quebras de numeracao.
            k = (item.cnpj, item.modelo, item.serie, item.ini, item.fin)
            if k in inutilizacoes:
                duplicadas[item.cnpj] += 1
            else:
                inutilizacoes[k] = item
            continue
        if mes:
            ano_mes = (item.data.strftime("%Y-%m") if isinstance(item, Nota) and item.data
                       else f"20{item.chave[2:4]}-{item.chave[4:6]}")
            if ano_mes != mes:
                ignorados_fora_do_mes += 1
                continue
        if isinstance(item, Nota):
            k = (item.chave, item.tipo)
            if k in notas:
                duplicadas[item.cnpj] += 1
                continue
            notas[k] = item
        else:
            if item.chave in eventos:
                duplicadas[item.cnpj] += 1
                continue
            eventos[item.chave] = item
    return notas, eventos, inutilizacoes, duplicadas, ilegiveis, ignorados_fora_do_mes


def consolidar(notas, eventos, inutilizacoes):
    resumos: Dict[str, ResumoCnpj] = {}
    problemas: Dict[str, List[str]] = defaultdict(list)
    canceladas: Dict[str, List[Nota]] = defaultdict(list)
    linhas_csv = []

    cancelam = {c: e for c, e in eventos.items()
                if e.tp_evento in EVENTOS_CANCELAMENTO and e.cstat in CSTAT_EVENTO_OK}
    for chave, e in eventos.items():
        if chave not in cancelam and e.tp_evento in EVENTOS_CANCELAMENTO:
            problemas[e.cnpj].append(f"evento de cancelamento sem homologacao (cStat {e.cstat}): {e.arquivo}")

    inut_validas = [i for i in inutilizacoes.values() if i.cstat in CSTAT_INUT_OK]
    for i in inutilizacoes.values():
        if i.cstat not in CSTAT_INUT_OK:
            problemas[i.cnpj].append(
                f"inutilizacao sem homologacao (cStat {i.cstat}) serie {i.serie} n. {i.ini}-{i.fin}: {i.arquivo}")
    inut_por_cnpj: Dict[str, List[Inutilizacao]] = defaultdict(list)
    for i in inut_validas:
        inut_por_cnpj[i.cnpj].append(i)
        resumos.setdefault(i.cnpj, ResumoCnpj(i.cnpj))

    chaves_saida = {n.chave for n in notas.values() if n.tipo == "saida"}
    sequencias = defaultdict(list)  # (cnpj, modelo, serie) -> numeros

    for (chave, tipo), n in notas.items():
        r = resumos.setdefault(n.cnpj, ResumoCnpj(n.cnpj))
        if n.data:
            r.data_min = min(r.data_min, n.data) if r.data_min else n.data
            r.data_max = max(r.data_max, n.data) if r.data_max else n.data

        if tipo == "entrada":
            r.entradas_qtd += 1
            r.entradas_valor += n.valor
            linhas_csv.append((n, "entrada"))
            continue

        if n.cstat is None:
            problemas[n.cnpj].append(f"nota {n.modelo}/{n.serie}/{n.numero} sem protocolo de autorizacao (nao contada): {n.arquivo}")
            linhas_csv.append((n, "sem_protocolo"))
            continue
        if n.cstat not in CSTAT_AUTORIZADA:
            problemas[n.cnpj].append(f"nota {n.modelo}/{n.serie}/{n.numero} nao autorizada (cStat {n.cstat}) (nao contada)")
            linhas_csv.append((n, f"cstat_{n.cstat}"))
            continue

        r.saidas_bruto_qtd += 1
        r.saidas_bruto_valor += n.valor
        sequencias[(n.cnpj, n.modelo, n.serie)].append(n.numero)
        if chave in cancelam:
            r.canceladas_qtd += 1
            r.canceladas_valor += n.valor
            canceladas[n.cnpj].append(n)
            linhas_csv.append((n, "cancelada"))
        else:
            linhas_csv.append((n, "autorizada"))

    for chave, e in cancelam.items():
        if chave not in chaves_saida:
            problemas[e.cnpj].append(
                f"cancelamento sem a nota correspondente neste lote (chave final ...{chave[-10:]}): {e.arquivo}")
            resumos.setdefault(e.cnpj, ResumoCnpj(e.cnpj))

    quebras = {}
    for (cnpj, modelo, serie), nums in sequencias.items():
        presentes = set(nums)
        unicos = sorted(presentes)
        faltando = [x for x in range(unicos[0], unicos[-1] + 1) if x not in presentes] if unicos else []
        faixas_inut = [(i.ini, i.fin) for i in inut_validas
                       if i.cnpj == cnpj and i.modelo == modelo and i.serie == serie]
        explicados = [x for x in faltando if any(a <= x <= b for a, b in faixas_inut)]
        set_explicados = set(explicados)
        sem_explicacao = [x for x in faltando if x not in set_explicados]
        quebras.setdefault(cnpj, []).append(
            (modelo, serie, unicos[0], unicos[-1], sem_explicacao, explicados))
    return resumos, problemas, canceladas, quebras, linhas_csv, cancelam, inut_por_cnpj


# ------------------------------------------------------------------
# SAIDA
# ------------------------------------------------------------------

def imprimir_resumo(resumos, problemas, quebras, duplicadas):
    cab = f"{'CNPJ':<16}{'SAIDAS':>8}{'CANCEL.':>9}{'LIQ. QTD':>10}{'LIQUIDO':>18}  ALERTAS"
    print(cab)
    print("-" * (len(cab) + 12))
    tot_q = tot_c = tot_l = 0
    tot_v = Decimal("0")
    for cnpj in sorted(resumos):
        r = resumos[cnpj]
        qtd_quebras = sum(len(q[4]) for q in quebras.get(cnpj, []))
        alertas = []
        if problemas.get(cnpj):
            alertas.append(f"{len(problemas[cnpj])} aviso(s)")
        if qtd_quebras:
            alertas.append(f"{qtd_quebras} num. faltando")
        if duplicadas.get(cnpj):
            alertas.append(f"{duplicadas[cnpj]} duplicada(s)")
        print(f"{cnpj:<16}{milhar(r.saidas_bruto_qtd):>8}{milhar(r.canceladas_qtd):>9}"
              f"{milhar(r.liquido_qtd):>10}{brl(r.liquido_valor):>18}  {'; '.join(alertas)}")
        tot_q += r.saidas_bruto_qtd
        tot_c += r.canceladas_qtd
        tot_l += r.liquido_qtd
        tot_v += r.liquido_valor
    print("-" * (len(cab) + 12))
    print(f"{'TOTAL':<16}{milhar(tot_q):>8}{milhar(tot_c):>9}{milhar(tot_l):>10}{brl(tot_v):>18}")


def imprimir_detalhe(cnpj, resumos, problemas, canceladas, quebras, eventos_validos, duplicadas, inut_por_cnpj):
    r = resumos[cnpj]
    print(f"\n=== CNPJ {cnpj} ===")
    if r.data_min:
        print(f"Periodo das notas: {r.data_min.strftime('%d/%m/%Y')} a {r.data_max.strftime('%d/%m/%Y')}")
    print(f"Saidas autorizadas : {milhar(r.saidas_bruto_qtd):>8}   {brl(r.saidas_bruto_valor):>18}")
    print(f"(-) Canceladas     : {milhar(r.canceladas_qtd):>8}   {brl(r.canceladas_valor):>18}")
    print(f"(=) Liquido        : {milhar(r.liquido_qtd):>8}   {brl(r.liquido_valor):>18}")
    if r.entradas_qtd:
        print(f"Entradas           : {milhar(r.entradas_qtd):>8}   {brl(r.entradas_valor):>18}")

    if canceladas.get(cnpj):
        print("\nNotas canceladas:")
        for n in sorted(canceladas[cnpj], key=lambda x: (x.serie, x.numero)):
            e = eventos_validos[n.chave]
            quando = n.data.strftime("%d/%m") if n.data else "--/--"
            print(f"  serie {n.serie} n. {n.numero:>6}  {quando}  {brl(n.valor):>14}  {e.justificativa[:50]}")

    for modelo, serie, primeiro, ultimo, faltando, explicados in quebras.get(cnpj, []):
        nome_modelo = "NFC-e" if modelo == "65" else "NF-e" if modelo == "55" else f"mod {modelo}"
        print(f"\n{nome_modelo} serie {serie}: de n. {primeiro} a {ultimo}")
        if explicados:
            print(f"  Numeros inutilizados ({len(explicados)}): {', '.join(faixas(explicados))}")
        if faltando:
            fx = faixas(faltando)
            extra = f" (+{len(fx) - MAX_FAIXAS_FALTANDO} faixas)" if len(fx) > MAX_FAIXAS_FALTANDO else ""
            print(f"  Numeros faltando SEM explicacao ({len(faltando)}): {', '.join(fx[:MAX_FAIXAS_FALTANDO])}{extra}")
            print("  (XML nao enviado, ou inutilizacao feita em outro mes: inclua a pasta do mes seguinte)")
        else:
            print("  Sequencia completa." if not explicados else "  Sequencia completa (com as inutilizacoes).")

    if inut_por_cnpj.get(cnpj):
        print("\nInutilizacoes registradas:")
        for i in sorted(inut_por_cnpj[cnpj], key=lambda x: (x.modelo, x.serie, x.ini)):
            quando = i.data.strftime("%d/%m/%Y") if i.data else "--/--/----"
            faixa = str(i.ini) if i.ini == i.fin else f"{i.ini}-{i.fin}"
            print(f"  serie {i.serie} n. {faixa:<12} homologada em {quando}  {i.justificativa[:50]}")

    if duplicadas.get(cnpj):
        print(f"\nArquivos duplicados ignorados: {duplicadas[cnpj]}")
    if problemas.get(cnpj):
        print("\nAvisos:")
        for p in problemas[cnpj]:
            print(f"  - {p}")


def gravar_csv(caminho: Path, linhas_csv, cancelam, inut_por_cnpj):
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["cnpj", "tipo", "status", "modelo", "serie", "numero", "data", "valor", "chave", "justificativa_cancelamento", "arquivo"])
        for n, status in sorted(linhas_csv, key=lambda x: (x[0].cnpj, x[0].modelo, x[0].serie, x[0].numero)):
            just = cancelam[n.chave].justificativa if status == "cancelada" and n.chave in cancelam else ""
            w.writerow([n.cnpj, n.tipo, status, n.modelo, n.serie, n.numero,
                        n.data.strftime("%d/%m/%Y") if n.data else "",
                        str(n.valor).replace(".", ","), n.chave, just, n.arquivo])
        for cnpj in sorted(inut_por_cnpj):
            for i in sorted(inut_por_cnpj[cnpj], key=lambda x: (x.modelo, x.serie, x.ini)):
                faixa = str(i.ini) if i.ini == i.fin else f"{i.ini}-{i.fin}"
                w.writerow([cnpj, "inutilizacao", "inutilizada", i.modelo, i.serie, faixa,
                            i.data.strftime("%d/%m/%Y") if i.data else "", "0", "", i.justificativa, i.arquivo])


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Conferencia mensal de XMLs (notas, cancelamentos e inutilizacoes)")
    ap.add_argument("pasta", nargs="+", help="pasta(s) baixada(s) do R2 (ex: C:\\Conferencia\\2026-10)")
    ap.add_argument("--cnpj", help="mostra o detalhe de um CNPJ (so digitos)")
    ap.add_argument("--mes", help="considera so o mes de emissao AAAA-MM")
    ap.add_argument("--csv", help="grava uma linha por nota em CSV (separador ;)")
    args = ap.parse_args()

    pastas = [Path(p) for p in args.pasta]
    for pasta in pastas:
        if not pasta.is_dir():
            print(f"Pasta nao encontrada: {pasta}")
            sys.exit(1)
    cnpj_filtro = _so_digitos(args.cnpj) if args.cnpj else None

    notas, eventos, inutilizacoes, duplicadas, ilegiveis, fora_mes = analisar(pastas, args.mes, cnpj_filtro)
    if not notas and not eventos and not inutilizacoes:
        print("Nenhuma nota ou evento encontrado nessa(s) pasta(s).")
        sys.exit(1)

    resumos, problemas, canceladas, quebras, linhas_csv, cancelam, inut_por_cnpj = consolidar(
        notas, eventos, inutilizacoes)

    print("Pasta: " + " + ".join(str(p) for p in pastas) + (f"  |  mes: {args.mes}" if args.mes else ""))
    print(f"Notas lidas: {milhar(len(notas))}  |  cancelamentos: {milhar(len(eventos))}"
          f"  |  inutilizacoes: {milhar(len(inutilizacoes))}"
          + (f"  |  XML nao reconhecido: {ilegiveis}" if ilegiveis else "")
          + (f"  |  fora do mes: {fora_mes}" if fora_mes else ""))
    print()
    imprimir_resumo(resumos, problemas, quebras, duplicadas)

    if cnpj_filtro:
        if cnpj_filtro in resumos:
            imprimir_detalhe(cnpj_filtro, resumos, problemas, canceladas, quebras, cancelam, duplicadas, inut_por_cnpj)
        else:
            print(f"\nCNPJ {cnpj_filtro} nao encontrado.")
    else:
        com_aviso = [c for c in sorted(resumos) if problemas.get(c)]
        if com_aviso:
            print("\nAvisos:")
            for c in com_aviso:
                for p in problemas[c]:
                    print(f"  [{c}] {p}")
        print("\nUse --cnpj <numero> para ver notas canceladas, inutilizacoes e numeracao faltando.")

    if args.csv:
        gravar_csv(Path(args.csv), linhas_csv, cancelam, inut_por_cnpj)
        print(f"\nCSV gravado em {args.csv}")


if __name__ == "__main__":
    main()
