/* Pam Listen capture: mic to /ws/listen, partials and finals to the page. */
(function () {
  "use strict";

  const FRAME_BYTES = 3840;
  const root = document.getElementById("listen-root");
  if (!root) return;

  const token = root.dataset.token || "";
  const project = root.dataset.project || "default";
  const startBtn = document.getElementById("listen-start");
  const stopBtn = document.getElementById("listen-stop");
  const titleInput = document.getElementById("listen-title");
  const statusEl = document.getElementById("listen-status");
  const partialEl = document.getElementById("listen-partial");
  const transcriptEl = document.getElementById("listen-transcript");
  const sessionEl = document.getElementById("listen-session");
  const notesBox = document.getElementById("listen-notes-link");
  const errorEl = document.getElementById("listen-error");
  const proposalsCard = document.getElementById("listen-proposals-card");
  const proposalsBox = document.getElementById("listen-proposals");
  const pipelineStatus = document.getElementById("listen-pipeline-status");
  const proposalsResult = document.getElementById("listen-proposals-result");
  const approveAllBtn = document.getElementById("listen-approve-all");
  const approveSelectedBtn = document.getElementById("listen-approve-selected");
  const discardBtn = document.getElementById("listen-discard");

  let ws = null;
  let ctx = null;
  let source = null;
  let node = null;
  let stream = null;
  let pending = new Uint8Array(0);
  let sessionId = root.dataset.session || "";
  let stopped = false;
  let pipelineStarted = false;
  let proposals = [];

  function setStatus(text, cls) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = "pill " + (cls || "");
  }

  function setError(text) {
    if (!errorEl) return;
    errorEl.textContent = text || "";
  }

  function setTitleDisabled(disabled) {
    if (titleInput) titleInput.disabled = !!disabled;
  }

  function addFinal(text, speaker, turnId) {
    if (!transcriptEl || !text) return;
    const empty = transcriptEl.querySelector("[data-empty]");
    if (empty) empty.remove();
    const li = document.createElement("li");
    const who = speaker ? speaker + ": " : "";
    li.textContent = who + text;
    if (turnId) li.dataset.turn = turnId;
    transcriptEl.appendChild(li);
  }

  function showNotesLink() {
    if (!notesBox || !sessionId) return;
    notesBox.innerHTML = "";
    const link = document.createElement("a");
    link.className = "btn";
    link.href = "/meetings/" + encodeURIComponent(sessionId) + "?project=" + encodeURIComponent(project);
    link.textContent = "Review transcript and notes";
    notesBox.appendChild(link);
  }

  function queueSend(bytes) {
    const next = new Uint8Array(pending.length + bytes.length);
    next.set(pending, 0);
    next.set(bytes, pending.length);
    pending = next;
    while (pending.length >= FRAME_BYTES && ws && ws.readyState === 1) {
      const frame = pending.slice(0, FRAME_BYTES);
      pending = pending.slice(FRAME_BYTES);
      ws.send(frame);
    }
  }

  function flushSend() {
    if (pending.length > 0 && ws && ws.readyState === 1) {
      const pad = FRAME_BYTES - (pending.length % FRAME_BYTES || FRAME_BYTES);
      if (pad > 0 && pad < FRAME_BYTES) {
        const next = new Uint8Array(pending.length + pad);
        next.set(pending, 0);
        pending = next;
      }
      ws.send(pending);
    }
    pending = new Uint8Array(0);
  }

  function wsUrl() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = new URL(proto + "//" + window.location.host + "/ws/listen");
    url.searchParams.set("token", token);
    url.searchParams.set("project", project);
    if (titleInput && titleInput.value.trim()) url.searchParams.set("title", titleInput.value.trim());
    if (sessionId) url.searchParams.set("session_id", sessionId);
    return url.toString();
  }

  async function start() {
    setError("");
    if (ws) return;
    stopped = false;
    pipelineStarted = false;
    proposals = [];
    if (proposalsCard) proposalsCard.hidden = true;
    if (proposalsResult) proposalsResult.textContent = "";
    setStatus("Connecting", "");
    if (startBtn) startBtn.disabled = true;
    setTitleDisabled(true);
    const socket = new WebSocket(wsUrl());
    socket.binaryType = "arraybuffer";
    ws = socket;

    socket.onopen = async function () {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch (err) {
        setError("Microphone was blocked. Allow mic access and try again.");
        setStatus("Mic blocked", "pill-fail");
        try { socket.close(); } catch (e) { /* already closing */ }
        ws = null;
        if (startBtn) startBtn.disabled = false;
        setTitleDisabled(false);
        return;
      }
      try {
        ctx = new (window.AudioContext || window.webkitAudioContext)();
        await ctx.audioWorklet.addModule("/static/listen-worklet.js");
        source = ctx.createMediaStreamSource(stream);
        node = new AudioWorkletNode(ctx, "pam-listen-capture");
        node.port.onmessage = function (evt) {
          if (stopped) return;
          queueSend(new Uint8Array(evt.data));
        };
        source.connect(node);
      } catch (err) {
        setError("Audio start failed. Try a recent Chrome or Edge.");
        setStatus("Audio failed", "pill-fail");
        stop();
        return;
      }
      setStatus("Transcribing", "pill-ok");
      if (stopBtn) stopBtn.disabled = false;
    };

    socket.onmessage = function (evt) {
      let msg = null;
      try { msg = JSON.parse(String(evt.data)); } catch (err) { return; }
      if (!msg || typeof msg !== "object") return;
      if (msg.session_id && !sessionId) {
        sessionId = String(msg.session_id);
        if (sessionEl) sessionEl.textContent = sessionId;
      }
      if (msg.type === "partial" && partialEl) {
        partialEl.textContent = String(msg.text || "");
      } else if (msg.type === "final") {
        if (partialEl) partialEl.textContent = "";
        addFinal(String(msg.text || ""), String(msg.speaker || ""), String(msg.turn_id || ""));
      } else if (msg.type === "status" && msg.state) {
        if (msg.state === "reconnecting") setStatus("Reconnecting", "");
        if (msg.state === "max_length") {
          setStatus("Two hour limit reached", "pill-fail");
          stop();
        }
      } else if (msg.type === "error" && msg.message) {
        setError(String(msg.message));
      }
    };

    socket.onclose = function () {
      if (!stopped) setStatus("Stopped", "");
      ws = null;
      if (startBtn) startBtn.disabled = false;
      if (stopBtn) stopBtn.disabled = true;
      setTitleDisabled(false);
      showNotesLink();
      runPipeline();
    };

    socket.onerror = function () {
      setError("Connection lost. Audio was kept for 30 seconds.");
    };
  }

  function stop() {
    stopped = true;
    try {
      if (ws && ws.readyState === 1) {
        flushSend();
        ws.send(JSON.stringify({ type: "end" }));
      }
    } catch (err) { /* socket already closed */ }
    try { if (node) node.disconnect(); } catch (err) { /* noop */ }
    try { if (source) source.disconnect(); } catch (err) { /* noop */ }
    try { if (ctx) void ctx.close(); } catch (err) { /* noop */ }
    try {
      if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    } catch (err) { /* noop */ }
    node = null; source = null; ctx = null; stream = null;
    setStatus("Stopped", "");
    if (startBtn) startBtn.disabled = false;
    if (stopBtn) stopBtn.disabled = true;
    setTitleDisabled(false);
    showNotesLink();
    runPipeline();
    try { if (ws) ws.close(); } catch (err) { /* noop */ }
  }

  if (startBtn) startBtn.addEventListener("click", function () { void start(); });
  if (stopBtn) stopBtn.addEventListener("click", stop);

  function api(path) {
    return "/api/meetings/" + encodeURIComponent(sessionId) + path +
      (path.indexOf("?") >= 0 ? "&" : "?") + "project=" + encodeURIComponent(project);
  }

  function setPipelineStatus(text) {
    if (pipelineStatus) pipelineStatus.textContent = text || "";
  }

  function setProposalsResult(text) {
    if (proposalsResult) proposalsResult.textContent = text || "";
  }

  function labeledInput(labelText, input) {
    const label = document.createElement("label");
    label.appendChild(document.createTextNode(labelText + " "));
    label.appendChild(input);
    return label;
  }

  function renderProposals(list) {
    proposals = Array.isArray(list) ? list : [];
    if (!proposalsBox) return;
    proposalsBox.innerHTML = "";
    if (!proposals.length) {
      const none = document.createElement("p");
      none.className = "muted";
      none.textContent = "No tasks proposed yet.";
      proposalsBox.appendChild(none);
      return;
    }
    proposals.forEach(function (draft, index) {
      const card = document.createElement("article");
      const heading = document.createElement("h3");
      const pick = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = true;
      box.dataset.index = String(index);
      box.setAttribute("aria-label", "Select proposal " + (index + 1));
      pick.appendChild(box);
      pick.appendChild(document.createTextNode(" Proposal " + (index + 1)));
      heading.appendChild(pick);
      card.appendChild(heading);
      const title = document.createElement("input");
      title.type = "text";
      title.value = draft.title || "";
      title.maxLength = 200;
      title.dataset.field = "title";
      title.dataset.index = String(index);
      card.appendChild(labeledInput("Title", title));
      const owner = document.createElement("input");
      owner.type = "text";
      owner.value = draft.owner || "";
      owner.placeholder = "Unassigned";
      owner.maxLength = 120;
      owner.dataset.field = "owner";
      owner.dataset.index = String(index);
      card.appendChild(labeledInput("Owner", owner));
      const estimate = document.createElement("input");
      estimate.type = "number";
      estimate.min = "0";
      estimate.max = "80";
      estimate.step = "0.5";
      estimate.value = String(draft.estimate == null ? 2 : draft.estimate);
      estimate.dataset.field = "estimate";
      estimate.dataset.index = String(index);
      card.appendChild(labeledInput("Estimate (hours)", estimate));
      const evidence = document.createElement("p");
      evidence.className = "muted";
      evidence.textContent = "Evidence: " + (draft.evidence_quote || "");
      card.appendChild(evidence);
      proposalsBox.appendChild(card);
    });
  }

  function readEdits() {
    const edits = proposals.map(function (draft) {
      return {
        title: draft.title || "",
        owner: draft.owner || "",
        estimate: draft.estimate == null ? 2 : draft.estimate,
      };
    });
    if (!proposalsBox) return { edits: edits, selected: [] };
    const selected = [];
    proposalsBox.querySelectorAll("input[data-field]").forEach(function (input) {
      const index = Number(input.dataset.index || "-1");
      if (index < 0 || index >= edits.length) return;
      if (input.dataset.field === "estimate") {
        const num = Number(input.value);
        edits[index].estimate = Number.isFinite(num) ? num : edits[index].estimate;
      } else {
        edits[index][input.dataset.field] = input.value;
      }
    });
    proposalsBox.querySelectorAll("input[type=checkbox][data-index]").forEach(function (box) {
      if (box.checked) selected.push(Number(box.dataset.index));
    });
    return { edits: edits, selected: selected };
  }

  async function runPipeline() {
    if (!sessionId || pipelineStarted || !proposalsCard) return;
    pipelineStarted = true;
    proposalsCard.hidden = false;
    setPipelineStatus("Summarizing the transcript and proposing tasks...");
    setProposalsResult("");
    try {
      const res = await fetch(api("/finish"), { method: "POST" });
      const body = await res.json();
      if (!res.ok || !body.ok) throw new Error((body && body.message) || "pipeline failed");
      renderProposals(body.proposals || []);
      const count = (body.proposals || []).length;
      setPipelineStatus(count ? "Edit below, then approve. Nothing is posted until you approve." : "No tasks proposed from this meeting.");
    } catch (err) {
      setPipelineStatus("Proposals failed. Open the meeting page and try Generate notes.");
    }
  }

  async function approve(all) {
    if (!proposals.length) return;
    const state = readEdits();
    const selected = all ? proposals.map(function (_, i) { return i; }) : state.selected;
    if (!selected.length) {
      setProposalsResult("Select at least one proposal.");
      return;
    }
    setProposalsResult("Approving...");
    try {
      const res = await fetch(api("/proposals/approve"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selected: selected, proposals: state.edits }),
      });
      const body = await res.json();
      if (!res.ok || !body.ok) throw new Error((body && body.message) || "approve failed");
      setProposalsResult("Created " + body.created + " of " + body.total + " proposed tasks.");
    } catch (err) {
      setProposalsResult("Approve failed. Try again.");
    }
  }

  async function discard() {
    if (!sessionId) return;
    setProposalsResult("Discarding...");
    try {
      const res = await fetch(api("/proposals/discard"), { method: "POST" });
      const body = await res.json();
      if (!res.ok || !body.ok) throw new Error("discard failed");
      renderProposals([]);
      setPipelineStatus("Proposals discarded.");
      setProposalsResult("Discarded. Nothing was posted.");
    } catch (err) {
      setProposalsResult("Discard failed. Try again.");
    }
  }

  if (approveAllBtn) approveAllBtn.addEventListener("click", function () { void approve(true); });
  if (approveSelectedBtn) approveSelectedBtn.addEventListener("click", function () { void approve(false); });
  if (discardBtn) discardBtn.addEventListener("click", function () { void discard(); });

  /* Test hooks for Playwright fake media runs. */
  window.pamListen = {
    get sessionId() { return sessionId; },
    get state() { return statusEl ? statusEl.textContent : ""; },
    start: start,
    stop: stop,
    proposals: function () { return proposals.slice(); },
    finals: function () {
      if (!transcriptEl) return [];
      return Array.from(transcriptEl.querySelectorAll("li")).map(function (li) { return li.textContent || ""; });
    },
    sendTestAudio: async function (url) {
      const res = await fetch(url);
      const buf = new Uint8Array(await res.arrayBuffer());
      const pcm = extractPcm16(buf);
      queueSend(pcm);
      flushSend();
      return pcm.length;
    },
  };

  function extractPcm16(bytes) {
    /* Minimal WAV parse: skip 44 byte header when present, else raw. */
    if (bytes.length > 44 && bytes[0] === 0x52 && bytes[1] === 0x49 && bytes[2] === 0x46 && bytes[3] === 0x46) {
      return bytes.slice(44);
    }
    return bytes;
  }
})();
