"""Download e processamento do 1º turno por seção (Boletim de Urna na Web do TSE).

Gera data/1t/secoes_{UF}.csv com uma linha por seção:
uf, cd_mun, zona, secao, dt_recebido, aptos, comparecimento, v22, v13, v_outros, brancos, nulos, secoes_agregadas

Uso: python primeiro_turno.py [--ufs AC,RR] [--manter-zips] [--workers 4]
"""
from __future__ import annotations

import argparse
import logging
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

import config
import tse_client

log = logging.getLogger(__name__)

COLUNAS = [
    "SG_UF", "CD_MUNICIPIO", "NR_ZONA", "NR_SECAO", "CD_CARGO_PERGUNTA", "DT_BU_RECEBIDO",
    "QT_APTOS", "QT_COMPARECIMENTO", "CD_TIPO_VOTAVEL", "NR_VOTAVEL", "QT_VOTOS",
    "DS_SECOES_AGREGADAS",
]
COLUNAS_SAIDA = [
    "uf", "cd_mun", "zona", "secao", "dt_recebido", "aptos", "comparecimento",
    "v22", "v13", "v_outros", "brancos", "nulos", "secoes_agregadas",
]
_parse_lock = threading.Semaphore(2)  # no máximo 2 parses simultâneos (memória)


def caminho_zip(uf: str) -> Path:
    return config.RAW_DIR / f"bweb_1t_{uf}.zip"


def caminho_csv(uf: str) -> Path:
    return config.DIR_1T / f"secoes_{uf}.csv"


def ufs_prontas() -> list[str]:
    return [uf for uf in config.UFS if caminho_csv(uf).exists()]


def baixar(uf: str) -> Path:
    destino = caminho_zip(uf)
    if destino.exists():
        return destino
    url = config.URL_BWEB.format(uf=uf)
    log.info("[%s] baixando %s", uf, url)
    t = time.time()
    tse_client.baixar_arquivo(url, destino, timeout=120)
    log.info("[%s] download ok (%.0f MB em %.0fs)", uf, destino.stat().st_size / 1e6, time.time() - t)
    return destino


def processar(uf: str, zip_path: Path) -> pd.DataFrame:
    """Lê o CSV dentro do zip, filtra Presidente e agrega por seção."""
    t = time.time()
    with zipfile.ZipFile(zip_path) as z:
        nome_csv = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        partes = []
        with z.open(nome_csv) as f:
            for ch in pd.read_csv(f, sep=";", encoding="latin-1", usecols=COLUNAS, dtype=str, chunksize=250_000):
                partes.append(ch[ch["CD_CARGO_PERGUNTA"] == "1"])
    df = pd.concat(partes, ignore_index=True) if partes else pd.DataFrame(columns=COLUNAS)
    del partes
    if df.empty:
        return pd.DataFrame(columns=COLUNAS_SAIDA)

    votos = pd.to_numeric(df["QT_VOTOS"], errors="coerce").fillna(0).astype("int64")
    tipo = df["CD_TIPO_VOTAVEL"].astype(str)
    nr = df["NR_VOTAVEL"].astype(str)
    cat = np.select(
        [
            (tipo == "1") & (nr == "22"),
            (tipo == "1") & (nr == "13"),
            tipo == "1",
            tipo == "2",
            tipo == "3",
        ],
        ["v22", "v13", "v_outros", "brancos", "nulos"],
        default="ignorar",
    )
    base = pd.DataFrame({
        "uf": df["SG_UF"].str.upper(),
        "cd_mun": df["CD_MUNICIPIO"].str.strip().str.zfill(5),
        "zona": df["NR_ZONA"].str.strip().str.zfill(4),
        "secao": df["NR_SECAO"].str.strip().str.zfill(4),
        "dt_recebido": df["DT_BU_RECEBIDO"],
        "aptos": pd.to_numeric(df["QT_APTOS"], errors="coerce").fillna(0).astype("int64"),
        "comparecimento": pd.to_numeric(df["QT_COMPARECIMENTO"], errors="coerce").fillna(0).astype("int64"),
        "secoes_agregadas": df["DS_SECOES_AGREGADAS"].fillna("").replace("#NULO#", ""),
        "cat": cat,
        "votos": votos,
    })
    del df
    chave = ["uf", "cd_mun", "zona", "secao"]
    piv = base.pivot_table(index=chave, columns="cat", values="votos", aggfunc="sum", fill_value=0)
    for c in ("v22", "v13", "v_outros", "brancos", "nulos"):
        if c not in piv.columns:
            piv[c] = 0
    piv = piv[["v22", "v13", "v_outros", "brancos", "nulos"]].astype("int64")
    meta = base.groupby(chave).agg(
        dt_recebido=("dt_recebido", "first"),
        aptos=("aptos", "first"),
        comparecimento=("comparecimento", "first"),
        secoes_agregadas=("secoes_agregadas", "first"),
    )
    out = meta.join(piv).reset_index()[COLUNAS_SAIDA]
    out = out.sort_values(["dt_recebido", "cd_mun", "zona", "secao"]).reset_index(drop=True)
    log.info("[%s] processado: %d seções, Flavio=%d Lula=%d outros=%d (%.0fs)", uf, len(out),
             out.v22.sum(), out.v13.sum(), out.v_outros.sum(), time.time() - t)
    return out


