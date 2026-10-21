// For browsers that can't run the page script but can run this: each page works
// as closely to how it does with the page script as the browser allows. Written
// so that the oldest browsers can run it too.
(function () {
  var d = document;
  if (/(^| )js-app( |$)/.test(d.documentElement.className) || !d.getElementById) return;
  function byId(id) { return d.getElementById(id); }
  function show(element, visible) {
    if (!element) return;
    if (visible) {
      element.removeAttribute("hidden");
      element.className = element.className.replace(/(^| )old-ie-hidden( |$)/, " ");
      element.style.display = "";
    } else {
      element.setAttribute("hidden", "hidden");
      element.style.display = "none";
    }
  }
  function listen(element, name, handler) {
    if (element.addEventListener) element.addEventListener(name, handler, false);
    else if (element.attachEvent) element.attachEvent("on" + name, handler);
  }
  function visible(element) {
    if (!element || element.style.display === "none") return false;
    return element.hasAttribute ? !element.hasAttribute("hidden") : element.getAttribute("hidden") === null;
  }

  // A date range is only shown once it's chosen.
  function manualRange(kind) {
    var choices = byId("eas_" + kind + "_options"), range = byId("eas_" + kind + "_manual");
    if (!choices || !range) return;
    function update() {
      var inputs = choices.getElementsByTagName("input"), chosen = false;
      for (var i = 0; i < inputs.length; i++) {
        if (inputs[i].value === "manual" && inputs[i].checked) chosen = true;
      }
      show(range, chosen);
    }
    listen(choices, "click", update);
    update();
  }
  manualRange("export");
  manualRange("delete");

  // The alias filter strength says its value as it's set.
  (function () {
    var strength = byId("alias_filter_strength"), label = byId("alias_filter_strength_label");
    if (!strength || !label) return;
    function update() {
      label.innerHTML = "";
      label.appendChild(d.createTextNode(strength.value + "%"));
    }
    listen(strength, "change", update);
    listen(strength, "input", update);
  })();

  // A stream's fallback delay shows only when there's something to wait for.
  (function () {
    var choices = byId("panel_fallback_action"), none = byId("stream_fallback_action_none");
    if (!choices || !none) return;
    function update() {
      show(byId("stream_fallback_delay_label"), !none.checked);
      show(byId("stream_fallback_delay_hint"), !none.checked);
    }
    listen(choices, "click", update);
    update();
  })();

  // Show password, where the browser can change a password field to a text field.
  (function () {
    var probe = d.createElement("input"), works = false;
    try {
      d.body.appendChild(probe);
      probe.type = "text";
      probe.type = "password";
      works = probe.type === "password";
    } catch (error) {}
    if (probe.parentNode) probe.parentNode.removeChild(probe);
    if (!works) return;
    function offer(labelId, checkboxId, passwordId) {
      var label = byId(labelId), checkbox = byId(checkboxId), password = byId(passwordId);
      if (!label || !checkbox || !password || !visible(byId(passwordId + "_label"))) return;
      show(label, true);
      listen(checkbox, "click", function () {
        password.type = checkbox.checked ? "text" : "password";
      });
    }
    offer("show_icecast_password_label", "show_icecast_password", "icecast_password");
    offer("settings_show_icecast_password_label", "settings_show_icecast_password", "settings_icecast_password");
  })();

  // An alternate stream's number shows once it's chosen.
  (function () {
    var prefixes = ["icecast", "settings_icecast"];
    for (var i = 0; i < prefixes.length; i++) {
      (function (prefix) {
        var checkbox = byId(prefix + "_alt_enabled"), number = byId(prefix + "_alt_number_label");
        if (!checkbox || !number || !visible(byId(prefix + "_alt_label"))) return;
        function update() { show(number, checkbox.checked); }
        listen(checkbox, "click", update);
        update();
      })(prefixes[i]);
    }
  })();

  // A sound card offers only the channels it has free.
  (function () {
    var select = byId("settings_soundcard_device");
    if (!select) return;
    function update() {
      var option = select.options[select.selectedIndex];
      var free = " " + ((option && option.getAttribute("data-channels")) || "") + " ";
      var modes = ["left", "both", "right"], checked = null, offered = [];
      for (var i = 0; i < modes.length; i++) {
        var control = byId("settings_soundcard_channel_" + modes[i]);
        var ok = modes[i] === "both" ? free.indexOf(" left ") >= 0 && free.indexOf(" right ") >= 0 : free.indexOf(" " + modes[i] + " ") >= 0;
        show(byId("settings_soundcard_channel_" + modes[i] + "_label"), ok);
        control.disabled = !ok;
        if (!ok) control.checked = false;
        if (ok) offered.push(control);
        if (control.checked) checked = control;
      }
      if (!checked && offered.length) {
        var both = byId("settings_soundcard_channel_both");
        (both.disabled ? offered[0] : both).checked = true;
      }
    }
    listen(select, "change", update);
  })();

  // Fallback audio's upload shows for a custom announcement, and its player only where it can play.
  (function () {
    var custom = byId("fallback_audio_custom"), controls = byId("fallback_custom_controls");
    if (custom && controls) {
      var update = function () { show(controls, custom.checked); };
      listen(byId("fallback_audio_default"), "click", update);
      listen(custom, "click", update);
      update();
    }
    var player = byId("fallback_baseline_audio");
    if (player && (!player.canPlayType || !player.canPlayType(player.getAttribute("data-audio-type")))) show(player, false);
  })();

  // Copy to clipboard, where the browser can copy.
  (function () {
    var copyCommand = false, clipboard = !!(window.navigator.clipboard && window.navigator.clipboard.writeText && window.isSecureContext);
    try {
      copyCommand = !!(d.queryCommandSupported && d.queryCommandSupported("copy"));
    } catch (error) {}
    if (!clipboard && !copyCommand && !window.clipboardData) return;
    var buttons = d.getElementsByTagName("button");
    for (var i = 0; i < buttons.length; i++) {
      (function (button) {
        var input = byId(button.getAttribute("data-copy-from") || "");
        if (!input) return;
        show(button, true);
        listen(button, "click", function () {
          var message = byId("notification-result");
          function report(copied) {
            if (!message) return;
            message.innerHTML = "";
            message.appendChild(d.createTextNode(copied ? "Copied to the clipboard." : "Copy failed. Select it and copy it instead."));
            message.className = copied ? "message success" : "message error";
          }
          function bySelecting() {
            var copied = false;
            input.focus();
            input.select();
            try {
              copied = window.clipboardData ? window.clipboardData.setData("Text", input.value) : d.execCommand("copy");
            } catch (error) {}
            button.focus();
            return copied;
          }
          if (clipboard) {
            window.navigator.clipboard.writeText(input.value).then(function () { report(true); }, function () { report(bySelecting()); });
          } else {
            report(bySelecting());
          }
        });
      })(buttons[i]);
    }
  })();

  // A notification setting's address shows only when it's the one chosen.
  (function () {
    function follow(selectId, value, ids) {
      var select = byId(selectId);
      if (!select) return;
      function update() {
        for (var i = 0; i < ids.length; i++) show(byId(ids[i]), select.value === value);
      }
      listen(select, "change", update);
      update();
    }
    follow("notification_server_mode", "self-hosted", ["notification_configured_server_url_label", "notification_configured_server_url_hint"]);
    follow("notification_access_url_mode", "custom", ["notification_custom_access_url_label", "notification_custom_access_url_hint"]);
    follow("notification_wizard_access_url_mode", "custom", ["notification_wizard_custom_access_url_label", "notification_wizard_custom_access_url_hint"]);
    follow("remote_pair_device", "manual", ["remote_pair_address_label", "remote_pair_address_hint"]);
  })();

  // While a pairing code shows, the page notices when pairing ends and reloads to say how.
  (function () {
    var status = byId("remote-pairing-status");
    if (!status || status.getAttribute("data-pairing") !== "active" || !window.XMLHttpRequest || !window.JSON) return;
    var request = new XMLHttpRequest();
    function check() {
      request.open("GET", "/api/remote-sdr?time=" + new Date().getTime(), true);
      request.onreadystatechange = function () {
        if (request.readyState !== 4) return;
        var pairing = null;
        try {
          if (request.status === 200) pairing = JSON.parse(request.responseText).sharing.pairing;
        } catch (error) {}
        if (pairing && !pairing.active) {
          window.location.reload();
          return;
        }
        setTimeout(check, 3000);
      };
      request.send(null);
    }
    status.innerHTML = "";
    status.appendChild(d.createTextNode("This page updates when a device pairs."));
    setTimeout(check, 3000);
  })();

  // The dashboard keeps itself up to date, changing only the parts that changed.
  (function () {
    var dashboard = byId("view_dashboard");
    if (!visible(dashboard) || !window.XMLHttpRequest || !window.JSON) return;
    var request = new XMLHttpRequest(), shown = {};
    function setText(element, text) {
      while (element.firstChild) element.removeChild(element.firstChild);
      element.appendChild(d.createTextNode(text));
    }
    function apply(parts) {
      var id, element;
      for (id in parts.text) {
        element = byId(id);
        if (element && shown[id] !== parts.text[id]) setText(element, parts.text[id]);
        shown[id] = parts.text[id];
      }
      for (id in parts.html) {
        element = byId(id);
        // The first time, it's what the page came with.
        if (element && shown[id] !== undefined && shown[id] !== parts.html[id]) element.innerHTML = parts.html[id];
        shown[id] = parts.html[id];
      }
      for (id in parts.hidden) {
        element = byId(id);
        if (element && visible(element) === parts.hidden[id]) show(element, !parts.hidden[id]);
      }
      // Storage goes first while it's low, and last otherwise.
      var storage = byId("dashboard_storage_section"), sections = [];
      for (element = dashboard.firstChild; element; element = element.nextSibling) {
        if (element.nodeType === 1) sections.push(element);
      }
      if (parts.storage_first && sections[0] !== storage) dashboard.insertBefore(storage, sections[0]);
      if (!parts.storage_first && sections[sections.length - 1] !== storage) dashboard.appendChild(storage);
    }
    function update() {
      request.open("GET", "/dashboard/parts?time=" + new Date().getTime(), true);
      request.onreadystatechange = function () {
        if (request.readyState !== 4) return;
        try {
          if (request.status === 200) apply(JSON.parse(request.responseText));
        } catch (error) {}
        setTimeout(update, 5000);
      };
      request.send(null);
    }
    update();
  })();

  // A new I/Q recording shows the settings for the chosen source.
  (function () {
    var choices = byId("iq_mode_choices"), spectrum = byId("iq_mode_spectrum");
    if (!choices || !spectrum) return;
    function update() {
      show(byId("iq_stream_fields"), !spectrum.checked);
      show(byId("iq_spectrum_fields"), spectrum.checked);
    }
    listen(choices, "click", update);
    update();
  })();

  // The I/Q recording timer keeps going, and where the browser can ask for it,
  // so does the rest of the recording's status.
  (function () {
    var banner = byId("iq_recording_banner");
    if (!visible(banner)) return;
    var loadedElapsed = Number(banner.getAttribute("data-elapsed-seconds")) || 0;
    var duration = Number(banner.getAttribute("data-duration-seconds")) || 0;
    var loadedAt = new Date().getTime();
    var onRecorder = visible(byId("view_iq_recorder"));
    function setText(id, text) {
      var element = byId(id);
      if (!element) return;
      while (element.firstChild) element.removeChild(element.firstChild);
      element.appendChild(d.createTextNode(text));
    }
    function pad(number) { return number < 10 ? "0" + number : String(number); }
    function formatDuration(seconds) {
      seconds = Math.max(0, Math.floor(Number(seconds) || 0));
      var hours = Math.floor(seconds / 3600), minutes = Math.floor((seconds % 3600) / 60);
      return (hours ? hours + ":" + pad(minutes) : minutes) + ":" + pad(seconds % 60);
    }
    function formatBytes(value) {
      var bytes = Number(value) || 0, units = ["B", "KB", "MB", "GB", "TB"], index = 0;
      while (bytes >= 1000 && index < units.length - 1) {
        bytes /= 1000;
        index += 1;
      }
      return index ? bytes.toFixed(1) + " " + units[index] : Math.round(bytes) + " B";
    }
    function showElapsed(elapsed) {
      setText("iq_recording_banner_elapsed", formatDuration(elapsed));
      setText("iq_elapsed", formatDuration(elapsed));
      if (duration > 0) setText("iq_auto_stop_remaining", formatDuration(duration - elapsed));
    }
    // Once it has finished, the I/Q recorder opens again to say how it went.
    function finished() {
      if (onRecorder) window.location.href = "/?view=iq_recorder";
      else show(banner, false);
    }
    var request = null;
    try {
      if (window.XMLHttpRequest && window.JSON) request = new XMLHttpRequest();
    } catch (error) {}
    if (!request) {
      setInterval(function () {
        var elapsed = loadedElapsed + (new Date().getTime() - loadedAt) / 1000;
        showElapsed(elapsed);
        if (duration > 0 && elapsed >= duration + 3) finished();
      }, 1000);
      return;
    }
    show(byId("iq_refresh_hint"), false);
    function update() {
      // The time keeps Internet Explorer from answering with what it asked for last time.
      request.open("GET", "/api/iq-recorder/status?time=" + new Date().getTime(), true);
      request.onreadystatechange = function () {
        if (request.readyState !== 4) return;
        var status = null;
        try {
          if (request.status === 200) status = JSON.parse(request.responseText);
        } catch (error) {}
        if (status && !status.active) {
          finished();
          return;
        }
        if (status) {
          showElapsed(status.elapsed_seconds);
          setText("iq_file_size", formatBytes(status.bytes_written));
          var remaining = status.storage_remaining_seconds;
          setText("iq_remaining", remaining === null || remaining === undefined ? "calculating" : formatDuration(remaining));
        }
        setTimeout(update, 1000);
      };
      request.send(null);
    }
    setTimeout(update, 1000);
  })();

  var audio = byId("eas_alert_baseline_audio");
  if (!audio || !audio.getAttribute("src")) return;
  var type = audio.getAttribute("data-audio-type") || "";
  if (!audio.canPlayType || !type || !audio.canPlayType(type)) {
    show(byId("eas_alert_player"), false);
    show(byId("eas_alert_cannot_play"), true);
    return;
  }
  var search = window.location.search;
  var autoplay = byId("eas_alert_baseline_autoplay");
  show(byId("eas_alert_baseline_autoplay_label"), true);
  autoplay.checked = /[?&]autoplay=1(&|$)/.test(search);
  var volume = /[?&]volume=([0-9]+)(&|$)/.exec(search);
  if (volume) {
    try { audio.volume = Math.min(100, Number(volume[1])) / 100; } catch (error) {}
  }
  // Another alert keeps playing, the autoplay choice and the volume.
  function carry(href, play) {
    href = href.replace(/&(autoplay|play|volume)=[^&]*/g, "");
    if (autoplay.checked) href += "&autoplay=1";
    if (play) href += "&play=1";
    if (audio.volume < 1) href += "&volume=" + Math.round(audio.volume * 100);
    return href;
  }
  function carryOnSelect(link) {
    if (!link) return;
    listen(link, "click", function () {
      link.href = carry(link.href, !audio.paused && !audio.ended);
    });
  }
  carryOnSelect(byId("eas_alert_previous_link"));
  carryOnSelect(byId("eas_alert_next_link"));
  listen(audio, "ended", function () {
    var next = byId("eas_alert_next_link");
    if (autoplay.checked && visible(next)) window.location.href = carry(next.href, true);
  });
  if (/[?&]play=1(&|$)/.test(search) && audio.play) {
    try {
      var playing = audio.play();
      if (playing && playing["catch"]) playing["catch"](function () {});
    } catch (error) {}
  }
})();
