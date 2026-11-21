import calendar
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

log = logging.getLogger("robocop_clone")

# Eventos de cancelamento tratados: 110111 = Cancelamento, 110112 = Cancelamento
# por substituição (NFC-e). Outros eventos (CC-e, manifestação etc.) não entram.
EVENTOS_CANCELAMENTO = {"110111", "110112"}
# cStat do retEvento que confirma o evento: 135 = registrado e vinculado,
# 155 = cancelamento homologado fora do prazo.
CSTAT_EVENTO_OK = {"135", "155"}


@dataclass
class FiscalDocInfo:
    cnpj: str
    ano: str
    mes: str
    data_emissao: datetime
    doc_tipo: str = "nota"          # "nota" ou "cancelamento"
    chave: Optional[str] = None     # chave de acesso (preenchida nos cancelamentos)


def _local_tag(element: ET.Element) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.split("}")[-1] if "}" in tag else tag


def _find_elem(root: ET.Element, tag_name: str) -> Optional[ET.Element]:
    # Assume um único documento fiscal por arquivo XML — pega o primeiro
    # elemento com esse nome em qualquer profundidade da árvore.
    for elem in root.iter():
        if _local_tag(elem) == tag_name:
            return elem
    return None


def _find_text(root: ET.Element, tag_name: str) -> Optional[str]:
    elem = _find_elem(root, tag_name)
    if elem is not None and elem.text:
        return elem.text.strip()
    return None


def _parse_iso_date(date_str: str) -> Optional[datetime]:
    if not date_str:
        return None
    raw = date_str.strip()[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def extract_fiscal_info(xml_path: Union[Path, str], usar_destinatario: bool = False) -> Optional[FiscalDocInfo]:
    """Extrai CNPJ (emitente ou destinatario) e data de emissao de um XML de
    NF-e/NFC-e. Retorna None se o XML nao for reconhecido como esse tipo de
    documento (eventos de cancelamento tem funcao propria:
    extract_cancelamento_info; inutilizacao e demais eventos nao sao tratados)."""
    xml_path = Path(xml_path)
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception:
        return None

    emit = _find_elem(root, "emit")
    ide = _find_elem(root, "ide")

    if emit is None or ide is None:
        return None

    dh_emi = _find_text(ide, "dhEmi") or _find_text(ide, "dEmi")
    dt = _parse_iso_date(dh_emi) if dh_emi else None
    if not dt:
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


def extract_cancelamento_info(xml_path: Union[Path, str]) -> Optional[FiscalDocInfo]:
    """Reconhece o XML de evento de cancelamento (procEventoNFe) de NFC-e/NF-e.

    Só aceita o evento se vier homologado pela SEFAZ (retEvento com cStat 135 ou
    155). O arquivo é classificado pelo mês de emissão da NOTA CANCELADA (AAMM da
    chave de acesso), e não pela data do cancelamento: assim o cancelamento de
    uma nota de setembro, feito em outubro, entra no lote de setembro.

    data_emissao devolvida = último dia desse mês, para o filtro de data de
    início só descartar eventos de meses inteiramente anteriores.
    """
    xml_path = Path(xml_path)
    try:
        root = ET.parse(xml_path).getroot()
    except Exception:
        return None

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
        log.warning(
            f"Evento de cancelamento sem homologação (cStat={cstat}), não será enviado: {xml_path.name}"
        )
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
    )
