(() => {
  const state = {
    board: null,
    periods: null,
    form: {},
    formLoading: new Set(),
    tab: "live",
    wp: { board: null, season: null, day: null, loading: false, error: null },
    filter: "all",
    expanded: new Set(),
    dates: "",
    timer: null,
    loading: false,
    error: null,
  };

  const LIVE_REFRESH_MS = 20000;

  const $ = (sel) => document.querySelector(sel);

  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);

  function stopLive() {
    if (state.timer) {
      clearInterval(state.timer);
      state.timer = null;
    }
  }

  function scheduleLive() {
    stopLive();
    let ticks = 0;
    state.timer = setInterval(() => {
      if (document.visibilityState !== "visible") return;
      loadLive();
      ticks += 1;
      if (ticks % 3 === 0) loadPeriods();
      if (ticks % 3 === 0 && state.tab === "winprob") loadWinProb();
    }, LIVE_REFRESH_MS);
  }

  function fmtPct(pct, games) {
    if (!games) return "–";
    return `${pct}%`;
  }

  function fmtMix(mix) {
    if (!mix) return "";
    const bits = [];
    if (mix.preseason) bits.push(`${mix.preseason} preseason`);
    if (mix.regular) bits.push(`${mix.regular} regular`);
    if (mix.playoffs) bits.push(`${mix.playoffs} playoff`);
    return bits.join(" · ");
  }

  function metricHtml(row, bucket, kind) {
    const games = row?.games || 0;
    const pct = bucket?.pct || 0;
    const count = bucket?.count || 0;
    const width = games ? Math.max(pct, count ? 2 : 0) : 0;
    return `
      <div class="p1-metric ${kind}">
        <span class="p1-pct">${fmtPct(pct, games)}</span>
        <span class="p1-count">${games ? count : "–"}</span>
        <span class="p1-bar"><span style="width:${width}%"></span></span>
      </div>`;
  }

  function renderPeriods() {
    const grid = $("#p1Grid");
    const legend = $("#p1Legend");
    const note = $("#p1Note");
    const board = state.periods;
    if (!board || board.error && !board.season) {
      grid.innerHTML = `<p class="p1-error">${esc(board?.error || "Distribution unavailable")}</p>`;
      legend.innerHTML = "";
      return;
    }
    const season = board.season || {};
    const recent = board.recent || {};
    const buckets = season.buckets || recent.buckets || [];
    if (!buckets.length) {
      grid.innerHTML = `<p class="p1-loading">No completed 1st periods yet.</p>`;
      legend.innerHTML = "";
      return;
    }
    const seasonMix = season.games ? `${season.games} games` : "no games yet";
    const recentMix = recent.games ? `${recent.games} games` : "no games yet";
    legend.innerHTML = `
      <span><i class="p1-swatch season"></i><b>Season</b> ${esc(season.detail || "")} · ${esc(seasonMix)}</span>
      <span><i class="p1-swatch recent"></i><b>Past 30 days</b> ${esc(fmtMix(recent.mix) || recent.detail || "")} · ${esc(recentMix)}</span>
    `;
    if (board.stale) note.textContent = "How the first period ends. Showing the last saved sample.";
    grid.innerHTML = buckets
      .map((bucket, index) => {
        const recentBucket = (recent.buckets || [])[index] || bucket;
        return `
          <article class="p1-cell">
            <h3 class="p1-score">${esc(bucket.name || bucket.label)}</h3>
            ${metricHtml(season, bucket, "season")}
            ${metricHtml(recent, recentBucket, "recent")}
          </article>`;
      })
      .join("");
  }

  async function loadPeriods() {
    try {
      const res = await fetch("/api/periods");
      const board = await res.json();
      if (!res.ok) throw new Error(board.error || `HTTP ${res.status}`);
      state.periods = board;
    } catch (err) {
      state.periods = state.periods || { error: err.message || String(err) };
      if (!state.periods.season) state.periods.error = err.message || String(err);
    }
    renderPeriods();
  }

  async function loadLive(force = false) {
    if (state.loading) return;
    state.loading = true;
    $("#liveRefresh").disabled = true;
    const params = new URLSearchParams();
    if (force) params.set("force", "1");
    if (state.dates) params.set("dates", state.dates);
    const q = params.toString();
    try {
      const res = await fetch(`/api/live${q ? `?${q}` : ""}`);
      const board = await res.json();
      if (!res.ok) throw new Error(board.error || `HTTP ${res.status}`);
      state.board = board;
      state.error = board.error || null;
      if (!state.dates && board.day) state.anchor = board.day;
    } catch (err) {
      state.error = err.message || String(err);
    } finally {
      state.loading = false;
      $("#liveRefresh").disabled = false;
    }
    renderLive();
  }

  function fmtKickoff(iso) {
    if (!iso) return "TBD";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "TBD";
    // NHL faceoffs are published in Eastern time, same as the ESPN slate.
    return d.toLocaleString("en-US", {
      weekday: "short",
      hour: "numeric",
      minute: "2-digit",
      timeZone: "America/New_York",
    });
  }

  function fmtUpdated(epoch) {
    if (!epoch) return "";
    return new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function fmtDay(iso) {
    if (!iso) return "Today";
    const [y, m, d] = iso.split("-").map(Number);
    if (!y || !m || !d) return iso;
    const dt = new Date(Date.UTC(y, m - 1, d));
    return dt.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
  }

  function shiftIso(iso, delta) {
    const [y, m, d] = iso.split("-").map(Number);
    const dt = new Date(Date.UTC(y, m - 1, d));
    dt.setUTCDate(dt.getUTCDate() + delta);
    const mm = String(dt.getUTCMonth() + 1).padStart(2, "0");
    const dd = String(dt.getUTCDate()).padStart(2, "0");
    return `${dt.getUTCFullYear()}-${mm}-${dd}`;
  }

  function ymd(iso) {
    return (iso || "").replaceAll("-", "");
  }

  function hexOf(raw) {
    let hex = String(raw || "").replace("#", "").trim();
    if (!/^[0-9a-fA-F]{3}$|^[0-9a-fA-F]{6}$/.test(hex)) return null;
    if (hex.length === 3) hex = hex.split("").map((c) => c + c).join("");
    return hex.toLowerCase();
  }

  function luminance(hex) {
    const r = parseInt(hex.slice(0, 2), 16);
    const g = parseInt(hex.slice(2, 4), 16);
    const b = parseInt(hex.slice(4, 6), 16);
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
  }

  function teamColor(team) {
    const primary = hexOf(team.color) || "555555";
    const alt = hexOf(team.alt_color);
    let hex = primary;
    if (luminance(primary) < 0.22 && alt && luminance(alt) > luminance(primary)) hex = alt;
    if (luminance(hex) < 0.22) {
      const lift = (n) => Math.min(255, n + 96);
      const r = lift(parseInt(hex.slice(0, 2), 16));
      const g = lift(parseInt(hex.slice(2, 4), 16));
      const b = lift(parseInt(hex.slice(4, 6), 16));
      hex = [r, g, b].map((n) => n.toString(16).padStart(2, "0")).join("");
    }
    return `#${hex}`;
  }

  function rinkSvg(game) {
    const lp = game.situation?.last_play;
    const x = lp && typeof lp.x === "number" ? Math.max(-100, Math.min(100, lp.x)) : null;
    const y = lp && typeof lp.y === "number" ? Math.max(-42, Math.min(42, lp.y)) : null;
    const puck =
      x === null
        ? ""
        : `<circle class="puck" cx="${(x + 100).toFixed(1)}" cy="${(42.5 - y).toFixed(1)}" r="2.1" />`;
    return `
      <svg class="rink" viewBox="0 0 200 85" role="img" aria-label="Rink, last play">
        <rect class="ice" x="1" y="1" width="198" height="83" rx="16" />
        <line class="goal-line" x1="11" y1="8" x2="11" y2="77" />
        <line class="goal-line" x1="189" y1="8" x2="189" y2="77" />
        <line class="blue-line" x1="75" y1="1" x2="75" y2="84" />
        <line class="blue-line" x1="125" y1="1" x2="125" y2="84" />
        <line class="red-line" x1="100" y1="1" x2="100" y2="84" />
        <circle class="center-circle" cx="100" cy="42.5" r="15" />
        <circle class="center-dot" cx="100" cy="42.5" r="1.3" />
        <circle class="faceoff" cx="69" cy="22" r="1" />
        <circle class="faceoff" cx="69" cy="63" r="1" />
        <circle class="faceoff" cx="131" cy="22" r="1" />
        <circle class="faceoff" cx="131" cy="63" r="1" />
        <path class="crease" d="M11 34 v17 a8 8 0 0 0 0 -17 z" />
        <path class="crease" d="M189 34 v17 a8 8 0 0 1 0 -17 z" />
        ${puck}
      </svg>`;
  }

  function statNumber(value) {
    if (value === undefined || value === null || value === "") return null;
    const n = Number(String(value).replace(/[%,]/g, "").split(/[-/:]/)[0]);
    return Number.isFinite(n) ? n : null;
  }

  function statRow(label, displayKey, game, barKey) {
    const a = game.away.stats?.[displayKey];
    const h = game.home.stats?.[displayKey];
    if ((a === undefined || a === "") && (h === undefined || h === "")) return "";
    const an = statNumber(game.away.stats?.[barKey || displayKey]);
    const hn = statNumber(game.home.stats?.[barKey || displayKey]);
    const total = (an ?? 0) + (hn ?? 0);
    const aw = total > 0 && an !== null ? Math.round((an / total) * 100) : 50;
    const hw = 100 - aw;
    return `
      <div class="stat-row">
        <span class="sv away">${esc(a ?? "–")}</span>
        <span class="sbar" role="img" aria-label="${esc(label)}">
          <span class="seg a" style="width:${aw}%;background:${teamColor(game.away)}"></span>
          <span class="seg h" style="width:${hw}%;background:${teamColor(game.home)}"></span>
        </span>
        <span class="sv home">${esc(h ?? "–")}</span>
        <span class="sl">${esc(label)}</span>
      </div>`;
  }

  function faceoffRow(game) {
    const line = (side) => {
      const n = side.stats?.faceoffs;
      const p = side.stats?.faceoff_pct;
      if ((n === undefined || n === "") && (p === undefined || p === "")) return "";
      if (p) return `${n ?? "–"} · ${p}%`;
      return String(n);
    };
    const a = line(game.away);
    const h = line(game.home);
    if (!a && !h) return "";
    const fake = {
      away: { stats: { faceoff_line: a, faceoffs: game.away.stats?.faceoffs }, color: game.away.color, alt_color: game.away.alt_color },
      home: { stats: { faceoff_line: h, faceoffs: game.home.stats?.faceoffs }, color: game.home.color, alt_color: game.home.alt_color },
    };
    return statRow("Faceoffs", "faceoff_line", fake, "faceoffs");
  }

  function statusBadge(game) {
    if (game.state === "in") {
      return `<span class="status live"><i class="dot"></i>${esc(game.period_label)}</span>`;
    }
    if (game.state === "post") {
      return `<span class="status final">${esc(game.period_label || "Final")}</span>`;
    }
    if (/postponed|canceled|delayed|suspended/i.test(game.period_label || "")) {
      return `<span class="status pre">${esc(game.period_label)}</span>`;
    }
    return `<span class="status pre">${esc(fmtKickoff(game.date))}</span>`;
  }

  function teamRow(game, side) {
    const t = game[side];
    const onPp = game.situation?.power_play === side;
    const loser = game.state === "post" && !t.winner && game.home.score !== game.away.score;
    const logo =
      t.logo && String(t.logo).startsWith("https://")
        ? `<img class="logo" src="${esc(t.logo)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'" />`
        : `<span class="logo logo-fallback">${esc(t.abbr.slice(0, 2))}</span>`;
    return `
      <div class="team-row ${side} ${onPp ? "on-pp" : ""} ${t.winner ? "winner" : ""} ${loser ? "loser" : ""}"
           style="--team:${teamColor(t)}">
        ${logo}
        <span class="tname">
          <b>${esc(t.abbr)}</b>
          <span class="full">${esc(t.name)}</span>
          ${t.record ? `<small class="rec">${esc(t.record)}</small>` : ""}
        </span>
        ${onPp ? `<span class="pp-pill">PP</span>` : "<span></span>"}
        <span class="tscore">${game.state === "pre" ? "" : t.score}</span>
      </div>`;
  }

  function goalLog(game) {
    const goals = game.goals || [];
    if (!goals.length) return "";
    return `<div class="goals">${goals
      .map((g) => {
        const team = g.side === "home" || g.side === "away" ? game[g.side] : null;
        const color = team ? teamColor(team) : "rgba(180,210,200,0.4)";
        const assists = (g.assists || []).length ? `<small>${esc(g.assists.join(", "))}</small>` : "";
        return `
          <div class="goal" style="--team:${color}" title="${esc(g.text || "")}">
            <span class="when">${esc(g.when)}</span>
            <span class="who"><b>${esc(g.scorer)}</b>${assists}</span>
            ${g.tag ? `<span class="tag">${esc(g.tag)}</span>` : "<span></span>"}
          </div>`;
      })
      .join("")}</div>`;
  }

  function periodHead(index, playoffs) {
    if (index < 3) return String(index + 1);
    if (!playoffs) return index === 3 ? "OT" : "SO";
    const n = index - 2;
    return n === 1 ? "OT" : `${n}OT`;
  }

  function linescore(game) {
    const a = game.away.linescores || [];
    const h = game.home.linescores || [];
    const n = Math.max(a.length, h.length);
    if (!n || game.state === "pre") return "";
    const heads = Array.from({ length: n }, (_, i) => periodHead(i, game.playoffs));
    const cells = (arr) => heads.map((_, i) => `<span>${arr[i] ?? "–"}</span>`).join("");
    return `
      <div class="linescore" style="grid-template-columns: 2.6rem repeat(${n}, minmax(0, 1fr)) 1.6rem">
        <span class="lsn"></span>${heads.map((x) => `<span class="lsh">${x}</span>`).join("")}<span class="lsh">T</span>
        <span class="lsn">${esc(game.away.abbr)}</span>${cells(a)}<span class="lst">${game.away.score}</span>
        <span class="lsn">${esc(game.home.abbr)}</span>${cells(h)}<span class="lst">${game.home.score}</span>
      </div>`;
  }

  function goalTimeline(game) {
    if (game.state === "pre") return "";
    const goals = (game.goals || []).filter((g) => typeof g.at === "number");
    const reg = 60 * 60;
    const otLen = game.ot_seconds || 300;
    const maxPeriod = Math.max(game.period || 0, 3, ...goals.map((g) => g.period || 0));
    let maxSec = reg;
    if (game.playoffs && maxPeriod > 3) maxSec = maxPeriod * 20 * 60;
    else if (maxPeriod > 3) maxSec = reg + otLen;
    const now = game.state === "in" ? Math.max(0, Math.min(maxSec, game.elapsed_sec || 0)) : maxSec;
    const W = 320;
    const H = 58;
    const padL = 26;
    const padR = 8;
    const axisY = 28;
    const xAt = (sec) => padL + (Math.max(0, Math.min(maxSec, sec)) / maxSec) * (W - padL - padR);
    const ticks = [20, 40, 60].filter((m) => m * 60 <= maxSec);
    if (maxSec > reg + 30) ticks.push(Math.round(maxSec / 60));
    const seen = new Map();
    const marks = goals
      .map((g) => {
        const bucket = `${g.side}:${Math.round(g.at / 25)}`;
        const n = seen.get(bucket) || 0;
        seen.set(bucket, n + 1);
        const home = g.side !== "away";
        const x = xAt(g.at) + (home ? 1 : -1) * n * 3.4;
        const color = teamColor(home ? game.home : game.away);
        const y1 = home ? axisY - 16 : axisY + 2;
        const y2 = home ? axisY - 2 : axisY + 16;
        const cy = home ? axisY - 11 : axisY + 11;
        const title = [g.when, g.score, g.scorer, g.tag].filter(Boolean).join(" ");
        return `<g class="tl-goal"><title>${esc(title)}</title><line x1="${x.toFixed(1)}" y1="${y1}" x2="${x.toFixed(1)}" y2="${y2}" stroke="${color}"/><circle cx="${x.toFixed(1)}" cy="${cy}" r="3.3" fill="${color}"/></g>`;
      })
      .join("");
    const tickMarks = ticks
      .map((m) => {
        const tx = xAt(m * 60).toFixed(1);
        const label = m > 60 ? (game.playoffs ? periodHead(Math.round(m / 20) - 1, true) : "OT") : String(m);
        return `<line x1="${tx}" y1="${axisY - 4}" x2="${tx}" y2="${axisY + 4}" class="tl-ht"/><text x="${tx}" y="${H - 2}" class="tl-label" text-anchor="middle">${esc(label)}</text>`;
      })
      .join("");
    const nowX = xAt(now).toFixed(1);
    return `
      <svg class="goal-tl" viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="Goal times, home above the line and away below. ${esc(game.home.abbr)} ${esc(game.home.score)}, ${esc(game.away.abbr)} ${esc(game.away.score)}">
        <text x="1" y="12" class="tl-side" fill="${teamColor(game.home)}">${esc(game.home.abbr)}</text>
        <text x="1" y="44" class="tl-side" fill="${teamColor(game.away)}">${esc(game.away.abbr)}</text>
        <line x1="${padL}" y1="${axisY}" x2="${W - padR}" y2="${axisY}" class="tl-axis"/>
        <line x1="${padL}" y1="${axisY}" x2="${nowX}" y2="${axisY}" class="tl-progress"/>
        ${game.state === "in" ? `<line x1="${nowX}" y1="8" x2="${nowX}" y2="${H - 14}" class="tl-now"/>` : ""}
        ${tickMarks}
        <text x="${padL}" y="${H - 2}" class="tl-label">0</text>
        ${marks}
      </svg>`;
  }

  function pairRow(label, awayHtml, homeHtml) {
    if (!awayHtml && !homeHtml) return "";
    return `
      <div class="goalie-row">
        <span class="side away">${awayHtml || ""}</span>
        <span class="mid-label">${esc(label)}</span>
        <span class="side home">${homeHtml || ""}</span>
      </div>`;
  }

  function goalieCell(list) {
    if (!list || !list.length) return "";
    return list
      .map((g) => {
        const bits = [g.saves && `${g.saves} SV`, g.save_pct, g.goals_against !== "" && g.goals_against !== undefined ? `${g.goals_against} GA` : "", g.toi]
          .filter(Boolean)
          .join(" · ");
        return `<span class="tender"><b>${esc(g.name)}</b><small>${esc(bits)}</small></span>`;
      })
      .join("");
  }

  function leaderCell(leader) {
    if (!leader) return "";
    return `<b>${esc(leader.name)}</b><small>${esc(leader.line)}</small>`;
  }

  function starLines(game) {
    const stars = game.stars || [];
    if (!stars.length) return "";
    return `<div class="stars">${stars
      .map((s) => {
        const team = s.side === "home" || s.side === "away" ? game[s.side] : null;
        const who = team ? `${s.name} · ${team.abbr}` : s.name;
        return `<div class="star-row"><span class="side"><b>★${esc(s.rank)}</b> ${esc(who)}</span><span></span><span class="side home"><small>${esc(s.line)}</small></span></div>`;
      })
      .join("")}</div>`;
  }

  function shotPair(game, away, home) {
    return `<span class="sp"><b style="color:${teamColor(game.away)}">${away}</b><i>–</i><b style="color:${teamColor(game.home)}">${home}</b></span>`;
  }

  function periodShots(game) {
    const rows = game.period_shots || [];
    if (!rows.length || game.state === "pre") return "";
    const awayShots = rows.reduce((sum, row) => sum + (row.away_shots || 0), 0);
    const homeShots = rows.reduce((sum, row) => sum + (row.home_shots || 0), 0);
    const awaySog = rows.reduce((sum, row) => sum + (row.away_sog || 0), 0);
    const homeSog = rows.reduce((sum, row) => sum + (row.home_sog || 0), 0);
    const head = rows.map((row) => `<span class="lsh">${esc(row.label)}</span>`).join("");
    const line = (keyA, keyH, totalA, totalH) =>
      rows.map((row) => shotPair(game, row[keyA] || 0, row[keyH] || 0)).join("") + shotPair(game, totalA, totalH);
    return `
      <div class="period-shots" style="grid-template-columns: 3.4rem repeat(${rows.length + 1}, minmax(0, 1fr))" aria-label="Shots and shots on goal by period, ${esc(game.away.abbr)} then ${esc(game.home.abbr)}">
        <span></span>${head}<span class="lsh">T</span>
        <span class="lsn" title="Shot attempts: on goal, missed, and blocked">Shots</span>${line("away_shots", "home_shots", awayShots, homeShots)}
        <span class="lsn" title="Shots on goal">On goal</span>${line("away_sog", "home_sog", awaySog, homeSog)}
      </div>`;
  }

  const RESULT_WORD = { W: "win", L: "loss", OTL: "overtime loss", SOL: "shootout loss", T: "tie" };

  function fmtFormDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "America/New_York" });
  }

  function formCell(p) {
    const sog = p.sog_f != null && p.sog_a != null ? `${p.sog_f}–${p.sog_a}` : "–";
    const cls = p.gf > p.ga ? "up" : p.gf < p.ga ? "down" : "";
    const title = p.shootout
      ? `Shootout ${p.gf}–${p.ga}`
      : `Period ${p.label}: goals ${p.gf}–${p.ga}, shots on goal ${sog}${p.shots_f != null ? `, attempts ${p.shots_f}–${p.shots_a}` : ""}`;
    return `<span class="f5-cell ${cls}" title="${esc(title)}"><b>${p.gf}–${p.ga}</b><small>${p.shootout ? "SO" : esc(sog)}</small></span>`;
  }

  function formTeamHtml(team, games, limit) {
    const color = teamColor(team);
    if (!games) {
      return `<div class="f5-team" style="--team:${color}"><span class="f5-name">${esc(team.abbr)}</span><span class="f5-empty">last ${limit}…</span></div>`;
    }
    if (!games.length) {
      return `<div class="f5-team" style="--team:${color}"><span class="f5-name">${esc(team.abbr)}</span><span class="f5-empty">no finished games yet</span></div>`;
    }
    const maxPeriods = Math.max(...games.map((g) => (g.periods || []).length), 3);
    const head = Array.from({ length: maxPeriods }, (_, i) => {
      const label = games.find((g) => g.periods[i])?.periods[i]?.label || String(i + 1);
      return `<span class="f5-h">${esc(label)}</span>`;
    }).join("");
    const rows = games
      .map((g) => {
        const res = String(g.result || "");
        const cls = res === "W" ? "w" : res === "T" ? "t" : "l";
        const vs = `${g.venue === "home" ? "v" : "@"} ${g.opponent}`;
        const when = fmtFormDate(g.date);
        const title = `${when ? `${when} · ` : ""}${vs} · ${RESULT_WORD[res] || res} ${g.gf}–${g.ga}${g.detail && /OT|SO/.test(g.detail) ? ` (${g.detail.replace("Final/", "")})` : ""}${g.preseason ? " · preseason" : ""}`;
        const cells = Array.from({ length: maxPeriods }, (_, i) => (g.periods[i] ? formCell(g.periods[i]) : `<span class="f5-cell none"></span>`)).join("");
        return `
          <span class="f5-opp" title="${esc(title)}"><em>${esc(vs)}</em>${g.preseason ? `<i class="f5-pre">pre</i>` : ""}</span>
          <span class="f5-res ${cls}" title="${esc(title)}"><b>${esc(res)}</b><small>${g.gf}–${g.ga}</small></span>
          ${cells}`;
      })
      .join("");
    return `
      <div class="f5-team" style="--team:${color}; grid-template-columns: 3.9rem 2.1rem repeat(${maxPeriods}, minmax(0, 1fr))">
        <span class="f5-name">${esc(team.abbr)}</span><span class="f5-h"></span>${head}
        ${rows}
      </div>`;
  }

  function lastFiveHtml(game) {
    const form = state.form[game.id];
    const limit = form?.limit || 5;
    return `
      <section class="last5" data-form-for="${esc(game.id)}" aria-label="Last ${limit} games for each team">
        <header class="f5-head">
          <span class="f5-label">Last ${limit}</span>
          <span class="f5-key">oldest → newest · goals <small>shots on goal</small></span>
        </header>
        ${formTeamHtml(game.away, form?.away, limit)}
        ${formTeamHtml(game.home, form?.home, limit)}
      </section>`;
  }

  async function loadForm(game) {
    if (!game || state.form[game.id] || state.formLoading.has(game.id)) return;
    if (!game.home?.id || !game.away?.id) return;
    state.formLoading.add(game.id);
    const params = new URLSearchParams({ home: game.home.id, away: game.away.id, game: game.id });
    if (state.board?.season_type) params.set("season_type", String(state.board.season_type));
    try {
      const res = await fetch(`/api/form?${params}`);
      const form = await res.json();
      if (!res.ok) throw new Error(form.error || `HTTP ${res.status}`);
      state.form[game.id] = form;
    } catch (err) {
      state.form[game.id] = { home: [], away: [], limit: 5, error: err.message || String(err) };
    } finally {
      state.formLoading.delete(game.id);
    }
    document.querySelectorAll(`[data-form-for="${CSS.escape(game.id)}"]`).forEach((el) => {
      el.outerHTML = lastFiveHtml(game);
    });
  }

  function loadVisibleForms() {
    for (const game of state.board?.games || []) loadForm(game);
  }

  function chicletHtml(game) {
    const expanded = state.expanded.has(game.id);
    const s = game.situation;
    const lp = s?.last_play;
    const pp = s?.power_play ? " power-play" : "";
    const showShots = game.state !== "pre";
    const core = showShots ? statRow("On goal", "shots", game) : "";
    const more = expanded
      ? [
          statRow("Hits", "hits", game),
          statRow("Blocked", "blocked", game),
          faceoffRow(game),
          statRow("Power play", "power_play", game, "pp_opps"),
          statRow("PIM", "pim", game),
          statRow("Giveaways", "giveaways", game),
          statRow("Takeaways", "takeaways", game),
        ].join("")
      : "";
    const goalies = expanded ? pairRow("GOAL", goalieCell(game.away.goalies), goalieCell(game.home.goalies)) : "";
    const leaders = expanded
      ? [
          pairRow("G", leaderCell(game.away.leaders?.goals), leaderCell(game.home.leaders?.goals)),
          pairRow("A", leaderCell(game.away.leaders?.assists), leaderCell(game.home.leaders?.assists)),
          pairRow("PTS", leaderCell(game.away.leaders?.points), leaderCell(game.home.leaders?.points)),
        ].join("")
      : "";
    const preBits = [];
    if (game.state === "pre") {
      const gA = game.away.goalie;
      const gH = game.home.goalie;
      if (gA || gH) preBits.push(`${gA || "TBD"} vs ${gH || "TBD"}`);
      if (game.venue) preBits.push(game.venue);
    }
    const headBits = [game.broadcast, game.state === "pre" ? "" : game.venue, ...(game.notes || [])].filter(Boolean);

    return `
      <article class="chiclet state-${esc(game.state)}${pp}${expanded ? " expanded" : ""}" data-id="${esc(game.id)}">
        <header class="chiclet-head">
          ${statusBadge(game)}
          <span class="net">${esc(headBits.join(" · "))}</span>
        </header>
        <div class="team-rows">
          ${teamRow(game, "away")}
          ${teamRow(game, "home")}
        </div>
        ${
          s
            ? `<div class="rink-wrap">
                 ${rinkSvg(game)}
                 <div class="down-line">
                   <span class="end" style="color:${teamColor(game.away)}">${esc(game.away.abbr)}</span>
                   <span class="dd ${s.strength ? "hot" : ""}">${esc(s.strength || "Even")}</span>
                   <span class="end" style="color:${teamColor(game.home)}">${esc(game.home.abbr)}</span>
                 </div>
               </div>`
            : ""
        }
        ${
          lp && lp.text
            ? `<div class="last-play ${lp.score_value ? "scoring" : ""}">
                 <span class="lp-tag">${esc(lp.type || "Last play")}${lp.score_value ? " · goal" : ""}</span>
                 <span class="lp-text">${esc(lp.text)}</span>
               </div>`
            : ""
        }
        ${goalTimeline(game)}
        ${linescore(game)}
        ${periodShots(game)}
        ${core || more ? `<div class="stat-compare">${core}${more}</div>` : ""}
        ${goalLog(game)}
        ${lastFiveHtml(game)}
        ${expanded && goalies ? `<div class="goalies">${goalies}</div>` : ""}
        ${expanded && leaders ? `<div class="goalies">${leaders}</div>` : ""}
        ${expanded ? starLines(game) : ""}
        ${preBits.length ? `<div class="pre-note">${esc(preBits.join(" · "))}</div>` : ""}
        ${
          game.state === "pre"
            ? ""
            : `<footer class="chiclet-foot">
                 <button type="button" class="mini toggle" data-act="toggle">${expanded ? "Less" : "More"}</button>
               </footer>`
        }
      </article>`;
  }

  function renderLive() {
    const grid = $("#liveGrid");
    const meta = $("#liveMeta");
    const badge = $("#liveBadge");
    const board = state.board;
    const dayIso = state.dates
      ? `${state.dates.slice(0, 4)}-${state.dates.slice(4, 6)}-${state.dates.slice(6, 8)}`
      : board?.day || "";
    $("#dayLabel").textContent = fmtDay(dayIso);
    $("#dayToday").classList.toggle("active", !state.dates);

    if (!board) {
      grid.innerHTML = `<p class="lede">${state.error ? `Live feed unavailable — ${esc(state.error)}` : "Loading scoreboard…"}</p>`;
      meta.textContent = state.error ? "Offline" : "Loading…";
      badge.hidden = true;
      return;
    }

    const counts = board.counts || {};
    if (counts.live) {
      badge.textContent = counts.live;
      badge.hidden = false;
    } else {
      badge.hidden = true;
    }
    const seasonBits = [];
    if (board.season_label) seasonBits.push(board.season_label);
    if (board.season_type_name) seasonBits.push(board.season_type_name);
    seasonBits.push(`${counts.live || 0} live · ${counts.final || 0} final · ${counts.upcoming || 0} upcoming`);
    seasonBits.push(`updated ${fmtUpdated(board.fetched_at)}`);
    if (state.error || board.stale) seasonBits.push("stale");
    meta.textContent = seasonBits.join(" · ");
    meta.classList.toggle("stale", Boolean(state.error || board.stale));
    $("#seasonLabel").textContent = board.season_label ? `${board.season_label} NHL` : "NHL";

    document.querySelectorAll("#liveFilter .chip").forEach((chip) => {
      chip.classList.toggle("active", chip.dataset.state === state.filter);
    });

    const games = (board.games || []).filter((g) => state.filter === "all" || g.state === state.filter);
    if (!games.length) {
      grid.innerHTML = `<p class="lede">No games in this view.</p>`;
      return;
    }
    grid.innerHTML = games.map(chicletHtml).join("");
    loadVisibleForms();
  }

  function onGridClick(ev) {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const card = btn.closest(".chiclet");
    const id = card?.dataset.id;
    const game = state.board?.games.find((g) => g.id === id);
    if (!game) return;
    if (state.expanded.has(id)) state.expanded.delete(id);
    else state.expanded.add(id);
    card.outerHTML = chicletHtml(game);
  }

  function stepDay(delta) {
    const base = state.dates
      ? `${state.dates.slice(0, 4)}-${state.dates.slice(4, 6)}-${state.dates.slice(6, 8)}`
      : state.board?.day;
    if (!base) return;
    const next = shiftIso(base, delta);
    const anchor = state.anchor || state.board?.day;
    state.dates = anchor && next === anchor ? "" : ymd(next);
    state.expanded.clear();
    loadLive(true);
  }

  // ------------------------------------------------------------------ WinProb

  function pct(x, digits = 0) {
    return x === null || x === undefined ? "–" : `${Number(x).toFixed(digits)}%`;
  }

  function wpTeamStyle(team) {
    return `--team:${teamColor(team)}`;
  }

  async function loadWinProb(force = false) {
    if (state.wp.loading) return;
    state.wp.loading = true;
    $("#wpRefresh").disabled = true;
    const params = new URLSearchParams();
    if (state.wp.season) params.set("season", state.wp.season);
    if (state.wp.day) params.set("day", state.wp.day);
    if (force) params.set("force", "1");
    try {
      const res = await fetch(`/api/winprob?${params}`);
      const board = await res.json();
      if (!res.ok) throw new Error(board.error || `HTTP ${res.status}`);
      state.wp.board = board;
      state.wp.season = board.season;
      state.wp.day = board.day;
      state.wp.error = null;
    } catch (err) {
      state.wp.error = err.message || String(err);
    } finally {
      state.wp.loading = false;
      $("#wpRefresh").disabled = false;
    }
    renderWinProb();
  }

  function wpStatus(g) {
    if (g.played) return `<span class="status final">Final${g.ot ? `/${esc(g.ot)}` : ""}</span>`;
    if (g.state === "in") return `<span class="status live"><i class="dot"></i>${esc(g.detail || "Live")}</span>`;
    return `<span class="status pre">${esc(fmtKickoff(g.date))}</span>`;
  }

  function wpGameCard(g) {
    const r = g.result;
    const cls = r ? (r.correct ? "hit" : "miss") : "pending";
    const row = (side) => {
      const t = g[side];
      const isFav = g.favorite === side;
      const won = r && r.winner === side;
      const lost = r && r.winner && r.winner !== side;
      const logo =
        t.logo && String(t.logo).startsWith("https://")
          ? `<img class="logo" src="${esc(t.logo)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'" />`
          : `<span class="logo logo-fallback">${esc(t.abbr.slice(0, 2))}</span>`;
      return `
        <div class="team-row ${side} ${isFav ? "fav" : ""} ${won ? "winner" : ""} ${lost ? "loser" : ""}" style="${wpTeamStyle(t)}">
          ${logo}
          <span class="tname">
            <b>${esc(t.abbr)}</b>
            <span class="full">${esc(t.name)}</span>
            <small class="rec">${esc(t.record)} · Elo ${t.elo}</small>
          </span>
          <span class="wp-pct ${isFav ? "fav" : ""}">${pct(t.prob * 100, 1)}</span>
          <span class="tscore">${g.played || g.state === "in" ? g[`${side}_score`] ?? "" : ""}</span>
        </div>`;
    };
    const ap = Math.round(g.away.prob * 100);
    const hp = 100 - ap;
    const awayColor = teamColor(g.away);
    const homeColor = teamColor(g.home);
    return `
      <article class="chiclet wp-card ${cls}" data-id="${esc(g.id)}">
        <header class="chiclet-head">
          ${wpStatus(g)}
          <span class="net">${g.game_type === "POST" ? "Playoffs" : g.neutral ? "Neutral site" : ""}</span>
          ${
            r
              ? `<span class="verdict ${cls}">${r.correct ? "✓ HIT" : "✗ MISS"}</span>`
              : `<span class="bucket-tag" title="Favourite's probability bucket">${esc(g.bucket)}%</span>`
          }
        </header>
        <div class="team-rows">${row("away")}${row("home")}</div>
        <div class="wp-bar" title="${esc(g.away.abbr)} ${ap}% · ${esc(g.home.abbr)} ${hp}%">
          <span class="seg a" style="width:${ap}%;background:${awayColor}">${ap >= 18 ? `<b>${esc(g.away.abbr)}</b> ${ap}%` : ap >= 10 ? `${ap}%` : ""}</span>
          <span class="seg h" style="width:${hp}%;background:${homeColor}">${hp >= 18 ? `<b>${esc(g.home.abbr)}</b> ${hp}%` : hp >= 10 ? `${hp}%` : ""}</span>
        </div>
        <div class="wp-lines">
          <span class="wp-margin"><small>Model</small> ${esc(g.favorite_abbr)} ${pct(g.favorite_prob * 100, 1)}</span>
          ${
            r
              ? `<span class="wp-actual"><small>Actual</small> ${esc(r.winner_abbr)} by ${r.margin}${r.ot ? ` in ${esc(r.ot)}` : ""} · Brier ${r.brier.toFixed(2)}</span>`
              : `<span class="wp-actual muted"><small>Bucket</small> ${esc(g.bucket)}% favourites</span>`
          }
        </div>
      </article>`;
  }

  function wpRecordHtml(board) {
    const t = board.record.total;
    const recent = board.record.recent;
    const daily = board.record.daily || [];
    return `
      <div class="card wp-season">
        <h3>Model picks · ${esc(board.season_label)}</h3>
        <div class="wp-big">
          <span class="wp-big-rec">${esc(t.record)}</span>
          <span class="wp-big-pct">${pct(t.pct, 1)}</span>
        </div>
        <p class="lede small">The favourite won ${t.correct} of ${t.decided} finished games. Home ice is worth ${board.model.hfa} Elo (${pct(board.model.hfa_pct, 1)} for even teams).</p>
        <div class="mini-kpis">
          <span><b>${t.decided}</b> decided</span>
          <span><b>${t.pending}</b> still to play</span>
          <span>Brier <b>${t.brier ?? "–"}</b></span>
          <span title="Last ${recent.days} days">Last ${recent.days}d <b>${esc(recent.record)}</b> (${pct(recent.pct)})</span>
        </div>
      </div>
      <div class="card wp-weekly">
        <h3>By game day</h3>
        <div class="wp-week-strip">
          ${
            daily.length
              ? daily
                  .map(
                    (d) => `
            <button type="button" class="wk ${d.day === state.wp.day ? "active" : ""} ${d.pct === null ? "pending" : d.pct >= 60 ? "good" : d.pct < 50 ? "bad" : ""}" data-day="${esc(d.day)}">
              <small>${esc(fmtDay(d.day))}</small>
              <b>${d.decided ? esc(d.record) : `${d.pending} tbd`}</b>
              <span>${d.decided ? pct(d.pct) : "–"}</span>
            </button>`
                  )
                  .join("")
              : `<p class="lede small">Game days show up here once games finish.</p>`
          }
        </div>
      </div>`;
  }

  function wpBucketsHtml(board) {
    const rows = board.buckets
      .map((b) => {
        const actual = b.pct;
        const expected = b.expected_pct;
        const decided = b.wins + b.losses;
        const diff = actual !== null && expected !== null ? actual - expected : null;
        return `
          <div class="bucket-row">
            <span class="bk-label">${esc(b.label)}</span>
            <div class="bk-bar" title="Actual ${pct(actual, 1)} vs expected ${pct(expected, 1)}">
              <i class="exp" style="left:${expected ?? 0}%"></i>
              <span class="act ${diff === null ? "" : diff >= 0 ? "good" : "bad"}" style="width:${actual ?? 0}%"></span>
              <em class="tick" style="left:50%"></em>
            </div>
            <span class="bk-rec"><b>${esc(b.record)}</b>${decided ? ` · ${pct(actual, 1)}` : ""}</span>
            <span class="bk-exp">${expected !== null ? `expected ${pct(expected, 1)}` : ""}${b.pending ? ` · ${b.pending} pending` : ""}</span>
            <span class="bk-split">home ${esc(b.home.record)} · away ${esc(b.away.record)}</span>
          </div>`;
      })
      .join("");
    return `
      <h3>Favourites by probability bucket <small>record · home / away split underneath</small></h3>
      <div class="bucket-legend"><i class="exp"></i> expected win % &nbsp; <span class="sw good"></span> actual ≥ expected &nbsp; <span class="sw bad"></span> actual below</div>
      ${rows}`;
  }

  function wpTeamBucketsHtml(board) {
    const keys = Object.keys(board.team_buckets[0]?.buckets || {});
    if (!board.team_buckets.length) return `<p class="lede small">Team records show up once games finish.</p>`;
    const cell = (b, muted = false) => {
      const n = b.wins + b.losses;
      const tone = !n ? "empty" : b.wins > b.losses ? "good" : b.wins < b.losses ? "bad" : "";
      return `<span class="tb-c ${tone} ${muted ? "muted" : ""}" title="${n ? `${b.record} · ${pct(b.pct)}` : "no games"}">${n ? esc(b.record) : "·"}</span>`;
    };
    return `
      <div class="tb-grid" style="--cols:${keys.length}">
        <span class="tb-h">Team</span><span class="tb-h">Favoured</span><span class="tb-h">Underdog</span>
        ${keys.map((k) => `<span class="tb-h">${esc(k)}%</span>`).join("")}
        ${board.team_buckets
          .map(
            (t) => `
          <span class="tb-team" style="${wpTeamStyle(t)}">${
            t.logo ? `<img class="logo" src="${esc(t.logo)}" alt="" loading="lazy" />` : ""
          }<b>${esc(t.abbr)}</b>${t.rank ? `<i class="pwrk">#${t.rank}</i>` : ""}</span>
          ${cell(t.favored)}
          ${cell(t.underdog, true)}
          ${keys.map((k) => cell(t.buckets[k])).join("")}`
          )
          .join("")}
      </div>`;
  }

  function wpRatingsHtml(board) {
    return `
      <p class="lede small">Elo after every game through ${esc(fmtDay(board.through_day))}. 1500 is average; records are W-L-OTL this regular season.</p>
      <div class="rating-list">
        <div class="rating-row head"><span class="rk"></span><span></span><span class="rn">Team</span><span class="rr">Rec</span><span class="re">Elo</span></div>
        ${board.ratings
          .map(
            (r) => `
          <div class="rating-row" style="${wpTeamStyle(r)}">
            <span class="rk">#${r.rank}</span>
            ${r.logo ? `<img class="logo" src="${esc(r.logo)}" alt="" loading="lazy" />` : "<span></span>"}
            <span class="rn"><b>${esc(r.abbr)}</b> ${esc(r.name)}</span>
            <span class="rr">${esc(r.record)}</span>
            <span class="re">${r.elo}</span>
          </div>`
          )
          .join("")}
      </div>`;
  }

  function renderWinProb() {
    const board = state.wp.board;
    const meta = $("#wpMeta");
    if (!board) {
      $("#wpGames").innerHTML = `<p class="lede">${state.wp.error ? `Model unavailable — ${esc(state.wp.error)}` : "Loading model…"}</p>`;
      meta.textContent = state.wp.error ? "Offline" : "Loading…";
      return;
    }
    const t = board.record.total;
    meta.textContent = `${board.season_label} · ${board.model.name} · ${t.record} (${pct(t.pct, 1)}) · ${board.games.length} games on ${fmtDay(board.day)}${state.wp.error ? " · stale" : ""}`;
    meta.classList.toggle("stale", Boolean(state.wp.error || board.error));
    $("#wpSeasons").innerHTML = (board.seasons || [board.season])
      .map((y) => `<button type="button" class="chip ${y === board.season ? "active" : ""}" data-season="${y}">${esc(`${y - 1}-${String(y).slice(-2)}`)}</button>`)
      .join("");
    $("#wpRecord").innerHTML = wpRecordHtml(board);
    $("#wpBuckets").innerHTML = wpBucketsHtml(board);
    const dr = board.day_record;
    $("#wpDayTitle").textContent = `${fmtDay(board.day)}${dr.decided ? ` · ${dr.record} (${pct(dr.pct)})` : ""}${dr.pending ? ` · ${dr.pending} to play` : ""}`;
    $("#wpDayLabel").textContent = fmtDay(board.day);
    $("#wpPrev").disabled = !board.prev_day;
    $("#wpNext").disabled = !board.next_day;
    $("#wpToday").classList.toggle("active", board.day === board.current_day && board.season === board.current_season);
    $("#wpGames").innerHTML = board.games.map(wpGameCard).join("") || `<p class="lede">No games on this day.</p>`;
    $("#wpTeamBuckets").innerHTML = wpTeamBucketsHtml(board);
    $("#wpRatingList").innerHTML = wpRatingsHtml(board);
  }

  function wpGoto({ season, day }) {
    if (season !== undefined) {
      state.wp.season = season;
      state.wp.day = null;
    }
    if (day !== undefined) state.wp.day = day;
    loadWinProb();
  }

  // ------------------------------------------------------------------ Tabs

  function switchTab(name) {
    state.tab = name;
    document.querySelectorAll(".tab").forEach((tab) => {
      const active = tab.dataset.tab === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".panel").forEach((panel) => {
      const active = panel.id === `panel-${name}`;
      panel.classList.toggle("active", active);
      panel.hidden = !active;
    });
    if (name === "winprob" && !state.wp.board) loadWinProb();
  }

  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => switchTab(tab.dataset.tab)));
  $("#wpRefresh").addEventListener("click", () => loadWinProb(true));
  $("#wpPrev").addEventListener("click", () => state.wp.board?.prev_day && wpGoto({ day: state.wp.board.prev_day }));
  $("#wpNext").addEventListener("click", () => state.wp.board?.next_day && wpGoto({ day: state.wp.board.next_day }));
  $("#wpToday").addEventListener("click", () => wpGoto({ season: state.wp.board?.current_season ?? null, day: null }));
  $("#wpSeasons").addEventListener("click", (ev) => {
    const chip = ev.target.closest("button[data-season]");
    if (chip) wpGoto({ season: Number(chip.dataset.season) });
  });
  $("#wpRecord").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-day]");
    if (btn) wpGoto({ day: btn.dataset.day });
  });

  $("#liveRefresh").addEventListener("click", () => loadLive(true));
  $("#dayPrev").addEventListener("click", () => stepDay(-1));
  $("#dayNext").addEventListener("click", () => stepDay(1));
  $("#dayToday").addEventListener("click", () => {
    state.dates = "";
    state.expanded.clear();
    loadLive(true);
  });
  $("#liveFilter").addEventListener("click", (ev) => {
    const chip = ev.target.closest("button[data-state]");
    if (!chip) return;
    state.filter = chip.dataset.state;
    renderLive();
  });
  $("#liveGrid").addEventListener("click", onGridClick);

  loadLive();
  loadPeriods();
  scheduleLive();
})();
