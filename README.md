# Painel de apuração – Presidência 2026, 2º turno

Projeto em Python que:

1. baixa os votos do **1º turno por seção eleitoral** (Boletim de Urna na Web do TSE, com o horário em que cada urna entrou na totalização);
2. fica **coletando os dados oficiais do 2º turno** (JSON de divulgação do TSE) em tempo real;
3. mostra uma interface web que se atualiza sozinha com 3 gráficos e 3 quadros.

Candidatos: **Flavio Bolsonaro (22, azul)**, **Lula (13, vermelho)**, **Outros (amarelo, só no 1º turno)**.
Percentuais sempre sobre votos válidos (brancos, nulos e abstenções excluídos).

## Requisitos

- Python 3.11 ou superior (testado com 3.13).
- Acesso à internet (TSE, IBGE e CDNs do Plotly/Leaflet para a página).
- ~6 GB livres em disco durante o download do 1º turno (os zips são apagados após o processamento; o resultado final ocupa ~40 MB).

## Instalação

### Obter o código (git clone)

O repositório já inclui os dados do 1º turno processados por seção (`data/1t/`), as malhas do IBGE e a configuração de municípios. Depois do clone não é preciso baixar os 5 GB do TSE.

Pré-requisito: git instalado (Windows: https://git-scm.com/download/win; Debian/Ubuntu: `sudo apt install -y git`).

Windows (PowerShell):

```powershell
cd C:\Users\$env:USERNAME\Desktop
git clone https://github.com/jpfranca-br/eleicoes-2026.git
cd eleicoes-2026
```

Linux:

```bash
sudo git clone https://github.com/jpfranca-br/eleicoes-2026.git /opt/eleicoes
sudo chown -R $USER:$USER /opt/eleicoes     # a pasta fica com o seu usuário; use o mesmo usuário no serviço systemd
cd /opt/eleicoes
```

Para atualizar uma instalação existente com a versão mais nova do GitHub:

```bash
git pull
```

### Windows (PowerShell)

```powershell
cd C:\Users\$env:USERNAME\Desktop\eleicoes-2026
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Se o PowerShell bloquear a ativação do venv: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

### Linux (Debian/Ubuntu)

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip
cd /opt/eleicoes            # pasta do git clone
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Em outras distribuições (Fedora/RHEL: `sudo dnf install python3 python3-pip`) os passos do venv são os mesmos.

## Como rodar

### 1. Baixar e processar o 1º turno (opcional)

Quem fez o git clone já tem os dados e pode pular este passo. Ele serve para regerar os arquivos a partir do TSE:

```bash
python primeiro_turno.py            # todas as 28 UFs (~5 GB de download; conexão rápida: poucos minutos)
python primeiro_turno.py --ufs AC,RR   # só algumas UFs, para testar
```

Gera `data/1t/secoes_{UF}.csv` (uma linha por seção). O `app.py` também faz isso sozinho em segundo plano se os arquivos não existirem.

### 2. Subir o painel (modo real)

```bash
python app.py                        # http://localhost:8000
python app.py --porta 8080 --host 0.0.0.0
```

Abra `http://<ip-do-servidor>:8000/`. A página se atualiza sozinha quando há dado novo: a cada 3 s ela consulta `/api/status`, que traz uma versão de cada painel, e baixa e redesenha só os painéis que mudaram. Um dado novo do TSE aparece em até ~13 s depois de coletado (10 s de recálculo no servidor + 3 s de consulta). Por segurança, tudo é redesenhado a cada 2 minutos. A série histórica fica em `data/apuracao.db` (SQLite), então reiniciar o programa não perde o que já foi coletado. Use `--limpar` para zerar.

### 3. Simulação (testar antes das 17h)

```bash
python app.py --replay                       # reproduz o 1º turno como se fosse o 2º (60x mais rápido)
python app.py --replay --velocidade 300      # 300 s virtuais por segundo real
python app.py --replay --forcar-2-candidatos # redistribui "outros" entre 22 e 13 (testa o "matematicamente eleito")
python app.py --replay --lote                # publica a lista de seções só no fim (testa o fallback por município)
python app.py --replay --loop --forcar-2-candidatos   # demonstração contínua: ao terminar, pausa 20 s, zera e recomeça
python app.py --replay --ufs AC,RR           # replay rápido só com algumas UFs
```

O replay usa um banco separado (`data/replay.db`) e sempre começa do zero. A página mostra o aviso “MODO SIMULAÇÃO”.

### Opções do `app.py`

| Opção | Efeito |
|---|---|
| `--host`, `--porta` | endereço/porta do servidor (padrão `0.0.0.0:8000`) |
| `--sem-1t` | não baixa o 1º turno automaticamente |
| `--ufs AC,RR` | limita o 1º turno a essas UFs (testes) |
| `--limpar` | apaga a série histórica antes de iniciar |
| `--replay`, `--velocidade`, `--inicio HH:MM`, `--forcar-2-candidatos`, `--lote`, `--loop` | simulação |
| `--debug` | log detalhado |

## Análise depois da apuração: seções e votos por janela de tempo

`analisar_fechamento.py` lê a base gravada durante a noite e mostra quantas seções e votos entraram na totalização em cada janela (padrão 10 min):

```bash
python analisar_fechamento.py                       # 2º turno, a partir de data/apuracao.db
python analisar_fechamento.py --turno 1             # 1º turno, a partir de data/1t (horário real por seção)
python analisar_fechamento.py --intervalo 5 --detalhe   # janelas de 5 min; acrescenta válidos, Flavio e Lula
python analisar_fechamento.py --csv fechamento.csv  # grava também em CSV
python analisar_fechamento.py --db /caminho/apuracao.db
```

Saída (exemplo):

```
17:00:00 até 17:09:59 -     1 075 seções -       280 731 votos
17:10:00 até 17:19:59 -     4 599 seções -     1 087 208 votos
```

Fontes possíveis (`--fonte`): `bu` (boletins de urna baixados por `baixar_bu.py`, com o horário real de cada seção), `snapshots` (2º turno: snapshot nacional do TSE gravado a cada 30 s durante a apuração; só recebimento, resolução de 30 s) e `bweb` (1º turno: `DT_BU_RECEBIDO` de `data/1t`). Com `--fonte bu`, `--evento` escolhe o horário: `encerramento` (último voto na seção, padrão), `emissao` (BU emitido pela urna), `abertura` ou `recebimento` (entrada na totalização). "Votos" = comparecimento (inclui brancos e nulos).

### Horário real de fechamento das seções: `baixar_bu.py`

O arquivo-urna do TSE publica, por seção, o boletim de urna em ASN.1/BER (`bu.dat`, ~1,5 KB) com abertura, encerramento e emissão na **hora local da urna**, além do recebimento na totalização (no `aux.json`). `baixar_bu.py` lista as seções pelo `cs.json` de cada UF, baixa `aux.json` + `bu.dat`, decodifica com `spec/bu.asn1` (especificação do TSE, versão v2 usada em 2026) e grava tudo em `data/bu_{turno}t.db` (binário bruto + campos decodificados). É resumível e respeita o limite do TSE.

```bash
pip install -r requirements.txt            # inclui asn1tools
python baixar_bu.py --ufs AC               # teste pequeno (~3 min)
nohup python baixar_bu.py > /tmp/bu.log 2>&1 &   # país inteiro: ~1 milhão de requisições, ~11 h a 25 req/s
python baixar_bu.py --exportar             # gera data/2t_bu/secoes_bu.csv (com colunas *_brasilia)
python analisar_fechamento.py --evento encerramento --intervalo 10
```

Medições de 26/10/2026: 30 req/s por 2 min sem nenhum 429; 70 req/s geraram bloqueio de ~6 min. Padrão `--taxa 25`. Seções agregadas (`nsp` no `cs.json`) não têm BU próprio: os votos delas estão na seção principal. Conversão para Brasília: AC +2 h; AM, RR, RO, MT, MS +1 h; demais 0; exterior (ZZ) fica na hora local do país e sai das janelas por padrão (`--incluir-exterior` para incluir).

## Linux: firewall e serviço

```bash
sudo ufw allow 8000/tcp        # se usar ufw
```

Serviço systemd (`/etc/systemd/system/eleicoes.service`). **O `User=` deve ser o mesmo usuário dono da pasta** (o que fez o `git clone` e o `chown` acima), senão o serviço não consegue criar o banco em `data/` e fica reiniciando com `status=1/FAILURE`. Troque `jpfranca` pelo seu login:

```ini
[Unit]
Description=Painel de apuração 2026
After=network-online.target
Wants=network-online.target

[Service]
User=jpfranca
WorkingDirectory=/opt/eleicoes
ExecStart=/opt/eleicoes/.venv/bin/python app.py --porta 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now eleicoes
systemctl status eleicoes --no-pager | head -5   # deve mostrar "active (running)"
sudo journalctl -u eleicoes -f                   # acompanhar o log (precisa de sudo)
```

Para atualizar o servidor depois de um `git push`:

```bash
cd /opt/eleicoes
git pull
source .venv/bin/activate && pip install -r requirements.txt
sudo systemctl restart eleicoes
```

Problemas comuns:

- `fatal: detected dubious ownership in repository` no `git pull`: a pasta pertence a outro usuário (clone feito com `sudo`). Corrija com `sudo chown -R $USER:$USER /opt/eleicoes`.
- Serviço em `activating (auto-restart)` com `status=1/FAILURE`: quase sempre o `User=` do serviço não é o dono da pasta. Veja o erro com `sudo journalctl -u eleicoes -n 30 --no-pager`.
- Horários do cabeçalho 3 h à frente: versão antiga do código. Faça `git pull` e reinicie; o painel usa sempre o horário de Brasília, mesmo com o servidor em UTC.
- Depois de um `git pull`, a tela não mostra uma novidade (ex.: um pill vazio): cache do navegador ou do Cloudflare com o `app.js` antigo. Ctrl+F5 resolve. Desde o commit de versionamento, a página referencia `app.js?v=<data do arquivo>` e as rotas `/api/*` saem com `Cache-Control: no-store`, então isso não deve mais acontecer.
- `502` intermitente atrás de proxy ou Cloudflare Tunnel: versão antiga calculava o Gráfico 2 dentro da requisição e podia levar 5 s. Desde o commit `calculadora`, todas as respostas são recalculadas numa thread a cada 10 s (`CACHE_API_SEGUNDOS`) e as rotas devolvem o resultado pronto em milissegundos. O campo `calculo` de `/api/status` mostra a hora e a duração do último cálculo.

O servidor embutido do Flask é suficiente para poucos usuários simultâneos. Para expor na internet, coloque um nginx na frente (proxy para `127.0.0.1:8000`).

## O que a interface mostra

Cabeçalho: relógio com a hora atual de Brasília (sincronizado com o servidor; no replay mostra a hora virtual da simulação, avançando na velocidade escolhida), hora dos dados do TSE com “há N min” (fica vermelho no modo real se a apuração já começou e o dado está parado há mais de 10 min), hora da última coleta de cada rotina (nacional/UF, municípios, lista de seções), hora da última atualização da tela, seções apuradas, votos apurados (total e válidos) e situação do 1º turno. O estado do cálculo no servidor continua disponível no campo `calculo` de `/api/status`. **Todos os horários são de Brasília**, o mesmo fuso dos dados do TSE, mesmo que o servidor esteja em UTC (padrão de muitas máquinas Linux) ou o navegador em outro fuso. O fuso fica em `config.FUSO_HORARIO`. Uma rotina fica vermelha se falhou ou se está há mais de 3 minutos sem sucesso.

- **Gráfico 1** – % de votos válidos × horário em que as urnas entraram na totalização. 1º turno (tracejado, por seção, 04/10) e 2º turno (sólido, feed do TSE, 25/10) sobrepostos no mesmo eixo de horas. Hover: %, votos, seções e horário.
- **Gráfico 2** – 2º turno × 1º turno **nas mesmas seções** apuradas. Dois modos, escolhidos automaticamente:
  - *seção*: o TSE publica a lista de seções com boletim disponível (`cs.json`); o painel registra o horário em que cada seção apareceu e soma os votos do 1º turno exatamente dessas seções;
  - *município* (fallback): se a lista vier em lote ou ainda estiver vazia, usa os votos do 1º turno de cada município × fração de seções já apuradas no 2º turno naquele município.
- **Gráfico 3** – mapa (Leaflet + malhas IBGE) por município ou estado, cor do candidato na liderança, intensidade = margem. Alterna 1º/2º turno.
- **Quadro 1** – tabela por região (Norte, Nordeste, Centro-Oeste, Sudeste, Sul, Exterior, Brasil): 1º turno completo, 2º turno apurado (votos, %, brancos, nulos, total apurado, seções), 1º turno nas mesmas seções e Δ em pontos percentuais.
- **Quadro 2** – extrapolação linear sobre o eleitorado apto informado pelo TSE (campo `e.te` do arquivo nacional; 158.745.502 em 25/10/2026). Para forçar um valor fixo, defina `TOTAL_ELEITORES` em `config.py`; a interface mostra a origem do número em uso. *Simples* (votos ÷ fração de seções apuradas no país) e *por município* (cada município escalado pela própria fração apurada; municípios sem apuração recebem as taxas de comparecimento/validade e os percentuais da sua UF).
- **Quadro 3** – “matematicamente eleito”: restantes = eleitorado apto (TSE) − eleitores das seções já totalizadas (pior caso: todos votam válido no adversário). Faltam = ⌊(válidos + restantes)/2⌋ + 1 − votos do candidato. Quando ≤ 0, o banner muda de cor.

## Fontes de dados

| Dado | URL |
|---|---|
| 1º turno por seção | `https://cdn.tse.jus.br/estatistica/sead/eleicoes/eleicoes2026/buweb/bweb_1t_{UF}_051020261403.zip` |
| 2º turno (Brasil/UF/município) | `https://resultados.tse.jus.br/oficial/ele2026/6258/dados/{uf}/{uf}[{mun}]-c0001-e006258-u.json` |
| Lista de seções com boletim | `https://resultados.tse.jus.br/oficial/ele2026/arquivo-urna/3221/config/{uf}/{uf}-p003221-cs.json` |
| Municípios (código TSE → IBGE) | `https://resultados.tse.jus.br/oficial/ele2026/6258/config/mun-e006258-cm.json` |
| Malhas | `https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR?intrarregiao=municipio|UF&qualidade=minima` |

Carga na rede em modo real: 29 requisições a cada 30 s (nacional/UFs), ~5.760 requisições por volta municipal e 28 a cada 60 s (seções).

**Limite de requisições do TSE.** Em 25/10/2026 o `resultados.tse.jus.br` passou a responder `429 Too Many Requests` depois de uma varredura municipal com 32 threads (~70 req/s), e a penalidade durou vários minutos. Por isso o cliente usa um limitador global (`TAXA_MAX_REQ_S = 6` req/s em `config.py`), 6 threads na varredura, pausa de `PAUSA_APOS_429` s quando recebe 429, prioridade para a coleta nacional/UF (a varredura espera enquanto ela roda) e não consulta de novo municípios já 100 % totalizados. Com isso uma volta completa leva ~25 min no início da noite (medido: ~1.000 municípios a cada 4,5 min, sem nenhum 429) e vai encurtando conforme os municípios terminam. O mapa é atualizado parcialmente a cada 500 municípios. O cabeçalho mostra o total de respostas 429 recebidas; se crescer, reduza `TAXA_MAX_REQ_S`.

## Limitações conhecidas

- O TSE não publica votos **por seção** do 2º turno em tempo real de forma agregada; o menor nível em tempo real é o **município**. A camada por seção serve para identificar *quais* seções já foram apuradas (lado do 1º turno da comparação).
- Os válidos do 1º turno somados dos boletins (119.306.034) diferem em 5.246 votos dos válidos do TSE (119.300.788): são votos anulados sub judice que o boletim registra como nominais. Os totais de Flavio e Lula batem exatamente.
- Projeções e “matematicamente eleito” são cálculos deste painel, não do TSE.
- **Nada aparece antes das 17h de Brasília, nem o Exterior.** As seções no exterior (Japão, Austrália etc.) encerram e são apuradas horas antes por causa do fuso, mas o TSE só começa a divulgar qualquer resultado do 2º turno, inclusive o do exterior, depois que as últimas seções do Brasil fecham, às 17h de Brasília. Até lá os arquivos de divulgação ficam congelados com zeros, e é isso que o painel mostra. Conferido em 25/10/2026 às 11h: Exterior 0 de 1.351 seções, Tóquio 0 de 43, nenhum boletim do exterior publicado. Quando a divulgação começar, o Exterior entra nos Gráficos 1 e 2 (dentro do total nacional), na linha “Exterior” do Quadro 1 e na extrapolação do Quadro 2. No mapa não há exterior, porque a malha do IBGE só cobre o Brasil.

## Estrutura

```
app.py              servidor Flask + rotas /api/* + thread Calculadora (recalcula as respostas a cada 10 s)
config.py           constantes (códigos de eleição, URLs, cores, total de eleitores)
tse_client.py       HTTP + parse dos JSON do TSE
primeiro_turno.py   download/processamento do 1º turno por seção
coletor_2t.py       threads de coleta do 2º turno
storage.py          SQLite (série histórica)
analise.py          cálculos dos gráficos e quadros
replay.py           simulação do 2º turno a partir do 1º
static/             index.html, app.js, style.css
data/               cache (1t/, geo/, apuracao.db, replay.db)
```
