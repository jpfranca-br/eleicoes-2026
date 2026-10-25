/* Painel de apuração – frontend. Faz polling nas rotas /api/* a cada 30 s. */
(function () {
  "use strict";
  const INTERVALO = 30; // s
  const CORES = { "22": "#1f5fbf", "13": "#d62828", outros: "#f2c230", sem: "#c9c9c9" };
  const NOMES = { "22": "Flavio Bolsonaro", "13": "Lula" };
  const fmtInt = new Intl.NumberFormat("pt-BR");
  const fmtPct = (v) => (v === null || v === undefined ? "—" : v.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + "%");
  const n = (v) => fmtInt.format(Math.round(v || 0));
  const $ = (id) => document.getElementById(id);
  let proximo = INTERVALO;
  let mapa = null, camada = null, contorno = null, geoCache = {}, dadosMapa = {};

  // ------------------------------------------------------------ util
  // Todos os horários vindos do servidor estão em Brasília; "agoraServidor" é a referência de tempo
  // (não o relógio do navegador), para funcionar de qualquer fuso.
  let agoraServidor = null;
  const paraMs = (ts) => new Date(ts.replace(" ", "T")).getTime();
  function horaDe(ts) { // 'YYYY-MM-DD HH:MM:SS' -> 'HH:MM:SS' (com dia se não for o dia do servidor)
    if (!ts) return "—";
    const [d, h] = ts.split(" ");
    const hoje = agoraServidor ? agoraServidor.slice(0, 10) : d;
    return d === hoje ? h : `${d.slice(8, 10)}/${d.slice(5, 7)} ${h}`;
  }
  function idade(ts) { return ts && agoraServidor ? (paraMs(agoraServidor) - paraMs(ts)) / 1000 : Infinity; }
  function pill(id, valor, estado) {
    const el = $(id); el.querySelector(".valor").textContent = valor;
    el.classList.remove("erro", "ok"); if (estado) el.classList.add(estado);
  }
  async function getJSON(url) { const r = await fetch(url, { cache: "no-store" }); if (!r.ok) throw new Error(url + " " + r.status); return r.json(); }

  // ------------------------------------------------------------ status
  function renderStatus(s) {
    agoraServidor = s.agora;
    pill("pill-tse", s.ts_feed_br ? horaDe(s.ts_feed_br) : "sem dados", s.ts_feed_br ? "ok" : "");
    const col = {}; (s.coletas || []).forEach((c) => (col[c.nome] = c));
    for (const [nome, id] of [["nacional", "pill-nacional"], ["municipios", "pill-municipios"], ["secoes", "pill-secoes"]]) {
      const c = col[nome];
      if (!c || !c.ultimo_ok) { pill(id, c && c.ultimo_erro ? "erro " + horaDe(c.ultimo_erro) : "aguardando", c && c.ultimo_erro ? "erro" : ""); continue; }
      const atrasada = idade(c.ultimo_ok) > 180 || (c.ultimo_erro && c.ultimo_erro > c.ultimo_ok);
      pill(id, horaDe(c.ultimo_ok) + (c.msg ? " · " + c.msg : ""), atrasada ? "erro" : "ok");
    }
    pill("pill-apurado", `${n(s.st)} / ${n(s.ts)} (${fmtPct(s.pst)})`, s.st > 0 ? "ok" : "");
    const t1 = s.turno1 || {};
    const txt1 = t1.ufs_prontas === t1.ufs_total ? `${t1.ufs_total} UFs · ${n(t1.secoes)} seções` : `${t1.ufs_prontas}/${t1.ufs_total} UFs${t1.carregando ? " (baixando…)" : ""}`;
    pill("pill-1t", txt1, t1.ufs_prontas === t1.ufs_total ? "ok" : (t1.erro ? "erro" : ""));
    const avisos = [];
    if (s.replay) avisos.push("MODO SIMULAÇÃO (replay do 1º turno) – não são dados reais do 2º turno.");
    avisos.push(`Gráfico 2 em modo ${s.modo_grafico2 === "secao" ? "SEÇÃO" : "MUNICÍPIO"}: ${s.motivo_grafico2}.`);
    if (t1.erro) avisos.push("Erro no 1º turno: " + t1.erro);
    if (s.respostas_429) avisos.push(`Respostas 429 (limite do TSE) recebidas: ${n(s.respostas_429)}.`);
    $("aviso").textContent = avisos.join(" ");
  }

  // ------------------------------------------------------------ gráficos
  const layoutBase = (titulo) => ({
    margin: { l: 55, r: 20, t: 10, b: 50 },
    hovermode: "x unified",
    legend: { orientation: "h", y: -0.18 },
    xaxis: { type: "date", tickformat: "%H:%M", title: { text: "horário (hora de Brasília)" }, gridcolor: "#eef0f3", hoverformat: "%H:%M:%S" },
    yaxis: { title: { text: "% dos votos válidos" }, ticksuffix: "%", gridcolor: "#eef0f3", rangemode: "tozero" },
    paper_bgcolor: "#fff", plot_bgcolor: "#fff", font: { family: "system-ui, sans-serif", size: 12, color: "#1d2433" },
  });
  const CONFIG = { responsive: true, displaylogo: false, locale: "pt-BR" };

  function traco(nome, x, y, votos, hora, secoes, cor, tracejado) {
    return {
      name: nome, x, y, type: "scatter", mode: "lines",
      line: { color: cor, width: 2, dash: tracejado ? "dash" : "solid" },
      customdata: y.map((_, i) => [n(votos[i]), hora[i], n(secoes[i])]),
      hovertemplate: "%{y:.2f}% · %{customdata[0]} votos · %{customdata[2]} seções · %{customdata[1]}<extra>" + nome + "</extra>",
    };
  }

  function renderG1(g) {
    const t1 = g.turno1, t2 = g.turno2, tr = [];
    if (t1.x.length) {
      tr.push(traco("Flavio 1ºT", t1.x, t1.p22, t1.v22, t1.hora, t1.secoes, CORES["22"], true));
      tr.push(traco("Lula 1ºT", t1.x, t1.p13, t1.v13, t1.hora, t1.secoes, CORES["13"], true));
      tr.push(traco("Outros 1ºT", t1.x, t1.p_outros, t1.v_outros, t1.hora, t1.secoes, CORES.outros, true));
    }
    if (t2.x.length) {
      tr.push(traco("Flavio 2ºT", t2.x, t2.p22, t2.v22, t2.hora, t2.secoes, CORES["22"], false));
      tr.push(traco("Lula 2ºT", t2.x, t2.p13, t2.v13, t2.hora, t2.secoes, CORES["13"], false));
      if (t2.v_outros.some((v) => v > 0)) tr.push(traco("Outros 2ºT", t2.x, t2.p_outros, t2.v_outros, t2.hora, t2.secoes, CORES.outros, false));
    }
    const lay = layoutBase();
    const dia = (t2.x[0] || t1.x[0] || "2026-10-25T17:00:00").slice(0, 10);
    lay.xaxis.range = [dia + "T17:00:00", dia + "T23:59:59"]; // zoom inicial; a barra do Plotly permite ver tudo
    Plotly.react("g1", tr, lay, CONFIG);
  }

  function renderG2(g) {
    const tr = [];
    if (g.x && g.x.length) {
      const a = g.turno2, b = g.turno1;
      tr.push(traco("Flavio 2ºT", g.x, a.p22, a.v22, g.hora, a.secoes, CORES["22"], false));
      tr.push(traco("Lula 2ºT", g.x, a.p13, a.v13, g.hora, a.secoes, CORES["13"], false));
      tr.push(traco("Flavio 1ºT (mesmas seções)", g.x, b.p22, b.v22, g.hora, b.secoes, CORES["22"], true));
      tr.push(traco("Lula 1ºT (mesmas seções)", g.x, b.p13, b.v13, g.hora, b.secoes, CORES["13"], true));
      tr.push(traco("Outros 1ºT (mesmas seções)", g.x, b.p_outros, b.v_outros, g.hora, b.secoes, CORES.outros, true));
    }
    Plotly.react("g2", tr, layoutBase(), CONFIG);
    const modo = g.modo === "secao" ? "por SEÇÃO (conjunto exato das seções já publicadas pelo TSE)" : "por MUNICÍPIO (votos do 1º turno de cada município × fração de seções apuradas no 2º turno)";
    $("g2-sub").textContent = `Eixo x: horário da apuração do 2º turno. Comparação ${modo}. ${g.motivo || ""}`;
  }

  // ------------------------------------------------------------ quadros
  function celVP(v, p) { return `<td>${n(v)}<small>${fmtPct(p)}</small></td>`; }
  function celDelta(d) { if (d === null || d === undefined) return "<td>—</td>"; const s = (d > 0 ? "+" : "") + d.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); return `<td class="${d > 0 ? "pos" : d < 0 ? "neg" : ""}">${s} p.p.</td>`; }

  function renderQ1(q) {
    const h = `<thead><tr><th rowspan="2">Região</th>
      <th class="grupo" colspan="5">1º turno (completo)</th>
      <th class="grupo" colspan="7">2º turno (apurado até agora)</th>
      <th class="grupo" colspan="3">1º turno nas mesmas seções</th>
      <th class="grupo" colspan="2">Δ 2ºT − 1ºT (mesmas seções)</th></tr>
      <tr><th class="grupo-ini">Flavio</th><th>Lula</th><th>Outros</th><th>Válidos</th><th>Seções</th>
      <th class="grupo-ini">Flavio</th><th>Lula</th><th>Válidos</th><th>Brancos</th><th>Nulos</th><th>Total apurado</th><th>Seções</th>
      <th class="grupo-ini">Flavio</th><th>Lula</th><th>Seções</th>
      <th class="grupo-ini">Flavio</th><th>Lula</th></tr></thead>`;
    const linhas = q.linhas.map((l) => {
      const a = l.turno1, b = l.turno2, m = l.mesmas;
      return `<tr class="${l.regiao === "Brasil" ? "total" : ""}"><td>${l.regiao}</td>
        <td class="grupo-ini">${n(a.v22)}<small>${fmtPct(a.p22)}</small></td>${celVP(a.v13, a.p13)}${celVP(a.v_outros, a.p_outros)}<td>${n(a.vv)}</td><td>${n(a.secoes)}</td>
        <td class="grupo-ini">${n(b.v22)}<small>${fmtPct(b.p22)}</small></td>${celVP(b.v13, b.p13)}<td>${n(b.vv)}</td><td>${n(b.brancos)}</td><td>${n(b.nulos)}</td><td>${n(b.total_apurado)}</td><td>${n(b.secoes)} / ${n(b.secoes_total)}<small>${fmtPct(b.pst)}</small></td>
        <td class="grupo-ini">${n(m.v22)}<small>${fmtPct(m.p22)}</small></td>${celVP(m.v13, m.p13)}<td>${n(m.secoes)}</td>
        <td class="grupo-ini">${celDelta(l.delta22).replace(/^<td/, "<span").replace(/<\/td>$/, "</span>")}</td>${celDelta(l.delta13)}</tr>`;
    }).join("");
    $("q1").innerHTML = h + "<tbody>" + linhas + "</tbody>";
    $("q1-sub").textContent = `“Mesmas seções” calculado em modo ${q.modo === "secao" ? "SEÇÃO" : "MUNICÍPIO (proporcional)"}. Percentuais sobre votos válidos.`;
  }

  function renderQ2(q) {
    $("q2-total").textContent = n(q.total_eleitores);
    $("q2-origem").textContent = `(origem: ${q.origem_total})`;
    const col = (t, d) => d ? `<td>${n(d.v22)}<small>${fmtPct(d.p22)}</small></td><td>${n(d.v13)}<small>${fmtPct(d.p13)}</small></td><td>${n(d.vv)}</td><td class="${d.vencedor === "22" ? "lider22" : d.vencedor === "13" ? "lider13" : ""}">${d.vencedor ? NOMES[d.vencedor] : "—"}</td>` : `<td colspan="4">aguardando apuração</td>`;
    $("q2").innerHTML = `<thead><tr><th></th><th>Flavio (22)</th><th>Lula (13)</th><th>Válidos</th><th>Vencedor</th></tr></thead><tbody>
      <tr><td>Apurado até agora (${fmtPct(q.pst)} das seções)</td>${col("apurado", { ...q.apurado, vencedor: q.apurado.v22 > q.apurado.v13 ? "22" : q.apurado.v13 > q.apurado.v22 ? "13" : null })}</tr>
      <tr><td>Projeção linear simples (nacional)</td>${col("simples", q.simples)}</tr>
      <tr class="total"><td>Projeção linear por município</td>${col("mun", q.por_municipio)}</tr></tbody>`;
    const pm = q.por_municipio;
    $("q2-rodape").textContent = `Eleitores das seções apuradas: ${n(q.eleitores_apurados)} · restantes: ${n(q.restantes)} · seções: ${n(q.secoes_apuradas)} de ${n(q.secoes_total)}` +
      (pm ? ` · municípios com apuração: ${n(pm.municipios_com_dados)}, sem: ${n(pm.municipios_sem_dados)}` : "");
  }

  function renderQ3(q) {
    const el = $("quadro3"); el.classList.remove("aguardando", "eleito22", "eleito13");
    const f22 = q.faltam["22"], f13 = q.faltam["13"];
    const detalhe = `Meta (50% + 1 dos válidos possíveis): ${n(q.meta)} votos · válidos apurados: ${n(q.vv)} · eleitores restantes (pior caso): ${n(q.restantes)} de ${n(q.total_eleitores)} aptos (${q.origem_total})` + (q.ts_feed ? ` · dados do TSE de ${horaDe(q.ts_feed)}` : "");
    if (q.eleito) {
      el.classList.add("eleito" + q.eleito);
      $("q3-corpo").innerHTML = `<span class="grande">${q.nome_eleito} está MATEMATICAMENTE ELEITO</span>
        Flavio: ${n(q.votos["22"])} votos · Lula: ${n(q.votos["13"])} votos<span class="detalhe">${detalhe}</span>`;
    } else {
      el.classList.add("aguardando");
      $("q3-corpo").innerHTML = `Ninguém matematicamente eleito ainda.
        <span class="detalhe">Faltam <b>${n(f22)}</b> votos para Flavio Bolsonaro (tem ${n(q.votos["22"])}) · faltam <b>${n(f13)}</b> votos para Lula (tem ${n(q.votos["13"])})</span>
        <span class="detalhe">${detalhe}</span>`;
    }
  }

  // ------------------------------------------------------------ mapa
  function estiloDe(f) {
    const d = dadosMapa[f.properties.codarea];
    if (!d || !d.lider || d.lider === "empate") return { fillColor: CORES.sem, fillOpacity: 0.6, color: "#fff", weight: 0.4 };
    const cor = d.lider === "22" ? CORES["22"] : d.lider === "13" ? CORES["13"] : CORES.outros;
    const margem = Math.abs(d.p22 - d.p13);
    return { fillColor: cor, fillOpacity: 0.3 + Math.min(margem, 40) / 40 * 0.65, color: "#fff", weight: 0.4 };
  }
  function tooltipDe(f) {
    const d = dadosMapa[f.properties.codarea];
    if (!d) return `<b>${f.properties.codarea}</b><br>sem dados`;
    return `<b>${d.nome}${d.uf && d.uf !== d.nome ? " – " + d.uf : ""}</b><br>
      <span style="color:${CORES["22"]}">■</span> Flavio: ${n(d.v22)} (${fmtPct(d.p22)})<br>
      <span style="color:${CORES["13"]}">■</span> Lula: ${n(d.v13)} (${fmtPct(d.p13)})<br>` +
      (d.v_outros ? `<span style="color:${CORES.outros}">■</span> Outros: ${n(d.v_outros)} (${fmtPct(d.p_outros)})<br>` : "") +
      `Válidos: ${n(d.vv)} · seções: ${n(d.st)}/${n(d.ts)} (${fmtPct(d.pst)})`;
  }
  async function renderMapa() {
    const turno = $("mapa-turno").value, nivel = $("mapa-nivel").value;
    if (!mapa) {
      mapa = L.map("mapa", { preferCanvas: true, zoomSnap: 0.5, attributionControl: false }).setView([-15, -54], 4);
    }
    const carregaGeo = (n) => geoCache[n] || getJSON(n === "uf" ? "/geo/ufs.geojson" : "/geo/municipios.geojson").then((g) => (geoCache[n] = g));
    const [geo, geoUf, dados] = await Promise.all([carregaGeo(nivel), carregaGeo("uf"), getJSON(`/api/mapa?turno=${turno}&nivel=${nivel}`)]);
    dadosMapa = dados;
    if (camada && camada._nivel !== nivel) { camada.remove(); camada = null; }
    if (!camada) {
      camada = L.geoJSON(geo, { style: estiloDe, onEachFeature: (f, l) => l.bindTooltip(() => tooltipDe(f), { sticky: true, className: "mapa-tip" }) }).addTo(mapa);
      camada._nivel = nivel;
      if (contorno) contorno.remove();
      contorno = L.geoJSON(geoUf, { style: { fill: false, color: nivel === "uf" ? "#fff" : "#555", weight: nivel === "uf" ? 1 : 0.8 }, interactive: false }).addTo(mapa);
    } else {
      camada.eachLayer((l) => l.setStyle(estiloDe(l.feature)));
    }
    let c22 = 0, c13 = 0, cs = 0;
    Object.values(dados).forEach((d) => { if (d.lider === "22") c22++; else if (d.lider === "13") c13++; else cs++; });
    const un = nivel === "uf" ? "estados" : "municípios";
    $("mapa-info").textContent = `Flavio lidera em ${n(c22)} ${un}, Lula em ${n(c13)}; ${n(cs)} sem dados/empate.`;
  }

  // ------------------------------------------------------------ ciclo
  async function atualizar() {
    try {
      const [s, g1, g2, q] = await Promise.all([getJSON("/api/status"), getJSON("/api/grafico1"), getJSON("/api/grafico2"), getJSON("/api/quadros")]);
      renderStatus(s); renderG1(g1); renderG2(g2); renderQ1(q.quadro1); renderQ2(q.quadro2); renderQ3(q.quadro3);
      renderMapa().catch((e) => { console.error(e); $("mapa-info").textContent = "mapa: " + e.message; });
      pill("pill-tela", (agoraServidor ? agoraServidor.slice(11) : new Date().toLocaleTimeString("pt-BR")) + " (Brasília)", "ok");
    } catch (e) {
      console.error(e);
      pill("pill-tela", "falha: " + e.message, "erro");
    }
    proximo = INTERVALO;
  }
  setInterval(() => {
    proximo -= 1;
    const el = $("pill-tela").querySelector(".valor");
    el.textContent = el.textContent.replace(/ \(próx\. \d+s\)$/, "") + ` (próx. ${Math.max(proximo, 0)}s)`;
    if (proximo <= 0) atualizar();
  }, 1000);
  $("mapa-turno").addEventListener("change", () => renderMapa());
  $("mapa-nivel").addEventListener("change", () => renderMapa());
  atualizar();
})();
