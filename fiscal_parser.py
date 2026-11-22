import calendar
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

log = logging.getLogger("robocop_clone")

# cStat do protocolo (protNFe) que indica nota AUTORIZADA:
# 100 = autorizado o uso; 150 = autorizado fora do prazo.
CSTAT_NOTA_OK = {"100", "150"}

# Eventos de cancelamento tratados: 110111 = Cancelamento, 110112 = Cancelamento
# por substituição (NFC-e). Outros eventos (CC-e, manifestação etc.) não entram.
EVENTOS_CANCELAMENTO = {"110111", "110112"}
# cStat do retEvento que confirma o evento: 135 = registrado e vinculado,
# 155 = cancelamento homologado fora do prazo.
CSTAT_EVENTO_OK = {"135", "155"}

# cStat do retInutNFe: 102 = inutilização de número homologada.
CSTAT_INUT_OK = {"102"}


@dataclass
class FiscalDocInfo:
    cnpj: str
    ano: str
    mes: str
    data_emissao: datetime
    doc_tipo: str = "nota"          # "nota", "cancelamento" ou "inutilizacao"
    chave: Optional[str] = None     # chave de acesso (cancelamentos)
    pasta: Optional[str] = None     # subpasta no R2 (None = usa o tipo da fonte: saida/entrada)
    nome_objeto: Optional[str] = None  # nome do arquivo no R2 (None = nome original)


def _local_tag(element: ET.Element) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.split("}")[-1] if "}" in tag else tag


def _find_elem(root: Optional[ET.Element], tag_name: str) -> Optional[ET.Element]:
    # Assume um único documento fiscal por arquivo XML — pega o primeiro
    # elemento com esse nome em qualquer profundidade da árvore.
    if root is None:
        return None
    for elem in root.iter():
        if _local_tag(elem) == tag_name:
            return elem
    return None


def _find_text(root: Optional[ET.Element], tag_name: str) -> Optional[str]:
    elem = _find_elem(root, tag_name)
    if elem is not None and elem.text:
        return elem.text.strip()
    return None


