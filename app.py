"""Servidor web do painel de apuração.

Uso:
  python app.py                      # modo real (coleta do TSE)
  python app.py --replay             # simula o 2º turno com os dados do 1º turno
  python app.py --porta 8000 --host 0.0.0.0
"""
from __future__ import annotations

import argparse
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from flask import Flask, jsonify, request, send_from_directory

import analise
import config
import primeiro_turno
import tse_client
from storage import Storage

log = logging.getLogger("app")
app = Flask(__name__, static_folder=str(config.STATIC_DIR), static_url_path="/static")


class Estado:
    """Estado compartilhado: dados do 1º turno em memória, storage, caches."""

    def __init__(self) -> None:
        self.storage: Storage | None = None
        self.df1t = pd.DataFrame()
        self.s1t = pd.DataFrame()
        self.mun_1t = pd.DataFrame()
        self.uf_1t = pd.DataFrame()
        self.ufs_1t_carregadas: list[str] = []
        self.carregando_1t = False
        self.erro_1t = ""
        self.municipios: list[dict[str, str]] = []
        self.replay = False
        self.replay_obj = None
        self.ufs_limite: list[str] | None = None
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}
        self.inicio = config.agora_brasilia()

    # ---------------------------------------------------------------- 1º turno
    def carregar_1t(self) -> None:
        prontas = [u for u in primeiro_turno.ufs_prontas() if not self.ufs_limite or u in self.ufs_limite]
        if prontas == self.ufs_1t_carregadas:
            return
        df = primeiro_turno.carregar(prontas)
        with self._lock:
            self.df1t = df
            self.s1t = analise.serie_1t(df)
            self.mun_1t = analise.agregar_1t_mun(df)
            self.uf_1t = analise.agregar_1t_uf(df)
            self.ufs_1t_carregadas = prontas
            self._cache.clear()
        log.info("1º turno carregado: %d UF(s), %d seções", len(prontas), len(df))

    def monitorar_1t(self, baixar: bool) -> None:
        def loop():
            if baixar:
                faltam = [u for u in (self.ufs_limite or config.UFS) if not primeiro_turno.caminho_csv(u).exists()]
                if faltam:
                    self.carregando_1t = True
                    try:
                        res = primeiro_turno.preparar_todas(faltam, callback=lambda uf, r: self.carregar_1t())
                        erros = {k: v for k, v in res.items() if v != "ok"}
                        self.erro_1t = "; ".join(f"{k}: {v}" for k, v in erros.items())
                    finally:
                        self.carregando_1t = False
            while True:
                try:
                    self.carregar_1t()
                except Exception as exc:  # noqa: BLE001
                    log.exception("carregar 1T: %s", exc)
                time.sleep(30)

        self.carregar_1t()
        threading.Thread(target=loop, name="monitor-1t", daemon=True).start()

    def cache(self, chave: str, fn: Callable[[], Any], segundos: float | None = None) -> Any:
        now = time.time()
        hit = self._cache.get(chave)
        if hit and now - hit[0] < (segundos or config.CACHE_API_SEGUNDOS):
            return hit[1]
        val = fn()
        self._cache[chave] = (now, val)
        return val


E = Estado()


