"""Seções e votos que entraram na totalização por janela de tempo (padrão: 10 minutos).

2º turno (padrão): usa os snapshots nacionais do TSE gravados em data/apuracao.db durante a apuração
    (campos st = seções totalizadas e tv = votos, acumulados). Cada janela mostra o que foi
    acrescentado entre o último snapshot anterior ao início da janela e o último dentro dela.
1º turno: usa data/1t/secoes_{UF}.csv (horário de recebimento de cada seção e comparecimento).

Uso:
  python analisar_fechamento.py                  # 2º turno, janelas de 10 min
  python analisar_fechamento.py --turno 1        # 1º turno
  python analisar_fechamento.py --intervalo 5    # janelas de 5 min
  python analisar_fechamento.py --detalhe        # acrescenta válidos, Flavio e Lula
  python analisar_fechamento.py --csv saida.csv  # grava também em CSV
  python analisar_fechamento.py --db /opt/eleicoes/data/apuracao.db
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

import config


def fmt(n: float) -> str:
    """345423 -> '345 423' (separador de milhar com espaço, como no exemplo do usuário)."""
    return f"{int(round(n)):,}".replace(",", " ")


def janelas_2t(db: Path, intervalo: int) -> pd.DataFrame:
    from storage import Storage
    if not db.exists():
        sys.exit(f"banco não encontrado: {db}")
    snaps = Storage(db).snapshots("br", "br")
    snaps = snaps[snaps["st"] > 0].copy()
    if snaps.empty:
        sys.exit("nenhum snapshot nacional com seções apuradas no banco (o app rodou durante a apuração?)")
    snaps["ts"] = pd.to_datetime(snaps["ts_feed"])
    snaps = snaps.sort_values("ts").drop_duplicates("ts", keep="last")
    acum = snaps.set_index("ts")[["st", "tv", "vv", "v22", "v13"]].astype("int64")
    passo = pd.Timedelta(minutes=intervalo)
    inicio = acum.index.min().floor(f"{intervalo}min")
    fim = acum.index.max().floor(f"{intervalo}min") + passo
    limites = pd.date_range(inicio, fim, freq=passo)
    # valor acumulado logo antes de cada limite (último snapshot com ts < limite); zero antes do 1º snapshot
    pos = acum.index.searchsorted(limites, side="left") - 1
    valores = acum.to_numpy()
    import numpy as np
    base = np.where(pos[:, None] >= 0, valores[np.clip(pos, 0, None)], 0)
    delta = np.diff(base, axis=0)
    out = pd.DataFrame(delta, columns=["secoes", "votos", "validos", "v22", "v13"])
    out.insert(0, "inicio", limites[:-1])
    out.insert(1, "fim", limites[1:] - pd.Timedelta(seconds=1))
    return out[(out[["secoes", "votos"]] != 0).any(axis=1) | (out.index == 0)]


def janelas_1t(intervalo: int) -> pd.DataFrame:
    import primeiro_turno
    df = primeiro_turno.carregar()
    if df.empty:
        sys.exit("dados do 1º turno não encontrados em data/1t (rode primeiro_turno.py)")
    df = df.dropna(subset=["dt_recebido"])
    df["janela"] = df["dt_recebido"].dt.floor(f"{intervalo}min")
    g = df.groupby("janela").agg(secoes=("secao", "size"), votos=("comparecimento", "sum"), validos=("vv", "sum"),
                                 v22=("v22", "sum"), v13=("v13", "sum")).reset_index()
    g.insert(0, "inicio", g.pop("janela"))
    g.insert(1, "fim", g["inicio"] + pd.Timedelta(minutes=intervalo) - pd.Timedelta(seconds=1))
    return g


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--turno", type=int, choices=(1, 2), default=2)
    ap.add_argument("--intervalo", type=int, default=10, help="minutos por janela (padrão 10)")
    ap.add_argument("--db", type=Path, default=config.DB_PATH, help="banco do 2º turno (padrão data/apuracao.db)")
    ap.add_argument("--detalhe", action="store_true", help="mostra também válidos, Flavio (22) e Lula (13)")
    ap.add_argument("--csv", type=Path, help="grava a tabela em CSV")
    args = ap.parse_args()

    tab = janelas_2t(args.db, args.intervalo) if args.turno == 2 else janelas_1t(args.intervalo)
    fonte = ("snapshots nacionais do TSE (feed a cada 30 s) em " + str(args.db)) if args.turno == 2 \
        else "boletins de urna por seção (DT_BU_RECEBIDO) em data/1t"
    print(f"{args.turno}º turno · janelas de {args.intervalo} min · fonte: {fonte}\n")
    for r in tab.itertuples(index=False):
        linha = f"{r.inicio:%H:%M:%S} até {r.fim:%H:%M:%S} - {fmt(r.secoes):>9} seções - {fmt(r.votos):>13} votos"
        if args.detalhe:
            linha += f" - válidos {fmt(r.validos):>13} - Flavio {fmt(r.v22):>12} - Lula {fmt(r.v13):>12}"
        if r.inicio.date() != tab["inicio"].iloc[0].date():
            linha = f"({r.inicio:%d/%m}) " + linha
        print(linha)
    print(f"\nTOTAL: {fmt(tab['secoes'].sum())} seções - {fmt(tab['votos'].sum())} votos"
          + (f" - válidos {fmt(tab['validos'].sum())} - Flavio {fmt(tab['v22'].sum())} - Lula {fmt(tab['v13'].sum())}"
             if args.detalhe else ""))
    if args.csv:
        tab.to_csv(args.csv, index=False, encoding="utf-8", date_format="%Y-%m-%d %H:%M:%S")
        print(f"\nCSV gravado em {args.csv}")


if __name__ == "__main__":
    main()
