"""Coleta contínua dos dados oficiais do 2º turno (resultados.tse.jus.br).

Thread A (nacional): br + 27 UFs + ZZ a cada INTERVALO_NACIONAL s.
Thread B (municípios): varredura de todos os -u.json municipais em loop (~90 s por volta).
Thread C (seções): listagem cs.json por UF a cada INTERVALO_SECOES s; registra quando cada seção apareceu.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import pandas as pd

import analise
import config
import tse_client
from storage import Storage, agora

log = logging.getLogger(__name__)


def _milhar(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _progresso(feitos: int, total: int, erros: int) -> str:
    texto = f"{_milhar(feitos)}/{_milhar(total)} nesta volta"
    return texto + (f", {erros} erro(s)" if erros else "")


class Coletor:
    def __init__(self, storage: Storage, mun_1t_getter: Callable[[], pd.DataFrame]):
        self.storage = storage
        self.mun_1t_getter = mun_1t_getter
        self.municipios = tse_client.lista_municipios()
        self._parar = threading.Event()
        self._nacional_livre = threading.Event()
        self._nacional_livre.set()
        self._secoes_vistas: set[tuple[str, str, str, str]] | None = None
        self.threads: list[threading.Thread] = []

    # ------------------------------------------------------------ ciclo de vida
    def iniciar(self) -> None:
        for nome, alvo in (("nacional", self._loop_nacional), ("municipios", self._loop_municipios),
                           ("secoes", self._loop_secoes)):
            t = threading.Thread(target=alvo, name=f"coletor-{nome}", daemon=True)
            t.start()
            self.threads.append(t)

    def parar(self) -> None:
        self._parar.set()

    def _dormir(self, segundos: float) -> None:
        self._parar.wait(segundos)

    # ------------------------------------------------------------ Thread A
    def _loop_nacional(self) -> None:
        while not self._parar.is_set():
            t0 = time.time()
            try:
                novos = self.coletar_nacional()
                self.storage.registrar_coleta("nacional", True, f"{novos} snapshot(s) novo(s)", time.time() - t0)
            except Exception as exc:  # noqa: BLE001
                log.warning("nacional: %s", exc)
                self.storage.registrar_coleta("nacional", False, str(exc)[:200])
            self._dormir(max(1.0, config.INTERVALO_NACIONAL - (time.time() - t0)))

    def coletar_nacional(self) -> int:
        alvos = [("br", "br")] + [("uf", uf) for uf in config.UFS]

        def buscar(item):
            nivel, cod = item
            d = tse_client.parse_u_json(tse_client.get_json(config.url_u(config.ELEICAO_2T, cod)))
            return nivel, cod, d

        novos = 0
        self._nacional_livre.clear()  # pausa a varredura municipal enquanto busca br/UFs
        try:
            with ThreadPoolExecutor(max_workers=4) as ex:
                for nivel, cod, d in ex.map(buscar, alvos):
                    if self.storage.gravar_snapshot(nivel, cod, d):
                        novos += 1
        finally:
            self._nacional_livre.set()
        return novos

    # ------------------------------------------------------------ Thread B
    def _loop_municipios(self) -> None:
        while not self._parar.is_set():
            t0 = time.time()
            try:
                n_ok, n_err = self.varrer_municipios()
                msg = f"volta completa: {_milhar(n_ok)} ok, {n_err} erro(s)" if n_ok or n_err else "todos os municípios 100% apurados"
                self.storage.registrar_coleta("municipios", True, msg, time.time() - t0)
            except Exception as exc:  # noqa: BLE001
                log.warning("municipios: %s", exc)
                self.storage.registrar_coleta("municipios", False, str(exc)[:200])
            self._dormir(5)

    def varrer_municipios(self) -> tuple[int, int]:
        inicio = agora()
        resultados: list[dict] = []
        erros = 0

        def buscar(m):
            # prioridade para a coleta nacional/UF: a varredura pausa enquanto ela roda
            self._nacional_livre.wait()
            if tse_client.limitador.em_penalidade(config.PAUSA_APOS_429):
                self._parar.wait(config.PAUSA_APOS_429)
            if self._parar.is_set():
                return None
            try:
                d = tse_client.parse_u_json(tse_client.get_json(config.url_u(config.ELEICAO_2T, m["uf"], m["cd"]), tentativas=3))
                d.update({"codigo": m["cd"], "uf": m["uf"], "nome": m["nome"], "cdi": m["cdi"]})
                return d
            except Exception as exc:  # noqa: BLE001
                log.debug("mun %s-%s: %s", m["uf"], m["cd"], exc)
                return None

        # municípios já 100% totalizados não mudam mais: não são consultados de novo
        atual = self.storage.mun_atual()
        concluidos = set(atual.loc[(atual["ts"] > 0) & (atual["st"] >= atual["ts"]), "codigo"]) if not atual.empty else set()
        pendentes = [m for m in self.municipios if m["cd"] not in concluidos]
        if not pendentes:
            self._parar.wait(60)
            return 0, 0
        log.info("varredura municipal: %d pendente(s), %d concluído(s)", len(pendentes), len(concluidos))
        total = len(pendentes)
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=config.THREADS_MUNICIPIOS) as ex:
            for d in ex.map(buscar, pendentes):
                if self._parar.is_set():
                    break
                if d is None:
                    erros += 1
                else:
                    resultados.append(d)
                    if len(resultados) % 500 == 0:  # grava parcial para o mapa ir aparecendo
                        self.storage.gravar_mun_atual(resultados[-500:])
                feitos = len(resultados) + erros
                if feitos % 500 == 0 and feitos < total:
                    # progresso parcial: mantém o "último sucesso" recente durante voltas longas (16-25 min)
                    self.storage.registrar_coleta("municipios", True, _progresso(feitos, total, erros), time.time() - t0)
        if resultados:
            self.storage.gravar_mun_atual(resultados)
            self.storage.gravar_snapshots("mun", [(d["codigo"], d) for d in resultados])
            ts_max = max((d["ts_feed"] or "" for d in resultados), default=None) or None
            self._gravar_mesmas_mun(ts_max)
            self.storage.registrar_varredura(inicio, len(resultados), erros, ts_max)
        return len(resultados), erros

    def _gravar_mesmas_mun(self, ts_feed: str | None) -> None:
        """1º turno 'mesmas seções' aproximado por município: votos1T × st/ts, por UF e Brasil."""
        mun_1t = self.mun_1t_getter()
        if mun_1t is None or mun_1t.empty or not ts_feed:
            return
        atual = self.storage.mun_atual()
        if atual.empty:
            return
        m = atual[["codigo", "uf", "st", "ts"]].merge(
            mun_1t.rename(columns={"cd_mun": "codigo"})[["codigo", *analise.VOTOS]], on="codigo", how="inner")
        m = m[m["ts"] > 0]
        fator = m["st"] / m["ts"]
        for c in analise.VOTOS:
            m[c] = m[c] * fator
        m["uf"] = m["uf"].str.upper()
        g = m.groupby("uf")[[*analise.VOTOS, "st"]].sum()
        itens = []
        for uf, r in g.iterrows():
            itens.append((uf, {"ts_feed": ts_feed, **{c: int(r[c]) for c in analise.VOTOS}, "st": int(r["st"])}))
        tot = g.sum()
        itens.append(("br", {"ts_feed": ts_feed, **{c: int(tot[c]) for c in analise.VOTOS}, "st": int(tot["st"])}))
        self.storage.gravar_snapshots("mesmas_mun", itens)

    # ------------------------------------------------------------ Thread C
    def _loop_secoes(self) -> None:
        while not self._parar.is_set():
            t0 = time.time()
            try:
                novas, erros = self.coletar_secoes()
                self.storage.registrar_coleta("secoes", True, f"{novas} seção(ões) nova(s), {erros} UF(s) com erro",
                                              time.time() - t0)
            except Exception as exc:  # noqa: BLE001
                log.warning("secoes: %s", exc)
                self.storage.registrar_coleta("secoes", False, str(exc)[:200])
            self._dormir(max(1.0, config.INTERVALO_SECOES - (time.time() - t0)))

    def coletar_secoes(self) -> tuple[int, int]:
        if self._secoes_vistas is None:
            self._secoes_vistas = self.storage.secoes_2t_chaves()

        def buscar(uf):
            try:
                return uf, tse_client.get_json(config.url_cs(config.PLEITO_2T, uf), timeout=60)
            except Exception as exc:  # noqa: BLE001
                log.debug("cs %s: %s", uf, exc)
                return uf, None

        novas: list[tuple[str, str, str, str, str, str]] = []
        erros = 0
        with ThreadPoolExecutor(max_workers=3) as ex:
            for uf, d in ex.map(buscar, config.UFS):
                if d is None:
                    erros += 1
                    continue
                for abr in d.get("abr") or []:
                    for m in abr.get("mu") or []:
                        cd_mun = str(m["cd"]).zfill(5)
                        for zn in m.get("zon") or []:
                            zona = str(zn["cd"]).zfill(4)
                            for s in zn.get("sec") or []:
                                if "da" not in s and "ha" not in s:
                                    continue  # ainda sem boletim publicado
                                chave = (uf, cd_mun, zona, str(s["ns"]).zfill(4))
                                if chave in self._secoes_vistas:
                                    continue
                                self._secoes_vistas.add(chave)
                                novas.append((*chave, s.get("da", ""), s.get("ha", "")))
        n = self.storage.gravar_secoes(novas) if novas else 0
        return n, erros
