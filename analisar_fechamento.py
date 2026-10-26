"""Seções e votos por janela de tempo (padrão: 10 minutos), segundo o horário escolhido.

Fontes:
  bu        boletins de urna baixados por baixar_bu.py (data/bu_{turno}t.db): horário real por seção.
            --evento escolhe o horário: encerramento (último voto na seção), emissao (BU emitido pela urna),
            abertura (primeiro voto) ou recebimento (entrada na totalização). Os três primeiros são hora local
            da urna e são convertidos para Brasília por UF; o exterior (ZZ) fica de fora deles.
  snapshots 2º turno: snapshots nacionais do TSE gravados em data/apuracao.db durante a apuração (st = seções
            totalizadas, tv = votos, acumulados; resolução de 30 s). Só horário de recebimento.
  bweb      1º turno: data/1t/secoes_{UF}.csv (DT_BU_RECEBIDO por seção). Só horário de recebimento.
Padrão: bu se o banco existir; senão snapshots (2º turno) ou bweb (1º turno).

Uso:
  python analisar_fechamento.py                              # 2º turno, encerramento, janelas de 10 min
  python analisar_fechamento.py --evento recebimento         # entrada na totalização
  python analisar_fechamento.py --fonte snapshots            # força a fonte dos snapshots
  python analisar_fechamento.py --turno 1 --fonte bweb       # 1º turno pelo bweb
  python analisar_fechamento.py --intervalo 5 --detalhe --csv saida.csv
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


def janelas_bu(turno: int, intervalo: int, evento: str, incluir_exterior: bool) -> pd.DataFrame:
    import baixar_bu
    db = baixar_bu.caminho_banco(turno)
    if not db.exists():
        sys.exit(f"banco de boletins não encontrado: {db} (rode baixar_bu.py --turno {turno})")
    df = baixar_bu.BancoBU(db).dataframe()
    if df.empty:
        sys.exit("banco de boletins vazio")
    col = {"encerramento": "dt_encerramento", "emissao": "dt_emissao", "abertura": "dt_abertura",
           "recebimento": "dt_recebido"}[evento]
    ts = pd.to_datetime(df[col], errors="coerce")
    if evento != "recebimento":  # hora local da urna -> Brasília
        ts = ts + pd.to_timedelta(df["uf"].map(baixar_bu.OFFSET_UF).fillna(0).astype(int), unit="h")
        if not incluir_exterior:
            ts = ts.where(df["uf"] != "ZZ")
    df = df.assign(ts=ts).dropna(subset=["ts"])
    df["vv"] = df["v22"] + df["v13"]
    df["janela"] = df["ts"].dt.floor(f"{intervalo}min")
    g = df.groupby("janela").agg(secoes=("secao", "size"), votos=("comparecimento", "sum"), validos=("vv", "sum"),
                                 v22=("v22", "sum"), v13=("v13", "sum")).reset_index()
    g.insert(0, "inicio", g.pop("janela"))
    g.insert(1, "fim", g["inicio"] + pd.Timedelta(minutes=intervalo) - pd.Timedelta(seconds=1))
    return g


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
    ap.add_argument("--fonte", choices=("bu", "snapshots", "bweb"), help="padrão: bu se existir o banco de boletins")
    ap.add_argument("--evento", choices=("encerramento", "emissao", "abertura", "recebimento"), default="encerramento",
                    help="horário considerado (só com --fonte bu; as outras fontes só têm recebimento)")
    ap.add_argument("--incluir-exterior", action="store_true", help="fonte bu: inclui ZZ na hora local do país")
    args = ap.parse_args()

    import baixar_bu
    fonte = args.fonte or ("bu" if baixar_bu.caminho_banco(args.turno).exists()
                           else ("snapshots" if args.turno == 2 else "bweb"))
    if fonte == "bu":
        tab = janelas_bu(args.turno, args.intervalo, args.evento, args.incluir_exterior)
        exterior = "na hora local" if args.incluir_exterior else "excluído"
        descr = f"boletins de urna ({baixar_bu.caminho_banco(args.turno)}) · evento: {args.evento}" + \
            ("" if args.evento == "recebimento" else f" (hora de Brasília; exterior {exterior})")
    elif fonte == "snapshots":
        tab = janelas_2t(args.db, args.intervalo)
        descr = f"snapshots nacionais do TSE (feed a cada 30 s) em {args.db} · evento: recebimento"
    else:
        tab = janelas_1t(args.intervalo)
        descr = "boletins de urna por seção (DT_BU_RECEBIDO) em data/1t · evento: recebimento"
    print(f"{args.turno}º turno · janelas de {args.intervalo} min · fonte: {descr}\n")
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
