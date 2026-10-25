"""Cliente HTTP para os arquivos JSON de divulgação do TSE."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config

log = logging.getLogger(__name__)

_local = threading.local()


class LimitadorTaxa:
    """Token bucket global (todas as threads) para não estourar o limite do TSE."""

    def __init__(self, taxa: float, capacidade: float | None = None):
        self.taxa = float(taxa)
        self.capacidade = float(capacidade or max(1.0, taxa))
        self._tokens = self.capacidade
        self._ultimo = time.monotonic()
        self._lock = threading.Lock()
        self.total_429 = 0
        self.ultimo_429 = 0.0

    def esperar(self) -> None:
        while True:
            with self._lock:
                agora = time.monotonic()
                self._tokens = min(self.capacidade, self._tokens + (agora - self._ultimo) * self.taxa)
                self._ultimo = agora
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                falta = (1.0 - self._tokens) / self.taxa
            time.sleep(falta)

    def registrar_429(self) -> None:
        with self._lock:
            self.total_429 += 1
            self.ultimo_429 = time.monotonic()

    def em_penalidade(self, janela: float) -> bool:
        return (time.monotonic() - self.ultimo_429) < janela


limitador = LimitadorTaxa(config.TAXA_MAX_REQ_S)
HOST_LIMITADO = "resultados.tse.jus.br"


class Erro429(IOError):
    pass


def session() -> requests.Session:
    """Uma Session por thread (requests.Session não é thread-safe para pool)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = config.USER_AGENT
        retry = Retry(total=2, backoff_factor=0.5, status_forcelist=(500, 502, 503, 504))
        adapter = HTTPAdapter(max_retries=retry, pool_connections=64, pool_maxsize=64)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _local.session = s
    return s


def get_json(url: str, timeout: int | None = None, tentativas: int = 4) -> dict[str, Any]:
    """GET com limitador de taxa global e tratamento de 429 (espera crescente e nova tentativa)."""
    limitado = HOST_LIMITADO in url
    espera = 2.0
    for tentativa in range(tentativas):
        if limitado:
            limitador.esperar()
        r = session().get(url, timeout=timeout or config.HTTP_TIMEOUT)
        if r.status_code == 429:
            limitador.registrar_429()
            if tentativa == tentativas - 1:
                raise Erro429(f"429 Too Many Requests: {url}")
            retry_after = r.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else espera)
            espera = min(espera * 2, 30.0)
            continue
        r.raise_for_status()
        return r.json()
    raise Erro429(url)


def baixar_arquivo(url: str, destino: Path, timeout: int | None = None) -> Path:
    """Download em streaming para destino (via .part), retorna o caminho final."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(destino.suffix + ".part")
    with session().get(url, stream=True, timeout=timeout or config.HTTP_TIMEOUT) as r:
        r.raise_for_status()
        esperado = int(r.headers.get("Content-Length") or 0)
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)
    if esperado and tmp.stat().st_size != esperado:
        tmp.unlink(missing_ok=True)
        raise IOError(f"download incompleto: {url} ({tmp.stat().st_size if tmp.exists() else 0}/{esperado})")
    tmp.replace(destino)
    return destino


# ------------------------------------------------------------------ parsing
def _int(v: Any) -> int:
    try:
        return int(str(v).replace(".", "").strip() or 0)
    except ValueError:
        return 0


def ts_feed(d: dict[str, Any]) -> str | None:
    """Converte dg='25/10/2026', hg='19:19:13' em 'YYYY-MM-DD HH:MM:SS'."""
    dg, hg = d.get("dg") or "", d.get("hg") or ""
    if not dg or not hg:
        return None
    try:
        return datetime.strptime(f"{dg} {hg}", "%d/%m/%Y %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def parse_u_json(d: dict[str, Any]) -> dict[str, Any]:
    """Achata o -u.json (cargo Presidente) num dicionário plano de inteiros."""
    cands: dict[str, int] = {}
    cargos = d.get("carg") or []
    cargo = cargos[0] if cargos else {}
    for agr in cargo.get("agr") or []:
        for par in agr.get("par") or []:
            for c in par.get("cand") or []:
                cands[str(c.get("n"))] = _int(c.get("vap"))
    s = d.get("s") or {}
    e = d.get("e") or {}
    v = d.get("v") or {}
    v22 = cands.get("22", 0)
    v13 = cands.get("13", 0)
    vv = _int(v.get("vv"))
    v_outros = max(0, sum(n for k, n in cands.items() if k not in ("22", "13")))
    if vv == 0:
        vv = v22 + v13 + v_outros
    return {
        "ts_feed": ts_feed(d),
        "v22": v22,
        "v13": v13,
        "v_outros": v_outros,
        "vv": vv,
        "vb": _int(v.get("vb")),
        "vn": _int(v.get("tvn") or v.get("vn")),
        "tv": _int(v.get("tv")),
        "ts": _int(s.get("ts")),
        "st": _int(s.get("st")),
        "te": _int(e.get("te")),
        "est": _int(e.get("est")),
        "esnt": _int(e.get("esnt")),
        "c": _int(e.get("c")),
        "a": _int(e.get("a")),
        "cands": cands,
    }


def carregar_mun_config(forcar: bool = False) -> dict[str, Any]:
    """Config de municípios (cd TSE -> cdi IBGE, nome, UF). Cacheado em disco."""
    p = config.MUN_CONFIG_PATH
    if p.exists() and not forcar:
        return json.loads(p.read_text(encoding="utf-8"))
    d = get_json(config.url_mun_config(config.ELEICAO_2T))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return d


def lista_municipios(cfg: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """[{uf, cd, cdi, nome}] para todos os municípios (inclui ZZ)."""
    cfg = cfg or carregar_mun_config()
    out = []
    for uf in cfg.get("abr") or []:
        sg = str(uf["cd"]).upper()
        for m in uf.get("mu") or []:
            out.append({"uf": sg, "cd": str(m["cd"]).zfill(5), "cdi": str(m.get("cdi") or ""), "nome": m.get("nm", "")})
    return out


def baixar_geojson(forcar: bool = False) -> None:
    config.GEO_DIR.mkdir(parents=True, exist_ok=True)
    for nome, url in (("municipios.geojson", config.URL_GEO_MUN), ("ufs.geojson", config.URL_GEO_UF)):
        destino = config.GEO_DIR / nome
        if destino.exists() and not forcar:
            continue
        log.info("baixando %s", nome)
        baixar_arquivo(url, destino, timeout=180)
