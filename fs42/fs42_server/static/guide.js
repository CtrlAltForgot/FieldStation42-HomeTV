(() => {
  "use strict";

  const WINDOW_MINUTES = 720;
  const PIXELS_PER_MINUTE = 11;
  const PREVIEW_DELAY = 400;
  const state = {
    stations: [], rows: [], rowIndex: 0, blockIndex: 0,
    start: null, end: null, selected: null,
    previewTimer: null, previewSession: null, hls: null,
    requestToken: 0, artworkToken: 0,
    artworkCache: new Map(), artworkFailures: new Set(), autoFollowNow: true,
    previewEnabled: localStorage.getItem("fs42-guide-preview") !== "false",
    previewActive: !["watch", "compact"].includes(
      new URLSearchParams(window.location.search).get("embedded") ||
      (new URLSearchParams(window.location.search).has("compact") ? "compact" : "")
    )
  };
  let channelDigits = "", channelDigitTimer = null;

  const $ = selector => document.querySelector(selector);
  const scroll = $("#guide-scroll");
  const video = $("#preview-video");
  const art = $("#preview-art");
  const liveFrame = $("#preview-live-news");
  if (video) video.disablePictureInPicture = true;

  function api(url, options) {
    return fetch(url, options).then(async response => {
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `Request failed (${response.status})`);
      }
      return response.status === 204 ? null : response.json();
    });
  }

  function dateForApi(value) {
    const local = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
    return local.toISOString().slice(0, 19);
  }

  function formatTime(value) {
    return value.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"})
      .replace(" ", "").toLowerCase();
  }

  function text(value, fallback = "") {
    return value === null || value === undefined || value === "" ? fallback : String(value);
  }

  function programTitle(block) {
    if (localStorage.getItem("fs42-guide-debug") === "true") {
      const path = block.content?.path || block.content?.realpath;
      if (path) return path.split(/[\\/]/).pop();
    }
    return text(block.display_title || block.title, "Untitled");
  }

  function resetWindow() {
    const now = new Date();
    state.start = new Date(
      now.getFullYear(), now.getMonth(), now.getDate(), now.getHours(), now.getMinutes()
    );
    state.end = new Date(state.start.getTime() + WINDOW_MINUTES * 60_000);
    document.documentElement.style.setProperty("--track-width", `${WINDOW_MINUTES * PIXELS_PER_MINUTE}px`);
  }

  async function load() {
    $("#guide-message").hidden = false;
    resetWindow();
    const all = await window.fs42Common.fetchStationSummary();
    state.stations = all
      .filter(item => !item.hidden && (item._has_schedule || item.is_live_source))
      .sort((a, b) => String(a.channel_number).localeCompare(String(b.channel_number), undefined, {numeric: true}));
    const extendedStart = new Date(state.start.getTime() - 3 * 60 * 60_000);
    state.rows = [];
    // Bound browser/API concurrency on large channel lineups while retaining
    // parallel loading. Six short SQLite readers keeps guide startup brisk
    // without creating a request storm on a small home server.
    for (let index = 0; index < state.stations.length; index += 6) {
      const batch = await Promise.all(state.stations.slice(index, index + 6).map(async station => ({
        station,
        blocks: await window.fs42Common.fetchSchedule(
          station.network_name, dateForApi(extendedStart), dateForApi(state.end), true, true
        )
      })));
      state.rows.push(...batch);
    }
    state.rows.forEach(row => {
      row.visibleBlocks = row.blocks.filter(block => {
        const start = new Date(block.start_time);
        const end = new Date(block.end_time);
        return end > state.start && start < state.end;
      });
    });
    await prewarmArtwork();
    render();
    selectInitial();
    $("#guide-message").hidden = true;
  }

  function artworkKey(station, block) {
    if (station.is_live_source) return `live:${station.channel_number}`;
    const meta = block.meta || {};
    const kind = String(meta.type || "program").toLocaleLowerCase();
    const series = meta.show_title || block.display_title || block.title || "";
    return `${kind}:${String(series).toLocaleLowerCase().normalize("NFKD").replace(/[^\p{L}\p{N}]+/gu, "")}`;
  }

  function artworkUrl(station, block) {
    return `/api/watch/channels/${encodeURIComponent(station.channel_number)}/artwork?at=${encodeURIComponent(block.start_time)}`;
  }

  async function cacheArtwork(key, url) {
    if (state.artworkCache.has(key)) return;
    const response = await fetch(url, {cache: "force-cache"});
    if (!response.ok) throw new Error(`Artwork request failed (${response.status})`);
    const blobUrl = URL.createObjectURL(await response.blob());
    try {
      await new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = resolve; image.onerror = reject; image.src = blobUrl;
      });
      state.artworkCache.set(key, blobUrl);
    } catch (error) {
      URL.revokeObjectURL(blobUrl);
      throw error;
    }
  }

  async function prewarmArtwork() {
    const jobs = new Map();
    state.rows.forEach(row => row.visibleBlocks.forEach(block => {
      const key = artworkKey(row.station, block);
      if (!jobs.has(key)) jobs.set(key, artworkUrl(row.station, block));
    }));
    const pending = [...jobs].filter(([key]) => !state.artworkCache.has(key));
    let complete = 0;
    for (let index = 0; index < pending.length; index += 6) {
      await Promise.all(pending.slice(index, index + 6).map(async ([key, url]) => {
        try { await cacheArtwork(key, url); }
        catch (error) { state.artworkFailures.add(key); console.error("Artwork preload failed", key, error); }
        complete += 1;
        $("#guide-message").textContent = `Preparing guide artwork ${complete}/${pending.length}…`;
      }));
    }
  }

  function renderTimeline() {
    const timeline = $("#timeline");
    timeline.replaceChildren();
    const corner = document.createElement("div");
    corner.className = "timeline-corner";
    const day = document.createElement("span");
    day.textContent = state.start.toLocaleDateString([], {weekday: "short", month: "short", day: "numeric"});
    const clock = document.createElement("time");
    clock.id = "guide-clock";
    corner.append(day, clock);
    timeline.append(corner);
    for (let minutes = 0; minutes <= WINDOW_MINUTES; minutes += 30) {
      const label = document.createElement("div");
      label.className = "timeline-label";
      label.style.left = `calc(var(--channel-width) + ${minutes * PIXELS_PER_MINUTE}px)`;
      label.style.width = `${30 * PIXELS_PER_MINUTE}px`;
      label.textContent = formatTime(new Date(state.start.getTime() + minutes * 60_000));
      timeline.append(label);
    }
  }

  function render() {
    renderTimeline();
    const rows = $("#rows");
    rows.replaceChildren();
    const now = new Date();
    state.rows.forEach((row, rowIndex) => {
      const rowElement = document.createElement("div");
      rowElement.className = "guide-row";
      const channel = document.createElement("div");
      channel.className = "channel-cell";
      const number = document.createElement("span");
      number.className = "channel-number";
      number.textContent = text(row.station.channel_number, "—");
      const name = document.createElement("span");
      name.className = "channel-name";
      name.textContent = text(row.station.network_long_name || row.station.network_name);
      channel.append(number, name);
      const track = document.createElement("div");
      track.className = "program-track";
      row.visibleBlocks.forEach((block, blockIndex) => {
        const start = new Date(block.start_time);
        const end = new Date(block.end_time);
        const visibleStart = Math.max(start, state.start);
        const visibleEnd = Math.min(end, state.end);
        const button = document.createElement("button");
        button.className = "program";
        if (start <= now && end > now) button.classList.add("current");
        button.style.left = `${(visibleStart - state.start) / 60_000 * PIXELS_PER_MINUTE + 2}px`;
        button.style.width = `${Math.max(34, (visibleEnd - visibleStart) / 60_000 * PIXELS_PER_MINUTE - 4)}px`;
        button.dataset.row = rowIndex;
        button.dataset.block = blockIndex;
        button.tabIndex = -1;
        const title = document.createElement("span");
        title.className = "program-title";
        title.textContent = programTitle(block);
        if (["premiere", "rerun"].includes(block.airing_kind)) {
          const badge = document.createElement("b");
          badge.className = `airing-badge ${block.airing_kind}`;
          badge.textContent = block.airing_kind === "premiere" ? "NEW" : "RERUN";
          title.prepend(badge);
        }
        const subtitle = document.createElement("span");
        subtitle.className = "program-subtitle";
        subtitle.textContent = `${formatTime(start)}${block.program_details ? ` · ${block.program_details}` : ""}`;
        button.append(title, subtitle);
        button.addEventListener("click", () => select(rowIndex, blockIndex, true));
        track.append(button);
      });
      if (!row.visibleBlocks.length) {
        const empty = document.createElement("div");
        empty.className = "program empty-program";
        empty.style.left = "2px";
        empty.style.width = `calc(100% - 4px)`;
        empty.textContent = "No programming available";
        track.append(empty);
      }
      rowElement.append(channel, track);
      rows.append(rowElement);
    });
    updateClockAndMarker();
  }

  function selectInitial() {
    const now = new Date();
    let candidate = null;
    const savedChannel = localStorage.getItem("fs42-channel");
    const orderedRows = state.rows.map((row, rowIndex) => ({row, rowIndex}));
    orderedRows.sort((a, b) => Number(b.row.station.channel_number === savedChannel) - Number(a.row.station.channel_number === savedChannel));
    orderedRows.some(({row, rowIndex}) => {
      const blockIndex = row.visibleBlocks.findIndex(block => new Date(block.start_time) <= now && new Date(block.end_time) > now);
      if (blockIndex >= 0) { candidate = [rowIndex, blockIndex]; return true; }
      return false;
    });
    if (!candidate) {
      const rowIndex = state.rows.findIndex(row => row.visibleBlocks.length);
      candidate = rowIndex >= 0 ? [rowIndex, 0] : null;
    }
    if (candidate) select(candidate[0], candidate[1], true);
    else $("#guide-message").textContent = "No programming is scheduled in this time window.";
  }

  function select(rowIndex, blockIndex, ensureVisible = false) {
    const row = state.rows[rowIndex];
    const block = row?.visibleBlocks[blockIndex];
    if (!block) return;
    document.querySelector(".program.selected")?.classList.remove("selected");
    state.rowIndex = rowIndex;
    state.blockIndex = blockIndex;
    state.selected = {row, block};
    const element = document.querySelector(`.program[data-row="${rowIndex}"][data-block="${blockIndex}"]`);
    element?.classList.add("selected");
    element?.focus({preventScroll: true});
    renderPreviewDetails();
    scheduleLivePreview();
    if (ensureVisible && element) reveal(element);
  }

  function reveal(element) {
    const channelWidth = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--channel-width")) || 180;
    const left = element.offsetLeft + channelWidth;
    const right = left + element.offsetWidth;
    const viewportLeft = scroll.scrollLeft + channelWidth;
    const viewportRight = scroll.scrollLeft + scroll.clientWidth;
    if (left < viewportLeft + 12) scroll.scrollLeft = Math.max(0, left - channelWidth - 12);
    else if (right > viewportRight - 12) scroll.scrollLeft += right - viewportRight + 12;
    const row = element.closest(".guide-row");
    const top = row.offsetTop;
    const bottom = top + row.offsetHeight;
    if (top < scroll.scrollTop + 46) scroll.scrollTop = Math.max(0, top - 46);
    else if (bottom > scroll.scrollTop + scroll.clientHeight) scroll.scrollTop += bottom - (scroll.scrollTop + scroll.clientHeight);
  }

  function renderPreviewDetails() {
    const {row, block} = state.selected;
    const station = row.station;
    const start = new Date(block.start_time);
    const end = new Date(block.end_time);
    const now = new Date();
    const live = start <= now && end > now;
    const meta = block.meta || {};
    const airingLabel = block.airing_kind === "premiere" ? "NEW" : block.airing_kind === "rerun" ? "RERUN" : "";
    $("#preview-kicker").textContent = `${live ? "ON NOW" : "UPCOMING"}${airingLabel ? ` · ${airingLabel}` : ""} · CH ${text(station.channel_number, "—")} · ${text(station.network_long_name || station.network_name)}`;
    $("#preview-title").textContent = programTitle(block);
    const facts = [`${formatTime(start)}–${formatTime(end)}`];
    if (block.program_details) facts.push(block.program_details);
    if (meta.year) facts.push(meta.year);
    if (meta.rating) facts.push(meta.rating);
    if (meta.genre) facts.push(Array.isArray(meta.genre) ? meta.genre[0] : meta.genre);
    $("#preview-meta").textContent = facts.filter(Boolean).join("  ·  ");
    $("#preview-description").textContent = text(meta.plot || meta.description || meta.outline, "Program information is not available yet.");
    $("#preview-status").textContent = live ? "ON NOW" : "LATER";
    $("#preview-status").classList.toggle("upcoming", !live);
    $("#watch-button").hidden = !live;
    $("#preview-progress").hidden = !live;
    updateProgress();
    art.hidden = true;
    art.removeAttribute("src");
    loadPreviewArtwork(station, block);
  }

  async function loadPreviewArtwork(station, block) {
    const token = ++state.artworkToken;
    art.hidden = true;
    const key = artworkKey(station, block);
    let blobUrl = state.artworkCache.get(key);
    if (!blobUrl && !state.artworkFailures.has(key)) {
      try {
        await cacheArtwork(key, artworkUrl(station, block));
        blobUrl = state.artworkCache.get(key);
      } catch (error) {
        state.artworkFailures.add(key);
        console.error("Show-specific artwork unavailable", error);
      }
    }
    if (token !== state.artworkToken) return;
    if (blobUrl) {
      art.src = blobUrl;
      art.hidden = false;
      $("#artwork-loading").hidden = true;
      return;
    }
    $("#artwork-loading").hidden = false;
    $("#artwork-loading span").textContent = "Artwork unavailable";
  }

  function updateProgress() {
    if (!state.selected) return;
    const start = new Date(state.selected.block.start_time);
    const end = new Date(state.selected.block.end_time);
    const now = new Date();
    const live = start <= now && end > now;
    if (live) $("#preview-progress span").style.width = `${Math.max(0, Math.min(100, 100 * (now - start) / (end - start)))}%`;
  }

  function scheduleLivePreview() {
    clearTimeout(state.previewTimer);
    stopPreview();
    if (!state.previewEnabled || !state.previewActive || !state.selected) return;
    const start = new Date(state.selected.block.start_time);
    const end = new Date(state.selected.block.end_time);
    if (!(start <= new Date() && end > new Date())) return;
    state.previewTimer = setTimeout(startPreview, PREVIEW_DELAY);
  }

  async function startPreview() {
    if (!state.selected) return;
    const token = ++state.requestToken;
    $("#preview-loading").hidden = false;
    try {
      const result = await api("/api/watch/sessions", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({channel: String(state.selected.row.station.channel_number), profile: "auto"})
      });
      if (token !== state.requestToken) {
        fetch(`/api/watch/sessions/${result.session_id}`, {method: "DELETE", keepalive: true});
        return;
      }
      state.previewSession = result.session_id;
      if (result.playback_kind === "embed") {
        // Older cached guide_frame.html versions do not contain this iframe.
        // Create it lazily so mixed-version browser caches remain functional.
        const frame = $("#preview-live-news") || (() => {
          const created = document.createElement("iframe");
          created.id = "preview-live-news";
          created.title = "Live news preview";
          created.allow = "autoplay; encrypted-media";
          created.tabIndex = -1;
          $("#preview-media")?.prepend(created);
          return created;
        })();
        frame.src = result.embed_url;
        frame.hidden = false;
        video.hidden = true;
        art.hidden = true;
        $("#preview-loading").hidden = true;
        return;
      }
      const showVideo = () => {
        if (token !== state.requestToken) return;
        video.hidden = false;
        art.hidden = true;
        $("#preview-loading").hidden = true;
      };
      video.addEventListener("playing", showVideo, {once: true});
      if (window.Hls && Hls.isSupported()) {
        state.hls = new Hls({liveSyncDurationCount: 1, maxBufferLength: 20, backBufferLength: 0});
        state.hls.loadSource(result.playlist_url);
        state.hls.attachMedia(video);
        state.hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
      } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = result.playlist_url;
        video.play().catch(() => {});
      }
    } catch (error) {
      console.warn("Live guide preview unavailable", error);
      video.hidden = true;
      if (token === state.requestToken) $("#preview-loading").hidden = true;
    } finally {
      if (token === state.requestToken && !video.hidden) $("#preview-loading").hidden = true;
    }
  }

  function stopPreview() {
    state.requestToken += 1;
    clearTimeout(state.previewTimer);
    $("#preview-loading").hidden = true;
    if (state.hls) { state.hls.destroy(); state.hls = null; }
    video.pause();
    video.hidden = true;
    video.removeAttribute("src");
    const activeLiveFrame = $("#preview-live-news");
    if (activeLiveFrame) {
      activeLiveFrame.hidden = true;
      activeLiveFrame.removeAttribute("src");
    }
    if (state.previewSession) {
      const id = state.previewSession;
      state.previewSession = null;
      fetch(`/api/watch/sessions/${id}`, {method: "DELETE", keepalive: true}).catch(() => {});
    }
  }

  function moveHorizontal(delta) {
    state.autoFollowNow = false;
    const row = state.rows[state.rowIndex];
    select(state.rowIndex, Math.max(0, Math.min(row.visibleBlocks.length - 1, state.blockIndex + delta)), true);
  }

  function moveVertical(delta) {
    const selected = state.selected?.block;
    if (!selected) return;
    const targetTime = (new Date(selected.start_time).getTime() + new Date(selected.end_time).getTime()) / 2;
    let rowIndex = state.rowIndex;
    do { rowIndex += delta; } while (state.rows[rowIndex] && !state.rows[rowIndex].visibleBlocks.length);
    const row = state.rows[rowIndex];
    if (!row) return;
    let closest = 0, distance = Infinity;
    row.visibleBlocks.forEach((block, index) => {
      const start = new Date(block.start_time).getTime();
      const end = new Date(block.end_time).getTime();
      const nextDistance = targetTime < start ? start - targetTime : targetTime > end ? targetTime - end : 0;
      if (nextDistance < distance) { closest = index; distance = nextDistance; }
    });
    select(rowIndex, closest, true);
  }

  function watchSelected() {
    if (!state.selected) return;
    const start = new Date(state.selected.block.start_time);
    const end = new Date(state.selected.block.end_time);
    if (start <= new Date() && end > new Date()) {
      stopPreview();
      window.top.location.href = `/watch?channel=${encodeURIComponent(state.selected.row.station.channel_number)}`;
    }
  }

  function returnToNow() {
    stopPreview();
    state.autoFollowNow = true;
    const now = new Date();
    const currentRow = state.rows[state.rowIndex];
    const currentIndex = currentRow?.visibleBlocks.findIndex(
      block => new Date(block.start_time) <= now && new Date(block.end_time) > now
    );
    if (currentIndex >= 0) select(state.rowIndex, currentIndex, false);
    updateClockAndMarker();
  }

  function jumpToChannel(digit) {
    channelDigits += digit;
    clearTimeout(channelDigitTimer);
    const apply = () => {
      const exact = state.rows.findIndex(row => String(row.station.channel_number) === channelDigits);
      const prefix = state.rows.findIndex(row => String(row.station.channel_number).startsWith(channelDigits));
      const rowIndex = exact >= 0 ? exact : prefix;
      if (rowIndex >= 0 && state.rows[rowIndex].visibleBlocks.length) {
        const now = new Date();
        const blockIndex = Math.max(0, state.rows[rowIndex].visibleBlocks.findIndex(block => new Date(block.start_time) <= now && new Date(block.end_time) > now));
        select(rowIndex, blockIndex, true);
      }
      channelDigits = "";
    };
    if (state.rows.some(row => String(row.station.channel_number).startsWith(channelDigits) && String(row.station.channel_number) !== channelDigits)) {
      channelDigitTimer = setTimeout(apply, 700);
    } else apply();
  }

  function closeGuide() {
    if (window.parent !== window) window.parent.postMessage({type: "fs42-guide-close"}, window.location.origin);
    else if (history.length > 1) history.back();
    else window.location.href = "/watch";
  }

  function updateClockAndMarker() {
    const now = new Date();
    const clock = $("#guide-clock");
    if (clock) clock.textContent = formatTime(now);
    // Let the grid drift left by a few pixels each second so the channel
    // boundary itself is the current-time indicator. No disruptive reload or
    // red line is needed. Once the viewer browses horizontally, leave their
    // chosen position alone until Return to now is pressed.
    if (state.autoFollowNow) {
      scroll.scrollLeft = Math.max(0, (now - state.start) / 60_000 * PIXELS_PER_MINUTE);
    }
    document.querySelectorAll(".program[data-row]").forEach(element => {
      const block = state.rows[Number(element.dataset.row)]?.visibleBlocks[Number(element.dataset.block)];
      if (block) element.classList.toggle(
        "current", new Date(block.start_time) <= now && new Date(block.end_time) > now
      );
    });
    updateProgress();
  }

  function showError(error) {
    console.error(error);
    $("#guide-message").hidden = false;
    $("#guide-message").textContent = `Guide unavailable — ${error.message}`;
  }

  document.addEventListener("keydown", event => {
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Enter", " ", "Escape", "Backspace"].includes(event.key)) event.preventDefault();
    if (event.key === "ArrowLeft") moveHorizontal(-1);
    else if (event.key === "ArrowRight") moveHorizontal(1);
    else if (event.key === "ArrowUp") moveVertical(-1);
    else if (event.key === "ArrowDown") moveVertical(1);
    else if (event.key === "Enter" || event.key === " ") watchSelected();
    else if (event.key === "Escape" || event.key === "Backspace") closeGuide();
    else if (event.key.toLowerCase() === "n") returnToNow();
    else if (/^\d$/.test(event.key)) jumpToChannel(event.key);
  });
  $("#watch-button").addEventListener("click", watchSelected);
  $("#now-button").addEventListener("click", returnToNow);
  window.addEventListener("message", event => {
    if (event.origin !== window.location.origin) return;
    if (event.data?.type === "myhometv-live-error") {
      const frame = $("#preview-live-news");
      if (frame) frame.hidden = true;
      if (art.src) art.hidden = false;
      $("#preview-loading").hidden = true;
    }
    if (event.data?.type === "fs42-guide-hidden") { state.previewActive = false; stopPreview(); }
    if (event.data?.type === "fs42-guide-shown") { state.previewActive = true; scheduleLivePreview(); }
  });
  window.addEventListener("pagehide", stopPreview);
  setInterval(updateClockAndMarker, 1000);
  scroll.addEventListener("wheel", () => { state.autoFollowNow = false; }, {passive:true});
  load().catch(showError);
})();
