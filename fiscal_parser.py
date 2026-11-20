import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Union


@dataclass
class FiscalDocInfo:
    cnpj: str
    ano: str
    mes: str
    data_emissao: datetime


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
    documento (ex: evento, cancelamento, inutilizacao - nao tratados aqui)."""
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
