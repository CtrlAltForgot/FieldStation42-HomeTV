(() => {
  const video = document.querySelector("#video");
  const channelSelect = document.querySelector("#channels");
  const message = document.querySelector("#message");
  const progress = document.querySelector("#progress span");
  let channels = [], sessionId = null, hls = null, nowInfo = null;
  let recoveryTimer = null, hlsRecoveryTimer = null;
  let controlsTimer = null, heartbeat = null;
  let boundaryTimer = null, boundaryFadeTimer = null;
  let isTuning = false, tuneAbort = null;
  let recoveryAttempts = 0;
  let hlsNetworkRecoveries = 0, hlsMediaRecoveries = 0;
  const TRANSITION_MS = 450;

  const showControls = () => {
    document.body.classList.add("active");
    clearTimeout(controlsTimer);
    controlsTimer = setTimeout(() => document.body.classList.remove("active"), 4000);
  };

  async function api(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `Request failed (${response.status})`);
    }
    return response.status === 204 ? null : response.json();
  }

  function reportClientEvent(event, detail = "") {
    fetch("/api/watch/client-events", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session_id: sessionId, event, detail}),
      keepalive: true
    }).catch(() => {});
  }

  async function requestPlayback() {
    try {
      await video.play();
      return true;
    } catch (error) {
      reportClientEvent("play-rejected", `${error.name}: ${error.message}`);
      if (error.name === "NotAllowedError") {
        message.textContent = "Press OK or click once to start playback";
        return false;
      }
      console.error("Video playback could not start", error);
      recover(true);
      return false;
    }
  }

  async function stopSession() {
    clearInterval(heartbeat);
    clearTimeout(hlsRecoveryTimer);
    clearTimeout(boundaryTimer);
    clearTimeout(boundaryFadeTimer);
    if (hls) { hls.destroy(); hls = null; }
    video.pause();
    video.removeAttribute("src");
    if (sessionId) {
      const old = sessionId; sessionId = null;
      await fetch(
        `/api/watch/sessions/${old}`,
        {method: "DELETE", keepalive: true}
      ).catch(() => {});
    }
  }

  async function attach(url, signal) {
    video.pause();
    // Chromium may report "maybe" for native HLS while failing to decode an
    // MPEG-TS playlist. Prefer HLS.js wherever Media Source is available and
    // reserve native HLS for Safari and other browsers without MSE support.
    if (window.Hls && Hls.isSupported()) {
      hls = new Hls({
        liveSyncDurationCount: 1,
        liveMaxLatencyDurationCount: 10,
        maxLiveSyncPlaybackRate: 1.15,
        maxBufferLength: 60,
        maxMaxBufferLength: 90,
        backBufferLength: 30
      });
      hls.loadSource(url);
      hls.attachMedia(video);
      await new Promise((resolve, reject) => {
        let startupComplete = false;
        const timeout = setTimeout(
          () => reject(new Error("HLS manifest did not become ready")),
          30000
        );
        signal.addEventListener("abort", () => {
          clearTimeout(timeout);
          reject(new DOMException("Tuning cancelled", "AbortError"));
        }, {once: true});
        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          clearTimeout(timeout);
          startupComplete = true;
          resolve();
        });
        hls.on(Hls.Events.FRAG_BUFFERED, () => {
          clearTimeout(hlsRecoveryTimer);
          hlsNetworkRecoveries = 0;
        });
        hls.on(Hls.Events.ERROR, (_event, data) => {
          if (!data.fatal) return;
          clearTimeout(timeout);
          const errorDetail = {
            type: data.type,
            details: data.details,
            error: data.error?.message || String(data.error || "")
          };
          console.error("Fatal HLS playback error", errorDetail);
          reportClientEvent("hls-fatal", JSON.stringify(errorDetail));
          if (!startupComplete) {
            reject(new Error(`HLS startup failed: ${data.details || data.type}`));
          } else if (
            data.type === Hls.ErrorTypes.NETWORK_ERROR &&
            hlsNetworkRecoveries < 3
          ) {
            hlsNetworkRecoveries += 1;
            message.textContent = "Stream delayed — resuming…";
            hls.startLoad();
            clearTimeout(hlsRecoveryTimer);
            hlsRecoveryTimer = setTimeout(() => {
              reportClientEvent(
                "network-recovery-timeout",
                "No fragment buffered within 6 seconds"
              );
              if (channelSelect.value) tune(channelSelect.value);
            }, 6000);
          } else if (
            data.type === Hls.ErrorTypes.MEDIA_ERROR &&
            hlsMediaRecoveries < 2
          ) {
            hlsMediaRecoveries += 1;
            message.textContent = "Decoder recovering…";
            hls.recoverMediaError();
          } else {
            recover();
          }
        });
      });
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
      video.load();
    } else {
      throw new Error("This browser has no HLS playback support");
    }
    if (signal.aborted) throw new DOMException("Tuning cancelled", "AbortError");
    return requestPlayback();
  }

  function transitionAfterPlayback() {
    if (isTuning || !channelSelect.value) return;
    clearTimeout(boundaryTimer);
    clearTimeout(boundaryFadeTimer);
    video.classList.add("switching");
    boundaryFadeTimer = setTimeout(
      () => tune(channelSelect.value, {boundary: true}),
      TRANSITION_MS
    );
  }

  async function tune(channel, {boundary = false} = {}) {
    if (tuneAbort) tuneAbort.abort();
    tuneAbort = new AbortController();
    const signal = tuneAbort.signal;
    isTuning = true;
    video.classList.add("switching");
    clearTimeout(recoveryTimer);
    message.textContent = boundary ? "" : "Tuning…";
    await stopSession();
    try {
      const result = await api("/api/watch/sessions", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({channel: String(channel), profile: "auto"}),
        signal
      });
      sessionId = result.session_id;
      nowInfo = result.now;
      renderNow();
      const playbackStarted = await attach(result.playlist_url, signal);
      // Playback completion, not wall time, owns item transitions. This late
      // watchdog only recovers a browser that never emits `ended`; it can
      // never truncate buffered commercial frames.
      const boundaryDelay = Number(nowInfo.item_remaining) * 1000 + 30000;
      boundaryTimer = setTimeout(
        () => {
          reportClientEvent(
            "item-end-watchdog",
            "Video did not end within 30 seconds of scheduled completion"
          );
          transitionAfterPlayback();
        },
        Math.max(250, boundaryDelay)
      );
      heartbeat = setInterval(() => {
        if (sessionId && !document.hidden && !video.paused && !video.ended) {
          fetch(`/api/watch/sessions/${sessionId}/heartbeat`, {method: "POST"});
        }
      }, 20000);
      if (playbackStarted) message.textContent = "";
      localStorage.setItem("fs42-channel", String(channel));
    } catch (error) {
      if (error.name === "AbortError") return;
      console.error("HomeTV startup failed", error);
      message.textContent = error.message;
      isTuning = false;
      recover(true);
      return;
    }
    isTuning = false;
  }

  function recover(force = false) {
    if (isTuning || (!sessionId && !force)) return;
    if (recoveryTimer) return;
    const delay = Math.min(30000, 3000 * (2 ** Math.min(recoveryAttempts, 3)));
    recoveryAttempts += 1;
    message.textContent = `Playback interrupted — retrying in ${delay / 1000}s…`;
    recoveryTimer = setTimeout(() => {
      recoveryTimer = null;
      if (channelSelect.value) tune(channelSelect.value);
    }, delay);
  }

  function renderNow() {
    if (!nowInfo) return;
    document.querySelector("#channel-number").textContent = nowInfo.channel_number;
    document.querySelector("#channel-name").textContent = nowInfo.channel_name;
    document.querySelector("#program-title").textContent = nowInfo.program_title;
    document.querySelector("#episode-title").textContent =
      nowInfo.program_details || "";
  }

  async function refreshNow() {
    if (!channelSelect.value) return;
    try {
      const previousEnd = nowInfo?.end;
      nowInfo = await api(`/api/watch/channels/${encodeURIComponent(channelSelect.value)}/now`);
      renderNow();
      if (previousEnd && previousEnd !== nowInfo.end) tune(channelSelect.value);
    } catch (_) {}
  }

  function adjacent(delta) {
    const index = Math.max(0, channels.findIndex(c => c.channel_number === channelSelect.value));
    const next = channels[(index + delta + channels.length) % channels.length];
    if (next) { channelSelect.value = next.channel_number; tune(next.channel_number); }
  }

  async function start() {
    try {
      channels = (await api("/api/watch/channels")).channels;
      channelSelect.innerHTML = channels.map(c =>
        `<option value="${c.channel_number}">${c.channel_number} — ${c.channel_name}</option>`
      ).join("");
      const saved = localStorage.getItem("fs42-channel");
      if (saved && channels.some(c => c.channel_number === saved)) channelSelect.value = saved;
      if (!channelSelect.value && channels[0]) channelSelect.value = channels[0].channel_number;
      if (!channelSelect.value) throw new Error("No scheduled channels are configured");
      await tune(channelSelect.value);
    } catch (error) {
      message.textContent = error.message;
      setTimeout(start, 5000);
    }
  }

  channelSelect.addEventListener("change", () => tune(channelSelect.value));
  document.querySelector("#previous").onclick = () => adjacent(-1);
  document.querySelector("#next").onclick = () => adjacent(1);
  document.querySelector("#mute").onclick = () => {
    video.muted = !video.muted;
    document.body.classList.toggle("muted", video.muted);
    document.querySelector("#mute").setAttribute(
      "aria-label",
      video.muted ? "Unmute" : "Mute"
    );
  };
  document.querySelector("#volume").oninput = event => video.volume = event.target.value;
  document.querySelector("#fullscreen").onclick = () => document.querySelector("#viewer").requestFullscreen();
  document.querySelector("#guide-button").onclick = () => document.querySelector("#guide").hidden = false;
  document.querySelector("#guide-close").onclick = () => document.querySelector("#guide").hidden = true;
  document.addEventListener("mousemove", showControls);
  document.addEventListener("click", showControls);
  document.addEventListener("click", () => {
    if (sessionId && video.paused && !document.hidden) requestPlayback();
  });
  document.addEventListener("keydown", event => {
    showControls();
    if ((event.key === "Enter" || event.key === " ") && sessionId && video.paused) {
      requestPlayback();
    }
    if (event.key === "ArrowUp" || event.key === "ArrowRight") adjacent(1);
    if (event.key === "ArrowDown" || event.key === "ArrowLeft") adjacent(-1);
    if (event.key.toLowerCase() === "m") document.querySelector("#mute").click();
    if (event.key.toLowerCase() === "f") document.querySelector("#fullscreen").click();
    if (event.key.toLowerCase() === "g") document.querySelector("#guide-button").click();
  });
  video.addEventListener("error", () => {
    const errorDetail = {
      code: video.error?.code,
      message: video.error?.message
    };
    console.error("Video element error", errorDetail);
    reportClientEvent("video-error", JSON.stringify(errorDetail));
    recover();
  });
  video.addEventListener("playing", () => {
    clearTimeout(hlsRecoveryTimer);
    recoveryAttempts = 0;
    hlsNetworkRecoveries = 0;
    hlsMediaRecoveries = 0;
    message.textContent = "";
    requestAnimationFrame(() => video.classList.remove("switching"));
  });
  video.addEventListener("ended", transitionAfterPlayback);
  window.addEventListener("pagehide", stopSession);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopSession();
    } else if (!sessionId && channelSelect.value) {
      tune(channelSelect.value);
    }
  });
  setInterval(() => {
    if (nowInfo) {
      const elapsed = (Date.now() - Date.parse(nowInfo.server_time)) / 1000 + nowInfo.elapsed;
      progress.style.width = `${Math.min(100, 100 * elapsed / nowInfo.duration)}%`;
    }
  }, 1000);
  setInterval(refreshNow, 15000);
  showControls();
  if (!document.hidden) start();
})();
