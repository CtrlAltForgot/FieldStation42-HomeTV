(function () {
  "use strict";
  function $(selector) { return document.querySelector(selector); }
  var server = localStorage.getItem("myhometv-server") || "http://192.168.1.254:4243";
  var rows = [], row = 0, program = 0, pane = "channels", sessionId = null;
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
    $("#channels").innerHTML = rows.map(function (item, index) {
      return '<div class="item ' + (pane === "channels" && index === row ? "focus" : "") + '">' + item.channel_number + '&nbsp;&nbsp; ' + item.channel_name + '</div>';
    }).join("");
    var programs = rows[row] && rows[row].programs || [];
    $("#programs").innerHTML = programs.map(function (item, index) {
      return '<div class="item ' + (pane === "programs" && index === program ? "focus" : "") + '">' + titleOf(item) + '&nbsp;&nbsp; ' + (item.program_details || "") + '</div>';
    }).join("") || '<div class="item">No programming available</div>';
    showDetails();
  }

  function showDetails() {
    var channel = rows[row];
    if (!channel) return;
    var item = (channel.programs || [])[program];
    $("#title").textContent = item ? titleOf(item) : channel.channel_name;
    $("#details").textContent = item && item.program_details || "No programming available";
    var metadata = item && item.meta || {};
    $("#description").textContent = metadata.plot || metadata.description || "";
    $("#artwork").src = server + channel.artwork_url + (item ? "?at=" + encodeURIComponent(item.start_time) : "");
  }

  function loadGuide() {
    $("#status").textContent = "Connecting to " + server + "…";
    return api("/api/tv/guide?hours=12").then(function (result) {
      rows = result.channels || [];
      row = Math.min(row, Math.max(0, rows.length - 1));
      program = 0;
      $("#status").textContent = "Ready";
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
    if (key === 37) pane = "channels";
    else if (key === 39) pane = "programs";
    else if (key === 38) {
      if (pane === "channels") { row = Math.max(0,row-1); program=0; }
      else program = Math.max(0,program-1);
    } else if (key === 40) {
      if (pane === "channels") { row=Math.min(rows.length-1,row+1); program=0; }
      else {
        var length = rows[row] && rows[row].programs && rows[row].programs.length || 1;
        program=Math.min(length-1,program+1);
      }
    } else if (key === 13) tune();
    else if (key === 405) openSettings();
    else if (key === 427) changeChannel(1);
    else if (key === 428) changeChannel(-1);
    render();
  });
  setInterval(function () {
    $("#clock").textContent = new Date().toLocaleTimeString([], {hour:"numeric",minute:"2-digit"});
  }, 1000);
  loadGuide();
}());
