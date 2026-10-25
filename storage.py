"""Persistência em SQLite da série histórica do 2º turno e do estado das coletas."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

import config

CAMPOS = ["v22", "v13", "v_outros", "vv", "vb", "vn", "tv", "ts", "st", "te", "est", "esnt", "c", "a"]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS snapshots (
    nivel TEXT NOT NULL, codigo TEXT NOT NULL, ts_feed TEXT NOT NULL, coletado_em TEXT NOT NULL,
    {", ".join(f"{c} INTEGER DEFAULT 0" for c in CAMPOS)},
    UNIQUE(nivel, codigo, ts_feed)
);
CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(nivel, codigo, ts_feed);
CREATE TABLE IF NOT EXISTS mun_atual (
    codigo TEXT PRIMARY KEY, uf TEXT, nome TEXT, cdi TEXT, ts_feed TEXT, coletado_em TEXT,
    {", ".join(f"{c} INTEGER DEFAULT 0" for c in CAMPOS)}
);
CREATE TABLE IF NOT EXISTS secoes_2t (
    uf TEXT, cd_mun TEXT, zona TEXT, secao TEXT, visto_em TEXT, da TEXT, ha TEXT,
    PRIMARY KEY(uf, cd_mun, zona, secao)
);
CREATE INDEX IF NOT EXISTS ix_sec_visto ON secoes_2t(visto_em);
CREATE TABLE IF NOT EXISTS coletas (
    nome TEXT PRIMARY KEY, ultimo_ok TEXT, ultimo_erro TEXT, msg TEXT, duracao REAL, contador INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS varreduras (
    id INTEGER PRIMARY KEY AUTOINCREMENT, inicio TEXT, fim TEXT, n_ok INTEGER, n_erro INTEGER, ts_feed_max TEXT
);
"""


def agora() -> str:
    """Horário de Brasília (fuso dos dados do TSE), não o da máquina."""
    return config.agora_brasilia().strftime("%Y-%m-%d %H:%M:%S")


