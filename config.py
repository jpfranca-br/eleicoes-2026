"""Configurações centrais do painel de apuração (Presidência 2026, 2º turno)."""
from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"          # zips do boletim de urna (1º turno)
DIR_1T = DATA_DIR / "1t"            # csv por seção já processado (1º turno)
GEO_DIR = DATA_DIR / "geo"          # geojson IBGE
STATIC_DIR = BASE_DIR / "static"
DB_PATH = DATA_DIR / "apuracao.db"
DB_REPLAY_PATH = DATA_DIR / "replay.db"
MUN_CONFIG_PATH = DATA_DIR / "mun_config.json"

# ---------------------------------------------------------------- Eleição
ANO = 2026
CICLO = "ele2026"
ELEICAO_1T = "6257"
ELEICAO_2T = "6258"
PLEITO_1T = "3220"
PLEITO_2T = "3221"
DATA_1T = "2026-10-04"
DATA_2T = "2026-10-25"

# Total de eleitores informado pelo usuário (TSE divulga 158.745.502)
TOTAL_ELEITORES = 158_745_463

CANDIDATOS = {"22": "Flavio Bolsonaro", "13": "Lula"}
OUTROS = "outros"
CORES = {
    "22": "#1f5fbf",      # azul
    "13": "#d62828",      # vermelho
    OUTROS: "#f2c230",    # amarelo
    "sem_dados": "#c9c9c9",
}

# ---------------------------------------------------------------- URLs TSE
URL_RESULTADOS = f"https://resultados.tse.jus.br/oficial/{CICLO}"
URL_ELE_C = "https://resultados.tse.jus.br/oficial/comum/config/ele-c.json"
URL_BWEB = (
    "https://cdn.tse.jus.br/estatistica/sead/eleicoes/eleicoes2026/buweb/"
    "bweb_1t_{uf}_051020261403.zip"
)
URL_GEO_MUN = (
    "https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR"
    "?intrarregiao=municipio&qualidade=minima&formato=application/vnd.geo+json"
)
URL_GEO_UF = (
    "https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR"
    "?intrarregiao=UF&qualidade=minima&formato=application/vnd.geo+json"
)
USER_AGENT = "Mozilla/5.0 (painel-apuracao-2026; uso pessoal)"


def url_u(eleicao: str, uf: str, cd_mun: str | None = None) -> str:
    """Arquivo de resultado unificado (-u.json). uf='br' para nacional."""
    uf = uf.lower()
    abr = f"{uf}{cd_mun}" if cd_mun else uf
    return f"{URL_RESULTADOS}/{eleicao}/dados/{uf}/{abr}-c0001-e{int(eleicao):06d}-u.json"


def url_mun_config(eleicao: str) -> str:
    return f"{URL_RESULTADOS}/{eleicao}/config/mun-e{int(eleicao):06d}-cm.json"


def url_cs(pleito: str, uf: str) -> str:
    """Listagem de seções do arquivo-urna por UF."""
    uf = uf.lower()
    return f"{URL_RESULTADOS}/arquivo-urna/{pleito}/config/{uf}/{uf}-p{int(pleito):06d}-cs.json"


# ---------------------------------------------------------------- Geografia
UFS = [
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT",
    "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP",
    "TO", "ZZ",
]
REGIOES = {
    "Norte": ["AC", "AM", "AP", "PA", "RO", "RR", "TO"],
    "Nordeste": ["AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"],
    "Centro-Oeste": ["DF", "GO", "MS", "MT"],
    "Sudeste": ["ES", "MG", "RJ", "SP"],
    "Sul": ["PR", "RS", "SC"],
    "Exterior": ["ZZ"],
}
UF_REGIAO = {uf: reg for reg, ufs in REGIOES.items() for uf in ufs}
ORDEM_REGIOES = ["Norte", "Nordeste", "Centro-Oeste", "Sudeste", "Sul", "Exterior"]
# código IBGE (2 dígitos) -> UF, para o mapa por estado
IBGE_UF = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP", "17": "TO",
    "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB", "26": "PE", "27": "AL",
    "28": "SE", "29": "BA", "31": "MG", "32": "ES", "33": "RJ", "35": "SP", "41": "PR",
    "42": "SC", "43": "RS", "50": "MS", "51": "MT", "52": "GO", "53": "DF",
}

# ---------------------------------------------------------------- Coleta
INTERVALO_NACIONAL = 30      # s: br + UFs + zz
INTERVALO_SECOES = 60        # s: cs.json por UF
THREADS_MUNICIPIOS = 6       # varredura municipal (o TSE devolve 429 com muitas requisições simultâneas)
TAXA_MAX_REQ_S = 6.0         # limite global de requisições/s ao resultados.tse.jus.br (token bucket)
PAUSA_APOS_429 = 20          # s de pausa da varredura municipal quando o TSE responde 429
THREADS_DOWNLOAD_1T = 4
HTTP_TIMEOUT = 30
CACHE_API_SEGUNDOS = 10      # cache das respostas /api/*
LIMIAR_LOTE = 0.80           # fração de seções de uma UF surgindo numa única coleta => "lote"


def garantir_pastas() -> None:
    for p in (DATA_DIR, RAW_DIR, DIR_1T, GEO_DIR):
        p.mkdir(parents=True, exist_ok=True)