# -------------------------------------------------------------------- cálculo em segundo plano
class Calculadora:
    """Recalcula todas as respostas da API a cada CACHE_API_SEGUNDOS numa thread própria.

    As rotas só devolvem o último resultado pronto, então nunca bloqueiam (o cruzamento das seções
    com o 1º turno é pesado e, feito dentro da requisição, derrubava o proxy com 502).
    O cruzamento é incremental: só as seções novas desde a última rodada são processadas.
    """

    COLS_SECOES = ["rowid", "uf", "cd_mun", "zona", "secao", "visto_em", "da", "ha"]

    def __init__(self) -> None:
        self.payloads: dict[str, Any] = {}
        self.secoes = pd.DataFrame(columns=self.COLS_SECOES)
        self.acc = pd.DataFrame()
        self.ultimo_rowid = 0
        self.modo, self.motivo = "municipio", "aguardando primeiro cálculo"
        self.ultima: str | None = None
        self.duracao = 0.0
        self.em_andamento = False
        self.etapa = ""
        self.etapa_n = 0
        self.etapas_total = 7
        self.inicio_calculo: str | None = None
        self._df1t_id: int | None = None
        self._lock = threading.Lock()

    def _passo(self, n: int, nome: str) -> None:
        self.etapa_n, self.etapa = n, nome

    def progresso(self) -> dict[str, Any]:
        """Estado leve para o /api/status (sem tocar nos dados pesados)."""
        return {
            "ultima": self.ultima, "duracao": self.duracao, "em_andamento": self.em_andamento,
            "etapa": self.etapa, "etapa_n": self.etapa_n, "etapas_total": self.etapas_total,
            "inicio": self.inicio_calculo, "intervalo": config.CACHE_API_SEGUNDOS,
            "secoes_cruzadas": int(len(self.acc)) if not self.acc.empty else 0,
        }

    def iniciar(self) -> None:
        def loop():
            while True:
                try:
                    self.calcular()
                except Exception as exc:  # noqa: BLE001
                    log.exception("calculadora: %s", exc)
                time.sleep(config.CACHE_API_SEGUNDOS)
        threading.Thread(target=loop, name="calculadora", daemon=True).start()

    def _atualizar_secoes(self) -> None:
        novas = E.storage.secoes_2t_desde(self.ultimo_rowid)
        if not novas.empty:
            self.ultimo_rowid = int(novas["rowid"].max())
            self.secoes = novas if self.secoes.empty else pd.concat([self.secoes, novas], ignore_index=True)
        df1t_mudou = id(E.df1t) != self._df1t_id
        if df1t_mudou:  # 1º turno (re)carregado: refaz o cruzamento inteiro
            self._df1t_id = id(E.df1t)
            self.acc = analise.mesmas_secoes_acumulado(self.secoes, E.df1t) if not E.df1t.empty else pd.DataFrame()
        elif not novas.empty and not E.df1t.empty:
            acc_novo = analise.mesmas_secoes_acumulado(novas, E.df1t)
            if not acc_novo.empty:
                self.acc = acc_novo if self.acc.empty else pd.concat([self.acc, acc_novo], ignore_index=True)
                if not self.acc["visto_em"].is_monotonic_increasing:
                    self.acc = self.acc.sort_values("visto_em", kind="stable").reset_index(drop=True)

    def calcular(self) -> None:
        with self._lock:
            t0 = time.time()
            self.em_andamento = True
            self.inicio_calculo = config.agora_brasilia().strftime("%Y-%m-%d %H:%M:%S")
            try:
                self._passo(1, "seções novas do 2º turno")
                self._atualizar_secoes()
                self.modo, self.motivo = analise.detectar_modo(self.secoes, E.df1t)
                self._passo(2, "leitura do banco")
                br = E.storage.ultimo_snapshot("br", "br")
                snaps_br = E.storage.snapshots("br", "br")
                mesmas_mun_br = E.storage.snapshots("mesmas_mun", "br")
                ufs = E.storage.ultimos_por_codigo("uf")
                mun = E.storage.mun_atual()
                mesmas_mun_ufs = E.storage.ultimos_por_codigo("mesmas_mun")
                mesmas_sec = self.acc if self.modo == "secao" else pd.DataFrame()
                varr = E.storage.varreduras()
                p: dict[str, Any] = {"br": br or {},
                                     "ultima_varredura": varr.iloc[-1].to_dict() if not varr.empty else None}
                self._passo(3, "gráfico 1")
                p["g1"] = analise.grafico1(E.s1t, snaps_br)
                self._passo(4, "gráfico 2 (mesmas seções)")
                p["g2"] = analise.grafico2(E.df1t, snaps_br, mesmas_mun_br, self.secoes, acc=self.acc,
                                           modo_motivo=(self.modo, self.motivo))
                self._passo(5, "quadros 1, 2 e 3")
                p["quadros"] = {
                    "quadro1": analise.quadro1(E.uf_1t, ufs, br, self.modo, mesmas_sec, mesmas_mun_ufs),
                    "quadro2": analise.quadro2(br, ufs, mun),
                    "quadro3": analise.quadro3(br),
                    "modo": self.modo, "motivo": self.motivo,
                }
                self._passo(6, "mapas")
                p["mapa-2-mun"] = analise.mapa_municipios(2, E.mun_1t, mun, E.municipios)
                p["mapa-2-uf"] = analise.mapa_ufs(2, E.uf_1t, ufs)
                p["mapa-1-mun"] = analise.mapa_municipios(1, E.mun_1t, mun, E.municipios)
                p["mapa-1-uf"] = analise.mapa_ufs(1, E.uf_1t, ufs)
                self._passo(7, "publicando")
                self.payloads = p
                self.duracao = round(time.time() - t0, 2)
                self.ultima = config.agora_brasilia().strftime("%Y-%m-%d %H:%M:%S")
            finally:
                self.em_andamento = False
                self.etapa = ""

    def payload(self, chave: str) -> Any:
        if not self.payloads:
            self.calcular()
        return self.payloads.get(chave)


E.calc = Calculadora()


# -------------------------------------------------------------------- rotas
@app.route("/")
def index():
    return send_from_directory(str(config.STATIC_DIR), "index.html")


@app.route("/geo/<nome>")
def geo(nome: str):
    return send_from_directory(str(config.GEO_DIR), nome, max_age=86400)


