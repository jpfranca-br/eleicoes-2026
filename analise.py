"""Cálculos dos gráficos e quadros. Funções puras sobre DataFrames (sem I/O)."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

import config

CHAVE_SECAO = ["uf", "cd_mun", "zona", "secao"]
VOTOS = ["v22", "v13", "v_outros", "vv"]


# ------------------------------------------------------------------ utilidades
def pct(v, vv) -> float:
    return round(float(v) * 100.0 / float(vv), 4) if vv else 0.0


def pct_list(v: pd.Series | np.ndarray, vv: pd.Series | np.ndarray) -> list[float]:
    v = np.asarray(v, dtype=float)
    vv = np.asarray(vv, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        p = np.where(vv > 0, v * 100.0 / vv, 0.0)
    return [round(float(x), 4) for x in p]


def eixo_comum(ts: pd.Series, data_base: str) -> list[str]:
    """Projeta horários de um dia qualquer no dia do 2º turno (eixo x comum, com virada de dia)."""
    ts = pd.to_datetime(ts)
    delta = ts - pd.Timestamp(data_base)
    return (pd.Timestamp(config.DATA_2T) + delta).dt.strftime("%Y-%m-%dT%H:%M:%S").tolist()


def _fmt_ts(ts: pd.Series) -> list[str]:
    return pd.to_datetime(ts).dt.strftime("%d/%m %H:%M:%S").tolist()


def regiao_da_uf(uf: str) -> str:
    return config.UF_REGIAO.get(str(uf).upper(), "Exterior")


# ------------------------------------------------------------------ 1º turno
def serie_1t(df1t: pd.DataFrame) -> pd.DataFrame:
    """Acumulado por minuto (ordem de recebimento das seções)."""
    if df1t.empty:
        return pd.DataFrame(columns=["ts", *VOTOS, "secoes"])
    d = df1t.dropna(subset=["dt_recebido"]).copy()
    d["ts"] = d["dt_recebido"].dt.floor("min")
    g = d.groupby("ts").agg(v22=("v22", "sum"), v13=("v13", "sum"), v_outros=("v_outros", "sum"),
                            vv=("vv", "sum"), secoes=("secao", "size")).sort_index()
    return g.cumsum().reset_index()


def agregar_1t_mun(df1t: pd.DataFrame) -> pd.DataFrame:
    """Totais do 1º turno por município (codigo = uf+cd_mun ... usamos cd_mun, que é único no TSE)."""
    if df1t.empty:
        return pd.DataFrame(columns=["uf", "cd_mun", *VOTOS, "brancos", "nulos", "aptos", "comparecimento", "secoes"])
    g = df1t.groupby(["uf", "cd_mun"]).agg(
        v22=("v22", "sum"), v13=("v13", "sum"), v_outros=("v_outros", "sum"), vv=("vv", "sum"),
        brancos=("brancos", "sum"), nulos=("nulos", "sum"), aptos=("aptos", "sum"),
        comparecimento=("comparecimento", "sum"), secoes=("secao", "size")).reset_index()
    return g


def agregar_1t_uf(df1t: pd.DataFrame) -> pd.DataFrame:
    if df1t.empty:
        return pd.DataFrame(columns=["uf", *VOTOS, "brancos", "nulos", "aptos", "comparecimento", "secoes"])
    return df1t.groupby("uf").agg(
        v22=("v22", "sum"), v13=("v13", "sum"), v_outros=("v_outros", "sum"), vv=("vv", "sum"),
        brancos=("brancos", "sum"), nulos=("nulos", "sum"), aptos=("aptos", "sum"),
        comparecimento=("comparecimento", "sum"), secoes=("secao", "size")).reset_index()


# ------------------------------------------------------------------ Gráfico 1
def _serie_dict(x: list[str], hora: list[str], v22, v13, v_outros, vv, secoes) -> dict[str, Any]:
    return {
        "x": x, "hora": hora,
        "v22": [int(v) for v in v22], "v13": [int(v) for v in v13], "v_outros": [int(v) for v in v_outros],
        "vv": [int(v) for v in vv], "secoes": [int(v) for v in secoes],
        "p22": pct_list(v22, vv), "p13": pct_list(v13, vv), "p_outros": pct_list(v_outros, vv),
    }


def grafico1(s1t: pd.DataFrame, snaps_br: pd.DataFrame) -> dict[str, Any]:
    t1 = _serie_dict([], [], [], [], [], [], []) if s1t.empty else _serie_dict(
        eixo_comum(s1t["ts"], config.DATA_1T), _fmt_ts(s1t["ts"]),
        s1t["v22"], s1t["v13"], s1t["v_outros"], s1t["vv"], s1t["secoes"])
    if snaps_br.empty:
        t2 = _serie_dict([], [], [], [], [], [], [])
    else:
        s = snaps_br[snaps_br["vv"] > 0]
        t2 = _serie_dict(eixo_comum(s["ts_feed"], config.DATA_2T), _fmt_ts(s["ts_feed"]),
                         s["v22"], s["v13"], s["v_outros"], s["vv"], s["st"])
    return {"turno1": t1, "turno2": t2}


# ------------------------------------------------------------------ Gráfico 2
def detectar_modo(secoes_2t: pd.DataFrame, df1t: pd.DataFrame) -> tuple[str, str]:
    """('secao'|'municipio', motivo)."""
    if secoes_2t is None or secoes_2t.empty:
        return "municipio", "listagem de seções do 2º turno ainda vazia"
    if df1t.empty:
        return "municipio", "1º turno ainda não carregado"
    tot = df1t.groupby("uf").size()
    por_coleta = secoes_2t.groupby(["uf", "visto_em"]).size().reset_index(name="n")
    por_coleta["frac"] = por_coleta["n"] / por_coleta["uf"].map(tot).replace(0, np.nan)
    lote = por_coleta[por_coleta["frac"] >= config.LIMIAR_LOTE]
    if not lote.empty:
        ufs = ", ".join(sorted(lote["uf"].unique())[:6])
        return "municipio", f"listagem de seções publicada em lote ({ufs})"
    return "secao", f"{len(secoes_2t):,} seções vistas de forma incremental".replace(",", ".")


def mesmas_secoes_acumulado(secoes_2t: pd.DataFrame, df1t: pd.DataFrame) -> pd.DataFrame:
    """Acumulado dos votos do 1º turno das seções já vistas no 2º turno, na ordem em que foram vistas."""
    cols = ["visto_em", *VOTOS, "n", "uf"]
    if secoes_2t.empty or df1t.empty:
        return pd.DataFrame(columns=cols)
    s = secoes_2t.merge(df1t[[*CHAVE_SECAO, *VOTOS]], on=CHAVE_SECAO, how="inner")
    s = s.sort_values("visto_em").reset_index(drop=True)
    s["n"] = 1
    return s[cols]


def grafico2(df1t: pd.DataFrame, snaps_br: pd.DataFrame, mesmas_mun_br: pd.DataFrame,
             secoes_2t: pd.DataFrame) -> dict[str, Any]:
    modo, motivo = detectar_modo(secoes_2t, df1t)
    vazio = {"modo": modo, "motivo": motivo, "x": [], "hora": [], "turno2": {}, "turno1": {}}
    if snaps_br.empty:
        return vazio
    s2 = snaps_br[snaps_br["vv"] > 0].copy()
    if s2.empty:
        return vazio
    s2["coletado_em"] = pd.to_datetime(s2["coletado_em"])
    s2 = s2.sort_values("ts_feed")  # eixo x = hora do TSE; coletado_em só para casar com o lado do 1º turno

    def asof(tempos_ref: np.ndarray, cum: pd.DataFrame, cols: list[str]) -> dict[str, np.ndarray]:
        """Para cada coletado_em do 2º turno, último valor acumulado com tempo_ref <= coletado_em."""
        idx = np.searchsorted(tempos_ref, s2["coletado_em"].values, side="right") - 1
        ok = idx >= 0
        return {c: np.where(ok, cum[c].values[np.clip(idx, 0, None)], 0) for c in cols}

    if modo == "secao":
        acc = mesmas_secoes_acumulado(secoes_2t, df1t)
        if acc.empty:
            modo, motivo = "municipio", "seções vistas não encontradas no 1º turno"
    if modo == "secao":
        acc["visto_em"] = pd.to_datetime(acc["visto_em"])
        t1 = asof(acc["visto_em"].values, acc[[*VOTOS, "n"]].cumsum(), [*VOTOS, "n"])
    else:
        if mesmas_mun_br is None or mesmas_mun_br.empty:
            return {**vazio, "modo": modo, "motivo": motivo}
        m = mesmas_mun_br.copy()
        m["coletado_em"] = pd.to_datetime(m["coletado_em"])
        m = m.sort_values("coletado_em").rename(columns={"st": "n"})
        t1 = asof(m["coletado_em"].values, m, [*VOTOS, "n"])

    return {
        "modo": modo, "motivo": motivo,
        "x": eixo_comum(s2["ts_feed"], config.DATA_2T), "hora": _fmt_ts(s2["ts_feed"]),
        "turno2": {
            "v22": s2["v22"].astype(int).tolist(), "v13": s2["v13"].astype(int).tolist(),
            "vv": s2["vv"].astype(int).tolist(), "secoes": s2["st"].astype(int).tolist(),
            "p22": pct_list(s2["v22"], s2["vv"]), "p13": pct_list(s2["v13"], s2["vv"]),
        },
        "turno1": {
            "v22": [int(v) for v in t1["v22"]], "v13": [int(v) for v in t1["v13"]],
            "v_outros": [int(v) for v in t1["v_outros"]], "vv": [int(v) for v in t1["vv"]],
            "secoes": [int(v) for v in t1["n"]],
            "p22": pct_list(t1["v22"], t1["vv"]), "p13": pct_list(t1["v13"], t1["vv"]),
            "p_outros": pct_list(t1["v_outros"], t1["vv"]),
        },
    }


# ------------------------------------------------------------------ Gráfico 3 (mapa)
def _lider(v22: float, v13: float, v_outros: float = 0) -> str | None:
    m = max(v22, v13, v_outros)
    if m <= 0:
        return None
    if v22 == m and v13 == m:
        return "empate"
    return "22" if v22 == m else ("13" if v13 == m else config.OUTROS)


def mapa_municipios(turno: int, mun_1t: pd.DataFrame, mun_atual: pd.DataFrame,
                    municipios: list[dict[str, str]]) -> dict[str, Any]:
    """{cdi_ibge: {...}} para o GeoJSON de municípios."""
    out: dict[str, Any] = {}
    if turno == 1:
        base = mun_1t.copy()
        if base.empty:
            return out
        base["st"] = base["secoes"]
        base["ts"] = base["secoes"]
    else:
        base = mun_atual.rename(columns={"codigo": "cd_mun"}).copy()
        if base.empty:
            return out
    info = {m["cd"]: m for m in municipios}
    for r in base.itertuples(index=False):
        cd = getattr(r, "cd_mun")
        i = info.get(cd)
        if not i or not i["cdi"]:
            continue
        v22, v13, vo, vv = int(r.v22), int(r.v13), int(r.v_outros), int(r.vv)
        out[i["cdi"]] = {
            "nome": i["nome"], "uf": i["uf"], "lider": _lider(v22, v13, vo),
            "v22": v22, "v13": v13, "v_outros": vo, "vv": vv,
            "p22": pct(v22, vv), "p13": pct(v13, vv), "p_outros": pct(vo, vv),
            "st": int(r.st), "ts": int(r.ts), "pst": pct(r.st, r.ts),
        }
    return out


def mapa_ufs(turno: int, uf_1t: pd.DataFrame, ufs_atual: pd.DataFrame) -> dict[str, Any]:
    """{cod_ibge_2dig: {...}} para o GeoJSON de UFs."""
    out: dict[str, Any] = {}
    uf_ibge = {v: k for k, v in config.IBGE_UF.items()}
    if turno == 1:
        base = uf_1t.copy()
        if base.empty:
            return out
        base["st"] = base["secoes"]
        base["ts"] = base["secoes"]
    else:
        base = ufs_atual.rename(columns={"codigo": "uf"}).copy()
        if base.empty:
            return out
    for r in base.itertuples(index=False):
        cod = uf_ibge.get(str(r.uf).upper())
        if not cod:
            continue
        v22, v13, vo, vv = int(r.v22), int(r.v13), int(r.v_outros), int(r.vv)
        out[cod] = {
            "nome": str(r.uf).upper(), "uf": str(r.uf).upper(), "lider": _lider(v22, v13, vo),
            "v22": v22, "v13": v13, "v_outros": vo, "vv": vv,
            "p22": pct(v22, vv), "p13": pct(v13, vv), "p_outros": pct(vo, vv),
            "st": int(r.st), "ts": int(r.ts), "pst": pct(r.st, r.ts),
        }
    return out


# ------------------------------------------------------------------ Quadro 1
def _linha(v22=0, v13=0, v_outros=0, vv=0, vb=0, vn=0, tv=0, st=0, ts=0) -> dict[str, Any]:
    return {
        "v22": int(v22), "v13": int(v13), "v_outros": int(v_outros), "vv": int(vv),
        "p22": pct(v22, vv), "p13": pct(v13, vv), "p_outros": pct(v_outros, vv),
        "brancos": int(vb), "nulos": int(vn), "total_apurado": int(tv) if tv else int(vv + vb + vn),
        "secoes": int(st), "secoes_total": int(ts), "pst": pct(st, ts),
    }


def quadro1(uf_1t: pd.DataFrame, ufs_atual: pd.DataFrame, br_atual: dict | None,
            modo: str, mesmas_sec: pd.DataFrame, mesmas_mun_ufs: pd.DataFrame) -> dict[str, Any]:
    regioes = [*config.ORDEM_REGIOES, "Brasil"]
    linhas: dict[str, dict[str, Any]] = {r: {"regiao": r} for r in regioes}

    # 1º turno completo
    if not uf_1t.empty:
        u = uf_1t.copy()
        u["regiao"] = u["uf"].map(regiao_da_uf)
        g = u.groupby("regiao").sum(numeric_only=True)
        tot = u.sum(numeric_only=True)
        for r in regioes:
            src = tot if r == "Brasil" else (g.loc[r] if r in g.index else None)
            linhas[r]["turno1"] = _linha() if src is None else _linha(
                src["v22"], src["v13"], src["v_outros"], src["vv"], src["brancos"], src["nulos"],
                src["comparecimento"], src["secoes"], src["secoes"])
    else:
        for r in regioes:
            linhas[r]["turno1"] = _linha()

    # 2º turno apurado
    if not ufs_atual.empty:
        u = ufs_atual.rename(columns={"codigo": "uf"}).copy()
        u["regiao"] = u["uf"].map(regiao_da_uf)
        g = u.groupby("regiao").sum(numeric_only=True)
        for r in config.ORDEM_REGIOES:
            src = g.loc[r] if r in g.index else None
            linhas[r]["turno2"] = _linha() if src is None else _linha(
                src["v22"], src["v13"], src["v_outros"], src["vv"], src["vb"], src["vn"], src["tv"], src["st"], src["ts"])
    else:
        for r in config.ORDEM_REGIOES:
            linhas[r]["turno2"] = _linha()
    b = br_atual or {}
    linhas["Brasil"]["turno2"] = _linha(b.get("v22", 0), b.get("v13", 0), b.get("v_outros", 0), b.get("vv", 0),
                                        b.get("vb", 0), b.get("vn", 0), b.get("tv", 0), b.get("st", 0), b.get("ts", 0))

    # 1º turno nas mesmas seções (modo seção: soma direta; modo município: proporcional)
    if modo == "secao" and not mesmas_sec.empty:
        m = mesmas_sec.copy()
        m["regiao"] = m["uf"].map(regiao_da_uf)
        g = m.groupby("regiao")[[*VOTOS, "n"]].sum()
        tot = m[[*VOTOS, "n"]].sum()
    elif not mesmas_mun_ufs.empty:
        m = mesmas_mun_ufs.rename(columns={"codigo": "uf"}).copy()
        m = m[m["uf"] != "br"]
        m["regiao"] = m["uf"].str.upper().map(regiao_da_uf)
        m["n"] = m["st"]
        g = m.groupby("regiao")[[*VOTOS, "n"]].sum()
        tot = m[[*VOTOS, "n"]].sum()
    else:
        g, tot = pd.DataFrame(), None
    for r in regioes:
        src = tot if (r == "Brasil" and tot is not None) else (g.loc[r] if (not g.empty and r in g.index) else None)
        linhas[r]["mesmas"] = _linha() if src is None else _linha(
            src["v22"], src["v13"], src["v_outros"], src["vv"], st=src["n"], ts=src["n"])
        t2, ms = linhas[r]["turno2"], linhas[r]["mesmas"]
        linhas[r]["delta22"] = round(t2["p22"] - ms["p22"], 2) if ms["vv"] and t2["vv"] else None
        linhas[r]["delta13"] = round(t2["p13"] - ms["p13"], 2) if ms["vv"] and t2["vv"] else None
    return {"modo": modo, "linhas": [linhas[r] for r in regioes]}


def total_eleitores(br: dict | None) -> tuple[int, str]:
    """(eleitorado apto, origem). Usa o e.te do TSE, salvo valor fixo em config."""
    if config.TOTAL_ELEITORES:
        return int(config.TOTAL_ELEITORES), "fixo (config.py)"
    te = int((br or {}).get("te") or 0)
    if te > 0:
        return te, "TSE (campo e.te)"
    return int(config.TOTAL_ELEITORES_RESERVA), "reserva (feed ainda sem dados)"


# ------------------------------------------------------------------ Quadro 2
def quadro2(br: dict | None, ufs_atual: pd.DataFrame, mun_atual: pd.DataFrame) -> dict[str, Any]:
    total, origem_total = total_eleitores(br)
    br = br or {}
    st, ts = int(br.get("st", 0)), int(br.get("ts", 0))
    frac = st / ts if ts else 0.0
    est = int(br.get("est", 0))
    out: dict[str, Any] = {
        "total_eleitores": total, "origem_total": origem_total, "restantes": max(0, total - est),
        "eleitores_apurados": est,
        "pst": pct(st, ts), "secoes_apuradas": st, "secoes_total": ts,
        "apurado": _linha(br.get("v22", 0), br.get("v13", 0), br.get("v_outros", 0), br.get("vv", 0),
                          br.get("vb", 0), br.get("vn", 0), br.get("tv", 0), st, ts),
        "simples": None, "por_municipio": None,
    }
    if frac > 0:
        v22, v13, vv = br.get("v22", 0) / frac, br.get("v13", 0) / frac, br.get("vv", 0) / frac
        out["simples"] = {"v22": int(v22), "v13": int(v13), "vv": int(vv), "p22": pct(v22, vv), "p13": pct(v13, vv),
                          "vencedor": _lider(v22, v13)}
    if mun_atual is not None and not mun_atual.empty and frac > 0:
        m = mun_atual.copy()
        for c in ("v22", "v13", "vv", "st", "ts", "te", "est"):
            m[c] = pd.to_numeric(m[c], errors="coerce").fillna(0)
        m["uf"] = m["uf"].str.upper()
        # taxas por UF (válidos por eleitor nas seções apuradas; shares)
        u = ufs_atual.rename(columns={"codigo": "uf"}).copy() if not ufs_atual.empty else pd.DataFrame()
        taxa_uf, s22_uf, s13_uf = {}, {}, {}
        if not u.empty:
            u["uf"] = u["uf"].str.upper()
            for r in u.itertuples(index=False):
                if r.est > 0 and r.vv > 0:
                    taxa_uf[r.uf] = r.vv / r.est
                    s22_uf[r.uf] = r.v22 / r.vv
                    s13_uf[r.uf] = r.v13 / r.vv
        taxa_br = br["vv"] / est if est else 0.0
        s22_br = br.get("v22", 0) / br["vv"] if br.get("vv") else 0.5
        s13_br = br.get("v13", 0) / br["vv"] if br.get("vv") else 0.5
        com = m[m["st"] > 0]
        fator = com["ts"] / com["st"]
        p22 = float((com["v22"] * fator).sum())
        p13 = float((com["v13"] * fator).sum())
        pvv = float((com["vv"] * fator).sum())
        sem = m[m["st"] <= 0]
        if not sem.empty:
            taxa = sem["uf"].map(taxa_uf).fillna(taxa_br)
            vv_s = sem["te"] * taxa
            p22 += float((vv_s * sem["uf"].map(s22_uf).fillna(s22_br)).sum())
            p13 += float((vv_s * sem["uf"].map(s13_uf).fillna(s13_br)).sum())
            pvv += float(vv_s.sum())
        out["por_municipio"] = {
            "v22": int(p22), "v13": int(p13), "vv": int(pvv), "p22": pct(p22, pvv), "p13": pct(p13, pvv),
            "vencedor": _lider(p22, p13), "municipios_com_dados": int(len(com)), "municipios_sem_dados": int(len(sem)),
        }
    return out


# ------------------------------------------------------------------ Quadro 3
def quadro3(br: dict | None) -> dict[str, Any]:
    br = br or {}
    vv = int(br.get("vv", 0))
    est = int(br.get("est", 0))
    total, origem_total = total_eleitores(br)
    restantes = max(0, total - est)
    meta = (vv + restantes) // 2 + 1  # 50% + 1 dos válidos possíveis (pior caso)
    faltam = {c: int(meta - int(br.get(f"v{c}", 0))) for c in ("22", "13")}
    eleito = next((c for c, f in faltam.items() if f <= 0), None)
    return {
        "eleito": eleito, "nome_eleito": config.CANDIDATOS.get(eleito) if eleito else None,
        "faltam": {c: max(0, f) for c, f in faltam.items()},
        "votos": {c: int(br.get(f"v{c}", 0)) for c in ("22", "13")},
        "restantes": restantes, "meta": int(meta), "vv": vv, "ts_feed": br.get("ts_feed"),
        "total_eleitores": total, "origem_total": origem_total,
    }