class Storage:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._con = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._con.executescript(_SCHEMA)
            self._con.commit()

    # ------------------------------------------------------------ escrita
    def gravar_snapshot(self, nivel: str, codigo: str, d: dict[str, Any]) -> bool:
        """Insere se (nivel, codigo, ts_feed) ainda não existe. Retorna True se inseriu."""
        if not d.get("ts_feed"):
            return False
        cols = ["nivel", "codigo", "ts_feed", "coletado_em", *CAMPOS]
        vals = [nivel, codigo, d["ts_feed"], agora(), *[int(d.get(c) or 0) for c in CAMPOS]]
        with self._lock:
            cur = self._con.execute(
                f"INSERT OR IGNORE INTO snapshots ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            self._con.commit()
            return cur.rowcount > 0

    def gravar_snapshots(self, nivel: str, itens: Iterable[tuple[str, dict[str, Any]]]) -> int:
        cols = ["nivel", "codigo", "ts_feed", "coletado_em", *CAMPOS]
        now = agora()
        rows = [[nivel, cod, d["ts_feed"], now, *[int(d.get(c) or 0) for c in CAMPOS]]
                for cod, d in itens if d.get("ts_feed")]
        if not rows:
            return 0
        with self._lock:
            cur = self._con.executemany(
                f"INSERT OR IGNORE INTO snapshots ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)
            self._con.commit()
            return cur.rowcount

    def gravar_mun_atual(self, itens: Iterable[dict[str, Any]]) -> None:
        """itens: dicts com codigo, uf, nome, cdi, ts_feed + CAMPOS."""
        cols = ["codigo", "uf", "nome", "cdi", "ts_feed", "coletado_em", *CAMPOS]
        now = agora()
        rows = [[m["codigo"], m.get("uf"), m.get("nome"), m.get("cdi"), m.get("ts_feed"), now,
                 *[int(m.get(c) or 0) for c in CAMPOS]] for m in itens]
        if not rows:
            return
        with self._lock:
            self._con.executemany(
                f"INSERT OR REPLACE INTO mun_atual ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)
            self._con.commit()

    def gravar_secoes(self, itens: Iterable[tuple[str, str, str, str, str, str]]) -> int:
        """itens: (uf, cd_mun, zona, secao, da, ha). visto_em = agora. Retorna nº de novas."""
        now = agora()
        rows = [(uf, m, z, s, now, da, ha) for uf, m, z, s, da, ha in itens]
        if not rows:
            return 0
        with self._lock:
            cur = self._con.executemany(
                "INSERT OR IGNORE INTO secoes_2t (uf, cd_mun, zona, secao, visto_em, da, ha) VALUES (?,?,?,?,?,?,?)", rows)
            self._con.commit()
            return cur.rowcount

    def registrar_coleta(self, nome: str, ok: bool, msg: str = "", duracao: float | None = None) -> None:
        now = agora()
        with self._lock:
            self._con.execute(
                "INSERT INTO coletas (nome, contador) VALUES (?, 0) ON CONFLICT(nome) DO NOTHING", (nome,))
            if ok:
                self._con.execute(
                    "UPDATE coletas SET ultimo_ok=?, msg=?, duracao=?, contador=contador+1 WHERE nome=?",
                    (now, msg, duracao, nome))
            else:
                self._con.execute("UPDATE coletas SET ultimo_erro=?, msg=? WHERE nome=?", (now, msg, nome))
            self._con.commit()

    def registrar_varredura(self, inicio: str, n_ok: int, n_erro: int, ts_feed_max: str | None) -> None:
        with self._lock:
            self._con.execute(
                "INSERT INTO varreduras (inicio, fim, n_ok, n_erro, ts_feed_max) VALUES (?,?,?,?,?)",
                (inicio, agora(), n_ok, n_erro, ts_feed_max))
            self._con.commit()

    # ------------------------------------------------------------ leitura
    def _df(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        with self._lock:
            return pd.read_sql_query(sql, self._con, params=params)

    def snapshots(self, nivel: str, codigo: str | None = None) -> pd.DataFrame:
        if codigo is None:
            return self._df("SELECT * FROM snapshots WHERE nivel=? ORDER BY ts_feed", (nivel,))
        return self._df("SELECT * FROM snapshots WHERE nivel=? AND codigo=? ORDER BY ts_feed", (nivel, codigo))

    def ultimo_snapshot(self, nivel: str, codigo: str) -> dict[str, Any] | None:
        df = self._df("SELECT * FROM snapshots WHERE nivel=? AND codigo=? ORDER BY ts_feed DESC LIMIT 1",
                      (nivel, codigo))
        return None if df.empty else df.iloc[0].to_dict()

    def ultimos_por_codigo(self, nivel: str) -> pd.DataFrame:
        """Último snapshot de cada código do nível (ex.: todas as UFs)."""
        return self._df(
            "SELECT s.* FROM snapshots s JOIN (SELECT codigo, MAX(ts_feed) m FROM snapshots WHERE nivel=? GROUP BY codigo) u "
            "ON s.codigo=u.codigo AND s.ts_feed=u.m WHERE s.nivel=?", (nivel, nivel))

    def mun_atual(self) -> pd.DataFrame:
        return self._df("SELECT * FROM mun_atual")

    def secoes_2t(self) -> pd.DataFrame:
        return self._df("SELECT * FROM secoes_2t")

    def secoes_2t_chaves(self) -> set[tuple[str, str, str, str]]:
        with self._lock:
            cur = self._con.execute("SELECT uf, cd_mun, zona, secao FROM secoes_2t")
            return {tuple(r) for r in cur.fetchall()}

    def coletas(self) -> list[dict[str, Any]]:
        return self._df("SELECT * FROM coletas").to_dict("records")

    def varreduras(self) -> pd.DataFrame:
        return self._df("SELECT * FROM varreduras ORDER BY id")

    def limpar(self) -> None:
        with self._lock:
            for t in ("snapshots", "mun_atual", "secoes_2t", "coletas", "varreduras"):
                self._con.execute(f"DELETE FROM {t}")
            self._con.commit()
