"""Simulação do 2º turno a partir dos dados por seção do 1º turno.

Reproduz a progressão real de recebimento das urnas (dt_recebido) em tempo acelerado, gravando no
Storage exatamente o que o coletor gravaria (snapshots br/uf/mun, mun_atual, mesmas_mun, secoes_2t).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta

import pandas as pd

import analise
import config
from storage import Storage

log = logging.getLogger(__name__)


class Replay:
    PAUSA_LOOP = 20  # s parado no resultado final antes de recomeçar (modo --loop)

    def __init__(self, storage: Storage, df1t: pd.DataFrame, velocidade: float = 60.0,
                 forcar_2: bool = False, lote: bool = False, inicio_hora: str | None = None,
                 loop: bool = False):
        """velocidade: segundos virtuais por segundo real. lote: publica as seções só no fim.
        loop: ao terminar, zera o banco e recomeça (demonstração contínua)."""
        self.storage = storage
        self.velocidade = velocidade
        self.loop = loop
        self.rodada = 1
        self.em_pausa = False
        self.forcar_2 = forcar_2
        self.lote = lote
        self._parar = threading.Event()
        df = df1t.dropna(subset=["dt_recebido"]).sort_values("dt_recebido").reset_index(drop=True).copy()
        if forcar_2:  # redistribui "outros" proporcionalmente entre 22 e 13 (eleição de 2 candidatos)
            base = (df["v22"] + df["v13"]).replace(0, 1)
            extra22 = (df["v_outros"] * df["v22"] / base).round().astype("int64")
            df["v22"] = df["v22"] + extra22
            df["v13"] = df["v13"] + (df["v_outros"] - extra22)
            df["v_outros"] = 0
            df["vv"] = df["v22"] + df["v13"]
        self.df = df
        self.mun_1t = analise.agregar_1t_mun(df)
        self.mun_tot = df.groupby(["uf", "cd_mun"]).agg(ts=("secao", "size"), te=("aptos", "sum")).reset_index()
        self.t_ini = df["dt_recebido"].min().floor("min") - timedelta(minutes=1)
        if inicio_hora:
            h, m = inicio_hora.split(":")
            self.t_ini = self.t_ini.normalize() + timedelta(hours=int(h), minutes=int(m))
        self.t_fim = df["dt_recebido"].max() + timedelta(minutes=2)
        self.t_virtual = self.t_ini
        self.thread: threading.Thread | None = None
        self.municipios_info = {}

    def iniciar(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="replay", daemon=True)
        self.thread.start()

    def parar(self) -> None:
        self._parar.set()

    def progresso(self) -> dict:
        """Avanço do relógio virtual, para o cabeçalho da interface."""
        total = (self.t_fim - self.t_ini).total_seconds() or 1
        feito = (self.t_virtual - self.t_ini).total_seconds()
        return {
            "hora_virtual": self.t_virtual.strftime("%H:%M:%S"),
            "ts_virtual": self._ts_feed(min(self.t_virtual, self.t_fim)),  # mesmo eixo de datas do ts_feed
            "pct": round(min(100.0, max(0.0, feito * 100.0 / total)), 1),
            "velocidade": self.velocidade,
            "concluido": self.t_virtual > self.t_fim,
            "loop": self.loop, "rodada": self.rodada, "em_pausa": self.em_pausa, "pausa": self.PAUSA_LOOP,
        }

    def _ts_feed(self, t: pd.Timestamp) -> str:
        delta = t - pd.Timestamp(config.DATA_1T)
        return (pd.Timestamp(config.DATA_2T) + delta).strftime("%Y-%m-%d %H:%M:%S")

    def _loop(self) -> None:
        while True:
            self._rodada()
            if not self.loop or self._parar.is_set():
                break
            log.info("replay: concluído; reinicia em %d s (modo --loop, rodada %d)", self.PAUSA_LOOP, self.rodada + 1)
            self.em_pausa = True
            self._parar.wait(self.PAUSA_LOOP)
            self.em_pausa = False
            if self._parar.is_set():
                break
            self.storage.limpar()           # zera a série; a Calculadora detecta pela "geração" do banco
            self.t_virtual = self.t_ini
            self.rodada += 1
        log.info("replay encerrado")

    def _rodada(self) -> None:
        idx = 0
        tick = 0
        acumulado = None
        col = ["v22", "v13", "v_outros", "vv", "brancos", "nulos", "aptos", "comparecimento"]
        log.info("replay: %s -> %s, velocidade %.0fx", self.t_ini, self.t_fim, self.velocidade)
        while not self._parar.is_set() and self.t_virtual <= self.t_fim:
            t0 = time.time()
            t = self.t_virtual
            fim = self.df["dt_recebido"].searchsorted(t, side="right")
            novos = self.df.iloc[idx:fim]
            idx = fim
            if not novos.empty or acumulado is None:
                g = novos.groupby(["uf", "cd_mun"])[col].sum()
                g["st"] = novos.groupby(["uf", "cd_mun"]).size()
                acumulado = g if acumulado is None else acumulado.add(g, fill_value=0)
            self._gravar(t, acumulado, tick)
            if not self.lote and not novos.empty:
                self.storage.gravar_secoes(
                    [(r.uf, r.cd_mun, r.zona, r.secao, t.strftime("%d/%m/%Y"), t.strftime("%H:%M:%S"))
                     for r in novos.itertuples(index=False)])
            tick += 1
            self.t_virtual = t + timedelta(seconds=self.velocidade)
            time.sleep(max(0.0, 1.0 - (time.time() - t0)))
        if self.lote and not self._parar.is_set():
            t = self.t_virtual
            self.storage.gravar_secoes(
                [(r.uf, r.cd_mun, r.zona, r.secao, t.strftime("%d/%m/%Y"), t.strftime("%H:%M:%S"))
                 for r in self.df.itertuples(index=False)])
        log.info("replay concluído")

    def _gravar(self, t: pd.Timestamp, acumulado: pd.DataFrame, tick: int) -> None:
        ts_feed = self._ts_feed(t)
        m = self.mun_tot.merge(acumulado.reset_index(), on=["uf", "cd_mun"], how="left").fillna(0)
        m["est"] = m["aptos"]
        m["esnt"] = m["te"] - m["est"]
        m["c"] = m["comparecimento"]
        m["a"] = m["est"] - m["c"]
        m["tv"] = m["c"]
        m["vb"] = m["brancos"]
        m["vn"] = m["nulos"]
        registros = []
        for r in m.itertuples(index=False):
            d = {"codigo": r.cd_mun, "uf": r.uf, "nome": "", "cdi": "", "ts_feed": ts_feed,
                 "v22": r.v22, "v13": r.v13, "v_outros": r.v_outros, "vv": r.vv, "vb": r.vb, "vn": r.vn,
                 "tv": r.tv, "ts": r.ts, "st": r.st, "te": r.te, "est": r.est, "esnt": r.esnt, "c": r.c, "a": r.a}
            registros.append(d)
        self.storage.gravar_mun_atual(registros)
        if tick % 5 == 0:
            self.storage.gravar_snapshots("mun", [(d["codigo"], d) for d in registros])
        campos = ["v22", "v13", "v_outros", "vv", "vb", "vn", "tv", "ts", "st", "te", "est", "esnt", "c", "a"]
        por_uf = m.groupby("uf")[campos].sum()
        itens = [(uf, {"ts_feed": ts_feed, **{c: int(r[c]) for c in campos}}) for uf, r in por_uf.iterrows()]
        self.storage.gravar_snapshots("uf", itens)
        tot = por_uf.sum()
        self.storage.gravar_snapshot("br", "br", {"ts_feed": ts_feed, **{c: int(tot[c]) for c in campos}})
        # "mesmas seções" por município (proporcional), como o coletor faz
        j = m[["uf", "cd_mun", "st", "ts"]].merge(self.mun_1t[["cd_mun", *analise.VOTOS]], on="cd_mun", how="inner")
        fator = j["st"] / j["ts"].replace(0, 1)
        for c in analise.VOTOS:
            j[c] = j[c] * fator
        g = j.groupby("uf")[[*analise.VOTOS, "st"]].sum()
        itens = [(uf, {"ts_feed": ts_feed, **{c: int(r[c]) for c in [*analise.VOTOS, "st"]}}) for uf, r in g.iterrows()]
        tg = g.sum()
        itens.append(("br", {"ts_feed": ts_feed, **{c: int(tg[c]) for c in [*analise.VOTOS, "st"]}}))
        self.storage.gravar_snapshots("mesmas_mun", itens)
        self.storage.registrar_coleta("nacional", True, "replay", 0)
        self.storage.registrar_coleta("municipios", True, "replay", 0)
        self.storage.registrar_coleta("secoes", True, "replay", 0)
        self.storage.registrar_varredura(ts_feed, len(registros), 0, ts_feed)
