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

    def cache(self, chave: str, fn: Callable[[], Any]) -> Any:
        now = time.time()
        hit = self._cache.get(chave)
        if hit and now - hit[0] < config.CACHE_API_SEGUNDOS:
            return hit[1]
        val = fn()
        self._cache[chave] = (now, val)
        return val


E = Estado()


# -------------------------------------------------------------------- helpers
def _br_atual() -> dict | None:
    return E.storage.ultimo_snapshot("br", "br")


def _modo_e_mesmas() -> tuple[str, str, pd.DataFrame]:
    secoes = E.storage.secoes_2t()
    modo, motivo = analise.detectar_modo(secoes, E.df1t)
    mesmas_sec = analise.mesmas_secoes_acumulado(secoes, E.df1t) if modo == "secao" else pd.DataFrame()
    return modo, motivo, mesmas_sec


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
        br = _br_atual() or {}
        coletas = E.storage.coletas()
        modo, motivo, _ = _modo_e_mesmas()
        varr = E.storage.varreduras()
        ultima = varr.iloc[-1].to_dict() if not varr.empty else None
        return {
            "agora": config.agora_brasilia().strftime("%Y-%m-%d %H:%M:%S"),
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
            "secoes_2t_vistas": int(len(E.storage.secoes_2t_chaves())),
            "candidatos": config.CANDIDATOS, "cores": config.CORES,
            "total_eleitores": analise.total_eleitores(br)[0], "origem_total": analise.total_eleitores(br)[1],
        }
    return jsonify(E.cache("status", calc))


@app.route("/api/grafico1")
def api_grafico1():
    return jsonify(E.cache("g1", lambda: analise.grafico1(E.s1t, E.storage.snapshots("br", "br"))))


@app.route("/api/grafico2")
def api_grafico2():
    def calc():
        return analise.grafico2(E.df1t, E.storage.snapshots("br", "br"),
                                E.storage.snapshots("mesmas_mun", "br"), E.storage.secoes_2t())
    return jsonify(E.cache("g2", calc))


@app.route("/api/quadros")
def api_quadros():
    def calc():
        br = _br_atual()
        ufs = E.storage.ultimos_por_codigo("uf")
        mun = E.storage.mun_atual()
        modo, motivo, mesmas_sec = _modo_e_mesmas()
        mesmas_mun_ufs = E.storage.ultimos_por_codigo("mesmas_mun")
        return {
            "quadro1": analise.quadro1(E.uf_1t, ufs, br, modo, mesmas_sec, mesmas_mun_ufs),
            "quadro2": analise.quadro2(br, ufs, mun),
            "quadro3": analise.quadro3(br),
            "modo": modo, "motivo": motivo,
        }
    return jsonify(E.cache("quadros", calc))


@app.route("/api/mapa")
def api_mapa():
    turno = 1 if request.args.get("turno") == "1" else 2
    nivel = "uf" if request.args.get("nivel") == "uf" else "mun"

    def calc():
        if nivel == "uf":
            return analise.mapa_ufs(turno, E.uf_1t, E.storage.ultimos_por_codigo("uf"))
        return analise.mapa_municipios(turno, E.mun_1t, E.storage.mun_atual(), E.municipios)
    return jsonify(E.cache(f"mapa-{turno}-{nivel}", calc))


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
        Replay(E.storage, E.df1t, args.velocidade, args.forcar_2_candidatos, args.lote, args.inicio).iniciar()
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

    log.info("painel em http://%s:%d/", "localhost" if args.host == "0.0.0.0" else args.host, args.porta)
    app.run(host=args.host, port=args.porta, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
