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
sudo chown -R $USER:$USER /opt/eleicoes
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

Abra `http://<ip-do-servidor>:8000/`. A página se atualiza a cada 30 s. A série histórica fica em `data/apuracao.db` (SQLite), então reiniciar o programa não perde o que já foi coletado. Use `--limpar` para zerar.

### 3. Simulação (testar antes das 17h)

```bash
python app.py --replay                       # reproduz o 1º turno como se fosse o 2º (60x mais rápido)
python app.py --replay --velocidade 300      # 300 s virtuais por segundo real
python app.py --replay --forcar-2-candidatos # redistribui "outros" entre 22 e 13 (testa o "matematicamente eleito")
python app.py --replay --lote                # publica a lista de seções só no fim (testa o fallback por município)
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
| `--replay`, `--velocidade`, `--inicio HH:MM`, `--forcar-2-candidatos`, `--lote` | simulação |
| `--debug` | log detalhado |

## Linux: firewall e serviço

```bash
sudo ufw allow 8000/tcp        # se usar ufw
```

Serviço systemd (`/etc/systemd/system/eleicoes.service`):

```ini
[Unit]
Description=Painel de apuração 2026
After=network-online.target
Wants=network-online.target

[Service]
User=www-data
WorkingDirectory=/opt/eleicoes
ExecStart=/opt/eleicoes/.venv/bin/python app.py --porta 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo chown -R www-data:www-data /opt/eleicoes
sudo systemctl daemon-reload
sudo systemctl enable --now eleicoes
sudo journalctl -u eleicoes -f      # acompanhar o log
```

O servidor embutido do Flask é suficiente para poucos usuários simultâneos. Para expor na internet, coloque um nginx na frente (proxy para `127.0.0.1:8000`).

## O que a interface mostra

Cabeçalho: hora dos dados do TSE, hora da última coleta de cada rotina (nacional/UF, municípios, lista de seções), hora da última atualização da tela, seções apuradas e situação do 1º turno. **Todos os horários são de Brasília**, o mesmo fuso dos dados do TSE, mesmo que o servidor esteja em UTC (padrão de muitas máquinas Linux) ou o navegador em outro fuso. O fuso fica em `config.FUSO_HORARIO`. Uma rotina fica vermelha se falhou ou se está há mais de 3 minutos sem sucesso.

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
app.py              servidor Flask + rotas /api/*
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
