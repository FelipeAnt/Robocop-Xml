"""
segredo.py
----------
Protege o Secret Access Key do R2 dentro do config.json usando o DPAPI do
Windows (CryptProtectData), em escopo de MÁQUINA.

O que isso resolve:
  - Se alguém copiar a pasta/config.json para outro computador (ou for parar num
    backup/e-mail), o segredo NÃO abre fora da máquina original.

O que isso NÃO resolve:
  - Quem já está logado no próprio PC do cliente (ou roda código nele) consegue
    usar o robô e, portanto, o segredo. Por isso o token de cada cliente é
    separado e revogável.

Formato guardado no config.json:  "dpapi:<base64>"
Valores sem esse prefixo são tratados como texto puro (instalações antigas).
Fora do Windows (testes) nada é criptografado.
"""

import base64
import sys

PREFIXO = "dpapi:"
_ENTROPIA = b"robocop_clone.v1"

_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_CRYPTPROTECT_LOCAL_MACHINE = 0x4


class SegredoIndisponivel(Exception):
    """Não foi possível abrir o segredo (copiado de outro PC ou corrompido)."""


def disponivel() -> bool:
    return sys.platform == "win32"


def esta_protegido(valor: str) -> bool:
    return isinstance(valor, str) and valor.startswith(PREFIXO)


def _dpapi(dados: bytes, proteger: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _para_blob(b: bytes):
        buf = ctypes.create_string_buffer(b, len(b))
        return _BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    if proteger:
        fn = crypt32.CryptProtectData
        fn.argtypes = [ctypes.POINTER(_BLOB), wintypes.LPCWSTR, ctypes.POINTER(_BLOB),
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_BLOB)]
    else:
        fn = crypt32.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(_BLOB), ctypes.c_void_p, ctypes.POINTER(_BLOB),
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_BLOB)]
    fn.restype = wintypes.BOOL

    entrada, _buf1 = _para_blob(dados)
    entropia, _buf2 = _para_blob(_ENTROPIA)
    saida = _BLOB()
    flags = _CRYPTPROTECT_UI_FORBIDDEN | _CRYPTPROTECT_LOCAL_MACHINE

    if proteger:
        ok = fn(ctypes.byref(entrada), "robocop", ctypes.byref(entropia), None, None, flags, ctypes.byref(saida))
    else:
        ok = fn(ctypes.byref(entrada), None, ctypes.byref(entropia), None, None, flags, ctypes.byref(saida))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        return ctypes.string_at(saida.pbData, saida.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(saida.pbData, ctypes.c_void_p))


def proteger(texto: str) -> str:
    """Devolve 'dpapi:<base64>' (ou o próprio texto fora do Windows)."""
    if not disponivel():
        return texto
    if esta_protegido(texto):
        return texto
    bruto = _dpapi(texto.encode("utf-8"), proteger=True)
    return PREFIXO + base64.b64encode(bruto).decode("ascii")


def revelar(valor: str) -> str:
    """Abre um valor protegido. Texto puro (sem prefixo) é devolvido como está."""
    if not esta_protegido(valor):
        return valor
    if not disponivel():
        raise SegredoIndisponivel("Segredo protegido com DPAPI, mas este sistema não é Windows.")
    try:
        bruto = base64.b64decode(valor[len(PREFIXO):])
        return _dpapi(bruto, proteger=False).decode("utf-8")
    except Exception as e:
        raise SegredoIndisponivel(str(e)) from e
