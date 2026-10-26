"""Baixa e decodifica os boletins de urna (BU) por seção do arquivo-urna do TSE.

Para cada seção listada no cs.json da UF (pleito 3221 = 2º turno), busca o aux.json (que traz o hash da
pasta e o horário de recebimento), baixa o bu.dat (ASN.1/BER, ~1,5 KB), decodifica com spec/bu.asn1 e grava
em data/bu_{turno}t.db: o binário bruto e os campos decodificados (abertura, encerramento, emissão da urna,
recebimento na totalização, comparecimento, aptos, votos por candidato).

É resumível: seções já gravadas não são baixadas de novo. Respeita o limite do TSE (token bucket global).

Uso:
  python baixar_bu.py                      # todas as UFs, 2º turno
  python baixar_bu.py --ufs AC,RR          # algumas UFs
  python baixar_bu.py --taxa 15 --threads 8
  python baixar_bu.py --turno 1            # 1º turno (pleito 3220)
  python baixar_bu.py --exportar           # só gera data/{turno}t_bu/secoes_bu.csv a partir do banco
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import config
import tse_client

log = logging.getLogger("baixar_bu")

SPEC = config.BASE_DIR / "spec" / "bu.asn1"
PLEITOS = {1: config.PLEITO_1T, 2: config.PLEITO_2T}
ELEICOES = {1: int(config.ELEICAO_1T), 2: int(config.ELEICAO_2T)}
# horas a SOMAR ao horário local da urna para obter Brasília (outubro: sem horário de verão)
OFFSET_UF = {"AC": 2, "AM": 1, "RR": 1, "RO": 1, "MT": 1, "MS": 1}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bu (
    uf TEXT, cd_mun TEXT, zona TEXT, secao TEXT, local TEXT,
    dt_abertura TEXT, dt_encerramento TEXT, dt_emissao TEXT, dt_recebido TEXT,
    aptos INTEGER, comparecimento INTEGER, v22 INTEGER, v13 INTEGER, brancos INTEGER, nulos INTEGER,
    tipo_urna TEXT, tipo_bu TEXT, versao TEXT, hash TEXT, baixado_em TEXT, bruto BLOB,
    PRIMARY KEY (uf, cd_mun, zona, secao)
);
CREATE TABLE IF NOT EXISTS falhas (
    uf TEXT, cd_mun TEXT, zona TEXT, secao TEXT, erro TEXT, quando TEXT,
    PRIMARY KEY (uf, cd_mun, zona, secao)
);
"""


# ------------------------------------------------------------------ decodificação
class Decodificador:
    def __init__(self, spec_path: Path = SPEC):
        import asn1tools
        self.spec = asn1tools.compile_files([str(spec_path)], codec="ber")

    @staticmethod
    def _dt(s: str | None) -> str | None:
        """'20261025T150152' -> '2026-10-25 15:01:52'."""
        if not s or len(s) < 15:
            return None
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]} {s[9:11]}:{s[11:13]}:{s[13:15]}"

    def decodificar(self, bruto: bytes, eleicao: int) -> dict:
        env = self.spec.decode("EntidadeEnvelopeGenerico", bruto)
        bu = self.spec.decode("EntidadeBoletimUrna", env["conteudo"])
        tipo, dados = bu["dadosSecaoSA"]
        out = {
            "local": str(bu["identificacaoSecao"]["local"]),
            "dt_emissao": self._dt(bu.get("dataHoraEmissao")),
            "dt_abertura": self._dt(dados.get("dataHoraAbertura")) if tipo == "dadosSecao" else None,
            "dt_encerramento": self._dt(dados.get("dataHoraEncerramento")) if tipo == "dadosSecao" else None,
            "comparecimento": int(bu.get("qtdEleitoresCompareceram") or 0),
            "aptos": 0, "v22": 0, "v13": 0, "brancos": 0, "nulos": 0,
            "tipo_urna": str(bu["urna"]["tipoUrna"]), "tipo_bu": tipo,
            "versao": str(bu["urna"].get("versaoVotacao", "")),
        }
        for res in bu.get("resultadosVotacaoPorEleicao") or []:
            if int(res.get("idEleicao", 0)) != eleicao:
                continue
            out["aptos"] = int(res.get("qtdEleitoresAptos") or 0)
            for rv in res.get("resultadosVotacao") or []:
                for cargo in rv.get("totaisVotosCargo") or []:
                    cod = cargo.get("codigoCargo")
                    if not (isinstance(cod, tuple) and cod[1] == "presidente"):
                        continue
                    for vv in cargo.get("votosVotaveis") or []:
                        q = int(vv.get("quantidadeVotos") or 0)
                        t = vv.get("tipoVoto")
                        if t == "nominal":
                            n = str((vv.get("identificacaoVotavel") or {}).get("codigo"))
                            if n == "22":
                                out["v22"] += q
                            elif n == "13":
                                out["v13"] += q
                        elif t == "branco":
                            out["brancos"] += q
                        elif t == "nulo":
                            out["nulos"] += q
        return out


