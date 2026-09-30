(() => {
  const state = {
    board: null,
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
    state.timer = setInterval(() => {
      if (document.visibilityState === "visible") loadLive();
    }, LIVE_REFRESH_MS);
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
    return d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
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

  function linescore(game) {
    const a = game.away.linescores || [];
    const h = game.home.linescores || [];
    const n = Math.max(a.length, h.length);
    if (!n) return "";
    const heads = Array.from({ length: n }, (_, i) => (i < 3 ? String(i + 1) : i === 3 ? "OT" : "SO"));
    const cells = (arr) => heads.map((_, i) => `<span>${arr[i] ?? "–"}</span>`).join("");
    return `
      <div class="linescore" style="grid-template-columns: 2.6rem repeat(${n}, 1.5rem) 1.6rem">
        <span class="lsn"></span>${heads.map((x) => `<span class="lsh">${x}</span>`).join("")}<span class="lsh">T</span>
        <span class="lsn">${esc(game.away.abbr)}</span>${cells(a)}<span class="lst">${game.away.score}</span>
        <span class="lsn">${esc(game.home.abbr)}</span>${cells(h)}<span class="lst">${game.home.score}</span>
      </div>`;
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

  function chicletHtml(game) {
    const expanded = state.expanded.has(game.id);
    const s = game.situation;
    const lp = s?.last_play;
    const pp = s?.power_play ? " power-play" : "";
    const showShots = game.state !== "pre";
    const core = showShots ? statRow("Shots", "shots", game) : "";
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
      if (game.status_detail) preBits.push(game.status_detail);
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
        ${core || more ? `<div class="stat-compare">${core}${more}</div>` : ""}
        ${goalLog(game)}
        ${expanded && goalies ? `<div class="goalies">${goalies}</div>` : ""}
        ${expanded && leaders ? `<div class="goalies">${leaders}</div>` : ""}
        ${expanded ? starLines(game) : ""}
        ${expanded ? linescore(game) : ""}
        ${preBits.length ? `<div class="pre-note">${esc(preBits.join(" · "))}</div>` : ""}
        <footer class="chiclet-foot">
          <button type="button" class="mini toggle" data-act="toggle">${expanded ? "Less" : "More"}</button>
        </footer>
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
  scheduleLive();
})();
