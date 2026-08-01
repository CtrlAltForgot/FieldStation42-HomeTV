(function () {
  "use strict";
  function $(selector) { return document.querySelector(selector); }
  var server = localStorage.getItem("myhometv-server") || "http://192.168.1.254:4243";
  var rows = [], timeline = [], row = 0, program = 0, sessionId = null;
  var windowOffset = 0, windowMinutes = 180;
  var autoFollowNow = true, guideLoadedAt = 0, lastGuideShift = 0;
  var video = $("#video");

  function api(path, options) {
    return fetch(server + path, options || {}).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) throw new Error(body.detail || "Request failed (" + response.status + ")");
        return body;
      });
    });
  }

  function titleOf(item) {
    return item && (item.display_title || item.title) || "Untitled";
  }

  function render() {
    renderTimeline();
    var first = Math.max(0, Math.min(row - 3, rows.length - 7));
    $("#epg").innerHTML = rows.slice(first, first + 7).map(function (channel, offset) {
      var rowIndex = first + offset;
      var entries = channel.programs || [];
      var cards = entries.map(function (item, itemIndex) {
        var starts = Number(item.guide_start_minute);
        var ends = Number(item.guide_end_minute);
        var visibleStart = Math.max(starts, windowOffset);
        var visibleEnd = Math.min(ends, windowOffset + windowMinutes);
        if (!isFinite(starts) || visibleEnd <= visibleStart) return "";
        var focused = rowIndex === row && itemIndex === program;
        var left = 100 * (visibleStart - windowOffset) / windowMinutes;
        var width = Math.max(5, 100 * (visibleEnd - visibleStart) / windowMinutes);
        var details = (item.guide_time || "") + (item.program_details ? " · " + item.program_details : "");
        return '<div class="program-card ' + (focused ? "focus" : "") + '" style="left:' + left + '%;width:calc(' + width + '% - 5px)"><strong>' + escapeHtml(titleOf(item)) + '</strong><span>' + escapeHtml(details) + '</span></div>';
      }).join("") || '<div class="program-card empty">No programming available</div>';
      return '<div class="epg-row"><div class="channel-card">' + escapeHtml(channel.channel_number + "  " + channel.channel_name) + '</div><div class="program-strip">' + cards + '</div></div>';
    }).join("");
    showDetails();
  }

  function renderTimeline() {
    $("#timeline").innerHTML = '<span class="now-label" style="left:0">NOW</span>' + timeline.map(function (tick) {
      if (tick.minute < windowOffset || tick.minute > windowOffset + windowMinutes) return "";
      var left = 100 * (tick.minute - windowOffset) / windowMinutes;
      return '<span style="left:' + left + '%">' + escapeHtml(tick.label) + '</span>';
    }).join("");
  }

  function ensureVisible(item) {
    if (!item) return;
    var starts = Number(item.guide_start_minute), ends = Number(item.guide_end_minute);
    if (starts < windowOffset) windowOffset = Math.max(0, starts);
    else if (ends > windowOffset + windowMinutes) windowOffset = Math.max(0, starts - 30);
  }

  function closestProgram(rowIndex, target) {
    var entries = rows[rowIndex] && rows[rowIndex].programs || [];
    var closest = 0, best = Infinity;
    entries.forEach(function (item, index) {
      var starts = Number(item.guide_start_minute), ends = Number(item.guide_end_minute);
      var distance = target < starts ? starts - target : target > ends ? target - ends : 0;
      if (distance < best) { best = distance; closest = index; }
    });
    return closest;
  }

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, function (character) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[character];
    });
  }

  function showDetails() {
    var channel = rows[row];
    if (!channel) return;
    var item = (channel.programs || [])[program];
    $("#title").textContent = item ? titleOf(item) : channel.channel_name;
    $("#details").textContent = item ? (item.guide_time_range || "") + (item.program_details ? " · " + item.program_details : "") : "No programming available";
    $("#description").textContent = item && item.program_description || "";
    var art = item && item.artwork_url;
    if (art) $("#artwork").src = server + art;
  }

  function loadGuide() {
    $("#status").textContent = "Connecting to " + server + "…";
    return api("/api/tv/guide?hours=6").then(function (result) {
      rows = result.channels || [];
      timeline = result.timeline || [];
      windowOffset = Number(result.current_offset_minute || 0);
      guideLoadedAt = Date.now(); lastGuideShift = guideLoadedAt;
      row = Math.min(row, Math.max(0, rows.length - 1));
      program = 0;
      $("#status").textContent = "";
      render();
    }).catch(function (error) { $("#status").textContent = error.message; });
  }

  function stop() {
    video.pause(); video.removeAttribute("src"); video.load();
    $("#player").hidden = true;
    if (sessionId) {
      var old = sessionId; sessionId = null;
      fetch(server + "/api/watch/sessions/" + old, {method:"DELETE"}).catch(function () {});
    }
    return Promise.resolve();
  }

  function tune() {
    if (!rows[row]) return Promise.resolve();
    return stop().then(function () {
      $("#status").textContent = "Tuning channel " + rows[row].channel_number + "…";
      return api("/api/watch/sessions", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({channel:rows[row].channel_number, profile:"auto", subtitles:"auto", client:"webos"})
      });
    }).then(function (result) {
      sessionId = result.session_id;
      var url = result.playlist_url.indexOf("/") === 0 ? server + result.playlist_url : result.playlist_url;
      var nowTitle = result.now && result.now.program_title || rows[row].channel_name;
      $("#hud-title").textContent = "CH " + rows[row].channel_number + "  " + nowTitle;
      video.src = url; $("#player").hidden = false;
      return video.play();
    }).catch(function (error) {
      $("#status").textContent = error.message; $("#player").hidden = true;
    });
  }

  function changeChannel(delta) {
    if (!rows.length) return;
    row = (row + delta + rows.length) % rows.length;
    program = 0; render(); tune();
  }

  function openSettings() {
    $("#server").value = server; $("#settings").hidden = false; $("#server").focus();
  }
  $("#settings form").addEventListener("submit", function (event) {
    event.preventDefault();
    server = $("#server").value.trim().replace(/\/$/, "");
    localStorage.setItem("myhometv-server", server); $("#settings").hidden = true; loadGuide();
  });
  video.addEventListener("ended", function () {
    if (!$("#player").hidden) tune();
  });

  document.addEventListener("keydown", function (event) {
    var key = event.keyCode;
    if (!$("#settings").hidden) {
      if (key === 461) { $("#settings").hidden = true; event.preventDefault(); }
      return;
    }
    if (!$("#player").hidden) {
      event.preventDefault();
      if (key === 461 || key === 27) stop();
      else if (key === 427 || key === 38) changeChannel(1);
      else if (key === 428 || key === 40) changeChannel(-1);
      return;
    }
    if ([13,37,38,39,40,403,404,405,406,427,428,461].indexOf(key) >= 0) event.preventDefault();
    if (key === 37) {
      autoFollowNow = false;
      program = Math.max(0, program - 1);
      ensureVisible(rows[row] && rows[row].programs[program]);
    }
    else if (key === 39) {
      autoFollowNow = false;
      var rowLength = rows[row] && rows[row].programs && rows[row].programs.length || 1;
      program = Math.min(rowLength - 1, program + 1);
      ensureVisible(rows[row] && rows[row].programs[program]);
    } else if (key === 38) {
      var current = rows[row] && rows[row].programs[program];
      var target = current ? (Number(current.guide_start_minute) + Number(current.guide_end_minute)) / 2 : windowOffset;
      row = Math.max(0,row-1); program=closestProgram(row, target);
    } else if (key === 40) {
      var selected = rows[row] && rows[row].programs[program];
      var selectedTarget = selected ? (Number(selected.guide_start_minute) + Number(selected.guide_end_minute)) / 2 : windowOffset;
      row=Math.min(rows.length-1,row+1); program=closestProgram(row, selectedTarget);
    } else if (key === 13) tune();
    else if (key === 405) openSettings();
    else if (key === 427) changeChannel(1);
    else if (key === 428) changeChannel(-1);
    render();
  });
  setInterval(function () {
    $("#clock").textContent = new Date().toLocaleTimeString([], {hour:"numeric",minute:"2-digit"});
    if (autoFollowNow && guideLoadedAt && Date.now() - lastGuideShift >= 10000) {
      windowOffset += (Date.now() - lastGuideShift) / 60000;
      lastGuideShift = Date.now(); render();
    }
  }, 1000);
  loadGuide();
}());
