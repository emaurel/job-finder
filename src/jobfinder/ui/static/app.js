"use strict";

let state = { funnel: null, queue: [], config: {}, sources: [], running: null };
let filter = "actionable";
const shots = {};   // job id -> screenshot filename from the last ATS run

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------- websocket ----------

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => { $("#dot").classList.add("live"); $("#conn").textContent = "live"; };
  ws.onclose = () => {
    $("#dot").classList.remove("live");
    $("#conn").textContent = "reconnecting";
    setTimeout(connect, 1000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (msg) => {
    let e;
    try { e = JSON.parse(msg.data); } catch { return; }
    handle(e);
  };
}

function handle(e) {
  switch (e.type) {
    case "snapshot":
      state = e;
      (e.log || []).forEach(addLog);
      renderAll();
      break;
    case "state":
      state.funnel = e.funnel;
      state.queue = e.queue;
      renderFunnel(); renderQueue();
      break;
    case "log":
      addLog(e);
      break;
    case "stage":
      state.running = e.state === "start" ? e.stage : null;
      addLog({ level: "stage", at: e.at,
               message: e.state === "start" ? `--- ${e.stage} started ---`
                                            : `--- ${e.stage} done ---` });
      if (e.state === "done") setBar(0, 0);
      renderFunnel(); renderControls();
      break;
    case "progress":
      setBar(e.done, e.total);
      break;
    case "job":
      break;
  }
}

// ---------- log ----------

function addLog(e) {
  const box = $("#log");
  const stuck = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  const t = (e.at || "").slice(11, 19);
  const div = document.createElement("div");
  div.className = e.level || "info";
  div.innerHTML = `<span class="t">${t}</span>${esc(e.message)}`;
  box.appendChild(div);
  while (box.children.length > 600) box.removeChild(box.firstChild);
  if (stuck) box.scrollTop = box.scrollHeight;
}

function setBar(done, total) {
  $("#bar > div").style.width = total > 0 ? `${(done / total) * 100}%` : "0";
}

// ---------- left rail ----------

function renderFunnel() {
  const f = state.funnel;
  if (!f) return;
  const s = f.stats;
  const rows = [
    ["seen", s.jobs, "fetch"],
    ["filtered out", s.filtered, "fetch"],
    ["scored", s.scored, "score"],
    ["drafts", s.drafted, "draft"],
    ["approved", s.approved, null],
    ["sent", s.sent, "send"],
  ];
  $("#funnel").innerHTML = rows.map(([name, n, stage]) =>
    `<div class="stage ${state.running === stage ? "active" : ""}">
       <span class="sname">${name}</span><span class="snum">${n}</span></div>`).join("");

  $("#sources").innerHTML = state.sources.map((src) => {
    const n = (f.by_source || {})[src.name];
    return `<div class="row"><span class="${src.enabled ? "on" : "off"}">${src.name}</span>
            <b>${n == null ? (src.enabled ? "-" : "off") : n}</b></div>`;
  }).join("");

  const reasons = Object.entries(f.reasons || {});
  $("#reasons").innerHTML = reasons.length
    ? reasons.map(([r, n]) => `<div class="row"><span>${esc(r)}</span><b>${n}</b></div>`).join("")
    : `<div class="row"><span class="off">nothing yet</span></div>`;

  const c = state.config || {};
  $("#gates").innerHTML = `
    <div class="row"><span>approval</span><b class="${c.require_approval ? "on" : "bad"}">${c.require_approval ? "required" : "OFF"}</b></div>
    <div class="row"><span>ATS submit</span><b class="${c.ats_submit ? "bad" : "on"}">${c.ats_submit ? "ON" : "off"}</b></div>
    <div class="row"><span>sent today</span><b>${s.sent_today} / ${c.max_sends_per_day}</b></div>
    <div class="row"><span>LLM key</span><b class="${c.has_llm ? "on" : "bad"}">${c.has_llm ? "yes" : "missing"}</b></div>
    <div class="row"><span>SMTP</span><b class="${c.smtp ? "on" : "off"}">${c.smtp ? "ready" : "not set"}</b></div>
    <div class="row"><span>draft at</span><b>score &ge; ${c.draft_threshold}</b></div>`;
}

function renderControls() {
  document.querySelectorAll("#controls button").forEach((b) => {
    b.disabled = !!state.running;
  });
  const busy = state.running;
  document.querySelectorAll(".card button").forEach((b) => { b.disabled = !!busy; });
}

// ---------- queue ----------

function scoreClass(n) { return n >= 60 ? "s-hi" : n >= 40 ? "s-mid" : "s-lo"; }

function visible(job) {
  if (filter === "all") return true;
  if (filter === "sent") return job.status === "sent";
  return ["none", "draft", "approved"].includes(job.status) && job.status !== "rejected";
}

function card(job) {
  const flags = (job.red_flags || []).slice(0, 3)
    .map((f) => `<span class="pill flag">${esc(f)}</span>`).join("");
  const good = (job.strengths || []).slice(0, 3)
    .map((f) => `<span class="pill good">${esc(f)}</span>`).join("");
  // Each channel delivers differently, and the card must not imply an email
  // where none is sent. "manual" means Edgar submits it himself.
  const CHANNELS = {
    email: {
      addr: "To",
      note: "",
      showSubject: true,
    },
    ats: {
      addr: "Form",
      note: "A browser fills this form. Nothing is submitted unless ATS submit is on.",
      showSubject: false,
    },
    manual: {
      addr: "Apply at",
      note: "No automatic sending. Open the posting and paste the letter yourself.",
      showSubject: false,
    },
  };
  const ch = CHANNELS[job.channel] || CHANNELS.manual;

  const head = job.cover_letter || job.subject
    ? `<div class="mhead">
         <div><span>${ch.addr}</span>${
           job.channel === "email"
             ? esc(job.recipient)
             : `<a href="${esc(job.recipient || job.url)}" target="_blank" rel="noopener">${esc(job.recipient || job.url)}</a>`
         }</div>
         ${ch.showSubject ? `<div><span>Subject</span><b>${esc(job.subject)}</b></div>` : ""}
         ${ch.showSubject && job.attachment
             ? `<div><span>Attached</span>${esc(job.attachment)}</div>` : ""}
         ${ch.note ? `<div><span>Delivery</span><i>${esc(ch.note)}</i></div>` : ""}
       </div>` : "";

  const letter = job.cover_letter
    ? `<div class="mail">${head}<div class="letter">${esc(job.cover_letter)}${esc(job.signature || "")}</div></div>`
    : (head ? `<div class="mail">${head}</div>` : "");

  const notes = job.notes ? `<div class="notes">${esc(job.notes)}</div>` : "";
  const shotName = shots[job.id] || job.screenshot;
  const shot = shotName
    ? `<div class="shot">
         <a href="/api/screenshot/${encodeURIComponent(shotName)}" target="_blank">open full screenshot &rarr;</a>
         <div class="frame"><img src="/api/screenshot/${encodeURIComponent(shotName)}" alt="filled form"></div>
       </div>` : "";

  let acts = "";
  if (job.status === "none") {
    acts = `<button data-act="draft" data-id="${job.id}">Write letter</button>
            <button data-act="approve" data-id="${job.id}">Approve as is</button>
            <button class="no" data-act="reject" data-id="${job.id}">Not interested</button>`;
  } else if (job.status === "draft") {
    acts = `<button class="go" data-act="approve" data-id="${job.id}">Approve</button>
            <button class="no" data-act="reject" data-id="${job.id}">Reject</button>`;
  } else if (job.status === "approved") {
    const label = job.channel === "email" ? "Send email now"
                : job.channel === "ats"   ? "Submit via browser"
                : "Mark as applied";
    const act = job.channel === "manual" ? "applied" : "send";
    acts = `<button class="go" data-act="${act}" data-id="${job.id}">${label}</button>
            <button class="no" data-act="reject" data-id="${job.id}">Cancel</button>`;
  } else if (job.status === "sent") {
    acts = `<span class="chan">sent</span>`;
  }
  if (job.channel === "ats" && job.status !== "sent") {
    acts += `<button data-act="ats" data-id="${job.id}">Fill form &amp; screenshot</button>`;
  }

  return `<div class="card" data-status="${job.status}">
    <div class="chead">
      <div>
        <div class="ctitle">${esc(job.title)}</div>
        <div class="cmeta">${esc(job.company)} &middot; ${esc(job.location)} &middot; ${esc(job.source)}
          ${job.salary ? " &middot; " + esc(job.salary) : ""} &middot; ${esc(job.language)}</div>
      </div>
      <div class="score ${scoreClass(job.score)}">${job.score}</div>
    </div>
    <div>${good}${flags}</div>
    <div class="why">${esc(job.reasoning)}</div>
    ${letter}${notes}${shot}
    <div class="acts">${acts}<span class="spacer"></span>
      <span class="chan">via ${esc(job.channel)}</span>
      <a href="${esc(job.url)}" target="_blank" rel="noopener">posting &rarr;</a></div>
  </div>`;
}

function renderQueue() {
  const list = (state.queue || []).filter(visible);
  $("#qcount").textContent = `${list.length} shown, ${(state.queue || []).length} scored`;
  $("#cards").innerHTML = list.length
    ? list.map(card).join("")
    : `<div class="empty">Nothing here yet. Run <b>Fetch sources</b>, then <b>Score</b>.</div>`;
  renderControls();
}

function renderAll() { renderFunnel(); renderQueue(); renderControls(); }

// ---------- actions ----------

async function post(url) {
  const r = await fetch(url, { method: "POST" });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) addLog({ level: "error", at: new Date().toISOString(),
                      message: body.detail || `${url} failed (${r.status})` });
  return body;
}

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button");
  if (!btn || btn.disabled || btn.id === "logtoggle") return;

  if (btn.dataset.stage) {
    await post(`/api/run/${btn.dataset.stage}`);
    return;
  }
  if (btn.dataset.f) {
    filter = btn.dataset.f;
    document.querySelectorAll("#filters button").forEach((b) =>
      b.classList.toggle("sel", b.dataset.f === filter));
    renderQueue();
    return;
  }
  const { act, id } = btn.dataset;
  if (!act) return;

  if (act === "ats") {
    const res = await post(`/api/jobs/${id}/ats`);
    if (res && res.screenshot) { shots[id] = res.screenshot; }
    renderQueue();
    return;
  }
  if (act === "send" && !confirm("Send this application now?")) return;
  if (act === "applied" && !confirm("Mark this as applied? Do it on the site first.")) return;
  await post(`/api/jobs/${id}/${act}`);
});

// ---------- log pane collapse ----------

function applyLogCollapsed(collapsed) {
  document.body.classList.toggle("log-collapsed", collapsed);
  const btn = $("#logtoggle");
  btn.innerHTML = collapsed ? "&laquo;" : "&raquo;";
  btn.title = collapsed ? "Show the log" : "Collapse the log";
}

(function initLogToggle() {
  let collapsed = false;
  try { collapsed = localStorage.getItem("jf.logCollapsed") === "1"; } catch { /* private mode */ }
  applyLogCollapsed(collapsed);
  $("#logtoggle").addEventListener("click", () => {
    const now = !document.body.classList.contains("log-collapsed");
    applyLogCollapsed(now);
    try { localStorage.setItem("jf.logCollapsed", now ? "1" : "0"); } catch { /* ignore */ }
  });
})();

connect();
