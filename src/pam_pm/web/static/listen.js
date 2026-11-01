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

  let ws = null;
  let ctx = null;
  let source = null;
  let node = null;
  let stream = null;
  let pending = new Uint8Array(0);
  let sessionId = root.dataset.session || "";
  let stopped = false;

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
    try { if (ws) ws.close(); } catch (err) { /* noop */ }
  }

  if (startBtn) startBtn.addEventListener("click", function () { void start(); });
  if (stopBtn) stopBtn.addEventListener("click", stop);

  /* Test hooks for Playwright fake media runs. */
  window.pamListen = {
    get sessionId() { return sessionId; },
    get state() { return statusEl ? statusEl.textContent : ""; },
    start: start,
    stop: stop,
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