# ------------------------------------------------------------------ banco
class BancoBU:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(path), check_same_thread=False, timeout=60)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def chaves(self) -> set[tuple[str, str, str, str]]:
        with self._lock:
            return {tuple(r) for r in self._con.execute("SELECT uf, cd_mun, zona, secao FROM bu")}

    def gravar(self, linhas: list[dict]) -> None:
        if not linhas:
            return
        cols = ["uf", "cd_mun", "zona", "secao", "local", "dt_abertura", "dt_encerramento", "dt_emissao", "dt_recebido",
                "aptos", "comparecimento", "v22", "v13", "brancos", "nulos", "tipo_urna", "tipo_bu", "versao", "hash",
                "baixado_em", "bruto"]
        with self._lock:
            self._con.executemany(
                f"INSERT OR REPLACE INTO bu ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [[l.get(c) for c in cols] for l in linhas])
            self._con.commit()

    def falha(self, chave: tuple, erro: str) -> None:
        with self._lock:
            self._con.execute("INSERT OR REPLACE INTO falhas VALUES (?,?,?,?,?,?)",
                              (*chave, erro[:300], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            self._con.commit()

    def limpar_falha(self, chave: tuple) -> None:
        with self._lock:
            self._con.execute("DELETE FROM falhas WHERE uf=? AND cd_mun=? AND zona=? AND secao=?", chave)

    def dataframe(self) -> pd.DataFrame:
        with self._lock:
            return pd.read_sql_query(
                "SELECT uf, cd_mun, zona, secao, local, dt_abertura, dt_encerramento, dt_emissao, dt_recebido, aptos, "
                "comparecimento, v22, v13, brancos, nulos, tipo_urna, tipo_bu FROM bu", self._con)

    def n_falhas(self) -> int:
        with self._lock:
            return self._con.execute("SELECT COUNT(*) FROM falhas").fetchone()[0]


# ------------------------------------------------------------------ listagem e download
def listar_secoes(pleito: str, ufs: list[str]) -> list[tuple[str, str, str, str]]:
    """Seções com boletim publicado (têm 'da'); seções agregadas ('nsp') não têm BU próprio."""
    out = []
    for uf in ufs:
        d = tse_client.get_json(config.url_cs(pleito, uf), timeout=120)
        n_uf = 0
        for abr in d.get("abr") or []:
            for m in abr.get("mu") or []:
                for z in m.get("zon") or []:
                    for s in z.get("sec") or []:
                        if "da" in s or "ha" in s:
                            out.append((uf, str(m["cd"]).zfill(5), str(z["cd"]).zfill(4), str(s["ns"]).zfill(4)))
                            n_uf += 1
        log.info("[%s] %d seções com boletim publicado", uf, n_uf)
    return out


def baixar_secao(chave: tuple[str, str, str, str], pleito: str, eleicao: int, dec: Decodificador) -> dict:
    uf, mun, zona, sec = chave
    base = f"{config.URL_RESULTADOS}/arquivo-urna/{pleito}/dados/{uf.lower()}/{mun}/{zona}/{sec}"
    aux = tse_client.get_json(f"{base}/p{int(pleito):06d}-{uf.lower()}-m{mun}-z{zona}-s{sec}-aux.json", tentativas=5)
    hashes = aux.get("hashes") or []
    if not hashes:
        raise ValueError("aux.json sem hashes")
    h = hashes[-1]  # última publicação
    nome_bu = next((a["nm"] for a in h.get("arq") or [] if a.get("tp") == "bu"), None)
    if not nome_bu:
        raise ValueError("aux.json sem arquivo bu")
    tse_client.limitador.esperar()  # o bu.dat também conta no limite (2 requisições por seção)
    r = tse_client.session().get(f"{base}/{h['hash']}/{nome_bu}", timeout=config.HTTP_TIMEOUT)
    if r.status_code == 429:
        tse_client.limitador.registrar_429()
        raise tse_client.Erro429(nome_bu)
    r.raise_for_status()
    bruto = r.content
    linha = dec.decodificar(bruto, eleicao)
    dr, hr = h.get("dr") or "", h.get("hr") or ""
    recebido = None
    if dr and hr:
        recebido = datetime.strptime(f"{dr} {hr}", "%d/%m/%Y %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    linha.update({"uf": uf, "cd_mun": mun, "zona": zona, "secao": sec, "dt_recebido": recebido,
                  "hash": h.get("hash"), "baixado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "bruto": bruto})
    return linha


class Adaptador:
    """Controle adaptativo da taxa (AIMD): corta pela metade ao ver 429, sobe 25 % a cada SOBE_APOS_S s sem 429,
    até o teto. Assim --taxa 300 significa "até 300": o script acha o limite real do TSE sem ficar bloqueado."""

    SOBE_APOS_S = 60       # s seguidos sem 429 para subir
    FATOR_SOBE = 1.25
    FATOR_CORTE = 0.5
    TAXA_MIN = 5.0
    INICIO_MAX = 50.0      # começa no máximo em 50 req/s mesmo que o teto seja maior

    def __init__(self, teto: float, limitador=None):
        self.teto = float(teto)
        self.limitador = limitador or tse_client.limitador
        self.taxa = min(self.teto, self.INICIO_MAX)
        self._aplicar()
        self._ultimo_429 = self.limitador.total_429
        self._estavel_desde = time.monotonic()
        self._ultimo_corte = 0.0
        self.cortes = 0

    def _aplicar(self) -> None:
        self.limitador.taxa = self.taxa
        self.limitador.capacidade = max(1.0, self.taxa)

    def tick(self) -> str | None:
        """Chamado a cada ~1 s. Devolve uma mensagem quando a taxa muda."""
        agora = time.monotonic()
        if self.limitador.total_429 != self._ultimo_429:
            self._ultimo_429 = self.limitador.total_429
            self._estavel_desde = agora
            if agora - self._ultimo_corte > 20:  # um corte por rajada de 429
                self._ultimo_corte = agora
                self.cortes += 1
                self.taxa = max(self.TAXA_MIN, self.taxa * self.FATOR_CORTE)
                self._aplicar()
                return f"429 recebido: taxa reduzida para {self.taxa:.0f} req/s"
            return None
        if self.taxa < self.teto and agora - self._estavel_desde >= self.SOBE_APOS_S:
            self._estavel_desde = agora
            self.taxa = min(self.teto, self.taxa * self.FATOR_SOBE)
            self._aplicar()
            return f"sem 429 há {self.SOBE_APOS_S} s: taxa elevada para {self.taxa:.0f} req/s"
        return None


def baixar_tudo(turno: int, ufs: list[str], taxa: float, threads: int, banco: BancoBU) -> None:
    pleito, eleicao = PLEITOS[turno], ELEICOES[turno]
    adaptador = Adaptador(taxa)
    threads = threads or min(128, max(8, int(taxa * 0.4)))  # ~0,4 conexão por req/s (latência ~0,3 s)
    dec = Decodificador()
    todas = listar_secoes(pleito, ufs)
    feitas = banco.chaves()
    pendentes = [c for c in todas if c not in feitas]
    log.info("%d seções listadas, %d já no banco, %d a baixar (≈%.0f min a %.0f req/s; teto %.0f, %d threads)",
             len(todas), len(todas) - len(pendentes), len(pendentes), len(pendentes) * 2 / adaptador.taxa / 60,
             adaptador.taxa, taxa, threads)
    if not pendentes:
        return
    lote: list[dict] = []
    lock = threading.Lock()
    ok = err = 0
    t0 = time.time()
    fim = threading.Event()

    def monitor():
        while not fim.wait(1.0):
            msg = adaptador.tick()
            if msg:
                log.info(msg)
    threading.Thread(target=monitor, name="adaptador", daemon=True).start()

    def tarefa(chave):
        nonlocal ok, err
        for tentativa in range(3):
            if tse_client.limitador.em_penalidade(config.PAUSA_APOS_429):
                time.sleep(config.PAUSA_APOS_429)
            try:
                linha = baixar_secao(chave, pleito, eleicao, dec)
                with lock:
                    lote.append(linha)
                    ok += 1
                    if len(lote) >= 200:
                        banco.gravar(lote)
                        lote.clear()
                    if ok % 1000 == 0:
                        dt = time.time() - t0
                        restam = len(pendentes) - ok - err
                        log.info("%d ok, %d erro(s), %.1f seções/s, taxa %.0f req/s, %d x 429, faltam %d (≈%s)",
                                 ok, err, ok / dt, adaptador.taxa, tse_client.limitador.total_429, restam,
                                 str(timedelta(seconds=int(restam / max(ok / dt, 0.01)))))
                banco.limpar_falha(chave)
                return
            except tse_client.Erro429:
                time.sleep(30)
            except Exception as exc:  # noqa: BLE001
                if tentativa == 2:
                    with lock:
                        err += 1
                    banco.falha(chave, repr(exc))
                    log.debug("%s: %s", chave, exc)
                    return
                time.sleep(2)
        with lock:
            err += 1
        banco.falha(chave, "429 persistente")

    try:
        with ThreadPoolExecutor(max_workers=threads) as ex:
            list(ex.map(tarefa, pendentes))
    finally:
        fim.set()
        with lock:
            banco.gravar(lote)
    log.info("concluído: %d ok, %d erro(s) em %s (taxa final %.0f req/s, %d corte(s) por 429)", ok, err,
             str(timedelta(seconds=int(time.time() - t0))), adaptador.taxa, adaptador.cortes)


# ------------------------------------------------------------------ exportação
def exportar(turno: int, banco: BancoBU, destino: Path) -> pd.DataFrame:
    df = banco.dataframe()
    if df.empty:
        log.warning("banco vazio")
        return df
    for c in ("dt_abertura", "dt_encerramento", "dt_emissao"):
        local = pd.to_datetime(df[c], errors="coerce")
        off = df["uf"].map(OFFSET_UF).fillna(0).astype(int)
        br = local + pd.to_timedelta(off, unit="h")
        df[f"{c}_brasilia"] = br.dt.strftime("%Y-%m-%d %H:%M:%S").where(local.notna() & (df["uf"] != "ZZ"), None)
    destino.parent.mkdir(parents=True, exist_ok=True)
    df.sort_values(["uf", "cd_mun", "zona", "secao"]).to_csv(destino, index=False, encoding="utf-8")
    log.info("exportado %d seções para %s", len(df), destino)
    return df


def caminho_banco(turno: int) -> Path:
    return config.DATA_DIR / f"bu_{turno}t.db"


def caminho_csv(turno: int) -> Path:
    return config.DATA_DIR / f"{turno}t_bu" / "secoes_bu.csv"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--turno", type=int, choices=(1, 2), default=2)
    ap.add_argument("--ufs", help="lista separada por vírgula (padrão: todas)")
    ap.add_argument("--taxa", type=float, default=50.0,
                    help="TETO de requisições/s ao TSE (padrão 50). Começa em até 50, cai pela metade a cada 429 e sobe "
                         "25%% a cada 60 s sem 429, até o teto. Medido em 26/10: 100 req/s por 30 s e 30 req/s por "
                         "2 min sem 429; 70 req/s por ~2 min bloqueou ~6 min.")
    ap.add_argument("--threads", type=int, default=0, help="conexões simultâneas (padrão: automático, ~0,4 × taxa)")
    ap.add_argument("--exportar", action="store_true", help="não baixa; só exporta o banco para CSV")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    if not SPEC.exists():
        sys.exit(f"especificação ASN.1 não encontrada: {SPEC}")
    config.garantir_pastas()
    banco = BancoBU(caminho_banco(args.turno))
    if not args.exportar:
        ufs = [u.strip().upper() for u in args.ufs.split(",")] if args.ufs else config.UFS
        baixar_tudo(args.turno, ufs, args.taxa, args.threads, banco)
        if banco.n_falhas():
            log.warning("%d seção(ões) com falha; rode de novo para tentar só as faltantes", banco.n_falhas())
    df = exportar(args.turno, banco, caminho_csv(args.turno))
    if not df.empty:
        print(f"seções: {len(df):,}  comparecimento: {df.comparecimento.sum():,}  Flavio={df.v22.sum():,}  "
              f"Lula={df.v13.sum():,}  brancos={df.brancos.sum():,}  nulos={df.nulos.sum():,}".replace(",", "."))


if __name__ == "__main__":
    main()