def _parse_iso_date(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str:
        return None
    raw = date_str.strip()[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def _carregar(xml_path: Union[Path, str]):
    xml_path = Path(xml_path)
    try:
        return xml_path, ET.parse(xml_path).getroot()
    except Exception:
        return xml_path, None


# ------------------------------------------------------------------
# NOTAS (NF-e / NFC-e)
# ------------------------------------------------------------------

def _nota_de_root(root, nome: str, usar_destinatario: bool, exigir_protocolo: bool) -> Optional[FiscalDocInfo]:
    emit = _find_elem(root, "emit")
    ide = _find_elem(root, "ide")

    if emit is None or ide is None:
        return None

    dh_emi = _find_text(ide, "dhEmi") or _find_text(ide, "dEmi")
    dt = _parse_iso_date(dh_emi) if dh_emi else None
    if not dt:
        return None

    # Só envia nota AUTORIZADA: o protocolo (protNFe) precisa ter cStat 100/150.
    inf_prot = _find_elem(_find_elem(root, "protNFe"), "infProt")
    cstat = _find_text(inf_prot, "cStat")
    if cstat is None:
        if exigir_protocolo:
            log.warning(f"Nota sem protocolo de autorização, não será enviada: {nome}")
            return None
    elif cstat not in CSTAT_NOTA_OK:
        log.warning(f"Nota não autorizada (cStat={cstat}), não será enviada: {nome}")
        return None

    dest = _find_elem(root, "dest") if usar_destinatario else None
    alvo = dest if dest is not None else emit
    cnpj = _find_text(alvo, "CNPJ") or _find_text(alvo, "CPF")

    # Fallback para emitente caso dest nao tenha CNPJ/CPF (comum em NFC-e sem CPF)
    if not cnpj and alvo is not emit:
        cnpj = _find_text(emit, "CNPJ") or _find_text(emit, "CPF")

    if not cnpj:
        return None

    return FiscalDocInfo(
        cnpj=cnpj,
        ano=f"{dt.year:04d}",
        mes=f"{dt.month:02d}",
        data_emissao=dt,
    )


def extract_fiscal_info(xml_path: Union[Path, str], usar_destinatario: bool = False,
                        exigir_protocolo: bool = True) -> Optional[FiscalDocInfo]:
    """Extrai CNPJ (emitente ou destinatario) e data de emissao de um XML de
    NF-e/NFC-e AUTORIZADA (protocolo com cStat 100/150). Retorna None se o XML
    nao for uma nota, ou se for rejeitada/denegada/sem protocolo."""
    xml_path, root = _carregar(xml_path)
    if root is None:
        return None
    return _nota_de_root(root, xml_path.name, usar_destinatario, exigir_protocolo)


# ------------------------------------------------------------------
# EVENTO DE CANCELAMENTO
# ------------------------------------------------------------------

def _cancelamento_de_root(root, nome: str) -> Optional[FiscalDocInfo]:
    evento = _find_elem(root, "evento")
    if evento is None:
        return None
    inf = _find_elem(evento, "infEvento")
    if inf is None:
        return None

    tp_evento = _find_text(inf, "tpEvento")
    if tp_evento not in EVENTOS_CANCELAMENTO:
        return None

    chave = _find_text(inf, "chNFe") or ""
    if not re.fullmatch(r"\d{44}", chave):
        return None

    ret = _find_elem(root, "retEvento")
    cstat = _find_text(ret, "cStat") if ret is not None else None
    if cstat not in CSTAT_EVENTO_OK:
        log.warning(f"Evento de cancelamento sem homologação (cStat={cstat}), não será enviado: {nome}")
        return None

    ano = f"20{chave[2:4]}"
    mes = chave[4:6]
    if not ("01" <= mes <= "12"):
        return None
    ultimo_dia = calendar.monthrange(int(ano), int(mes))[1]

    cnpj = _find_text(inf, "CNPJ") or _find_text(inf, "CPF") or chave[6:20]

    return FiscalDocInfo(
        cnpj=cnpj,
        ano=ano,
        mes=mes,
        data_emissao=datetime(int(ano), int(mes), ultimo_dia),
        doc_tipo="cancelamento",
        chave=chave,
        pasta="cancelamento",
        nome_objeto=f"{chave}-cancelamento.xml",
    )


def extract_cancelamento_info(xml_path: Union[Path, str]) -> Optional[FiscalDocInfo]:
    """Reconhece o XML de evento de cancelamento (procEventoNFe) de NFC-e/NF-e.

    Só aceita o evento se vier homologado pela SEFAZ (retEvento com cStat 135 ou
    155). O arquivo é classificado pelo mês de emissão da NOTA CANCELADA (AAMM da
    chave de acesso), e não pela data do cancelamento: assim o cancelamento de
    uma nota de setembro, feito em outubro, entra no lote de setembro.

    data_emissao devolvida = último dia desse mês, para o filtro de data de
    início só descartar eventos de meses inteiramente anteriores.
    """
    xml_path, root = _carregar(xml_path)
    if root is None:
        return None
    return _cancelamento_de_root(root, xml_path.name)


# ------------------------------------------------------------------
# INUTILIZAÇÃO DE NUMERAÇÃO
# ------------------------------------------------------------------

def _inutilizacao_de_root(root, nome: str) -> Optional[FiscalDocInfo]:
    inf = _find_elem(_find_elem(root, "inutNFe"), "infInut")
    if inf is None:
        return None

    ret = _find_elem(root, "retInutNFe")
    inf_ret = _find_elem(ret, "infInut")
    cstat = _find_text(inf_ret, "cStat")
    if cstat not in CSTAT_INUT_OK:
        log.warning(f"Inutilização sem homologação (cStat={cstat}), não será enviada: {nome}")
        return None

    cnpj = _find_text(inf, "CNPJ") or _find_text(inf_ret, "CNPJ")
    modelo = re.sub(r"\D", "", _find_text(inf, "mod") or "")
    serie = re.sub(r"\D", "", _find_text(inf, "serie") or "")
    n_ini = re.sub(r"\D", "", _find_text(inf, "nNFIni") or "")
    n_fin = re.sub(r"\D", "", _find_text(inf, "nNFFin") or "")
    dt = _parse_iso_date(_find_text(inf_ret, "dhRecbto"))
    if not (cnpj and modelo and serie and n_ini and n_fin and dt):
        log.warning(f"Inutilização incompleta (faltam CNPJ/modelo/série/faixa/data de homologação): {nome}")
        return None

    # Não há mês de emissão (são números que não foram usados): arquiva pelo mês
    # em que a SEFAZ homologou a inutilização.
    return FiscalDocInfo(
        cnpj=cnpj,
        ano=f"{dt.year:04d}",
        mes=f"{dt.month:02d}",
        data_emissao=dt,
        doc_tipo="inutilizacao",
        pasta="inutilizacao",
        nome_objeto=f"inutilizacao-{modelo}-s{serie}-{int(n_ini)}-{int(n_fin)}.xml",
    )


def extract_inutilizacao_info(xml_path: Union[Path, str]) -> Optional[FiscalDocInfo]:
    """Reconhece o XML de inutilização de numeração (ProcInutNFe) homologada
    (retInutNFe com cStat 102)."""
    xml_path, root = _carregar(xml_path)
    if root is None:
        return None
    return _inutilizacao_de_root(root, xml_path.name)


# ------------------------------------------------------------------
# ENTRADA ÚNICA: lê o XML uma vez e decide o tipo pela raiz
# ------------------------------------------------------------------

def extract_documento(xml_path: Union[Path, str], usar_destinatario: bool = False,
                      exigir_protocolo: bool = True, aceitar_eventos: bool = True) -> Optional[FiscalDocInfo]:
    """Reconhece nota autorizada, cancelamento homologado ou inutilização
    homologada. Eventos só são aceitos com aceitar_eventos=True (pastas de
    saída: o evento traz o CNPJ do emitente). Devolve None para todo o resto."""
    xml_path, root = _carregar(xml_path)
    if root is None:
        return None
    raiz = _local_tag(root).lower()
    if raiz == "proceventonfe":
        return _cancelamento_de_root(root, xml_path.name) if aceitar_eventos else None
    if raiz == "procinutnfe":
        return _inutilizacao_de_root(root, xml_path.name) if aceitar_eventos else None
    return _nota_de_root(root, xml_path.name, usar_destinatario, exigir_protocolo)