def preparar_uf(uf: str, manter_zip: bool = False) -> Path:
    csv = caminho_csv(uf)
    if csv.exists():
        return csv
    z = baixar(uf)
    with _parse_lock:
        df = processar(uf, z)
        tmp = csv.with_suffix(".csv.part")
        df.to_csv(tmp, index=False, encoding="utf-8")
        tmp.replace(csv)
    if not manter_zip:
        z.unlink(missing_ok=True)
    return csv


def preparar_todas(ufs: list[str] | None = None, manter_zips: bool = False, workers: int | None = None,
                   callback=None) -> dict[str, str]:
    """Baixa/processa as UFs em paralelo. Retorna {uf: 'ok'|'erro: ...'}."""
    config.garantir_pastas()
    ufs = ufs or config.UFS
    # maiores primeiro para melhor uso do paralelismo
    ordem = ["SP", "RJ", "MG", "BA", "RS", "PR", "PE", "CE", "PA", "GO", "SC", "MA"]
    ufs = sorted(ufs, key=lambda u: (ordem.index(u) if u in ordem else 99, u))
    resultado: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers or config.THREADS_DOWNLOAD_1T) as ex:
        futs = {ex.submit(preparar_uf, uf, manter_zips): uf for uf in ufs}
        for fut in as_completed(futs):
            uf = futs[fut]
            try:
                fut.result()
                resultado[uf] = "ok"
            except Exception as exc:  # noqa: BLE001
                log.exception("[%s] falhou", uf)
                resultado[uf] = f"erro: {exc}"
            if callback:
                callback(uf, resultado[uf])
    return resultado


def carregar(ufs: list[str] | None = None) -> pd.DataFrame:
    """Carrega todos os CSVs prontos num único DataFrame (dt_recebido como datetime)."""
    partes = []
    for uf in ufs or ufs_prontas():
        p = caminho_csv(uf)
        if p.exists():
            partes.append(pd.read_csv(p, dtype={"cd_mun": str, "zona": str, "secao": str, "secoes_agregadas": str},
                                      encoding="utf-8", keep_default_na=False))
    if not partes:
        return pd.DataFrame(columns=COLUNAS_SAIDA)
    df = pd.concat(partes, ignore_index=True)
    df["dt_recebido"] = pd.to_datetime(df["dt_recebido"], errors="coerce")
    for c in ("aptos", "comparecimento", "v22", "v13", "v_outros", "brancos", "nulos"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")
    df["vv"] = df["v22"] + df["v13"] + df["v_outros"]
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ufs", help="lista separada por vírgula (padrão: todas)")
    ap.add_argument("--manter-zips", action="store_true", help="não apagar os zips após processar")
    ap.add_argument("--workers", type=int, default=config.THREADS_DOWNLOAD_1T)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ufs = [u.strip().upper() for u in args.ufs.split(",")] if args.ufs else None
    res = preparar_todas(ufs, args.manter_zips, args.workers)
    erros = {k: v for k, v in res.items() if v != "ok"}
    print(f"concluído: {len(res) - len(erros)} ok, {len(erros)} erro(s)")
    for uf, msg in erros.items():
        print(f"  {uf}: {msg}")
    df = carregar(ufs)
    if not df.empty:
        print(f"seções: {len(df):,}  Flavio(22)={df.v22.sum():,}  Lula(13)={df.v13.sum():,}  "
              f"outros={df.v_outros.sum():,}  válidos={df.vv.sum():,}")


if __name__ == "__main__":
    main()