@app.route("/api/status")
def api_status():
    def calc():
        br = E.calc.payload("br") or {}
        coletas = E.storage.coletas()
        modo, motivo = E.calc.modo, E.calc.motivo
        ultima = E.calc.payload("ultima_varredura")
        return {
            "agora": config.agora_brasilia().strftime("%Y-%m-%d %H:%M:%S"),
            "calculo": E.calc.progresso(),
            "replay_progresso": E.replay_obj.progresso() if E.replay_obj else None,
            "fuso": config.FUSO_HORARIO,
            "replay": E.replay,
            "ts_feed_br": br.get("ts_feed"),
            "coletado_em_br": br.get("coletado_em"),
            "st": int(br.get("st", 0)), "ts": int(br.get("ts", 0)), "pst": analise.pct(br.get("st", 0), br.get("ts", 0)),
            "vv": int(br.get("vv", 0)),
            "turno1": {
                "ufs_prontas": len(E.ufs_1t_carregadas), "ufs_total": len(E.ufs_limite or config.UFS),
                "secoes": int(len(E.df1t)), "carregando": E.carregando_1t, "erro": E.erro_1t,
                "faltam": [u for u in (E.ufs_limite or config.UFS) if u not in E.ufs_1t_carregadas],
            },
            "coletas": coletas,
            "ultima_varredura": ultima,
            "respostas_429": tse_client.limitador.total_429,
            "modo_grafico2": modo, "motivo_grafico2": motivo,
            "secoes_2t_vistas": int(len(E.calc.secoes)),
            "candidatos": config.CANDIDATOS, "cores": config.CORES,
            "total_eleitores": analise.total_eleitores(br)[0], "origem_total": analise.total_eleitores(br)[1],
        }
    return jsonify(E.cache("status", calc, segundos=5))


@app.route("/api/grafico1")
def api_grafico1():
    return jsonify(E.calc.payload("g1"))


@app.route("/api/grafico2")
def api_grafico2():
    return jsonify(E.calc.payload("g2"))


@app.route("/api/quadros")
def api_quadros():
    return jsonify(E.calc.payload("quadros"))


@app.route("/api/mapa")
def api_mapa():
    turno = 1 if request.args.get("turno") == "1" else 2
    nivel = "uf" if request.args.get("nivel") == "uf" else "mun"
    return jsonify(E.calc.payload(f"mapa-{turno}-{nivel}"))


# -------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--porta", type=int, default=8000)
    ap.add_argument("--replay", action="store_true", help="simula o 2º turno com os dados do 1º turno")
    ap.add_argument("--velocidade", type=float, default=60.0, help="replay: segundos virtuais por segundo real")
    ap.add_argument("--inicio", default=None, help="replay: hora virtual inicial HH:MM (padrão: 1ª urna)")
    ap.add_argument("--forcar-2-candidatos", action="store_true", help="replay: redistribui 'outros' entre 22 e 13")
    ap.add_argument("--lote", action="store_true", help="replay: publica a lista de seções só no fim (testa fallback)")
    ap.add_argument("--sem-1t", action="store_true", help="não baixar o 1º turno automaticamente")
    ap.add_argument("--ufs", help="limitar o 1º turno a estas UFs (testes), ex.: AC,RR")
    ap.add_argument("--limpar", action="store_true", help="apaga a série histórica (banco) antes de iniciar")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    config.garantir_pastas()

    E.replay = args.replay
    E.ufs_limite = [u.strip().upper() for u in args.ufs.split(",")] if args.ufs else None
    E.storage = Storage(config.DATA_DIR / f"replay_{args.porta}.db" if args.replay else config.DB_PATH)
    if args.limpar or args.replay:
        E.storage.limpar()

    # dados auxiliares (config de municípios + geojson) em background
    def auxiliares():
        for tentativa in range(5):
            try:
                E.municipios = tse_client.lista_municipios()
                tse_client.baixar_geojson()
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("auxiliares (tentativa %d): %s", tentativa + 1, exc)
                time.sleep(10)
    threading.Thread(target=auxiliares, name="auxiliares", daemon=True).start()

    E.monitorar_1t(baixar=not args.sem_1t)

    if args.replay:
        from replay import Replay
        for _ in range(60):
            if not E.df1t.empty:
                break
            time.sleep(1)
        if E.df1t.empty:
            raise SystemExit("replay: nenhum dado do 1º turno disponível (rode primeiro_turno.py)")
        E.replay_obj = Replay(E.storage, E.df1t, args.velocidade, args.forcar_2_candidatos, args.lote, args.inicio)
        E.replay_obj.iniciar()
    else:
        from coletor_2t import Coletor
        def iniciar_coletor():
            while True:
                try:
                    Coletor(E.storage, lambda: E.mun_1t).iniciar()
                    return
                except Exception as exc:  # noqa: BLE001
                    log.warning("coletor: %s (nova tentativa em 15s)", exc)
                    time.sleep(15)
        threading.Thread(target=iniciar_coletor, name="iniciar-coletor", daemon=True).start()

    E.calc.iniciar()
    log.info("painel em http://%s:%d/", "localhost" if args.host == "0.0.0.0" else args.host, args.porta)
    app.run(host=args.host, port=args.porta, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
