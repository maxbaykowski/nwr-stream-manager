const controls = ["serial", "gain", "ppm_correction", "bias_tee", "gain_auto", "alias_filter_strength", "notify_sdr_failures"];
const DEFAULT_STREAM_SAMPLE_RATE = 24000;
const DEFAULT_STREAM_BITRATES = {mp3: 64, ogg: 48};
const STREAM_SERVICE_CUSTOM = "custom";
const STREAM_SERVICE_GWES = "gwes";
const STREAM_SERVICE_WEATHERUSA = "weatherusa";
const STREAM_SERVICE_NWRORG = "nwrorg";
const STREAM_SERVICE_HOSTS = {
  "ingest.wxr.gwes-cdn.net": STREAM_SERVICE_GWES,
  "radio-master.weatherusa.net": STREAM_SERVICE_WEATHERUSA,
  "wxradio.org": STREAM_SERVICE_NWRORG
};
const STREAM_SERVICE_NAMES = {
  custom: "Custom Icecast server",
  gwes: "GWES Weather Radio",
  weatherusa: "WeatherUSA",
  nwrorg: "NOAA Weather Radio Org"
};
const STREAM_SERVICE_HELP = {
  gwes: `No credentials yet? <a href="https://forms.office.com/r/MLx6hKmnCe" target="_blank" rel="noopener noreferrer">Submit your stream</a> to GWES Weather Radio.`,
  weatherusa: `No credentials yet? <a href="https://www.weatherusa.net/members/new" target="_blank" rel="noopener noreferrer">Create a WeatherUSA account</a>, then <a href="https://www.weatherusa.net/members/services/radio" target="_blank" rel="noopener noreferrer">create a stream</a>.`,
  nwrorg: `Find the mountpoint with the <a href="https://noaaweatherradio.org/N2radio-finder.php" target="_blank" rel="noopener noreferrer">Weather Radio Station Lookup Utility</a>.`
};
const SAME_TEST_ACK_STORAGE_KEY = "nwr-stream-manager:same-test-warning-ack";
const ADD_OUTPUT_TYPE_STEP = 10;
const ADD_OUTPUT_ICECAST_SERVICE_STEP = 11;
const ADD_OUTPUT_ICECAST_CODEC_STEP = 12;
const ADD_OUTPUT_ICECAST_CREDENTIALS_STEP = 13;
const ADD_OUTPUT_ICECAST_QUALITY_STEP = 14;
const ADD_OUTPUT_SOUNDCARD_DEVICE_STEP = 15;
const ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP = 16;
let applying = false;
let timer = null;
let gainValues = [];
let lastManualGain = null;
let lastControlSignature = "";
// SDR settings are shared by everyone using this machine, and with a remote SDR, by
// everyone using the host. Others' changes are shown in place as they arrive, except
// for a setting changed here that the server has not confirmed yet.
const SDR_SETTING_FOR_CONTROL = {
  serial: "serial",
  gain: "gain",
  gain_auto: "gain",
  ppm_correction: "ppm_correction",
  bias_tee: "bias_tee",
  alias_filter_strength: "alias_filter_strength",
  notify_sdr_failures: "notify_sdr_failures"
};
const pendingSdrSettingEdits = new Map();
// A reset can take nearly 20 seconds, and a remote SDR's host adds its round trip.
const SDR_RESET_REQUEST_TIMEOUT_MS = 30000;
let sdrResetRequestPending = false;
let sdrResetInProgress = false;
let lastSdrResetKey = "";
let statusRequestCount = 0;
// Status requests sent before a save finished may carry the old values.
let sdrControlsStaleThrough = 0;
let stationResults = [];
let selectedStationKey = "";
let wizardStationOverride = null;
let soundcardDevices = [];
// Why sound cards can't be used, such as missing permission, or "" when they can.
let soundcardProblem = "";
let configuredStreams = [];
let developmentIqSourcesEnabled = false;
let editingStreamId = "";
let editingOutputId = "";
let settingsStreamId = "";
let outputFormMode = "add";
let outputFormDirty = false;
let outputFormOriginalSignature = "";
let icecastOutputTableSignature = "";
let soundcardOutputTableSignature = "";
let activeStreamSnapshots = [];
let streamSignals = {};
let fallbackSignature = "";
let fallbackUpdateTimer = null;
let easSignature = "";
let easUpdateTimer = null;
let audioEffectsSignature = "";
let audioEffectsUpdateTimer = null;
let streamNotificationsSignature = "";
let streamNotificationsUpdateTimer = null;
let streamFallbackActionSignature = "";
let streamFallbackActionUpdateTimer = null;
let streamTestMode = {active: false};
let streamTestModeSignature = "";
let streamTestModeClientId = "";
let streamTestModeHeartbeatTimer = null;
let streamTestModeSignalTimer = null;
let sameTestWarningPreviousFocus = null;
const TEST_TONE_NAMES = ["sine", "square", "triangle", "sawtooth"];
let selectedTestTone = "sine";
let testTonesSignature = "";
let testTonesUpdateTimer = null;
let testModeClockOffset = 0;
let testModeTimeUsedUpShown = false;
let wizardStep = 0;
let wizardMode = "add";
let wizardDirty = false;
let icecastAuthPassed = false;
let icecastAuthSignature = "";
let wizardSoundcardOutputId = "";
let wizardSoundcardPreviewId = "";
let wizardSoundcardUpdateTimer = null;
let activeStreamsSignature = "";
let configuredStreamsSignature = "";
let lastDashboardStreamRefreshAt = 0;
let dashboardAttentionSignature = "";
let dashboardRecentAlertsSignature = "";
let storageIsCritical = false;
let easAlertStreams = [];
let easAlertStreamsSignature = "";
let easAlertListSignature = "";
let easAlertStreamId = "";
let easAlertDetailId = "";
let easAlertPage = 1;
let easAlertTotalPages = 1;
let easAlertReturnPage = 1;
let lastEasAlertRefreshAt = 0;
let easAlertDetailPageAlerts = [];
let easAlertDetailTotalPages = 1;
let easAlertDetailIsPlaying = false;
let easAlertAutoplay = false;
let easAlertDetailStream = null;
let easAlertDetailCurrent = null;
let easAlertLastRoute = {view: "eas_alerts", streamId: "", alertId: "", page: 1};
let easAlertPlaybackVolume = 100;
let easBulkOptionsSignature = "";
let easBulkServerNow = null;
let iqRecorderSignature = "";
let iqStreamOptionsSignature = "";
let iqRecordings = [];
let iqRecordingsSignature = "";
let selectedIqRecordingId = "";
let lastIqRecordingsRefreshAt = 0;
let iqTestSourcesSignature = "";
let monitorClientId = "";
let monitorStreamId = "";
let monitorPeerConnection = null;
let monitorPaused = false;
let monitorUnstableTimer = null;
let monitorStatsTimer = null;
let monitorLastPacketCount = 0;
let monitorLastPacketAt = 0;
let receiverClientId = "";
let receiverPeerConnection = null;
let receiverRemoteStream = null;
let receiverPlaying = false;
let receiverPaused = false;
let receiverChannelIndex = 3;
let receiverUnstableTimer = null;
let receiverStatsTimer = null;
let receiverLastPacketCount = 0;
let receiverLastPacketAt = 0;
let receiverPlaybackVolume = 100;
let liveAudioSocket = null;
let liveAudioMode = "";
let liveAudioCodec = "";
let liveAudioContext = null;
let liveAudioGain = null;
let liveAudioDecoder = null;
let liveAudioScheduledTime = 0;
let liveAudioScheduledSources = new Set();
let liveAudioPacketCount = 0;
let liveAudioDroppedLateFrames = 0;
let liveAudioCatchingUp = false;
let liveAudioRanOutCount = 0;
let liveAudioServerHasNoAudio = false;
let liveAudioServerGapSinceFrame = false;
let liveAudioNoAudioBannerTimer = null;
let liveAudioRecoveryFadePending = false;
let liveAudioLastScheduledSample = 0;
let liveAudioThroughputWindowStartedAt = 0;
let liveAudioThroughputBytes = 0;
let liveAudioThroughputMediaMs = 0;
let liveAudioSocketParams = null;
let liveAudioLastMessageAt = 0;
let liveAudioWatchdogTimer = null;
let liveAudioReconnecting = false;
let liveAudioReconnectStartedAt = 0;
let liveAudioReconnectTimer = null;
let liveAudioReconnectAttempt = 0;
let liveAudioReconnectBannerTimer = null;
let unloadLiveAudioStopSent = false;
let mediaSessionAnchorUrl = "";
let mediaSessionAnchorStarted = false;
let currentAccount = null;
let accountsSignature = "";
let notificationSettingsSignature = "";
const MONITOR_UNSTABLE_TIMEOUT_MS = 30000;
const MONITOR_STATS_INTERVAL_MS = 5000;
// Covers the SDR handing over 40 ms of audio at a time plus about 160 ms of
// network hiccups.
const LIVE_AUDIO_TARGET_LATENCY_SECONDS = 0.20;
// Above this much queued audio, skip quiet frames until back near the target.
// Only silence is skipped, so SAME bursts and speech play untouched; speeding
// playback up instead would distort SAME data.
const LIVE_AUDIO_QUIET_SKIP_LATENCY_SECONDS = 0.39;
const LIVE_AUDIO_CATCH_UP_DONE_SECONDS = LIVE_AUDIO_TARGET_LATENCY_SECONDS + 0.02;
const LIVE_AUDIO_QUIET_FRAME_RMS = 0.015;
// Above this, skip frames even if they are not quiet. Running dry and restarting
// adds the target on top of the backlog that follows, so this leaves room for the
// backlog of a stall of about 800 ms before anything audible is cut.
const LIVE_AUDIO_MAX_LATENCY_SECONDS = 1.0;
const LIVE_AUDIO_HARD_RESET_LATENCY_SECONDS = 1.5;
// A stream fills an SDR outage with its own silence within about a second, so a
// stream with fallback audio never reaches this; the receiver and streams
// without fallback audio do.
const LIVE_AUDIO_NO_AUDIO_BANNER_DELAY_MS = 2000;
const LIVE_AUDIO_RECOVERY_CROSSFADE_SECONDS = 0.004;
// The server sends audio every 20 ms, or a keepalive each second when it has none,
// so this long without anything means the connection died (the device changed
// networks, say) even if the browser has not noticed yet.
const LIVE_AUDIO_SILENT_CONNECTION_MS = 4000;
const LIVE_AUDIO_RECONNECT_FIRST_TRY_MS = 250;
const LIVE_AUDIO_CONNECT_TIMEOUT_MS = 5000;
// Reconnecting usually takes well under a second, even over cellular.
const LIVE_AUDIO_RECONNECT_CONNECT_TIMEOUT_MS = 3000;
const LIVE_AUDIO_RECONNECT_RETRY_MS = 1000;
const LIVE_AUDIO_RECONNECT_BANNER_DELAY_MS = 2000;
const LIVE_AUDIO_THROUGHPUT_REPORT_INTERVAL_MS = 2000;
const LIVE_AUDIO_BINARY_HEADER_BYTES = 16;
const LIVE_AUDIO_BINARY_MAGIC = "NWA1";
const LIVE_AUDIO_CODEC_OPUS = 1;
const LIVE_AUDIO_CODEC_PCM_S16LE = 2;
const MEDIA_SESSION_ANCHOR_SECONDS = 8;
const MEDIA_SESSION_ANCHOR_SAMPLE_RATE = 8000;
const IQ_RECORDER_SAMPLE_RATES = [192000, 256000, 384000, 512000, 768000, 1024000, 1536000];
const IQ_RECORDER_DEFAULT_SAMPLE_RATE = 192000;
const IQ_RECORDER_DEFAULT_DURATION_MINUTES = 0;
const IQ_RECORDER_PREFS_KEY = "nwr-stream-manager:iq-recorder-preferences";
const NWR_RECEIVER_CHANNELS = [
  {frequency_hz: 162400000, label: "162.400 MHz"},
  {frequency_hz: 162425000, label: "162.425 MHz"},
  {frequency_hz: 162450000, label: "162.450 MHz"},
  {frequency_hz: 162475000, label: "162.475 MHz"},
  {frequency_hz: 162500000, label: "162.500 MHz"},
  {frequency_hz: 162525000, label: "162.525 MHz"},
  {frequency_hz: 162550000, label: "162.550 MHz"}
];
const EAS_ALERTS_PER_PAGE = 25;
const PROTECTED_AUDIO_BANDS = [
  {min: 900, max: 1100},
  {min: 1400, max: 1600},
  {min: 2000, max: 2200}
];
// Attention tone, SAME space tone, SAME mark tone; matches config.PROTECTED_TONES_HZ.
const PROTECTED_AUDIO_TONES = [1050, 1562.5, 2083.3];
const PROTECTED_TONE_MAX_LOSS_DB = 1.0;
const NOTCH_MIN_WIDTH_HZ = 60;
const NOTCH_MAX_WIDTH_HZ = 2000;
let sameAlertFlashTimer = null;
let sameAlertLastAnnouncementId = "";
let sameAlertLiveRegionTimer = null;

function concatenateFloatAudio(parts) {
  const total = parts.reduce((sum, part) => sum + part.length, 0);
  const output = new Float32Array(total);
  let offset = 0;
  for (const part of parts) {
    output.set(part, offset);
    offset += part.length;
  }
  return output;
}

function shouldAnnounceReceiverSameAlert() {
  return Boolean(receiverPeerConnection && receiverPlaying && !receiverPaused && !monitorStreamId);
}

function articleForPhrase(phrase) {
  const text = String(phrase || "").trim();
  if (!text) return "An";
  return /^[aeiou]/i.test(text) ? "An" : "A";
}

function sameAlertDisplayData(event) {
  const payload = event && event.payload && typeof event.payload === "object" ? event.payload : {};
  const display = payload.display && typeof payload.display === "object" ? payload.display : {};
  const parsed = payload.parsed && typeof payload.parsed === "object" ? payload.parsed : {};
  const eventName = String(display.event_name || parsed.event_type || "EAS alert").trim();
  const areas = Array.isArray(display.areas) ? display.areas.map(area => String(area).trim()).filter(Boolean) : [];
  const areaText = areas.length ? areas.join(", ") : "an unknown area";
  const expiresAt = String(display.expires_at || "").trim();
  const expiresText = expiresAt ? `, effective until ${expiresAt}` : "";
  return {
    eventName,
    fullText: `${articleForPhrase(eventName)} ${eventName} has been issued for ${areaText}${expiresText}.`
  };
}

function shouldUseCompactSameAlertFlash() {
  const width = Math.min(window.innerWidth || 0, document.documentElement.clientWidth || window.innerWidth || 0);
  const height = Math.min(window.innerHeight || 0, document.documentElement.clientHeight || window.innerHeight || 0);
  if (width >= 700) return false;
  if (width >= 640 && width > height) return false;
  return true;
}

function renderSameAlertFlashText(element, displayData) {
  element.textContent = "";
  if (!shouldUseCompactSameAlertFlash()) {
    element.textContent = displayData.fullText;
    return;
  }
  const visualText = document.createElement("span");
  visualText.textContent = displayData.eventName;
  element.append(visualText);
}

function hideSameAlertFlash() {
  if (sameAlertFlashTimer) {
    clearTimeout(sameAlertFlashTimer);
    sameAlertFlashTimer = null;
  }
  const element = document.getElementById("same_alert_flash");
  if (!element) return;
  element.hidden = true;
}

function announceSameAlertForScreenReaders(text) {
  const liveRegion = document.getElementById("same_alert_live");
  if (!liveRegion) return;
  if (sameAlertLiveRegionTimer) {
    clearTimeout(sameAlertLiveRegionTimer);
    sameAlertLiveRegionTimer = null;
  }
  liveRegion.replaceChildren();
  window.setTimeout(() => {
    const alert = document.createElement("div");
    alert.setAttribute("role", "alert");
    alert.setAttribute("aria-atomic", "true");
    alert.textContent = text;
    liveRegion.append(alert);
  }, 50);
  sameAlertLiveRegionTimer = setTimeout(() => {
    liveRegion.replaceChildren();
    sameAlertLiveRegionTimer = null;
  }, 2500);
}

function flashReceiverSameAlert(event) {
  if (!shouldAnnounceReceiverSameAlert()) {
    logClientEvent("info", "same", "skipped receiver SAME alert announcement", {
      receiver_peer: Boolean(receiverPeerConnection),
      receiver_playing: Boolean(receiverPlaying),
      receiver_paused: Boolean(receiverPaused),
      monitor_stream_id: monitorStreamId || ""
    });
    return;
  }
  const eventId = String(event && event.id || "");
  if (eventId && eventId === sameAlertLastAnnouncementId) return;
  sameAlertLastAnnouncementId = eventId;
  const element = document.getElementById("same_alert_flash");
  if (!element) return;
  const displayData = sameAlertDisplayData(event);
  announceSameAlertForScreenReaders(displayData.fullText);
  if (sameAlertFlashTimer) clearTimeout(sameAlertFlashTimer);
  element.textContent = "";
  element.hidden = false;
  window.setTimeout(() => {
    renderSameAlertFlashText(element, displayData);
  }, 20);
  sameAlertFlashTimer = setTimeout(hideSameAlertFlash, 5000);
  logClientEvent("info", "same", "receiver SAME alert announcement shown", {
    id: eventId,
    event_name: displayData.eventName,
    compact: shouldUseCompactSameAlertFlash()
  });
}

function handleLiveSameEvent(event) {
  // The weather radio receiver shows alerts as they are decoded; the SAME tones
  // themselves arrive untouched in the audio.
  if (!event || typeof event !== "object" || event.type !== "same_alert_confirmed") return;
  logClientEvent("info", "same", "received confirmed live SAME alert event", {id: event.id});
  flashReceiverSameAlert(event);
}

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function statusRequestPath() {
  if (!wizardSoundcardPreviewId) return "/api/status";
  return `/api/status?soundcard_preview_id=${encodeURIComponent(wizardSoundcardPreviewId)}`;
}

async function requestWithTimeout(path, options = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await request(path, {...options, signal: controller.signal});
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new Error("The RTL-SDR reset did not finish. Physically unplug and replug the dongle if it does not recover.");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function logClientEvent(level, area, message, details = {}) {
  fetch("/api/client-log", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({level, area, message, details})
  }).catch(() => {});
}

function mediaSessionAnchorDiagnostics() {
  const anchor = document.getElementById("media_session_anchor_audio");
  return {
    media_session_anchor_supported: shouldUseMediaSessionAnchor(),
    anchor_present: Boolean(anchor),
    anchor_started: mediaSessionAnchorStarted,
    anchor_paused: Boolean(anchor && anchor.paused),
    anchor_muted: Boolean(anchor && anchor.muted),
    anchor_volume: anchor ? anchor.volume : null,
    anchor_ready_state: anchor ? anchor.readyState : null,
    anchor_network_state: anchor ? anchor.networkState : null,
    anchor_current_time: anchor ? anchor.currentTime : null,
    anchor_has_src: Boolean(anchor && anchor.currentSrc)
  };
}

function prepareLiveAudioElement(audio) {
  if (!audio) return;
  audio.hidden = false;
  audio.classList.add("media-session-audio");
  audio.setAttribute("aria-hidden", "true");
  audio.setAttribute("tabindex", "-1");
  audio.muted = false;
  audio.volume = 1;
}

function playbackVolumeScalar(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 1;
  return Math.min(100, Math.max(0, numeric)) / 100;
}

function applyReceiverPlaybackVolume() {
  const audio = document.getElementById("stream_monitor_audio");
  if (!audio || !receiverPeerConnection) return;
  audio.volume = playbackVolumeScalar(receiverPlaybackVolume);
}

function applyEasAlertPlaybackVolume() {
  const audio = document.getElementById("eas_alert_audio");
  if (!audio) return;
  audio.volume = playbackVolumeScalar(easAlertPlaybackVolume);
}

function shouldUseMediaSessionAnchor() {
  return "mediaSession" in navigator;
}

function createMediaSessionAnchorUrl() {
  if (mediaSessionAnchorUrl) return mediaSessionAnchorUrl;
  const samples = MEDIA_SESSION_ANCHOR_SECONDS * MEDIA_SESSION_ANCHOR_SAMPLE_RATE;
  const dataBytes = samples * 2;
  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);
  const writeText = (offset, text) => {
    for (let index = 0; index < text.length; index += 1) {
      view.setUint8(offset + index, text.charCodeAt(index));
    }
  };
  writeText(0, "RIFF");
  view.setUint32(4, 36 + dataBytes, true);
  writeText(8, "WAVE");
  writeText(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, MEDIA_SESSION_ANCHOR_SAMPLE_RATE, true);
  view.setUint32(28, MEDIA_SESSION_ANCHOR_SAMPLE_RATE * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeText(36, "data");
  view.setUint32(40, dataBytes, true);
  for (let index = 0; index < samples; index += 1) {
    const sample = index % 2 === 0 ? 1 : -1;
    view.setInt16(44 + index * 2, sample, true);
  }
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let index = 0; index < bytes.length; index += 1) {
    binary += String.fromCharCode(bytes[index]);
  }
  mediaSessionAnchorUrl = `data:audio/wav;base64,${btoa(binary)}`;
  return mediaSessionAnchorUrl;
}

function prepareMediaSessionAnchor() {
  const anchor = document.getElementById("media_session_anchor_audio");
  if (!anchor || !shouldUseMediaSessionAnchor()) return null;
  anchor.hidden = false;
  anchor.classList.add("media-session-audio");
  anchor.setAttribute("aria-hidden", "true");
  anchor.setAttribute("tabindex", "-1");
  anchor.loop = true;
  anchor.muted = false;
  anchor.volume = 1;
  if (!anchor.src) anchor.src = createMediaSessionAnchorUrl();
  return anchor;
}

async function startMediaSessionAnchor(reason = "") {
  const anchor = prepareMediaSessionAnchor();
  if (!anchor) return false;
  try {
    await anchor.play();
    mediaSessionAnchorStarted = true;
    logClientEvent("info", "media-session", "media session anchor started", {
      reason,
      ...mediaSessionAnchorDiagnostics()
    });
    return true;
  } catch (error) {
    mediaSessionAnchorStarted = false;
    logClientEvent("warning", "media-session", "media session anchor playback failed", {
      reason,
      error: error.message,
      ...mediaSessionAnchorDiagnostics()
    });
    return false;
  }
}

function pauseMediaSessionAnchor(reason = "") {
  const anchor = document.getElementById("media_session_anchor_audio");
  if (!anchor || !mediaSessionAnchorStarted) return;
  try {
    anchor.pause();
    logClientEvent("info", "media-session", "media session anchor paused", {
      reason,
      ...mediaSessionAnchorDiagnostics()
    });
  } catch (error) {
    console.debug("media session anchor pause failed", error);
  }
}

function stopMediaSessionAnchor(reason = "") {
  const anchor = document.getElementById("media_session_anchor_audio");
  if (!anchor) return;
  try {
    anchor.pause();
    anchor.currentTime = 0;
    mediaSessionAnchorStarted = false;
    logClientEvent("info", "media-session", "media session anchor stopped", {
      reason,
      ...mediaSessionAnchorDiagnostics()
    });
  } catch (error) {
    console.debug("media session anchor stop failed", error);
  }
}

function monitoredStreamLabel() {
  const stream = configuredStreams.find(candidate => candidate.id === monitorStreamId);
  if (!stream) return "Monitor of stream";
  const station = stream.station || {};
  return `Monitor of ${station.callsign || "stream"}`;
}

function clearLiveMediaSession() {
  if (!("mediaSession" in navigator)) return;
  try {
    for (const action of ["previoustrack", "nexttrack", "play", "pause", "seekto", "seekbackward", "seekforward"]) {
      navigator.mediaSession.setActionHandler(action, null);
    }
    navigator.mediaSession.metadata = null;
    navigator.mediaSession.playbackState = "none";
  } catch (error) {
    console.debug("media session cleanup failed", error);
  }
}

function formatDecimalBytes(value) {
  let bytes = Number(value || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  while (bytes >= 1000 && index < units.length - 1) {
    bytes /= 1000;
    index += 1;
  }
  return index === 0 ? `${Math.round(bytes)} ${units[index]}` : `${bytes.toFixed(1)} ${units[index]}`;
}

function formatDuration(seconds) {
  seconds = Math.max(0, Math.floor(Number(seconds || 0)));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remaining = seconds % 60;
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${String(remaining).padStart(2, "0")}`;
  return `${minutes}:${String(remaining).padStart(2, "0")}`;
}

function supportsWebSocketLiveAudio() {
  return "WebSocket" in window && Boolean(window.AudioContext || window.webkitAudioContext);
}

async function supportsWebCodecsOpusAudio() {
  if (!("AudioDecoder" in window) || !("EncodedAudioChunk" in window) || typeof AudioDecoder.isConfigSupported !== "function") return false;
  try {
    const result = await AudioDecoder.isConfigSupported({
      codec: "opus",
      sampleRate: 48000,
      numberOfChannels: 1
    });
    return Boolean(result && result.supported);
  } catch (error) {
    return false;
  }
}

function liveAudioUrl(params) {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  const query = new URLSearchParams(params);
  return `${scheme}//${window.location.host}/api/live-audio?${query.toString()}`;
}

async function ensureLiveAudioContext() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) throw new Error("This browser does not support Web Audio playback.");
  if (!liveAudioContext) {
    liveAudioContext = new AudioContextClass();
    liveAudioGain = liveAudioContext.createGain();
    liveAudioGain.connect(liveAudioContext.destination);
  }
  if (liveAudioContext.state === "suspended") await liveAudioContext.resume();
  return liveAudioContext;
}

function closeLiveAudioDecoder() {
  if (!liveAudioDecoder) return;
  try {
    liveAudioDecoder.close();
  } catch (error) {
    console.debug("live audio decoder close failed", error);
  }
  liveAudioDecoder = null;
}

function stopScheduledLiveAudioSources() {
  for (const source of Array.from(liveAudioScheduledSources)) {
    try {
      source.stop();
    } catch (error) {
      console.debug("scheduled live audio stop failed", error);
    }
  }
  liveAudioScheduledSources.clear();
  liveAudioRecoveryFadePending = false;
  liveAudioCatchingUp = false;
  liveAudioLastScheduledSample = 0;
  // The next frame starts a fresh schedule.
  liveAudioScheduledTime = 0;
}

function applyLiveAudioVolume() {
  if (!liveAudioGain || !liveAudioContext) return;
  const volume = liveAudioMode === "receiver" ? playbackVolumeScalar(receiverPlaybackVolume) : 1;
  try {
    liveAudioGain.gain.setTargetAtTime(volume, liveAudioContext.currentTime, 0.01);
  } catch (error) {
    liveAudioGain.gain.value = volume;
  }
}

function bytesFromBase64(value) {
  const binary = atob(String(value || ""));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function pcmS16BytesToFloat32(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const samples = new Float32Array(Math.floor(bytes.byteLength / 2));
  for (let index = 0; index < samples.length; index += 1) {
    samples[index] = Math.max(-1, Math.min(1, view.getInt16(index * 2, true) / 32768));
  }
  return samples;
}

function liveAudioBinaryMagic(view) {
  if (!view || view.byteLength < 4) return "";
  return String.fromCharCode(
    view.getUint8(0),
    view.getUint8(1),
    view.getUint8(2),
    view.getUint8(3)
  );
}

async function liveAudioMessageBytes(data) {
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  if (data instanceof Blob) return new Uint8Array(await data.arrayBuffer());
  return null;
}

function audioDataToFloat32(audioData) {
  const frames = audioData.numberOfFrames || 0;
  const channels = audioData.numberOfChannels || 1;
  const format = String(audioData.format || "");
  const sampleRate = audioData.sampleRate || 48000;
  const samples = new Float32Array(frames);
  try {
    if (format.includes("f32")) {
      const raw = new Float32Array(frames * channels);
      audioData.copyTo(raw, {planeIndex: 0});
      for (let index = 0; index < frames; index += 1) samples[index] = raw[index * channels] || 0;
    } else {
      const raw = new Int16Array(frames * channels);
      audioData.copyTo(raw, {planeIndex: 0});
      for (let index = 0; index < frames; index += 1) samples[index] = Math.max(-1, Math.min(1, (raw[index * channels] || 0) / 32768));
    }
  } finally {
    audioData.close();
  }
  return {samples, sampleRate};
}

function dropLiveAudioFrameForLatency(scheduledLead, reason = "latency") {
  liveAudioDroppedLateFrames += 1;
  liveAudioRecoveryFadePending = true;
  if (liveAudioDroppedLateFrames === 1 || liveAudioDroppedLateFrames % 50 === 0) {
    logClientEvent("warning", "live-audio", "dropped live audio frame to reduce playback latency", {
      mode: liveAudioMode,
      client_id: liveAudioClientIdForCurrentMode(),
      dropped_late_frames: liveAudioDroppedLateFrames,
      scheduled_lead_seconds: Number(scheduledLead.toFixed(3)),
      reason
    });
  }
}

function liveAudioClientIdForCurrentMode() {
  if (liveAudioMode === "receiver") return pageReceiverClientId();
  if (liveAudioMode === "monitor") return pageMonitorClientId();
  return "";
}

// A new connection's first window starts with its first audio, not when it opens:
// counting the time spent connecting would make every start look like a slow link.
function resetLiveAudioThroughputWindow(startedAt = performance.now()) {
  liveAudioThroughputWindowStartedAt = startedAt;
  liveAudioThroughputBytes = 0;
  liveAudioThroughputMediaMs = 0;
}

function recordLiveAudioThroughput(byteLength, durationMs) {
  const now = performance.now();
  if (!liveAudioThroughputWindowStartedAt) {
    resetLiveAudioThroughputWindow();
  }
  liveAudioThroughputBytes += Math.max(0, Number(byteLength || 0));
  liveAudioThroughputMediaMs += Math.max(0, Number(durationMs || 0));
  const elapsedMs = now - liveAudioThroughputWindowStartedAt;
  if (elapsedMs < LIVE_AUDIO_THROUGHPUT_REPORT_INTERVAL_MS) return;
  const receiveKbps = (liveAudioThroughputBytes * 8) / Math.max(1, elapsedMs);
  const mediaDeliveryRatio = liveAudioThroughputMediaMs / Math.max(1, elapsedMs);
  logClientEvent("info", "live-audio", "live audio throughput sample", {
    mode: liveAudioMode,
    client_id: liveAudioClientIdForCurrentMode(),
    codec: liveAudioCodec,
    received_bytes: liveAudioThroughputBytes,
    elapsed_ms: Math.round(elapsedMs),
    media_ms: Math.round(liveAudioThroughputMediaMs),
    receive_kbps: Number(receiveKbps.toFixed(1)),
    media_delivery_ratio: Number(mediaDeliveryRatio.toFixed(3))
  });
  resetLiveAudioThroughputWindow();
}

function applyLiveAudioRecoveryCrossfade(samples, sampleRate) {
  if (!liveAudioRecoveryFadePending || !samples || !samples.length) return samples;
  liveAudioRecoveryFadePending = false;
  const fadeSamples = Math.min(
    samples.length,
    Math.max(1, Math.round(Number(sampleRate || 1) * LIVE_AUDIO_RECOVERY_CROSSFADE_SECONDS))
  );
  const output = new Float32Array(samples);
  const startSample = Number.isFinite(liveAudioLastScheduledSample) ? liveAudioLastScheduledSample : 0;
  for (let index = 0; index < fadeSamples; index += 1) {
    const mix = (index + 1) / fadeSamples;
    output[index] = startSample * (1 - mix) + samples[index] * mix;
  }
  return output;
}

function liveAudioFrameIsQuiet(samples) {
  let sum = 0;
  for (let index = 0; index < samples.length; index += 1) sum += samples[index] * samples[index];
  return Math.sqrt(sum / Math.max(1, samples.length)) < LIVE_AUDIO_QUIET_FRAME_RMS;
}

function scheduleLiveAudioSamples(samples, sampleRate) {
  if (!samples || !samples.length || !liveAudioContext) return;
  const now = liveAudioContext.currentTime;
  const serverGap = liveAudioServerGapSinceFrame;
  liveAudioServerGapSinceFrame = false;
  if (!liveAudioScheduledTime || liveAudioScheduledTime <= now) {
    // Only start over once the queue has truly run dry: while queued audio is still
    // playing, the next frame goes right after it, so a late frame never opens a gap.
    // Running dry because the server itself had no audio is logged when it says so.
    if (liveAudioScheduledTime && !serverGap) {
      liveAudioRanOutCount += 1;
      logClientEvent("warning", "live-audio", "live audio ran out before more arrived", {
        mode: liveAudioMode,
        client_id: liveAudioClientIdForCurrentMode(),
        ran_out_count: liveAudioRanOutCount,
        late_seconds: Number((now - liveAudioScheduledTime).toFixed(3))
      });
    }
    liveAudioScheduledTime = now + LIVE_AUDIO_TARGET_LATENCY_SECONDS;
  }
  const scheduledLead = liveAudioScheduledTime - now;
  if (scheduledLead > LIVE_AUDIO_HARD_RESET_LATENCY_SECONDS) {
    stopScheduledLiveAudioSources();
    liveAudioScheduledTime = now + LIVE_AUDIO_TARGET_LATENCY_SECONDS;
  } else if (scheduledLead > LIVE_AUDIO_MAX_LATENCY_SECONDS) {
    liveAudioCatchingUp = true;
    dropLiveAudioFrameForLatency(scheduledLead, "above-max");
    return;
  } else if (scheduledLead > LIVE_AUDIO_QUIET_SKIP_LATENCY_SECONDS) {
    liveAudioCatchingUp = true;
  } else if (scheduledLead <= LIVE_AUDIO_CATCH_UP_DONE_SECONDS) {
    liveAudioCatchingUp = false;
    liveAudioDroppedLateFrames = 0;
  }
  if (liveAudioCatchingUp && liveAudioFrameIsQuiet(samples)) {
    dropLiveAudioFrameForLatency(scheduledLead, "quiet");
    return;
  }
  samples = applyLiveAudioRecoveryCrossfade(samples, sampleRate);
  liveAudioLastScheduledSample = samples.length ? samples[samples.length - 1] : liveAudioLastScheduledSample;
  const buffer = liveAudioContext.createBuffer(1, samples.length, sampleRate);
  buffer.copyToChannel(samples, 0);
  const source = liveAudioContext.createBufferSource();
  source.buffer = buffer;
  source.connect(liveAudioGain || liveAudioContext.destination);
  liveAudioScheduledSources.add(source);
  source.addEventListener("ended", () => liveAudioScheduledSources.delete(source));
  source.start(liveAudioScheduledTime);
  liveAudioScheduledTime += samples.length / sampleRate;
}

function setListeningNoAudioBanner(show) {
  const banner = document.getElementById("listening_no_audio_banner");
  if (!banner || banner.hidden === !show) return;
  banner.hidden = !show;
  const live = document.getElementById("listening_status_live");
  if (live) {
    live.textContent = show
      ? "No audio is coming from the SDR right now. Listening will continue when it returns."
      : "Audio from the SDR is back.";
  }
}

function resetLiveAudioServerAudioState() {
  if (liveAudioNoAudioBannerTimer) {
    clearTimeout(liveAudioNoAudioBannerTimer);
    liveAudioNoAudioBannerTimer = null;
  }
  liveAudioServerHasNoAudio = false;
  liveAudioServerGapSinceFrame = false;
  const banner = document.getElementById("listening_no_audio_banner");
  if (banner) banner.hidden = true;
  const live = document.getElementById("listening_status_live");
  if (live) live.textContent = "";
}

// The server ran out of audio to send: its SDR stopped, or a remote SDR fell behind.
// Nothing about this listener's connection is wrong, so it is not treated as one.
function handleLiveAudioServerNoAudio() {
  liveAudioServerHasNoAudio = true;
  liveAudioServerGapSinceFrame = true;
  logClientEvent("info", "live-audio", "server had no audio to send", {
    mode: liveAudioMode,
    client_id: liveAudioClientIdForCurrentMode()
  });
  if (!liveAudioNoAudioBannerTimer) {
    liveAudioNoAudioBannerTimer = setTimeout(() => {
      liveAudioNoAudioBannerTimer = null;
      if (liveAudioServerHasNoAudio) setListeningNoAudioBanner(true);
    }, LIVE_AUDIO_NO_AUDIO_BANNER_DELAY_MS);
  }
}

function handleLiveAudioServerAudioBack(seconds) {
  liveAudioServerHasNoAudio = false;
  if (liveAudioNoAudioBannerTimer) {
    clearTimeout(liveAudioNoAudioBannerTimer);
    liveAudioNoAudioBannerTimer = null;
  }
  setListeningNoAudioBanner(false);
  logClientEvent("info", "live-audio", "server audio is back", {
    mode: liveAudioMode,
    client_id: liveAudioClientIdForCurrentMode(),
    seconds_without_audio: Number(seconds || 0)
  });
}

async function configureLiveAudioDecoder() {
  closeLiveAudioDecoder();
  if (liveAudioCodec !== "opus") return;
  liveAudioDecoder = new AudioDecoder({
    output: audioData => {
      const decoded = audioDataToFloat32(audioData);
      scheduleLiveAudioSamples(decoded.samples, decoded.sampleRate);
    },
    error: error => {
      logClientEvent("warning", "live-audio", "Opus decode failed", {error: error.message});
    }
  });
  liveAudioDecoder.configure({codec: "opus", sampleRate: 48000, numberOfChannels: 1});
}

async function handleLiveAudioMessage(event) {
  const binary = await liveAudioMessageBytes(event.data);
  if (binary) {
    if (binary.byteLength < LIVE_AUDIO_BINARY_HEADER_BYTES) return;
    const view = new DataView(binary.buffer, binary.byteOffset, binary.byteLength);
    if (liveAudioBinaryMagic(view) !== LIVE_AUDIO_BINARY_MAGIC) return;
    const codecId = view.getUint8(4);
    const durationMs = view.getUint16(6, false) || 20;
    const sequence = view.getUint32(8, false);
    const sampleRate = view.getUint32(12, false) || 24000;
    const payload = binary.slice(LIVE_AUDIO_BINARY_HEADER_BYTES);
    liveAudioPacketCount += 1;
    recordLiveAudioThroughput(binary.byteLength, durationMs);
    clearMonitorUnstableTimer();
    clearReceiverUnstableTimer();
    if (codecId === LIVE_AUDIO_CODEC_OPUS && liveAudioDecoder) {
      liveAudioDecoder.decode(new EncodedAudioChunk({
        type: "key",
        timestamp: Math.round(sequence * durationMs * 1000),
        duration: Math.round(durationMs * 1000),
        data: payload
      }));
    } else if (codecId === LIVE_AUDIO_CODEC_PCM_S16LE) {
      scheduleLiveAudioSamples(pcmS16BytesToFloat32(payload), sampleRate);
    }
    return;
  }
  if (typeof event.data !== "string") return;
  const message = JSON.parse(event.data);
  if (message.type === "server_shutdown") {
    enterServerUnavailable("shutdown");
    return;
  }
  if (message.type === "server_restarting") {
    enterServerRestarting(message.instance);
    return;
  }
  if (message.type === "start") {
    liveAudioCodec = message.codec || "pcm";
    await configureLiveAudioDecoder();
    return;
  }
  if (message.type === "same_event") {
    handleLiveSameEvent(message.event || {});
    return;
  }
  if (message.type === "source_no_audio") {
    handleLiveAudioServerNoAudio();
    return;
  }
  if (message.type === "source_audio_back") {
    handleLiveAudioServerAudioBack(message.seconds);
    return;
  }
  if (message.type !== "audio") return;
  liveAudioPacketCount += 1;
  const bytes = bytesFromBase64(message.data);
  recordLiveAudioThroughput(bytes.byteLength, Number(message.duration_ms || 20));
  clearMonitorUnstableTimer();
  clearReceiverUnstableTimer();
  if (message.codec === "opus" && liveAudioDecoder) {
    liveAudioDecoder.decode(new EncodedAudioChunk({
      type: "key",
      timestamp: Math.round(Number(message.timestamp_ms || 0) * 1000),
      duration: Math.round(Number(message.duration_ms || 20) * 1000),
      data: bytes
    }));
  } else {
    scheduleLiveAudioSamples(pcmS16BytesToFloat32(bytes), Number(message.sample_rate || 24000));
  }
}

function closeLiveAudioSocket(expectedSocket = null) {
  const socket = expectedSocket || liveAudioSocket;
  const closingCurrent = !expectedSocket || expectedSocket === liveAudioSocket;
  if (closingCurrent) {
    liveAudioSocket = null;
    liveAudioMode = "";
    closeLiveAudioDecoder();
    stopScheduledLiveAudioSources();
    resetLiveAudioThroughputWindow(0);
    resetLiveAudioServerAudioState();
  }
  if (!socket) return;
  try {
    socket.onclose = null;
    socket.close();
  } catch (error) {
    console.debug("live audio socket close failed", error);
  }
}

function liveAudioSocketPeer(mode, socket) {
  return {
    transport: "websocket",
    get connectionState() {
      return socket && socket.readyState === WebSocket.OPEN ? "connected" : "closed";
    },
    get iceConnectionState() {
      return socket && socket.readyState === WebSocket.OPEN ? "connected" : "closed";
    },
    socket,
    getStats() {
      const packetsReceived = socket && socket === liveAudioSocket ? liveAudioPacketCount : 0;
      return Promise.resolve(new Map([["live-audio", {type: "inbound-rtp", kind: "audio", packetsReceived}]]));
    },
    close() {
      closeLiveAudioSocket(socket);
    },
    mode
  };
}

function liveAudioPeerUsesWebSocket(peer) {
  return Boolean(peer && peer.transport === "websocket");
}

function liveAudioPeerSocketIsClosed(peer) {
  return liveAudioPeerUsesWebSocket(peer) && (!peer.socket || peer.socket.readyState >= WebSocket.CLOSING);
}

function isInactiveLiveAudioSessionError(error) {
  return /client session is not active|receiver is not playing|monitor is not active/i.test(String(error && error.message || error || ""));
}

function setListeningReconnectingBanner(show) {
  const banner = document.getElementById("listening_reconnecting_banner");
  if (!banner || banner.hidden === !show) return;
  banner.hidden = !show;
  const live = document.getElementById("listening_status_live");
  if (live) {
    live.textContent = show
      ? "Lost the connection to NWR Stream Manager. Listening will continue when it's back."
      : "Reconnected to NWR Stream Manager.";
  }
}

function liveAudioConnectionHealthy() {
  return Boolean(
    liveAudioSocket &&
    liveAudioSocket.readyState === WebSocket.OPEN &&
    performance.now() - liveAudioLastMessageAt < LIVE_AUDIO_SILENT_CONNECTION_MS
  );
}

function startLiveAudioWatchdog() {
  if (liveAudioWatchdogTimer) return;
  liveAudioWatchdogTimer = setInterval(() => {
    const socket = liveAudioSocket;
    if (!socket) return;
    if (socket.readyState === WebSocket.OPEN && performance.now() - liveAudioLastMessageAt >= LIVE_AUDIO_SILENT_CONNECTION_MS) {
      liveAudioConnectionLost(socket, "nothing received");
    }
  }, 1000);
}

// Switching between Wi-Fi and cellular drops the connection; listening picks up again
// on its own once the device is back online instead of stopping.
function liveAudioConnectionLost(socket, reason) {
  if (socket !== liveAudioSocket || !liveAudioSocketParams || serverUnavailable) return;
  liveAudioSocket = null;
  try {
    socket.close();
  } catch (error) {
    console.debug("live audio socket close failed", error);
  }
  if (liveAudioReconnecting) return;
  const mode = liveAudioSocketParams.mode;
  liveAudioReconnecting = true;
  liveAudioReconnectStartedAt = performance.now();
  logClientEvent("info", "live-audio", "live audio connection lost; reconnecting", {mode, client_id: liveAudioSocketParams.client_id, reason});
  // Gives up, as before, if it cannot get back in time.
  if (mode === "monitor") scheduleMonitorUnstableStop("live audio connection lost");
  if (mode === "receiver") scheduleReceiverUnstableStop("live audio connection lost");
  liveAudioReconnectBannerTimer = setTimeout(() => {
    liveAudioReconnectBannerTimer = null;
    if (liveAudioReconnecting) setListeningReconnectingBanner(true);
  }, LIVE_AUDIO_RECONNECT_BANNER_DELAY_MS);
  liveAudioReconnectTimer = setTimeout(attemptLiveAudioReconnect, LIVE_AUDIO_RECONNECT_FIRST_TRY_MS);
}

// What this page is listening to now, which may have changed since listening started
// (a new receiver channel, say), so a reconnect picks up where the listener is.
function currentListeningParams(mode) {
  if (mode === "monitor") return {mode, client_id: pageMonitorClientId(), stream_id: monitorStreamId};
  return {mode, client_id: pageReceiverClientId(), frequency_hz: currentReceiverChannel().frequency_hz};
}

async function attemptLiveAudioReconnect() {
  liveAudioReconnectTimer = null;
  if (!liveAudioReconnecting) return;
  const params = currentListeningParams(liveAudioSocketParams.mode);
  const attempt = ++liveAudioReconnectAttempt;
  let socket;
  try {
    socket = await openLiveAudioSocket(params, {reconnecting: true});
  } catch (error) {
    // A newer attempt (started when the device came back online) takes over.
    if (liveAudioReconnecting && attempt === liveAudioReconnectAttempt) {
      liveAudioReconnectTimer = setTimeout(attemptLiveAudioReconnect, LIVE_AUDIO_RECONNECT_RETRY_MS);
    }
    return;
  }
  if (!liveAudioReconnecting) {
    // Listening was stopped while this was connecting.
    closeLiveAudioSocket(socket);
    return;
  }
  const seconds = (performance.now() - liveAudioReconnectStartedAt) / 1000;
  liveAudioReconnecting = false;
  if (liveAudioReconnectBannerTimer) {
    clearTimeout(liveAudioReconnectBannerTimer);
    liveAudioReconnectBannerTimer = null;
  }
  setListeningReconnectingBanner(false);
  logClientEvent("info", "live-audio", "live audio reconnected", {mode: params.mode, client_id: params.client_id, seconds: Number(seconds.toFixed(1))});
  if (params.mode === "monitor" && monitorPeerConnection) {
    monitorPeerConnection = liveAudioSocketPeer("monitor", socket);
    if (monitorPaused) await resendLiveAudioPause("/api/monitor/pause", params.client_id);
  }
  if (params.mode === "receiver" && receiverPeerConnection) {
    receiverPeerConnection = liveAudioSocketPeer("receiver", socket);
    if (receiverPaused) await resendLiveAudioPause("/api/receiver/pause", params.client_id);
  }
}

async function resendLiveAudioPause(path, clientId) {
  // The new connection starts a new session on the server, which starts out playing.
  try {
    await request(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({client_id: clientId})
    });
  } catch (error) {
    console.debug("could not pause the reconnected session", error);
  }
}

// The browser says the device is online again, so an attempt made over the old
// network is not worth waiting out.
function retryLiveAudioReconnectNow() {
  if (!liveAudioReconnecting) return;
  if (liveAudioReconnectTimer) {
    clearTimeout(liveAudioReconnectTimer);
    liveAudioReconnectTimer = null;
  }
  attemptLiveAudioReconnect();
}

function cancelLiveAudioReconnect() {
  const wasReconnecting = liveAudioReconnecting;
  liveAudioReconnecting = false;
  if (liveAudioReconnectTimer) {
    clearTimeout(liveAudioReconnectTimer);
    liveAudioReconnectTimer = null;
  }
  if (liveAudioReconnectBannerTimer) {
    clearTimeout(liveAudioReconnectBannerTimer);
    liveAudioReconnectBannerTimer = null;
  }
  const banner = document.getElementById("listening_reconnecting_banner");
  if (banner) banner.hidden = true;
  // Stop what was left playing from the lost connection.
  if (wasReconnecting && !liveAudioSocket) closeLiveAudioSocket();
}

let insecureAudioNoticeShown = false;

function noteInsecureAudioFallback(opus) {
  // Browsers such as Firefox and Chrome only offer the Opus decoder on secure (https)
  // pages; elsewhere audio falls back to uncompressed PCM. Say so once per page load.
  if (opus || window.isSecureContext || insecureAudioNoticeShown) return;
  insecureAudioNoticeShown = true;
  setHidden("insecure_audio_banner", false);
  logClientEvent("info", "live-audio", "uncompressed audio used on an insecure page", {});
}

async function startLiveAudioSocket(params) {
  cancelLiveAudioReconnect();
  return await openLiveAudioSocket(params);
}

async function openLiveAudioSocket(params, options = {}) {
  if (options.reconnecting) {
    // Nothing here may wait: a phone can hold back resuming audio until the next tap,
    // and the codec this browser supports has not changed since listening started.
    if (liveAudioContext && liveAudioContext.state === "suspended") liveAudioContext.resume().catch(() => {});
  } else {
    await ensureLiveAudioContext();
    const opus = await supportsWebCodecsOpusAudio();
    noteInsecureAudioFallback(opus);
    liveAudioCodec = opus ? "opus" : "pcm";
  }
  closeLiveAudioSocket();
  liveAudioMode = params.mode;
  // A reconnect continues the same listening session, so its count of audio
  // received carries on; the checks that listening is still getting audio compare
  // it with what they saw before the connection was lost.
  if (!options.reconnecting) liveAudioPacketCount = 0;
  liveAudioDroppedLateFrames = 0;
  liveAudioCatchingUp = false;
  liveAudioRanOutCount = 0;
  resetLiveAudioServerAudioState();
  liveAudioRecoveryFadePending = false;
  liveAudioLastScheduledSample = 0;
  resetLiveAudioThroughputWindow(0);
  liveAudioScheduledTime = 0;
  applyLiveAudioVolume();
  const socket = new WebSocket(liveAudioUrl({...params, codec: liveAudioCodec}));
  socket.binaryType = "arraybuffer";
  liveAudioSocket = socket;
  liveAudioSocketParams = params;
  return await new Promise((resolve, reject) => {
    let opened = false;
    // Connecting while a phone is between networks can hang for minutes; give up
    // on this attempt so the next one can go out over the new network.
    const connectTimer = setTimeout(() => {
      if (opened) return;
      try {
        socket.close();
      } catch (error) {
        console.debug("live audio socket close failed", error);
      }
      if (liveAudioSocket === socket) liveAudioSocket = null;
      reject(new Error("Live audio connection timed out."));
    }, options.reconnecting ? LIVE_AUDIO_RECONNECT_CONNECT_TIMEOUT_MS : LIVE_AUDIO_CONNECT_TIMEOUT_MS);
    socket.addEventListener("open", () => {
      clearTimeout(connectTimer);
      opened = true;
      liveAudioLastMessageAt = performance.now();
      startLiveAudioWatchdog();
      resolve(socket);
    });
    socket.addEventListener("message", event => {
      if (socket === liveAudioSocket) liveAudioLastMessageAt = performance.now();
      handleLiveAudioMessage(event).catch(error => {
        logClientEvent("warning", "live-audio", "live audio message failed", {error: error.message});
      });
    });
    socket.addEventListener("close", () => {
      clearTimeout(connectTimer);
      if (opened) liveAudioConnectionLost(socket, "closed");
      else reject(new Error("Live audio connection failed."));
    });
    socket.addEventListener("error", () => {
      if (!opened) reject(new Error("Live audio connection failed."));
    });
  });
}

function pageMonitorClientId() {
  if (!monitorClientId) {
    monitorClientId = window.crypto && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  }
  return monitorClientId;
}

function liveAudioPeerIsUnusable(peer) {
  if (!peer) return false;
  if (liveAudioPeerSocketIsClosed(peer)) return true;
  return (
    ["closed", "failed", "disconnected"].includes(peer.connectionState) ||
    ["closed", "failed", "disconnected"].includes(peer.iceConnectionState)
  );
}

function resetLiveAudioElement() {
  hideSameAlertFlash();
  const audio = document.getElementById("stream_monitor_audio");
  if (!audio) return;
  prepareLiveAudioElement(audio);
  audio.muted = false;
  audio.volume = 1;
  audio.pause();
  audio.srcObject = null;
  try {
    audio.load();
  } catch (error) {
    console.debug("live audio element reset failed", error);
  }
}

function clearLocalStreamMonitor() {
  if (liveAudioSocketParams && liveAudioSocketParams.mode === "monitor") cancelLiveAudioReconnect();
  const peer = monitorPeerConnection;
  monitorPeerConnection = null;
  monitorStreamId = "";
  monitorPaused = false;
  clearMonitorWatchdogs();
  if (!receiverPeerConnection) {
    resetLiveAudioElement();
    stopMediaSessionAnchor("monitor-local-clear");
    clearLiveMediaSession();
  }
  if (peer) {
    try {
      peer.close();
    } catch (error) {
      console.debug("failed to close monitor peer", error);
    }
  }
}

function showMonitorUnstableDialog() {
  const dialog = document.getElementById("monitor_unstable_dialog");
  if (!dialog) return;
  dialog.hidden = false;
  const button = document.getElementById("dismiss_monitor_unstable");
  if (button) button.focus();
}

function dismissMonitorUnstableDialog() {
  const dialog = document.getElementById("monitor_unstable_dialog");
  if (dialog) dialog.hidden = true;
}

function clearMonitorUnstableTimer() {
  if (monitorUnstableTimer) {
    clearTimeout(monitorUnstableTimer);
    monitorUnstableTimer = null;
  }
}

function clearMonitorStatsTimer() {
  if (monitorStatsTimer) {
    clearInterval(monitorStatsTimer);
    monitorStatsTimer = null;
  }
}

function clearMonitorWatchdogs() {
  clearMonitorUnstableTimer();
  clearMonitorStatsTimer();
}

function markMonitorPacketProgress(packetCount) {
  monitorLastPacketCount = packetCount;
  monitorLastPacketAt = Date.now();
  clearMonitorUnstableTimer();
}

async function stopMonitorForUnstableConnection(reason = "") {
  if (!monitorStreamId) return;
  clearMonitorUnstableTimer();
  console.warn("monitor connection unstable", reason);
  await stopStreamMonitor({notifyServer: true, unstable: true});
}

function scheduleMonitorUnstableStop(reason = "") {
  if (!monitorStreamId || monitorPaused || monitorUnstableTimer) return;
  monitorUnstableTimer = setTimeout(async () => {
    monitorUnstableTimer = null;
    await stopMonitorForUnstableConnection(reason);
  }, MONITOR_UNSTABLE_TIMEOUT_MS);
}

async function pollMonitorPacketStats() {
  const peer = monitorPeerConnection;
  if (!peer || !monitorStreamId || monitorPaused) return;
  try {
    const stats = await peer.getStats();
    let packetCount = 0;
    stats.forEach(report => {
      if (report.type === "inbound-rtp" && (report.kind === "audio" || report.mediaType === "audio")) {
        packetCount += Number(report.packetsReceived || 0);
      }
    });
    // Waiting on the server's own audio is not an unstable connection.
    if (packetCount > monitorLastPacketCount || liveAudioServerHasNoAudio) {
      markMonitorPacketProgress(packetCount);
      return;
    }
    if (monitorLastPacketAt && Date.now() - monitorLastPacketAt >= MONITOR_UNSTABLE_TIMEOUT_MS) {
      await stopMonitorForUnstableConnection("no incoming audio packets");
    }
  } catch (error) {
    console.debug("monitor packet stats failed", error);
  }
}

function startMonitorPacketStats() {
  clearMonitorStatsTimer();
  monitorLastPacketCount = 0;
  monitorLastPacketAt = Date.now();
  monitorStatsTimer = setInterval(pollMonitorPacketStats, MONITOR_STATS_INTERVAL_MS);
}

async function startStreamMonitor(streamId) {
  pauseCurrentEasAlert();
  pauseStreamArchive({forOther: true});
  if (monitorStreamId === streamId && monitorPeerConnection) {
    if (!liveAudioPeerIsUnusable(monitorPeerConnection)) {
      if (monitorPaused) {
        await resumeStreamMonitor();
        return;
      }
      return;
    }
    logClientEvent("info", "monitor", "restarting stale monitor live-audio session", {stream_id: streamId});
    await stopStreamMonitor({notifyServer: true});
  }
  logClientEvent("info", "monitor", "monitor start requested", {stream_id: streamId});
  await stopWeatherReceiver({notifyServer: true});
  await stopStreamMonitor({notifyServer: true});
  if (supportsWebSocketLiveAudio()) {
    const socket = await startLiveAudioSocket({
      mode: "monitor",
      client_id: pageMonitorClientId(),
      stream_id: streamId
    });
    monitorPeerConnection = liveAudioSocketPeer("monitor", socket);
    monitorStreamId = streamId;
    monitorPaused = false;
    hideResumeListeningOffer();
    await startMediaSessionAnchor("monitor-start");
    updateMonitorMediaSession();
    startMonitorPacketStats();
    renderStreams(configuredStreams);
    if (settingsStreamId) renderStreamSettings();
    return;
  }
  logClientEvent("warning", "monitor", "browser does not support live audio", {});
  throw new Error("This browser does not support live audio playback.");
}

async function stopStreamMonitor(options = {}) {
  const notifyServer = options.notifyServer !== false;
  const unstable = options.unstable === true;
  const clientId = pageMonitorClientId();
  clearLocalStreamMonitor();
  if (notifyServer) {
    try {
      await request("/api/monitor/stop", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({client_id: clientId})
      });
    } catch (error) {
      console.warn("monitor stop failed", error);
    }
  }
  renderStreams(configuredStreams);
  if (settingsStreamId) renderStreamSettings();
  if (unstable) showMonitorUnstableDialog();
}

async function pauseStreamMonitor() {
  if (!monitorPeerConnection || !monitorStreamId || monitorPaused) return;
  const streamId = monitorStreamId;
  monitorPaused = true;
  clearMonitorUnstableTimer();
  hideSameAlertFlash();
  await request("/api/monitor/pause", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({client_id: pageMonitorClientId()})
  });
  stopScheduledLiveAudioSources();
  pauseMediaSessionAnchor("monitor-pause");
  updateMonitorMediaSession();
  logClientEvent("info", "monitor", "monitor paused", {stream_id: streamId});
  renderStreams(configuredStreams);
  if (settingsStreamId) renderStreamSettings();
}

async function resumeStreamMonitor() {
  if (!monitorPeerConnection || !monitorStreamId) return;
  const streamId = monitorStreamId;
  try {
    await request("/api/monitor/resume", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({client_id: pageMonitorClientId()})
    });
  } catch (error) {
    if (!isInactiveLiveAudioSessionError(error)) throw error;
    logClientEvent("info", "monitor", "restarting inactive monitor live-audio session", {stream_id: streamId});
    await stopStreamMonitor({notifyServer: false});
    await startStreamMonitor(streamId);
    return;
  }
  monitorPaused = false;
  hideResumeListeningOffer();
  clearMonitorUnstableTimer();
  await startMediaSessionAnchor("monitor-resume");
  await ensureLiveAudioContext();
  liveAudioScheduledTime = 0;
  updateMonitorMediaSession();
  startMonitorPacketStats();
  logClientEvent("info", "monitor", "monitor resumed", {stream_id: streamId});
  renderStreams(configuredStreams);
  if (settingsStreamId) renderStreamSettings();
}

async function toggleStreamMonitor(streamId, resultHandler = setStreamResult) {
  if (monitorStreamId === streamId) {
    logClientEvent("info", "monitor", "monitor stop requested", {stream_id: streamId});
    await stopStreamMonitor();
    resultHandler("Monitoring stopped.", "success");
    return;
  }
  resultHandler("Starting monitor...");
  try {
    await startStreamMonitor(streamId);
    resultHandler("Monitoring started.", "success");
  } catch (error) {
    logClientEvent("warning", "monitor", "monitor start failed in browser", {stream_id: streamId, error: error.message});
    throw error;
  }
}

function pageReceiverClientId() {
  if (!receiverClientId) {
    receiverClientId = window.crypto && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  }
  return receiverClientId;
}

function currentReceiverChannel() {
  return NWR_RECEIVER_CHANNELS[receiverChannelIndex] || NWR_RECEIVER_CHANNELS[3];
}

function renderReceiverControls() {
  const channel = currentReceiverChannel();
  setText("receiver_frequency", channel.label);
  const playPause = document.getElementById("receiver_play_pause");
  const playIcon = document.getElementById("receiver_play_icon");
  const pauseIcon = document.getElementById("receiver_pause_icon");
  const label = receiverPlaying ? "Pause" : "Play";
  if (playPause) {
    playPause.setAttribute("aria-label", label);
    playPause.title = label;
  }
  if (playIcon) playIcon.hidden = receiverPlaying;
  if (pauseIcon) pauseIcon.hidden = !receiverPlaying;
}

function setReceiverResult(message, kind = "") {
  const element = document.getElementById("receiver-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function showReceiverUnstableDialog() {
  const dialog = document.getElementById("receiver_unstable_dialog");
  if (!dialog) return;
  dialog.hidden = false;
  const button = document.getElementById("dismiss_receiver_unstable");
  if (button) button.focus();
}

function dismissReceiverUnstableDialog() {
  const dialog = document.getElementById("receiver_unstable_dialog");
  if (dialog) dialog.hidden = true;
}

function clearReceiverUnstableTimer() {
  if (receiverUnstableTimer) {
    clearTimeout(receiverUnstableTimer);
    receiverUnstableTimer = null;
  }
}

function clearReceiverStatsTimer() {
  if (receiverStatsTimer) {
    clearInterval(receiverStatsTimer);
    receiverStatsTimer = null;
  }
}

function clearReceiverWatchdogs() {
  clearReceiverUnstableTimer();
  clearReceiverStatsTimer();
}

function markReceiverPacketProgress(packetCount) {
  receiverLastPacketCount = packetCount;
  receiverLastPacketAt = Date.now();
  clearReceiverUnstableTimer();
}

async function stopReceiverForUnstableConnection(reason = "") {
  if (!receiverPeerConnection) return;
  clearReceiverUnstableTimer();
  console.warn("weather receiver connection unstable", reason);
  await stopWeatherReceiver({notifyServer: true, unstable: true});
}

function scheduleReceiverUnstableStop(reason = "") {
  if (!receiverPlaying || receiverPaused || receiverUnstableTimer) return;
  receiverUnstableTimer = setTimeout(async () => {
    receiverUnstableTimer = null;
    await stopReceiverForUnstableConnection(reason);
  }, MONITOR_UNSTABLE_TIMEOUT_MS);
}

async function pollReceiverPacketStats() {
  const peer = receiverPeerConnection;
  if (!peer || !receiverPlaying || receiverPaused) return;
  try {
    const stats = await peer.getStats();
    let packetCount = 0;
    stats.forEach(report => {
      if (report.type === "inbound-rtp" && (report.kind === "audio" || report.mediaType === "audio")) {
        packetCount += Number(report.packetsReceived || 0);
      }
    });
    // Waiting on the server's own audio is not an unstable connection.
    if (packetCount > receiverLastPacketCount || liveAudioServerHasNoAudio) {
      markReceiverPacketProgress(packetCount);
      return;
    }
    if (receiverLastPacketAt && Date.now() - receiverLastPacketAt >= MONITOR_UNSTABLE_TIMEOUT_MS) {
      await stopReceiverForUnstableConnection("no incoming audio packets");
    }
  } catch (error) {
    console.debug("receiver packet stats failed", error);
  }
}

function startReceiverPacketStats() {
  clearReceiverStatsTimer();
  receiverLastPacketCount = 0;
  receiverLastPacketAt = Date.now();
  receiverStatsTimer = setInterval(pollReceiverPacketStats, MONITOR_STATS_INTERVAL_MS);
}

function updateReceiverMediaSession() {
  if (!("mediaSession" in navigator) || !("MediaMetadata" in window) || !receiverPeerConnection) return;
  const channel = currentReceiverChannel();
  navigator.mediaSession.metadata = new MediaMetadata({
    title: channel.label,
    artist: "NOAA Weather Radio"
  });
  navigator.mediaSession.playbackState = receiverPlaying && !receiverPaused ? "playing" : "paused";
  try {
    navigator.mediaSession.setActionHandler("previoustrack", () => receiverPreviousChannel());
    navigator.mediaSession.setActionHandler("nexttrack", () => receiverNextChannel());
    navigator.mediaSession.setActionHandler("play", () => startWeatherReceiver());
    navigator.mediaSession.setActionHandler("pause", () => {
      stopWeatherReceiver({preserveMediaSession: true})
        .catch(error => console.debug("media session receiver pause failed", error));
    });
  } catch (error) {
    console.debug("media session action setup failed", error);
  }
}

function updateMonitorMediaSession() {
  if (!("mediaSession" in navigator) || !("MediaMetadata" in window) || !monitorPeerConnection || !monitorStreamId) return;
  navigator.mediaSession.metadata = new MediaMetadata({
    title: monitoredStreamLabel(),
    artist: "NWR Stream Manager"
  });
  navigator.mediaSession.playbackState = monitorPaused ? "paused" : "playing";
  try {
    navigator.mediaSession.setActionHandler("previoustrack", null);
    navigator.mediaSession.setActionHandler("nexttrack", null);
    navigator.mediaSession.setActionHandler("play", () => {
      resumeStreamMonitor()
        .catch(error => console.debug("media session monitor play failed", error));
    });
    navigator.mediaSession.setActionHandler("pause", () => {
      pauseStreamMonitor()
        .catch(error => console.debug("media session monitor pause failed", error));
    });
  } catch (error) {
    console.debug("monitor media session action setup failed", error);
  }
}

function refreshReceiverMediaSession() {
  if (!receiverPeerConnection) return;
  updateReceiverMediaSession();
}

function scheduleReceiverMediaSessionRefresh() {
  if (!receiverPeerConnection) return;
  refreshReceiverMediaSession();
  for (const delay of [50, 250, 1000]) {
    window.setTimeout(refreshReceiverMediaSession, delay);
  }
}

function clearReceiverMediaSession() {
  clearLiveMediaSession();
}

async function startWeatherReceiver() {
  pauseCurrentEasAlert();
  pauseStreamArchive({forOther: true});
  if (receiverPeerConnection) {
    if (liveAudioPeerIsUnusable(receiverPeerConnection) || !receiverRemoteStream) {
      logClientEvent("info", "receiver", "restarting stale receiver live-audio session", {frequency: currentReceiverChannel().label});
      await stopWeatherReceiver({notifyServer: true});
    } else {
      logClientEvent("info", "receiver", "receiver WebSocket resume requested", {frequency: currentReceiverChannel().label});
      try {
        await request("/api/receiver/resume", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({client_id: pageReceiverClientId()})
        });
      } catch (error) {
        if (!isInactiveLiveAudioSessionError(error)) throw error;
        logClientEvent("info", "receiver", "restarting inactive receiver live-audio session", {frequency: currentReceiverChannel().label});
        await stopWeatherReceiver({notifyServer: false});
        return startWeatherReceiver();
      }
      receiverPlaying = true;
      receiverPaused = false;
      hideResumeListeningOffer();
      clearReceiverUnstableTimer();
      await ensureLiveAudioContext();
      liveAudioScheduledTime = 0;
      await startMediaSessionAnchor("receiver-resume");
      startReceiverPacketStats();
      updateReceiverMediaSession();
      renderReceiverControls();
      setReceiverResult(`Listening to ${currentReceiverChannel().label}.`, "success");
      return;
    }
  }
  logClientEvent("info", "receiver", "receiver start requested", {frequency: currentReceiverChannel().label});
  await stopStreamMonitor({notifyServer: true});
  if (supportsWebSocketLiveAudio()) {
    const channel = currentReceiverChannel();
    const socket = await startLiveAudioSocket(currentListeningParams("receiver"));
    receiverPeerConnection = liveAudioSocketPeer("receiver", socket);
    receiverRemoteStream = {transport: "websocket"};
    receiverPlaying = true;
    receiverPaused = false;
    hideResumeListeningOffer();
    await startMediaSessionAnchor("receiver-start");
    updateReceiverMediaSession();
    renderReceiverControls();
    startReceiverPacketStats();
    setReceiverResult(`Listening to ${channel.label}.`, "success");
    return;
  }
  logClientEvent("warning", "receiver", "browser does not support live audio", {});
  throw new Error("This browser does not support live audio playback.");
}

async function stopWeatherReceiver(options = {}) {
  const notifyServer = options.notifyServer !== false;
  const unstable = options.unstable === true;
  const preserveMediaSession = options.preserveMediaSession === true;
  const clientId = pageReceiverClientId();
  const peer = receiverPeerConnection;
  receiverPlaying = false;
  receiverPaused = preserveMediaSession && !!peer;
  clearReceiverWatchdogs();
  if (preserveMediaSession && peer) {
    hideSameAlertFlash();
    stopScheduledLiveAudioSources();
    logClientEvent("info", "receiver", "receiver pause requested", {frequency: currentReceiverChannel().label});
    await request("/api/receiver/pause", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({client_id: clientId})
    });
    pauseMediaSessionAnchor("receiver-pause");
    scheduleReceiverMediaSessionRefresh();
    renderReceiverControls();
    return;
  }
  if (liveAudioSocketParams && liveAudioSocketParams.mode === "receiver") cancelLiveAudioReconnect();
  receiverPeerConnection = null;
  receiverRemoteStream = null;
  receiverPaused = false;
  clearReceiverMediaSession();
  stopMediaSessionAnchor("receiver-stop");
  resetLiveAudioElement();
  if (peer) peer.close();
  if (notifyServer) {
    try {
      await request("/api/receiver/stop", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({client_id: clientId})
      });
    } catch (error) {
      console.warn("receiver stop failed", error);
    }
  }
  renderReceiverControls();
  if (unstable) showReceiverUnstableDialog();
}

async function setReceiverChannel(index) {
  const count = NWR_RECEIVER_CHANNELS.length;
  receiverChannelIndex = ((index % count) + count) % count;
  renderReceiverControls();
  updateReceiverMediaSession();
  if (!receiverPeerConnection) return;
  const channel = currentReceiverChannel();
  if (liveAudioPeerIsUnusable(receiverPeerConnection)) {
    logClientEvent("info", "receiver", "restarting stale receiver live-audio session before tune", {frequency: channel.label});
    await stopWeatherReceiver({notifyServer: true});
    await startWeatherReceiver();
    return;
  }
  try {
    await request("/api/receiver/tune", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        client_id: pageReceiverClientId(),
        frequency_hz: channel.frequency_hz
      })
    });
  } catch (error) {
    if (!isInactiveLiveAudioSessionError(error)) throw error;
    logClientEvent("info", "receiver", "restarting inactive receiver before tune", {frequency: channel.label});
    await stopWeatherReceiver({notifyServer: false});
    await startWeatherReceiver();
    return;
  }
  setReceiverResult(`Listening to ${channel.label}.`, "success");
}

async function receiverPreviousChannel() {
  try {
    await setReceiverChannel(receiverChannelIndex - 1);
  } catch (error) {
    setReceiverResult(error.message, "error");
  }
}

async function receiverNextChannel() {
  try {
    await setReceiverChannel(receiverChannelIndex + 1);
  } catch (error) {
    setReceiverResult(error.message, "error");
  }
}

function deviceLabel(device) {
  const parts = [device.vendor, device.name || "RTL-SDR", `serial ${device.serial}`].filter(Boolean);
  return parts.join(", ");
}

function configuredDeviceLabel(serial) {
  return `Configured SDR, serial ${serial} (not connected)`;
}

function friendlySoundcardLabel(device) {
  const card = String(device.card_long_name || device.card_name || device.card_id || "").trim();
  const pcm = String(device.pcm_name || device.pcm_id || "").trim();
  const bus = device.bus === "usb" ? "USB" : device.bus === "pci" ? "PCI" : "";
  const parts = [];
  if (card) parts.push(card);
  if (pcm && !card.toLowerCase().includes(pcm.toLowerCase())) parts.push(pcm);
  if (bus) parts.push(bus);
  const label = parts.join(", ").replace(/\b(Generic USB|SOF[- ]DSP|sof[- ]hdadsp)\b/gi, "").replace(/\s+,/g, ",").replace(/\s{2,}/g, " ").trim();
  return label || device.display_name || device.hw_device || "Sound card";
}

function soundcardChannelsForMode(mode) {
  if (mode === "left") return ["left"];
  if (mode === "right") return ["right"];
  return ["left", "right"];
}

function soundcardOccupancy(ignoreOutputId = "") {
  const occupied = new Map();
  for (const stream of configuredStreams) {
    for (const output of stream.outputs || []) {
      if ((output.type || "icecast") !== "soundcard") continue;
      if (output.enabled === false) continue;
      if (ignoreOutputId && output.id === ignoreOutputId) continue;
      const soundcard = output.soundcard || {};
      const stableId = soundcard.stable_id || "";
      if (!stableId) continue;
      if (!occupied.has(stableId)) occupied.set(stableId, new Set());
      for (const channel of soundcardChannelsForMode(soundcard.channel_mode || "both")) {
        occupied.get(stableId).add(channel);
      }
    }
  }
  return occupied;
}

function availableSoundcardChannels(stableId, ignoreOutputId = "") {
  const used = soundcardOccupancy(ignoreOutputId).get(stableId) || new Set();
  return ["left", "right"].filter(channel => !used.has(channel));
}

function soundcardDeviceHasAvailableChannels(stableId, ignoreOutputId = "") {
  return availableSoundcardChannels(stableId, ignoreOutputId).length > 0;
}

function chooseAvailableSoundcardChannel(preferred, stableId, ignoreOutputId = "") {
  const available = availableSoundcardChannels(stableId, ignoreOutputId);
  if (available.length === 0) return "";
  const preferredChannels = soundcardChannelsForMode(preferred || "both");
  if (preferred === "both" && available.length === 2) return "both";
  if (preferredChannels.length === 1 && available.includes(preferredChannels[0])) return preferredChannels[0];
  return available[0];
}

function setSoundcardChannelOptions(prefix, stableId, ignoreOutputId = "") {
  const available = availableSoundcardChannels(stableId, ignoreOutputId);
  const controls = {
    left: document.getElementById(`${prefix}_soundcard_channel_left`),
    both: document.getElementById(`${prefix}_soundcard_channel_both`),
    right: document.getElementById(`${prefix}_soundcard_channel_right`)
  };
  for (const [mode, control] of Object.entries(controls)) {
    if (!control) continue;
    const modeChannels = soundcardChannelsForMode(mode);
    const visible = stableId && modeChannels.every(channel => available.includes(channel));
    const label = control.closest("label");
    if (label) label.hidden = !visible;
    control.disabled = !visible;
    if (!visible) control.checked = false;
  }
  const current = Object.entries(controls).find(([_mode, control]) => control && control.checked && !control.disabled);
  if (!current) {
    const next = chooseAvailableSoundcardChannel("both", stableId, ignoreOutputId);
    if (next && controls[next]) controls[next].checked = true;
  }
}

// What the sound card choice says under it; the server says the same on pages without this script.
function soundcardDeviceHint(chosen, configuredMissing = false) {
  if (soundcardProblem && soundcardDevices.length === 0) return soundcardProblem;
  if (configuredMissing) return "The configured sound card is not connected.";
  if (chosen) return "Used directly, not shared with other programs.";
  if (soundcardDevices.length) return "Every sound card's channels are in use.";
  return "No sound cards found on this computer.";
}

function emptySoundcardOption() {
  return {value: "", label: soundcardDevices.length ? "No sound cards with free channels" : "No sound cards found"};
}

function renderWizardSoundcardDevices() {
  const select = document.getElementById("wizard_soundcard_device");
  if (!select) return;
  const ignoreOutputId = wizardSoundcardOutputId || "";
  const options = soundcardDevices
    .filter(device => soundcardDeviceHasAvailableChannels(device.stable_id, ignoreOutputId))
    .map(device => ({
      value: device.stable_id,
      label: friendlySoundcardLabel(device)
    }));
  if (options.length === 0) {
    options.push(emptySoundcardOption());
  }
  const signature = JSON.stringify(options);
  if (select.dataset.signature !== signature) {
    syncSelectOptions(select, options);
    select.dataset.signature = signature;
  }
  setText("wizard_soundcard_device_hint", soundcardDeviceHint(options[0] && options[0].value));
  const selectedStableId = select.value || "";
  setSoundcardChannelOptions("wizard", selectedStableId, ignoreOutputId);
}

function renderSettingsSoundcardDevices(selectedStableId = "") {
  const select = document.getElementById("settings_soundcard_device");
  if (!select) return;
  const options = [];
  const ignoreOutputId = editingOutputId || "";
  const selectedDevice = soundcardDevices.find(device => device.stable_id === selectedStableId);
  if (selectedStableId && !selectedDevice) {
    options.push({value: selectedStableId, label: `Configured sound card, ${selectedStableId} (not connected)`});
  }
  for (const device of soundcardDevices) {
    if (device.stable_id !== selectedStableId && !soundcardDeviceHasAvailableChannels(device.stable_id, ignoreOutputId)) continue;
    options.push({value: device.stable_id, label: friendlySoundcardLabel(device)});
  }
  if (options.length === 0) {
    options.push(emptySoundcardOption());
  }
  const signature = JSON.stringify(options);
  if (select.dataset.signature !== signature) {
    syncSelectOptions(select, options);
    select.dataset.signature = signature;
  }
  setValue("settings_soundcard_device", selectedStableId || (options[0] ? options[0].value : ""));
  setText(
    "settings_soundcard_device_hint",
    soundcardDeviceHint(options[0] && options[0].value, Boolean(selectedStableId && !selectedDevice))
  );
  setSoundcardChannelOptions("settings", select.value || "", ignoreOutputId);
  updateSettingsSoundcardResetButton();
}

function updateSettingsSoundcardResetButton() {
  const button = document.getElementById("reset_soundcard_device");
  if (!button) return;
  const selected = findConfiguredOutput(settingsStreamId, editingOutputId);
  const output = selected ? selected.output : null;
  const stableId = document.getElementById("settings_soundcard_device").value || "";
  const device = soundcardDeviceForStableId(stableId);
  button.hidden = accountIsReadOnly() || outputFormMode !== "edit_soundcard" || !output || !device || device.bus !== "usb";
}

// The Active SDR list holds this machine's RTL-SDRs and, after them, the ones
// paired NWR Stream Managers share over the network ("remote:" and their id).
const REMOTE_SDR_CHOICE_PREFIX = "remote:";
let localSdrDevices = [];
let configuredLocalSerial = "";

function renderActiveSdrChoices() {
  const select = document.getElementById("serial");
  const usesRemote = remoteSdrUsesRemote();
  const paired = remoteSdr.devices || [];
  const configured = configuredLocalSerial;
  const configuredMissing = configured && !localSdrDevices.some(device => device.serial === configured);
  const choices = [];
  if (!usesRemote && configuredMissing) {
    choices.push({value: configured, label: configuredDeviceLabel(configured)});
  } else if (!usesRemote && !configured && localSdrDevices.length !== 1) {
    choices.push({
      value: "",
      label: localSdrDevices.length === 0 && paired.length === 0 ? "No RTL-SDR devices found" : "Select an RTL-SDR..."
    });
  }
  for (const device of localSdrDevices) {
    choices.push({value: device.serial, label: deviceLabel(device)});
  }
  for (const device of paired) {
    choices.push({value: `${REMOTE_SDR_CHOICE_PREFIX}${device.id}`, label: `Shared by ${remoteDeviceName(device)} over the network`});
  }
  const signature = JSON.stringify(choices);
  if (select.dataset.signature !== signature) {
    syncSelectOptions(select, choices);
    select.dataset.signature = signature;
  }
  if (!sdrSettingIsBeingChanged("serial") && !remoteSdrRequestPending) {
    const remoteId = (remoteSdr.remote || {}).device_id;
    const fallback = !configured && localSdrDevices.length === 1 ? localSdrDevices[0].serial : "";
    setValue("serial", usesRemote ? `${REMOTE_SDR_CHOICE_PREFIX}${remoteId}` : configured || fallback);
  }
  const showRemote = document.getElementById("serial").value.startsWith(REMOTE_SDR_CHOICE_PREFIX);
  setHidden("reset_rtl_device", showRemote);
  setHidden("reset_remote_sdr", !showRemote);
  setHidden("remote_sdr_controls", !showRemote);
}

async function chooseActiveSdr(value) {
  if (value.startsWith(REMOTE_SDR_CHOICE_PREFIX)) {
    const id = value.slice(REMOTE_SDR_CHOICE_PREFIX.length);
    const device = (remoteSdr.devices || []).find(item => item.id === id);
    await saveSdrSource({source_mode: "remote", remote_host_id: id}, `Using the RTL-SDR shared by ${remoteDeviceName(device)}.`);
    return;
  }
  if (remoteSdrUsesRemote()) {
    await saveSdrSource({source_mode: "local", serial: value}, "Using the RTL-SDR connected to this machine.");
    return;
  }
  scheduleUpdate("serial");
}

async function loadDevices(selected, options = {}) {
  const data = await request("/api/devices");
  soundcardDevices = Array.isArray(data.soundcards) ? data.soundcards : [];
  soundcardProblem = String(data.soundcard_problem || "");
  localSdrDevices = Array.isArray(data.devices) ? data.devices : [];
  configuredLocalSerial = selected || "";
  if (options.force) document.getElementById("serial").dataset.signature = "";
  renderActiveSdrChoices();
  setText("device-errors", data.errors.join(" | "));
  renderWizardSoundcardDevices();
  if (outputFormMode === "edit_soundcard") {
    const selectedOutput = findConfiguredOutput(settingsStreamId, editingOutputId);
    renderSettingsSoundcardDevices(selectedOutput && selectedOutput.output.soundcard ? selectedOutput.output.soundcard.stable_id : "");
  }
  if (currentViewName() === "stream_settings" || currentViewName() === "stream_output") {
    icecastOutputTableSignature = "";
    soundcardOutputTableSignature = "";
    renderStreamSettings();
  }
}

async function loadIqTestSources(options = {}) {
  if (!developmentIqSourcesEnabled) return {files: [], active: null};
  const data = await request("/api/iq-test-sources");
  const select = document.getElementById("iq_test_source_file");
  const sourceOptions = data.files.map(file => ({
    value: file.name,
    label: `${file.name} (${formatDecimalBytes(file.size_bytes)})`
  }));
  if (sourceOptions.length === 0) {
    sourceOptions.push({value: "", label: "No I/Q files found"});
  }
  const signature = JSON.stringify(sourceOptions);
  if (options.force || iqTestSourcesSignature !== signature) {
    syncSelectOptions(select, sourceOptions);
    iqTestSourcesSignature = signature;
  }
  const active = data.active || null;
  if (active && data.files.some(file => file.name === active.name)) {
    setValue("iq_test_source_file", active.name);
    setValue("iq_test_source_sample_rate", active.sample_rate);
  }
  setText(
    "iq_test_source_directory",
    `Place interleaved complex float32 I/Q files in ${data.directory}.`
  );
  setDisabled(document.getElementById("start_iq_test_source"), sourceOptions.length === 0 || !sourceOptions[0].value);
  return data;
}

function renderIqTestSourceStatus(data) {
  developmentIqSourcesEnabled = Boolean(data && data.capabilities && data.capabilities.development_iq_sources);
  setHidden("iq_test_source_section", !developmentIqSourcesEnabled || remoteSdrUsesRemote());
  if (!developmentIqSourcesEnabled) {
    setText("iq-test-source-result", "");
    setText("iq_test_source_directory", "");
    setText("iq_test_source_position", "");
    return;
  }
  const source = data.source || {};
  const active = source.kind === "iq_file";
  const stats = data.capture_stats || {};
  setText(
    "iq-test-source-result",
    active ? `Using I/Q file source ${source.name} at ${source.sample_rate} S/s.` : ""
  );
  setText(
    "iq_test_source_position",
    active && stats.duration_seconds
      ? `Position: ${formatDuration(Number(stats.position_seconds || 0))} of ${formatDuration(Number(stats.duration_seconds || 0))}.`
      : ""
  );
  setDisabled(document.getElementById("stop_iq_test_source"), !active);
  for (const id of [
    "seek_iq_test_source_back_60",
    "seek_iq_test_source_back_10",
    "seek_iq_test_source_forward_10",
    "seek_iq_test_source_forward_60",
  ]) {
    setDisabled(document.getElementById(id), !active);
  }
}

async function seekIqTestSource(seconds) {
  setText("iq-test-source-result", seconds > 0 ? `Seeking forward ${formatDuration(seconds)}...` : `Seeking backward ${formatDuration(Math.abs(seconds))}...`);
  const data = await request("/api/iq-test-source/seek", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({seconds})
  });
  applyStatus(data, {syncControls: true});
  await loadIqTestSources({force: true});
}

function stationLabel(station) {
  const place = [station.city, station.state].filter(Boolean).join(", ");
  const site = station.site_name && station.site_name !== station.city ? `, ${station.site_name}` : "";
  return `${station.callsign} ${station.frequency} MHz, ${place}${site}`;
}

function stationSearchQuery() {
  const input = document.getElementById("station_search");
  return input ? input.value.trim() : "";
}

function updateStationSearchControls() {
  const button = document.getElementById("station_search_button");
  if (button) setDisabled(button, stationSearchQuery() === "");
}

function clearStationResults(message = "Enter search text to find a station.") {
  stationResults = [];
  selectedStationKey = "";
  const select = document.getElementById("station_results");
  if (select) syncSelectOptions(select, []);
  const label = document.getElementById("station_results_label");
  if (label) label.hidden = true;
  setText("selected_station", message);
  renderWizard();
}

async function searchStations() {
  const query = stationSearchQuery();
  updateStationSearchControls();
  if (query === "") {
    clearStationResults();
    return;
  }
  const data = await request(`/api/stations?q=${encodeURIComponent(query)}&limit=75`);
  stationResults = data.stations || [];
  const select = document.getElementById("station_results");
  const selectOptions = [];
  if (stationResults.length === 0) {
    selectOptions.push({value: "", label: "No stations found"});
  }
  for (const station of stationResults) {
    selectOptions.push({value: station.key, label: stationLabel(station)});
  }
  syncSelectOptions(select, selectOptions);
  const label = document.getElementById("station_results_label");
  if (label) label.hidden = false;
  selectedStationKey = "";
  setText("selected_station", "Select a station to continue.");
  renderWizard();
}

async function runStationSearchFromUi() {
  if (stationSearchQuery() === "") {
    updateStationSearchControls();
    clearStationResults();
    return;
  }
  await searchStations();
  setStreamResult("");
}

function selectedStation() {
  if (wizardStationOverride) return wizardStationOverride;
  return stationResults.find(station => station.key === selectedStationKey);
}

function chooseStation() {
  selectedStationKey = document.getElementById("station_results").value;
  const station = selectedStation();
  if (!station) {
    setText("selected_station", "Select a station to continue.");
    renderWizard();
    return;
  }
  setText("selected_station", `Selected ${stationLabel(station)}`);
  wizardDirty = true;
  renderWizard();
}

function streamPayload() {
  const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
  const station = selectedStation();
  const outputType = selectedWizardOutputType();
  if (outputType === "soundcard" && wizardMode !== "edit") {
    return {
      station_key: selectedStationKey,
      type: "soundcard",
      preview_id: wizardSoundcardPreviewId,
      soundcard: wizardSoundcardPayload()
    };
  }
  const selectedFormat = selectedWizardFormat();
  const icecast = applyServicePresetToIcecast({
    host: document.getElementById("icecast_host").value,
    port: Number(document.getElementById("icecast_port").value),
    username: document.getElementById("icecast_username").value,
    password: document.getElementById("icecast_password").value,
    mount: document.getElementById("icecast_mount").value,
    format: selectedFormat,
    sample_rate: Number(document.getElementById("icecast_sample_rate").value),
    bitrate: Number(document.getElementById("icecast_bitrate").value)
  }, service, station, "icecast");
  return {
    station_key: selectedStationKey,
    type: "icecast",
    icecast
  };
}

function selectedSettingsSoundcardChannelMode() {
  const selected = document.querySelector("input[name='settings_soundcard_channel']:checked");
  return selected ? selected.value : "both";
}

function settingsSoundcardPayload() {
  return {
    stable_id: document.getElementById("settings_soundcard_device").value || "",
    channel_mode: selectedSettingsSoundcardChannelMode(),
    volume: Number(document.getElementById("settings_soundcard_volume").value),
    sample_rate: 48000
  };
}

function selectedWizardOutputType() {
  const selected = document.querySelector("input[name='wizard_output_type']:checked");
  return selected ? selected.value : "icecast";
}

function selectedWizardSoundcardChannelMode() {
  const selected = document.querySelector("input[name='wizard_soundcard_channel']:checked");
  return selected ? selected.value : "both";
}

function wizardSoundcardPayload() {
  return {
    stable_id: document.getElementById("wizard_soundcard_device").value || "",
    channel_mode: selectedWizardSoundcardChannelMode(),
    volume: Number(document.getElementById("wizard_soundcard_volume").value),
    sample_rate: 48000
  };
}

function adjustWizardSoundcardChannelForDevice() {
  const stableId = document.getElementById("wizard_soundcard_device").value || "";
  const current = selectedWizardSoundcardChannelMode();
  const next = chooseAvailableSoundcardChannel(current, stableId, wizardSoundcardOutputId || "");
  setSoundcardChannelOptions("wizard", stableId, wizardSoundcardOutputId || "");
  if (next) setChecked(`wizard_soundcard_channel_${next}`, true);
}

function adjustSettingsSoundcardChannelForDevice() {
  const stableId = document.getElementById("settings_soundcard_device").value || "";
  const current = selectedSettingsSoundcardChannelMode();
  const next = chooseAvailableSoundcardChannel(current, stableId, editingOutputId || "");
  setSoundcardChannelOptions("settings", stableId, editingOutputId || "");
  if (next) setChecked(`settings_soundcard_channel_${next}`, true);
  updateSettingsSoundcardResetButton();
}

async function resetSoundcardByStableId(stableId, resultSetter = setOutputResult) {
  if (!stableId || accountIsReadOnly()) return;
  resultSetter("Resetting USB sound card...");
  const data = await requestWithTimeout("/api/soundcard-reset", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({stable_id: stableId})
  }, 15000);
  resultSetter(data.message || "Sound card reset finished.", data.success ? "success" : "error");
  applyStatus(data.status || await request(statusRequestPath()), {syncControls: false});
  await loadDevices("", {force: true});
}

function addOutputIcecastStep(streamStep) {
  if (streamStep === 1) return ADD_OUTPUT_ICECAST_SERVICE_STEP;
  if (streamStep === 2) return ADD_OUTPUT_ICECAST_CODEC_STEP;
  if (streamStep === 3) return ADD_OUTPUT_ICECAST_CREDENTIALS_STEP;
  if (streamStep === 4) return ADD_OUTPUT_ICECAST_QUALITY_STEP;
  return streamStep;
}

function streamIcecastStep(addOutputStep) {
  if (addOutputStep === ADD_OUTPUT_ICECAST_SERVICE_STEP) return 1;
  if (addOutputStep === ADD_OUTPUT_ICECAST_CODEC_STEP) return 2;
  if (addOutputStep === ADD_OUTPUT_ICECAST_CREDENTIALS_STEP) return 3;
  if (addOutputStep === ADD_OUTPUT_ICECAST_QUALITY_STEP) return 4;
  return addOutputStep;
}

function wizardUsesOutputSteps() {
  return wizardMode === "add_output" || wizardStep >= ADD_OUTPUT_TYPE_STEP;
}

function wizardIsSoundcardFlow() {
  return (
    selectedWizardOutputType() === "soundcard" &&
    (wizardMode === "add_output" || wizardMode === "add") &&
    wizardStep >= ADD_OUTPUT_TYPE_STEP
  );
}

async function setStreamEnabled(streamId, enabled, resultHandler = setStreamResult) {
  if (accountIsReadOnly()) {
    resultHandler("This account is read-only.", "error");
    return null;
  }
  const stream = configuredStreams.find(item => item.id === streamId);
  const previousEnabled = stream ? streamIsEnabled(stream) : null;
  if (stream) {
    stream.enabled = enabled;
    if (!enabled && monitorStreamId === streamId) clearLocalStreamMonitor();
    renderStreams(configuredStreams, {force: true});
  }
  try {
    const data = await request("/api/streams", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({stream_id: streamId, enabled})
    });
    if (!enabled && monitorStreamId === streamId) clearLocalStreamMonitor();
    renderStreams(data.streams || [], {force: true});
    resultHandler(enabled ? "Stream started." : "Stream stopped.", "success");
    return data;
  } catch (error) {
    if (stream && previousEnabled !== null) {
      stream.enabled = previousEnabled;
      renderStreams(configuredStreams, {force: true});
    }
    throw error;
  }
}

function streamLabel(stream) {
  const station = stream.station;
  const output = streamOutputs(stream)[0] || {};
  const icecast = output.icecast || stream.icecast || {};
  if (!station || !icecast.host) return "Incomplete stream";
  return `${station.callsign} ${station.frequency} MHz to ${icecast.username}@${icecast.host}:${icecast.port}${icecast.mount} (${icecast.format.toUpperCase()})`;
}

function normalizeStreamStatus(status) {
  const normalized = String(status || "disabled").toLowerCase().replace(/_/g, "-");
  if (normalized === "enabled" || normalized === "needs-attention" || normalized === "disabled" || normalized === "standby") {
    return normalized;
  }
  return "disabled";
}

function streamStatusLabel(status) {
  const normalized = normalizeStreamStatus(status);
  if (normalized === "enabled") return "Enabled";
  if (normalized === "needs-attention") return "Needs attention";
  if (normalized === "standby") return "Disconnected (no station audio)";
  return "Disabled";
}

function streamIsEnabled(stream) {
  return Boolean(stream && stream.enabled !== false && stream.enabled !== "false" && stream.enabled !== 0);
}

function streamOutputCount(stream) {
  // As the streams list counts them: stream archiving isn't one of them.
  return enabledConfiguredOutputCount(stream) + (easRecordingEnabled(stream) ? 1 : 0);
}

function enabledConfiguredOutputCount(stream) {
  return streamOutputs(stream).filter(output => output.enabled !== false).length;
}

function easRecordingEnabled(stream) {
  return Boolean(stream && stream.eas_recording && stream.eas_recording.enabled);
}

function archiveEnabled(stream) {
  return Boolean(stream && stream.archive && stream.archive.enabled);
}

// Everything that keeps a stream running. Stream archiving does, like an output.
function enabledStreamOutputCount(stream) {
  return enabledConfiguredOutputCount(stream) + (easRecordingEnabled(stream) ? 1 : 0) + (archiveEnabled(stream) ? 1 : 0);
}

function canDisableOutput(stream, output) {
  if (!output || output.enabled === false) return true;
  return enabledStreamOutputCount(stream) > 1;
}

function canRemoveOutput(stream, output) {
  if (!output || output.enabled === false) return true;
  return enabledStreamOutputCount(stream) > 1;
}

function canDisableEasRecording(stream) {
  if (!easRecordingEnabled(stream)) return true;
  return enabledStreamOutputCount(stream) > 1;
}

function populateBitrates() {
  for (const select of [document.getElementById("icecast_bitrate"), document.getElementById("settings_icecast_bitrate")]) {
    if (!select || select.options.length) continue;
    for (let bitrate = 8; bitrate <= 320; bitrate += 8) {
      const option = document.createElement("option");
      option.value = String(bitrate);
      option.textContent = `${bitrate} Kbps`;
      if (bitrate === 48) option.selected = true;
      select.appendChild(option);
    }
  }
}

function streamOutputs(stream) {
  if (Array.isArray(stream.outputs)) return stream.outputs;
  if (stream.icecast) return [{id: stream.id, enabled: streamIsEnabled(stream), icecast: stream.icecast}];
  return [];
}

function normalizedServiceHost(host) {
  // Just the host name, whether it was typed bare, as host:port, or as a web address.
  const text = String(host || "").trim().toLowerCase().replace(/^[a-z][a-z0-9+.-]*:[/][/]/, "");
  return text.split(/[/:?#]/)[0];
}

function icecastHostForDisplay(host, tls) {
  // Encrypted (TLS) servers are saved as a plain host plus a flag; show them as typed.
  const text = String(host || "");
  return tls && text && !/^https:/i.test(text) ? `https://${text}` : text;
}

function serviceForHost(host) {
  return STREAM_SERVICE_HOSTS[normalizedServiceHost(host)] || STREAM_SERVICE_CUSTOM;
}

function stationSiteToken(station) {
  return String((station && station.site_name) || "")
    .replace(/[^A-Za-z0-9 ]+/g, " ")
    .split(/\s+/)
    .filter(Boolean)
    .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join("");
}

function stationCallsign(station) {
  return String((station && station.callsign) || "").trim().toUpperCase();
}

function stationState(station) {
  return String((station && station.state) || "").trim().toUpperCase();
}

function selectedWizardFormat() {
  const selectedFormat = document.querySelector("input[name='icecast_format']:checked");
  return selectedFormat ? selectedFormat.value : "mp3";
}

function selectedSettingsFormat() {
  const selectedFormat = document.querySelector("input[name='settings_icecast_format']:checked");
  return selectedFormat ? selectedFormat.value : "mp3";
}

function weatherUsaMount(station, format) {
  const callsign = stationCallsign(station) || "CALLSIGN";
  return `/NWR/${callsign}.${format || "mp3"}`;
}

function isWeatherUsaGeneratedMount(value, station) {
  const mount = String(value || "").trim();
  return mount === weatherUsaMount(station, "mp3") || mount === weatherUsaMount(station, "ogg");
}

function nwrOrgBaseMount(station) {
  const state = stationState(station) || "XX";
  const site = stationSiteToken(station) || "Site";
  const callsign = stationCallsign(station) || "CALLSIGN";
  return `/${state}-${site}-${callsign}`;
}

function nwrOrgMount(station, alternate, alternateNumber) {
  const suffix = alternate ? `-alt${Math.max(1, Math.min(9, Number(alternateNumber || 1)))}` : "";
  return `${nwrOrgBaseMount(station)}${suffix}`;
}

function serviceFromIcecast(icecast) {
  const explicit = String((icecast && icecast.service) || "").trim();
  if (explicit && STREAM_SERVICE_NAMES[explicit]) return explicit;
  return serviceForHost(icecast && icecast.host);
}

function applyServicePresetToIcecast(icecast, service, station, prefix = "icecast") {
  const payload = Object.assign({}, icecast, {service});
  const format = payload.format || "mp3";
  if (service === STREAM_SERVICE_GWES) {
    payload.host = "ingest.wxr.gwes-cdn.net";
    payload.port = 10000;
    payload.format = "mp3";
    payload.bitrate = Math.max(64, Number(payload.bitrate || 64));
    payload.sample_rate = Number(payload.sample_rate || 22050);
  } else if (service === STREAM_SERVICE_WEATHERUSA) {
    payload.host = "radio-master.weatherusa.net";
    payload.port = 80;
    payload.username = "source";
    payload.format = format;
    payload.bitrate = Math.max(32, Math.min(56, Number(payload.bitrate || (format === "mp3" ? 56 : 48))));
    payload.sample_rate = Math.min(22050, Number(payload.sample_rate || 22050));
    if (!String(payload.mount || "").trim()) payload.mount = weatherUsaMount(station, payload.format);
  } else if (service === STREAM_SERVICE_NWRORG) {
    const altEnabled = document.getElementById(`${prefix}_alt_enabled`);
    const altNumber = document.getElementById(`${prefix}_alt_number`);
    payload.host = "wxradio.org";
    payload.port = 8000;
    payload.username = "source";
    payload.password = "WxRadio2014";
    payload.format = "mp3";
    payload.bitrate = 32;
    payload.sample_rate = 22050;
    payload.mount = nwrOrgMount(station, altEnabled && altEnabled.checked, altNumber ? altNumber.value : 1);
  }
  return payload;
}

function setServiceHelp(elementId, service) {
  const element = document.getElementById(elementId);
  if (!element) return;
  element.innerHTML = STREAM_SERVICE_HELP[service] || "";
}

function setHidden(id, hidden) {
  const element = document.getElementById(id);
  if (element) element.hidden = hidden;
}

function setReadOnly(id, readOnly) {
  const element = document.getElementById(id);
  if (element) element.readOnly = readOnly;
}

function setSelectDisabled(id, disabled) {
  const element = document.getElementById(id);
  if (element) element.disabled = disabled;
}

function setOptionAvailability(selectId, predicate) {
  const select = document.getElementById(selectId);
  if (!select) return;
  let selectedStillAvailable = false;
  for (const option of select.options) {
    const allowed = predicate(Number(option.value));
    option.disabled = !allowed;
    if (option.selected && allowed) selectedStillAvailable = true;
  }
  if (!selectedStillAvailable) {
    for (const option of select.options) {
      if (!option.disabled) {
        select.value = option.value;
        break;
      }
    }
  }
}

function clampServiceFields(prefix, service) {
  if (service === STREAM_SERVICE_GWES) {
    setOptionAvailability(`${prefix}_bitrate`, value => value >= 64);
    setOptionAvailability(`${prefix}_sample_rate`, () => true);
  } else if (service === STREAM_SERVICE_WEATHERUSA) {
    setOptionAvailability(`${prefix}_bitrate`, value => value >= 32 && value <= 56);
    setOptionAvailability(`${prefix}_sample_rate`, value => value <= 22050);
  } else if (service === STREAM_SERVICE_NWRORG) {
    setOptionAvailability(`${prefix}_bitrate`, value => value === 32);
    setOptionAvailability(`${prefix}_sample_rate`, value => value === 22050);
    setValue(`${prefix}_bitrate`, 32);
    setValue(`${prefix}_sample_rate`, 22050);
  } else {
    setOptionAvailability(`${prefix}_bitrate`, () => true);
    setOptionAvailability(`${prefix}_sample_rate`, () => true);
  }
}

function applyServiceControls(prefix, service) {
  setHidden(`${prefix}_host_label`, service !== STREAM_SERVICE_CUSTOM);
  setHidden(`${prefix}_port_label`, service !== STREAM_SERVICE_CUSTOM);
  setHidden(`${prefix}_username_label`, service === STREAM_SERVICE_WEATHERUSA || service === STREAM_SERVICE_NWRORG);
  setHidden(`${prefix}_password_label`, service === STREAM_SERVICE_NWRORG);
  setHidden(prefix === "icecast" ? "show_icecast_password_label" : "settings_show_icecast_password_label", service === STREAM_SERVICE_NWRORG);
  setHidden(`${prefix}_mount_label`, service === STREAM_SERVICE_NWRORG);
  setHidden(`${prefix}_alt_label`, service !== STREAM_SERVICE_NWRORG);
  const alternateEnabled = document.getElementById(`${prefix}_alt_enabled`);
  setHidden(`${prefix}_alt_number_label`, service !== STREAM_SERVICE_NWRORG || !alternateEnabled || !alternateEnabled.checked);
  setHidden(`${prefix}_format_fieldset`, service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_NWRORG);
  setHidden(`${prefix}_sample_rate_label`, service === STREAM_SERVICE_NWRORG);
  setHidden(`${prefix}_bitrate_label`, service === STREAM_SERVICE_NWRORG);
  setReadOnly(`${prefix}_username`, service === STREAM_SERVICE_WEATHERUSA);
  setSelectDisabled(`${prefix}_sample_rate`, service === STREAM_SERVICE_NWRORG);
  setSelectDisabled(`${prefix}_bitrate`, service === STREAM_SERVICE_NWRORG);
  const mp3 = document.getElementById(`${prefix}_format_mp3`);
  const ogg = document.getElementById(`${prefix}_format_ogg`);
  if (mp3) mp3.disabled = service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_NWRORG;
  if (ogg) ogg.disabled = service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_NWRORG;
  clampServiceFields(prefix, service);
}

function findConfiguredOutput(streamId, outputId) {
  const stream = configuredStreams.find(item => item.id === streamId);
  if (!stream) return null;
  const outputs = streamOutputs(stream);
  const output = outputs.find(item => item.id === outputId);
  if (!output) return null;
  return {stream, output, outputs};
}

function setIcecastForm(icecast) {
  const service = serviceFromIcecast(icecast);
  const station = selectedStation();
  const preset = applyServicePresetToIcecast(icecast || {}, service, station, "icecast");
  setValue("icecast_service", service);
  setValue("icecast_host", preset.host || "");
  setValue("icecast_port", preset.port || "");
  setValue("icecast_username", preset.username || "");
  setValue("icecast_password", preset.password || "");
  setValue("icecast_mount", preset.mount || "");
  const format = (icecast && icecast.format) || "mp3";
  setChecked("icecast_format_mp3", preset.format === "mp3");
  setChecked("icecast_format_ogg", preset.format === "ogg");
  setValue("icecast_sample_rate", preset.sample_rate || 24000);
  setValue("icecast_bitrate", preset.bitrate || (format === "mp3" ? 64 : 48));
  setServiceHelp("icecast_service_help", service);
  applyServiceControls("icecast", service);
}

function settingsIcecastPayload() {
  const service = document.getElementById("settings_icecast_service").value || STREAM_SERVICE_CUSTOM;
  const selected = findConfiguredOutput(settingsStreamId, editingOutputId);
  const station = selected ? selected.stream.station : currentSettingsStream() ? currentSettingsStream().station : null;
  return applyServicePresetToIcecast({
    host: document.getElementById("settings_icecast_host").value,
    port: Number(document.getElementById("settings_icecast_port").value),
    username: document.getElementById("settings_icecast_username").value,
    password: document.getElementById("settings_icecast_password").value,
    mount: document.getElementById("settings_icecast_mount").value,
    format: selectedSettingsFormat(),
    sample_rate: Number(document.getElementById("settings_icecast_sample_rate").value),
    bitrate: Number(document.getElementById("settings_icecast_bitrate").value)
  }, service, station, "settings_icecast");
}

function outputFormSignature() {
  return JSON.stringify(settingsIcecastPayload());
}

function outputCredentialSignature(icecast) {
  return JSON.stringify({
    host: icecast.host,
    port: Number(icecast.port),
    username: icecast.username,
    password: icecast.password,
    mount: icecast.mount
  });
}

function duplicateOutputExists(icecast, ignoreOutputId = "") {
  const signature = outputCredentialSignature(icecast);
  for (const stream of configuredStreams) {
    for (const output of streamOutputs(stream)) {
      if (ignoreOutputId && output.id === ignoreOutputId) continue;
      if (outputCredentialSignature(output.icecast || {}) === signature) return true;
    }
  }
  return false;
}

function customServiceHostWarning(host) {
  const service = serviceForHost(host);
  if (service === STREAM_SERVICE_CUSTOM) return "";
  return `Please select ${STREAM_SERVICE_NAMES[service]} instead of typing its Icecast URL in custom Icecast setup.`;
}

function settingsCredentialsComplete() {
  const payload = settingsIcecastPayload();
  const service = document.getElementById("settings_icecast_service").value || STREAM_SERVICE_CUSTOM;
  if (service === STREAM_SERVICE_CUSTOM && customServiceHostWarning(payload.host)) return false;
  return Boolean(
    payload.host.trim() &&
    payload.port >= 1 &&
    payload.port <= 65535 &&
    payload.username.trim() &&
    payload.password &&
    payload.mount.trim()
  );
}

function setSettingsIcecastForm(icecast) {
  const service = serviceFromIcecast(icecast);
  const selected = findConfiguredOutput(settingsStreamId, editingOutputId);
  const station = selected ? selected.stream.station : currentSettingsStream() ? currentSettingsStream().station : null;
  const preset = applyServicePresetToIcecast(icecast || {}, service, station, "settings_icecast");
  setValue("settings_icecast_service", service);
  setValue("settings_icecast_host", icecastHostForDisplay(preset.host, icecast && icecast.tls));
  setValue("settings_icecast_port", preset.port || "");
  setValue("settings_icecast_username", preset.username || "");
  setValue("settings_icecast_password", preset.password || "");
  setValue("settings_icecast_mount", preset.mount || "");
  const format = preset.format || "mp3";
  setChecked("settings_icecast_format_mp3", format === "mp3");
  setChecked("settings_icecast_format_ogg", format === "ogg");
  setValue("settings_icecast_sample_rate", preset.sample_rate || 24000);
  setValue("settings_icecast_bitrate", preset.bitrate || (format === "mp3" ? 64 : 48));
  setServiceHelp("settings_icecast_service_help", service);
  applyServiceControls("settings_icecast", service);
}

function clearSettingsIcecastForm() {
  setSettingsIcecastForm({service: STREAM_SERVICE_CUSTOM, format: "mp3", sample_rate: 24000, bitrate: 64});
  setChecked("settings_show_icecast_password", false);
  document.getElementById("settings_icecast_password").type = "password";
  outputFormOriginalSignature = outputFormSignature();
  outputFormDirty = false;
}

function setOutputResult(message, kind = "") {
  const element = document.getElementById("output-result");
  const listElement = document.getElementById("output-list-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  const text = String(message || "");
  for (const target of [element, listElement]) {
    if (!target) continue;
    if (target.className !== className) target.className = className;
    if (target.textContent !== text) target.textContent = text;
  }
}

function dismissNwrOrgSubmissionDialog() {
  const dialog = document.getElementById("nwrorg_submission_dialog");
  if (dialog) dialog.hidden = true;
}

function maybeShowNwrOrgSubmissionNotice(icecast) {
  if (!icecast || icecast.service !== STREAM_SERVICE_NWRORG) return;
  const dialog = document.getElementById("nwrorg_submission_dialog");
  if (!dialog) return;
  dialog.hidden = false;
  const link = document.getElementById("nwrorg_submission_link");
  if (link) link.focus();
}

function resetWizardForService(service) {
  const station = selectedStation();
  const current = streamPayload().icecast;
  const next = applyServicePresetToIcecast({
    service,
    format: selectedWizardFormat(),
    sample_rate: DEFAULT_STREAM_SAMPLE_RATE,
    bitrate: service === STREAM_SERVICE_WEATHERUSA ? 48 : DEFAULT_STREAM_BITRATES.mp3,
    mount: service === STREAM_SERVICE_WEATHERUSA ? weatherUsaMount(station, selectedWizardFormat()) : ""
  }, service, station, "icecast");
  if (service === STREAM_SERVICE_CUSTOM) {
    next.host = "";
    next.port = "";
    next.username = "";
    next.password = "";
    next.mount = "";
  } else if (service === STREAM_SERVICE_WEATHERUSA) {
    next.password = current.password || "";
  }
  setIcecastForm(next);
  icecastAuthPassed = false;
  icecastAuthSignature = "";
  renderWizard();
}

function resetSettingsForService(service) {
  const stream = currentSettingsStream();
  const station = stream ? stream.station : null;
  const current = settingsIcecastPayload();
  const next = applyServicePresetToIcecast({
    service,
    format: selectedSettingsFormat(),
    sample_rate: DEFAULT_STREAM_SAMPLE_RATE,
    bitrate: service === STREAM_SERVICE_WEATHERUSA ? 48 : DEFAULT_STREAM_BITRATES.mp3,
    mount: service === STREAM_SERVICE_WEATHERUSA ? weatherUsaMount(station, selectedSettingsFormat()) : ""
  }, service, station, "settings_icecast");
  if (service === STREAM_SERVICE_CUSTOM) {
    next.host = "";
    next.port = "";
    next.username = "";
    next.password = "";
    next.mount = "";
  } else if (service === STREAM_SERVICE_WEATHERUSA) {
    next.password = current.password || "";
  }
  setSettingsIcecastForm(next);
  updateOutputFormButtons();
}

// Which announcement fallback audio plays, as the server last reported it.
let fallbackAudioState = {audio: "default", custom_audio_available: false, custom_audio_seconds: null, custom_audio_problem: ""};
// Custom was chosen before there was a custom announcement to switch to.
let fallbackCustomChosenWithoutAudio = false;
let announcementPlayer = null;
let fallbackRecorder = null;
let fallbackRecording = null;
const FALLBACK_AUDIO_MAX_BYTES = 10 * 1024 * 1024;
const FALLBACK_RECORDING_MAX_SECONDS = 60;
const FALLBACK_RECORDER_WORKLET = `
class AnnouncementRecorder extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input.length) {
      const mono = new Float32Array(input[0].length);
      for (const channel of input) {
        for (let i = 0; i < mono.length; i++) mono[i] += channel[i] / input.length;
      }
      this.port.postMessage(mono, [mono.buffer]);
    }
    return true;
  }
}
registerProcessor("announcement-recorder", AnnouncementRecorder);
`;

function setFallbackAudioResult(message, kind = "") {
  setMessage("fallback-audio-result", message, kind);
}

function fallbackCustomSelected() {
  return document.getElementById("fallback_audio_custom").checked;
}

function renderFallbackAnnouncement() {
  const state = fallbackAudioState;
  const custom = fallbackCustomChosenWithoutAudio || state.audio === "custom";
  setChecked("fallback_audio_default", !custom);
  setChecked("fallback_audio_custom", custom);
  // Plays whichever announcement is chosen, so it goes when there's nothing to play.
  setHidden("fallback_play", custom && !state.custom_audio_available);
  setHidden("fallback_custom_controls", !custom);
  const status = document.getElementById("fallback_custom_status");
  let text = "";
  if (state.custom_audio_problem) {
    text = state.audio === "custom"
      ? `${state.custom_audio_problem} The default announcement is playing instead. Upload or record a new custom announcement.`
      : state.custom_audio_problem;
  } else if (state.custom_audio_available) {
    text = `The custom announcement is ${formatDuration(state.custom_audio_seconds)} long.`;
  } else if (custom) {
    text = "There's no custom announcement yet. Upload a file or record one below.";
  }
  const className = state.custom_audio_problem ? "message error" : "hint";
  if (status.className !== className) status.className = className;
  if (status.textContent !== text) status.textContent = text;
  status.hidden = !text;
}

async function saveFallbackAnnouncementChoice(audio) {
  try {
    const data = await request("/api/fallback-settings", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({audio})
    });
    applyStatus(data, {syncControls: false});
    setFallbackControls(data.fallback);
    setFallbackAudioResult(
      audio === "custom" ? "Fallback audio now plays the custom announcement." : "Fallback audio now plays the default announcement.",
      "success"
    );
  } catch (error) {
    setFallbackAudioResult(error.message, "error");
    fallbackSignature = "";
    await refresh().catch(() => {});
  }
}

function chooseFallbackAnnouncement(audio) {
  if (audio === "custom" && !fallbackAudioState.custom_audio_available) {
    // Nothing to switch to yet; show how to add one.
    stopAnnouncementPlayback();
    fallbackCustomChosenWithoutAudio = true;
    renderFallbackAnnouncement();
    setFallbackAudioResult("");
    return;
  }
  fallbackCustomChosenWithoutAudio = false;
  if (audio === fallbackAudioState.audio) {
    renderFallbackAnnouncement();
    return;
  }
  // The play button now plays the other announcement, from its start.
  stopAnnouncementPlayback();
  // Show the choice straight away; the reply confirms it, or a refresh undoes it.
  fallbackAudioState = {...fallbackAudioState, audio};
  renderFallbackAnnouncement();
  saveFallbackAnnouncementChoice(audio);
}

// Each play button pauses what it's playing, and keeps focus while its label changes.
const ANNOUNCEMENT_BUTTONS = {
  fallback_play: {play: "Play announcement", pause: "Pause announcement", page: "fallback_audio", setResult: message => setFallbackAudioResult(message, "error")},
  fallback_recording_play: {play: "Play recording", pause: "Pause recording", page: "record_announcement", setResult: message => setFallbackRecordingResult(message, "error")}
};
let announcementPlayerButton = "";

function renderAnnouncementButtons() {
  for (const [id, button] of Object.entries(ANNOUNCEMENT_BUTTONS)) {
    const playing = announcementPlayerButton === id && announcementPlayer && !announcementPlayer.paused;
    setText(id, playing ? button.pause : button.play);
  }
}

function stopAnnouncementPlayback() {
  if (announcementPlayer) {
    announcementPlayer.pause();
    announcementPlayer.removeAttribute("src");
    announcementPlayer.load();
  }
  announcementPlayer = null;
  announcementPlayerButton = "";
  renderAnnouncementButtons();
}

// Announcements only play on the page they were started from.
function stopAnnouncementPlaybackOffPage(view, section) {
  if (!announcementPlayerButton) return;
  if (view !== "settings" || section !== ANNOUNCEMENT_BUTTONS[announcementPlayerButton].page) stopAnnouncementPlayback();
}

function resumeAnnouncement(player, buttonId) {
  player.play().catch(error => {
    if (announcementPlayer !== player) return;
    stopAnnouncementPlayback();
    ANNOUNCEMENT_BUTTONS[buttonId].setResult(`The announcement couldn't be played: ${error.message}`);
  });
}

function toggleAnnouncement(buttonId, url) {
  if (announcementPlayer && announcementPlayerButton === buttonId) {
    if (announcementPlayer.paused) resumeAnnouncement(announcementPlayer, buttonId);
    else announcementPlayer.pause();
    return;
  }
  stopAnnouncementPlayback();
  const player = new Audio(url);
  announcementPlayer = player;
  announcementPlayerButton = buttonId;
  player.addEventListener("play", renderAnnouncementButtons);
  player.addEventListener("pause", renderAnnouncementButtons);
  // Finished, so the next Play starts from the beginning.
  player.addEventListener("ended", () => {
    if (announcementPlayer === player) stopAnnouncementPlayback();
  });
  player.addEventListener("error", () => {
    if (announcementPlayer !== player) return;
    stopAnnouncementPlayback();
    ANNOUNCEMENT_BUTTONS[buttonId].setResult("The announcement couldn't be played.");
  });
  resumeAnnouncement(player, buttonId);
}

// An announcement on its way to be checked. Buttons stay enabled while it is,
// so they keep focus; this stops it being sent twice.
let fallbackAnnouncementSending = false;

async function sendFallbackAnnouncement(blob, setResult) {
  if (blob.size >= FALLBACK_AUDIO_MAX_BYTES) {
    throw new Error("The file is 10 MB or larger. Choose a file under 10 MB.");
  }
  fallbackAnnouncementSending = true;
  setResult("Checking the announcement...");
  try {
    const data = await request("/api/fallback-audio", {
      method: "POST",
      headers: {"Content-Type": "application/octet-stream"},
      body: blob
    });
    fallbackCustomChosenWithoutAudio = false;
    // The custom announcement may have just been replaced.
    if (announcementPlayerButton === "fallback_play") stopAnnouncementPlayback();
    applyStatus(data, {syncControls: false});
    setFallbackControls(data.fallback);
    return data;
  } finally {
    fallbackAnnouncementSending = false;
  }
}

function chooseFallbackAnnouncementFile() {
  if (fallbackAnnouncementSending) return;
  const input = document.getElementById("fallback_audio_file");
  // Cleared first, so choosing the same file again still uploads it.
  input.value = "";
  input.click();
}

async function uploadFallbackAnnouncementFile() {
  const input = document.getElementById("fallback_audio_file");
  const file = input.files && input.files[0];
  if (!file || fallbackAnnouncementSending) return;
  try {
    await sendFallbackAnnouncement(file, setFallbackAudioResult);
    setFallbackAudioResult("Announcement uploaded. Fallback audio now plays the custom announcement.", "success");
  } catch (error) {
    setFallbackAudioResult(error.message, "error");
  }
}

function microphoneErrorMessage(error) {
  const name = error && error.name;
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "Microphone access was blocked. To record, allow this site to use your microphone in your browser's site settings, then select Start recording.";
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return "No microphone was found. Connect a microphone, then select Start recording.";
  }
  if (name === "NotReadableError" || name === "AbortError") {
    return "Your microphone couldn't be used. Another app may be using it.";
  }
  return `The microphone couldn't be used: ${error && error.message ? error.message : error}`;
}

// Some browsers report a blocked microphone as another kind of error.
async function microphoneBlocked() {
  try {
    const permission = await navigator.permissions.query({name: "microphone"});
    return permission.state === "denied";
  } catch (error) {
    return false;
  }
}

function renderFallbackRecordingAvailability() {
  // Browsers only allow microphone access in a secure context.
  const insecure = !window.isSecureContext;
  const unsupported = !insecure && (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.AudioContext || !window.AudioWorkletNode);
  setHidden("fallback_record_insecure", !insecure);
  setHidden("fallback_record_unsupported", !unsupported);
  setHidden("fallback_record_controls", insecure || unsupported);
}

function setFallbackRecordingResult(message, kind = "") {
  setMessage("fallback-recording-result", message, kind);
}

// The recording screen holds the microphone from when it opens, so the browser
// asks for it then, and recording starts only when Start recording is selected.
let fallbackRecordingPageOpen = false;
let fallbackMicrophone = null;
let fallbackMicrophoneRequest = null;
let fallbackRecorderStarting = false;

function fallbackRecordingUnsaved() {
  return Boolean(fallbackRecorder || fallbackRecording);
}

function confirmLeavingFallbackRecording() {
  if (!fallbackRecordingUnsaved()) return true;
  if (!window.confirm("Leave this page? Your recording hasn't been saved.")) return false;
  endFallbackRecordingPage();
  return true;
}

function syncFallbackRecordingPage(section) {
  const open = section === "record_announcement";
  if (open === fallbackRecordingPageOpen) return;
  if (!open) {
    endFallbackRecordingPage();
    return;
  }
  fallbackRecordingPageOpen = true;
  renderFallbackRecordButton();
  setFallbackRecordingResult("");
  requestFallbackMicrophone().catch(() => {});
}

function endFallbackRecordingPage() {
  fallbackRecordingPageOpen = false;
  if (fallbackRecorder) {
    const recorder = fallbackRecorder;
    fallbackRecorder = null;
    releaseFallbackRecorder(recorder);
  }
  discardFallbackRecording();
  releaseFallbackMicrophone();
  renderFallbackRecordButton();
}

function releaseFallbackMicrophone() {
  if (fallbackMicrophone) {
    for (const track of fallbackMicrophone.getTracks()) track.stop();
  }
  fallbackMicrophone = null;
}

// The microphone, asking for it if this screen doesn't have it yet.
function requestFallbackMicrophone() {
  if (fallbackMicrophone && fallbackMicrophone.getAudioTracks().some(track => track.readyState === "live")) {
    return Promise.resolve(fallbackMicrophone);
  }
  if (fallbackMicrophoneRequest) return fallbackMicrophoneRequest;
  fallbackMicrophoneRequest = (async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: false}});
      if (!fallbackRecordingPageOpen) {
        // Left the screen while the browser was asking.
        for (const track of stream.getTracks()) track.stop();
        throw new Error("The recording screen was closed.");
      }
      fallbackMicrophone = stream;
      if (!fallbackRecorder && !fallbackRecording) {
        setFallbackRecordingResult("Your microphone is ready. Select Start recording when you're ready to speak.");
      }
      return stream;
    } catch (error) {
      if (fallbackRecordingPageOpen) {
        setFallbackRecordingResult(microphoneErrorMessage(await microphoneBlocked() ? {name: "NotAllowedError"} : error), "error");
      }
      throw error;
    } finally {
      fallbackMicrophoneRequest = null;
    }
  })();
  return fallbackMicrophoneRequest;
}

// One button starts, stops and records again, so it keeps focus throughout.
function renderFallbackRecordButton() {
  const text = fallbackRecorder ? "Stop recording" : fallbackRecording ? "Record again" : "Start recording";
  setText("fallback_record", text);
  setHidden("fallback_record_time", !fallbackRecorder);
  setHidden("fallback_recording_review", !fallbackRecording || Boolean(fallbackRecorder));
}

function updateFallbackRecordTime() {
  const recorder = fallbackRecorder;
  if (!recorder) return;
  setText("fallback_record_time", `${formatDuration(recorder.samples / recorder.sampleRate)} of ${formatDuration(FALLBACK_RECORDING_MAX_SECONDS)}`);
}

function releaseFallbackRecorder(recorder) {
  clearInterval(recorder.timer);
  recorder.node.port.onmessage = null;
  recorder.context.close().catch(() => {});
}

function discardFallbackRecording() {
  if (fallbackRecording) {
    if (announcementPlayerButton === "fallback_recording_play") stopAnnouncementPlayback();
    URL.revokeObjectURL(fallbackRecording.url);
  }
  fallbackRecording = null;
}

async function startFallbackRecording() {
  if (fallbackRecorder || fallbackRecorderStarting) return;
  fallbackRecorderStarting = true;
  try {
    stopAnnouncementPlayback();
    let stream;
    try {
      stream = await requestFallbackMicrophone();
    } catch (error) {
      return;
    }
    const context = new AudioContext();
    try {
      const moduleUrl = URL.createObjectURL(new Blob([FALLBACK_RECORDER_WORKLET], {type: "application/javascript"}));
      try {
        await context.audioWorklet.addModule(moduleUrl);
      } finally {
        URL.revokeObjectURL(moduleUrl);
      }
      const source = context.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(context, "announcement-recorder");
      // Connected to the speakers at zero volume so the browser keeps it running.
      const silent = context.createGain();
      silent.gain.value = 0;
      source.connect(node);
      node.connect(silent);
      silent.connect(context.destination);
      const recorder = {
        context,
        node,
        chunks: [],
        samples: 0,
        sampleRate: context.sampleRate,
        maxSamples: Math.round(FALLBACK_RECORDING_MAX_SECONDS * context.sampleRate),
        timer: null
      };
      node.port.onmessage = event => {
        if (fallbackRecorder !== recorder) return;
        const room = recorder.maxSamples - recorder.samples;
        const chunk = event.data.length > room ? event.data.subarray(0, room) : event.data;
        recorder.chunks.push(chunk);
        recorder.samples += chunk.length;
        if (recorder.samples >= recorder.maxSamples) stopFallbackRecording(true);
      };
      if (context.state === "suspended") await context.resume();
      if (!fallbackRecordingPageOpen) {
        context.close().catch(() => {});
        return;
      }
      // Recording again replaces the last recording.
      discardFallbackRecording();
      fallbackRecorder = recorder;
      recorder.timer = setInterval(updateFallbackRecordTime, 250);
      updateFallbackRecordTime();
      renderFallbackRecordButton();
      setFallbackRecordingResult("Recording. Select Stop recording when you've finished. Recording stops by itself after one minute.");
    } catch (error) {
      context.close().catch(() => {});
      setFallbackRecordingResult(`Recording couldn't start: ${error.message}`, "error");
    }
  } finally {
    fallbackRecorderStarting = false;
  }
}

function encodeMonoWav(chunks, sampleCount, sampleRate) {
  const buffer = new ArrayBuffer(44 + sampleCount * 2);
  const view = new DataView(buffer);
  const text = (offset, value) => {
    for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i));
  };
  text(0, "RIFF");
  view.setUint32(4, 36 + sampleCount * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, sampleCount * 2, true);
  let offset = 44;
  for (const chunk of chunks) {
    for (let i = 0; i < chunk.length; i++) {
      const sample = Math.max(-1, Math.min(1, chunk[i]));
      view.setInt16(offset, Math.round(sample * 32767), true);
      offset += 2;
    }
  }
  return new Blob([buffer], {type: "audio/wav"});
}

function stopFallbackRecording(reachedLimit = false) {
  const recorder = fallbackRecorder;
  if (!recorder) return;
  fallbackRecorder = null;
  releaseFallbackRecorder(recorder);
  const seconds = recorder.samples / recorder.sampleRate;
  if (seconds < 0.5) {
    renderFallbackRecordButton();
    setFallbackRecordingResult("The recording was too short. Select Start recording to try again.", "error");
    return;
  }
  const blob = encodeMonoWav(recorder.chunks, recorder.samples, recorder.sampleRate);
  fallbackRecording = {blob, url: URL.createObjectURL(blob), seconds};
  renderFallbackRecordButton();
  setFallbackRecordingResult(
    `${reachedLimit ? "Recording stopped at the one minute limit. " : ""}Recorded ${formatDuration(seconds)}. `
    + "Select Play recording to listen to it, then Save recording to use it. Record again replaces it."
  );
}

async function saveFallbackRecording() {
  if (!fallbackRecording || fallbackAnnouncementSending) return;
  stopAnnouncementPlayback();
  try {
    await sendFallbackAnnouncement(fallbackRecording.blob, setFallbackRecordingResult);
  } catch (error) {
    setFallbackRecordingResult(error.message, "error");
    return;
  }
  // Saved, so there is nothing left to lose by leaving.
  endFallbackRecordingPage();
  leaveSettingsSubpage();
  // Once Fallback audio is showing again, so it is read out there.
  setTimeout(() => setFallbackAudioResult("Recording saved. Fallback audio now plays the custom announcement.", "success"), 300);
}

function setFallbackResult(message, kind = "") {
  const element = document.getElementById("fallback-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setArchiveResult(message, kind = "") {
  const element = document.getElementById("archive-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

// Stream archives: play a stream's archive, or choose a time and download it. Where the
// browser can't play MP3, downloading is all there is, as it is without the page script.
let streamArchiveStreamId = "";
let streamArchives = [];
let streamArchiveDownloadMode = false;
// The player: its address, length, and the times in it, as /api/stream-archive-player gives.
let streamArchivePlayer = null;
let streamArchivePlayerStreamId = "";
let streamArchiveIsPlaying = false;
// Whether the lock screen's controls are the archive's; another player starting takes them.
let streamArchiveOwnsMediaSession = false;
let streamArchivePlaybackVolume = 100;
// Where to be once the audio has loaded enough to go there.
let streamArchiveWantedSeconds = 0;
let streamArchiveSliderActive = false;
let streamArchiveRecovering = false;
// The lock screen's title and controls, set once when the archive takes them: iOS drops a
// paused page's Now Playing if they're set anew.
let streamArchiveMediaSessionSet = false;
// The audio failed while the page was hidden, such as when iOS cut its connection after a
// pause; a new player is opened when it's played again, not while the phone is locked.
let streamArchiveNeedsRecovery = false;
const STREAM_ARCHIVE_SKIP_SECONDS = 10;
const STREAM_ARCHIVE_PAGE_SECONDS = 600;
const STREAM_ARCHIVE_MP3_BYTES_PER_SECOND = 8000; // 64 kbps
// At the end of a player, newer audio than this goes on playing; less, and it's caught up.
const STREAM_ARCHIVE_CAUGHT_UP_SECONDS = 15;

function setStreamArchiveResult(message, kind = "") {
  const element = document.getElementById("stream-archive-result");
  element.className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  element.textContent = String(message || "");
}

function setStreamArchivePlayerResult(message, kind = "") {
  const element = document.getElementById("stream-archive-player-result");
  element.className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  element.textContent = String(message || "");
}

function streamArchivePlaybackSupported() {
  const audio = document.getElementById("stream_archive_audio");
  return Boolean(audio && audio.canPlayType && audio.canPlayType("audio/mpeg"));
}

function streamArchivePlayerShown() {
  return streamArchivePlaybackSupported() && !streamArchiveDownloadMode;
}

async function loadStreamArchives() {
  const data = await request("/api/stream-archives");
  streamArchives = data.streams || [];
  renderStreamArchives(true);
}

function renderStreamArchives(resetTimes) {
  const select = document.getElementById("stream_archive_stream");
  const form = document.getElementById("stream_archive_download_form");
  const playing = streamArchivePlayerShown();
  // The download page of a player's stream: the stream is chosen on the player.
  const downloadOfPlayer = streamArchivePlaybackSupported() && streamArchiveDownloadMode;
  if (!streamArchives.length) {
    select.replaceChildren();
    setText("stream_archive_available", "No stream has archived audio yet.");
    setHidden("stream_archive_gaps_section", true);
    setHidden("stream_archive_player", true);
    setHidden("stream_archive_stream_form", false);
    setHidden("stream_archive_delete_actions", true);
    form.hidden = true;
    if (streamArchiveDeletedMessage) setStreamArchiveResult(streamArchiveDeletedMessage, "success");
    streamArchiveDeletedMessage = "";
    return;
  }
  form.hidden = false;
  if (!streamArchives.some(item => item.id === streamArchiveStreamId)) streamArchiveStreamId = streamArchives[0].id;
  select.replaceChildren(...streamArchives.map(item => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.label;
    return option;
  }));
  select.value = streamArchiveStreamId;
  const archive = streamArchives.find(item => item.id === streamArchiveStreamId);
  setText("stream_archives_heading", downloadOfPlayer ? `Download archived audio from ${archive.callsign || "stream"}` : "Stream archives");
  setText("stream_archives_intro", playing
    ? "Listen to a stream's archived audio, or download it."
    : downloadOfPlayer ? "Download up to 12 hours at a time." : "Download archived audio from a stream, up to 12 hours at a time.");
  if (downloadOfPlayer) setPageTitle(`Download archived audio from ${archive.callsign || "stream"}`);
  setHidden("stream_archive_stream_form", downloadOfPlayer);
  setHidden("stream_archive_player", !playing);
  setHidden("stream_archive_download_section", playing);
  setHidden("stream_archive_back_to_player", !downloadOfPlayer);
  document.getElementById("stream_archive_open_download").href = routeForView("stream_archives", {streamId: archive.id, mode: "download"});
  document.getElementById("stream_archive_back_to_player").href = routeForView("stream_archives", {streamId: archive.id});
  // Deleting is on the player, or on the download page where that's the first page.
  const deleteLink = routeForView("stream_archive_delete", {streamId: archive.id});
  document.getElementById("stream_archive_open_delete").href = deleteLink;
  document.getElementById("stream_archive_open_delete_link").href = deleteLink;
  // Not for read-only accounts, nor until it's known the account isn't one.
  const mayDelete = Boolean(currentAccount) && !currentAccount.read_only;
  setHidden("stream_archive_open_delete", !mayDelete);
  setHidden("stream_archive_delete_actions", !mayDelete || downloadOfPlayer);
  // Where the player's shown, it says so once it's open again.
  if (streamArchiveDeletedMessage && !playing) {
    setStreamArchiveResult(streamArchiveDeletedMessage, "success");
    streamArchiveDeletedMessage = "";
  }
  setText("stream_archive_available", `Archived audio from ${archive.from_text} to ${archive.to_text} (${archive.storage}).`);
  const gaps = document.getElementById("stream_archive_gaps");
  gaps.replaceChildren(...archive.gaps.map(gap => {
    const item = document.createElement("li");
    item.textContent = gap;
    return item;
  }));
  setHidden("stream_archive_gaps", !archive.gaps.length);
  setText("stream_archive_gaps_note", archive.gaps_note);
  setHidden("stream_archive_gaps_note", !archive.gaps_note);
  setHidden("stream_archive_gaps_section", !archive.gap_count);
  document.getElementById("stream_archive_download_stream").value = archive.id;
  setText("stream_archive_time_zone", archive.time_zone || "this computer's time zone");
  setText("stream_archive_jump_zone", archive.time_zone || "this computer's time zone");
  if (resetTimes) {
    // Starts at the last hour archived, in the server's time, as the download takes it.
    for (const field of ["start_date", "start_time", "end_date", "end_time"]) {
      document.getElementById(`stream_archive_${field}`).value = archive[`default_${field}`];
    }
  }
  if (playing && streamArchivePlayerStreamId !== archive.id) {
    openStreamArchivePlayer(archive.id).catch(error => setStreamArchivePlayerResult(error.message, "error"));
  }
}

function streamArchiveCallsign() {
  const archive = streamArchives.find(item => item.id === streamArchivePlayerStreamId);
  return (archive && archive.callsign) || "stream";
}

function streamArchiveDuration(player = streamArchivePlayer) {
  return player ? Number(player.bytes || 0) / STREAM_ARCHIVE_MP3_BYTES_PER_SECOND : 0;
}

// When the audio at a place in the player was heard, from the marks the server gives:
// the times between them run on at one second a second.
function streamArchiveTimeAt(seconds, player = streamArchivePlayer) {
  const marks = player.marks || [];
  const position = Math.min(Math.max(0, seconds - player.delay_seconds), player.seconds);
  let mark = marks[0] || {seconds: 0, ms: 0};
  for (const candidate of marks) {
    if (candidate.seconds > position) break;
    mark = candidate;
  }
  // To the millisecond: a browser keeps where it's playing a hair short, and a time a hair
  // short of a second would be named as the second before.
  return Math.round(mark.ms + (position - mark.seconds) * 1000);
}

// The place in a player for a time; a time after the end of a stretch of audio is where the
// next one starts, so a newer player carries on from where an older one ended.
function streamArchiveSecondsAt(player, ms) {
  const marks = player.marks || [];
  for (let index = marks.length - 1; index >= 0; index -= 1) {
    if (marks[index].ms > ms) continue;
    const limit = index + 1 < marks.length ? marks[index + 1].seconds : player.seconds;
    return Math.min(marks[index].seconds + (ms - marks[index].ms) / 1000, limit) + player.delay_seconds;
  }
  return player.delay_seconds;
}

function streamArchiveTimeOptions() {
  const zone = streamArchivePlayer && streamArchivePlayer.time_zone_name;
  if (!zone) return {};
  try {
    new Intl.DateTimeFormat(undefined, {timeZone: zone});
    return {timeZone: zone};
  } catch (error) {
    return {};
  }
}

// In the server's time zone, as times are typed and as downloads name them.
function streamArchiveTimeText(ms) {
  const moment = new Date(ms);
  const options = streamArchiveTimeOptions();
  const date = new Intl.DateTimeFormat(undefined, {...options, month: "long", day: "numeric", year: "numeric"}).format(moment);
  const time = new Intl.DateTimeFormat(undefined, {...options, hour: "numeric", minute: "2-digit", second: "2-digit", timeZoneName: "short"}).format(moment);
  return `${date}, ${time}`;
}

// The date and time fields' values for a time, in the server's time zone.
function streamArchiveFieldValues(ms) {
  const parts = {};
  const format = new Intl.DateTimeFormat("en-US", {
    ...streamArchiveTimeOptions(),
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"
  });
  for (const part of format.formatToParts(new Date(ms))) parts[part.type] = part.value;
  return {date: `${parts.year}-${parts.month}-${parts.day}`, time: `${parts.hour}:${parts.minute}:${parts.second}`};
}

function streamArchiveCurrentSeconds() {
  const audio = document.getElementById("stream_archive_audio");
  return audio.readyState >= 1 ? audio.currentTime : streamArchiveWantedSeconds;
}

function renderStreamArchivePosition(seconds, options = {}) {
  if (!streamArchivePlayer) return;
  const slider = document.getElementById("stream_archive_position");
  const text = streamArchiveTimeText(streamArchiveTimeAt(seconds));
  setText("stream_archive_now", text);
  // Kept up to date even with focus, for speech and braille alike, but only while it's not
  // being dragged; just its value and text change, only when they do.
  if (options.fromUser || !streamArchiveSliderActive) {
    const value = String(seconds);
    if (slider.value !== value) slider.value = value;
    if (slider.getAttribute("aria-valuetext") !== text) slider.setAttribute("aria-valuetext", text);
  }
}

function useStreamArchivePlayer(streamId, player, seconds) {
  const audio = document.getElementById("stream_archive_audio");
  streamArchivePlayer = player;
  streamArchivePlayerStreamId = streamId;
  const duration = streamArchiveDuration(player);
  const start = Math.min(Math.max(0, seconds), duration);
  streamArchiveWantedSeconds = start;
  audio.src = player.url;
  try {
    // Before the audio has loaded, this is where it starts.
    audio.currentTime = start;
  } catch (error) {
    console.debug("archive start position not set yet", error);
  }
  const slider = document.getElementById("stream_archive_position");
  slider.max = String(duration);
  renderStreamArchivePosition(start, {fromUser: true});
}

async function openStreamArchivePlayer(streamId) {
  pauseStreamArchive({forOther: true});
  streamArchivePlayerStreamId = streamId;
  setStreamArchivePlayerResult("");
  const player = await request(`/api/stream-archive-player?stream=${encodeURIComponent(streamId)}`);
  if (streamArchivePlayerStreamId !== streamId) return;
  // Starts an hour before the newest audio, or at the start of a shorter archive.
  useStreamArchivePlayer(streamId, player, streamArchiveDuration(player) - 3600);
  setStreamArchivePlaybackState(false);
  if (streamArchiveDeletedMessage) setStreamArchivePlayerResult(streamArchiveDeletedMessage, "success");
  streamArchiveDeletedMessage = "";
  // Jump to time starts out at where it's playing.
  const fields = streamArchiveFieldValues(streamArchiveTimeAt(streamArchiveWantedSeconds));
  document.getElementById("stream_archive_jump_date").value = fields.date;
  document.getElementById("stream_archive_jump_time").value = fields.time;
}

// A new player for the same stream, at the same time, for when this one has closed or ended.
async function reopenStreamArchivePlayer() {
  const streamId = streamArchivePlayerStreamId;
  const player = await request(`/api/stream-archive-player?stream=${encodeURIComponent(streamId)}`);
  if (streamArchivePlayerStreamId !== streamId) return null;
  return player;
}

async function playStreamArchive() {
  const audio = document.getElementById("stream_archive_audio");
  if (!streamArchivePlayer || !audio.src) return;
  if (streamArchiveNeedsRecovery || audio.error) {
    streamArchiveNeedsRecovery = false;
    await recoverStreamArchivePlayer({play: true});
    return;
  }
  pauseCurrentEasAlert();
  if (monitorPeerConnection || receiverPeerConnection) {
    await stopStreamMonitor({notifyServer: true});
    await stopWeatherReceiver({notifyServer: true});
  }
  streamArchiveOwnsMediaSession = true;
  audio.volume = playbackVolumeScalar(streamArchivePlaybackVolume);
  if (audio.ended) audio.currentTime = streamArchiveWantedSeconds;
  await audio.play();
}

function pauseStreamArchive(options = {}) {
  const audio = document.getElementById("stream_archive_audio");
  // Another player is starting: the lock screen's controls become its.
  if (options.forOther) {
    streamArchiveOwnsMediaSession = false;
    streamArchiveMediaSessionSet = false;
  }
  if (audio && !audio.paused) audio.pause();
}

function seekStreamArchive(seconds) {
  if (!streamArchivePlayer) return;
  const audio = document.getElementById("stream_archive_audio");
  const target = Math.min(Math.max(0, seconds), streamArchiveDuration());
  streamArchiveWantedSeconds = target;
  try {
    audio.currentTime = target;
  } catch (error) {
    console.debug("archive seek failed", error);
  }
  renderStreamArchivePosition(target, {fromUser: true});
  updateStreamArchivePositionState();
}

function skipStreamArchive(seconds) {
  seekStreamArchive(streamArchiveCurrentSeconds() + seconds);
}

function setStreamArchivePlaybackState(playing) {
  streamArchiveIsPlaying = Boolean(playing);
  const label = streamArchiveIsPlaying ? "Pause" : "Play";
  const button = document.getElementById("stream_archive_play_pause");
  button.setAttribute("aria-label", label);
  button.title = label;
  setHidden("stream_archive_play_icon", streamArchiveIsPlaying);
  setHidden("stream_archive_pause_icon", !streamArchiveIsPlaying);
  updateStreamArchiveMediaSession();
}

// The lock screen shows the clock hour being played, not the whole archive, which can be a
// year long: its elapsed time is the minutes and seconds into the hour, as the clock says.
function streamArchiveHour(seconds = streamArchiveCurrentSeconds()) {
  const ms = streamArchiveTimeAt(seconds);
  const parts = {};
  const format = new Intl.DateTimeFormat("en-US", {...streamArchiveTimeOptions(), minute: "2-digit", second: "2-digit"});
  for (const part of format.formatToParts(new Date(ms))) parts[part.type] = part.value;
  const into = Number(parts.minute) * 60 + Number(parts.second) + (ms % 1000) / 1000;
  const startMs = ms - into * 1000;
  const options = streamArchiveTimeOptions();
  const date = new Intl.DateTimeFormat(undefined, {...options, month: "long", day: "numeric", year: "numeric"}).format(new Date(startMs));
  const hour = new Intl.DateTimeFormat(undefined, {...options, hour: "numeric", timeZoneName: "short"}).format(new Date(startMs));
  return {startMs, into: Math.min(Math.max(0, into), 3600), text: `${date}, ${hour}`};
}

// The hour's in the title: iOS shows a title and artist, but not an album.
function streamArchiveMediaTitle(hour) {
  return `Archive of ${streamArchiveCallsign()}, ${hour.text}`;
}

function updateStreamArchivePositionState() {
  if (!streamArchiveOwnsMediaSession || !streamArchivePlayer || !("mediaSession" in navigator) || typeof navigator.mediaSession.setPositionState !== "function") return;
  const hour = streamArchiveHour();
  // Paused in another hour than the lock screen names: both change when it plays again.
  const current = navigator.mediaSession.metadata;
  if (!streamArchiveIsPlaying && current && current.title !== streamArchiveMediaTitle(hour)) return;
  try {
    navigator.mediaSession.setPositionState({duration: 3600, playbackRate: 1, position: hour.into});
  } catch (error) {
    console.debug("archive media session position update failed", error);
  }
}

function logStreamArchiveEvent(event, details = {}) {
  const audio = document.getElementById("stream_archive_audio");
  logClientEvent("info", "archive-player", event, {
    ...details,
    visibility: document.visibilityState,
    paused: audio.paused,
    current_time: Math.round(audio.currentTime * 10) / 10,
    ready_state: audio.readyState,
    network_state: audio.networkState,
    error: audio.error ? audio.error.code : null
  });
}

function setStreamArchiveMediaAction(action, handler) {
  try {
    if (handler) {
      const original = handler;
      handler = details => {
        logStreamArchiveEvent(`lock screen ${action}`);
        original(details);
      };
    }
    navigator.mediaSession.setActionHandler(action, handler);
  } catch (error) {
    console.debug(`archive media session action ${action} is not supported`, error);
  }
}

function updateStreamArchiveMediaSession() {
  if (!streamArchiveOwnsMediaSession || !streamArchivePlayer) return;
  if (!("mediaSession" in navigator) || !("MediaMetadata" in window)) return;
  if (monitorPeerConnection || receiverPeerConnection) return;
  navigator.mediaSession.playbackState = streamArchiveIsPlaying ? "playing" : "paused";
  const title = streamArchiveMediaTitle(streamArchiveHour());
  const current = navigator.mediaSession.metadata;
  const changed = !current || current.title !== title;
  // Changed only while playing, as the hour turns over: iOS drops a paused page's Now
  // Playing if it's set anew then.
  if (!streamArchiveMediaSessionSet || (changed && streamArchiveIsPlaying)) {
    navigator.mediaSession.metadata = new MediaMetadata({title, artist: "NWR Stream Manager"});
  }
  updateStreamArchivePositionState();
  if (streamArchiveMediaSessionSet) return;
  streamArchiveMediaSessionSet = true;
  setStreamArchiveMediaAction("previoustrack", null);
  setStreamArchiveMediaAction("nexttrack", null);
  setStreamArchiveMediaAction("play", () => {
    playStreamArchive().catch(error => setStreamArchivePlayerResult(`The archived audio couldn't start: ${error.message}`, "error"));
  });
  setStreamArchiveMediaAction("pause", () => pauseStreamArchive());
  setStreamArchiveMediaAction("seekbackward", event => skipStreamArchive(-((event && event.seekOffset) || STREAM_ARCHIVE_SKIP_SECONDS)));
  setStreamArchiveMediaAction("seekforward", event => skipStreamArchive((event && event.seekOffset) || STREAM_ARCHIVE_SKIP_SECONDS));
  // Within the hour shown; a time in it with no audio goes to where the audio picks up.
  setStreamArchiveMediaAction("seekto", event => {
    if (!event || !Number.isFinite(event.seekTime) || !streamArchivePlayer) return;
    const target = streamArchiveHour().startMs + Math.min(Math.max(0, event.seekTime), 3600) * 1000;
    seekStreamArchive(streamArchiveSecondsAt(streamArchivePlayer, target));
  });
}

// Delete archived audio: older than a choice of time, or all of it, as EAS alerts are deleted.
let streamArchiveDeleteChoices = [];
// Said on the player, or the download page, once it's shown again after deleting.
let streamArchiveDeletedMessage = "";

function setStreamArchiveDeleteResult(message, kind = "") {
  const element = document.getElementById("stream-archive-delete-result");
  element.className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  element.textContent = String(message || "");
}

async function loadStreamArchiveDeleteChoices() {
  setStreamArchiveDeleteResult("");
  if (!streamArchives.length) {
    const data = await request("/api/stream-archives");
    streamArchives = data.streams || [];
  }
  const archive = streamArchives.find(item => item.id === streamArchiveStreamId) || streamArchives[0];
  const container = document.getElementById("stream_archive_delete_options");
  if (!archive) {
    setText("stream_archive_delete_stream_label", "No stream has archived audio yet.");
    container.replaceChildren();
    setDisabled(document.getElementById("stream_archive_delete_audio"), true);
    return;
  }
  streamArchiveStreamId = archive.id;
  setText("stream_archive_delete_stream_label", `Stream: ${archive.label}`);
  const data = await request(`/api/stream-archive-delete-options?stream=${encodeURIComponent(archive.id)}`);
  streamArchiveDeleteChoices = data.choices || [];
  container.replaceChildren(...streamArchiveDeleteChoices.map((choice, index) => {
    const label = document.createElement("label");
    label.className = "checkbox-row";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "choice";
    input.value = choice.id;
    input.checked = index === 0;
    label.append(input, ` ${choice.label} (${choice.storage})`);
    return label;
  }));
  setDisabled(document.getElementById("stream_archive_delete_audio"), !streamArchiveDeleteChoices.length);
}

async function deleteStreamArchiveAudio() {
  if (accountIsReadOnly()) return;
  const selected = document.querySelector("#stream_archive_delete_options input[name='choice']:checked");
  const choice = streamArchiveDeleteChoices.find(item => selected && item.id === selected.value);
  if (!choice) return;
  if (!window.confirm(`${choice.storage} of archived audio will be permanently deleted.`)) {
    navigateTo("stream_archives", {streamId: streamArchiveStreamId}, true, true);
    return;
  }
  try {
    const response = await request("/api/stream-archive-delete", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({stream: streamArchiveStreamId, choice: choice.id})
    });
    // The player opens again on what's left.
    pauseStreamArchive();
    streamArchivePlayerStreamId = "";
    streamArchivePlayer = null;
    streamArchives = [];
    streamArchiveDeletedMessage = response.message || "";
    navigateTo("stream_archives", {streamId: streamArchiveStreamId}, true, true);
  } catch (error) {
    setStreamArchiveDeleteResult(error.message, "error");
  }
}

// At the end of a player: carry on in a newer one if there's newer audio enough, or stop,
// caught up with the archive.
async function continueStreamArchive() {
  const endMs = streamArchiveTimeAt(streamArchiveDuration());
  try {
    const player = await reopenStreamArchivePlayer();
    if (!player) return;
    const resume = streamArchiveSecondsAt(player, endMs);
    if (streamArchiveDuration(player) - resume < STREAM_ARCHIVE_CAUGHT_UP_SECONDS) {
      setStreamArchivePlaybackState(false);
      setStreamArchivePlayerResult("You've caught up to the newest archived audio.", "success");
      return;
    }
    useStreamArchivePlayer(streamArchivePlayerStreamId, player, resume);
    await playStreamArchive();
  } catch (error) {
    setStreamArchivePlaybackState(false);
    setStreamArchivePlayerResult(`The archived audio couldn't continue: ${error.message}`, "error");
  }
}

// A player the server no longer has, such as after a restart: a new one, at the same time.
async function recoverStreamArchivePlayer(options = {}) {
  if (streamArchiveRecovering || !streamArchivePlayer) return;
  streamArchiveRecovering = true;
  const wasPlaying = options.play || streamArchiveIsPlaying;
  const atMs = streamArchiveTimeAt(streamArchiveCurrentSeconds());
  try {
    const player = await reopenStreamArchivePlayer();
    if (!player) return;
    useStreamArchivePlayer(streamArchivePlayerStreamId, player, streamArchiveSecondsAt(player, atMs));
    if (wasPlaying) await playStreamArchive();
  } catch (error) {
    setStreamArchivePlaybackState(false);
    setStreamArchivePlayerResult(`The archived audio couldn't be played: ${error.message}`, "error");
  } finally {
    window.setTimeout(() => { streamArchiveRecovering = false; }, 5000);
  }
}

async function jumpStreamArchive() {
  if (!streamArchivePlayer) return;
  const date = document.getElementById("stream_archive_jump_date").value;
  const time = document.getElementById("stream_archive_jump_time").value;
  const ask = async () => {
    const response = await fetch(`/api/stream-archive-player-time?player=${encodeURIComponent(streamArchivePlayer.id)}&date=${encodeURIComponent(date)}&time=${encodeURIComponent(time)}`);
    return {response, data: await response.json()};
  };
  let {response, data} = await ask();
  if (response.status === 409 && data.reopen) {
    // Archived since this player opened, or it's closed: a new one has it.
    const atMs = streamArchiveTimeAt(streamArchiveCurrentSeconds());
    const player = await reopenStreamArchivePlayer();
    if (!player) return;
    const wasPlaying = streamArchiveIsPlaying;
    useStreamArchivePlayer(streamArchivePlayerStreamId, player, streamArchiveSecondsAt(player, atMs));
    if (wasPlaying) await playStreamArchive();
    ({response, data} = await ask());
  }
  if (!response.ok) {
    setStreamArchivePlayerResult(data.error || response.statusText, "error");
    return;
  }
  seekStreamArchive(data.seconds + streamArchivePlayer.delay_seconds);
  setStreamArchivePlayerResult(`Jumped to ${data.text}.`, "success");
}

function setEasResult(message, kind = "") {
  const element = document.getElementById("eas-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setAudioEffectsResult(message, kind = "") {
  const element = document.getElementById("audio-effects-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setStreamNotificationsResult(message, kind = "") {
  const element = document.getElementById("stream-notifications-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setTestModeResult(message, kind = "") {
  const element = document.getElementById("test-mode-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function fallbackPayload() {
  return {loop_delay_seconds: numericControlValue("fallback_loop_delay")};
}

function filterPayload(name) {
  const frequencyElement = document.getElementById(`audio_${name}_frequency`);
  return {
    enabled: document.getElementById(`audio_${name}_enabled`).checked,
    frequency: numericControlValue(frequencyElement),
    sharpness: numericControlValue(`audio_${name}_sharpness`)
  };
}

function notchPayload() {
  return {
    enabled: document.getElementById("audio_notch_enabled").checked,
    frequency: numericControlValue("audio_notch_frequency"),
    width: numericControlValue("audio_notch_width")
  };
}

function notchMaxWidth(frequency) {
  const allowed = Math.sqrt(10 ** (PROTECTED_TONE_MAX_LOSS_DB / 10) - 1);
  let widest = Math.min(NOTCH_MAX_WIDTH_HZ, 2 * frequency);
  for (const tone of PROTECTED_AUDIO_TONES) {
    widest = Math.min(widest, allowed * Math.abs(tone ** 2 - frequency ** 2) / tone);
  }
  return Math.max(NOTCH_MIN_WIDTH_HZ, Math.floor(widest));
}

function updateNotchWidthLimit(clamp) {
  const frequency = Number(document.getElementById("audio_notch_frequency").value);
  if (!Number.isFinite(frequency) || frequency <= 0) return;
  const widest = notchMaxWidth(frequency);
  const width = document.getElementById("audio_notch_width");
  width.max = String(widest);
  document.getElementById("audio_notch_width_max").textContent = String(widest);
  if (clamp) normalizeNumericControl(width);
}

function clampNumber(value, minimum, maximum) {
  const number = Number(value);
  if (!Number.isFinite(number)) return minimum;
  return Math.min(maximum, Math.max(minimum, number));
}

function previousNumberValue(element) {
  const previous = Number(element.dataset.previousValue || element.defaultValue || element.min || 0);
  return Number.isFinite(previous) ? previous : 0;
}

function numericControlValue(elementOrId) {
  const element = typeof elementOrId === "string" ? document.getElementById(elementOrId) : elementOrId;
  if (!element) return 0;
  const raw = element.dataset.userEditing === "1"
    ? element.dataset.previousValue || element.defaultValue || element.value
    : element.value;
  const value = Number(raw);
  if (Number.isFinite(value)) return value;
  return previousNumberValue(element);
}

function normalizeNumericControl(elementOrId) {
  const element = typeof elementOrId === "string" ? document.getElementById(elementOrId) : elementOrId;
  if (!element || element.value === "") {
    if (element) delete element.dataset.userEditing;
    return;
  }
  const number = Number(element.value);
  if (!Number.isFinite(number)) {
    delete element.dataset.userEditing;
    return;
  }
  const min = element.min === "" ? -Infinity : Number(element.min);
  const max = element.max === "" ? Infinity : Number(element.max);
  const minimum = Number.isFinite(min) ? min : -Infinity;
  const maximum = Number.isFinite(max) ? max : Infinity;
  const clamped = Math.min(maximum, Math.max(minimum, number));
  const step = Number(element.step);
  const normalized = Number.isFinite(step) && step > 0 && step < 1 ? String(clamped) : String(Math.round(clamped));
  if (element.value !== normalized) element.value = normalized;
  element.dataset.previousValue = normalized;
  delete element.dataset.userEditing;
}

function normalizeAudioFrequencyControl(nameOrElement) {
  const element = typeof nameOrElement === "string" ? document.getElementById(`audio_${nameOrElement}_frequency`) : nameOrElement;
  if (!element || element.value === "") return;
  const name = audioFrequencyNameForElement(element);
  if (!name) return;
  const previous = previousNumberValue(element);
  let value = Number(element.value);
  if (!Number.isFinite(value)) return;
  if (name === "highpass") {
    value = clampNumber(value, 1, 900);
  } else if (name === "lowpass") {
    value = clampNumber(value, 2200, 12000);
  } else if (name === "notch") {
    value = clampNumber(value, 1, 12000);
    value = skipProtectedAudioBands(value, previous);
  }
  const normalized = String(Math.round(value));
  if (element.value !== normalized) element.value = normalized;
  element.dataset.previousValue = normalized;
  if (name === "notch") updateNotchWidthLimit(true);
}

function skipProtectedAudioBands(value, previous) {
  for (const band of PROTECTED_AUDIO_BANDS) {
    if (value < band.min || value > band.max) continue;
    if (previous < band.min) return band.max + 1;
    if (previous > band.max) return band.min - 1;
    const downDistance = Math.abs(value - (band.min - 1));
    const upDistance = Math.abs((band.max + 1) - value);
    return upDistance <= downDistance ? band.max + 1 : band.min - 1;
  }
  return value;
}

function normalizeAudioFrequencyInput(event) {
  const target = event.target;
  if (!target || !target.id) return false;
  const match = target.id.match(/^audio_(highpass|lowpass|notch)_frequency$/);
  if (!match) return false;
  normalizeAudioFrequencyControl(match[1]);
  return true;
}

function audioFrequencyNameForElement(element) {
  if (!element || !element.id) return "";
  const match = element.id.match(/^audio_(highpass|lowpass|notch)_frequency$/);
  return match ? match[1] : "";
}

function isTextEditingInputEvent(event) {
  return event && typeof event.inputType === "string" && (
    event.inputType.startsWith("insert") ||
    event.inputType.startsWith("delete")
  );
}

function isTextEditingKey(event) {
  if (!event || event.ctrlKey || event.altKey || event.metaKey) return false;
  return event.key.length === 1 || ["Backspace", "Delete"].includes(event.key);
}

function isNumericControl(element) {
  return Boolean(element && element.tagName === "INPUT" && element.type === "number");
}

function beginNumericTextEdit(element) {
  if (!isNumericControl(element)) return false;
  if (element.dataset.userEditing !== "1") {
    element.dataset.previousValue = element.value || element.dataset.previousValue || element.defaultValue || element.min || "0";
  }
  element.dataset.userEditing = "1";
  return true;
}

function commitNumericControlElement(element, saveCallback = null) {
  if (!isNumericControl(element)) return false;
  if (audioFrequencyNameForElement(element)) {
    commitAudioFrequencyElement(element, false);
  } else {
    normalizeNumericControl(element);
  }
  if (typeof saveCallback === "function") saveCallback();
  return true;
}

function numericSaveCallbackForElement(element) {
  if (!element || !element.id) return null;
  if (controls.includes(element.id)) return () => scheduleUpdate(element.id);
  if (element.id === "fallback_loop_delay") return scheduleFallbackUpdate;
  if (element.id === "stream_fallback_delay") return scheduleStreamFallbackActionUpdate;
  if (["eas_pre_seconds", "eas_post_seconds", "eas_max_seconds"].includes(element.id)) return scheduleEasUpdate;
  if (element.id.startsWith("audio_")) return scheduleAudioEffectsUpdate;
  if (element.id.startsWith("test_tone")) return scheduleTestTonesUpdate;
  return null;
}

function commitAudioFrequencyElement(element, save = true) {
  const name = audioFrequencyNameForElement(element);
  if (!name) return false;
  normalizeAudioFrequencyControl(element);
  delete element.dataset.userEditing;
  if (save) scheduleAudioEffectsUpdate();
  return true;
}

function audioEffectsPayload() {
  return {
    volume: {
      enabled: document.getElementById("audio_volume_enabled").checked,
      multiplier: numericControlValue("audio_volume_multiplier")
    },
    comfort_noise: {
      enabled: document.getElementById("audio_comfort_noise_enabled").checked,
      level_db: numericControlValue("audio_comfort_noise_level")
    },
    highpass: filterPayload("highpass"),
    lowpass: filterPayload("lowpass"),
    notch: notchPayload()
  };
}

function streamNotificationSettingsForStream(stream) {
  const settings = stream && stream.notifications ? stream.notifications : {};
  return {
    icecast_failures: Boolean(settings.icecast_failures),
    soundcard_failures: Boolean(settings.soundcard_failures),
    recorded_eas_alerts: Boolean(settings.recorded_eas_alerts),
    bad_reception: Boolean(settings.bad_reception)
  };
}

function streamNotificationsPayload() {
  return {
    icecast_failures: document.getElementById("stream_notify_icecast_failures").checked,
    soundcard_failures: document.getElementById("stream_notify_soundcard_failures").checked,
    recorded_eas_alerts: document.getElementById("stream_notify_eas_alerts").checked,
    bad_reception: document.getElementById("stream_notify_bad_reception").checked
  };
}

function pageTestModeClientId() {
  if (!streamTestModeClientId) {
    streamTestModeClientId = window.crypto && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  }
  return streamTestModeClientId;
}

function testModeSignalValue() {
  const value = Number(document.getElementById("test_mode_signal").value);
  return Number.isFinite(value) ? value : -20;
}

function activeTestModeBelongsToThisPage() {
  return Boolean(
    streamTestMode &&
    streamTestMode.active &&
    streamTestMode.client_id === pageTestModeClientId()
  );
}

function activeTestModeForCurrentStream() {
  return Boolean(streamTestMode && streamTestMode.active && streamTestMode.stream_id === settingsStreamId);
}

function testModeVisibleForCurrentPage() {
  return Boolean(!streamTestMode || !streamTestMode.active || activeTestModeBelongsToThisPage());
}

function updateTestModeSignalText() {
  setText("test_mode_signal_value", `${testModeSignalValue()} dBFS`);
}

function setStreamTestModeStatus(status) {
  streamTestMode = status || {active: false};
  if (streamTestMode.time && Number.isFinite(streamTestMode.time.now)) {
    testModeClockOffset = streamTestMode.time.now - Date.now() / 1000;
  }
  if (settingsStreamId) setTestModeControls();
  updateStreamTestModeHeartbeat();
}

function setTestModeControls() {
  const stream = currentSettingsStream();
  const visible = testModeVisibleForCurrentPage();
  setHidden("tab_test_mode", !visible);
  if (!visible) {
    closeUnavailableStreamSettingsSection("test_mode");
    setTestModeResult("");
    setTestToneControls(false);
    streamTestModeSignature = "";
    return;
  }
  const activeForStream = activeTestModeForCurrentStream();
  const sameActive = activeForStream && Boolean(streamTestMode.same_active);
  const stopping = activeForStream && Boolean(streamTestMode.stopping);
  setTestToneControls(activeForStream && !stopping);
  const nextSignature = JSON.stringify({
    stream_id: settingsStreamId || "",
    enabled: streamIsEnabled(stream),
    visible,
    active_stream_id: streamTestMode.stream_id || "",
    active_client_id: streamTestMode.client_id || "",
    signal_dbfs: streamTestMode.signal_dbfs,
    same_active: sameActive,
    stopping,
    time_used_up: testModeTimeUsedUp(),
    available_at: Math.round(testModeAvailableAt() || 0)
  });
  renderTestModeTime();
  if (nextSignature === streamTestModeSignature) return;
  const timeUsedUp = testModeTimeUsedUp();
  testModeTimeUsedUpShown = timeUsedUp;
  if (activeForStream) setValue("test_mode_signal", streamTestMode.signal_dbfs ?? -20);
  setChecked("test_mode_enabled", activeForStream);
  setDisabled(
    document.getElementById("test_mode_enabled"),
    !stream || !streamIsEnabled(stream) || sameActive || stopping || (!activeForStream && timeUsedUp)
  );
  setDisabled(document.getElementById("test_mode_signal"), !activeForStream || stopping);
  setDisabled(document.getElementById("test_mode_same"), !activeForStream || sameActive || stopping);
  updateTestModeSignalText();
  if (activeForStream) {
    setTestModeResult(stopping ? "Test mode is stopping." : sameActive ? "SAME test is running." : "Test mode is active.", "success");
  } else if (streamTestMode.active) {
    setTestModeResult("Test mode is active for another stream.", "");
  } else if (timeUsedUp) {
    setTestModeResult(`You've hit your time limit for test mode.${testModeMoreTimeText()}`, "");
  } else {
    setTestModeResult("");
  }
  streamTestModeSignature = nextSignature;
}

function testModeTime() {
  return (streamTestMode && streamTestMode.time) || {limit_seconds: 600, window_seconds: 3600, usage: []};
}

function testModeServerNow() {
  return Date.now() / 1000 + testModeClockOffset;
}

function testModeUsage(now) {
  return (testModeTime().usage || []).map(([start, end]) => [start, end === null ? now : end]);
}

function testModeSecondsLeft() {
  const time = testModeTime();
  const now = testModeServerNow();
  const windowStart = now - time.window_seconds;
  let used = 0;
  for (const [start, end] of testModeUsage(now)) {
    used += Math.max(0, Math.min(end, now) - Math.max(start, windowStart));
  }
  return Math.max(0, time.limit_seconds - used);
}

function testModeAvailableAt() {
  if (testModeSecondsLeft() > 0) return null;
  const time = testModeTime();
  const now = testModeServerNow();
  const starts = testModeUsage(now).filter(([, end]) => end > now - time.window_seconds).map(([start]) => start);
  return starts.length ? Math.max(now, Math.min(...starts) + time.window_seconds) : now;
}

function testModeTimeUsedUp() {
  return !activeTestModeForCurrentStream() && testModeSecondsLeft() <= 0;
}

function formatTestModeDuration(seconds) {
  const whole = Math.ceil(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function testModeMoreTimeText() {
  const availableAt = testModeAvailableAt();
  if (!availableAt) return "";
  // Shown in the browser's own clock.
  const when = new Date((availableAt - testModeClockOffset) * 1000).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short"
  });
  return ` More time will be available at ${when}.`;
}

function renderTestModeTime() {
  const time = testModeTime();
  setText("test_mode_time", `Time left: ${formatTestModeDuration(testModeSecondsLeft())} of ${formatTestModeDuration(time.limit_seconds)}.`);
}

function tickTestModeTime() {
  if (!settingsStreamId || document.getElementById("panel_test_mode").hidden) return;
  renderTestModeTime();
  if (testModeTimeUsedUp() !== testModeTimeUsedUpShown) {
    streamTestModeSignature = "";
    setTestModeControls();
  }
}

function setTestToneControls(show) {
  const section = document.getElementById("test_tones_section");
  if (!show) {
    // Test mode is off, so leave any open tone generator page and hide the generators.
    const layout = document.getElementById("test_tones_layout");
    const hadFocus = section.contains(document.activeElement) ||
      document.getElementById("test_tones_mute").contains(document.activeElement);
    layout.classList.remove("effect-detail-active");
    setHidden("test_tones_section", true);
    setHidden("test_tones_mute", true);
    if (hadFocus) document.getElementById("test_mode_enabled").focus();
    testTonesSignature = "";
    return;
  }
  setHidden("test_tones_section", false);
  setHidden("test_tones_mute", false);
  const tones = streamTestMode.tones || {};
  const nextSignature = JSON.stringify(tones);
  if (nextSignature === testTonesSignature) return;
  const generators = tones.generators || {};
  setChecked("test_tones_muted", Boolean(tones.muted));
  for (const name of TEST_TONE_NAMES) {
    const settings = generators[name] || {enabled: false, frequency: 440, amplitude: 25};
    setChecked(`test_tone_${name}_enabled`, Boolean(settings.enabled));
    setValue(`test_tone_${name}_frequency`, settings.frequency);
    setValue(`test_tone_${name}_amplitude`, settings.amplitude);
  }
  testTonesSignature = nextSignature;
}

function selectTestTone(name, showDetail = true) {
  selectedTestTone = name;
  for (const button of document.querySelectorAll("[data-test-tone]")) {
    button.setAttribute("aria-current", button.dataset.testTone === name ? "true" : "false");
  }
  for (const panel of document.querySelectorAll(".test-tone-panel")) {
    panel.hidden = panel.id !== `test_tone_${name}`;
  }
  document.getElementById("test_tones_layout").classList.toggle("effect-detail-active", showDetail);
}

function testTonesPayload() {
  const generators = {};
  for (const name of TEST_TONE_NAMES) {
    generators[name] = {
      enabled: document.getElementById(`test_tone_${name}_enabled`).checked,
      frequency: numericControlValue(`test_tone_${name}_frequency`),
      amplitude: numericControlValue(`test_tone_${name}_amplitude`)
    };
  }
  return {muted: document.getElementById("test_tones_muted").checked, generators};
}

function scheduleTestTonesUpdate() {
  if (!activeTestModeForCurrentStream()) return;
  clearTimeout(testTonesUpdateTimer);
  testTonesUpdateTimer = setTimeout(async () => {
    try {
      await sendStreamTestMode("tones", {tones: testTonesPayload()});
    } catch (error) {
      setTestModeResult(error.message, "error");
    }
  }, 150);
}

function easPayload() {
  const selectedFormat = document.querySelector("input[name='eas_format']:checked");
  return {
    enabled: document.getElementById("eas_enabled").checked,
    pre_seconds: numericControlValue("eas_pre_seconds"),
    post_seconds: numericControlValue("eas_post_seconds"),
    max_seconds: numericControlValue("eas_max_seconds"),
    format: selectedFormat ? selectedFormat.value : "wav"
  };
}

function defaultEasSettings() {
  return {enabled: false, pre_seconds: 2, post_seconds: 5, max_seconds: 120, format: "wav"};
}

function defaultAudioEffectsSettings() {
  return {
    volume: {enabled: false, multiplier: 1.0},
    comfort_noise: {enabled: false, level_db: -40.0},
    highpass: {enabled: false, frequency: 300.0, sharpness: 0.0},
    lowpass: {enabled: false, frequency: 3400.0, sharpness: 2.0},
    notch: {enabled: false, frequency: 3000.0, width: 100.0}
  };
}

function audioEffectsSettingsForStream(stream) {
  const defaults = defaultAudioEffectsSettings();
  const audio = stream && stream.audio ? stream.audio : {};
  return {
    volume: Object.assign(defaults.volume, audio.volume || {}),
    comfort_noise: Object.assign(defaults.comfort_noise, audio.comfort_noise || {}),
    highpass: normalizeAudioFilterSettings(defaults.highpass, audio.highpass),
    lowpass: normalizeAudioFilterSettings(defaults.lowpass, audio.lowpass),
    notch: normalizeAudioFilterSettings(defaults.notch, audio.notch)
  };
}

function normalizeAudioFilterSettings(defaults, raw) {
  const settings = Object.assign({}, defaults, raw || {});
  if (!Number(settings.frequency)) settings.frequency = defaults.frequency;
  return settings;
}

function easSettingsForStream(stream) {
  return Object.assign(defaultEasSettings(), stream && stream.eas_recording ? stream.eas_recording : {});
}

function setAudioEffectsControls(stream) {
  const settings = audioEffectsSettingsForStream(stream);
  const nextSignature = JSON.stringify(settings);
  if (nextSignature === audioEffectsSignature) return;
  setChecked("audio_volume_enabled", settings.volume.enabled);
  setValue("audio_volume_multiplier", settings.volume.multiplier);
  setChecked("audio_comfort_noise_enabled", settings.comfort_noise.enabled);
  setValue("audio_comfort_noise_level", settings.comfort_noise.level_db);
  setAudioFilterControls("highpass", settings.highpass);
  setAudioFilterControls("lowpass", settings.lowpass);
  setNotchControls(settings.notch);
  audioEffectsSignature = nextSignature;
}

function setAudioFilterControls(name, settings) {
  setChecked(`audio_${name}_enabled`, settings.enabled);
  setValue(`audio_${name}_frequency`, settings.frequency);
  setValue(`audio_${name}_sharpness`, settings.sharpness);
  const frequency = document.getElementById(`audio_${name}_frequency`);
  frequency.dataset.previousValue = String(settings.frequency);
}

function setNotchControls(settings) {
  setChecked("audio_notch_enabled", settings.enabled);
  setValue("audio_notch_frequency", settings.frequency);
  setValue("audio_notch_width", settings.width);
  document.getElementById("audio_notch_frequency").dataset.previousValue = String(settings.frequency);
  updateNotchWidthLimit(false);
}

let streamArchivesStatus = {};
let archiveSignature = "";
let archiveUpdatePending = false;

function archiveSettingsForStream(stream) {
  return Object.assign({enabled: false, retention: "1w"}, stream && stream.archive ? stream.archive : {});
}

function streamArchiveSummaryText(summary) {
  if (summary && summary.ranges && summary.ranges.length) {
    return `Archived audio from ${summary.from_text} to ${summary.to_text}, using ${summary.storage}.`;
  }
  return "No audio archived yet.";
}

function setArchiveControls(stream) {
  if (archiveUpdatePending) return;
  const settings = archiveSettingsForStream(stream);
  const summary = (stream && streamArchivesStatus[stream.id]) || {};
  // Like EAS recording, it can't be turned off when it's all that keeps the stream running.
  const showEnabledControl = !settings.enabled || enabledStreamOutputCount(stream) > 1;
  const paused = summary.status === "paused";
  const text = streamArchiveSummaryText(summary);
  const nextSignature = JSON.stringify({settings, showEnabledControl, paused, text});
  if (nextSignature === archiveSignature) return;
  setChecked("archive_enabled", settings.enabled);
  // Labelled for what selecting it does now.
  setText("archive_enabled_text", settings.enabled ? "Disable stream archiving" : "Enable stream archiving");
  document.getElementById("archive_enabled_label").hidden = !showEnabledControl;
  setValue("archive_retention", settings.retention);
  setHidden("archive_storage_blocked_hint", !paused);
  setText("archive_summary", text);
  archiveSignature = nextSignature;
}

async function sendArchiveSettings(confirmDelete = false) {
  if (applying || !settingsStreamId) return;
  const stream = currentSettingsStream();
  const previous = archiveSettingsForStream(stream);
  const payload = {
    enabled: document.getElementById("archive_enabled").checked,
    retention: document.getElementById("archive_retention").value
  };
  if (stream && previous.enabled && !payload.enabled && enabledStreamOutputCount(stream) <= 1) {
    setChecked("archive_enabled", true);
    setArchiveResult("At least one output must remain enabled for each stream.", "error");
    return;
  }
  archiveUpdatePending = true;
  try {
    const response = await fetch("/api/stream-archive", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({stream_id: settingsStreamId, archive: payload, confirm_delete: confirmDelete})
    });
    const data = await response.json();
    if (response.status === 409 && data.confirm) {
      // A shorter time to keep audio deletes what's older: ask first.
      archiveUpdatePending = false;
      if (window.confirm(`${data.confirm} Delete it?`)) {
        await sendArchiveSettings(true);
      } else {
        setValue("archive_retention", previous.retention);
        setArchiveResult("Archived audio kept.");
      }
      return;
    }
    if (!response.ok) throw new Error(data.error || response.statusText);
    archiveUpdatePending = false;
    archiveSignature = "";
    renderStreams(data.streams || []);
    setArchiveResult(payload.enabled !== previous.enabled
      ? (payload.enabled ? "Stream archiving is on." : "Stream archiving is off. Audio already archived is kept.")
      : "Stream archiving settings saved.", "success");
  } catch (error) {
    archiveUpdatePending = false;
    archiveSignature = "";
    setArchiveResult(error.message, "error");
    setArchiveControls(currentSettingsStream());
  } finally {
    archiveUpdatePending = false;
  }
}

function setEasControls(stream) {
  const settings = easSettingsForStream(stream);
  const showEnabledControl = !settings.enabled || canDisableEasRecording(stream);
  const storageBlocked = storageIsCritical && settings.enabled;
  const nextSignature = JSON.stringify({settings, showEnabledControl, storageBlocked});
  if (nextSignature === easSignature) return;
  setChecked("eas_enabled", settings.enabled);
  document.getElementById("eas_enabled_label").hidden = !showEnabledControl;
  setHidden("eas_storage_blocked_hint", !storageBlocked);
  setValue("eas_pre_seconds", settings.pre_seconds);
  setValue("eas_post_seconds", settings.post_seconds);
  setValue("eas_max_seconds", settings.max_seconds);
  setChecked("eas_format_wav", settings.format !== "mp3");
  setChecked("eas_format_mp3", settings.format === "mp3");
  easSignature = nextSignature;
}

function setFallbackControls(fallback) {
  if (!fallback) return;
  const nextSignature = JSON.stringify(fallback);
  if (nextSignature === fallbackSignature) return;
  // A number being typed here isn't replaced by a change made elsewhere.
  if (!numberFieldIsBeingTyped(document.getElementById("fallback_loop_delay"))) {
    setValue("fallback_loop_delay", fallback.loop_delay_seconds);
  }
  fallbackSignature = nextSignature;
  const previous = fallbackAudioState;
  fallbackAudioState = fallback;
  // The play button plays the chosen announcement, so it stops if that changes
  // elsewhere, such as on another page.
  const announcementChanged = previous.audio !== fallback.audio
    || (fallback.audio === "custom" && previous.custom_audio_id !== fallback.custom_audio_id);
  if (announcementChanged && announcementPlayerButton === "fallback_play") {
    stopAnnouncementPlayback();
    setFallbackAudioResult("Playback stopped because the announcement was changed.");
  }
  if (fallback.custom_audio_available || fallback.audio === "custom") fallbackCustomChosenWithoutAudio = false;
  renderFallbackAnnouncement();
}

function setStreamNotificationControls(stream) {
  const configured = notificationIsConfigured() && !accountIsReadOnly();
  setHidden("tab_notifications", !configured);
  if (!configured) {
    closeUnavailableStreamSettingsSection("notifications");
    return;
  }
  const settings = streamNotificationSettingsForStream(stream);
  const easEnabled = easRecordingEnabled(stream);
  const nextSignature = JSON.stringify({settings, easEnabled});
  if (nextSignature === streamNotificationsSignature) return;
  setChecked("stream_notify_icecast_failures", settings.icecast_failures);
  setChecked("stream_notify_soundcard_failures", settings.soundcard_failures);
  setChecked("stream_notify_eas_alerts", settings.recorded_eas_alerts);
  setChecked("stream_notify_bad_reception", settings.bad_reception);
  setHidden("stream_notify_eas_alerts_label", !easEnabled);
  setHidden("stream_notify_eas_alerts_hint", !easEnabled);
  streamNotificationsSignature = nextSignature;
}

const STREAM_FALLBACK_ACTION_VALUES = ["none", "disconnect_icecast", "play_fallback_audio"];

function streamFallbackActionForStream(stream) {
  const raw = stream && stream.fallback_action ? stream.fallback_action : {};
  return {
    action: STREAM_FALLBACK_ACTION_VALUES.includes(raw.action) ? raw.action : "play_fallback_audio",
    delay_seconds: Number.isFinite(Number(raw.delay_seconds)) ? Number(raw.delay_seconds) : 30
  };
}

function selectedStreamFallbackAction() {
  const checked = document.querySelector("input[name='stream_fallback_action']:checked");
  return checked ? checked.value : "play_fallback_audio";
}

function updateStreamFallbackDelayVisibility() {
  // "Do nothing" has nothing to wait for.
  const none = selectedStreamFallbackAction() === "none";
  setHidden("stream_fallback_delay_label", none);
  setHidden("stream_fallback_delay_hint", none);
}

function setStreamFallbackActionControls(stream) {
  const settings = streamFallbackActionForStream(stream);
  const nextSignature = JSON.stringify(settings);
  if (nextSignature === streamFallbackActionSignature) return;
  for (const value of STREAM_FALLBACK_ACTION_VALUES) {
    setChecked(`stream_fallback_action_${value}`, settings.action === value);
  }
  if (!numberFieldIsBeingTyped(document.getElementById("stream_fallback_delay"))) {
    setValue("stream_fallback_delay", settings.delay_seconds);
  }
  updateStreamFallbackDelayVisibility();
  streamFallbackActionSignature = nextSignature;
}

function setStreamFallbackActionResult(message, kind = "") {
  setMessage("stream-fallback-action-result", message, kind);
}

function scheduleStreamFallbackActionUpdate() {
  if (applying || !settingsStreamId || accountIsReadOnly()) return;
  clearTimeout(streamFallbackActionUpdateTimer);
  streamFallbackActionUpdateTimer = setTimeout(async () => {
    try {
      const data = await request("/api/stream-fallback-action", {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          stream_id: settingsStreamId,
          fallback_action: {
            action: selectedStreamFallbackAction(),
            delay_seconds: numericControlValue("stream_fallback_delay")
          }
        })
      });
      renderStreams(data.streams || []);
      setStreamFallbackActionResult("Fallback action saved.", "success");
    } catch (error) {
      setStreamFallbackActionResult(error.message, "error");
    }
  }, 250);
}

// A stream's settings are laid out like Settings: a list of sections, with the
// chosen one beside it, or on its own on phones.
const STREAM_SETTINGS_SECTION_TITLES = {
  outputs: "Outputs",
  eas: "EAS recording",
  fallback_action: "Fallback action",
  audio: "Audio effects",
  notifications: "Notifications",
  test_mode: "Test mode",
  archive: "Stream archiving"
};
let streamSettingsSection = "";
let streamSettingsOpenedFrom = null;

function streamSettingsTabs() {
  return Array.from(document.querySelectorAll("#stream_settings_list [data-stream-section]"));
}

function availableStreamSettingsTabs() {
  return streamSettingsTabs().filter(tab => !tab.hidden);
}

function normalizeStreamSettingsSection(section) {
  return availableStreamSettingsTabs().some(tab => tab.dataset.streamSection === section) ? section : "";
}

function streamSettingsPageTitle() {
  const stream = currentSettingsStream();
  const title = `Edit stream ${stream ? streamCallsign(stream) : ""}`.trim();
  return streamSettingsSection ? `${STREAM_SETTINGS_SECTION_TITLES[streamSettingsSection]} - ${title}` : title;
}

function renderStreamSettingsSection() {
  const section = streamSettingsSection;
  const singlePane = settingsSinglePane();
  const list = document.getElementById("stream_settings_list");
  // As in Settings: tabs on wider screens, and on phones buttons that each open a section.
  if (singlePane) {
    list.setAttribute("role", "group");
    list.removeAttribute("aria-orientation");
  } else {
    list.setAttribute("role", "tablist");
    list.setAttribute("aria-orientation", "vertical");
  }
  for (const tab of streamSettingsTabs()) {
    const name = tab.dataset.streamSection;
    const selected = name === section;
    const panel = document.getElementById(`panel_${name}`);
    panel.hidden = !selected;
    if (singlePane) {
      tab.removeAttribute("role");
      tab.removeAttribute("aria-selected");
      tab.removeAttribute("aria-controls");
      tab.tabIndex = 0;
      panel.removeAttribute("role");
      panel.removeAttribute("aria-labelledby");
    } else {
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.setAttribute("aria-controls", panel.id);
      tab.tabIndex = selected ? 0 : -1;
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", tab.id);
    }
  }
  document.getElementById("stream_settings_layout").classList.toggle("settings-detail-active", Boolean(section));
  setPageTitle(streamSettingsPageTitle());
}

function streamSettingsPageHeading() {
  const panel = streamSettingsSection ? document.getElementById(`panel_${streamSettingsSection}`) : null;
  return panel ? Array.from(panel.querySelectorAll("h3")).find(heading => heading.getClientRects().length) || null : null;
}

function selectStreamSettingsTab(section) {
  // Moving through the tabs on wider screens shows each section without adding
  // every one to the browser history.
  streamSettingsSection = section;
  streamSettingsOpenedFrom = null;
  replaceCurrentRoute("stream_settings", {streamId: settingsStreamId, section});
  renderStreamSettingsSection();
}

function openStreamSettingsSection(section) {
  if (settingsSinglePane()) {
    navigateTo("stream_settings", {streamId: settingsStreamId, section, openedFrom: ""});
  } else {
    selectStreamSettingsTab(section);
  }
}

function leaveStreamSettingsSection() {
  if (streamSettingsOpenedFrom === "" && history.state && history.state.view === "stream_settings") {
    history.back();
    return;
  }
  navigateTo("stream_settings", {streamId: settingsStreamId}, true, true);
}

// A section that stops being available, such as Test mode when it can't be used,
// closes, going to Outputs on wider screens or back to the list on phones.
function closeUnavailableStreamSettingsSection(section) {
  if (streamSettingsSection !== section || currentViewName() !== "stream_settings") return;
  const panel = document.getElementById(`panel_${section}`);
  const hadFocus = panel.contains(document.activeElement);
  selectStreamSettingsTab(settingsSinglePane() ? "" : "outputs");
  if (hadFocus) focusSettingsElement(streamSettingsPageHeading() || document.getElementById("stream_settings_title"));
}

function streamSettingsLayoutChanged() {
  if (currentViewName() !== "stream_settings") return;
  const hadFocus = document.activeElement;
  if (!streamSettingsSection && !settingsSinglePane()) {
    selectStreamSettingsTab("outputs");
  } else {
    renderStreamSettingsSection();
  }
  if (hadFocus && hadFocus !== document.body && !hadFocus.getClientRects().length) {
    focusSettingsElement(streamSettingsPageHeading() || document.getElementById("stream_settings_title"));
  }
}

function updateOutputFormButtons() {
  if (outputFormMode === "edit_soundcard") {
    outputFormDirty = false;
    document.getElementById("add_output").hidden = true;
    document.getElementById("save_output_settings").hidden = true;
    return;
  }
  const changed = outputFormSignature() !== outputFormOriginalSignature;
  outputFormDirty = !document.getElementById("output_form_panel").hidden && changed;
  setDisabled(document.getElementById("add_output"), !settingsCredentialsComplete());
  document.getElementById("add_output").hidden = outputFormMode !== "add";
  document.getElementById("save_output_settings").hidden = outputFormMode !== "edit" || !changed;
  setDisabled(document.getElementById("save_output_settings"), !settingsCredentialsComplete());
}

function outputFormIsOpen() {
  return !document.getElementById("output_form_panel").hidden;
}

function currentSettingsStream() {
  return configuredStreams.find(stream => stream.id === settingsStreamId) || null;
}

function prepareAddOutput() {
  outputFormMode = "add";
  editingOutputId = "";
  document.getElementById("icecast_output_edit_panel").hidden = false;
  document.getElementById("soundcard_output_edit_panel").hidden = true;
  clearSettingsIcecastForm();
  outputFormOriginalSignature = outputFormSignature();
  outputFormDirty = false;
  setText("output_form_title", "Add output");
  document.getElementById("output_form_panel").hidden = false;
  setOutputResult("");
  updateOutputFormButtons();
}

function beginAddOutput() {
  if (!settingsStreamId) return;
  if (!prepareAddOutputWizard(settingsStreamId)) return;
  navigateTo("add_stream", {streamId: settingsStreamId}, false, true);
}

function prepareEditOutput(outputId) {
  const selected = findConfiguredOutput(settingsStreamId, outputId);
  if (!selected) {
    setOutputResult("Stream output was not found.", "error");
    return false;
  }
  if ((selected.output.type || "icecast") === "soundcard") {
    return prepareEditSoundcardOutput(selected);
  }
  outputFormMode = "edit";
  editingOutputId = outputId;
  document.getElementById("icecast_output_edit_panel").hidden = false;
  document.getElementById("soundcard_output_edit_panel").hidden = true;
  setSettingsIcecastForm(selected.output.icecast || {});
  outputFormOriginalSignature = outputFormSignature();
  outputFormDirty = false;
  setText("output_form_title", "Edit output");
  document.getElementById("output_form_panel").hidden = false;
  setOutputResult("");
  updateOutputFormButtons();
  return true;
}

function prepareEditSoundcardOutput(selected) {
  outputFormMode = "edit_soundcard";
  editingOutputId = selected.output.id || "";
  outputFormOriginalSignature = "";
  outputFormDirty = false;
  document.getElementById("icecast_output_edit_panel").hidden = true;
  document.getElementById("soundcard_output_edit_panel").hidden = false;
  const soundcard = selected.output.soundcard || {};
  renderSettingsSoundcardDevices(soundcard.stable_id || "");
  setValue("settings_soundcard_volume", soundcard.volume ?? 1);
  setChecked("settings_soundcard_channel_left", soundcard.channel_mode === "left");
  setChecked("settings_soundcard_channel_both", !soundcard.channel_mode || soundcard.channel_mode === "both");
  setChecked("settings_soundcard_channel_right", soundcard.channel_mode === "right");
  setText("output_form_title", "Edit sound card output");
  document.getElementById("output_form_panel").hidden = false;
  setOutputResult("");
  updateOutputFormButtons();
  loadDevices("", {force: true}).catch(error => setOutputResult(error.message, "error"));
  return true;
}

function beginEditOutput(outputId) {
  if (!settingsStreamId || !prepareEditOutput(outputId)) return;
  navigateTo("stream_output", {streamId: settingsStreamId, outputId});
}

function closeOutputForm() {
  document.getElementById("output_form_panel").hidden = true;
  document.getElementById("icecast_output_edit_panel").hidden = false;
  document.getElementById("soundcard_output_edit_panel").hidden = true;
  outputFormDirty = false;
  outputFormOriginalSignature = "";
  editingOutputId = "";
  outputFormMode = "add";
  setOutputResult("");
}

function cancelOutputForm() {
  const streamId = settingsStreamId;
  closeOutputForm();
  if (currentViewName() === "stream_output" && streamId) {
    navigateTo("stream_settings", {streamId, section: "outputs"}, true, true);
  }
}

function clearIcecastForm() {
  setIcecastForm({service: STREAM_SERVICE_CUSTOM, format: "mp3", sample_rate: 24000, bitrate: 64});
  setChecked("show_icecast_password", false);
  document.getElementById("icecast_password").type = "password";
}

function icecastCredentialSignature() {
  const payload = streamPayload().icecast;
  return JSON.stringify({
    host: payload.host,
    port: payload.port,
    username: payload.username,
    password: payload.password,
    mount: payload.mount
  });
}

function credentialsComplete() {
  const payload = streamPayload().icecast;
  const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
  if (service === STREAM_SERVICE_CUSTOM && customServiceHostWarning(payload.host)) return false;
  return Boolean(
    payload.host.trim() &&
    payload.port >= 1 &&
    payload.port <= 65535 &&
    payload.username.trim() &&
    payload.password &&
    payload.mount.trim()
  );
}

function setWizardStep(step) {
  wizardStep = Math.max(0, Math.min(ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP, step));
  renderWizard();
}

function setWizardPanel(id, visible) {
  document.getElementById(id).hidden = !visible;
}

function renderWizard() {
  const editMode = wizardMode === "edit";
  const addOutputMode = wizardMode === "add_output";
  const soundcardMode = wizardIsSoundcardFlow();
  const outputStepMode = !editMode && (addOutputMode || wizardStep >= ADD_OUTPUT_TYPE_STEP);
  const streamStep = wizardUsesOutputSteps() ? streamIcecastStep(wizardStep) : wizardStep;
  const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
  const needsCodecStep = service === STREAM_SERVICE_CUSTOM || service === STREAM_SERVICE_WEATHERUSA;
  const needsQualityStep = service === STREAM_SERVICE_CUSTOM || service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_WEATHERUSA;
  if (service === STREAM_SERVICE_GWES) {
    setChecked("icecast_format_mp3", true);
    setChecked("icecast_format_ogg", false);
  }
  const hostWarning = service === STREAM_SERVICE_CUSTOM ? customServiceHostWarning(document.getElementById("icecast_host").value) : "";
  if (hostWarning) setStreamResult(hostWarning, "error");
  else if (document.getElementById("stream-result").textContent.startsWith("Please select ")) setStreamResult("");
  setServiceHelp("icecast_service_help", service);
  applyServiceControls("icecast", service);
  const stream = addOutputMode ? configuredStreams.find(item => item.id === settingsStreamId) : null;
  const title = editMode ? "Edit Stream Output" : addOutputMode ? `Add output for ${streamCallsign(stream)}` : "Add Stream";
  setText("stream_wizard_title", title);
  if (currentViewName() === "add_stream") setPageTitle(title);
  setWizardPanel("wizard_step_station", !editMode && !addOutputMode && wizardStep === 0);
  setWizardPanel("wizard_step_output_type", outputStepMode && wizardStep === ADD_OUTPUT_TYPE_STEP);
  setWizardPanel("wizard_step_soundcard_device", soundcardMode && wizardStep === ADD_OUTPUT_SOUNDCARD_DEVICE_STEP);
  setWizardPanel("wizard_step_soundcard_controls", soundcardMode && wizardStep === ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP);
  setWizardPanel("wizard_step_service", !editMode && !soundcardMode && streamStep === 1);
  setWizardPanel("wizard_step_codec", !editMode && !soundcardMode && streamStep === 2 && needsCodecStep);
  setWizardPanel("wizard_step_credentials", !soundcardMode && (editMode || streamStep === 3 || (!needsCodecStep && streamStep === 2)));
  setWizardPanel("wizard_step_quality", !editMode && !soundcardMode && streamStep === 4 && needsQualityStep);
  document.getElementById("cancel_wizard").hidden = editMode;
  const backButton = document.getElementById("wizard_back");
  backButton.hidden = editMode || (!addOutputMode && wizardStep === 0);
  setDisabled(backButton, addOutputMode && wizardStep === ADD_OUTPUT_TYPE_STEP);
  const next = document.getElementById("wizard_next");
  const finish = document.getElementById("wizard_finish");
  next.hidden = editMode || (soundcardMode && wizardStep === ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP) || (!soundcardMode && streamStep === 4) || (!soundcardMode && !needsQualityStep && streamStep >= 3);
  finish.hidden = editMode || !(soundcardMode && wizardStep === ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP || (!soundcardMode && (streamStep === 4 || (!needsQualityStep && streamStep >= 3))));
  renderWizardSoundcardDevices();
  if (wizardStep === 0) {
    setDisabled(next, !selectedStation());
  } else if (wizardStep === ADD_OUTPUT_TYPE_STEP) {
    setDisabled(next, false);
  } else if (wizardStep === ADD_OUTPUT_SOUNDCARD_DEVICE_STEP) {
    setDisabled(next, !document.getElementById("wizard_soundcard_device").value);
  } else if (!soundcardMode && (streamStep === 3 || (!needsCodecStep && streamStep === 2))) {
    setDisabled(next, !credentialsComplete());
  } else {
    setDisabled(next, false);
  }
  if (!finish.hidden) {
    setDisabled(finish, !soundcardMode && streamStep === 3 && !credentialsComplete());
  }
}

function resetStreamWizardState() {
  wizardMode = "add";
  wizardStep = 0;
  wizardDirty = true;
  icecastAuthPassed = false;
  icecastAuthSignature = "";
  editingStreamId = "";
  editingOutputId = "";
  settingsStreamId = "";
  wizardStationOverride = null;
  selectedStationKey = "";
  setValue("station_search", "");
  updateStationSearchControls();
  clearStationResults();
  clearIcecastForm();
  setChecked("wizard_output_type_icecast", true);
  setChecked("wizard_output_type_soundcard", false);
  setChecked("wizard_soundcard_channel_both", true);
  setValue("wizard_soundcard_volume", 1);
  setStreamResult("");
  renderWizard();
}

function beginStreamWizard() {
  resetStreamWizardState();
  navigateTo("add_stream", {}, false, true);
}

function finishWizard(options = {}) {
  const replace = options.replace !== false;
  const returnStreamId = wizardMode === "add_output" ? settingsStreamId : "";
  wizardDirty = false;
  icecastAuthPassed = false;
  icecastAuthSignature = "";
  wizardStationOverride = null;
  wizardSoundcardOutputId = "";
  wizardSoundcardPreviewId = "";
  setStreamResult("");
  if (returnStreamId) {
    navigateTo("stream_settings", {streamId: returnStreamId, section: "outputs"}, replace, true);
  } else {
    navigateTo("streams", {}, replace, true);
  }
}

function prepareAddOutputWizard(streamId) {
  const stream = configuredStreams.find(item => item.id === streamId);
  if (!stream) {
    setOutputResult("Stream was not found.", "error");
    return false;
  }
  settingsStreamId = streamId;
  editingStreamId = streamId;
  editingOutputId = "";
  wizardMode = "add_output";
  wizardStep = ADD_OUTPUT_TYPE_STEP;
  wizardDirty = true;
  icecastAuthPassed = false;
  icecastAuthSignature = "";
  wizardSoundcardOutputId = "";
  wizardSoundcardPreviewId = "";
  wizardStationOverride = stream.station || null;
  selectedStationKey = stream.station && stream.station.key ? stream.station.key : "";
  setChecked("wizard_output_type_icecast", true);
  setChecked("wizard_output_type_soundcard", false);
  setChecked("wizard_soundcard_channel_both", true);
  setValue("wizard_soundcard_volume", 1);
  clearIcecastForm();
  setText("selected_station", `Adding output for ${streamCallsign(stream)} ${stream.station && stream.station.frequency ? `${stream.station.frequency} MHz` : ""}`);
  setStreamResult("");
  renderWizard();
  loadDevices("", {force: true}).catch(error => setStreamResult(error.message, "error"));
  return true;
}

async function authenticateWizardIcecastOutput() {
  const button = document.getElementById("wizard_next");
  setDisabled(button, true);
  setStreamResult("Testing Icecast authentication...");
  try {
    const signature = icecastCredentialSignature();
    const data = await request("/api/icecast-auth", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({icecast: streamPayload().icecast})
    });
    setStreamResult(data.message, data.success ? "success" : "error");
    if (data.success) {
      icecastAuthPassed = true;
      icecastAuthSignature = signature;
      const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
      const outputSteps = wizardUsesOutputSteps();
      if (service === STREAM_SERVICE_NWRORG) {
        setWizardStep(outputSteps ? ADD_OUTPUT_ICECAST_CREDENTIALS_STEP : 3);
      } else {
        setWizardStep(outputSteps ? ADD_OUTPUT_ICECAST_QUALITY_STEP : 4);
      }
    }
  } catch (error) {
    setStreamResult(error.message, "error");
  } finally {
    renderWizard();
  }
}

async function createWizardSoundcardOutput() {
  if (!settingsStreamId) return;
  const button = document.getElementById("wizard_next");
  setDisabled(button, true);
  setStreamResult(wizardSoundcardOutputId ? "Updating soundcard output..." : "Adding soundcard output...");
  try {
    const data = await request("/api/stream-output", {
      method: wizardSoundcardOutputId ? "PATCH" : "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        stream_id: settingsStreamId,
        output_id: wizardSoundcardOutputId,
        type: "soundcard",
        enabled: true,
        soundcard: wizardSoundcardPayload()
      })
    });
    renderStreams(data.streams || []);
    if (data.success) {
      wizardSoundcardOutputId = data.output_id || wizardSoundcardOutputId;
      setStreamResult(data.message, "success");
      setWizardStep(ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP);
    } else {
      setStreamResult(data.message, "error");
    }
  } catch (error) {
    setStreamResult(error.message, "error");
  } finally {
    setDisabled(button, false);
    renderWizard();
  }
}

async function upsertWizardSoundcardPreview() {
  if (wizardMode !== "add" || selectedWizardOutputType() !== "soundcard") return false;
  const button = document.getElementById("wizard_next");
  setDisabled(button, true);
  setStreamResult(wizardSoundcardPreviewId ? "Updating soundcard preview..." : "Starting soundcard preview...");
  try {
    const data = await request("/api/stream-soundcard-preview", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        preview_id: wizardSoundcardPreviewId,
        station_key: selectedStationKey,
        soundcard: wizardSoundcardPayload()
      })
    });
    if (data.success) {
      wizardSoundcardPreviewId = data.preview_id || wizardSoundcardPreviewId;
      setStreamResult(data.message, "success");
      setWizardStep(ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP);
      return true;
    }
    setStreamResult(data.message || "Soundcard preview could not be started.", "error");
  } catch (error) {
    setStreamResult(error.message, "error");
  } finally {
    setDisabled(button, false);
    renderWizard();
  }
  return false;
}

async function updateWizardSoundcardPreview() {
  if (wizardMode !== "add" || !wizardSoundcardPreviewId) return;
  try {
    const data = await request("/api/stream-soundcard-preview", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        preview_id: wizardSoundcardPreviewId,
        station_key: selectedStationKey,
        soundcard: wizardSoundcardPayload()
      })
    });
    setText("wizard_soundcard_status", data.success ? "Soundcard preview updated." : data.message);
  } catch (error) {
    setText("wizard_soundcard_status", error.message);
  }
}

async function discardWizardSoundcardPreview() {
  if (!wizardSoundcardPreviewId) return;
  const previewId = wizardSoundcardPreviewId;
  wizardSoundcardPreviewId = "";
  try {
    await request(`/api/stream-soundcard-preview?preview_id=${encodeURIComponent(previewId)}`, {
      method: "DELETE"
    });
  } catch (error) {
    setStreamResult(error.message, "error");
  }
}

function sendWizardSoundcardPreviewDiscardBeacon() {
  if (!wizardSoundcardPreviewId) return;
  const previewId = wizardSoundcardPreviewId;
  wizardSoundcardPreviewId = "";
  const payload = JSON.stringify({preview_id: previewId});
  if (navigator.sendBeacon) {
    navigator.sendBeacon("/api/stream-soundcard-preview/discard", new Blob([payload], {type: "application/json"}));
    return;
  }
  fetch("/api/stream-soundcard-preview/discard", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: payload,
    keepalive: true
  }).catch(() => {});
}

async function updateWizardSoundcardOutput() {
  if (!settingsStreamId || !wizardSoundcardOutputId) return;
  try {
    const data = await request("/api/stream-output", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        stream_id: settingsStreamId,
        output_id: wizardSoundcardOutputId,
        type: "soundcard",
        enabled: true,
        soundcard: wizardSoundcardPayload()
      })
    });
    renderStreams(data.streams || []);
    setText("wizard_soundcard_status", data.success ? "Soundcard output updated." : data.message);
  } catch (error) {
    setText("wizard_soundcard_status", error.message);
  }
}

function scheduleWizardSoundcardUpdate() {
  wizardDirty = true;
  if (wizardMode === "add" && wizardSoundcardPreviewId) {
    if (wizardSoundcardUpdateTimer) window.clearTimeout(wizardSoundcardUpdateTimer);
    wizardSoundcardUpdateTimer = window.setTimeout(() => {
      wizardSoundcardUpdateTimer = null;
      updateWizardSoundcardPreview();
    }, 120);
    return;
  }
  if (!wizardSoundcardOutputId) {
    renderWizard();
    return;
  }
  if (wizardSoundcardUpdateTimer) window.clearTimeout(wizardSoundcardUpdateTimer);
  wizardSoundcardUpdateTimer = window.setTimeout(() => {
    wizardSoundcardUpdateTimer = null;
    updateWizardSoundcardOutput();
  }, 120);
}

let settingsSoundcardUpdateTimer = null;

async function updateSettingsSoundcardOutput() {
  if (!settingsStreamId || !editingOutputId || outputFormMode !== "edit_soundcard") return;
  const selected = findConfiguredOutput(settingsStreamId, editingOutputId);
  if (!selected) {
    setOutputResult("Stream output was not found.", "error");
    return;
  }
  try {
    const data = await request("/api/stream-output", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        stream_id: settingsStreamId,
        output_id: editingOutputId,
        type: "soundcard",
        enabled: selected.output.enabled !== false,
        soundcard: settingsSoundcardPayload()
      })
    });
    renderStreams(data.streams || []);
    setOutputResult(data.message || "Sound card output updated.", data.success ? "success" : "error");
  } catch (error) {
    setOutputResult(error.message, "error");
  }
}

function scheduleSettingsSoundcardUpdate() {
  outputFormDirty = false;
  if (settingsSoundcardUpdateTimer) window.clearTimeout(settingsSoundcardUpdateTimer);
  settingsSoundcardUpdateTimer = window.setTimeout(() => {
    settingsSoundcardUpdateTimer = null;
    updateSettingsSoundcardOutput();
  }, 120);
}

async function discardWizardSoundcardOutput() {
  if (!settingsStreamId || !wizardSoundcardOutputId) return;
  const outputId = wizardSoundcardOutputId;
  wizardSoundcardOutputId = "";
  try {
    const data = await request(
      `/api/stream-output?stream_id=${encodeURIComponent(settingsStreamId)}&output_id=${encodeURIComponent(outputId)}`,
      {method: "DELETE"}
    );
    renderStreams(data.streams || []);
  } catch (error) {
    setStreamResult(error.message, "error");
  }
}

function editStreamSettings(streamId, outputId = "") {
  if (outputId) {
    settingsStreamId = streamId;
    if (prepareEditOutput(outputId)) navigateTo("stream_output", {streamId, outputId});
    return;
  }
  showStreamSettings(streamId);
}

function showStreamSettings(streamId) {
  const stream = configuredStreams.find(item => item.id === streamId);
  if (!stream) {
    setStreamResult("Stream was not found.", "error");
    return;
  }
  if (activeTestModeBelongsToThisPage() && streamTestMode.stream_id !== streamId) {
    stopStreamTestMode({beacon: true});
  }
  settingsStreamId = streamId;
  icecastOutputTableSignature = "";
  soundcardOutputTableSignature = "";
  easSignature = "";
  audioEffectsSignature = "";
  streamNotificationsSignature = "";
  streamFallbackActionSignature = "";
  streamTestModeSignature = "";
  selectTestTone(selectedTestTone, false);
  testTonesSignature = "";
  closeOutputForm();
  const station = stream.station || {};
  setText("stream_settings_station", `${station.callsign || "Unknown"} ${station.frequency || ""} MHz`);
  renderStreamSettings();
  navigateTo("stream_settings", {streamId});
}

function renderStreamSettings() {
  const stream = currentSettingsStream();
  const addButton = document.getElementById("open_add_output");
  const monitorLabel = document.getElementById("stream_monitor_label");
  const monitorHint = document.getElementById("stream_monitor_hint");
  const enabled = streamIsEnabled(stream);
  const monitorHidden = !enabled;
  setDisabled(addButton, !stream);
  setDisabled(document.getElementById("stream_enabled"), !stream);
  monitorLabel.hidden = monitorHidden;
  monitorHint.hidden = monitorHidden;
  setDisabled(document.getElementById("stream_monitor_enabled"), monitorHidden);
  if (stream) setChecked("stream_enabled", enabled);
  setChecked("stream_monitor_enabled", Boolean(enabled && monitorStreamId === stream.id));
  setAudioEffectsControls(stream);
  setEasControls(stream);
  setArchiveControls(stream);
  setStreamNotificationControls(stream);
  setStreamFallbackActionControls(stream);
  renderStreamSettingsSignal(stream);
  setTestModeControls();
  renderOutputPanels();
  renderIcecastOutputsTable(stream);
  renderSoundcardOutputsTable(stream);
}

function outputStatusFor(stream, output) {
  if (!streamIsEnabled(stream)) return "disabled";
  if (!output || output.enabled === false) return "disabled";
  const active = activeStreamSnapshots.find(snapshot =>
    snapshot.id === stream.id && streamOutputs(snapshot).some(activeOutput => activeOutput.id === output.id)
  );
  const status = normalizeStreamStatus(active ? active.status : "");
  if (status === "enabled") return "connected";
  if (status === "standby") return "standby";
  return "needs-attention";
}

function outputStatusLabel(status) {
  if (status === "connected") return "Connected";
  if (status === "needs-attention") return "Needs attention";
  if (status === "standby") return "Disconnected (no station audio)";
  return "Disabled";
}

function outputStatusLabelForOutput(output, status) {
  if ((output.type || "icecast") === "soundcard" && status === "connected") return "Enabled";
  return outputStatusLabel(status);
}

function outputDestination(icecast) {
  if (!icecast || !icecast.host) return "Unknown";
  return `${icecast.host}:${icecast.port}${icecast.mount}`;
}

function soundcardDestination(soundcard) {
  const device = soundcardDevices.find(item => item.stable_id === soundcard.stable_id);
  return device ? friendlySoundcardLabel(device) : soundcard.display_name || soundcard.card_long_name || soundcard.card_name || soundcard.device || "Sound card";
}

function soundcardDeviceForStableId(stableId) {
  return soundcardDevices.find(item => item.stable_id === stableId) || null;
}

function soundcardOutputUsbDevice(output) {
  if (!output || output.type !== "soundcard") return null;
  const stableId = output.soundcard && output.soundcard.stable_id ? output.soundcard.stable_id : "";
  const device = soundcardDeviceForStableId(stableId);
  return device && device.bus === "usb" ? device : null;
}

function outputRowDestination(output) {
  if (output.type === "soundcard") return soundcardDestination(output.soundcard || {});
  return outputDestination(output.icecast || {});
}

function outputRowFormat(output) {
  if (output.type === "soundcard") return "Soundcard";
  return String((output.icecast || {}).format || "mp3").toUpperCase();
}

function outputRowSampleRate(output) {
  if (output.type === "soundcard") return `${(output.soundcard || {}).sample_rate || 48000} Hz`;
  return `${(output.icecast || {}).sample_rate || DEFAULT_STREAM_SAMPLE_RATE} Hz`;
}

function outputRowBitrate(output) {
  if (output.type === "soundcard") {
    const mode = (output.soundcard || {}).channel_mode || "both";
    const volume = Number((output.soundcard || {}).volume ?? 1).toFixed(2);
    return `${mode}, volume ${volume}`;
  }
  const icecast = output.icecast || {};
  return `${icecast.bitrate || DEFAULT_STREAM_BITRATES[icecast.format || "mp3"]} Kbps`;
}

function outputRows(stream) {
  if (!stream) return [];
  return streamOutputs(stream).map(output => ({
    stream,
    output,
    icecast: output.icecast || {},
    soundcard: output.soundcard || {},
    status: outputStatusFor(stream, output)
  }));
}

function outputRowsByType(stream, type) {
  return outputRows(stream).filter(row => (row.output.type || "icecast") === type);
}

function outputTableNextSignature(rows) {
  return JSON.stringify(rows.map(row => ({
    id: row.output.id || "",
    enabled: row.output.enabled !== false,
    type: row.output.type || "icecast",
    destination: outputRowDestination(row.output),
    format: outputRowFormat(row.output),
    sample_rate: outputRowSampleRate(row.output),
    bitrate: outputRowBitrate(row.output),
    bus: row.output.type === "soundcard" ? (soundcardDeviceForStableId((row.output.soundcard || {}).stable_id || "") || {}).bus || "" : "",
    status: row.status
  })));
}

function renderOutputPanels() {
  const selected = document.getElementById("output_type_filter").value || "icecast";
  document.getElementById("icecast_outputs_panel").hidden = selected !== "icecast";
  document.getElementById("soundcard_outputs_panel").hidden = selected !== "soundcard";
}

function renderIcecastOutputsTable(stream) {
  const tbody = document.getElementById("icecast-outputs-body");
  const rows = outputRowsByType(stream, "icecast");
  const nextSignature = outputTableNextSignature(rows);
  if (nextSignature === icecastOutputTableSignature) return;
  icecastOutputTableSignature = nextSignature;
  if (rows.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "hint";
    cell.textContent = "No Icecast outputs configured.";
    row.appendChild(cell);
    reconcileKeyedChildren(tbody, [keyedRow(labelResponsiveTableRow(row, tbody), "empty")]);
    return;
  }
  reconcileKeyedChildren(tbody, rows.map(row => keyedRow(labelResponsiveTableRow(icecastOutputRow(row), tbody), `output:${row.output.id || ""}`)));
}

function renderSoundcardOutputsTable(stream) {
  const tbody = document.getElementById("soundcard-outputs-body");
  const rows = outputRowsByType(stream, "soundcard");
  const nextSignature = outputTableNextSignature(rows);
  if (nextSignature === soundcardOutputTableSignature) return;
  soundcardOutputTableSignature = nextSignature;
  if (rows.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.className = "hint";
    cell.textContent = "No sound card outputs configured.";
    row.appendChild(cell);
    reconcileKeyedChildren(tbody, [keyedRow(labelResponsiveTableRow(row, tbody), "empty")]);
    return;
  }
  reconcileKeyedChildren(tbody, rows.map(row => keyedRow(labelResponsiveTableRow(soundcardOutputRow(row), tbody), `output:${row.output.id || ""}`)));
}

function icecastOutputRow(row) {
  const tr = document.createElement("tr");
  tr.appendChild(tableCell(outputRowDestination(row.output)));
  tr.appendChild(tableCell(outputRowFormat(row.output)));
  tr.appendChild(tableCell(outputRowSampleRate(row.output)));
  tr.appendChild(tableCell(outputRowBitrate(row.output)));
  const statusCell = tableCell(outputStatusLabelForOutput(row.output, row.status));
  statusCell.className = `status-text status-${row.status}`;
  tr.appendChild(statusCell);
  tr.appendChild(outputActionsCell(row.stream, row.output, row.status));
  return tr;
}

function soundcardChannelLabel(mode) {
  if (mode === "left") return "Left";
  if (mode === "right") return "Right";
  return "Both left and right";
}

function soundcardOutputRow(row) {
  const tr = document.createElement("tr");
  const soundcard = row.output.soundcard || {};
  tr.appendChild(tableCell(soundcardDestination(soundcard)));
  tr.appendChild(tableCell(soundcardChannelLabel(soundcard.channel_mode || "both")));
  const statusCell = tableCell(outputStatusLabelForOutput(row.output, row.status));
  statusCell.className = `status-text status-${row.status}`;
  tr.appendChild(statusCell);
  tr.appendChild(outputActionsCell(row.stream, row.output, row.status));
  return tr;
}

function outputActionsCell(stream, output) {
  const cell = document.createElement("td");
  cell.className = "menu-cell";
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "More actions";
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-label", `More actions for ${outputRowDestination(output)}`);
  button.dataset.outputMenu = output.id || "";
  const menu = document.createElement("div");
  menu.className = "stream-actions-menu";
  menu.hidden = true;
  menu.setAttribute("role", "menu");
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.textContent = output.enabled === false ? "Enable" : "Disable";
  toggle.setAttribute("role", "menuitem");
  toggle.dataset.action = "toggle-output";
  toggle.dataset.streamId = stream.id || "";
  toggle.dataset.outputId = output.id || "";
  const edit = document.createElement("button");
  edit.type = "button";
  edit.textContent = "Edit output";
  edit.setAttribute("role", "menuitem");
  edit.dataset.action = "edit-settings-output";
  edit.dataset.streamId = stream.id || "";
  edit.dataset.outputId = output.id || "";
  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "Remove";
  remove.setAttribute("role", "menuitem");
  remove.dataset.action = "remove-output";
  remove.dataset.streamId = stream.id || "";
  remove.dataset.outputId = output.id || "";
  const reset = document.createElement("button");
  reset.type = "button";
  reset.textContent = "Reset USB sound card";
  reset.setAttribute("role", "menuitem");
  reset.dataset.action = "reset-soundcard-output";
  reset.dataset.streamId = stream.id || "";
  reset.dataset.outputId = output.id || "";
  if (!accountIsReadOnly()) {
    if (output.enabled === false || canDisableOutput(stream, output)) {
      menu.appendChild(toggle);
    }
    menu.appendChild(edit);
    if (soundcardOutputUsbDevice(output)) {
      menu.appendChild(reset);
    }
    if (canRemoveOutput(stream, output)) {
      menu.appendChild(remove);
    }
  }
  cell.appendChild(button);
  cell.appendChild(menu);
  return cell;
}

function activeStreamRows(activeStreams, configured = configuredStreams) {
  const rows = [];
  const activeById = new Map();
  for (const stream of activeStreams || []) {
    const items = activeById.get(stream.id) || [];
    items.push(stream);
    activeById.set(stream.id, items);
  }
  for (const stream of configured || []) {
    const activeItems = activeById.get(stream.id) || [];
    const signal = streamIsEnabled(stream) ? streamSignalFor(stream.id) : null;
    if (activeItems.length) {
      const statuses = activeItems.map(item => normalizeStreamStatus(item.status));
      if (streamReceptionProblem(signal)) statuses.push("needs-attention");
      const status = statuses.includes("needs-attention") ? "needs-attention"
        : statuses.includes("enabled") ? "enabled"
        : statuses.includes("standby") ? "standby"
        : "disabled";
      rows.push({
        id: stream.id,
        enabled: streamIsEnabled(stream),
        station: stream.station,
        outputs: streamOutputs(stream),
        signal,
        status
      });
      activeById.delete(stream.id);
    } else {
      rows.push({
        id: stream.id,
        enabled: streamIsEnabled(stream),
        station: stream.station,
        outputs: streamOutputs(stream),
        signal,
        status: streamReceptionProblem(signal) ? "needs-attention" : "disabled"
      });
    }
  }
  for (const streams of activeById.values()) {
    for (const stream of streams) rows.push(stream);
  }
  return rows;
}

function activeStreamSignature(rows) {
  return JSON.stringify(rows.map(stream => {
    const station = stream.station || {};
    return {
      id: stream.id || "",
      enabled: streamIsEnabled(stream),
      callsign: station.callsign || "",
      frequency: station.frequency || "",
      outputs: streamOutputs(stream).map(output => ({
        id: output.id || "",
        enabled: output.enabled !== false,
        type: output.type || "icecast"
      })),
      monitoring: monitorStreamId === stream.id,
      signal: streamSignalDisplay(stream.signal),
      status: normalizeStreamStatus(stream.status),
      read_only: accountIsReadOnly()
    };
  }));
}

function renderActiveStreams(activeStreams, configured = configuredStreams, options = {}) {
  const tbody = document.getElementById("active-streams-body");
  const rows = activeStreamRows(activeStreams, configured);
  const nextSignature = activeStreamSignature(rows);
  if (nextSignature === activeStreamsSignature) {
    return;
  }
  activeStreamsSignature = nextSignature;
  if (rows.length === 0) {
    const row = document.createElement("tr");
    row.id = "active-streams-empty";
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "hint";
    cell.textContent = "No streams configured.";
    row.appendChild(cell);
    reconcileKeyedChildren(tbody, [keyedRow(labelResponsiveTableRow(row, tbody), "empty")]);
    return;
  }
  reconcileKeyedChildren(tbody, rows.map(stream => keyedRow(labelResponsiveTableRow(activeStreamRow(stream), tbody), `stream:${stream.id || ""}`)));
}

function dashboardAttentionSignatureFor(items, configured = configuredStreams) {
  return JSON.stringify({
    summary: configuredStreamSummary(configured),
    read_only: accountIsReadOnly(),
    items: items.map(item => ({
      id: item.id || "",
      callsign: item.callsign || "",
      status: item.status || "",
      detail: item.detail || "",
      error: item.error || ""
    }))
  });
}

function pluralize(count, singular, plural = `${singular}s`) {
  return `${count} ${count === 1 ? singular : plural}`;
}

function configuredStreamSummary(configured = configuredStreams) {
  const streams = configured || [];
  const total = streams.length;
  const enabled = streams.filter(stream => streamIsEnabled(stream)).length;
  const disabled = Math.max(0, total - enabled);
  if (!total) return "No streams configured.";
  return `${pluralize(total, "stream")} configured, ${enabled} enabled, ${disabled} disabled.`;
}

function sentenceList(items) {
  const values = (items || []).map(item => String(item || "").trim()).filter(Boolean);
  if (values.length <= 1) return values[0] || "";
  if (values.length === 2) return `${values[0]} and ${values[1]}`;
  return `${values.slice(0, -1).join(", ")} and ${values[values.length - 1]}`;
}

function stationHasIdentity(station) {
  return Boolean(station && (station.callsign || station.frequency));
}

function streamAttentionItems(activeStreams, configured = configuredStreams) {
  const configuredById = new Map((configured || []).map(stream => [stream.id, stream]));
  const byStream = new Map();
  for (const active of activeStreams || []) {
    if (normalizeStreamStatus(active.status) !== "needs-attention") continue;
    const configuredStream = configuredById.get(active.id) || {};
    const station = stationHasIdentity(active.station) ? active.station : configuredStream.station || {};
    const outputs = active.outputs || [];
    const streamId = active.id || configuredStream.id || "";
    if (!streamId) continue;
    let item = byStream.get(streamId);
    if (!item) {
      item = {
        id: streamId,
        callsign: station.callsign || "Unknown",
        frequency: station.frequency || "",
        status: "needs-attention",
        icecastFailed: false,
        soundcardFailed: false,
        receptionProblem: null,
        errors: []
      };
      byStream.set(streamId, item);
    }
    if (active.error) item.errors.push(active.error);
    if (!outputs.length) continue;
    for (const output of outputs) {
      const type = output.type || "icecast";
      const outputStatus = normalizeStreamStatus(output.status || active.status);
      if (outputStatus !== "needs-attention") continue;
      if (type === "soundcard") {
        item.soundcardFailed = true;
      } else {
        item.icecastFailed = true;
      }
    }
  }
  for (const stream of configured || []) {
    const receptionProblem = streamIsEnabled(stream) ? streamReceptionProblem(streamSignalFor(stream.id)) : null;
    if (!receptionProblem) continue;
    let item = byStream.get(stream.id);
    if (!item) {
      const station = stream.station || {};
      item = {
        id: stream.id,
        callsign: station.callsign || "Unknown",
        frequency: station.frequency || "",
        status: "needs-attention",
        icecastFailed: false,
        soundcardFailed: false,
        receptionProblem: null,
        errors: []
      };
      byStream.set(stream.id, item);
    }
    item.receptionProblem = receptionProblem;
  }
  const items = Array.from(byStream.values()).map(item => {
    // One sentence per stream, listing every problem it currently has.
    const reasons = [];
    if (item.receptionProblem) reasons.push(RECEPTION_PROBLEM_REASONS[item.receptionProblem]);
    if (item.icecastFailed) reasons.push("one or more Icecast destinations failed to connect");
    if (item.soundcardFailed) reasons.push("one or more sound cards are not connected");
    let detail;
    if (!reasons.length) {
      detail = item.errors[0] || "This stream needs attention.";
    } else if (reasons.length === 1 && item.receptionProblem) {
      detail = RECEPTION_PROBLEM_DETAILS[item.receptionProblem];
    } else {
      const sentence = sentenceList(reasons);
      detail = `${sentence.charAt(0).toUpperCase()}${sentence.slice(1)}.`;
    }
    return Object.assign(item, {detail});
  });
  items.sort((a, b) => String(a.callsign).localeCompare(String(b.callsign)) || String(a.destination).localeCompare(String(b.destination)));
  return items;
}

function renderDashboardStreamAttention(activeStreams, configured = configuredStreams) {
  const container = document.getElementById("dashboard-stream-attention");
  const items = streamAttentionItems(activeStreams, configured);
  const nextSignature = dashboardAttentionSignatureFor(items, configured);
  if (nextSignature === dashboardAttentionSignature) return;
  dashboardAttentionSignature = nextSignature;
  setText("dashboard-stream-summary", configuredStreamSummary(configured));
  if (!items.length) {
    const healthy = document.createElement("div");
    healthy.className = "stream-item success";
    healthy.textContent = "All streams are healthy!";
    reconcileKeyedChildren(container, [keyedRow(healthy, "healthy")]);
    return;
  }
  const children = [];
  const alert = document.createElement("p");
  alert.className = "status-text status-needs-attention";
  const names = Array.from(new Set(items.map(item => item.callsign)));
  alert.textContent = names.length === 1
    ? `Stream ${names[0]} requires attention!`
    : `Streams ${sentenceList(names)} require attention.`;
  children.push(keyedRow(alert, "summary"));
  const readOnly = accountIsReadOnly();
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "stream-item";
    if (readOnly) {
      const label = document.createElement("strong");
      label.textContent = item.callsign;
      row.appendChild(label);
      row.append(`: ${item.detail}`);
    } else {
      const link = document.createElement("a");
      link.href = routeForView("stream_settings", {streamId: item.id});
      link.dataset.view = "stream_settings";
      link.dataset.streamId = item.id;
      link.textContent = `View stream details for ${item.callsign}`;
      row.appendChild(link);
      row.append(`: ${item.detail}`);
    }
    children.push(keyedRow(row, `stream:${item.id}`));
  }
  reconcileKeyedChildren(container, children);
}

function sentenceCaseAlertName(name) {
  const text = String(name || "Unknown alert").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function relativeTimeAgo(epochSeconds) {
  const ageSeconds = Math.max(0, Math.floor((Date.now() / 1000) - Number(epochSeconds || 0)));
  const minutes = Math.max(0, Math.floor(ageSeconds / 60));
  if (minutes < 1) return "less than a minute ago";
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.floor(minutes / 60);
  return `${hours} hour${hours === 1 ? "" : "s"} ago`;
}

function recentAlertsSignature(alerts) {
  return JSON.stringify({
    now_minute: Math.floor(Date.now() / 60000),
    alerts: (alerts || []).map(alert => [
      alert.id || "",
      alert.stream_id || "",
      alert.callsign || "",
      alert.event_name || "",
      Math.floor(Number(alert.issued_at_epoch || 0) / 60)
    ])
  });
}

function renderDashboardRecentAlerts(alerts) {
  const container = document.getElementById("dashboard-recent-alerts");
  const nextSignature = recentAlertsSignature(alerts);
  if (nextSignature === dashboardRecentAlertsSignature) return;
  dashboardRecentAlertsSignature = nextSignature;
  if (!alerts || alerts.length === 0) {
    const empty = document.createElement("div");
    empty.className = "stream-item hint";
    empty.textContent = "No EAS alerts issued in the last 24 hours.";
    reconcileKeyedChildren(container, [keyedRow(empty, "empty")]);
    return;
  }
  const children = [];
  for (const alert of alerts) {
    const item = document.createElement("div");
    item.className = "stream-item";
    const alertLink = document.createElement("a");
    alertLink.href = routeForView("eas_alert_detail", {
      streamId: alert.stream_id || "",
      alertId: alert.id || "",
      page: 1
    });
    alertLink.dataset.view = "eas_alert_detail";
    alertLink.dataset.streamId = alert.stream_id || "";
    alertLink.dataset.alertId = alert.id || "";
    alertLink.dataset.page = "1";
    alertLink.dataset.easEntry = "detail";
    alertLink.textContent = sentenceCaseAlertName(alert.event_name);
    const allLink = document.createElement("a");
    allLink.href = routeForView("eas_alerts", {streamId: alert.stream_id || "", page: 1});
    allLink.dataset.view = "eas_alerts";
    allLink.dataset.streamId = alert.stream_id || "";
    allLink.dataset.page = "1";
    allLink.dataset.easEntry = "list";
    allLink.textContent = `View all EAS alerts for ${alert.callsign || "this stream"}`;
    item.appendChild(alertLink);
    item.append(` issued ${relativeTimeAgo(alert.issued_at_epoch)} on ${alert.callsign || "Unknown"}. `);
    item.appendChild(allLink);
    children.push(keyedRow(item, `alert:${alert.stream_id || ""}:${alert.id || ""}`));
  }
  reconcileKeyedChildren(container, children);
}

function activeStreamRow(stream) {
  const station = stream.station || {};
  const status = normalizeStreamStatus(stream.status);
  const row = document.createElement("tr");
  row.appendChild(tableCell(station.callsign || "Unknown"));
  row.appendChild(tableCell(station.frequency ? `${station.frequency} MHz` : "Unknown"));
  row.appendChild(tableCell(streamOutputCount(stream)));
  row.appendChild(streamSignalCell(stream.signal));
  const statusCell = tableCell(streamStatusLabel(status));
  statusCell.className = `status-text status-${status}`;
  row.appendChild(statusCell);
  row.appendChild(streamActionsCell(stream));
  return row;
}

function labelResponsiveTableRow(row, tbody) {
  // Roles keep table semantics when phones restyle rows as cards; data-label
  // is the visible card label, taken from the table's own column headers.
  const table = tbody.closest("table");
  const labels = table ? Array.from(table.querySelectorAll("thead th")).map(header => header.textContent.trim()) : [];
  const cells = Array.from(row.children);
  const placeholder = cells.length === 1 && cells[0].colSpan > 1;
  row.setAttribute("role", "row");
  cells.forEach((cell, index) => {
    cell.setAttribute("role", "cell");
    if (!placeholder && labels[index]) cell.dataset.label = labels[index];
  });
  return row;
}

const SIGNAL_QUALITY_LABELS = {excellent: "Excellent", good: "Good", fair: "Fair", poor: "Poor", no_signal: "No signal"};
const RECEPTION_PROBLEM_DETAILS = {
  bad_reception: "Bad reception: the signal is too weak or noisy to be usable.",
  no_signal: "No signal: only static is being received."
};
const RECEPTION_PROBLEM_REASONS = {bad_reception: "bad reception", no_signal: "no signal"};

function streamSignalFor(streamId) {
  const signal = streamSignals && streamSignals[streamId];
  return signal && signal.available ? signal : null;
}

function streamReceptionProblem(signal) {
  if (!signal || !signal.available) return null;
  return RECEPTION_PROBLEM_DETAILS[signal.reception_problem] ? signal.reception_problem : null;
}

function streamSignalDisplay(signal) {
  if (!signal || !signal.available) return {text: "No signal data", quality: "", detail: ""};
  const snr = Number(signal.snr_db);
  const hasSnr = signal.snr_db !== null && Number.isFinite(snr);
  const quality = !hasSnr ? "no_signal" : SIGNAL_QUALITY_LABELS[signal.quality] ? signal.quality : "poor";
  // Below 0 dB the station never breaks through the FM noise floor, so an SNR
  // figure or signal level would only suggest there is something to hear.
  const noSignal = quality === "no_signal";
  const text = noSignal ? SIGNAL_QUALITY_LABELS.no_signal : `${SIGNAL_QUALITY_LABELS[quality]} (${Math.round(snr)} dB SNR)`;
  const parts = [];
  if (!noSignal && Number.isFinite(Number(signal.signal_dbfs)) && signal.signal_dbfs !== null) parts.push(`Signal ${Number(signal.signal_dbfs).toFixed(0)} dBFS`);
  if (Number.isFinite(Number(signal.noise_floor_dbfs)) && signal.noise_floor_dbfs !== null) parts.push(`${parts.length ? "noise" : "Noise"} floor ${Number(signal.noise_floor_dbfs).toFixed(0)} dBFS`);
  return {text, quality, detail: parts.join(", ")};
}

function renderStreamSettingsSignal(stream) {
  const value = document.getElementById("stream_settings_signal_value");
  const enabled = Boolean(stream) && streamIsEnabled(stream);
  const signal = enabled ? streamSignalFor(stream.id) : null;
  const display = streamSignalDisplay(signal);
  let text = display.text;
  if (!enabled) {
    text = "Not measured while this stream is disabled.";
  } else if (streamReceptionProblem(signal) === "no_signal") {
    text = RECEPTION_PROBLEM_DETAILS.no_signal;
  } else if (streamReceptionProblem(signal)) {
    text = `${display.text}. ${RECEPTION_PROBLEM_DETAILS.bad_reception}`;
  }
  setText("stream_settings_signal_value", text);
  const className = display.quality ? `status-text signal-${display.quality}` : "hint";
  if (value.className !== className) value.className = className;
  const showLevels = Boolean(display.detail);
  setHidden("stream_settings_signal_levels_label", !showLevels);
  setHidden("stream_settings_signal_levels", !showLevels);
  setText("stream_settings_signal_levels", display.detail);
}

function streamSignalCell(signal) {
  const display = streamSignalDisplay(signal);
  const cell = tableCell(display.text);
  if (display.quality) {
    cell.className = `status-text signal-${display.quality}`;
    if (display.detail) cell.title = display.detail;
  } else {
    cell.className = "hint";
  }
  return cell;
}

function tableCell(value) {
  const cell = document.createElement("td");
  cell.textContent = String(value);
  return cell;
}

function streamActionsCell(stream) {
  const cell = document.createElement("td");
  cell.className = "menu-cell";
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "More actions";
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-label", `More actions for ${stationLabelForActionMenu(stream)}`);
  button.dataset.activeStreamMenu = stream.id || "";
  const menu = document.createElement("div");
  menu.className = "stream-actions-menu";
  menu.hidden = true;
  menu.setAttribute("role", "menu");
  const toggle = document.createElement("button");
  toggle.type = "button";
  const enabled = streamIsEnabled(stream);
  toggle.textContent = enabled ? "Stop stream" : "Start stream";
  toggle.setAttribute("role", "menuitem");
  toggle.dataset.action = "toggle-active-stream";
  toggle.dataset.streamId = stream.id || "";
  const monitor = document.createElement("button");
  monitor.type = "button";
  monitor.textContent = monitorStreamId === stream.id ? "Stop monitoring" : "Monitor";
  monitor.setAttribute("role", "menuitem");
  monitor.dataset.action = "toggle-monitor";
  monitor.dataset.streamId = stream.id || "";
  const edit = document.createElement("button");
  edit.type = "button";
  edit.textContent = "Edit stream settings";
  edit.setAttribute("role", "menuitem");
  edit.dataset.action = "edit-active-stream";
  edit.dataset.streamId = stream.id || "";
  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "Remove stream";
  remove.setAttribute("role", "menuitem");
  remove.dataset.action = "remove-active-stream";
  remove.dataset.streamId = stream.id || "";
  if (!accountIsReadOnly()) {
    menu.appendChild(toggle);
  }
  if (enabled) {
    menu.appendChild(monitor);
  }
  if (!accountIsReadOnly()) {
    menu.appendChild(edit);
    menu.appendChild(remove);
  }
  cell.appendChild(button);
  cell.appendChild(menu);
  return cell;
}

function stationLabelForActionMenu(stream) {
  const station = stream.station || {};
  if (station.callsign && station.frequency) return `${station.callsign} ${station.frequency} MHz`;
  return station.callsign || "stream";
}

function menuItems(menu) {
  return Array.from(menu.querySelectorAll("[role='menuitem'], [role='menuitemcheckbox']"));
}

function openStreamActionMenu(button, focus = "first") {
  const menu = button.nextElementSibling;
  if (!menu) return;
  closeStreamActionMenus();
  menu.hidden = false;
  button.setAttribute("aria-expanded", "true");
  if (focus) {
    const items = menuItems(menu);
    const target = focus === "last" ? items[items.length - 1] : items[0];
    if (target) target.focus();
  }
}

function closeStreamActionMenu(menu, restoreFocus = true) {
  menu.hidden = true;
  const button = menu.parentElement.querySelector("button[aria-haspopup='menu']");
  if (button) {
    button.setAttribute("aria-expanded", "false");
    if (restoreFocus) button.focus();
  }
}

function closeStreamActionMenus() {
  for (const menu of document.querySelectorAll(".stream-actions-menu")) {
    closeStreamActionMenu(menu, false);
  }
}

function renderStreams(streams, options = {}) {
  configuredStreams = streams || [];
  configuredStreamsSignature = configuredStreamsRefreshSignature(configuredStreams);
  if (monitorStreamId) {
    const monitored = configuredStreams.find(stream => stream.id === monitorStreamId);
    if (!monitored || !streamIsEnabled(monitored)) clearLocalStreamMonitor();
    else updateMonitorMediaSession();
  }
  renderActiveStreams(activeStreamSnapshots, configuredStreams, options);
  if (settingsStreamId) renderStreamSettings();
}

function syncConfiguredStreamsFromStatus(data) {
  if (!data || !Array.isArray(data.streams)) return;
  const signature = configuredStreamsRefreshSignature(data.streams);
  if (signature === configuredStreamsSignature) return;
  renderStreams(data.streams);
}

function configuredStreamsRefreshSignature(streams) {
  return JSON.stringify((streams || []).map(stream => ({
    id: stream.id || "",
    enabled: stream.enabled !== false,
    station: stream.station || {},
    output_count: streamOutputs(stream).length,
    outputs: streamOutputs(stream).map(output => ({
      id: output.id || "",
      type: output.type || "icecast",
      enabled: output.enabled !== false,
      icecast: output.icecast || null,
      soundcard: output.soundcard || null
    })),
    eas_recording: stream.eas_recording || {},
    archive: stream.archive || {},
    fallback_action: stream.fallback_action || {},
    audio_effects: stream.audio_effects || {},
    notifications: stream.notifications || {}
  })));
}

async function loadStreams(options = {}) {
  const data = await request("/api/streams");
  const streams = data.streams || [];
  const signature = configuredStreamsRefreshSignature(streams);
  if (options.force || signature !== configuredStreamsSignature) {
    configuredStreamsSignature = signature;
    renderStreams(streams);
  }
  if (options.refreshEas !== false) {
    await loadEasAlertStreams({preserve: true, quiet: true});
  }
}

function gainIndexFor(value) {
  if (value === null || gainValues.length === 0) return 0;
  return gainValues.reduce((best, gain, index) =>
    Math.abs(gain - value) < Math.abs(gainValues[best] - value) ? index : best, 0);
}

function gainTextForIndex(index) {
  if (!gainValues.length) return "No manual gain values available";
  const safeIndex = Math.max(0, Math.min(gainValues.length - 1, Number(index) || 0));
  return `${gainValues[safeIndex]} dB`;
}

function updateGainSliderText() {
  const gain = document.getElementById("gain");
  const automatic = document.getElementById("gain_auto").checked;
  const text = automatic ? "Automatic" : gainTextForIndex(gain.value);
  setText("gain_label", text);
  setAttributeIfChanged(gain, "aria-valuetext", automatic ? "Automatic gain control" : text);
}

function storedManualGain() {
  if (lastManualGain !== null) return lastManualGain;
  try {
    const stored = Number(window.localStorage.getItem("nwr-stream-manager:last-manual-gain"));
    if (Number.isFinite(stored)) {
      lastManualGain = stored;
      return stored;
    }
  } catch (error) {
    return null;
  }
  return null;
}

function rememberManualGain(value) {
  const gain = Number(value);
  if (!Number.isFinite(gain)) return;
  lastManualGain = gain;
  try {
    window.localStorage.setItem("nwr-stream-manager:last-manual-gain", String(gain));
  } catch (error) {
    // localStorage may be unavailable; keeping the in-memory value is enough.
  }
}

function rememberManualGainFromSlider() {
  if (!gainValues.length) return;
  const index = Number(document.getElementById("gain").value);
  if (!Number.isInteger(index) || index < 0 || index >= gainValues.length) return;
  rememberManualGain(gainValues[index]);
}

function restoreRememberedManualGain() {
  if (!gainValues.length) return;
  const remembered = storedManualGain();
  if (remembered === null) return;
  setValue("gain", gainIndexFor(remembered));
}

function syncSelectOptions(select, options) {
  const active = document.activeElement;
  const existing = new Map(Array.from(select.options).map(option => [option.value, option]));
  const wanted = new Set(options.map(option => String(option.value)));
  for (const option of Array.from(select.options)) {
    if (!wanted.has(option.value)) option.remove();
  }
  let cursor = select.firstChild;
  for (const spec of options) {
    const value = String(spec.value);
    const label = String(spec.label);
    let option = existing.get(value);
    if (!option || option.parentElement !== select) {
      option = document.createElement("option");
      option.value = value;
    }
    if (option.textContent !== label) option.textContent = label;
    if (option !== cursor) {
      select.insertBefore(option, cursor);
    } else {
      cursor = cursor.nextSibling;
    }
  }
  if (active === select && document.activeElement !== select) select.focus();
}

// Screen readers keep their reading position on the DOM node they are on.
// Replacing a list's rows on every status poll destroys that node and throws
// the reader back to the top of the page, so polled lists are updated in
// place: rows are matched by data-row-key and only changed attributes and
// text are written.
function keyedRow(element, key) {
  element.dataset.rowKey = String(key);
  return element;
}

function reconcileKeyedChildren(container, nextChildren) {
  const seen = new Map();
  for (const child of nextChildren) {
    const key = child.dataset.rowKey || "";
    const count = seen.get(key) || 0;
    seen.set(key, count + 1);
    if (count) child.dataset.rowKey = `${key}#${count}`;
  }
  const wanted = new Set(nextChildren.map(child => child.dataset.rowKey));
  const existing = new Map();
  for (const child of Array.from(container.children)) {
    const key = child.dataset.rowKey;
    if (key && wanted.has(key) && !existing.has(key)) {
      existing.set(key, child);
    } else {
      child.remove();
    }
  }
  const currentOrder = new Map(Array.from(container.children).map((child, index) => [child, index]));
  const nodes = nextChildren.map(next => {
    const current = existing.get(next.dataset.rowKey);
    return current ? morphNode(current, next) : next;
  });
  // Moving a row detaches it, which loses a screen reader's place just like
  // replacing it would, so only rows outside the longest already-ordered run
  // are moved.
  const stable = longestOrderedRun(nodes.map(node => currentOrder.has(node) ? currentOrder.get(node) : -1));
  let anchor = null;
  for (let index = nodes.length - 1; index >= 0; index -= 1) {
    const node = nodes[index];
    if (!stable.has(index)) container.insertBefore(node, anchor);
    anchor = node;
  }
}

function longestOrderedRun(positions) {
  // Indexes of the longest strictly increasing subsequence of existing
  // positions (-1 marks new nodes, which are never part of it).
  const tails = [];
  const previous = new Array(positions.length).fill(-1);
  positions.forEach((position, index) => {
    if (position < 0) return;
    let low = 0;
    let high = tails.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (positions[tails[middle]] < position) low = middle + 1;
      else high = middle;
    }
    if (low > 0) previous[index] = tails[low - 1];
    tails[low] = index;
  });
  const stable = new Set();
  let index = tails.length ? tails[tails.length - 1] : -1;
  while (index >= 0) {
    stable.add(index);
    index = previous[index];
  }
  return stable;
}

function preservedAttributes(element) {
  // Open menus and their buttons belong to the user, not to the poll.
  if (element.classList && element.classList.contains("stream-actions-menu")) return ["hidden"];
  if (element.hasAttribute && element.hasAttribute("aria-haspopup")) return ["aria-expanded"];
  return [];
}

function morphAttributes(current, next) {
  const preserved = preservedAttributes(current);
  for (const attribute of Array.from(next.attributes)) {
    if (preserved.includes(attribute.name)) continue;
    if (current.getAttribute(attribute.name) !== attribute.value) current.setAttribute(attribute.name, attribute.value);
  }
  for (const attribute of Array.from(current.attributes)) {
    if (preserved.includes(attribute.name)) continue;
    if (!next.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
  }
}

function morphNode(current, next) {
  if (current.nodeType !== next.nodeType || current.nodeName !== next.nodeName) {
    current.replaceWith(next);
    return next;
  }
  if (current.nodeType === Node.TEXT_NODE) {
    if (current.data !== next.data) current.data = next.data;
    return current;
  }
  if (current.nodeType !== Node.ELEMENT_NODE) return current;
  morphAttributes(current, next);
  const currentChildren = Array.from(current.childNodes);
  const nextChildren = Array.from(next.childNodes);
  nextChildren.forEach((child, index) => {
    if (currentChildren[index]) {
      morphNode(currentChildren[index], child);
    } else {
      current.appendChild(child);
    }
  });
  for (let index = nextChildren.length; index < currentChildren.length; index += 1) {
    currentChildren[index].remove();
  }
  return current;
}

function containsFocusedElement(element) {
  const active = document.activeElement;
  return Boolean(active && element && element.contains(active));
}

function controlSignature(data) {
  const s = data.settings;
  return JSON.stringify({
    serial: s.serial || "",
    gain: s.gain,
    ppm_correction: s.ppm_correction,
    bias_tee: s.bias_tee,
    alias_filter_strength: s.alias_filter_strength,
    notify_sdr_failures: Boolean(s.notify_sdr_failures),
    gain_values: data.gain_values || []
  });
}

function numberFieldIsBeingTyped(element) {
  if (!element || document.activeElement !== element) return false;
  if (element.dataset.userEditing === "1") return true;
  return element.dataset.previousValue !== undefined && element.value !== element.dataset.previousValue;
}

function sdrSettingIsBeingChanged(key) {
  if (pendingSdrSettingEdits.has(key)) return true;
  return key === "ppm_correction" && numberFieldIsBeingTyped(document.getElementById("ppm_correction"));
}

function setValue(id, value) {
  const element = document.getElementById(id);
  const text = String(value);
  if (element.value !== text) element.value = text;
  if (element.type === "number" && element.dataset.userEditing !== "1") {
    element.dataset.previousValue = text;
  }
}

function setChecked(id, value) {
  const element = document.getElementById(id);
  const checked = Boolean(value);
  if (element.checked !== checked) element.checked = checked;
}

function setDisabled(element, value) {
  const disabled = Boolean(value);
  if (element.disabled !== disabled) element.disabled = disabled;
}

function setControlBusy(element, value) {
  if (!element) return;
  const busy = Boolean(value);
  if (busy) {
    element.dataset.busy = "true";
    element.setAttribute("aria-disabled", "true");
  } else {
    delete element.dataset.busy;
    element.removeAttribute("aria-disabled");
  }
}

function setAttributeIfChanged(element, name, value) {
  const text = String(value);
  if (element.getAttribute(name) !== text) element.setAttribute(name, text);
}

function setText(id, value) {
  const element = document.getElementById(id);
  const text = String(value);
  if (element.textContent === text) return;
  // Edit the existing text node rather than replacing it, so a screen reader
  // reading a live value is not moved when it changes.
  if (element.childNodes.length === 1 && element.firstChild.nodeType === Node.TEXT_NODE && text) {
    element.firstChild.data = text;
  } else {
    element.textContent = text;
  }
}

function renderLogs(lines) {
  // One element per line, so a new line is appended and the oldest dropped
  // without replacing the lines a screen reader may be reading.
  const container = document.getElementById("logs");
  const signature = (lines || []).join("\n");
  if (container.dataset.signature === signature) return;
  container.dataset.signature = signature;
  reconcileKeyedChildren(container, (lines || []).map(line => {
    const row = document.createElement("span");
    row.textContent = `${line}\n`;
    return keyedRow(row, line);
  }));
}

function accountIsOwner() {
  return currentAccount && currentAccount.role === "owner";
}

function accountIsReadOnly() {
  return currentAccount && currentAccount.read_only;
}

function formatAccountDate(epochSeconds) {
  const seconds = Number(epochSeconds || 0);
  if (!seconds) return "Never";
  const moment = new Date(seconds * 1000);
  const date = new Intl.DateTimeFormat(undefined, {month: "long", day: "numeric", year: "numeric"}).format(moment);
  const time = moment.toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit", timeZoneName: "short"});
  return `${date}, ${time}`;
}

function setAccountResult(message, kind = "") {
  const element = document.getElementById("account-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setCreateAccountResult(message, kind = "") {
  const element = document.getElementById("create-account-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setChangePasswordResult(message, kind = "") {
  const element = document.getElementById("change-password-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setNotificationResult(message, kind = "") {
  const element = document.getElementById("notification-result");
  if (!element) return;
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

const NTFY_PUBLIC_SERVER_URL = "https://ntfy.sh";
let notificationSettings = {};
let notificationWizardActive = false;
let notificationWizardStep = 0;
let notificationWizardSelfHosted = false;
let notificationWizardServerUrl = NTFY_PUBLIC_SERVER_URL;
let notificationWizardTopic = "";
let notificationAccessUrlOptions = [];
let notificationAccessUrlSelectionSignature = "";
let notificationServerSelectionSignature = "";
let notificationAccessUrlsLoading = false;

const NOTIFICATION_STEP_SERVER_CHOICE = 0;
const NOTIFICATION_STEP_SERVER_URL = 1;
const NOTIFICATION_STEP_APP_INSTALL = 2;
const NOTIFICATION_STEP_TOPIC = 3;
const NOTIFICATION_STEP_ACCESS_URL = 4;
const NOTIFICATION_STEP_TEST = 5;
const NOTIFICATION_WIZARD_STEPS = [
  [NOTIFICATION_STEP_SERVER_CHOICE, "notification_step_server_choice"],
  [NOTIFICATION_STEP_SERVER_URL, "notification_step_server_url"],
  [NOTIFICATION_STEP_APP_INSTALL, "notification_step_app_install"],
  [NOTIFICATION_STEP_TOPIC, "notification_step_topic"],
  [NOTIFICATION_STEP_ACCESS_URL, "notification_step_access_url"],
  [NOTIFICATION_STEP_TEST, "notification_step_test"]
];

function notificationSettingsPayload(overrides = {}) {
  // Everything the server keeps, so saving one setting leaves the others as they are.
  return Object.assign({
    enabled: true,
    server_url: notificationSettings.server_url || NTFY_PUBLIC_SERVER_URL,
    topic: notificationSettings.topic || "",
    priority: notificationSettings.priority || 3,
    access_url_mode: notificationSettings.access_url_mode || "auto",
    custom_access_url: notificationSettings.custom_access_url || "",
    access_token: "",
    keep_access_token: Boolean(notificationSettings.access_token_set),
    username: notificationSettings.username || "",
    password: "",
    keep_password: Boolean(notificationSettings.password_set)
  }, overrides);
}

function notificationWizardAccessUrl() {
  const mode = document.getElementById("notification_wizard_access_url_mode").value || "auto";
  if (mode !== "custom") return {access_url_mode: mode, custom_access_url: notificationSettings.custom_access_url || ""};
  const value = document.getElementById("notification_wizard_custom_access_url").value;
  return {access_url_mode: mode, custom_access_url: normalizeNotificationAccessUrl(value)};
}

function notificationPayload() {
  let access = {};
  try {
    access = notificationWizardAccessUrl();
  } catch (error) {
    access = {};
  }
  return notificationSettingsPayload(Object.assign({
    server_url: notificationWizardServerUrl || NTFY_PUBLIC_SERVER_URL,
    topic: notificationWizardTopic || notificationSettings.topic || ""
  }, access));
}

function setNotificationSettings(settings = {}) {
  const signature = JSON.stringify(settings || {});
  if (signature === notificationSettingsSignature) return;
  const panel = document.getElementById("settings_panel_notifications");
  if (panel && containsFocusedElement(panel) && notificationWizardActive) return;
  notificationSettingsSignature = signature;
  notificationSettings = Object.assign({}, settings || {});
  if (!notificationWizardActive) renderNotificationSettings();
  updateRtlNotificationControlsVisibility();
  if (settingsStreamId) renderStreamSettings();
}

function notificationIsConfigured() {
  return Boolean(notificationSettings && notificationSettings.topic);
}

function updateRtlNotificationControlsVisibility() {
  setHidden("rtl_notifications_section", !notificationIsConfigured() || accountIsReadOnly());
}

function renderNotificationSettings() {
  const intro = document.getElementById("notification_intro");
  const configured = document.getElementById("notification_configured");
  const wizard = document.getElementById("notification_wizard");
  if (!intro || !configured || !wizard) return;
  intro.hidden = notificationWizardActive || notificationIsConfigured();
  configured.hidden = notificationWizardActive || !notificationIsConfigured();
  wizard.hidden = !notificationWizardActive;
  if (notificationIsConfigured()) {
    renderNotificationServerControls();
    setValue("notification_configured_topic_secret", notificationSettings.topic || "");
    renderNotificationAccessUrlControls();
    loadNotificationAccessUrlOptions();
  }
  if (notificationWizardActive) renderNotificationWizard();
}

function notificationServerIsSelfHosted(url = notificationSettings.server_url) {
  return Boolean(url) && url.replace(/[/]+$/, "") !== NTFY_PUBLIC_SERVER_URL;
}

function updateNotificationServerUrlVisibility(mode = document.getElementById("notification_server_mode").value) {
  const selfHosted = mode === "self-hosted";
  setHidden("notification_configured_server_url_label", !selfHosted);
  setHidden("notification_configured_server_url_hint", !selfHosted);
}

function renderNotificationServerControls() {
  const select = document.getElementById("notification_server_mode");
  const input = document.getElementById("notification_configured_server_url");
  const serverUrl = notificationSettings.server_url || NTFY_PUBLIC_SERVER_URL;
  const signature = serverUrl;
  if (notificationServerSelectionSignature !== signature) {
    notificationServerSelectionSignature = signature;
    // Leave the controls alone while they are being changed.
    if (document.activeElement !== select) setValue("notification_server_mode", notificationServerIsSelfHosted(serverUrl) ? "self-hosted" : "ntfy.sh");
    if (document.activeElement !== input) setValue("notification_configured_server_url", notificationServerIsSelfHosted(serverUrl) ? serverUrl : "");
  }
  updateNotificationServerUrlVisibility();
}

async function saveNotificationServerSettings() {
  if (!notificationIsConfigured() || accountIsReadOnly()) return;
  const mode = document.getElementById("notification_server_mode").value;
  updateNotificationServerUrlVisibility(mode);
  if (mode !== "self-hosted") {
    if (notificationServerIsSelfHosted()) await saveNotificationSettings(notificationSettingsPayload({server_url: NTFY_PUBLIC_SERVER_URL}));
    return;
  }
  const value = document.getElementById("notification_configured_server_url").value.trim();
  if (!value) {
    setNotificationResult("Enter the URL of your self-hosted ntfy server.");
    return;
  }
  let serverUrl = "";
  try {
    serverUrl = normalizeNotificationAccessUrl(value);
  } catch (error) {
    setNotificationResult("Enter a valid ntfy server URL, such as ntfy.example.com or https://ntfy.example.com.", "error");
    return;
  }
  if (serverUrl === (notificationSettings.server_url || "").replace(/[/]+$/, "")) return;
  await saveNotificationSettings(notificationSettingsPayload({server_url: serverUrl}));
}

function notificationAccessUrlMode() {
  const select = document.getElementById("notification_access_url_mode");
  return select ? (select.value || "auto") : (notificationSettings.access_url_mode || "auto");
}

function normalizeNotificationAccessUrl(value, defaultScheme = "https") {
  let text = String(value || "").trim().replace(/[/]+$/, "");
  if (!text) throw new Error("missing URL");
  if (!text.includes("://")) text = `${defaultScheme}://${text}`;
  const parsed = new URL(text);
  if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("invalid scheme");
  return text;
}

function updateNotificationCustomUrlVisibility(mode = notificationAccessUrlMode()) {
  setHidden("notification_custom_access_url_label", mode !== "custom");
  setHidden("notification_custom_access_url_hint", mode !== "custom");
}

function updateNotificationWizardCustomUrlVisibility() {
  const custom = document.getElementById("notification_wizard_access_url_mode").value === "custom";
  setHidden("notification_wizard_custom_access_url_label", !custom);
  setHidden("notification_wizard_custom_access_url_hint", !custom);
}

function notificationAccessUrlOptionSpecs() {
  const saved = notificationSettings.access_url_mode || "auto";
  const options = Array.isArray(notificationAccessUrlOptions) ? notificationAccessUrlOptions : [];
  const specs = [{value: "auto", label: options.length ? `Automatic (${options[0].label})` : "Automatic"}];
  for (const option of options) specs.push({value: option.id, label: option.label});
  if (saved !== "auto" && saved !== "custom" && !options.some(option => option.id === saved)) {
    specs.push({value: saved, label: "Previously selected address unavailable"});
  }
  specs.push({value: "custom", label: "Custom URL"});
  return specs;
}

function renderNotificationAccessUrlOptions() {
  const optionSpecs = notificationAccessUrlOptionSpecs();
  const signature = JSON.stringify(optionSpecs);
  for (const id of ["notification_access_url_mode", "notification_wizard_access_url_mode"]) {
    const select = document.getElementById(id);
    if (!select) continue;
    const domSignature = JSON.stringify(Array.from(select.options).map(option => ({value: option.value, label: option.textContent || ""})));
    if (domSignature !== signature) syncSelectOptions(select, optionSpecs);
  }
}

function syncNotificationAccessUrlSelection() {
  const select = document.getElementById("notification_access_url_mode");
  if (!select) return;
  const selected = notificationSettings.access_url_mode || "auto";
  const customUrl = notificationSettings.custom_access_url || "";
  const signature = JSON.stringify({selected, customUrl});
  if (notificationAccessUrlSelectionSignature !== signature) {
    notificationAccessUrlSelectionSignature = signature;
    if (document.activeElement !== select) {
      setValue("notification_access_url_mode", selected);
    }
    setValue("notification_custom_access_url", customUrl);
  }
  updateNotificationCustomUrlVisibility(document.activeElement === select ? notificationAccessUrlMode() : selected);
}

function renderNotificationAccessUrlControls() {
  renderNotificationAccessUrlOptions();
  syncNotificationAccessUrlSelection();
}

async function loadNotificationAccessUrlOptions() {
  if (notificationAccessUrlsLoading || accountIsReadOnly()) return;
  notificationAccessUrlsLoading = true;
  try {
    const data = await request("/api/notification-access-urls");
    notificationAccessUrlOptions = Array.isArray(data.options) ? data.options : [];
    renderNotificationAccessUrlControls();
  } catch (error) {
    setNotificationResult(error.message, "error");
  } finally {
    notificationAccessUrlsLoading = false;
  }
}

async function saveNotificationAccessUrlSettings() {
  if (!notificationIsConfigured() || accountIsReadOnly()) return;
  const mode = notificationAccessUrlMode();
  if (mode !== "custom") {
    await saveNotificationSettings(notificationSettingsPayload({access_url_mode: mode}));
    return;
  }
  const value = document.getElementById("notification_custom_access_url").value;
  if (!value.trim()) return;  // wait for the address
  let customUrl = "";
  try {
    customUrl = normalizeNotificationAccessUrl(value);
  } catch (error) {
    setNotificationResult("Enter a valid custom URL, such as nwr.example.com or https://nwr.example.com.", "error");
    return;
  }
  await saveNotificationSettings(notificationSettingsPayload({access_url_mode: mode, custom_access_url: customUrl}));
}

function startNotificationWizard() {
  notificationWizardActive = true;
  notificationWizardStep = NOTIFICATION_STEP_SERVER_CHOICE;
  notificationWizardSelfHosted = false;
  notificationWizardServerUrl = NTFY_PUBLIC_SERVER_URL;
  notificationWizardTopic = "";
  setChecked("notification_self_hosted_no", true);
  setChecked("notification_self_hosted_yes", false);
  setValue("notification_server_url", "");
  renderNotificationAccessUrlOptions();
  setValue("notification_wizard_access_url_mode", notificationSettings.access_url_mode || "auto");
  setValue("notification_wizard_custom_access_url", notificationSettings.custom_access_url || "");
  loadNotificationAccessUrlOptions();
  setNotificationResult("");
  renderNotificationSettings();
  const first = document.getElementById("notification_self_hosted_no");
  if (first) first.focus();
}

function cancelNotificationWizard() {
  notificationWizardActive = false;
  notificationWizardTopic = "";
  setNotificationResult("");
  renderNotificationSettings();
  const start = document.getElementById("start_notification_setup");
  if (start && !start.hidden && start.getClientRects().length) start.focus();
}

function generateNotificationTopic() {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  const bytes = new Uint8Array(24);
  if (window.crypto && crypto.getRandomValues) {
    crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  }
  let suffix = "";
  for (const value of bytes) suffix += alphabet[value % alphabet.length];
  return `NWRSTMGR-${suffix}`;
}

function notificationWebAppUrl() {
  return `${(notificationWizardServerUrl || NTFY_PUBLIC_SERVER_URL).replace(/[/]+$/, "")}/app`;
}

function renderNotificationWizard() {
  const back = document.getElementById("notification_back");
  const next = document.getElementById("notification_next");
  const finish = document.getElementById("notification_finish");
  if (!back || !next || !finish) return;
  const focused = document.activeElement;
  for (const [step, id] of NOTIFICATION_WIZARD_STEPS) setHidden(id, notificationWizardStep !== step);
  const last = notificationWizardStep === NOTIFICATION_STEP_TEST;
  back.hidden = notificationWizardStep === NOTIFICATION_STEP_SERVER_CHOICE;
  next.hidden = last;
  finish.hidden = !last;
  const webApp = document.getElementById("notification_web_app_link");
  if (webApp && webApp.getAttribute("href") !== notificationWebAppUrl()) webApp.setAttribute("href", notificationWebAppUrl());
  if (notificationWizardStep === NOTIFICATION_STEP_TOPIC) setValue("notification_topic_secret", notificationWizardTopic);
  if (notificationWizardStep === NOTIFICATION_STEP_ACCESS_URL) updateNotificationWizardCustomUrlVisibility();
  // A button that was just pressed may now be hidden; keep keyboard focus in the wizard.
  if (focused && focused.hidden && document.getElementById("notification_wizard").contains(focused)) {
    const target = last ? document.getElementById("wizard_test_notifications") : next;
    target.focus();
  }
}

function notificationWizardServerChoice() {
  const selected = document.querySelector("input[name='notification_self_hosted']:checked");
  return selected ? selected.value : "no";
}

async function saveNotificationSettings(payload = notificationPayload()) {
  if (accountIsReadOnly()) return;
  setNotificationResult("Saving notification settings...");
  const data = await request("/api/notification-settings", {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  setNotificationSettings(data.notifications || {});
  applyStatus(data, {syncControls: false});
  setNotificationResult("Notification settings saved.", "success");
}

async function testNotificationSettings(payload = notificationPayload()) {
  if (accountIsReadOnly()) return;
  setNotificationResult("Sending test notification...");
  const data = await request("/api/notification-test", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  setNotificationResult(data.message || "Test notification sent.", "success");
}

function notificationWizardPreviousStep() {
  if (notificationWizardStep === NOTIFICATION_STEP_APP_INSTALL) {
    return notificationWizardSelfHosted ? NOTIFICATION_STEP_SERVER_URL : NOTIFICATION_STEP_SERVER_CHOICE;
  }
  return Math.max(NOTIFICATION_STEP_SERVER_CHOICE, notificationWizardStep - 1);
}

function showNotificationWizardStep(step) {
  notificationWizardStep = step;
  setNotificationResult("");
  renderNotificationWizard();
}

async function notificationWizardNext() {
  if (notificationWizardStep === NOTIFICATION_STEP_SERVER_CHOICE) {
    notificationWizardSelfHosted = notificationWizardServerChoice() === "yes";
    if (!notificationWizardSelfHosted) notificationWizardServerUrl = NTFY_PUBLIC_SERVER_URL;
    showNotificationWizardStep(notificationWizardSelfHosted ? NOTIFICATION_STEP_SERVER_URL : NOTIFICATION_STEP_APP_INSTALL);
    return;
  }
  if (notificationWizardStep === NOTIFICATION_STEP_SERVER_URL) {
    const value = document.getElementById("notification_server_url").value;
    if (!value.trim()) {
      setNotificationResult("Enter the URL of your self-hosted ntfy server.", "error");
      return;
    }
    try {
      notificationWizardServerUrl = normalizeNotificationAccessUrl(value);
    } catch (error) {
      setNotificationResult("Enter a valid ntfy server URL, such as ntfy.example.com or https://ntfy.example.com.", "error");
      return;
    }
    showNotificationWizardStep(NOTIFICATION_STEP_APP_INSTALL);
    return;
  }
  if (notificationWizardStep === NOTIFICATION_STEP_APP_INSTALL) {
    if (!notificationWizardTopic) notificationWizardTopic = generateNotificationTopic();
    showNotificationWizardStep(NOTIFICATION_STEP_TOPIC);
    return;
  }
  if (notificationWizardStep === NOTIFICATION_STEP_TOPIC) {
    showNotificationWizardStep(NOTIFICATION_STEP_ACCESS_URL);
    return;
  }
  if (notificationWizardStep === NOTIFICATION_STEP_ACCESS_URL) {
    try {
      notificationWizardAccessUrl();
    } catch (error) {
      setNotificationResult("Enter a valid custom URL, such as nwr.example.com or https://nwr.example.com.", "error");
      return;
    }
    showNotificationWizardStep(NOTIFICATION_STEP_TEST);
  }
}

async function finishNotificationWizard() {
  const button = document.getElementById("notification_finish");
  setDisabled(button, true);
  try {
    await saveNotificationSettings(notificationPayload());
  } catch (error) {
    setNotificationResult(error.message, "error");
    return;
  } finally {
    setDisabled(button, false);
  }
  notificationWizardActive = false;
  notificationWizardTopic = "";
  setNotificationResult("");
  renderNotificationSettings();
  showNotificationSetupDialog();
}

function showNotificationSetupDialog() {
  const dialog = document.getElementById("notification_setup_dialog");
  if (!dialog) return;
  dialog.hidden = false;
  dialog.focus();
}

function dismissNotificationSetupDialog() {
  const dialog = document.getElementById("notification_setup_dialog");
  if (!dialog || dialog.hidden) return;
  const hadFocus = containsFocusedElement(dialog);
  dialog.hidden = true;
  const next = document.getElementById("notification_server_mode");
  if (hadFocus && next && next.getClientRects().length) next.focus();
}

function notificationWizardBack() {
  showNotificationWizardStep(notificationWizardPreviousStep());
}

async function copyNotificationTopic(inputId = "notification_topic_secret") {
  const input = document.getElementById(inputId);
  const topic = input ? input.value : "";
  if (!topic) return;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(topic);
    } else if (!copyTextWithSelectionFallback(input)) {
      throw new Error("clipboard API is unavailable");
    }
    setNotificationResult("Topic copied to clipboard.", "success");
  } catch (error) {
    setNotificationResult("Copy failed. Select the topic and copy it manually.", "error");
  }
}

async function regenerateNotificationTopic() {
  if (!notificationIsConfigured()) return;
  if (!window.confirm("Regenerate the notification topic secret? You will need to unsubscribe from the old topic and subscribe to the new one on every device.")) {
    return;
  }
  await saveNotificationSettings(notificationSettingsPayload({topic: generateNotificationTopic()}));
  setNotificationResult("Topic regenerated. Subscribe your devices to the new topic.", "success");
}

function applyAccountUi(account) {
  const previousRole = currentAccount ? currentAccount.role : "";
  const previousReadOnly = currentAccount ? Boolean(currentAccount.read_only) : false;
  currentAccount = account || currentAccount;
  const owner = accountIsOwner();
  const readOnly = accountIsReadOnly();
  const roleChanged = previousRole !== (currentAccount ? currentAccount.role : "") || previousReadOnly !== readOnly;
  setHidden("nav_settings", readOnly);
  setHidden("nav_change_password", !readOnly);
  setHidden("settings_tab_accounts", !owner);
  setHidden("settings_tab_account", owner || readOnly);
  setHidden("settings_tab_advanced", !owner);
  setHidden("settings_tab_tailscale", !owner);
  setHidden("nav_restart", readOnly);
  placeChangePasswordForm();
  setHidden("nav_rtl", readOnly);
  setHidden("dashboard_configure_sdr", readOnly);
  setHidden("open_add_stream", readOnly);
  setHidden("open_iq_start", readOnly);
  setHidden("iq_stop_recording", readOnly);
  setHidden("open_eas_delete", readOnly);
  setHidden("eas_delete_alerts", readOnly);
  setHidden("remove_eas_alert", readOnly);
  updateRtlNotificationControlsVisibility();
  if (roleChanged) {
    // Whether it can delete archived audio.
    if (streamArchives.length) renderStreamArchives(false);
    activeStreamsSignature = "";
    icecastOutputTableSignature = "";
    soundcardOutputTableSignature = "";
    iqRecordingsSignature = "";
    accountsSignature = "";
    dashboardAttentionSignature = "";
    renderActiveStreams(activeStreamSnapshots, configuredStreams);
    renderStreams(configuredStreams);
    renderDashboardStreamAttention(activeStreamSnapshots, configuredStreams);
    renderIqRecordings(iqRecordings);
  }
  if (readOnly && isReadOnlyRestrictedView(currentViewName())) {
    navigateTo("dashboard", {}, true, true);
  }
  if (roleChanged && ["settings", "change_password"].includes(currentViewName())) {
    // Which settings this account has depends on its role. On the first load the
    // page's address hasn't been applied yet, so it still says which setting is open.
    const params = currentViewName() === "settings" && !settingsSection ? routeFromLocation() : currentRouteParams();
    navigateTo(currentViewName(), params, true, true);
  }
}

function isReadOnlyRestrictedView(view) {
  return ["rtl", "add_stream", "stream_settings", "stream_output", "iq_recorder_start", "eas_alert_delete", "stream_archive_delete", "settings", "accounts", "create_account", "notifications"].includes(view);
}

function accountsTableSignature(accounts) {
  return JSON.stringify((accounts || []).map(account => ({
    id: account.id,
    username: account.username,
    role: account.role,
    last_accessed_at: account.last_accessed_at,
    must_change_password: account.must_change_password
  })));
}

function renderAccounts(accounts) {
  const tbody = document.getElementById("accounts-body");
  if (!tbody) return;
  const signature = accountsTableSignature(accounts);
  if (signature === accountsSignature) return;
  accountsSignature = signature;
  if (!accounts || accounts.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.className = "hint";
    cell.textContent = "No accounts.";
    row.appendChild(cell);
    reconcileKeyedChildren(tbody, [keyedRow(labelResponsiveTableRow(row, tbody), "empty")]);
    return;
  }
  reconcileKeyedChildren(tbody, accounts.map(account => {
    const row = document.createElement("tr");
    row.appendChild(tableCell(account.username || ""));
    row.appendChild(tableCell(account.account_type || "Administrator"));
    row.appendChild(tableCell(formatAccountDate(account.last_accessed_at)));
    row.appendChild(accountActionsCell(account));
    return keyedRow(labelResponsiveTableRow(row, tbody), `account:${account.id || ""}`);
  }));
}

function accountActionsCell(account) {
  const cell = document.createElement("td");
  cell.className = "menu-cell";
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "More actions";
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-label", `More actions for ${account.username || "account"}`);
  button.dataset.accountMenu = String(account.id || "");
  const menu = document.createElement("div");
  menu.className = "stream-actions-menu";
  menu.hidden = true;
  menu.setAttribute("role", "menu");
  const reset = document.createElement("button");
  reset.type = "button";
  reset.textContent = currentAccount && currentAccount.id === account.id ? "Change account password" : "Reset password";
  reset.setAttribute("role", "menuitem");
  reset.dataset.action = currentAccount && currentAccount.id === account.id ? "change-own-password" : "reset-account-password";
  reset.dataset.accountId = account.id || "";
  menu.appendChild(reset);
  if (!account.owner) {
    const readOnly = document.createElement("button");
    readOnly.type = "button";
    readOnly.textContent = account.read_only ? "Read-only account checked" : "Read-only account unchecked";
    readOnly.setAttribute("role", "menuitemcheckbox");
    readOnly.setAttribute("aria-checked", account.read_only ? "true" : "false");
    readOnly.dataset.action = "toggle-account-read-only";
    readOnly.dataset.accountId = account.id || "";
    readOnly.dataset.readOnly = account.read_only ? "0" : "1";
    menu.appendChild(readOnly);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Delete account";
    remove.setAttribute("role", "menuitem");
    remove.dataset.action = "delete-account";
    remove.dataset.accountId = account.id || "";
    menu.appendChild(remove);
  }
  cell.appendChild(button);
  cell.appendChild(menu);
  return cell;
}

// General's Save changes shows once a setting has been changed, and saving hides it again.
const GENERAL_SETTING_INPUTS = {instance_name: "general_instance_name", location: "general_location"};
let generalSettingsLoaded = null;
let generalSettingsShowing = false;

function generalSettingsChanges() {
  const changes = {};
  if (!generalSettingsLoaded) return changes;
  for (const [name, id] of Object.entries(GENERAL_SETTING_INPUTS)) {
    const value = document.getElementById(id).value;
    if (value !== generalSettingsLoaded[name]) changes[name] = value;
  }
  return changes;
}

function generalSettingsValues() {
  return Object.values(GENERAL_SETTING_INPUTS).map(id => document.getElementById(id).value);
}

function renderGeneralSettings(data, {keepTyped = false} = {}) {
  generalSettingsLoaded = {instance_name: String(data.instance_name || ""), location: String(data.location || "")};
  if (!keepTyped) {
    for (const [name, id] of Object.entries(GENERAL_SETTING_INPUTS)) setValue(id, generalSettingsLoaded[name]);
  }
  syncGeneralSaveButton();
  setInstanceName(generalSettingsLoaded.instance_name);
}

function syncGeneralSaveButton() {
  setHidden("save_general_settings", !Object.keys(generalSettingsChanges()).length);
}

async function loadGeneralSettings() {
  setMessage("general-result", "");
  generalSettingsLoaded = null;
  syncGeneralSaveButton();
  const before = generalSettingsValues().join("\n");
  try {
    const data = await request("/api/general-settings");
    // Anything typed while it loaded is kept, not replaced.
    renderGeneralSettings(data, {keepTyped: generalSettingsValues().join("\n") !== before});
  } catch (error) {
    setMessage("general-result", error.message, "error");
  }
}

async function saveGeneralSettings() {
  const changes = generalSettingsChanges();
  if (!Object.keys(changes).length) return;
  const hadFocus = document.activeElement;
  try {
    renderGeneralSettings(await request("/api/general-settings", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(changes)
    }));
    setMessage("general-result", "Changes saved.", "success");
    // Save changes is gone now, so focus goes back to the first setting rather than being lost.
    if (hadFocus && hadFocus.id === "save_general_settings") document.getElementById("general_instance_name").focus();
  } catch (error) {
    setMessage("general-result", error.message, "error");
  }
}

// Advanced is saved only when Save changes is selected, not as it's changed:
// a wrong port or address could leave nobody able to reach NWR Stream Manager.
// Tailscale: NWR Stream Manager joined to a tailnet as a device of its own. Checked every
// couple of seconds while it's shown, so signing in on Tailscale's page shows here by itself.
let tailscaleTimer = null;
const TAILSCALE_STATUS_INTERVAL_MS = 2000;

function tailscaleStatusText(status) {
  const state = status.state;
  if (state === "system") {
    const message = "A systemwide installation of Tailscale was detected on this computer, so NWR Stream Manager uses it instead of connecting to Tailscale by itself.";
    return status.signed_out_for_system ? `${message} NWR Stream Manager signed itself out of Tailscale when it found it, so the two aren't on your tailnet at once. Its own device stays in the Tailscale admin console, marked as expired, until you remove it there.` : message;
  }
  if (state === "unavailable") return "Tailscale can't be used here yet: its library (libtailscale) isn't installed. To install it, run nwr-stream-manager --build-libtailscale on this computer, with sudo if NWR Stream Manager runs as a systemd service. It's used as soon as it's built, without restarting.";
  if (state === "off") return "Not connected.";
  if (state === "needs-login") return "Waiting for you to sign in to Tailscale.";
  if (state === "connected") return "Connected to your tailnet.";
  if (state === "error") return `Tailscale couldn't connect: ${status.error || "unknown error"}.`;
  return "Connecting to Tailscale.";
}

function tailscaleHttpsText(https) {
  const state = https.state;
  if (state === "needs-enabling") return "HTTPS is turned off for your tailnet. Turn it on with the link below; the secure address is set up as soon as it is.";
  if (state === "on") return "The secure address is ready.";
  if (state === "error") return `The secure address couldn't be set up: ${https.error || "unknown error"}. NWR Stream Manager keeps trying.`;
  if (state === "starting") return "Setting up the secure address. Getting its certificate can take a minute.";
  return "";
}

function renderTailscaleHttps(status) {
  const https = status.https || {};
  const shown = status.state === "connected";
  setHidden("tailscale_https_section", !shown);
  if (!shown) return;
  // Left alone while a change to it is on its way.
  if (!tailscaleHttpsSaving) setChecked("tailscale_https", Boolean(https.enabled));
  setText("tailscale_https_name", status.name || "");
  const text = https.enabled ? tailscaleHttpsText(https) : "";
  setText("tailscale_https_status", text);
  setHidden("tailscale_https_status", !text);
  const enabling = Boolean(https.enabled) && https.state === "needs-enabling";
  setHidden("tailscale_https_enable", !enabling);
  const link = document.getElementById("tailscale_https_enable_link");
  const url = https.enable_url || "https://login.tailscale.com/admin/dns";
  if (enabling && link.getAttribute("href") !== url) link.setAttribute("href", url);
  renderTailscaleFunnel(status);
}

function tailscaleFunnelText(funnel) {
  const state = funnel.state;
  if (state === "on") return `NWR Stream Manager is reachable from the internet at ${funnel.url || ""}`;
  if (state === "needs-permission") return "Your tailnet doesn't allow this device to use Funnel yet. Allow it with the link below; Funnel opens as soon as it's allowed.";
  if (state === "error") return `Funnel couldn't be opened: ${funnel.error || "unknown error"}. NWR Stream Manager keeps trying.`;
  return "";
}

// Funnel, once the secure address works: it's what Funnel serves.
function renderTailscaleFunnel(status) {
  const https = status.https || {};
  const funnel = status.funnel || {};
  const shown = status.state === "connected" && Boolean(https.enabled) && https.state === "on";
  setHidden("tailscale_funnel_section", !shown);
  const text = shown && funnel.enabled ? tailscaleFunnelText(funnel) : "";
  setText("tailscale_funnel_status", text);
  setHidden("tailscale_funnel_status", !text);
  const allowing = shown && Boolean(funnel.enabled) && funnel.state === "needs-permission";
  setHidden("tailscale_funnel_enable", !allowing);
  if (!shown) return;
  if (!tailscaleHttpsSaving) setChecked("tailscale_funnel", Boolean(funnel.enabled));
  setText("tailscale_funnel_address", funnel.url || "");
  const link = document.getElementById("tailscale_funnel_enable_link");
  const url = funnel.enable_url || "https://login.tailscale.com/admin/acls";
  if (allowing && link.getAttribute("href") !== url) link.setAttribute("href", url);
}

async function saveTailscaleFunnel() {
  const checkbox = document.getElementById("tailscale_funnel");
  if (checkbox.checked && !window.confirm(`Make NWR Stream Manager reachable from the internet? Anyone on the internet can then open its sign-in page, at ${document.getElementById("tailscale_funnel_address").textContent}. Only your accounts' passwords keep them out.`)) {
    checkbox.checked = false;
    return;
  }
  tailscaleHttpsSaving = true;
  setMessage("tailscale-result", "");
  try {
    renderTailscale(await request("/api/tailscale", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: "funnel", enabled: checkbox.checked})
    }));
  } catch (error) {
    setMessage("tailscale-result", error.message, "error");
  } finally {
    tailscaleHttpsSaving = false;
  }
}

let tailscaleHttpsSaving = false;

async function saveTailscaleHttps() {
  tailscaleHttpsSaving = true;
  setMessage("tailscale-result", "");
  try {
    renderTailscale(await request("/api/tailscale", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: "https", enabled: document.getElementById("tailscale_https").checked})
    }));
  } catch (error) {
    setMessage("tailscale-result", error.message, "error");
  } finally {
    tailscaleHttpsSaving = false;
  }
}

function renderTailscale(status) {
  const state = status.state;
  renderTailscaleHttps(status);
  setText("tailscale_status", tailscaleStatusText(status));
  const signingIn = state === "needs-login" && Boolean(status.auth_url);
  setHidden("tailscale_sign_in", !signingIn);
  if (signingIn) {
    const link = document.getElementById("tailscale_sign_in_link");
    if (link.getAttribute("href") !== status.auth_url) link.setAttribute("href", status.auth_url);
    setText("tailscale_sign_in_address", status.auth_url);
  }
  setHidden("tailscale_details", state !== "connected");
  if (state === "connected") {
    const address = document.getElementById("tailscale_address");
    if (address.getAttribute("href") !== status.url) address.setAttribute("href", status.url);
    setText("tailscale_address", status.url || "");
    setText("tailscale_account", status.account || "Unknown");
    setText("tailscale_addresses", (status.addresses || []).join(", "));
  }
  // With Tailscale installed on this computer, the message is all there is.
  setHidden("tailscale_intro", state === "system");
  const connected = !["off", "unavailable", "system"].includes(state);
  setHidden("tailscale_connect", connected || ["unavailable", "system"].includes(state));
  setHidden("tailscale_sign_out", !connected);
  setText("tailscale_sign_out", ["starting", "needs-login"].includes(state) ? "Cancel" : "Sign out of Tailscale");
}

async function loadTailscaleStatus() {
  try {
    renderTailscale(await request("/api/tailscale"));
  } catch (error) {
    setMessage("tailscale-result", error.message, "error");
  }
}

function showTailscaleSettings(showing) {
  if (showing && !tailscaleTimer) {
    setMessage("tailscale-result", "");
    loadTailscaleStatus();
    tailscaleTimer = setInterval(loadTailscaleStatus, TAILSCALE_STATUS_INTERVAL_MS);
  } else if (!showing && tailscaleTimer) {
    clearInterval(tailscaleTimer);
    tailscaleTimer = null;
  }
}

async function tailscaleAction(action) {
  const connected = document.getElementById("tailscale_sign_out").textContent !== "Cancel";
  if (action === "sign_out" && connected
    && !window.confirm("Sign out of Tailscale? Devices on your tailnet will no longer reach NWR Stream Manager there. To connect again, you'll sign in again.")) {
    return;
  }
  setMessage("tailscale-result", "");
  try {
    renderTailscale(await request("/api/tailscale", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action})
    }));
    if (action === "sign_out") setMessage("tailscale-result", connected ? "Signed out of Tailscale." : "Connecting was cancelled.", "success");
  } catch (error) {
    setMessage("tailscale-result", error.message, "error");
  }
}

let advancedSettingsLoaded = null;
let advancedSettingsShowing = false;

function describeWebHost(host) {
  if (["0.0.0.0", "::", ""].includes(host)) return "all";
  if (["127.0.0.1", "localhost", "::1"].includes(host) || host.startsWith("127.")) return "local";
  return "address";
}

function advancedListeningText(data) {
  const listening = data.listening || {};
  const host = String(listening.host || "");
  const where = describeWebHost(host);
  let text = where === "local"
    ? `Right now, NWR Stream Manager only accepts connections from this computer, on port ${listening.port}.`
    : where === "address"
      ? `Right now, NWR Stream Manager is listening on ${host}, port ${listening.port}.`
      : `Right now, NWR Stream Manager accepts connections from other devices on port ${listening.port}.`;
  if (data.restart_needed) text += " Your saved changes take effect the next time it starts.";
  return text;
}

function advancedSavedText(data) {
  const flags = [data.host_flag, data.port_flag].filter(Boolean);
  if (flags.length) return `Changes saved. They take effect once NWR Stream Manager is started without ${flags.join(" or ")}.`;
  return "Changes saved. They take effect the next time NWR Stream Manager starts.";
}

function advancedFlagText(flag) {
  return flag ? `NWR Stream Manager was started with ${flag}, which is used instead of this setting until it's started without it.` : "";
}

function renderAdvancedSettings(data, {keepTyped = false} = {}) {
  advancedSettingsLoaded = {port: String(data.port), localhost_only: Boolean(data.localhost_only)};
  setText("advanced_listening", advancedListeningText(data));
  if (!keepTyped) {
    setValue("advanced_port", advancedSettingsLoaded.port);
    setChecked("advanced_localhost_only", advancedSettingsLoaded.localhost_only);
  }
  setText("advanced_port_flag", advancedFlagText(data.port_flag));
  setHidden("advanced_port_flag", !data.port_flag);
  setText("advanced_host_flag", advancedFlagText(data.host_flag));
  setHidden("advanced_host_flag", !data.host_flag);
}

async function loadAdvancedSettings() {
  setMessage("advanced-result", "");
  const port = document.getElementById("advanced_port");
  const localhostOnly = document.getElementById("advanced_localhost_only");
  const before = [port.value, localhostOnly.checked];
  try {
    const data = await request("/api/advanced-settings");
    // Anything changed while it loaded is kept, not replaced.
    renderAdvancedSettings(data, {keepTyped: port.value !== before[0] || localhostOnly.checked !== before[1]});
  } catch (error) {
    setMessage("advanced-result", error.message, "error");
  }
}

async function saveAdvancedSettings() {
  if (!advancedSettingsLoaded) return;
  const changes = {};
  const port = document.getElementById("advanced_port").value.trim();
  if (port !== advancedSettingsLoaded.port) changes.port = port;
  const localhostOnly = document.getElementById("advanced_localhost_only").checked;
  if (localhostOnly !== advancedSettingsLoaded.localhost_only) changes.localhost_only = localhostOnly;
  try {
    const data = Object.keys(changes).length
      ? await request("/api/advanced-settings", {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(changes)
      })
      : await request("/api/advanced-settings");
    renderAdvancedSettings(data);
    setMessage("advanced-result", advancedSavedText(data), "success");
  } catch (error) {
    setMessage("advanced-result", error.message, "error");
  }
}

async function loadAccounts() {
  const data = await request("/api/accounts");
  renderAccounts(data.accounts || []);
  return data;
}

function showAccountSecret(secret) {
  setValue("account_secret", secret || "");
  const dialog = document.getElementById("account_secret_dialog");
  if (dialog) dialog.hidden = !secret;
}

function dismissAccountSecret() {
  const dialog = document.getElementById("account_secret_dialog");
  if (dialog) dialog.hidden = true;
  setValue("account_secret", "");
}

async function copyAccountSecret() {
  const input = document.getElementById("account_secret");
  const secret = input ? input.value : "";
  if (!secret) return;
  const button = document.getElementById("copy_account_secret");
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(secret);
    } else if (!copyTextWithSelectionFallback(input)) {
      throw new Error("clipboard API is unavailable");
    }
    setAccountResult("Temporary password copied to clipboard.", "success");
    if (button) button.focus();
  } catch (error) {
    setAccountResult("Copy failed. Select the temporary password and copy it manually.", "error");
  }
}

function copyTextWithSelectionFallback(input) {
  if (!input || !document.queryCommandSupported || !document.queryCommandSupported("copy")) {
    return false;
  }
  const active = document.activeElement;
  input.focus();
  input.select();
  input.setSelectionRange(0, input.value.length);
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch (error) {
    copied = false;
  }
  input.setSelectionRange(input.value.length, input.value.length);
  if (active && active.focus && active !== input) active.focus();
  return copied;
}

async function createAccountFromForm() {
  const data = await request("/api/accounts", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      username: document.getElementById("new_account_username").value,
      read_only: document.getElementById("new_account_read_only").checked
    })
  });
  setValue("new_account_username", "");
  setChecked("new_account_read_only", false);
  showAccountSecret(data.secret || "");
  await loadAccounts();
  leaveSettingsSubpage();
  setAccountResult("Account created.", "success");
}

async function resetAccountPassword(accountId) {
  const data = await request("/api/account-reset-password", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({account_id: Number(accountId)})
  });
  showAccountSecret(data.secret || "");
  await loadAccounts();
  setAccountResult("Password reset. The temporary password is shown below.", "success");
}

async function setAccountReadOnly(accountId, readOnly) {
  await request("/api/account-read-only", {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({account_id: Number(accountId), read_only: Boolean(readOnly)})
  });
  await loadAccounts();
  setAccountResult("Account updated.", "success");
}

async function deleteAccount(accountId) {
  await request(`/api/accounts?account_id=${encodeURIComponent(accountId)}`, {method: "DELETE"});
  await loadAccounts();
  setAccountResult("Account deleted.", "success");
}

async function changeOwnPasswordFromForm() {
  const newPassword = document.getElementById("account_new_password").value;
  const confirmPassword = document.getElementById("account_confirm_password").value;
  if (newPassword !== confirmPassword) {
    throw new Error("Passwords do not match.");
  }
  const data = await request("/api/account/password", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      current_password: document.getElementById("account_current_password").value,
      new_password: newPassword,
      confirm_password: confirmPassword
    })
  });
  setChangePasswordResult("Password changed. Log in again with the new password.", "success");
  window.location.replace("/");
  return data;
}

function setStreamResult(message, kind = "") {
  const element = document.getElementById("stream-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setIqRecorderResult(message, kind = "") {
  const element = document.getElementById("iq-recorder-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setIqStartResult(message, kind = "") {
  const element = document.getElementById("iq-start-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setIqDownloadResult(message, kind = "") {
  const element = document.getElementById("iq-download-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setStreamsResult(message, kind = "") {
  const element = document.getElementById("streams-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setStreamSettingsResult(message, kind = "") {
  const element = document.getElementById("stream-settings-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setEasAlertResult(message, kind = "") {
  const element = document.getElementById("eas-alert-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setEasAlertDetailResult(message, kind = "") {
  const element = document.getElementById("eas-alert-detail-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setEasBulkResult(action, message, kind = "") {
  const element = document.getElementById(`eas-${action}-result`);
  if (!element) return;
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function easAlertStreamLabel(stream) {
  const frequency = stream.frequency ? ` ${stream.frequency} MHz` : "";
  const count = Number(stream.alert_count || 0);
  return `${stream.callsign || "Unknown"}${frequency} (${count} alert${count === 1 ? "" : "s"})`;
}

function easAlertStreamsNextSignature(streams) {
  return JSON.stringify((streams || []).map(stream => ({
    id: stream.id || "",
    callsign: stream.callsign || "",
    frequency: stream.frequency || "",
    alert_count: stream.alert_count || 0
  })));
}

function renderEasAlertStreamSelector(streams, preserve = true) {
  const select = document.getElementById("eas_alert_stream");
  const nextSignature = easAlertStreamsNextSignature(streams);
  if (select.dataset.signature !== nextSignature) {
    syncSelectOptions(
      select,
      streams.map(stream => ({value: stream.id || "", label: easAlertStreamLabel(stream)}))
    );
    select.dataset.signature = nextSignature;
  }
  if (!streams.length) {
    easAlertStreamId = "";
    return;
  }
  const currentExists = streams.some(stream => stream.id === easAlertStreamId);
  if (!preserve || !currentExists) {
    easAlertStreamId = streams[0].id || "";
  }
  setValue("eas_alert_stream", easAlertStreamId);
}

async function loadEasAlertStreams(options = {}) {
  let data;
  try {
    data = await request("/api/eas-alert-streams");
  } catch (error) {
    if (!options.quiet) setEasAlertResult(error.message, "error");
    return;
  }
  easAlertStreams = data.streams || [];
  const hasAlerts = easAlertStreams.length > 0;
  document.getElementById("nav_eas_alerts").hidden = !hasAlerts;
  renderEasAlertStreamSelector(easAlertStreams, options.preserve !== false);
  const nextSignature = easAlertStreamsNextSignature(easAlertStreams);
  const streamListChanged = nextSignature !== easAlertStreamsSignature;
  easAlertStreamsSignature = nextSignature;
  if (!hasAlerts) {
    easAlertListSignature = "";
    document.getElementById("eas-alert-list").innerHTML = "";
    setText("eas_alert_page", "Page 1 of 1");
    setDisabled(document.getElementById("eas_alert_prev"), true);
    setDisabled(document.getElementById("eas_alert_next"), true);
    if (["eas_alerts", "eas_alert_export", "eas_alert_delete", "eas_alert_detail"].includes(currentViewName())) {
      navigateTo("dashboard", {}, true, true);
    }
    return;
  }
  if (currentViewName() === "eas_alerts" && (streamListChanged || options.forceList)) {
    await loadEasAlerts({quiet: options.quiet});
  }
}

async function loadEasBulkOptions(options = {}) {
  if (!easAlertStreamId) return;
  try {
    const data = await request(`/api/eas-alert-bulk-options?stream_id=${encodeURIComponent(easAlertStreamId)}`);
    renderEasBulkOptions(data);
    updateEasBulkStreamLabels();
    if (!options.quiet) {
      setEasBulkResult("export", "");
      setEasBulkResult("delete", "");
    }
  } catch (error) {
    if (!options.quiet) {
      setEasBulkResult("export", error.message, "error");
      setEasBulkResult("delete", error.message, "error");
    }
  }
}

function renderEasBulkOptions(data) {
  easBulkServerNow = data.now || null;
  const signature = JSON.stringify(data);
  if (signature === easBulkOptionsSignature) return;
  easBulkOptionsSignature = signature;
  renderBulkOptionGroup("export", data.export_presets || [], "Export all alerts");
  renderBulkOptionGroup("delete", data.delete_presets || [], "Delete all alerts");
  setDisabled(document.getElementById("eas_export_alerts"), Number(data.total || 0) === 0);
  setDisabled(document.getElementById("eas_delete_alerts"), Number(data.total || 0) === 0);
}

function updateEasBulkStreamLabels() {
  const stream = easAlertStreams.find(item => item.id === easAlertStreamId);
  const label = stream ? `Stream: ${easAlertStreamLabel(stream)}` : "";
  setText("eas_export_stream_label", label);
  setText("eas_delete_stream_label", label);
}

function renderBulkOptionGroup(kind, presets) {
  const container = document.getElementById(`eas_${kind}_options`);
  const manual = document.getElementById(`eas_${kind}_manual`);
  container.innerHTML = "";
  for (const preset of presets) {
    const label = document.createElement("label");
    label.className = "checkbox-row";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = `eas_${kind}_mode`;
    input.value = preset.id;
    label.appendChild(input);
    label.appendChild(document.createTextNode(` ${preset.label} (${preset.count})`));
    container.appendChild(label);
  }
  const manualLabel = document.createElement("label");
  manualLabel.className = "checkbox-row";
  const manualInput = document.createElement("input");
  manualInput.type = "radio";
  manualInput.name = `eas_${kind}_mode`;
  manualInput.value = "manual";
  manualLabel.appendChild(manualInput);
  manualLabel.appendChild(document.createTextNode(" Manually select date range"));
  container.appendChild(manualLabel);
  buildManualRangeControls(kind, manual);
  const first = container.querySelector(`input[name='eas_${kind}_mode']`);
  if (first) first.checked = true;
  updateManualRangeVisibility(kind);
}

function buildManualRangeControls(kind, container) {
  container.innerHTML = "";
  const now = easBulkServerNow || currentDateParts();
  const start = offsetDateParts(now, -24 * 60);
  container.appendChild(dateRangeControl(kind, "start", "Start", start, false));
  container.appendChild(dateRangeControl(kind, "end", "End", now, true));
}

function dateRangeControl(kind, edge, labelText, value, includeNowButton) {
  const fieldset = document.createElement("fieldset");
  const legend = document.createElement("legend");
  legend.textContent = labelText;
  fieldset.appendChild(legend);
  const grid = document.createElement("div");
  grid.className = "grid";
  for (const spec of dateControlSpecs(value)) {
    const label = document.createElement("label");
    label.textContent = spec.label;
    const select = document.createElement("select");
    select.id = `eas_${kind}_${edge}_${spec.name}`;
    for (const optionSpec of spec.options) {
      const option = document.createElement("option");
      option.value = String(optionSpec.value);
      option.textContent = optionSpec.label;
      if (optionSpec.value === spec.value) option.selected = true;
      select.appendChild(option);
    }
    label.appendChild(select);
    grid.appendChild(label);
  }
  fieldset.appendChild(grid);
  if (includeNowButton) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Now";
    button.dataset.bulkNow = kind;
    fieldset.appendChild(button);
  }
  return fieldset;
}

function dateControlSpecs(value) {
  const year = Number(value.year);
  const years = [];
  for (let next = year; next >= year - 10; next -= 1) years.push({value: next, label: String(next)});
  return [
    {name: "month", label: "Month", value: Number(value.month), options: Array.from({length: 12}, (_, index) => ({value: index + 1, label: String(index + 1)}))},
    {name: "day", label: "Day", value: Number(value.day), options: Array.from({length: 31}, (_, index) => ({value: index + 1, label: String(index + 1)}))},
    {name: "year", label: "Year", value: year, options: years},
    {name: "hour", label: "Hour", value: hour12(value.hour), options: Array.from({length: 12}, (_, index) => ({value: index + 1, label: String(index + 1)}))},
    {name: "minute", label: "Minute", value: Number(value.minute), options: Array.from({length: 60}, (_, index) => ({value: index, label: String(index).padStart(2, "0")}))},
    {name: "ampm", label: "AM/PM", value: Number(value.hour) >= 12 ? "PM" : "AM", options: [{value: "AM", label: "AM"}, {value: "PM", label: "PM"}]}
  ];
}

function hour12(hour) {
  const value = Number(hour) % 12;
  return value === 0 ? 12 : value;
}

function currentDateParts() {
  const now = new Date();
  return {year: now.getFullYear(), month: now.getMonth() + 1, day: now.getDate(), hour: now.getHours(), minute: now.getMinutes()};
}

function offsetDateParts(parts, offsetMinutes) {
  const date = new Date(Number(parts.year), Number(parts.month) - 1, Number(parts.day), Number(parts.hour), Number(parts.minute) + offsetMinutes);
  return {year: date.getFullYear(), month: date.getMonth() + 1, day: date.getDate(), hour: date.getHours(), minute: date.getMinutes()};
}

function setEndRangeToNow(kind) {
  const now = easBulkServerNow || currentDateParts();
  setRangeControls(kind, "end", now);
}

function setRangeControls(kind, edge, value) {
  setValue(`eas_${kind}_${edge}_month`, value.month);
  setValue(`eas_${kind}_${edge}_day`, value.day);
  setValue(`eas_${kind}_${edge}_year`, value.year);
  setValue(`eas_${kind}_${edge}_hour`, hour12(value.hour));
  setValue(`eas_${kind}_${edge}_minute`, value.minute);
  setValue(`eas_${kind}_${edge}_ampm`, Number(value.hour) >= 12 ? "PM" : "AM");
}

function selectedBulkMode(kind) {
  const selected = document.querySelector(`input[name='eas_${kind}_mode']:checked`);
  return selected ? selected.value : "";
}

function updateManualRangeVisibility(kind) {
  document.getElementById(`eas_${kind}_manual`).hidden = selectedBulkMode(kind) !== "manual";
}

function bulkRangeParams(kind) {
  const params = new URLSearchParams({stream_id: easAlertStreamId, mode: selectedBulkMode(kind)});
  if (selectedBulkMode(kind) === "manual") {
    params.set("start", localRangeDateTime(kind, "start"));
    params.set("end", localRangeDateTime(kind, "end"));
  }
  return params;
}

function localRangeDateTime(kind, edge) {
  const month = String(document.getElementById(`eas_${kind}_${edge}_month`).value).padStart(2, "0");
  const day = String(document.getElementById(`eas_${kind}_${edge}_day`).value).padStart(2, "0");
  const year = document.getElementById(`eas_${kind}_${edge}_year`).value;
  let hour = Number(document.getElementById(`eas_${kind}_${edge}_hour`).value);
  const minute = String(document.getElementById(`eas_${kind}_${edge}_minute`).value).padStart(2, "0");
  const ampm = document.getElementById(`eas_${kind}_${edge}_ampm`).value;
  if (ampm === "AM" && hour === 12) hour = 0;
  if (ampm === "PM" && hour !== 12) hour += 12;
  return `${year}-${month}-${day}T${String(hour).padStart(2, "0")}:${minute}:00`;
}

async function easBulkCount(kind) {
  return request(`/api/eas-alert-range-count?${bulkRangeParams(kind).toString()}`);
}

async function exportEasAlerts() {
  if (!easAlertStreamId) return;
  try {
    const count = await easBulkCount("export");
    if (!count.count) {
      setEasBulkResult("export", "No alerts were issued during this time.", "error");
      return;
    }
    const link = document.createElement("a");
    link.href = `/api/eas-alert-export?${bulkRangeParams("export").toString()}`;
    link.download = "";
    document.body.appendChild(link);
    link.click();
    link.remove();
    navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage}, true, true);
  } catch (error) {
    setEasBulkResult("export", error.message, "error");
  }
}

async function deleteEasAlerts() {
  if (!easAlertStreamId) return;
  if (accountIsReadOnly()) {
    navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage}, true, true);
    return;
  }
  try {
    const count = await easBulkCount("delete");
    if (!count.count) {
      setEasBulkResult("delete", "No alerts were issued during this time.", "error");
      return;
    }
    const confirmed = window.confirm(`${count.count} alert${count.count === 1 ? "" : "s"} and their audio files will be permanently deleted.`);
    if (!confirmed) {
      navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage}, true, true);
      return;
    }
    const response = await request("/api/eas-alert-delete", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(Object.fromEntries(bulkRangeParams("delete").entries()))
    });
    easAlertListSignature = "";
    await loadEasAlertStreams({preserve: true, forceList: true, quiet: true});
    navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage}, true, true);
  } catch (error) {
    setEasBulkResult("delete", error.message, "error");
  }
}

function easAlertListNextSignature(data) {
  return JSON.stringify({
    stream: data.stream && data.stream.id || "",
    page: data.page || 1,
    total_pages: data.total_pages || 1,
    alerts: (data.alerts || []).map(alert => [alert.id, alert.summary])
  });
}

function renderEasAlertList(data) {
  const list = document.getElementById("eas-alert-list");
  const nextSignature = easAlertListNextSignature(data);
  easAlertPage = Number(data.page || 1);
  easAlertTotalPages = Number(data.total_pages || 1);
  setText("eas_alert_page", `Page ${easAlertPage} of ${easAlertTotalPages}`);
  setDisabled(document.getElementById("eas_alert_prev"), easAlertPage <= 1);
  setDisabled(document.getElementById("eas_alert_next"), easAlertPage >= easAlertTotalPages);
  if (nextSignature === easAlertListSignature) return;
  easAlertListSignature = nextSignature;
  const alerts = data.alerts || [];
  if (alerts.length === 0) {
    const empty = document.createElement("div");
    empty.className = "hint";
    empty.textContent = "No EAS alerts recorded for this stream.";
    reconcileKeyedChildren(list, [keyedRow(empty, "empty")]);
    return;
  }
  reconcileKeyedChildren(list, alerts.map(alert => {
    const item = document.createElement("a");
    item.href = routeForView("eas_alert_detail", {
      streamId: easAlertStreamId,
      alertId: alert.id || "",
      page: easAlertPage
    });
    item.className = "stream-item";
    item.textContent = alert.summary || "Unknown EAS alert";
    item.dataset.alertId = alert.id || "";
    return keyedRow(item, `alert:${alert.id || ""}`);
  }));
}

async function loadEasAlerts(options = {}) {
  if (!easAlertStreamId) {
    renderEasAlertList({alerts: [], page: 1, total_pages: 1});
    return;
  }
  try {
    const data = await request(`/api/eas-alerts?stream_id=${encodeURIComponent(easAlertStreamId)}&page=${encodeURIComponent(easAlertPage)}&per_page=${EAS_ALERTS_PER_PAGE}`);
    renderEasAlertList(data);
    setEasAlertResult("");
  } catch (error) {
    if (!options.quiet) setEasAlertResult(error.message, "error");
  }
}

async function fetchEasAlertPage(page) {
  if (!easAlertStreamId) return {alerts: [], page: 1, total_pages: 1};
  return request(`/api/eas-alerts?stream_id=${encodeURIComponent(easAlertStreamId)}&page=${encodeURIComponent(Math.max(1, Number(page || 1)))}&per_page=${EAS_ALERTS_PER_PAGE}`);
}

function currentEasAlertPageIndex() {
  return easAlertDetailPageAlerts.findIndex(alert => alert.id === easAlertDetailId);
}

async function updateEasAlertDetailNavigation() {
  if (!easAlertStreamId || !easAlertDetailId) {
    easAlertDetailPageAlerts = [];
    easAlertDetailTotalPages = 1;
    setEasAlertDetailNavButtons();
    return;
  }
  const pageData = await fetchEasAlertPage(easAlertReturnPage);
  easAlertDetailPageAlerts = pageData.alerts || [];
  easAlertDetailTotalPages = Number(pageData.total_pages || 1);
  setEasAlertDetailNavButtons();
}

function setEasAlertDetailNavButtons() {
  const index = currentEasAlertPageIndex();
  const previousButton = document.getElementById("eas_alert_detail_previous");
  const nextButton = document.getElementById("eas_alert_detail_next");
  const hasPreviousOlder = index >= 0 && (index < easAlertDetailPageAlerts.length - 1 || easAlertReturnPage < easAlertDetailTotalPages);
  const hasNextNewer = index >= 0 && (index > 0 || easAlertReturnPage > 1);
  setDisabled(previousButton, !hasPreviousOlder);
  setDisabled(nextButton, !hasNextNewer);
}

function setEasAlertPlaybackState(playing) {
  easAlertDetailIsPlaying = Boolean(playing);
  const button = document.getElementById("eas_alert_detail_play_pause");
  const playIcon = document.getElementById("eas_alert_detail_play_icon");
  const pauseIcon = document.getElementById("eas_alert_detail_pause_icon");
  const label = easAlertDetailIsPlaying ? "Pause" : "Play";
  if (button) {
    button.setAttribute("aria-label", label);
    button.title = label;
  }
  if (playIcon) playIcon.hidden = easAlertDetailIsPlaying;
  if (pauseIcon) pauseIcon.hidden = !easAlertDetailIsPlaying;
  updateEasAlertMediaSession();
}

async function playCurrentEasAlert() {
  const audio = document.getElementById("eas_alert_audio");
  if (!audio || !audio.src || document.getElementById("eas_alert_app_player").hidden) return;
  if (monitorPeerConnection || receiverPeerConnection) {
    await stopStreamMonitor({notifyServer: true});
    await stopWeatherReceiver({notifyServer: true});
  }
  pauseStreamArchive({forOther: true});
  applyEasAlertPlaybackVolume();
  await audio.play();
  setEasAlertPlaybackState(true);
}

function pauseCurrentEasAlert() {
  const audio = document.getElementById("eas_alert_audio");
  if (audio) audio.pause();
  setEasAlertPlaybackState(false);
}

function stopCurrentEasAlertForSwitch() {
  const audio = document.getElementById("eas_alert_audio");
  if (!audio) {
    setEasAlertPlaybackState(false);
    return;
  }
  try {
    audio.pause();
    if (Number.isFinite(audio.duration) && audio.currentTime !== 0) audio.currentTime = 0;
    audio.removeAttribute("src");
    audio.load();
  } catch (error) {
    console.debug("EAS alert audio stop before switch failed", error);
  }
  setEasAlertPlaybackState(false);
}

function easAlertMediaTitle() {
  const alert = easAlertDetailCurrent || {};
  const eventType = alert.event_type || "EAS alert";
  const issued = alert.issued_at_media || alert.issued_at || "unknown time";
  return `${eventType} from ${issued}`;
}

function easAlertMediaAlbum() {
  const stream = easAlertDetailStream || {};
  return `EAS Alerts from ${stream.callsign || "Unknown"}`;
}

function updateEasAlertPositionState() {
  if (!("mediaSession" in navigator) || typeof navigator.mediaSession.setPositionState !== "function") return;
  const audio = document.getElementById("eas_alert_audio");
  if (!audio || !Number.isFinite(audio.duration) || audio.duration <= 0) return;
  const position = Number.isFinite(audio.currentTime) ? Math.min(Math.max(0, audio.currentTime), audio.duration) : 0;
  try {
    navigator.mediaSession.setPositionState({
      duration: audio.duration,
      playbackRate: Number.isFinite(audio.playbackRate) && audio.playbackRate > 0 ? audio.playbackRate : 1,
      position
    });
  } catch (error) {
    console.debug("EAS alert media session position update failed", error);
  }
}

function seekEasAlertAudio(target) {
  const audio = document.getElementById("eas_alert_audio");
  if (!audio || !Number.isFinite(target)) return;
  const duration = Number.isFinite(audio.duration) ? audio.duration : target;
  const clamped = Math.min(Math.max(0, target), duration);
  if (typeof audio.fastSeek === "function") audio.fastSeek(clamped);
  else audio.currentTime = clamped;
  updateEasAlertPositionState();
}

function setEasAlertMediaAction(action, handler) {
  try {
    navigator.mediaSession.setActionHandler(action, handler);
  } catch (error) {
    console.debug(`EAS alert media session action ${action} is not supported`, error);
  }
}

function updateEasAlertMediaSession() {
  if (!("mediaSession" in navigator) || !("MediaMetadata" in window) || !easAlertDetailCurrent) return;
  if (monitorPeerConnection || receiverPeerConnection) return;
  navigator.mediaSession.metadata = new MediaMetadata({
    title: easAlertMediaTitle(),
    artist: "NWR Stream Manager",
    album: easAlertMediaAlbum()
  });
  navigator.mediaSession.playbackState = easAlertDetailIsPlaying ? "playing" : "paused";
  updateEasAlertPositionState();
  setEasAlertMediaAction("previoustrack", () => {
    navigateEasAlertDetail("previous", {autoplay: true}).catch(error => setEasAlertDetailResult(error.message, "error"));
  });
  setEasAlertMediaAction("nexttrack", () => {
    navigateEasAlertDetail("next", {autoplay: true}).catch(error => setEasAlertDetailResult(error.message, "error"));
  });
  setEasAlertMediaAction("play", () => {
    playCurrentEasAlert().catch(error => setEasAlertDetailResult(`Alert audio could not start: ${error.message}`, "error"));
  });
  setEasAlertMediaAction("pause", () => {
    pauseCurrentEasAlert();
  });
  setEasAlertMediaAction("seekto", event => {
    if (!event || !Number.isFinite(event.seekTime)) return;
    seekEasAlertAudio(event.seekTime);
  });
}

function clearEasAlertMediaSession() {
  easAlertDetailCurrent = null;
  easAlertDetailStream = null;
  if (monitorPeerConnection) updateMonitorMediaSession();
  else if (receiverPeerConnection) updateReceiverMediaSession();
  else clearLiveMediaSession();
}

function clearEasAlertBrowserMediaSession() {
  if (monitorPeerConnection) updateMonitorMediaSession();
  else if (receiverPeerConnection) updateReceiverMediaSession();
  else clearLiveMediaSession();
}

async function navigateEasAlertDetail(direction, options = {}) {
  const replace = options.replace === true;
  const autoplay = options.autoplay === true;
  await updateEasAlertDetailNavigation();
  const index = currentEasAlertPageIndex();
  if (index < 0) return false;
  let target = null;
  let targetPage = easAlertReturnPage;
  if (direction === "previous") {
    if (index < easAlertDetailPageAlerts.length - 1) {
      target = easAlertDetailPageAlerts[index + 1];
    } else if (easAlertReturnPage < easAlertDetailTotalPages) {
      targetPage = easAlertReturnPage + 1;
      const pageData = await fetchEasAlertPage(targetPage);
      target = (pageData.alerts || [])[0] || null;
    }
  } else if (direction === "next") {
    if (index > 0) {
      target = easAlertDetailPageAlerts[index - 1];
    } else if (easAlertReturnPage > 1) {
      targetPage = easAlertReturnPage - 1;
      const pageData = await fetchEasAlertPage(targetPage);
      const alerts = pageData.alerts || [];
      target = alerts[alerts.length - 1] || null;
    }
  }
  if (!target || !target.id) {
    setEasAlertDetailNavButtons();
    return false;
  }
  stopCurrentEasAlertForSwitch();
  easAlertDetailId = target.id;
  easAlertReturnPage = targetPage;
  navigateTo("eas_alert_detail", {
    streamId: easAlertStreamId,
    alertId: easAlertDetailId,
    page: easAlertReturnPage
  }, replace, true);
  if (autoplay) {
    window.setTimeout(() => {
      playCurrentEasAlert().catch(error => setEasAlertDetailResult(`Alert audio could not start: ${error.message}`, "error"));
    }, 250);
  }
  return true;
}

async function loadEasAlertDetail() {
  if (!easAlertStreamId || !easAlertDetailId) {
    setEasAlertDetailResult("EAS alert was not found.", "error");
    return;
  }
  try {
    const data = await request(`/api/eas-alert?stream_id=${encodeURIComponent(easAlertStreamId)}&alert_id=${encodeURIComponent(easAlertDetailId)}`);
    const alert = data.alert || {};
    easAlertDetailStream = data.stream || null;
    easAlertDetailCurrent = alert;
    setText("eas_alert_detail_title", alert.event_type || "EAS alert");
    setText("eas_detail_event", alert.event_type || "Unknown event");
    setText("eas_detail_areas", Array.isArray(alert.areas) && alert.areas.length ? alert.areas.join(", ") : "Unknown area");
    setText("eas_detail_issued", alert.issued_at || "Unknown");
    setText("eas_detail_expires", alert.expires_at || "Unknown");
    const audio = document.getElementById("eas_alert_audio");
    // Played here only where the browser can play its format; otherwise it can be downloaded.
    const playable = Boolean(alert.audio_type && audio.canPlayType && audio.canPlayType(alert.audio_type));
    setHidden("eas_alert_app_player", !playable);
    setHidden("eas_alert_cannot_play", playable);
    if (!playable) stopCurrentEasAlertForSwitch();
    if (playable && audio.src !== alert.audio_url) {
      audio.src = alert.audio_url || "";
      audio.currentTime = 0;
      applyEasAlertPlaybackVolume();
      audio.load();
    }
    document.getElementById("eas_alert_download").href = alert.download_url || "#";
    document.getElementById("back_to_eas_alerts").href = routeForView("eas_alerts", {streamId: easAlertStreamId, page: easAlertReturnPage});
    await updateEasAlertDetailNavigation();
    setEasAlertPlaybackState(Boolean(audio && !audio.paused && !audio.ended));
    setPageTitle(`Details for alert ${alert.event_type || "EAS alert"} issued on ${alert.issued_at || "an unknown time"}`);
    setEasAlertDetailResult("");
  } catch (error) {
    setEasAlertDetailResult(error.message, "error");
  }
}

async function removeCurrentEasAlert() {
  if (!easAlertStreamId || !easAlertDetailId) return;
  if (accountIsReadOnly()) {
    setEasAlertDetailResult("This account is read-only.", "error");
    return;
  }
  const button = document.getElementById("remove_eas_alert");
  setDisabled(button, true);
  try {
    await request(`/api/eas-alert?stream_id=${encodeURIComponent(easAlertStreamId)}&alert_id=${encodeURIComponent(easAlertDetailId)}`, {
      method: "DELETE"
    });
    easAlertDetailId = "";
    easAlertListSignature = "";
    await loadEasAlertStreams({preserve: true, forceList: true, quiet: true});
    navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertReturnPage}, true, true);
  } catch (error) {
    setEasAlertDetailResult(error.message, "error");
  } finally {
    setDisabled(button, false);
  }
}

// The title names this NWR Stream Manager by its instance name, as the server's pages do.
let instanceName = (document.getElementById("instance_name_meta") || {}).content || "NWR Stream Manager";
let pageTitle = "";

function setPageTitle(title) {
  pageTitle = String(title || "").trim();
  const full = pageTitle && pageTitle !== "NWR Stream Manager" ? `${pageTitle} - ${instanceName}` : instanceName;
  if (document.title !== full) document.title = full;
}

function setInstanceName(name) {
  if (!name || name === instanceName) return;
  instanceName = name;
  setPageTitle(pageTitle);
  setText("site_heading", name);
}

function defaultViewTitle(name) {
  const titles = {
    dashboard: "Dashboard",
    rtl: "Configure RTL-SDR",
    receiver: "Weather Radio Receiver",
    iq_recorder: "I/Q Recorder",
    iq_recorder_start: "New I/Q Recording",
    iq_recording_download: "Download I/Q Recording",
    logs: "Logs",
    settings: "Settings",
    change_password: "Change Account Password",
    streams: "Manage Streams",
    add_stream: "Add Stream",
    stream_settings: "Stream Settings",
    stream_output: "Icecast Output",
    eas_alerts: "EAS Alerts",
    eas_alert_export: "Export Alerts",
    eas_alert_delete: "Delete Alerts",
    eas_alert_detail: "EAS Alert Details",
    stream_archives: "Stream Archives",
    stream_archive_delete: "Delete Archived Audio"
  };
  return titles[name] || "NWR Stream Manager";
}

function streamCallsign(stream) {
  const station = stream && stream.station ? stream.station : {};
  return station.callsign || "stream";
}

function focusViewHeading(name) {
  const view = document.getElementById(`view_${name}`);
  if (!view) return;
  const heading = view.querySelector("h1, h2");
  if (!heading) return;
  if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
  window.requestAnimationFrame(() => {
    try {
      heading.focus({preventScroll: true});
    } catch (error) {
      heading.focus();
    }
  });
}

function showView(name, options = {}) {
  if (name !== "stream_settings" && activeTestModeBelongsToThisPage()) {
    stopStreamTestMode({beacon: true});
  }
  for (const view of document.querySelectorAll(".view")) {
    view.hidden = view.id !== `view_${name}`;
  }
  setPageTitle(defaultViewTitle(name));
  if (name !== "settings") syncRemotePairingPage("");
  if (name !== "settings") stopAnnouncementPlaybackOffPage(name, null);
  if (name !== "settings") advancedSettingsShowing = false;
  if (name !== "settings") showTailscaleSettings(false);
  if (name !== "settings") generalSettingsShowing = false;
  if (options.focus !== false) focusViewHeading(name);
  const moreButton = document.getElementById("nav_more_button");
  for (const item of document.querySelectorAll("nav [data-view]")) {
    if (
      item.dataset.view === name ||
      (item.dataset.view === "streams" && ["add_stream", "stream_settings", "stream_output"].includes(name)) ||
      (item.dataset.view === "iq_recorder" && ["iq_recorder_start", "iq_recording_download"].includes(name)) ||
      (item.dataset.view === "eas_alerts" && ["eas_alert_export", "eas_alert_delete", "eas_alert_detail"].includes(name)) ||
      (item.dataset.view === "stream_archives" && name === "stream_archive_delete")
    ) {
      item.setAttribute("aria-current", "page");
    } else {
      item.removeAttribute("aria-current");
    }
  }
  if (moreButton) {
    if (["receiver", "iq_recorder", "iq_recorder_start", "iq_recording_download", "logs", "settings", "change_password"].includes(name)) {
      moreButton.setAttribute("aria-current", "page");
    } else {
      moreButton.removeAttribute("aria-current");
    }
  }
}

function currentViewName() {
  const visible = Array.from(document.querySelectorAll(".view")).find(view => !view.hidden);
  return visible ? visible.id.replace(/^view_/, "") : "dashboard";
}

function isEasAlertsView(name) {
  return ["eas_alerts", "eas_alert_export", "eas_alert_delete", "eas_alert_detail"].includes(name);
}

function easRouteFromParams(view, params = {}) {
  return {
    view,
    streamId: params.streamId || "",
    alertId: params.alertId || "",
    page: Math.max(1, Number(params.page || 1))
  };
}

function rememberEasRoute(view, params = {}) {
  if (view === "eas_alert_detail" && params.alertId) {
    easAlertLastRoute = easRouteFromParams(view, params);
    return;
  }
  if (view === "eas_alerts") {
    easAlertLastRoute = easRouteFromParams(view, params);
  }
}

function restoredEasRoute() {
  return Object.assign({view: "eas_alerts", streamId: "", alertId: "", page: 1}, easAlertLastRoute || {});
}

function routeForView(name, params = {}) {
  const query = new URLSearchParams();
  if (name === "dashboard") return "/";
  if (name === "rtl") query.set("view", "rtl");
  if (name === "receiver") query.set("view", "receiver");
  if (name === "iq_recorder") query.set("view", "iq_recorder");
  if (name === "iq_recorder_start") query.set("view", "iq_recorder_start");
  if (name === "iq_recording_download") {
    query.set("view", "iq_recording_download");
    if (params.recordingId) query.set("recording", params.recordingId);
  }
  if (name === "logs") query.set("view", "logs");
  if (name === "settings") {
    query.set("view", "settings");
    if (params.section) query.set("section", params.section);
  }
  if (name === "change_password") query.set("view", "change_password");
  if (name === "streams") query.set("view", "streams");
  if (name === "add_stream") {
    query.set("view", "add_stream");
    if (params.streamId) query.set("stream", params.streamId);
  }
  if (name === "stream_settings") {
    query.set("view", "stream_settings");
    if (params.streamId) query.set("stream", params.streamId);
    if (params.section) query.set("tab", params.section);
  }
  if (name === "stream_output") {
    query.set("view", "stream_output");
    if (params.streamId) query.set("stream", params.streamId);
    if (params.outputId) query.set("output", params.outputId);
  }
  if (name === "stream_archive_delete") {
    query.set("view", "stream_archive_delete");
    if (params.streamId) query.set("stream", params.streamId);
  }
  if (name === "stream_archives") {
    query.set("view", "stream_archives");
    if (params.streamId) query.set("stream", params.streamId);
    // Where the archive can be played, downloading it is a page of its own.
    if (params.mode === "download") query.set("download", "1");
  }
  if (name === "eas_alerts") {
    query.set("view", "eas_alerts");
    if (params.streamId) query.set("stream", params.streamId);
    if (params.page) query.set("page", params.page);
  }
  if (name === "eas_alert_export") {
    query.set("view", "eas_alert_export");
    if (params.streamId) query.set("stream", params.streamId);
    if (params.page) query.set("page", params.page);
  }
  if (name === "eas_alert_delete") {
    query.set("view", "eas_alert_delete");
    if (params.streamId) query.set("stream", params.streamId);
    if (params.page) query.set("page", params.page);
  }
  if (name === "eas_alert_detail") {
    query.set("view", "eas_alert_detail");
    if (params.streamId) query.set("stream", params.streamId);
    if (params.alertId) query.set("alert", params.alertId);
    if (params.page) query.set("page", params.page);
  }
  const text = query.toString();
  return text ? `/?${text}` : "/";
}

// Pages that are now sections of Settings, so older links and bookmarks still work.
const SETTINGS_SECTIONS_FROM_OLD_VIEWS = {notifications: "notifications", accounts: "accounts", create_account: "create_account"};

function routeFromLocation() {
  const query = new URLSearchParams(window.location.search);
  const view = query.get("view") || "dashboard";
  if (SETTINGS_SECTIONS_FROM_OLD_VIEWS[view]) {
    return routeState("settings", {section: SETTINGS_SECTIONS_FROM_OLD_VIEWS[view]});
  }
  if (view === "settings") return routeState("settings", {section: query.get("section") || ""});
  if (["dashboard", "rtl", "receiver", "iq_recorder", "iq_recorder_start", "iq_recording_download", "logs", "change_password", "streams", "add_stream", "stream_settings", "stream_output", "eas_alerts", "eas_alert_export", "eas_alert_delete", "eas_alert_detail", "stream_archives", "stream_archive_delete"].includes(view)) {
    return {
      view,
      streamId: query.get("stream") || "",
      outputId: query.get("output") || "",
      alertId: query.get("alert") || "",
      recordingId: query.get("recording") || "",
      page: Math.max(1, Number(query.get("page") || 1)),
      // A stream's settings section, named as it is without the page script.
      section: view === "stream_settings" ? query.get("tab") || "" : "",
      mode: view === "stream_archives" && query.get("download") === "1" ? "download" : ""
    };
  }
  return {view: "dashboard", streamId: "", outputId: "", alertId: "", page: 1};
}

function routeState(view, params = {}) {
  return {
    view,
    streamId: params.streamId || "",
    outputId: params.outputId || "",
    alertId: params.alertId || "",
    recordingId: params.recordingId || "",
    page: Math.max(1, Number(params.page || 1)),
    section: params.section || "",
    mode: params.mode || "",
    // The settings page this one was opened from, so Back can return to it.
    openedFrom: typeof params.openedFrom === "string" ? params.openedFrom : null
  };
}

function replaceCurrentRoute(view, params = {}) {
  history.replaceState(routeState(view, params), "", routeForView(view, params));
}

function applyRoute(route) {
  if (accountIsReadOnly() && isReadOnlyRestrictedView(route.view)) {
    replaceCurrentRoute("dashboard");
    showView("dashboard");
    return;
  }
  if (route.view === "change_password" && !accountIsReadOnly()) {
    // Other accounts change their password in Settings.
    route = routeState("settings", {section: "account"});
    replaceCurrentRoute("settings", route);
  }
  if (route.view === "settings") {
    settingsStreamId = "";
    applySettingsRoute(route);
    return;
  }
  if (route.view === "change_password") {
    placeChangePasswordForm();
    setChangePasswordResult("");
  }
  if (route.view === "add_stream") {
    if (route.streamId) {
      if (wizardMode !== "add_output" || settingsStreamId !== route.streamId) {
        if (!prepareAddOutputWizard(route.streamId)) {
          replaceCurrentRoute("streams");
          showView("streams");
          return;
        }
      }
    } else if (wizardMode !== "add") {
      resetStreamWizardState();
    }
    showView("add_stream");
    renderWizard();
    return;
  }
  if (route.view === "stream_settings") {
    const streamId = route.streamId || settingsStreamId;
    const stream = configuredStreams.find(item => item.id === streamId);
    if (stream) {
      if (activeTestModeBelongsToThisPage() && streamTestMode.stream_id !== streamId) {
        stopStreamTestMode({beacon: true});
      }
      const wasShowing = currentViewName() === "stream_settings" && settingsStreamId === streamId;
      const previous = wasShowing ? streamSettingsSection : "";
      settingsStreamId = streamId;
      icecastOutputTableSignature = "";
      soundcardOutputTableSignature = "";
      easSignature = "";
      audioEffectsSignature = "";
      streamNotificationsSignature = "";
      streamFallbackActionSignature = "";
  streamFallbackActionSignature = "";
      streamTestModeSignature = "";
      closeOutputForm();
      const station = stream.station || {};
      setText("stream_settings_title", `Edit stream ${streamCallsign(stream)}`);
      setText("stream_settings_station", `${station.callsign || "Unknown"} ${station.frequency || ""} MHz`);
      // Shows which sections this stream has, before choosing one.
      renderStreamSettings();
      let section = normalizeStreamSettingsSection(route.section);
      if (!section && !settingsSinglePane()) section = "outputs";
      streamSettingsSection = section;
      streamSettingsOpenedFrom = section === route.section ? route.openedFrom : null;
      if (section !== route.section) {
        replaceCurrentRoute("stream_settings", {streamId, section, openedFrom: streamSettingsOpenedFrom});
      }
      // As in Settings: arriving goes to the page's heading, opening a section goes
      // to its heading, and going back to the list goes to the section that was left.
      const focusPage = Boolean(section) && (wasShowing ? section !== previous : settingsSinglePane() && Boolean(route.section));
      showView("stream_settings", {focus: !wasShowing && !focusPage});
      renderStreamSettingsSection();
      if (focusPage) {
        focusSettingsElement(streamSettingsPageHeading());
      } else if (wasShowing && !section && previous) {
        focusSettingsElement(document.getElementById(`tab_${previous}`));
      }
      return;
    }
    closeOutputForm();
    replaceCurrentRoute("streams");
    showView("streams");
    return;
  }
  if (route.view === "stream_output") {
    const streamId = route.streamId || settingsStreamId;
    const stream = configuredStreams.find(item => item.id === streamId);
    if (!stream) {
      closeOutputForm();
      replaceCurrentRoute("streams");
      showView("streams");
      return;
    }
    if (!route.outputId) {
      if (prepareAddOutputWizard(streamId)) {
        showView("add_stream");
        renderWizard();
      } else {
        showView("streams");
      }
      return;
    }
    settingsStreamId = streamId;
    const station = stream.station || {};
    setText("stream_settings_station", `${station.callsign || "Unknown"} ${station.frequency || ""} MHz`);
    if (editingOutputId !== route.outputId || !["edit", "edit_soundcard"].includes(outputFormMode)) {
      if (!prepareEditOutput(route.outputId)) {
        closeOutputForm();
        navigateTo("stream_settings", {streamId, section: "outputs"}, true, true);
        return;
      }
    }
    const selected = findConfiguredOutput(streamId, route.outputId);
    const isSoundcard = selected && (selected.output.type || "icecast") === "soundcard";
    setText("output_form_title", `${isSoundcard ? "Edit sound card output" : "Edit output"} for ${streamCallsign(stream)}`);
    showView("stream_output");
    setPageTitle(`${isSoundcard ? "Edit sound card output" : "Edit output"} for ${streamCallsign(stream)}`);
    return;
  }
  if (route.view === "stream_archives") {
    streamArchiveStreamId = route.streamId || streamArchiveStreamId;
    streamArchiveDownloadMode = route.mode === "download";
    showView("stream_archives");
    loadStreamArchives().catch(error => setStreamArchiveResult(error.message, "error"));
    return;
  }
  if (route.view === "stream_archive_delete") {
    streamArchiveStreamId = route.streamId || streamArchiveStreamId;
    showView("stream_archive_delete");
    loadStreamArchiveDeleteChoices().catch(error => setStreamArchiveDeleteResult(error.message, "error"));
    return;
  }
  if (route.view === "eas_alerts") {
    easAlertStreamId = route.streamId || easAlertStreamId;
    easAlertPage = Math.max(1, Number(route.page || 1));
    rememberEasRoute("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage});
    showView("eas_alerts");
    loadEasAlertStreams({preserve: true, forceList: true});
    return;
  }
  if (route.view === "eas_alert_export" || route.view === "eas_alert_delete") {
    easAlertStreamId = route.streamId || easAlertStreamId;
    easAlertPage = Math.max(1, Number(route.page || easAlertPage || 1));
    easBulkOptionsSignature = "";
    showView(route.view);
    loadEasAlertStreams({preserve: true, quiet: true}).then(() => loadEasBulkOptions());
    return;
  }
  if (route.view === "eas_alert_detail") {
    easAlertStreamId = route.streamId || easAlertStreamId;
    easAlertDetailId = route.alertId || "";
    easAlertReturnPage = Math.max(1, Number(route.page || 1));
    rememberEasRoute("eas_alert_detail", {streamId: easAlertStreamId, alertId: easAlertDetailId, page: easAlertReturnPage});
    showView("eas_alert_detail");
    loadEasAlertDetail();
    return;
  }
  if (route.view !== "stream_settings") settingsStreamId = "";
  if (route.view === "rtl" && developmentIqSourcesEnabled) {
    loadIqTestSources({force: true}).catch(error => setText("iq-test-source-result", error.message));
  }
  if (route.view === "receiver") renderReceiverControls();
  if (route.view === "iq_recorder") {
    loadIqRecordings().catch(error => setIqRecorderResult(error.message, "error"));
  }
  if (route.view === "iq_recorder_start") {
    restoreIqRecorderPreferences();
  }
  if (route.view === "iq_recording_download") {
    selectedIqRecordingId = route.recordingId || selectedIqRecordingId;
    loadIqRecordings()
      .then(() => renderIqDownloadPage())
      .catch(error => setIqDownloadResult(error.message, "error"));
  }
  showView(route.view);
}

// Settings shows its list beside the selected setting on wider screens. On phones,
// in either orientation, it shows the list, then one setting at a time with a
// way back, the way phone settings apps do.
const SETTINGS_SINGLE_PANE_QUERY = "(max-width: 680px), (max-width: 900px) and (orientation: landscape), (orientation: landscape) and (max-height: 500px)";
const SETTINGS_SECTION_TITLES = {
  general: "General",
  accounts: "Accounts",
  create_account: "Create account",
  account: "Account",
  sdr_sharing: "SDR sharing",
  pair_device: "Pair a device",
  find_devices: "Find devices",
  notifications: "Notifications",
  fallback_audio: "Fallback audio",
  record_announcement: "Record an announcement",
  tailscale: "Tailscale",
  advanced: "Advanced"
};
// How "Back to ..." names the page a sub-page belongs to.
const SETTINGS_BACK_NAMES = {accounts: "accounts", sdr_sharing: "SDR sharing", fallback_audio: "fallback audio"};
let settingsSection = "";
let settingsOpenedFrom = null;

function settingsSinglePane() {
  return window.matchMedia(SETTINGS_SINGLE_PANE_QUERY).matches;
}

function settingsTabs() {
  return Array.from(document.querySelectorAll("#settings_list [data-settings-section]"));
}

function availableSettingsTabs() {
  return settingsTabs().filter(tab => !tab.hidden);
}

function settingsSectionsForAccount() {
  if (accountIsReadOnly()) return [];
  // Only the owner chooses where NWR Stream Manager can be reached.
  if (accountIsOwner()) return ["general", "accounts", "create_account", "account", "sdr_sharing", "pair_device", "find_devices", "notifications", "fallback_audio", "record_announcement", "tailscale", "advanced"];
  return ["general", "account", "sdr_sharing", "pair_device", "find_devices", "notifications", "fallback_audio", "record_announcement"];
}

function normalizeSettingsSection(section) {
  const sections = settingsSectionsForAccount();
  if (!section || !sections.length) return "";
  // Only the owner manages accounts; everyone else has their own account.
  if (!accountIsOwner() && ["accounts", "create_account"].includes(section)) return "account";
  return sections.includes(section) ? section : "";
}

// The entry in the list that a page of Settings belongs to. The owner's own
// password and new accounts are pages within Accounts.
function settingsTabSection(section) {
  if (section === "create_account") return "accounts";
  if (section === "pair_device" || section === "find_devices") return "sdr_sharing";
  if (section === "record_announcement") return "fallback_audio";
  if (section === "account" && accountIsOwner()) return "accounts";
  return section;
}

// Where Back goes from a page of Settings: "" is the list itself.
function settingsParentSection(section) {
  const tabSection = settingsTabSection(section);
  return tabSection !== section ? tabSection : "";
}

function defaultSettingsSection() {
  const first = availableSettingsTabs()[0];
  return first ? first.dataset.settingsSection : "";
}

function placeChangePasswordForm() {
  // One password form serves the read-only page, an administrator's Account
  // setting and the owner's page within Accounts.
  const form = document.getElementById("change_password_form");
  if (!form) return;
  let home = document.getElementById("settings_panel_account");
  if (accountIsReadOnly()) home = document.querySelector("#view_change_password section");
  else if (accountIsOwner()) home = document.getElementById("settings_owner_password_page");
  if (home && form.parentNode !== home) home.appendChild(form);
}

function renderSettingsSection() {
  const section = settingsSection;
  const tabSection = settingsTabSection(section);
  const singlePane = settingsSinglePane();
  const list = document.getElementById("settings_list");
  placeChangePasswordForm();
  // On wider screens the list is a set of tabs: Tab reaches the selected one, the
  // arrow keys move between them, and Tab again goes into that setting. On phones
  // each entry is a button that opens its setting.
  if (singlePane) {
    list.setAttribute("role", "group");
    list.removeAttribute("aria-orientation");
  } else {
    list.setAttribute("role", "tablist");
    list.setAttribute("aria-orientation", "vertical");
  }
  for (const tab of settingsTabs()) {
    const name = tab.dataset.settingsSection;
    const selected = name === tabSection;
    // Marked by the server for browsers without this script.
    tab.removeAttribute("aria-current");
    if (singlePane) {
      tab.removeAttribute("role");
      tab.removeAttribute("aria-selected");
      tab.removeAttribute("aria-controls");
      tab.tabIndex = 0;
    } else {
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.setAttribute("aria-controls", `settings_panel_${name}`);
      tab.tabIndex = selected ? 0 : -1;
    }
  }
  for (const panel of document.querySelectorAll(".settings-panel")) {
    const name = panel.id.replace(/^settings_panel_/, "");
    panel.hidden = name !== tabSection;
    if (singlePane) {
      panel.removeAttribute("role");
      panel.removeAttribute("aria-labelledby");
    } else {
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", `settings_tab_${name}`);
    }
  }
  setHidden("settings_accounts_page", section !== "accounts");
  setHidden("settings_create_account_page", section !== "create_account");
  setHidden("settings_owner_password_page", !(section === "account" && accountIsOwner()));
  setHidden("settings_sdr_sharing_page", section !== "sdr_sharing");
  setHidden("settings_pair_device_page", section !== "pair_device");
  setHidden("settings_find_devices_page", section !== "find_devices");
  setHidden("settings_fallback_audio_page", section !== "fallback_audio");
  setHidden("settings_record_announcement_page", section !== "record_announcement");
  document.getElementById("settings_layout").classList.toggle("settings-detail-active", Boolean(section));
  const parent = settingsParentSection(section);
  document.getElementById("settings_layout").classList.toggle("settings-subpage-active", Boolean(parent));
  setText("settings_back", parent ? `Back to ${SETTINGS_BACK_NAMES[parent]}` : "Back to settings");
  // The recording page has its own Cancel button instead.
  setHidden("settings_back", section === "record_announcement");
  setPageTitle(section ? `${SETTINGS_SECTION_TITLES[section]} - Settings` : "Settings");
  if (tabSection === "notifications") renderNotificationSettings();
  if (section === "accounts") loadAccounts().catch(error => setAccountResult(error.message, "error"));
  if (section === "create_account") setCreateAccountResult("");
  // Loaded on arriving, not each time Settings is redrawn, which would undo what's been typed.
  if (section === "advanced" && !advancedSettingsShowing) loadAdvancedSettings();
  advancedSettingsShowing = section === "advanced";
  showTailscaleSettings(section === "tailscale");
  if (section === "general" && !generalSettingsShowing) loadGeneralSettings();
  generalSettingsShowing = section === "general";
  syncRemotePairingPage(section);
  syncFallbackRecordingPage(section);
  stopAnnouncementPlaybackOffPage(currentViewName(), section);
}

function settingsPageHeading() {
  if (!settingsSection) return null;
  const panel = document.getElementById(`settings_panel_${settingsTabSection(settingsSection)}`);
  if (!panel) return null;
  return Array.from(panel.querySelectorAll("h3")).find(heading => heading.getClientRects().length) || null;
}

function focusSettingsElement(element) {
  if (!element) return;
  if (element.tagName === "H3" && !element.hasAttribute("tabindex")) element.setAttribute("tabindex", "-1");
  window.requestAnimationFrame(() => {
    try {
      element.focus({preventScroll: false});
    } catch (error) {
      element.focus();
    }
  });
}

function applySettingsRoute(route) {
  const wasShowing = currentViewName() === "settings";
  const previous = settingsSection;
  let section = normalizeSettingsSection(route.section);
  if (!section && !settingsSinglePane()) section = defaultSettingsSection();
  settingsSection = section;
  settingsOpenedFrom = section === route.section ? route.openedFrom : null;
  if (section !== route.section) {
    replaceCurrentRoute("settings", {section, openedFrom: settingsOpenedFrom});
  }
  // Arriving at Settings goes to its heading, like any other page. Moving around
  // within Settings goes to the page that opened, or back to the entry in the
  // list that was left.
  const focusPage = Boolean(section) && (wasShowing ? section !== previous : settingsSinglePane() && Boolean(route.section));
  showView("settings", {focus: !wasShowing && !focusPage});
  renderSettingsSection();
  if (focusPage) {
    focusSettingsElement(settingsPageHeading());
  } else if (wasShowing && !section && previous) {
    focusSettingsElement(document.getElementById(`settings_tab_${settingsTabSection(previous)}`));
  }
}

function selectSettingsTab(section) {
  // Moving through the tabs on wider screens shows each setting without adding
  // every one to the browser history.
  if (section !== settingsSection && (!confirmLeavingRemotePairing() || !confirmLeavingFallbackRecording())) {
    const current = document.getElementById(`settings_tab_${settingsTabSection(settingsSection)}`);
    if (current) current.focus();
    return;
  }
  settingsSection = section;
  settingsOpenedFrom = null;
  replaceCurrentRoute("settings", {section});
  renderSettingsSection();
}

function openSettingsSection(section) {
  if (settingsSinglePane()) {
    navigateTo("settings", {section, openedFrom: ""});
  } else {
    selectSettingsTab(section);
  }
}

function leaveSettingsSubpage() {
  const parent = settingsParentSection(settingsSection);
  if (settingsOpenedFrom === parent && history.state && history.state.view === "settings") {
    history.back();
    return;
  }
  navigateTo("settings", {section: parent}, true, true);
}

function settingsLayoutChanged() {
  if (currentViewName() !== "settings") return;
  const hadFocus = document.activeElement;
  if (!settingsSection && !settingsSinglePane()) {
    selectSettingsTab(defaultSettingsSection());
  } else {
    renderSettingsSection();
  }
  if (hadFocus && hadFocus !== document.body && !hadFocus.getClientRects().length) {
    focusSettingsElement(settingsPageHeading() || document.querySelector("#view_settings h2"));
  }
}

function outputFormHasUnsavedChanges() {
  return currentViewName() === "stream_output" && outputFormMode !== "edit_soundcard" && outputFormDirty;
}

function wizardHasUnsavedChanges() {
  return currentViewName() === "add_stream" && wizardDirty;
}

function hasUnsavedNavigationState() {
  return Boolean(wizardHasUnsavedChanges() || outputFormHasUnsavedChanges() || remotePairingPage || fallbackRecordingUnsaved());
}

function confirmDiscardNavigation() {
  if (remotePairingPage) return confirmLeavingRemotePairing();
  if (fallbackRecordingUnsaved()) return confirmLeavingFallbackRecording();
  if (!hasUnsavedNavigationState()) return true;
  return window.confirm("Discard the current setup changes?");
}

function closeNavMoreMenu(focusButton = false) {
  const button = document.getElementById("nav_more_button");
  const menu = document.getElementById("nav_more_menu");
  if (!button || !menu) return;
  menu.hidden = true;
  button.setAttribute("aria-expanded", "false");
  if (focusButton) button.focus();
}

function openNavMoreMenu(focusFirst = false) {
  const button = document.getElementById("nav_more_button");
  const menu = document.getElementById("nav_more_menu");
  if (!button || !menu) return;
  menu.hidden = false;
  button.setAttribute("aria-expanded", "true");
  if (focusFirst) {
    const first = Array.from(menu.querySelectorAll("[role='menuitem']")).find(item => !item.hidden);
    if (first) first.focus();
  }
}

function toggleNavMoreMenu(focusFirst = false) {
  const menu = document.getElementById("nav_more_menu");
  if (!menu) return;
  if (menu.hidden) {
    openNavMoreMenu(focusFirst);
  } else {
    closeNavMoreMenu();
  }
}

function navigateTo(view, params = {}, replace = false, force = false) {
  if (!replace && !force && !confirmDiscardNavigation()) return;
  const leavingOutputForm = currentViewName() === "stream_output" && view !== "stream_output";
  if (leavingOutputForm) closeOutputForm();
  if (accountIsReadOnly() && isReadOnlyRestrictedView(view)) {
    view = "dashboard";
    params = {};
    replace = true;
  }
  closeNavMoreMenu();
  const url = routeForView(view, params);
  const state = routeState(view, params);
  if (replace) {
    history.replaceState(state, "", url);
  } else {
    history.pushState(state, "", url);
  }
  applyRoute(state);
}

function currentRouteParams() {
  const view = currentViewName();
  if (view === "add_stream" && wizardMode === "add_output") return {streamId: settingsStreamId};
  if (view === "stream_settings") return {streamId: settingsStreamId, section: streamSettingsSection};
  if (view === "stream_output") return {streamId: settingsStreamId, outputId: editingOutputId};
  if (view === "eas_alerts") return {streamId: easAlertStreamId, page: easAlertPage};
  if (view === "eas_alert_export" || view === "eas_alert_delete") return {streamId: easAlertStreamId, page: easAlertPage};
  if (view === "eas_alert_detail") return {streamId: easAlertStreamId, alertId: easAlertDetailId, page: easAlertReturnPage};
  if (view === "iq_recording_download") return {recordingId: selectedIqRecordingId};
  if (view === "stream_archives") return {streamId: streamArchiveStreamId, mode: streamArchiveDownloadMode ? "download" : ""};
  if (view === "stream_archive_delete") return {streamId: streamArchiveStreamId};
  if (view === "settings") return {section: settingsSection, openedFrom: settingsOpenedFrom};
  return {};
}

function activeSdrLabel(settings) {
  if (!settings.serial) return "none";
  const select = document.getElementById("serial");
  const option = Array.from(select.options).find(item => item.value === settings.serial);
  return option ? option.textContent : `serial ${settings.serial}`;
}

function sourceIsIqFile(data) {
  return data && data.source && data.source.kind === "iq_file";
}

function rtlRequiresAttention(data) {
  if (!data || sourceIsIqFile(data)) return false;
  const settings = data.settings || {};
  if (data.capture_error) return true;
  if (settings.serial && !data.active) return true;
  return false;
}

// Such as "2 days, 7 hours, 5 minutes", leaving out what's zero, as the server says it.
function formatUptime(seconds) {
  let minutes = Math.floor(Number(seconds || 0) / 60);
  if (!(minutes >= 1)) return "less than a minute";
  const days = Math.floor(minutes / 1440);
  minutes -= days * 1440;
  const hours = Math.floor(minutes / 60);
  minutes -= hours * 60;
  return [[days, "day"], [hours, "hour"], [minutes, "minute"]]
    .filter(([count]) => count)
    .map(([count, unit]) => `${count} ${unit}${count === 1 ? "" : "s"}`)
    .join(", ");
}

function renderDashboardSdr(data) {
  const settings = data.settings || {};
  const source = data.source || {};
  const attention = rtlRequiresAttention(data);
  const attentionElement = document.getElementById("dashboard_sdr_attention");
  const detailsElement = document.getElementById("dashboard_sdr_details");
  // Shown whether or not the SDR needs attention, and only once it's set.
  const location = String(data.location || "");
  setText("summary_location", location);
  setHidden("dashboard_location", !location);
  if (attentionElement) attentionElement.hidden = !attention;
  if (detailsElement) detailsElement.hidden = attention;
  if (attention) return;

  if (sourceIsIqFile(data)) {
    setText("summary_source", "I/Q file");
    setText("summary_sdr", source.name || "unknown file");
    setText("summary_ppm", "not applicable");
    setText("summary_gain", "not applicable");
    setText("summary_bias_tee", "not applicable");
    setText("summary_alias_filter", `${settings.alias_filter_strength ?? 100}%`);
    setText("summary_capture", data.active ? "active" : "inactive");
    return;
  }

  if (source.kind === "remote") {
    setText("summary_source", `Shared by ${source.name || "another NWR Stream Manager"}`);
    setText("summary_sdr", source.device_name || "unknown");
  } else {
    setText("summary_source", "RTL-SDR");
    setText("summary_sdr", activeSdrLabel(settings));
  }
  setText("summary_ppm", `${settings.ppm_correction || 0} PPM`);
  setText("summary_gain", settings.gain === null ? "automatic" : `${settings.gain} dB`);
  setText("summary_bias_tee", settings.bias_tee ? "enabled" : "disabled");
  setText("summary_alias_filter", `${settings.alias_filter_strength ?? 100}%`);
  setText("summary_capture", data.active ? "active" : "inactive");
}

function renderStorageSummary(storage) {
  const filesystems = storage && Array.isArray(storage.filesystems) ? storage.filesystems : [];
  const primary = filesystems.length ? filesystems[0] : null;
  const status = storage && storage.status ? String(storage.status) : "unknown";
  storageIsCritical = status === "critical";
  reorderDashboardStorage(status);
  const storageText = primary
    ? `${formatStorageBytes(primary.used_bytes || 0)} of ${formatStorageBytes(primary.total_bytes || 0)} used (${Math.round(Number(primary.used_percent || 0))}%)`
    : "unknown";
  setText("summary_storage", storageText);
  const usage = storage && storage.usage ? storage.usage : {};
  setText("summary_iq_storage", usage.iq_recordings || "0 B");
  setText("summary_eas_storage", usage.eas_recordings || "0 B");
  setText("summary_archive_storage", usage.stream_archives || "0 B");
  const warning = document.getElementById("storage-warning");
  const message = storage && storage.status && storage.status !== "ok" ? storage.message || "Please free up disk space." : "";
  if (warning.textContent !== message) warning.textContent = message;
  const hidden = !message;
  if (warning.hidden !== hidden) warning.hidden = hidden;
}

function reorderDashboardStorage(status) {
  const dashboard = document.getElementById("view_dashboard");
  const storage = document.getElementById("dashboard_storage_section");
  const sdr = document.getElementById("dashboard_sdr_section");
  const recent = document.getElementById("dashboard_recent_alerts_section");
  if (!dashboard || !storage || !sdr || !recent) return;
  const elevated = status === "low" || status === "critical";
  if (elevated && dashboard.firstElementChild !== storage) {
    dashboard.insertBefore(storage, dashboard.firstElementChild);
  } else if (!elevated && storage.previousElementSibling !== recent) {
    dashboard.insertBefore(storage, recent.nextElementSibling);
  }
}

function formatStorageBytes(value) {
  let amount = Math.max(0, Number(value || 0));
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let index = 0;
  while (amount >= 1000 && index < units.length - 1) {
    amount /= 1000;
    index += 1;
  }
  if (index === 0) return `${Math.round(amount)} ${units[index]}`;
  return `${amount.toFixed(1)} ${units[index]}`;
}

function primaryStorageSummary(storage) {
  const filesystems = storage && Array.isArray(storage.filesystems) ? storage.filesystems : [];
  const primary = filesystems.length ? filesystems[0] : null;
  return primary && primary.summary ? primary.summary.replace(/^Storage: /, "") : "unknown";
}

function populateIqSampleRates() {
  const select = document.getElementById("iq_sample_rate");
  if (!select || select.options.length) return;
  for (const rate of IQ_RECORDER_SAMPLE_RATES) {
    const option = document.createElement("option");
    option.value = String(rate);
    option.textContent = `${rate} S/s`;
    if (rate === IQ_RECORDER_DEFAULT_SAMPLE_RATE) option.selected = true;
    select.appendChild(option);
  }
}

function defaultIqRecorderPreferences() {
  return {
    mode: "stream",
    stream_id: "",
    sample_rate: IQ_RECORDER_DEFAULT_SAMPLE_RATE,
    duration_minutes: IQ_RECORDER_DEFAULT_DURATION_MINUTES
  };
}

function loadIqRecorderPreferences() {
  const defaults = defaultIqRecorderPreferences();
  try {
    const parsed = JSON.parse(window.localStorage.getItem(IQ_RECORDER_PREFS_KEY) || "{}");
    if (!parsed || typeof parsed !== "object") return defaults;
    const sampleRate = Number(parsed.sample_rate);
    const durationMinutes = Number(parsed.duration_minutes);
    const mode = parsed.mode === "spectrum" ? "spectrum" : "stream";
    return {
      mode,
      stream_id: String(parsed.stream_id || ""),
      sample_rate: IQ_RECORDER_SAMPLE_RATES.includes(sampleRate) ? sampleRate : defaults.sample_rate,
      duration_minutes: Number.isFinite(durationMinutes)
        ? Math.max(0, Math.min(1440, Math.round(durationMinutes)))
        : defaults.duration_minutes
    };
  } catch (error) {
    return defaults;
  }
}

function rememberIqRecorderPreferences() {
  const prefs = {
    mode: currentIqMode(),
    stream_id: document.getElementById("iq_stream_select").value || "",
    sample_rate: Number(document.getElementById("iq_sample_rate").value) || IQ_RECORDER_DEFAULT_SAMPLE_RATE,
    duration_minutes: Math.max(0, Math.min(1440, Number(document.getElementById("iq_duration_minutes").value || 0) || 0))
  };
  try {
    window.localStorage.setItem(IQ_RECORDER_PREFS_KEY, JSON.stringify(prefs));
  } catch (error) {
    // localStorage may be unavailable; the form still works with in-page values.
  }
}

function restoreIqRecorderPreferences() {
  populateIqSampleRates();
  renderIqStreamOptions(true);
  const prefs = loadIqRecorderPreferences();
  const mode = prefs.mode === "spectrum" ? "spectrum" : "stream";
  setChecked("iq_mode_stream", mode === "stream");
  setChecked("iq_mode_spectrum", mode === "spectrum");
  const streamSelect = document.getElementById("iq_stream_select");
  if (streamSelect && prefs.stream_id && Array.from(streamSelect.options).some(option => option.value === prefs.stream_id)) {
    setValue("iq_stream_select", prefs.stream_id);
  }
  setValue("iq_sample_rate", String(prefs.sample_rate));
  setValue("iq_duration_minutes", String(prefs.duration_minutes));
  renderIqModeFields();
}

function activeIqStreamOptions() {
  return configuredStreams
    .filter(stream => streamIsEnabled(stream))
    .map(stream => {
      const station = stream.station || {};
      const callsign = station.callsign || "Unknown";
      const frequency = station.frequency ? `${station.frequency} MHz` : "";
      return {id: stream.id, label: `${callsign}${frequency ? ` ${frequency}` : ""}`};
    });
}

function renderIqStreamOptions(force = false) {
  const select = document.getElementById("iq_stream_select");
  if (!select) return;
  const options = activeIqStreamOptions();
  const signature = JSON.stringify(options);
  if (!force && signature === iqStreamOptionsSignature) return;
  const current = select.value;
  syncSelectOptions(select, options.map(item => ({value: item.id, label: item.label})));
  if (options.some(item => item.id === current)) {
    select.value = current;
  }
  iqStreamOptionsSignature = signature;
}

function currentIqMode() {
  const selected = document.querySelector("input[name='iq_recording_mode']:checked");
  return selected ? selected.value : "stream";
}

function renderIqModeFields() {
  const mode = currentIqMode();
  setHidden("iq_stream_fields", mode !== "stream");
  setHidden("iq_spectrum_fields", mode !== "spectrum");
  const start = document.getElementById("iq_start_recording");
  if (start) {
    start.disabled = mode === "stream" && !document.getElementById("iq_stream_select").value;
  }
}

function iqRecorderSourceText(recorder) {
  if (!recorder || recorder.status === "idle") return "None";
  if (recorder.mode === "stream") return recorder.stream_label || "Stream channel";
  return "Entire spectrum at 162.475 MHz";
}

function renderIqRecorder(recorder, storage) {
  recorder = recorder || {active: false, status: "idle"};
  if (accountIsReadOnly()) recorder = {active: false, status: "idle"};
  populateIqSampleRates();
  renderIqStreamOptions();
  const active = Boolean(recorder.active);
  setHidden("iq_recording_banner", !active);
  if (active) setText("iq_recording_banner_elapsed", formatDuration(recorder.elapsed_seconds));
  setHidden("iq_recorder_active", !active);
  setHidden("iq_recorder_idle", active);
  const openStart = document.getElementById("open_iq_start");
  if (openStart) setDisabled(openStart, active);
  if (!active) {
    renderIqModeFields();
    const signature = JSON.stringify({status: recorder.status, error: recorder.error || ""});
    if (signature !== iqRecorderSignature && recorder.status && recorder.status !== "idle") {
      const message = recorder.error || (recorder.status === "completed" ? "I/Q recording completed." : "I/Q recording stopped.");
      setIqRecorderResult(message, recorder.status === "needs-attention" ? "error" : "success");
    }
    iqRecorderSignature = signature;
    return;
  }
  setText("iq_status", recorder.status === "recording" ? "Recording" : recorder.status);
  setText("iq_source", iqRecorderSourceText(recorder));
  setText("iq_active_sample_rate", `${recorder.sample_rate || 0} S/s`);
  setText("iq_file_size", formatDecimalBytes(recorder.bytes_written));
  setText("iq_elapsed", formatDuration(recorder.elapsed_seconds));
  setText("iq_remaining", recorder.storage_remaining_seconds === null || recorder.storage_remaining_seconds === undefined ? "calculating" : formatDuration(recorder.storage_remaining_seconds));
  const hasAutoStop = Number(recorder.duration_seconds || 0) > 0;
  setHidden("iq_auto_stop_metric", !hasAutoStop);
  if (hasAutoStop) {
    const autoStopSeconds = Math.max(0, Number(recorder.duration_seconds || 0) - Number(recorder.elapsed_seconds || 0));
    setText("iq_auto_stop_remaining", formatDuration(autoStopSeconds));
  }
  setText("iq_storage", primaryStorageSummary(storage));
  iqRecorderSignature = JSON.stringify({status: recorder.status, file_name: recorder.file_name});
}

function iqRecordingTableSignature(recordings) {
  return JSON.stringify((recordings || []).map(recording => ({
    id: recording.id || "",
    recorded_at: recording.recorded_at || "",
    duration: recording.duration || "",
    sample_rate: recording.sample_rate || 0,
    frequency_hz: recording.frequency_hz || 0,
    downloading: Boolean(recording.downloading)
  })));
}

function renderIqRecordings(recordings) {
  iqRecordings = recordings || [];
  const tbody = document.getElementById("iq-recordings-body");
  if (!tbody) return;
  const signature = iqRecordingTableSignature(iqRecordings);
  if (signature === iqRecordingsSignature) return;
  iqRecordingsSignature = signature;
  if (!iqRecordings.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "hint";
    cell.textContent = "No I/Q recordings.";
    row.appendChild(cell);
    reconcileKeyedChildren(tbody, [keyedRow(labelResponsiveTableRow(row, tbody), "empty")]);
    return;
  }
  reconcileKeyedChildren(tbody, iqRecordings.map(recording => keyedRow(labelResponsiveTableRow(iqRecordingRow(recording), tbody), `recording:${recording.id || ""}`)));
}

function iqRecordingRow(recording) {
  const row = document.createElement("tr");
  row.appendChild(tableCell(recording.recorded_at || "Unknown"));
  row.appendChild(tableCell(recording.duration || "0:00"));
  row.appendChild(tableCell(`${recording.sample_rate || 0} S/s`));
  row.appendChild(tableCell(`${recording.frequency_hz || 0} Hz`));
  row.appendChild(iqRecordingActionsCell(recording));
  return row;
}

function iqRecordingActionsCell(recording) {
  const cell = document.createElement("td");
  cell.className = "menu-cell";
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "More actions";
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-label", `More actions for I/Q recording from ${recording.recorded_at || "unknown time"}`);
  button.dataset.iqRecordingMenu = recording.id || "";
  const menu = document.createElement("div");
  menu.className = "stream-actions-menu";
  menu.hidden = true;
  menu.setAttribute("role", "menu");
  const download = document.createElement("button");
  download.type = "button";
  download.textContent = "Download recording";
  download.setAttribute("role", "menuitem");
  download.dataset.action = "download-iq-recording";
  download.dataset.recordingId = recording.id || "";
  menu.appendChild(download);
  if (!recording.downloading && !accountIsReadOnly()) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove recording";
    remove.setAttribute("role", "menuitem");
    remove.dataset.action = "remove-iq-recording";
    remove.dataset.recordingId = recording.id || "";
    menu.appendChild(remove);
  }
  cell.appendChild(button);
  cell.appendChild(menu);
  return cell;
}

async function loadIqRecordings() {
  const data = await request("/api/iq-recordings");
  renderIqRecordings(data.recordings || []);
  return data;
}

async function startIqRecording() {
  if (accountIsReadOnly()) {
    setIqStartResult("This account is read-only.", "error");
    return;
  }
  const mode = currentIqMode();
  const rawMinutes = Number(document.getElementById("iq_duration_minutes").value || 0);
  const minutes = Math.max(0, Math.min(1440, Number.isFinite(rawMinutes) ? rawMinutes : IQ_RECORDER_DEFAULT_DURATION_MINUTES));
  setValue("iq_duration_minutes", String(minutes));
  rememberIqRecorderPreferences();
  const payload = {
    mode,
    duration_seconds: Math.round(minutes * 60)
  };
  if (mode === "stream") {
    payload.stream_id = document.getElementById("iq_stream_select").value;
  } else {
    payload.sample_rate = Number(document.getElementById("iq_sample_rate").value);
  }
  setIqStartResult("Starting I/Q recording...");
  const data = await request("/api/iq-recorder/start", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  renderIqRecorder(data.iq_recorder || {}, {});
  await loadIqRecordings();
  navigateTo("iq_recorder", {}, false, true);
  setIqRecorderResult("I/Q recording started.", "success");
}

async function stopIqRecording() {
  if (accountIsReadOnly()) {
    setIqRecorderResult("This account is read-only.", "error");
    return;
  }
  setIqRecorderResult("Stopping I/Q recording...");
  const data = await request("/api/iq-recorder/stop", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: "{}"
  });
  renderIqRecorder(data.iq_recorder || {}, {});
  await loadIqRecordings();
  setIqRecorderResult("I/Q recording stopped.", "success");
}

function selectedIqRecording() {
  return iqRecordings.find(recording => recording.id === selectedIqRecordingId) || null;
}

// Only the formats this recording fits in: WAV files can't be larger than 4 GB.
function renderIqDownloadFormats(formats) {
  const select = document.getElementById("iq_download_format");
  if (!select || !formats.length) return;
  const selected = select.value;
  select.replaceChildren(...formats.map(format => {
    const option = document.createElement("option");
    option.value = format.value;
    option.textContent = format.label;
    return option;
  }));
  select.value = formats.some(format => format.value === selected) ? selected : formats[0].value;
}

function renderIqDownloadPage() {
  const recording = selectedIqRecording();
  if (!recording) {
    setText("iq_download_recording_label", "I/Q recording was not found.");
    setPageTitle("Download I/Q Recording");
    return;
  }
  setText(
    "iq_download_recording_label",
    `${recording.recorded_at || "Unknown"}; ${recording.sample_rate || 0} S/s; ${recording.frequency_hz || 0} Hz`
  );
  renderIqDownloadFormats(recording.download_formats || []);
  setPageTitle(`Download I/Q recording from ${recording.recorded_at || "unknown time"}`);
}

async function removeIqRecording(recordingId) {
  if (accountIsReadOnly()) {
    setIqRecorderResult("This account is read-only.", "error");
    return;
  }
  const data = await request(`/api/iq-recording?id=${encodeURIComponent(recordingId)}`, {method: "DELETE"});
  renderIqRecordings(data.recordings || []);
  setIqRecorderResult("I/Q recording removed.", "success");
}

async function downloadSelectedIqRecording() {
  const recording = selectedIqRecording();
  if (!recording) {
    setIqDownloadResult("I/Q recording was not found.", "error");
    return;
  }
  const format = document.getElementById("iq_download_format").value || "cf";
  const link = document.createElement("a");
  link.href = `/api/iq-recording-download?id=${encodeURIComponent(recording.id)}&format=${encodeURIComponent(format)}`;
  link.download = "";
  document.body.appendChild(link);
  link.click();
  link.remove();
  navigateTo("iq_recorder", {}, false, true);
  setIqRecorderResult("Download started.", "success");
  setTimeout(() => loadIqRecordings().catch(() => {}), 500);
}

function updateDashboard(data) {
  renderDashboardSdr(data);
  renderStorageSummary(data.storage || {});
  activeStreamSnapshots = data.active_streams || [];
  streamSignals = data.stream_signals || {};
  renderActiveStreams(data.active_streams || [], configuredStreams);
  renderDashboardStreamAttention(data.active_streams || [], configuredStreams);
  renderDashboardRecentAlerts(data.recent_eas_alerts || []);
  renderIqRecorder(data.iq_recorder || {}, data.storage || {});
  if (settingsStreamId) renderStreamSettings();
}

let remoteSdr = {};
let remoteDiscoveredDevices = [];
let remoteSdrRequestPending = false;
let remotePairingWasActive = false;
let remotePairingDeviceIds = [];
// While "Pair a device" (this machine shows a code) or "Find devices" (this
// machine looks for one to pair with) is open. Pairing lasts only that long.
const REMOTE_PAIRING_KEEPALIVE_MS = 3000;
const REMOTE_DEVICE_SEARCH_PAUSE_MS = 1000;
let remotePairingPage = "";
let remotePairingKeepaliveTimer = null;
let remoteDeviceSearchGeneration = 0;
let remotePairDeviceChosen = false;

function setMessage(id, message, kind = "") {
  const element = document.getElementById(id);
  if (!element) return;
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function remoteDeviceName(device) {
  return (device && device.name) || "Unnamed NWR Stream Manager";
}

function formatClockTime(epochSeconds) {
  return new Date(Number(epochSeconds || 0) * 1000).toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
}

function remoteSdrUsesRemote() {
  const remote = remoteSdr.remote || {};
  return remote.source_mode === "remote" && Boolean(remote.device_id);
}

function renderRemoteSdr(data) {
  remoteSdr = data.remote_sdr || {};
  const devices = remoteSdr.devices || [];
  const sharing = remoteSdr.sharing || {};
  const remote = remoteSdr.remote || {};
  renderActiveSdrChoices();
  setText("remote_sdr_status", remoteSdrStatusText(remote, devices));
  const buffer = document.getElementById("remote_buffer_seconds");
  const savedBuffer = data.settings && data.settings.remote_buffer_seconds;
  if (savedBuffer !== undefined && !numberFieldIsBeingTyped(buffer) && !remoteSdrRequestPending) {
    setValue("remote_buffer_seconds", savedBuffer);
  }

  const available = Boolean(sharing.available);
  setHidden("remote_access_unavailable", available);
  setHidden("remote_access_controls", !available);
  if (!remoteSdrRequestPending) {
    setChecked("remote_access_enabled", Boolean(sharing.enabled));
  }
  setHidden("remote_pairing_controls", !sharing.running);
  renderRemotePairing(sharing, devices);
  // The sharing machine shows a pairing code; the machine that uses it enters
  // the code. Entering a code here while sharing would be backwards.
  const sharingThisSdr = Boolean(sharing.enabled && available);
  setHidden("remote_pair_section", sharingThisSdr);
  if (sharing.error) setMessage("remote-sharing-result", sharing.error, "error");
  renderPairedDevices(devices, remote);
}

function remoteSdrStatusText(remote, devices) {
  if (!remoteSdrUsesRemote()) return "";
  const status = remote.status || {};
  const device = devices.find(item => item.id === remote.device_id) || {};
  const name = status.host_name || remoteDeviceName(device);
  if (!status.reachable && (status.connecting || !status.host_id)) return `Connecting to ${name}...`;
  if (!status.reachable) return `${name} is not reachable. Check that it is running and sharing its RTL-SDR.`;
  const sdr = status.sdr || {};
  // A reset briefly takes the RTL-SDR off USB; the reset message says how it is going,
  // as it does for an RTL-SDR on this machine.
  const problem = sdr.reset && sdr.reset.state === "resetting" ? null : sdr.problem;
  if (problem === "disconnected") return `Connected to ${name}, but its RTL-SDR is not connected.`;
  if (problem) return `Connected to ${name}, but its RTL-SDR needs attention.`;
  return `Connected to ${name}. Using ${sdr.device_name || "its RTL-SDR"}.`;
}

function renderRemotePairing(sharing, devices) {
  const pairing = sharing.pairing || {};
  const active = Boolean(pairing.active && sharing.running);
  setValue("remote_pairing_code", active ? String(pairing.code || "") : "");
  setText(
    "remote_pairing_code_hint",
    active ? `Works for one device, until ${formatClockTime(pairing.expires_at)}.` : ""
  );
  if (!active) setMessage("remote-pairing-copy-result", "");
  if (remotePairingWasActive && !active) {
    const added = devices.filter(device => !remotePairingDeviceIds.includes(device.id));
    let message = "Pairing stopped.";
    let kind = "";
    if (pairing.ended_reason === "paired" && added.length) {
      message = `Paired with ${sentenceList(added.map(remoteDeviceName))}. Either device can now use the other's shared RTL-SDR.`;
      kind = "success";
    } else if (pairing.ended_reason === "expired") {
      message = "The pairing code expired. Select Pair a device to get a new one.";
    } else if (pairing.ended_reason === "too-many-attempts") {
      message = "Pairing stopped after too many incorrect pairing codes. Select Pair a device to start again.";
      kind = "error";
    } else if (pairing.ended_reason === "page-closed") {
      message = "Pairing stopped because the page showing the pairing code was closed or lost its connection.";
    }
    setMessage("remote-sharing-result", message, kind);
    if (remotePairingPage === "pair_device") {
      // Pairing is over, so this page has nothing left to show.
      endRemotePairingPage();
      leaveSettingsSubpage();
    }
  }
  if (!remotePairingWasActive && active) remotePairingDeviceIds = devices.map(device => device.id);
  remotePairingWasActive = active;
}

function syncRemotePairingPage(section) {
  const page = ["pair_device", "find_devices"].includes(section) ? section : "";
  if (page === remotePairingPage) return;
  endRemotePairingPage();
  remotePairingPage = page;
  if (page === "pair_device") startShowingPairingCode();
  if (page === "find_devices") searchForRemoteDevices();
}

function endRemotePairingPage() {
  const page = remotePairingPage;
  remotePairingPage = "";
  if (remotePairingKeepaliveTimer) clearInterval(remotePairingKeepaliveTimer);
  remotePairingKeepaliveTimer = null;
  remoteDeviceSearchGeneration += 1;
  if (page === "pair_device") {
    remoteSdrAction("/api/remote-sdr/pairing/cancel").catch(error => setMessage("remote-sharing-result", error.message, "error"));
  }
}

function confirmLeavingRemotePairing() {
  if (!remotePairingPage) return true;
  if (!window.confirm("Leave this page? Pairing will stop.")) return false;
  endRemotePairingPage();
  return true;
}

async function startShowingPairingCode() {
  setMessage("remote-sharing-result", "");
  setMessage("remote-pairing-status", "Getting a pairing code...");
  setMessage("remote-pairing-copy-result", "");
  try {
    await remoteSdrAction("/api/remote-sdr/pairing/start");
  } catch (error) {
    if (remotePairingPage === "pair_device") setMessage("remote-pairing-status", error.message, "error");
    return;
  }
  if (remotePairingPage !== "pair_device") {
    // Left the page while the code was on its way.
    remoteSdrAction("/api/remote-sdr/pairing/cancel").catch(() => {});
    return;
  }
  setMessage("remote-pairing-status", "");
  // Pairing ends soon after these stop, such as when the tab is closed.
  remotePairingKeepaliveTimer = setInterval(() => {
    remoteSdrAction("/api/remote-sdr/pairing/keepalive").catch(() => {});
  }, REMOTE_PAIRING_KEEPALIVE_MS);
}

async function searchForRemoteDevices() {
  const generation = ++remoteDeviceSearchGeneration;
  remoteDiscoveredDevices = [];
  remotePairDeviceChosen = false;
  setValue("remote_pair_code", "");
  setValue("remote_pair_address", "");
  setMessage("remote-pair-result", "");
  setMessage("remote-pair-outcome", "");
  renderRemotePairDeviceOptions();
  setText("remote_pair_search_status", "Searching for devices that are ready to pair...");
  while (generation === remoteDeviceSearchGeneration) {
    try {
      const data = await request("/api/remote-sdr/discover", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
      if (generation !== remoteDeviceSearchGeneration) return;
      remoteDiscoveredDevices = data.hosts || [];
      renderRemotePairDeviceOptions();
      const count = remoteDiscoveredDevices.length;
      setText(
        "remote_pair_search_status",
        count
          ? `Found ${pluralize(count, "device")} ready to pair. Still searching.`
          : "No devices are ready to pair yet. Still searching."
      );
    } catch (error) {
      if (generation !== remoteDeviceSearchGeneration) return;
      setText("remote_pair_search_status", `Searching failed: ${error.message} Trying again.`);
    }
    await new Promise(resolve => setTimeout(resolve, REMOTE_DEVICE_SEARCH_PAUSE_MS));
  }
}


async function copyRemotePairingCode() {
  const input = document.getElementById("remote_pairing_code");
  const button = document.getElementById("copy_remote_pairing_code");
  if (!input.value) return;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(input.value);
    } else if (!copyTextWithSelectionFallback(input)) {
      throw new Error("clipboard is unavailable");
    }
    setMessage("remote-pairing-copy-result", "Pairing code copied.", "success");
  } catch (error) {
    setMessage("remote-pairing-copy-result", "Copy failed. Select the pairing code and copy it manually.", "error");
  }
  if (document.activeElement !== button) button.focus();
}

function pairedDeviceStatus(device, remote) {
  const network = device.via === "tailscale" ? " over Tailscale" : "";
  if (device.in_use) {
    const status = remote.status || {};
    return status.reachable ? `This machine is using its RTL-SDR${network}` : "This machine is using its RTL-SDR (not reachable)";
  }
  if (device.using_this_sdr) return `Using this machine's RTL-SDR${network}`;
  return "Paired";
}

function renderPairedDevices(devices, remote) {
  const tbody = document.getElementById("paired-devices-body");
  if (!devices.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.className = "hint";
    cell.textContent = "No paired devices.";
    row.appendChild(cell);
    reconcileKeyedChildren(tbody, [keyedRow(labelResponsiveTableRow(row, tbody), "empty")]);
    return;
  }
  reconcileKeyedChildren(tbody, devices.map(device => {
    const row = document.createElement("tr");
    row.appendChild(tableCell(remoteDeviceName(device)));
    row.appendChild(tableCell(pairedDeviceStatus(device, remote)));
    const actions = document.createElement("td");
    const unpair = document.createElement("button");
    unpair.type = "button";
    unpair.textContent = "Unpair";
    unpair.setAttribute("aria-label", `Unpair ${remoteDeviceName(device)}`);
    unpair.dataset.action = "unpair-remote-device";
    unpair.dataset.deviceId = device.id;
    unpair.dataset.deviceName = remoteDeviceName(device);
    actions.appendChild(unpair);
    row.appendChild(actions);
    return keyedRow(labelResponsiveTableRow(row, tbody), `device:${device.id}`);
  }));
}

async function saveSdrSource(payload, successMessage) {
  remoteSdrRequestPending = true;
  setMessage("sdr-source-result", "Saving...");
  try {
    const data = await request("/api/settings", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    remoteSdrRequestPending = false;
    applyStatus(data, {syncControls: true});
    setMessage("sdr-source-result", successMessage, "success");
  } catch (error) {
    remoteSdrRequestPending = false;
    setMessage("sdr-source-result", error.message, "error");
    await refresh().catch(() => {});
  }
}

async function saveRemoteBufferSeconds() {
  const input = document.getElementById("remote_buffer_seconds");
  const seconds = Number(input.value);
  if (!Number.isFinite(seconds) || seconds < 0.1 || seconds > 10) {
    setMessage("sdr-source-result", "The network buffer must be between 0.1 and 10 seconds.", "error");
    return;
  }
  await saveSdrSource({remote_buffer_seconds: seconds}, `Network buffer set to ${seconds} seconds.`);
}

async function setRemoteAccessEnabled(enabled) {
  remoteSdrRequestPending = true;
  try {
    const data = await request("/api/settings", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({remote_access_enabled: enabled})
    });
    remoteSdrRequestPending = false;
    applyStatus(data, {syncControls: true});
    setMessage("remote-sharing-result", enabled ? "This RTL-SDR is now shared with paired devices." : "Sharing is turned off.", "success");
  } catch (error) {
    remoteSdrRequestPending = false;
    setChecked("remote_access_enabled", !enabled);
    setMessage("remote-sharing-result", error.message, "error");
  }
}

async function remoteSdrAction(path, payload = {}) {
  const data = await request(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  renderRemoteSdr({remote_sdr: data});
  return data;
}

function renderRemotePairDeviceOptions() {
  const select = document.getElementById("remote_pair_device");
  const options = remoteDiscoveredDevices.map(device => ({
    value: device.id,
    label: `${remoteDeviceName(device)} (${device.via === "tailscale" ? "Tailscale" : "local network"})${device.paired ? ", already paired" : ""}`
  }));
  options.push({value: "manual", label: "Enter an address"});
  const previous = select.value;
  syncSelectOptions(select, options);
  // Searching goes on while someone picks a device; keep their choice.
  if (remotePairDeviceChosen && options.some(option => option.value === previous)) {
    setValue("remote_pair_device", previous);
  } else {
    const firstUnpaired = remoteDiscoveredDevices.find(device => !device.paired);
    setValue("remote_pair_device", firstUnpaired ? firstUnpaired.id : options[0].value);
  }
  updateRemotePairAddressVisibility();
}

function updateRemotePairAddressVisibility() {
  const manual = document.getElementById("remote_pair_device").value === "manual";
  setHidden("remote_pair_address_label", !manual);
  setHidden("remote_pair_address_hint", !manual);
}

function remotePairTarget() {
  const choice = document.getElementById("remote_pair_device").value;
  if (choice && choice !== "manual") {
    const device = remoteDiscoveredDevices.find(item => item.id === choice);
    if (device) return {address: device.address, addresses: device.addresses || [], port: device.port, host_id: device.id};
  }
  const raw = document.getElementById("remote_pair_address").value.trim();
  const match = raw.match(/^\[?([^\]]+?)\]?(?::(\d+))?$/);
  if (!raw || !match) return null;
  return {address: match[1], port: match[2] ? Number(match[2]) : undefined};
}

async function pairRemoteDevice() {
  const target = remotePairTarget();
  if (!target) {
    setMessage("remote-pair-result", "Find a device or enter its address first.", "error");
    return;
  }
  const code = document.getElementById("remote_pair_code").value.replace(/\D/g, "");
  if (code.length !== 6) {
    setMessage("remote-pair-result", "Enter the 6-digit pairing code shown on the other device.", "error");
    document.getElementById("remote_pair_code").focus();
    return;
  }
  const button = document.getElementById("pair_remote_device");
  setDisabled(button, true);
  setMessage("remote-pair-result", "Pairing...");
  try {
    const before = (remoteSdr.devices || []).map(device => device.id);
    const data = await remoteSdrAction("/api/remote-sdr/pair", {...target, code});
    const added = (data.devices || []).find(device => !before.includes(device.id));
    setValue("remote_pair_code", "");
    setMessage("remote-pair-result", "");
    if (remotePairingPage === "find_devices") {
      endRemotePairingPage();
      leaveSettingsSubpage();
    }
    setMessage("remote-pair-outcome", `Paired with ${remoteDeviceName(added)}. Either device can now use the other's shared RTL-SDR.`, "success");
  } catch (error) {
    setMessage("remote-pair-result", error.message, "error");
  } finally {
    setDisabled(button, false);
  }
}

async function unpairRemoteDevice(deviceId, name) {
  if (!window.confirm(`Unpair ${name}? Neither device will be able to use the other's RTL-SDR until you pair them again.`)) return;
  try {
    await remoteSdrAction("/api/remote-sdr/unpair", {device_id: deviceId});
    setMessage("paired-devices-result", `Unpaired ${name}.`, "success");
  } catch (error) {
    setMessage("paired-devices-result", error.message, "error");
  }
}

function syncControls(data) {
  const s = data.settings;
  gainValues = data.gain_values || [];
  configuredLocalSerial = s.serial || "";
  renderActiveSdrChoices();
  const gain = document.getElementById("gain");
  setAttributeIfChanged(gain, "max", Math.max(0, gainValues.length - 1));
  if (!sdrSettingIsBeingChanged("gain")) {
    setChecked("gain_auto", s.gain === null);
    setDisabled(gain, s.gain === null || gainValues.length === 0);
    if (s.gain !== null) rememberManualGain(s.gain);
    const displayedGain = s.gain === null ? storedManualGain() : s.gain;
    const gainIndex = gainIndexFor(displayedGain);
    if (gain.value !== String(gainIndex)) gain.value = String(gainIndex);
  }
  updateGainSliderText();
  if (!sdrSettingIsBeingChanged("ppm_correction")) setValue("ppm_correction", s.ppm_correction);
  if (!sdrSettingIsBeingChanged("bias_tee")) setChecked("bias_tee", s.bias_tee);
  if (!sdrSettingIsBeingChanged("alias_filter_strength")) {
    setValue("alias_filter_strength", s.alias_filter_strength);
    setText("alias_filter_strength_label", `${s.alias_filter_strength}%`);
  }
  if (!sdrSettingIsBeingChanged("notify_sdr_failures")) setChecked("notify_sdr_failures", s.notify_sdr_failures);
  updateRtlNotificationControlsVisibility();
  lastControlSignature = controlSignature(data);
}

function applyStatus(data, options = {}) {
  // Settings changes are not saved while this fills the page from the server, so the
  // flag must always be cleared: if any update below fails, every later change would
  // otherwise be silently dropped.
  applying = true;
  try {
    if (data.account) applyAccountUi(data.account);
    if (data.instance_name) setInstanceName(data.instance_name);
    const nextSignature = controlSignature(data);
    const controlsFresh = !options.requestNumber || options.requestNumber > sdrControlsStaleThrough;
    if (
      options.syncControls ||
      lastControlSignature === "" ||
      (nextSignature !== lastControlSignature && controlsFresh)
    ) {
      syncControls(data);
    } else {
      gainValues = data.gain_values || gainValues;
    }
    setText("active", data.active ? "active" : "inactive");
    setText("chunks", data.received_chunks);
    setText("bytes", data.received_bytes);
    setText("last", data.last_batch_at ? `${data.last_batch_at.toFixed(3)}s` : "never");
    setText("capture-error", data.capture_error || "");
    renderSdrReset(data);
    if (data.remote_sdr && data.remote_sdr.identity) renderRemoteSdr(data);
    renderLogs(data.logs || []);
    renderIqTestSourceStatus(data);
    setFallbackControls(data.fallback);
    setNotificationSettings(data.notifications || {});
    setStreamTestModeStatus(data.test_mode || {});
    if (data.stream_archives) {
      streamArchivesStatus = data.stream_archives;
      if (settingsStreamId) setArchiveControls(currentSettingsStream());
    }
    document.getElementById("nav_stream_archives").hidden = !data.stream_archives_available;
    syncConfiguredStreamsFromStatus(data);
    updateDashboard(data);
    setText("general_uptime", formatUptime(data.uptime_seconds));
    const now = Date.now();
    if (now - lastEasAlertRefreshAt > 10000) {
      lastEasAlertRefreshAt = now;
      loadEasAlertStreams({preserve: true, quiet: true});
      if (currentViewName() === "eas_alert_detail") {
        updateEasAlertDetailNavigation().catch(error => console.debug("EAS alert detail navigation refresh failed", error));
      }
    }
    if (currentViewName().startsWith("iq_") && now - lastIqRecordingsRefreshAt > 5000) {
      lastIqRecordingsRefreshAt = now;
      loadIqRecordings().catch(() => {});
    }
  } finally {
    applying = false;
  }
}

function currentPayload() {
  const auto = document.getElementById("gain_auto").checked;
  const gainIndex = Number(document.getElementById("gain").value);
  return {
    serial: document.getElementById("serial").value,
    gain: auto || gainValues.length === 0 ? null : gainValues[gainIndex],
    ppm_correction: numericControlValue("ppm_correction"),
    bias_tee: document.getElementById("bias_tee").checked,
    alias_filter_strength: Number(document.getElementById("alias_filter_strength").value),
    notify_sdr_failures: document.getElementById("notify_sdr_failures").checked
  };
}

// Only the settings changed here are sent, so this page never puts back values
// someone else has since changed.
function scheduleUpdate(controlId) {
  if (applying) return;
  const key = SDR_SETTING_FOR_CONTROL[controlId];
  if (!key) return;
  pendingSdrSettingEdits.set(key, (pendingSdrSettingEdits.get(key) || 0) + 1);
  clearTimeout(timer);
  timer = setTimeout(saveSdrSettingEdits, 250);
}

function settleSdrSettingEdits(sent) {
  for (const [key, edits] of sent) {
    // A setting changed again while saving stays pending for the next save.
    if (pendingSdrSettingEdits.get(key) === edits) pendingSdrSettingEdits.delete(key);
  }
  sdrControlsStaleThrough = statusRequestCount;
}

async function saveSdrSettingEdits() {
  const sent = new Map(pendingSdrSettingEdits);
  if (!sent.size) return;
  const values = currentPayload();
  const payload = {};
  for (const key of sent.keys()) payload[key] = values[key];
  try {
    const data = await request("/api/settings", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    settleSdrSettingEdits(sent);
    setMessage("sdr-settings-result", "");
    applyStatus(data, {syncControls: true});
  } catch (error) {
    settleSdrSettingEdits(sent);
    setMessage("sdr-settings-result", error.message, "error");
    // Show what the SDR is really using again.
    lastControlSignature = "";
    await refresh().catch(() => {});
  }
}

function scheduleFallbackUpdate() {
  if (applying) return;
  clearTimeout(fallbackUpdateTimer);
  fallbackUpdateTimer = setTimeout(async () => {
    try {
      const data = await request("/api/fallback-settings", {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(fallbackPayload())
      });
      setFallbackResult("Fallback audio settings saved.", "success");
      applyStatus(data, {syncControls: false});
    } catch (error) {
      setFallbackResult(error.message, "error");
    }
  }, 250);
}

function scheduleEasUpdate() {
  if (applying || !settingsStreamId) return;
  clearTimeout(easUpdateTimer);
  easUpdateTimer = setTimeout(async () => {
    const stream = currentSettingsStream();
    const payload = easPayload();
    if (stream && easRecordingEnabled(stream) && !payload.enabled && !canDisableEasRecording(stream)) {
      setChecked("eas_enabled", true);
      setEasResult("At least one output must remain enabled for each stream.", "error");
      return;
    }
    try {
      const data = await request("/api/eas-recording", {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          stream_id: settingsStreamId,
          eas_recording: payload
        })
      });
      renderStreams(data.streams || []);
      if (payload.enabled && storageIsCritical) {
        setEasResult("Disabled due to insufficient disk space.", "error");
      } else {
        setEasResult("EAS recording settings saved.", "success");
      }
      loadEasAlertStreams({preserve: true, quiet: true});
    } catch (error) {
      setEasResult(error.message, "error");
    }
  }, 250);
}

function scheduleAudioEffectsUpdate() {
  if (applying || !settingsStreamId) return;
  clearTimeout(audioEffectsUpdateTimer);
  audioEffectsUpdateTimer = setTimeout(async () => {
    try {
      const data = await request("/api/audio-effects", {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          stream_id: settingsStreamId,
          audio: audioEffectsPayload()
        })
      });
      renderStreams(data.streams || []);
      setAudioEffectsResult("Audio effects saved.", "success");
    } catch (error) {
      setAudioEffectsResult(error.message, "error");
    }
  }, 250);
}

function scheduleStreamNotificationsUpdate() {
  if (applying || !settingsStreamId || !notificationIsConfigured() || accountIsReadOnly()) return;
  clearTimeout(streamNotificationsUpdateTimer);
  streamNotificationsUpdateTimer = setTimeout(async () => {
    try {
      const payload = streamNotificationsPayload();
      const data = await request("/api/stream-notifications", {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          stream_id: settingsStreamId,
          notifications: payload
        })
      });
      renderStreams(data.streams || []);
      setStreamNotificationsResult("Notification settings saved.", "success");
    } catch (error) {
      setStreamNotificationsResult(error.message, "error");
    }
  }, 250);
}

async function sendStreamTestMode(action, extra = {}) {
  const payload = Object.assign({
    action,
    stream_id: settingsStreamId,
    client_id: pageTestModeClientId()
  }, extra);
  const data = await request("/api/stream-test-mode", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  setStreamTestModeStatus(data.test_mode || {});
  if (Array.isArray(data.streams)) renderStreams(data.streams);
  return data;
}

function sameTestWarningAcknowledged() {
  try {
    return localStorage.getItem(SAME_TEST_ACK_STORAGE_KEY) === "true";
  } catch (error) {
    return false;
  }
}

function setSameTestWarningAcknowledged(value) {
  try {
    localStorage.setItem(SAME_TEST_ACK_STORAGE_KEY, value ? "true" : "false");
  } catch (error) {
    console.debug("failed to store SAME test warning acknowledgement", error);
  }
}

function updateSameTestWarningControls() {
  const checkbox = document.getElementById("same_test_warning_ack");
  const confirm = document.getElementById("confirm_same_test_warning");
  const acknowledged = Boolean(checkbox && checkbox.checked);
  if (confirm) confirm.hidden = !acknowledged;
  if (!acknowledged && document.activeElement === confirm && checkbox) checkbox.focus();
}

function sameTestWarningModalTargets() {
  return [
    document.querySelector("header"),
    document.querySelector("main"),
    document.getElementById("iq_recording_banner"),
    document.getElementById("same_alert_flash"),
    document.getElementById("same_alert_live")
  ].filter(Boolean);
}

function setSameTestWarningModalOpen(open) {
  for (const element of sameTestWarningModalTargets()) {
    if ("inert" in element) element.inert = open;
    if (open) element.setAttribute("aria-hidden", "true");
    else element.removeAttribute("aria-hidden");
  }
}

function sameTestWarningFocusableElements() {
  const dialog = document.getElementById("same_test_warning_dialog");
  if (!dialog || dialog.hidden) return [];
  return Array.from(dialog.querySelectorAll("a[href], button, input, select, textarea, [tabindex]:not([tabindex='-1'])"))
    .filter(element => !element.disabled && !element.hidden && element.getClientRects().length > 0);
}

function focusSameTestWarningInitialControl() {
  const checkbox = document.getElementById("same_test_warning_ack");
  const confirm = document.getElementById("confirm_same_test_warning");
  const cancel = document.getElementById("cancel_same_test_warning");
  const focusTarget = checkbox && checkbox.checked && confirm && !confirm.hidden ? confirm : checkbox || cancel;
  if (focusTarget) focusTarget.focus();
}

function trapSameTestWarningFocus(event) {
  if (event.key !== "Tab") return;
  const focusable = sameTestWarningFocusableElements();
  if (!focusable.length) {
    event.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function guardSameTestWarningFocus(event) {
  const dialog = document.getElementById("same_test_warning_dialog");
  if (!dialog || dialog.hidden || dialog.contains(event.target)) return;
  event.preventDefault();
  focusSameTestWarningInitialControl();
}

function openSameTestWarning() {
  if (!activeTestModeForCurrentStream()) return;
  const dialog = document.getElementById("same_test_warning_dialog");
  const checkbox = document.getElementById("same_test_warning_ack");
  if (!dialog || !checkbox) return;
  sameTestWarningPreviousFocus = document.activeElement;
  checkbox.checked = sameTestWarningAcknowledged();
  updateSameTestWarningControls();
  setSameTestWarningModalOpen(true);
  dialog.hidden = false;
  focusSameTestWarningInitialControl();
}

function closeSameTestWarning() {
  const dialog = document.getElementById("same_test_warning_dialog");
  if (dialog) dialog.hidden = true;
  setSameTestWarningModalOpen(false);
  const previousFocus = sameTestWarningPreviousFocus;
  sameTestWarningPreviousFocus = null;
  if (previousFocus && typeof previousFocus.focus === "function") {
    previousFocus.focus();
    return;
  }
  const button = document.getElementById("test_mode_same");
  if (button && !button.disabled) button.focus();
}

async function triggerSameTestFromWarning() {
  if (!activeTestModeForCurrentStream()) return;
  const checkbox = document.getElementById("same_test_warning_ack");
  if (!checkbox || !checkbox.checked) return;
  setSameTestWarningAcknowledged(true);
  closeSameTestWarning();
  setDisabled(document.getElementById("test_mode_same"), true);
  try {
    await sendStreamTestMode("same");
    setTestModeResult("SAME test is running.", "success");
  } catch (error) {
    setTestModeResult(error.message, "error");
  } finally {
    setTestModeControls();
  }
}

async function stopStreamTestMode(options = {}) {
  if (!activeTestModeBelongsToThisPage()) return;
  const payload = JSON.stringify({
    action: "stop",
    stream_id: streamTestMode.stream_id || settingsStreamId,
    client_id: pageTestModeClientId()
  });
  clearInterval(streamTestModeHeartbeatTimer);
  streamTestModeHeartbeatTimer = null;
  if (options.beacon && navigator.sendBeacon) {
    navigator.sendBeacon("/api/stream-test-mode", new Blob([payload], {type: "application/json"}));
    streamTestMode = Object.assign({}, streamTestMode, {stopping: true});
    streamTestModeSignature = "";
    return;
  }
  try {
    const data = await request("/api/stream-test-mode", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: payload
    });
    setStreamTestModeStatus(data.test_mode || {});
  } catch (error) {
    console.debug("failed to stop stream test mode", error);
  }
  streamTestModeSignature = "";
  setTestModeControls();
}

function updateStreamTestModeHeartbeat() {
  if (!activeTestModeBelongsToThisPage()) {
    clearInterval(streamTestModeHeartbeatTimer);
    streamTestModeHeartbeatTimer = null;
    return;
  }
  if (streamTestModeHeartbeatTimer) return;
  streamTestModeHeartbeatTimer = setInterval(async () => {
    if (!activeTestModeBelongsToThisPage()) {
      clearInterval(streamTestModeHeartbeatTimer);
      streamTestModeHeartbeatTimer = null;
      return;
    }
    try {
      await sendStreamTestMode("heartbeat", {stream_id: streamTestMode.stream_id || settingsStreamId});
    } catch (error) {
      // Test mode may have ended on the server (for example after 5 idle minutes).
      try {
        await refresh();
      } catch (refreshError) {
        console.debug("failed to refresh status after heartbeat error", refreshError);
      }
      if (activeTestModeBelongsToThisPage()) setTestModeResult(error.message, "error");
    }
  }, 5000);
}

function scheduleStreamTestModeSignalUpdate() {
  updateTestModeSignalText();
  if (!activeTestModeForCurrentStream()) return;
  clearTimeout(streamTestModeSignalTimer);
  streamTestModeSignalTimer = setTimeout(async () => {
    try {
      await sendStreamTestMode("signal", {signal_dbfs: testModeSignalValue()});
    } catch (error) {
      setTestModeResult(error.message, "error");
    }
  }, 150);
}

document.getElementById("serial").addEventListener("change", event => {
  chooseActiveSdr(event.target.value);
});
document.getElementById("remote_buffer_seconds").addEventListener("change", saveRemoteBufferSeconds);
document.getElementById("remote_buffer_seconds").addEventListener("keydown", event => {
  if (event.key === "Enter") {
    event.preventDefault();
    saveRemoteBufferSeconds();
  }
});
document.getElementById("remote_access_enabled").addEventListener("change", event => {
  setRemoteAccessEnabled(event.target.checked);
});
document.getElementById("start_remote_pairing").addEventListener("click", () => {
  navigateTo("settings", {section: "pair_device", openedFrom: "sdr_sharing"});
});
document.getElementById("cancel_remote_pairing").addEventListener("click", () => {
  endRemotePairingPage();
  leaveSettingsSubpage();
});
document.getElementById("copy_remote_pairing_code").addEventListener("click", copyRemotePairingCode);
document.getElementById("find_remote_devices").addEventListener("click", () => {
  navigateTo("settings", {section: "find_devices", openedFrom: "sdr_sharing"});
});
document.getElementById("cancel_find_devices").addEventListener("click", () => {
  endRemotePairingPage();
  leaveSettingsSubpage();
});
document.getElementById("remote_pair_device").addEventListener("change", () => {
  remotePairDeviceChosen = true;
  updateRemotePairAddressVisibility();
});
document.getElementById("pair_remote_device").addEventListener("click", pairRemoteDevice);
document.getElementById("remote_pair_code").addEventListener("keydown", event => {
  if (event.key === "Enter") {
    event.preventDefault();
    pairRemoteDevice();
  }
});
document.getElementById("paired-devices-body").addEventListener("click", event => {
  const button = event.target.closest("button[data-action='unpair-remote-device']");
  if (button) unpairRemoteDevice(button.dataset.deviceId, button.dataset.deviceName);
});
renderRemotePairDeviceOptions();

for (const id of controls) {
  if (id === "serial") continue;
  document.addEventListener("input", event => {
    if (event.target && event.target.id === id) {
      if (isNumericControl(event.target) && isTextEditingInputEvent(event)) {
        beginNumericTextEdit(event.target);
        return;
      }
      if (id === "gain") rememberManualGainFromSlider();
      if (id === "gain_auto" && !event.target.checked) restoreRememberedManualGain();
      if (id === "gain" || id === "gain_auto") updateGainSliderText();
      scheduleUpdate(id);
    }
  });
  document.addEventListener("change", event => {
    if (event.target && event.target.id === id) {
      if (isNumericControl(event.target)) commitNumericControlElement(event.target, null);
      if (id === "gain") rememberManualGainFromSlider();
      if (id === "gain_auto" && !event.target.checked) restoreRememberedManualGain();
      if (id === "gain" || id === "gain_auto") updateGainSliderText();
      scheduleUpdate(id);
    }
  });
}

for (const radio of document.querySelectorAll("input[name='stream_fallback_action']")) {
  radio.addEventListener("change", () => {
    updateStreamFallbackDelayVisibility();
    scheduleStreamFallbackActionUpdate();
  });
}
document.addEventListener("input", event => {
  if (!event.target || event.target.id !== "stream_fallback_delay") return;
  if (isNumericControl(event.target) && isTextEditingInputEvent(event)) {
    beginNumericTextEdit(event.target);
    return;
  }
  scheduleStreamFallbackActionUpdate();
});
document.addEventListener("change", event => {
  if (!event.target || event.target.id !== "stream_fallback_delay") return;
  commitNumericControlElement(event.target, null);
  scheduleStreamFallbackActionUpdate();
});

for (const radio of document.querySelectorAll("input[name='fallback_audio']")) {
  radio.addEventListener("change", () => {
    if (radio.checked) chooseFallbackAnnouncement(radio.value);
  });
}
document.getElementById("fallback_play").addEventListener("click", () => {
  const announcement = fallbackCustomSelected() ? "custom" : "default";
  toggleAnnouncement("fallback_play", `/api/fallback-audio?announcement=${announcement}&t=${Date.now()}`);
});
document.getElementById("fallback_recording_play").addEventListener("click", () => {
  if (fallbackRecording) toggleAnnouncement("fallback_recording_play", fallbackRecording.url);
});
document.getElementById("fallback_upload").addEventListener("click", chooseFallbackAnnouncementFile);
document.getElementById("fallback_audio_file").addEventListener("change", uploadFallbackAnnouncementFile);
document.getElementById("open_fallback_recording").addEventListener("click", () => {
  navigateTo("settings", {section: "record_announcement", openedFrom: "fallback_audio"});
});
document.getElementById("fallback_record").addEventListener("click", () => {
  if (fallbackRecorder) stopFallbackRecording();
  else startFallbackRecording();
});
document.getElementById("fallback_recording_save").addEventListener("click", saveFallbackRecording);
// Cancel is a deliberate choice to throw the recording away, so it doesn't ask.
document.getElementById("fallback_recording_cancel").addEventListener("click", () => {
  endFallbackRecordingPage();
  leaveSettingsSubpage();
});
renderFallbackRecordingAvailability();

for (const id of ["fallback_loop_delay"]) {
  document.addEventListener("input", event => {
    if (event.target && event.target.id === id) {
      if (isNumericControl(event.target) && isTextEditingInputEvent(event)) {
        beginNumericTextEdit(event.target);
        return;
      }
      scheduleFallbackUpdate();
    }
  });
  document.addEventListener("change", event => {
    if (event.target && event.target.id === id) {
      if (isNumericControl(event.target)) commitNumericControlElement(event.target, null);
      scheduleFallbackUpdate();
    }
  });
}

document.getElementById("start_notification_setup").addEventListener("click", startNotificationWizard);
document.getElementById("notification_cancel").addEventListener("click", cancelNotificationWizard);
document.getElementById("notification_back").addEventListener("click", notificationWizardBack);
document.getElementById("notification_next").addEventListener("click", async () => {
  try {
    await notificationWizardNext();
  } catch (error) {
    setNotificationResult(error.message, "error");
  }
});
document.getElementById("notification_finish").addEventListener("click", finishNotificationWizard);
document.getElementById("dismiss_notification_setup").addEventListener("click", dismissNotificationSetupDialog);
for (const link of document.querySelectorAll("#notification_setup_dialog a")) {
  link.addEventListener("click", () => {
    document.getElementById("notification_setup_dialog").hidden = true;
  });
}
document.getElementById("notification_setup_dialog").addEventListener("keydown", event => {
  if (event.key === "Escape") {
    event.preventDefault();
    dismissNotificationSetupDialog();
  }
});
document.getElementById("regenerate_notification_topic").addEventListener("click", async () => {
  try {
    await regenerateNotificationTopic();
  } catch (error) {
    setNotificationResult(error.message, "error");
  }
});
document.getElementById("copy_configured_notification_topic").addEventListener("click", async () => {
  await copyNotificationTopic("notification_configured_topic_secret");
});
for (const id of ["notification_server_mode", "notification_configured_server_url"]) {
  document.getElementById(id).addEventListener("change", async () => {
    try {
      await saveNotificationServerSettings();
    } catch (error) {
      setNotificationResult(error.message, "error");
    }
  });
}
document.getElementById("notification_access_url_mode").addEventListener("change", async () => {
  updateNotificationCustomUrlVisibility();
  try {
    await saveNotificationAccessUrlSettings();
  } catch (error) {
    setNotificationResult(error.message, "error");
  }
});
document.getElementById("notification_custom_access_url").addEventListener("change", async () => {
  try {
    await saveNotificationAccessUrlSettings();
  } catch (error) {
    setNotificationResult(error.message, "error");
  }
});
document.getElementById("notification_wizard_access_url_mode").addEventListener("change", updateNotificationWizardCustomUrlVisibility);
document.getElementById("configured_test_notifications").addEventListener("click", async event => {
  const button = event.currentTarget;
  setDisabled(button, true);
  try {
    await testNotificationSettings(notificationSettingsPayload());
  } catch (error) {
    setNotificationResult(error.message, "error");
  } finally {
    setDisabled(button, false);
  }
});
document.getElementById("copy_notification_topic").addEventListener("click", async () => {
  await copyNotificationTopic("notification_topic_secret");
});
document.getElementById("wizard_test_notifications").addEventListener("click", async event => {
  const button = event.currentTarget;
  setDisabled(button, true);
  try {
    await testNotificationSettings(notificationPayload());
  } catch (error) {
    setNotificationResult(error.message, "error");
  } finally {
    setDisabled(button, false);
  }
});

for (const id of ["eas_enabled", "eas_pre_seconds", "eas_post_seconds", "eas_max_seconds"]) {
  document.addEventListener("input", event => {
    if (event.target && event.target.id === id) {
      if (isNumericControl(event.target) && isTextEditingInputEvent(event)) {
        beginNumericTextEdit(event.target);
        return;
      }
      scheduleEasUpdate();
    }
  });
  document.addEventListener("change", event => {
    if (event.target && event.target.id === id) {
      if (isNumericControl(event.target)) commitNumericControlElement(event.target, null);
      scheduleEasUpdate();
    }
  });
}

for (const formatControl of document.querySelectorAll("input[name='eas_format']")) {
  formatControl.addEventListener("change", scheduleEasUpdate);
}

for (const id of [
  "audio_volume_enabled",
  "audio_volume_multiplier",
  "audio_comfort_noise_enabled",
  "audio_comfort_noise_level",
  "audio_highpass_enabled",
  "audio_highpass_frequency",
  "audio_highpass_sharpness",
  "audio_lowpass_enabled",
  "audio_lowpass_frequency",
  "audio_lowpass_sharpness",
  "audio_notch_enabled",
  "audio_notch_frequency",
  "audio_notch_width"
]) {
  document.addEventListener("input", event => {
    if (event.target && event.target.id === id) {
      if (isNumericControl(event.target)) {
        if (isTextEditingInputEvent(event) || event.target.dataset.userEditing === "1") {
          beginNumericTextEdit(event.target);
          return;
        }
        if (audioFrequencyNameForElement(event.target)) {
          commitAudioFrequencyElement(event.target, false);
        } else {
          normalizeNumericControl(event.target);
        }
      } else if (audioFrequencyNameForElement(event.target)) {
        if (isTextEditingInputEvent(event) || event.target.dataset.userEditing === "1") {
          beginNumericTextEdit(event.target);
          return;
        }
        commitAudioFrequencyElement(event.target, false);
      }
      scheduleAudioEffectsUpdate();
    }
  });
  document.addEventListener("change", event => {
    if (event.target && event.target.id === id) {
      if (isNumericControl(event.target)) {
        commitNumericControlElement(event.target, null);
      } else {
        commitAudioFrequencyElement(event.target, false);
      }
      scheduleAudioEffectsUpdate();
    }
  });
}

document.addEventListener("keydown", event => {
  if (!isNumericControl(event.target)) return;
  if (isTextEditingKey(event)) {
    beginNumericTextEdit(event.target);
    return;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    commitNumericControlElement(event.target, numericSaveCallbackForElement(event.target));
    return;
  }
  if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"].includes(event.key)) {
    setTimeout(() => commitNumericControlElement(event.target, numericSaveCallbackForElement(event.target)), 0);
  }
});

document.addEventListener("blur", event => {
  if (!isNumericControl(event.target)) return;
  commitNumericControlElement(event.target, numericSaveCallbackForElement(event.target));
}, true);

document.addEventListener("click", event => {
  const link = event.target && event.target.closest("a[data-view]");
  if (!link) return;
  if (
    event.defaultPrevented ||
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey
  ) {
    return;
  }
  event.preventDefault();
  const currentView = currentViewName();
  let targetView = link.dataset.view;
  let targetParams = {
    streamId: link.dataset.streamId || "",
    outputId: link.dataset.outputId || "",
    alertId: link.dataset.alertId || "",
    recordingId: link.dataset.recordingId || "",
    page: Number(link.dataset.page || 1)
  };
  if (targetView === "eas_alerts" && link.dataset.easEntry === "restore") {
    const restored = restoredEasRoute();
    targetView = restored.view;
    targetParams = {streamId: restored.streamId, alertId: restored.alertId, page: restored.page};
  }
  if (isEasAlertsView(targetView) && link.dataset.easEntry === "list") {
    pauseCurrentEasAlert();
    targetView = "eas_alerts";
    targetParams.alertId = "";
  } else if (targetView === "eas_alert_detail" && link.dataset.easEntry === "detail" && !isEasAlertsView(currentView)) {
    pauseCurrentEasAlert();
  }
  const replace = isEasAlertsView(currentView) && isEasAlertsView(targetView);
  navigateTo(targetView, targetParams, replace);
});

document.getElementById("nav_restart").addEventListener("click", event => {
  event.preventDefault();
  requestRestart();
});
for (const id of Object.values(GENERAL_SETTING_INPUTS)) {
  document.getElementById(id).addEventListener("input", () => {
    syncGeneralSaveButton();
    setMessage("general-result", "");
  });
  document.getElementById(id).addEventListener("keydown", event => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    saveGeneralSettings();
  });
}
document.getElementById("save_general_settings").addEventListener("click", saveGeneralSettings);
document.getElementById("save_advanced_settings").addEventListener("click", saveAdvancedSettings);
document.getElementById("tailscale_connect").addEventListener("click", () => tailscaleAction("connect"));
document.getElementById("tailscale_sign_out").addEventListener("click", () => tailscaleAction("sign_out"));
document.getElementById("tailscale_https").addEventListener("change", saveTailscaleHttps);
document.getElementById("tailscale_funnel").addEventListener("change", saveTailscaleFunnel);
document.getElementById("tailscale_https_form").addEventListener("submit", event => event.preventDefault());
// Enter in the port saves too, as it would without this script.
document.getElementById("advanced_port").addEventListener("keydown", event => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  saveAdvancedSettings();
});
// Buttons that submit a form only for browsers that don't run this script; here
// they act on the page instead, and their forms aren't sent.
for (const button of document.querySelectorAll("[data-baseline-submit]")) {
  button.type = "button";
  if (button.form) button.form.addEventListener("submit", event => event.preventDefault());
}
// The page comes with the More links listed in the bar, for browsers that don't
// run this script; here they become a menu.
(() => {
  const menu = document.getElementById("nav_more_menu");
  menu.setAttribute("role", "menu");
  for (const item of menu.querySelectorAll("a")) item.setAttribute("role", "menuitem");
  menu.hidden = true;
  document.getElementById("nav_more_button").hidden = false;
})();
document.getElementById("nav_more_button").addEventListener("click", () => {
  toggleNavMoreMenu();
});

document.getElementById("nav_more_button").addEventListener("keydown", event => {
  if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    openNavMoreMenu(true);
  }
  if (event.key === "Escape") {
    closeNavMoreMenu(true);
  }
});

document.getElementById("nav_more_menu").addEventListener("keydown", event => {
  if (event.key === "Escape") {
    event.preventDefault();
    closeNavMoreMenu(true);
    return;
  }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    // Only the items this account has; a hidden one can't take focus, and would stop the arrows there.
    const items = Array.from(document.querySelectorAll("#nav_more_menu [role='menuitem']")).filter(item => !item.hidden);
    const current = items.indexOf(document.activeElement);
    const step = event.key === "ArrowDown" ? 1 : -1;
    const next = items[(current + step + items.length) % items.length];
    if (next) next.focus();
  }
});

document.getElementById("dismiss_account_secret").addEventListener("click", dismissAccountSecret);
document.getElementById("copy_account_secret").addEventListener("click", copyAccountSecret);
document.getElementById("open_create_account").addEventListener("click", () => {
  setAccountResult("");
  setCreateAccountResult("");
  showAccountSecret("");
  setValue("new_account_username", "");
  setChecked("new_account_read_only", false);
  navigateTo("settings", {section: "create_account", openedFrom: "accounts"});
});
document.getElementById("cancel_create_account").addEventListener("click", () => {
  leaveSettingsSubpage();
});
document.getElementById("create_account").addEventListener("click", async () => {
  try {
    await createAccountFromForm();
  } catch (error) {
    setCreateAccountResult(error.message, "error");
  }
});
document.getElementById("change_account_password").addEventListener("click", async () => {
  try {
    await changeOwnPasswordFromForm();
  } catch (error) {
    setChangePasswordResult(error.message, "error");
  }
});
document.getElementById("cancel_change_password").addEventListener("click", () => {
  for (const id of ["account_current_password", "account_new_password", "account_confirm_password"]) setValue(id, "");
  setChangePasswordResult("");
  if (currentViewName() === "change_password") {
    navigateTo("dashboard", {}, true, true);
  } else if (accountIsOwner() || settingsSinglePane()) {
    leaveSettingsSubpage();
  }
});
document.getElementById("accounts-body").addEventListener("click", async event => {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.accountMenu !== undefined) {
    const menu = target.nextElementSibling;
    if (!menu) return;
    if (menu.hidden) {
      openStreamActionMenu(target, "first");
    } else {
      closeStreamActionMenu(menu, false);
    }
    return;
  }
  const accountId = target.dataset.accountId || "";
  try {
    if (target.dataset.action === "change-own-password") {
      closeStreamActionMenus();
      navigateTo("settings", {section: "account", openedFrom: "accounts"});
      return;
    }
    if (target.dataset.action === "reset-account-password") {
      closeStreamActionMenus();
      if (!window.confirm("Reset this account password?")) return;
      await resetAccountPassword(accountId);
      return;
    }
    if (target.dataset.action === "toggle-account-read-only") {
      closeStreamActionMenus();
      await setAccountReadOnly(accountId, target.dataset.readOnly === "1");
      return;
    }
    if (target.dataset.action === "delete-account") {
      closeStreamActionMenus();
      if (!window.confirm("Delete this account?")) return;
      await deleteAccount(accountId);
    }
  } catch (error) {
    setAccountResult(error.message, "error");
  }
});
document.getElementById("accounts-body").addEventListener("keydown", event => {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.accountMenu !== undefined) {
    if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
      event.preventDefault();
      openStreamActionMenu(target, "first");
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      openStreamActionMenu(target, "last");
      return;
    }
  }
  if (["menuitem", "menuitemcheckbox"].includes(target.getAttribute("role"))) {
    const menu = target.closest(".stream-actions-menu");
    if (!menu) return;
    const items = menuItems(menu);
    const index = items.indexOf(target);
    if (event.key === "ArrowDown") {
      event.preventDefault();
      items[(index + 1) % items.length].focus();
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      items[(index - 1 + items.length) % items.length].focus();
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      items[0].focus();
      return;
    }
    if (event.key === "End") {
      event.preventDefault();
      items[items.length - 1].focus();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeStreamActionMenu(menu, true);
    }
  }
});

document.getElementById("dismiss_nwrorg_submission").addEventListener("click", dismissNwrOrgSubmissionDialog);
document.getElementById("dismiss_insecure_audio_banner").addEventListener("click", () => {
  setHidden("insecure_audio_banner", true);
});
document.getElementById("server_unavailable_reload").addEventListener("click", () => window.location.reload());
document.getElementById("resume_listening").addEventListener("click", resumeListening);
document.getElementById("dismiss_resume_listening").addEventListener("click", () => {
  document.getElementById("resume_listening_dialog").hidden = true;
});
document.getElementById("nwrorg_submission_link").addEventListener("click", dismissNwrOrgSubmissionDialog);
document.getElementById("dismiss_monitor_unstable").addEventListener("click", dismissMonitorUnstableDialog);
document.getElementById("dismiss_receiver_unstable").addEventListener("click", dismissReceiverUnstableDialog);

document.getElementById("receiver_previous").addEventListener("click", receiverPreviousChannel);
document.getElementById("receiver_next").addEventListener("click", receiverNextChannel);
document.getElementById("receiver_volume").addEventListener("input", event => {
  receiverPlaybackVolume = Number(event.target.value);
  applyReceiverPlaybackVolume();
  applyLiveAudioVolume();
});
document.getElementById("receiver_play_pause").addEventListener("click", async () => {
  try {
    if (receiverPeerConnection && !receiverPaused) {
      await stopWeatherReceiver({preserveMediaSession: true});
      setReceiverResult("Receiver paused.", "success");
    } else {
      setReceiverResult("Starting receiver...");
      await startWeatherReceiver();
    }
  } catch (error) {
    setReceiverResult(error.message, "error");
    renderReceiverControls();
  }
});

for (const radio of document.querySelectorAll("input[name='iq_recording_mode']")) {
  radio.addEventListener("change", () => {
    renderIqModeFields();
    rememberIqRecorderPreferences();
  });
}
document.getElementById("iq_stream_select").addEventListener("change", () => {
  renderIqModeFields();
  rememberIqRecorderPreferences();
});
document.getElementById("iq_sample_rate").addEventListener("change", rememberIqRecorderPreferences);
document.getElementById("iq_duration_minutes").addEventListener("change", rememberIqRecorderPreferences);
document.getElementById("iq_start_recording").addEventListener("click", async () => {
  try {
    await startIqRecording();
  } catch (error) {
    setIqStartResult(error.message, "error");
  }
});
document.getElementById("iq_stop_recording").addEventListener("click", async () => {
  try {
    await stopIqRecording();
  } catch (error) {
    setIqRecorderResult(error.message, "error");
  }
});
document.getElementById("open_iq_start").addEventListener("click", () => {
  setIqStartResult("");
  navigateTo("iq_recorder_start");
});
document.getElementById("cancel_iq_start").addEventListener("click", () => {
  navigateTo("iq_recorder", {}, true, true);
});
document.getElementById("cancel_iq_download").addEventListener("click", () => {
  navigateTo("iq_recorder", {}, true, true);
});
document.getElementById("iq_download_recording").addEventListener("click", async () => {
  try {
    await downloadSelectedIqRecording();
  } catch (error) {
    setIqDownloadResult(error.message, "error");
  }
});
document.getElementById("iq-recordings-body").addEventListener("click", async event => {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.iqRecordingMenu !== undefined) {
    const menu = target.nextElementSibling;
    const shouldOpen = menu.hidden;
    if (shouldOpen) {
      openStreamActionMenu(target, null);
    } else {
      closeStreamActionMenu(menu, false);
    }
    return;
  }
  if (target.dataset.action === "download-iq-recording") {
    closeStreamActionMenus();
    selectedIqRecordingId = target.dataset.recordingId;
    navigateTo("iq_recording_download", {recordingId: selectedIqRecordingId});
    return;
  }
  if (target.dataset.action === "remove-iq-recording") {
    closeStreamActionMenus();
    if (!window.confirm("Remove this I/Q recording?")) return;
    try {
      await removeIqRecording(target.dataset.recordingId);
    } catch (error) {
      setIqRecorderResult(error.message, "error");
    }
  }
});
document.getElementById("iq-recordings-body").addEventListener("keydown", event => {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.iqRecordingMenu !== undefined) {
    if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
      event.preventDefault();
      openStreamActionMenu(target, "first");
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      openStreamActionMenu(target, "last");
      return;
    }
  }
  if (target.getAttribute("role") === "menuitem") {
    const menu = target.closest(".stream-actions-menu");
    if (!menu) return;
    const items = menuItems(menu);
    const index = items.indexOf(target);
    if (event.key === "ArrowDown") {
      event.preventDefault();
      items[(index + 1) % items.length].focus();
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      items[(index - 1 + items.length) % items.length].focus();
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      items[0].focus();
      return;
    }
    if (event.key === "End") {
      event.preventDefault();
      items[items.length - 1].focus();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeStreamActionMenu(menu, true);
    }
  }
});

document.getElementById("stream_settings_list").addEventListener("click", event => {
  const tab = event.target && event.target.closest ? event.target.closest("[data-stream-section]") : null;
  if (!tab) return;
  openStreamSettingsSection(tab.dataset.streamSection);
});

document.getElementById("stream_settings_list").addEventListener("keydown", event => {
  const tabs = availableStreamSettingsTabs();
  const index = tabs.indexOf(event.target);
  if (index < 0) return;
  let nextIndex = index;
  if (event.key === "ArrowDown" || event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
  else if (event.key === "ArrowUp" || event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
  else if (event.key === "Home") nextIndex = 0;
  else if (event.key === "End") nextIndex = tabs.length - 1;
  else return;
  event.preventDefault();
  tabs[nextIndex].focus();
  if (!settingsSinglePane()) selectStreamSettingsTab(tabs[nextIndex].dataset.streamSection);
});

document.getElementById("stream_settings_back").addEventListener("click", leaveStreamSettingsSection);
window.matchMedia(SETTINGS_SINGLE_PANE_QUERY).addEventListener("change", streamSettingsLayoutChanged);

for (const id of ["stream_notify_icecast_failures", "stream_notify_soundcard_failures", "stream_notify_eas_alerts", "stream_notify_bad_reception"]) {
  document.getElementById(id).addEventListener("change", scheduleStreamNotificationsUpdate);
}

document.getElementById("test_mode_enabled").addEventListener("change", async event => {
  if (!settingsStreamId) return;
  try {
    if (event.target.checked) {
      await sendStreamTestMode("start", {signal_dbfs: testModeSignalValue()});
    } else {
      await stopStreamTestMode();
    }
  } catch (error) {
    setChecked("test_mode_enabled", activeTestModeForCurrentStream());
    setTestModeResult(error.message, "error");
  }
});

document.getElementById("test_mode_signal").addEventListener("input", scheduleStreamTestModeSignalUpdate);
setInterval(tickTestModeTime, 1000);

document.getElementById("test_mode_same").addEventListener("click", openSameTestWarning);
document.getElementById("cancel_same_test_warning").addEventListener("click", closeSameTestWarning);
document.getElementById("same_test_warning_ack").addEventListener("change", event => {
  if (event.target.checked) setSameTestWarningAcknowledged(true);
  updateSameTestWarningControls();
});
document.getElementById("confirm_same_test_warning").addEventListener("click", triggerSameTestFromWarning);
document.getElementById("same_test_warning_dialog").addEventListener("keydown", event => {
  trapSameTestWarningFocus(event);
  if (event.key === "Escape") {
    event.preventDefault();
    closeSameTestWarning();
  }
});
document.addEventListener("focusin", guardSameTestWarningFocus);

document.getElementById("test_tones_list").addEventListener("click", event => {
  const button = event.target && event.target.closest ? event.target.closest("[data-test-tone]") : null;
  if (!button) return;
  selectTestTone(button.dataset.testTone, true);
});

document.getElementById("test_tones_list").addEventListener("keydown", event => {
  const buttons = Array.from(document.querySelectorAll("[data-test-tone]"));
  const index = buttons.indexOf(event.target);
  if (index < 0) return;
  let nextIndex = index;
  if (event.key === "ArrowDown" || event.key === "ArrowRight") nextIndex = (index + 1) % buttons.length;
  else if (event.key === "ArrowUp" || event.key === "ArrowLeft") nextIndex = (index - 1 + buttons.length) % buttons.length;
  else if (event.key === "Home") nextIndex = 0;
  else if (event.key === "End") nextIndex = buttons.length - 1;
  else return;
  event.preventDefault();
  buttons[nextIndex].focus();
  selectTestTone(buttons[nextIndex].dataset.testTone, false);
});

document.getElementById("test_tones_back").addEventListener("click", () => {
  document.getElementById("test_tones_layout").classList.remove("effect-detail-active");
  const selected = document.querySelector(`[data-test-tone='${selectedTestTone}']`);
  if (selected) selected.focus();
});

document.getElementById("test_tones_muted").addEventListener("change", scheduleTestTonesUpdate);

for (const name of TEST_TONE_NAMES) {
  document.getElementById(`test_tone_${name}_enabled`).addEventListener("change", scheduleTestTonesUpdate);
  for (const field of ["frequency", "amplitude"]) {
    const element = document.getElementById(`test_tone_${name}_${field}`);
    element.addEventListener("input", event => {
      if (isTextEditingInputEvent(event) || element.dataset.userEditing === "1") {
        beginNumericTextEdit(element);
        return;
      }
      normalizeNumericControl(element);
      scheduleTestTonesUpdate();
    });
    element.addEventListener("change", () => {
      commitNumericControlElement(element, null);
      scheduleTestTonesUpdate();
    });
  }
}

document.getElementById("settings_list").addEventListener("click", event => {
  const tab = event.target && event.target.closest ? event.target.closest("[data-settings-section]") : null;
  if (!tab) return;
  openSettingsSection(tab.dataset.settingsSection);
});

document.getElementById("settings_list").addEventListener("keydown", event => {
  const tabs = availableSettingsTabs();
  const index = tabs.indexOf(event.target);
  if (index < 0) return;
  let nextIndex = index;
  if (event.key === "ArrowDown" || event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
  else if (event.key === "ArrowUp" || event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
  else if (event.key === "Home") nextIndex = 0;
  else if (event.key === "End") nextIndex = tabs.length - 1;
  else return;
  event.preventDefault();
  tabs[nextIndex].focus();
  if (!settingsSinglePane()) selectSettingsTab(tabs[nextIndex].dataset.settingsSection);
});

document.getElementById("settings_back").addEventListener("click", () => {
  if (!confirmLeavingFallbackRecording()) return;
  endRemotePairingPage();
  leaveSettingsSubpage();
});

window.matchMedia(SETTINGS_SINGLE_PANE_QUERY).addEventListener("change", settingsLayoutChanged);

window.addEventListener("popstate", event => {
  const nextRoute = event.state || routeFromLocation();
  if (!confirmDiscardNavigation()) {
    const currentView = currentViewName();
    const currentParams = currentRouteParams();
    history.pushState(routeState(currentView, currentParams), "", routeForView(currentView, currentParams));
    return;
  }
  if (currentViewName() === "stream_output" && nextRoute.view !== "stream_output") closeOutputForm();
  applyRoute(nextRoute);
});

document.getElementById("open_add_stream").addEventListener("click", () => {
  if (accountIsReadOnly()) return;
  beginStreamWizard();
});

document.getElementById("active-streams-body").addEventListener("click", async event => {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.activeStreamMenu !== undefined) {
    const menu = target.nextElementSibling;
    const shouldOpen = menu.hidden;
    if (shouldOpen) {
      openStreamActionMenu(target, null);
    } else {
      closeStreamActionMenu(menu, false);
    }
    return;
  }
  if (target.dataset.action === "edit-active-stream") {
    closeStreamActionMenus();
    if (accountIsReadOnly()) return;
    editStreamSettings(target.dataset.streamId);
    return;
  }
  if (target.dataset.action === "toggle-active-stream") {
    closeStreamActionMenus();
    if (accountIsReadOnly()) return;
    const stream = configuredStreams.find(item => item.id === target.dataset.streamId);
    if (!stream) {
      setStreamResult("Stream was not found.", "error");
      return;
    }
    try {
      await setStreamEnabled(target.dataset.streamId, !streamIsEnabled(stream), setStreamsResult);
    } catch (error) {
      setStreamsResult(error.message, "error");
    }
    return;
  }
  if (target.dataset.action === "toggle-monitor") {
    closeStreamActionMenus();
    try {
      await toggleStreamMonitor(target.dataset.streamId, setStreamsResult);
    } catch (error) {
      setStreamsResult(error.message, "error");
      renderStreams(configuredStreams);
      if (settingsStreamId) renderStreamSettings();
    }
    return;
  }
  if (target.dataset.action === "remove-active-stream") {
    closeStreamActionMenus();
    if (accountIsReadOnly()) return;
    // Removing a stream deletes its archived audio, which can be months of it: ask first.
    const archive = streamArchivesStatus[target.dataset.streamId];
    if (archive && archive.ranges && archive.ranges.length) {
      const stream = configuredStreams.find(item => item.id === target.dataset.streamId);
      if (!window.confirm(`Remove stream ${stream ? streamCallsign(stream) : ""}? Its archived audio (${archive.storage}) will be permanently deleted.`)) return;
    }
    try {
      const data = await request(`/api/streams?id=${encodeURIComponent(target.dataset.streamId)}`, {
        method: "DELETE"
      });
      renderStreams(data.streams || []);
      setStreamsResult("");
    } catch (error) {
      setStreamsResult(error.message, "error");
    }
  }
});

document.getElementById("active-streams-body").addEventListener("keydown", event => {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.activeStreamMenu !== undefined) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openStreamActionMenu(target, "first");
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      openStreamActionMenu(target, "first");
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      openStreamActionMenu(target, "last");
      return;
    }
  }
  if (target.getAttribute("role") === "menuitem") {
    const menu = target.closest(".stream-actions-menu");
    if (!menu) return;
    const items = menuItems(menu);
    const index = items.indexOf(target);
    if (event.key === "ArrowDown") {
      event.preventDefault();
      items[(index + 1) % items.length].focus();
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      items[(index - 1 + items.length) % items.length].focus();
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      items[0].focus();
      return;
    }
    if (event.key === "End") {
      event.preventDefault();
      items[items.length - 1].focus();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeStreamActionMenu(menu, true);
    }
  }
});

document.addEventListener("click", event => {
  if (!event.target || !event.target.closest(".nav-more")) closeNavMoreMenu();
  if (!event.target || event.target.closest(".menu-cell")) return;
  closeStreamActionMenus();
});

document.addEventListener("focusin", event => {
  if (!event.target || !event.target.closest(".nav-more")) closeNavMoreMenu();
  if (event.target && event.target.closest(".menu-cell")) return;
  closeStreamActionMenus();
});

document.getElementById("rescan_devices").addEventListener("click", async () => {
  const button = document.getElementById("rescan_devices");
  setDisabled(button, true);
  try {
    const status = await request(statusRequestPath());
    await loadDevices(status.settings.serial);
  } catch (error) {
    setText("device-errors", error.message);
  } finally {
    setDisabled(button, false);
  }
});

// Resets the SDR this machine uses: its own, or a remote SDR's, which the host resets.
async function resetSdr(remote) {
  const serial = document.getElementById("serial").value;
  if (!remote && !serial) {
    setMessage("sdr-reset-status", "Select an RTL-SDR to reset.", "error");
    return;
  }
  sdrResetRequestPending = true;
  setSdrResetButtonsDisabled(true);
  setMessage("sdr-reset-status", "Resetting RTL-SDR...");
  try {
    const data = await requestWithTimeout("/api/rtl-reset", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(remote ? {} : {serial})
    }, SDR_RESET_REQUEST_TIMEOUT_MS);
    setMessage("sdr-reset-status", data.message || "RTL-SDR reset finished.", data.success && data.reappeared ? "success" : "error");
    sdrResetRequestPending = false;
    applyStatus(data.status || await request(statusRequestPath()), {syncControls: true});
    if (!remote) await loadDevices(serial);
  } catch (error) {
    setMessage("sdr-reset-status", error.message, "error");
  } finally {
    sdrResetRequestPending = false;
    setSdrResetButtonsDisabled(sdrResetInProgress);
  }
}

function setSdrResetButtonsDisabled(disabled) {
  setDisabled(document.getElementById("reset_rtl_device"), disabled);
  setDisabled(document.getElementById("reset_remote_sdr"), disabled);
}

// Every page shows a reset as it happens, whoever started it and on whichever
// machine; the page that started one shows its own result instead.
function renderSdrReset(data) {
  const reset = data.sdr_reset || null;
  sdrResetInProgress = Boolean(reset && reset.state === "resetting");
  setSdrResetButtonsDisabled(sdrResetInProgress || sdrResetRequestPending);
  if (sdrResetRequestPending) return;
  const key = reset ? `${reset.id}:${reset.state}` : "";
  if (key === lastSdrResetKey) return;
  lastSdrResetKey = key;
  if (!reset) {
    setMessage("sdr-reset-status", "");
    return;
  }
  setMessage("sdr-reset-status", reset.message, reset.state === "finished" ? "success" : reset.state === "failed" ? "error" : "");
}

document.getElementById("reset_rtl_device").addEventListener("click", () => resetSdr(false));
document.getElementById("reset_remote_sdr").addEventListener("click", () => resetSdr(true));

document.getElementById("rescan_iq_test_sources").addEventListener("click", async () => {
  const button = document.getElementById("rescan_iq_test_sources");
  setDisabled(button, true);
  try {
    await loadIqTestSources({force: true});
    setText("iq-test-source-result", "I/Q source file list updated.");
  } catch (error) {
    setText("iq-test-source-result", error.message);
  } finally {
    setDisabled(button, false);
  }
});

document.getElementById("start_iq_test_source").addEventListener("click", async () => {
  const button = document.getElementById("start_iq_test_source");
  const fileName = document.getElementById("iq_test_source_file").value;
  const sampleRate = Number(document.getElementById("iq_test_source_sample_rate").value);
  if (!fileName) {
    setText("iq-test-source-result", "Select an I/Q source file.");
    return;
  }
  setDisabled(button, true);
  setText("iq-test-source-result", "Starting I/Q file source...");
  try {
    const data = await request("/api/iq-test-source", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({file_name: fileName, sample_rate: sampleRate})
    });
    applyStatus(data, {syncControls: true});
    await loadIqTestSources({force: true});
  } catch (error) {
    setText("iq-test-source-result", error.message);
  } finally {
    setDisabled(button, false);
  }
});

document.getElementById("stop_iq_test_source").addEventListener("click", async () => {
  const button = document.getElementById("stop_iq_test_source");
  setDisabled(button, true);
  setText("iq-test-source-result", "Returning to RTL-SDR...");
  try {
    const data = await request("/api/iq-test-source/stop", {method: "POST"});
    applyStatus(data, {syncControls: true});
    await loadIqTestSources({force: true});
  } catch (error) {
    setText("iq-test-source-result", error.message);
  } finally {
    setDisabled(button, false);
  }
});

document.getElementById("seek_iq_test_source_back_60").addEventListener("click", async () => {
  try {
    await seekIqTestSource(-60);
  } catch (error) {
    setText("iq-test-source-result", error.message);
  }
});

document.getElementById("seek_iq_test_source_back_10").addEventListener("click", async () => {
  try {
    await seekIqTestSource(-10);
  } catch (error) {
    setText("iq-test-source-result", error.message);
  }
});

document.getElementById("seek_iq_test_source_forward_10").addEventListener("click", async () => {
  try {
    await seekIqTestSource(10);
  } catch (error) {
    setText("iq-test-source-result", error.message);
  }
});

document.getElementById("seek_iq_test_source_forward_60").addEventListener("click", async () => {
  try {
    await seekIqTestSource(60);
  } catch (error) {
    setText("iq-test-source-result", error.message);
  }
});

document.getElementById("iq_test_source_seek_controls").addEventListener("keydown", async event => {
  const keyMap = {
    ArrowUp: 10,
    ArrowDown: -10,
    PageUp: 60,
    PageDown: -60
  };
  if (!(event.key in keyMap)) return;
  event.preventDefault();
  try {
    await seekIqTestSource(keyMap[event.key]);
  } catch (error) {
    setText("iq-test-source-result", error.message);
  }
});

document.getElementById("station_search_button").addEventListener("click", async () => {
  try {
    await runStationSearchFromUi();
  } catch (error) {
    setStreamResult(error.message, "error");
  }
});

document.getElementById("station_search").addEventListener("input", () => {
  updateStationSearchControls();
  clearStationResults(stationSearchQuery() === "" ? "Enter search text to find a station." : "Press Search to find matching stations.");
});

document.getElementById("station_search").addEventListener("keydown", async event => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  try {
    await runStationSearchFromUi();
  } catch (error) {
    setStreamResult(error.message, "error");
  }
});

document.getElementById("station_results").addEventListener("change", chooseStation);

for (const formatControl of document.querySelectorAll("input[name='icecast_format']")) {
  formatControl.addEventListener("change", () => {
    const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
    const station = selectedStation();
    const mount = document.getElementById("icecast_mount").value;
    if (service === STREAM_SERVICE_WEATHERUSA && (!mount.trim() || isWeatherUsaGeneratedMount(mount, station))) {
      setValue("icecast_mount", weatherUsaMount(station, selectedWizardFormat()));
    }
    wizardDirty = true;
    icecastAuthPassed = false;
    icecastAuthSignature = "";
    renderWizard();
  });
}

document.getElementById("icecast_service").addEventListener("change", event => {
  wizardDirty = true;
  resetWizardForService(event.target.value || STREAM_SERVICE_CUSTOM);
});

for (const id of ["icecast_host", "icecast_port", "icecast_username", "icecast_password", "icecast_mount"]) {
  document.getElementById(id).addEventListener("input", () => {
    wizardDirty = true;
    icecastAuthPassed = false;
    icecastAuthSignature = "";
    renderWizard();
  });
}

for (const id of ["icecast_sample_rate", "icecast_bitrate", "icecast_alt_enabled", "icecast_alt_number"]) {
  document.getElementById(id).addEventListener("change", () => {
    wizardDirty = true;
    icecastAuthPassed = false;
    icecastAuthSignature = "";
    applyServiceControls("icecast", document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM);
    renderWizard();
  });
}

for (const control of document.querySelectorAll("input[name='wizard_output_type']")) {
  control.addEventListener("change", () => {
    wizardDirty = true;
    wizardSoundcardOutputId = "";
    if (selectedWizardOutputType() !== "soundcard") {
      discardWizardSoundcardPreview();
    }
    renderWizard();
  });
}

document.getElementById("wizard_soundcard_device").addEventListener("change", () => {
  wizardDirty = true;
  adjustWizardSoundcardChannelForDevice();
  if (wizardSoundcardPreviewId) scheduleWizardSoundcardUpdate();
  renderWizard();
});

for (const control of document.querySelectorAll("input[name='wizard_soundcard_channel']")) {
  control.addEventListener("change", scheduleWizardSoundcardUpdate);
}

document.getElementById("wizard_soundcard_volume").addEventListener("input", scheduleWizardSoundcardUpdate);

document.getElementById("show_icecast_password").addEventListener("change", event => {
  document.getElementById("icecast_password").type = event.target.checked ? "text" : "password";
});

document.getElementById("settings_show_icecast_password").addEventListener("change", event => {
  document.getElementById("settings_icecast_password").type = event.target.checked ? "text" : "password";
});

for (const id of [
  "settings_icecast_service",
  "settings_icecast_host",
  "settings_icecast_port",
  "settings_icecast_username",
  "settings_icecast_password",
  "settings_icecast_mount",
  "settings_icecast_sample_rate",
  "settings_icecast_bitrate",
  "settings_icecast_alt_enabled",
  "settings_icecast_alt_number"
]) {
  document.getElementById(id).addEventListener("input", updateOutputFormButtons);
  document.getElementById(id).addEventListener("change", event => {
    if (id === "settings_icecast_service") {
      resetSettingsForService(event.target.value || STREAM_SERVICE_CUSTOM);
      return;
    }
    if (id === "settings_icecast_alt_enabled" || id === "settings_icecast_alt_number") {
      applyServiceControls("settings_icecast", document.getElementById("settings_icecast_service").value || STREAM_SERVICE_CUSTOM);
    }
    updateOutputFormButtons();
  });
}

for (const formatControl of document.querySelectorAll("input[name='settings_icecast_format']")) {
  formatControl.addEventListener("change", () => {
    const service = document.getElementById("settings_icecast_service").value || STREAM_SERVICE_CUSTOM;
    const stream = currentSettingsStream();
    const station = stream ? stream.station : null;
    const mount = document.getElementById("settings_icecast_mount").value;
    if (service === STREAM_SERVICE_WEATHERUSA && (!mount.trim() || isWeatherUsaGeneratedMount(mount, station))) {
      setValue("settings_icecast_mount", weatherUsaMount(station, selectedSettingsFormat()));
    }
    updateOutputFormButtons();
  });
}

document.getElementById("settings_soundcard_device").addEventListener("change", () => {
  adjustSettingsSoundcardChannelForDevice();
  scheduleSettingsSoundcardUpdate();
});

for (const control of document.querySelectorAll("input[name='settings_soundcard_channel']")) {
  control.addEventListener("change", scheduleSettingsSoundcardUpdate);
}

document.getElementById("settings_soundcard_volume").addEventListener("input", scheduleSettingsSoundcardUpdate);

document.getElementById("reset_soundcard_device").addEventListener("click", async event => {
  const button = event.currentTarget;
  const stableId = document.getElementById("settings_soundcard_device").value || "";
  if (!stableId) {
    setOutputResult("Select a sound card to reset.", "error");
    return;
  }
  setDisabled(button, true);
  try {
    await resetSoundcardByStableId(stableId);
  } catch (error) {
    setOutputResult(error.message, "error");
  } finally {
    setDisabled(button, false);
    updateSettingsSoundcardResetButton();
  }
});

document.getElementById("open_add_output").addEventListener("click", beginAddOutput);

document.getElementById("output_type_filter").addEventListener("change", () => {
  renderOutputPanels();
});

document.getElementById("cancel_output_form").addEventListener("click", cancelOutputForm);
document.getElementById("cancel_soundcard_output_form").addEventListener("click", cancelOutputForm);

document.getElementById("stream_enabled").addEventListener("change", async event => {
  if (!settingsStreamId || applying) return;
  if (event.target.dataset.busy === "true") {
    renderStreamSettings();
    return;
  }
  const enabled = event.target.checked;
  setControlBusy(event.target, true);
  try {
    await setStreamEnabled(settingsStreamId, enabled, setOutputResult);
  } catch (error) {
    setOutputResult(error.message, "error");
    const stream = currentSettingsStream();
    if (stream) setChecked("stream_enabled", streamIsEnabled(stream));
  } finally {
    setControlBusy(event.target, false);
  }
});

document.getElementById("stream_monitor_enabled").addEventListener("change", async event => {
  if (!settingsStreamId || applying) return;
  if (event.target.dataset.busy === "true") {
    renderStreamSettings();
    return;
  }
  setControlBusy(event.target, true);
  try {
    if (event.target.checked) {
      await toggleStreamMonitor(settingsStreamId, setStreamSettingsResult);
    } else if (monitorStreamId === settingsStreamId) {
      await stopStreamMonitor();
      setStreamSettingsResult("Monitoring stopped.", "success");
    }
  } catch (error) {
    setStreamSettingsResult(error.message, "error");
  } finally {
    setControlBusy(event.target, false);
    renderStreamSettings();
  }
});

document.getElementById("add_output").addEventListener("click", async () => {
  const button = document.getElementById("add_output");
  const icecast = settingsIcecastPayload();
  if (duplicateOutputExists(icecast)) {
    setOutputResult("An output with these credentials already exists.", "error");
    return;
  }
  setDisabled(button, true);
  setOutputResult("Testing Icecast authentication...");
  try {
    const data = await request("/api/stream-output", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({stream_id: settingsStreamId, icecast})
    });
    icecastOutputTableSignature = "";
    soundcardOutputTableSignature = "";
    renderStreams(data.streams || []);
    if (data.success) {
      cancelOutputForm();
      maybeShowNwrOrgSubmissionNotice(icecast);
    }
    setOutputResult(data.message, data.success ? "success" : "error");
  } catch (error) {
    setOutputResult(error.message, "error");
  } finally {
    setDisabled(button, false);
    updateOutputFormButtons();
  }
});

document.getElementById("save_output_settings").addEventListener("click", async () => {
  const button = document.getElementById("save_output_settings");
  const selected = findConfiguredOutput(settingsStreamId, editingOutputId);
  if (!selected) {
    setOutputResult("Stream output was not found.", "error");
    return;
  }
  const icecast = settingsIcecastPayload();
  const oldService = serviceFromIcecast(selected.output.icecast || {});
  if (duplicateOutputExists(icecast, editingOutputId)) {
    setOutputResult("An output with these credentials already exists.", "error");
    return;
  }
  setDisabled(button, true);
  setOutputResult("Saving output settings...");
  try {
    const data = await request("/api/stream-output", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        stream_id: settingsStreamId,
        output_id: editingOutputId,
        enabled: selected.output.enabled !== false,
        icecast
      })
    });
    renderStreams(data.streams || []);
    if (data.success) {
      cancelOutputForm();
      if (oldService !== STREAM_SERVICE_NWRORG) maybeShowNwrOrgSubmissionNotice(icecast);
    }
    setOutputResult(data.message, data.success ? "success" : "error");
  } catch (error) {
    setOutputResult(error.message, "error");
  } finally {
    setDisabled(button, false);
    updateOutputFormButtons();
  }
});

async function handleOutputTableClick(event) {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.outputMenu !== undefined) {
    const menu = target.nextElementSibling;
    const shouldOpen = menu.hidden;
    if (shouldOpen) {
      openStreamActionMenu(target, null);
    } else {
      closeStreamActionMenu(menu, false);
    }
    return;
  }
  if (target.dataset.action === "edit-settings-output") {
    closeStreamActionMenus();
    beginEditOutput(target.dataset.outputId);
    return;
  }
  if (target.dataset.action === "toggle-output") {
    closeStreamActionMenus();
    const selected = findConfiguredOutput(target.dataset.streamId, target.dataset.outputId);
    if (!selected) {
      setOutputResult("Stream output was not found.", "error");
      return;
    }
    const nextEnabled = selected.output.enabled === false;
    if (!nextEnabled && !canDisableOutput(selected.stream, selected.output)) {
      setOutputResult("At least one output must remain enabled for each stream.", "error");
      return;
    }
    try {
      const data = await request("/api/stream-output", {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          stream_id: target.dataset.streamId,
          output_id: target.dataset.outputId,
          enabled: nextEnabled,
          type: selected.output.type || "icecast",
          icecast: selected.output.icecast,
          soundcard: selected.output.soundcard
        })
      });
      renderStreams(data.streams || []);
      setOutputResult(data.message, data.success ? "success" : "error");
    } catch (error) {
      setOutputResult(error.message, "error");
    }
    return;
  }
  if (target.dataset.action === "reset-soundcard-output") {
    closeStreamActionMenus();
    const selected = findConfiguredOutput(target.dataset.streamId, target.dataset.outputId);
    if (!selected || selected.output.type !== "soundcard") {
      setOutputResult("Sound card output was not found.", "error");
      return;
    }
    const device = soundcardOutputUsbDevice(selected.output);
    if (!device) {
      setOutputResult("Only USB sound cards can be reset.", "error");
      return;
    }
    target.disabled = true;
    try {
      await resetSoundcardByStableId(device.stable_id);
    } catch (error) {
      setOutputResult(error.message, "error");
    } finally {
      target.disabled = false;
    }
    return;
  }
  if (target.dataset.action === "remove-output") {
    closeStreamActionMenus();
    const selected = findConfiguredOutput(target.dataset.streamId, target.dataset.outputId);
    if (!selected) {
      setOutputResult("Stream output was not found.", "error");
      return;
    }
    if (!canRemoveOutput(selected.stream, selected.output)) {
      setOutputResult("At least one output must remain enabled for each stream.", "error");
      return;
    }
    try {
      const data = await request(
        `/api/stream-output?stream_id=${encodeURIComponent(target.dataset.streamId)}&output_id=${encodeURIComponent(target.dataset.outputId)}`,
        {method: "DELETE"}
      );
      renderStreams(data.streams || []);
      cancelOutputForm();
      setOutputResult("");
    } catch (error) {
      setOutputResult(error.message, "error");
    }
  }
}

function handleOutputTableKeydown(event) {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.outputMenu !== undefined) {
    if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
      event.preventDefault();
      openStreamActionMenu(target, "first");
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      openStreamActionMenu(target, "last");
      return;
    }
  }
  if (target.getAttribute("role") === "menuitem") {
    const menu = target.closest(".stream-actions-menu");
    if (!menu) return;
    const items = menuItems(menu);
    const index = items.indexOf(target);
    if (event.key === "ArrowDown") {
      event.preventDefault();
      items[(index + 1) % items.length].focus();
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      items[(index - 1 + items.length) % items.length].focus();
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      items[0].focus();
      return;
    }
    if (event.key === "End") {
      event.preventDefault();
      items[items.length - 1].focus();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeStreamActionMenu(menu, true);
    }
  }
}

document.getElementById("icecast-outputs-body").addEventListener("click", handleOutputTableClick);
document.getElementById("soundcard-outputs-body").addEventListener("click", handleOutputTableClick);
document.getElementById("icecast-outputs-body").addEventListener("keydown", handleOutputTableKeydown);
document.getElementById("soundcard-outputs-body").addEventListener("keydown", handleOutputTableKeydown);

document.getElementById("cancel_wizard").addEventListener("click", async () => {
  if (wizardMode === "add_output" && selectedWizardOutputType() === "soundcard") {
    await discardWizardSoundcardOutput();
  }
  if (wizardMode === "add" && selectedWizardOutputType() === "soundcard") {
    await discardWizardSoundcardPreview();
  }
  finishWizard();
});

document.getElementById("wizard_back").addEventListener("click", async () => {
  if (wizardUsesOutputSteps()) {
    if (wizardStep === ADD_OUTPUT_TYPE_STEP) {
      if (wizardMode === "add") setWizardStep(0);
      return;
    }
    if (wizardStep === ADD_OUTPUT_ICECAST_SERVICE_STEP || wizardStep === ADD_OUTPUT_SOUNDCARD_DEVICE_STEP) {
      if (wizardMode === "add" && wizardStep === ADD_OUTPUT_SOUNDCARD_DEVICE_STEP) {
        await discardWizardSoundcardPreview();
      }
      setWizardStep(ADD_OUTPUT_TYPE_STEP);
      return;
    }
    if (wizardStep === ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP) {
      setWizardStep(ADD_OUTPUT_SOUNDCARD_DEVICE_STEP);
      return;
    }
    const streamStep = streamIcecastStep(wizardStep);
    const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
    if (streamStep === 3 && (service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_NWRORG)) {
      setWizardStep(ADD_OUTPUT_ICECAST_SERVICE_STEP);
    } else {
      setWizardStep(addOutputIcecastStep(streamStep - 1));
    }
    return;
  }
  const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
  if (wizardStep === 3 && (service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_NWRORG)) {
    setWizardStep(1);
  } else {
    setWizardStep(wizardStep - 1);
  }
});

document.getElementById("wizard_next").addEventListener("click", async () => {
  if (wizardUsesOutputSteps()) {
    if (wizardStep === ADD_OUTPUT_TYPE_STEP) {
      if (selectedWizardOutputType() === "soundcard") {
        setWizardStep(ADD_OUTPUT_SOUNDCARD_DEVICE_STEP);
        loadDevices("", {force: true}).catch(error => {
          setStreamResult(error.message, "error");
          renderWizard();
        });
      } else {
        setWizardStep(ADD_OUTPUT_ICECAST_SERVICE_STEP);
      }
      return;
    }
    if (wizardStep === ADD_OUTPUT_SOUNDCARD_DEVICE_STEP) {
      if (wizardMode === "add_output") {
        await createWizardSoundcardOutput();
      } else {
        await upsertWizardSoundcardPreview();
      }
      return;
    }
    const streamStep = streamIcecastStep(wizardStep);
    if (streamStep === 1) {
      const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
      if (service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_NWRORG) {
        setWizardStep(ADD_OUTPUT_ICECAST_CREDENTIALS_STEP);
      } else {
        setWizardStep(ADD_OUTPUT_ICECAST_CODEC_STEP);
      }
      return;
    }
    if (streamStep === 2) {
      const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
      if (service === STREAM_SERVICE_WEATHERUSA && !document.getElementById("icecast_mount").value.trim()) {
        setValue("icecast_mount", weatherUsaMount(selectedStation(), selectedWizardFormat()));
      }
      setWizardStep(ADD_OUTPUT_ICECAST_CREDENTIALS_STEP);
      return;
    }
    if (streamStep === 3) {
      await authenticateWizardIcecastOutput();
      return;
    }
  }
  if (wizardStep === 0) {
    setWizardStep(ADD_OUTPUT_TYPE_STEP);
    return;
  }
  if (wizardStep === 1) {
    const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
    if (service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_NWRORG) {
      setWizardStep(3);
    } else {
      setWizardStep(2);
    }
    return;
  }
  if (wizardStep === 2) {
    const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
    if (service === STREAM_SERVICE_WEATHERUSA && !document.getElementById("icecast_mount").value.trim()) {
      setValue("icecast_mount", weatherUsaMount(selectedStation(), selectedWizardFormat()));
    }
    setWizardStep(3);
    return;
  }
  if (wizardStep === 3) {
    await authenticateWizardIcecastOutput();
    return;
  }
});

document.getElementById("wizard_finish").addEventListener("click", async () => {
  if (wizardMode === "add_output" && selectedWizardOutputType() === "soundcard") {
    wizardDirty = false;
    await updateWizardSoundcardOutput();
    finishWizard();
    return;
  }
  if (wizardMode === "add" && selectedWizardOutputType() === "soundcard") {
    const button = document.getElementById("wizard_finish");
    setDisabled(button, true);
    setStreamResult("Creating stream...");
    try {
      const data = await request("/api/streams", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(streamPayload())
      });
      renderStreams(data.streams || []);
      setStreamResult(data.message, data.success ? "success" : "error");
      if (data.success) {
        wizardSoundcardPreviewId = "";
        finishWizard();
      }
    } catch (error) {
      setStreamResult(error.message, "error");
    } finally {
      setDisabled(button, false);
      renderWizard();
    }
    return;
  }
  const button = document.getElementById("wizard_finish");
  setDisabled(button, true);
  const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
  const addOutputMode = wizardMode === "add_output";
  setStreamResult(service === STREAM_SERVICE_NWRORG && !icecastAuthPassed ? "Testing Icecast authentication..." : addOutputMode ? "Adding output..." : "Creating stream...");
  try {
    const payload = streamPayload();
    const createdIcecast = payload.icecast;
    if (addOutputMode && duplicateOutputExists(createdIcecast)) {
      setStreamResult("An output with these credentials already exists.", "error");
      return;
    }
    if (!icecastAuthPassed || icecastAuthSignature !== icecastCredentialSignature()) {
      const signature = icecastCredentialSignature();
      const auth = await request("/api/icecast-auth", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({icecast: createdIcecast})
      });
      if (!auth.success) {
        setStreamResult(auth.message, "error");
        return;
      }
      icecastAuthPassed = true;
      icecastAuthSignature = signature;
    }
    setStreamResult(addOutputMode ? "Adding output..." : "Creating stream...");
    const data = addOutputMode
      ? await request("/api/stream-output", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({stream_id: settingsStreamId, icecast: createdIcecast})
        })
      : await request("/api/streams", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
    renderStreams(data.streams || []);
    setStreamResult(data.message, data.success ? "success" : "error");
    if (data.success) {
      finishWizard();
      maybeShowNwrOrgSubmissionNotice(createdIcecast);
    }
  } catch (error) {
    setStreamResult(error.message, "error");
  } finally {
    setDisabled(button, false);
    renderWizard();
  }
});

function sendLiveAudioStopBeacon(options = {}) {
  const forceReceiverStop = options.forceReceiverStop === true;
  if ((unloadLiveAudioStopSent && !forceReceiverStop) || !navigator.sendBeacon) return;
  unloadLiveAudioStopSent = true;
  if (monitorStreamId && navigator.sendBeacon) {
    const payload = JSON.stringify({client_id: pageMonitorClientId()});
    navigator.sendBeacon("/api/monitor/stop", new Blob([payload], {type: "application/json"}));
  }
  if (receiverPeerConnection && (!receiverPaused || forceReceiverStop) && navigator.sendBeacon) {
    const payload = JSON.stringify({client_id: pageReceiverClientId()});
    navigator.sendBeacon("/api/receiver/stop", new Blob([payload], {type: "application/json"}));
  }
}

window.addEventListener("pagehide", event => {
  if (event.persisted) return;
  if (remotePairingPage === "pair_device" && navigator.sendBeacon) {
    navigator.sendBeacon("/api/remote-sdr/pairing/cancel", new Blob(["{}"], {type: "application/json"}));
  }
  sendWizardSoundcardPreviewDiscardBeacon();
  if (document.visibilityState === "hidden") return;
  sendLiveAudioStopBeacon();
});

window.addEventListener("pageshow", () => {
  unloadLiveAudioStopSent = false;
});

const liveAudioElement = document.getElementById("stream_monitor_audio");
if (liveAudioElement) {
  liveAudioElement.addEventListener("pause", () => {
    if (receiverPeerConnection && receiverPlaying && !receiverPaused) {
      logClientEvent("info", "receiver", "receiver native media pause observed", {frequency: currentReceiverChannel().label});
      stopWeatherReceiver({preserveMediaSession: true})
        .catch(error => console.debug("receiver native pause handling failed", error))
      return;
    }
    if (monitorPeerConnection && monitorStreamId && !monitorPaused) {
      logClientEvent("info", "monitor", "monitor native media pause observed", {stream_id: monitorStreamId});
      pauseStreamMonitor()
        .catch(error => console.debug("monitor native pause handling failed", error));
      return;
    }
    if (receiverPaused && receiverPeerConnection) {
      scheduleReceiverMediaSessionRefresh();
    }
  });
}

window.addEventListener("beforeunload", event => {
  sendWizardSoundcardPreviewDiscardBeacon();
  stopStreamTestMode({beacon: true});
  sendLiveAudioStopBeacon({forceReceiverStop: true});
  if (!hasUnsavedNavigationState()) return;
  event.preventDefault();
  event.returnValue = "";
});

document.getElementById("eas_alert_stream").addEventListener("change", event => {
  easAlertStreamId = event.target.value;
  easAlertPage = 1;
  easAlertListSignature = "";
  easBulkOptionsSignature = "";
  navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage}, true, true);
});

document.getElementById("open_eas_export").addEventListener("click", () => {
  easBulkOptionsSignature = "";
  navigateTo("eas_alert_export", {streamId: easAlertStreamId, page: easAlertPage}, true, true);
});

document.getElementById("open_eas_delete").addEventListener("click", () => {
  if (accountIsReadOnly()) return;
  easBulkOptionsSignature = "";
  navigateTo("eas_alert_delete", {streamId: easAlertStreamId, page: easAlertPage}, true, true);
});

document.getElementById("eas_export_options").addEventListener("change", () => updateManualRangeVisibility("export"));
document.getElementById("eas_delete_options").addEventListener("change", () => updateManualRangeVisibility("delete"));

document.getElementById("eas_export_manual").addEventListener("click", event => {
  if (event.target && event.target.dataset && event.target.dataset.bulkNow) setEndRangeToNow(event.target.dataset.bulkNow);
});

document.getElementById("eas_delete_manual").addEventListener("click", event => {
  if (event.target && event.target.dataset && event.target.dataset.bulkNow) setEndRangeToNow(event.target.dataset.bulkNow);
});

document.getElementById("eas_export_alerts").addEventListener("click", exportEasAlerts);
document.getElementById("eas_delete_alerts").addEventListener("click", deleteEasAlerts);
document.getElementById("cancel_eas_export").addEventListener("click", () => {
  navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage}, true, true);
});
document.getElementById("cancel_eas_delete").addEventListener("click", () => {
  navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage}, true, true);
});

document.getElementById("eas_alert_prev").addEventListener("click", () => {
  if (easAlertPage <= 1) return;
  easAlertPage -= 1;
  navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage}, true, true);
});

document.getElementById("eas_alert_next").addEventListener("click", () => {
  if (easAlertPage >= easAlertTotalPages) return;
  easAlertPage += 1;
  navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage}, true, true);
});

document.getElementById("eas-alert-list").addEventListener("click", event => {
  const link = event.target && event.target.closest ? event.target.closest("a[data-alert-id]") : null;
  if (!link) return;
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  easAlertDetailId = link.dataset.alertId;
  easAlertReturnPage = easAlertPage;
  navigateTo("eas_alert_detail", {
    streamId: easAlertStreamId,
    alertId: easAlertDetailId,
    page: easAlertReturnPage
  }, true, true);
});

document.getElementById("back_to_eas_alerts").addEventListener("click", event => {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  pauseCurrentEasAlert();
  easAlertDetailId = "";
  navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertReturnPage}, true, true);
});

document.getElementById("remove_eas_alert").addEventListener("click", async () => {
  if (accountIsReadOnly()) return;
  if (!window.confirm("Remove this EAS alert and its audio file?")) return;
  await removeCurrentEasAlert();
});

document.getElementById("eas_alert_detail_previous").addEventListener("click", () => {
  const shouldResume = easAlertDetailIsPlaying;
  navigateEasAlertDetail("previous", {replace: true, autoplay: shouldResume}).catch(error => setEasAlertDetailResult(error.message, "error"));
});

document.getElementById("eas_alert_detail_next").addEventListener("click", () => {
  const shouldResume = easAlertDetailIsPlaying;
  navigateEasAlertDetail("next", {replace: true, autoplay: shouldResume}).catch(error => setEasAlertDetailResult(error.message, "error"));
});

document.getElementById("eas_alert_detail_play_pause").addEventListener("click", () => {
  if (easAlertDetailIsPlaying) {
    pauseCurrentEasAlert();
  } else {
    playCurrentEasAlert().catch(error => setEasAlertDetailResult(`Alert audio could not start: ${error.message}`, "error"));
  }
});

document.getElementById("eas_alert_autoplay").addEventListener("change", event => {
  easAlertAutoplay = Boolean(event.target.checked);
});

document.getElementById("eas_alert_volume").addEventListener("input", event => {
  easAlertPlaybackVolume = Number(event.target.value);
  applyEasAlertPlaybackVolume();
});

document.getElementById("eas_alert_audio").addEventListener("play", () => {
  setEasAlertPlaybackState(true);
});

document.getElementById("eas_alert_audio").addEventListener("pause", () => {
  setEasAlertPlaybackState(false);
});

for (const eventName of ["loadedmetadata", "durationchange", "timeupdate", "seeked", "ratechange"]) {
  document.getElementById("eas_alert_audio").addEventListener(eventName, () => {
    updateEasAlertPositionState();
  });
}

document.getElementById("eas_alert_audio").addEventListener("ended", async () => {
  setEasAlertPlaybackState(false);
  updateEasAlertPositionState();
  if (!easAlertAutoplay) {
    clearEasAlertBrowserMediaSession();
    return;
  }
  try {
    const advanced = await navigateEasAlertDetail("next", {replace: true, autoplay: true});
    if (!advanced) clearEasAlertBrowserMediaSession();
  } catch (error) {
    clearEasAlertBrowserMediaSession();
    setEasAlertDetailResult(error.message, "error");
  }
});

async function refresh() {
  if (serverUnavailable) return;
  const requestNumber = ++statusRequestCount;
  let data;
  try {
    data = await request(statusRequestPath());
  } catch (error) {
    noteServerRequestFailure(error);
    throw error;
  }
  serverFailureCount = 0;
  applyStatus(data, {syncControls: false, requestNumber});
}

// The server says when SDR settings change, here or on a remote SDR's host or
// another machine using it, so they show at once instead of at the next status check.
const SETTINGS_EVENTS_RETRY_MIN_MS = 1000;
// The server closes with this code when it shuts down.
const SERVER_SHUTDOWN_CLOSE_CODE = 1012;
const SETTINGS_EVENTS_RETRY_MAX_MS = 30000;
let settingsEventsSocket = null;
let settingsEventsRetryMs = SETTINGS_EVENTS_RETRY_MIN_MS;
let settingsChangeRefreshRunning = false;
let settingsChangeRefreshAgain = false;

function settingsEventsUrl() {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/api/settings-events`;
}

function startSettingsEvents() {
  if (settingsEventsSocket || typeof WebSocket === "undefined") return;
  let socket;
  try {
    socket = new WebSocket(settingsEventsUrl());
  } catch (error) {
    scheduleSettingsEventsRetry();
    return;
  }
  settingsEventsSocket = socket;
  socket.addEventListener("open", () => {
    settingsEventsRetryMs = SETTINGS_EVENTS_RETRY_MIN_MS;
    // Anything changed while not connected shows now.
    refreshForSettingsChange();
  });
  socket.addEventListener("message", event => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (error) {
      return;
    }
    if (message.type === "settings_changed") refreshForSettingsChange();
    if (message.type === "server_restarting") enterServerRestarting(message.instance);
  });
  socket.addEventListener("close", event => {
    if (settingsEventsSocket === socket) settingsEventsSocket = null;
    if (event.code === SERVER_SHUTDOWN_CLOSE_CODE) {
      enterServerUnavailable("shutdown");
      return;
    }
    scheduleSettingsEventsRetry();
  });
}

function scheduleSettingsEventsRetry() {
  // Status checks every second keep the page current meanwhile.
  const delay = settingsEventsRetryMs;
  settingsEventsRetryMs = Math.min(SETTINGS_EVENTS_RETRY_MAX_MS, settingsEventsRetryMs * 2);
  setTimeout(startSettingsEvents, delay);
}

async function refreshForSettingsChange() {
  // Changes arriving while a check is under way get one more check after it.
  if (settingsChangeRefreshRunning) {
    settingsChangeRefreshAgain = true;
    return;
  }
  settingsChangeRefreshRunning = true;
  try {
    do {
      settingsChangeRefreshAgain = false;
      await refresh().catch(() => {});
    } while (settingsChangeRefreshAgain);
  } finally {
    settingsChangeRefreshRunning = false;
  }
}

const RESUME_LISTENING_STORAGE_KEY = "nwr-stream-manager:resume-listening";
// A server that is shutting down says so, and the page reacts at once. Otherwise it
// takes this long without reaching it, so a device switching between Wi-Fi and
// cellular rides it out instead of being told the server may be restarting.
const SERVER_UNREACHABLE_GRACE_MS = 20000;
const SERVER_RETURN_CHECK_MS = 1000;
let serverUnavailable = false;
let serverFailureCount = 0;
let serverUnreachableSince = 0;
let serverReturnTimer = null;
// The run of NWR Stream Manager that's restarting; until another answers, it isn't back.
let restartingFromInstance = "";

function noteServerRequestFailure(error) {
  // fetch() rejects with a TypeError when it can't reach the server at all.
  if (!(error instanceof TypeError)) return;
  // Audio still arriving means the server is there.
  if (liveAudioConnectionHealthy()) {
    serverFailureCount = 0;
    return;
  }
  serverFailureCount += 1;
  if (serverFailureCount === 1) serverUnreachableSince = Date.now();
  if (Date.now() - serverUnreachableSince >= SERVER_UNREACHABLE_GRACE_MS) enterServerUnavailable("unreachable");
}

function serverUnavailableModalTargets() {
  return [
    document.querySelector("header"),
    document.querySelector("main"),
    ...document.querySelectorAll(".global-status-banner, .notice-dialog, .same-alert-flash, #same_alert_live, #listening_status_live")
  ].filter(Boolean);
}

function setServerUnavailableModalOpen(open) {
  for (const element of serverUnavailableModalTargets()) {
    if ("inert" in element) element.inert = open;
    if (open) element.setAttribute("aria-hidden", "true");
    else element.removeAttribute("aria-hidden");
  }
}

function rememberListeningForRestart() {
  let listening = null;
  if (monitorStreamId) {
    const stream = configuredStreams.find(item => item.id === monitorStreamId);
    listening = {mode: "monitor", stream_id: monitorStreamId, label: streamCallsign(stream)};
  } else if (receiverPeerConnection && receiverPlaying) {
    const channel = currentReceiverChannel();
    listening = {mode: "receiver", frequency_hz: channel.frequency_hz, label: channel.label};
  }
  if (!listening) return;
  try {
    sessionStorage.setItem(RESUME_LISTENING_STORAGE_KEY, JSON.stringify(listening));
  } catch (error) {
    console.debug("could not remember listening state", error);
  }
}

function enterServerUnavailable(reason) {
  if (serverUnavailable) return;
  serverUnavailable = true;
  logClientEvent("info", "server", "server unavailable", {reason});
  rememberListeningForRestart();
  // The server is gone, so stop listening here without asking it to, and without
  // blaming the connection.
  clearMonitorUnstableTimer();
  clearReceiverUnstableTimer();
  stopStreamMonitor({notifyServer: false}).catch(error => console.debug("monitor stop failed", error));
  stopWeatherReceiver({notifyServer: false}).catch(error => console.debug("receiver stop failed", error));
  dismissMonitorUnstableDialog();
  dismissReceiverUnstableDialog();
  const titles = {
    restart: "NWR Stream Manager is restarting",
    shutdown: "NWR Stream Manager is shutting down"
  };
  const messages = {
    restart: "This page will reload automatically as soon as it's back.",
    shutdown: "It may be restarting. This page will reload automatically as soon as it's back."
  };
  setText("server_unavailable_title", titles[reason] || "Can't reach NWR Stream Manager");
  setText(
    "server_unavailable_message",
    messages[reason] || "It may be restarting, or this device may have lost its network connection. This page will reload automatically as soon as NWR Stream Manager is back."
  );
  setHidden("server_unavailable_reload", true);
  setServerUnavailableModalOpen(true);
  setHidden("server_unavailable_dialog", false);
  document.getElementById("server_unavailable_title").focus();
  clearInterval(serverReturnTimer);
  serverReturnTimer = setInterval(checkServerReturned, SERVER_RETURN_CHECK_MS);
}

function enterServerRestarting(instance) {
  if (instance) restartingFromInstance = instance;
  enterServerUnavailable("restart");
}

async function requestRestart() {
  closeNavMoreMenu();
  if (!window.confirm("Restart NWR Stream Manager? Your streams will go offline for a few seconds and any I/Q recordings in progress will be ended.")) {
    document.getElementById("nav_more_button").focus();
    return;
  }
  try {
    // Names the run being restarted, so this request can never restart a later one.
    const state = await request("/api/setup-state");
    const data = await request("/api/restart", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({instance: state.instance})
    });
    enterServerRestarting(data.instance);
  } catch (error) {
    window.alert(`NWR Stream Manager couldn't restart: ${error.message}`);
  }
}

async function checkServerReturned() {
  try {
    const response = await fetch("/api/setup-state", {cache: "no-store"});
    if (!response.ok) return;
    if (restartingFromInstance) {
      const state = await response.json();
      if (state.instance === restartingFromInstance) return;
    }
  } catch (error) {
    return;
  }
  clearInterval(serverReturnTimer);
  serverReturnTimer = null;
  if (hasUnsavedNavigationState()) {
    // Reloading would throw away what they were in the middle of, so let them choose.
    setText("server_unavailable_title", "NWR Stream Manager is back");
    setText("server_unavailable_message", "Reload the page to continue. Anything you hadn't finished will need to be entered again.");
    setHidden("server_unavailable_reload", false);
    document.getElementById("server_unavailable_reload").focus();
    return;
  }
  window.location.reload();
}

function offerResumeListening() {
  let listening = null;
  try {
    listening = JSON.parse(sessionStorage.getItem(RESUME_LISTENING_STORAGE_KEY) || "null");
    sessionStorage.removeItem(RESUME_LISTENING_STORAGE_KEY);
  } catch (error) {
    return;
  }
  if (!listening || !listening.mode) return;
  const what = listening.mode === "monitor" ? `monitoring ${listening.label}` : `the weather radio receiver on ${listening.label}`;
  setText("resume_listening_message", `Listening stopped while it restarted. Resume ${what}?`);
  const dialog = document.getElementById("resume_listening_dialog");
  dialog.dataset.listening = JSON.stringify(listening);
  dialog.hidden = false;
  document.getElementById("resume_listening").focus();
}

function hideResumeListeningOffer() {
  // Once they are listening again, by any route, the offer no longer applies.
  const dialog = document.getElementById("resume_listening_dialog");
  if (dialog) dialog.hidden = true;
}

async function resumeListening() {
  const dialog = document.getElementById("resume_listening_dialog");
  let listening = null;
  try {
    listening = JSON.parse(dialog.dataset.listening || "null");
  } catch (error) {
    listening = null;
  }
  dialog.hidden = true;
  if (!listening) return;
  try {
    if (listening.mode === "monitor") {
      await toggleStreamMonitor(listening.stream_id);
    } else if (listening.mode === "receiver") {
      const index = NWR_RECEIVER_CHANNELS.findIndex(channel => channel.frequency_hz === listening.frequency_hz);
      if (index >= 0) receiverChannelIndex = index;
      renderReceiverControls();
      await startWeatherReceiver();
    }
  } catch (error) {
    setText("resume_listening_message", `Couldn't resume listening: ${error.message}`);
    dialog.hidden = false;
  }
}

(async function init() {
  populateBitrates();
  populateIqSampleRates();
  selectTestTone("sine", false);
  renderReceiverControls();
  const data = await request("/api/status");
  if (!data.account || !data.account.read_only) {
    await loadDevices(data.settings.serial);
  }
  await searchStations();
  await loadStreams();
  await loadEasAlertStreams({preserve: true, quiet: true});
  await loadIqRecordings();
  applyStatus(data, {syncControls: true});
  const initialRoute = routeFromLocation();
  navigateTo(initialRoute.view, {
    streamId: initialRoute.streamId,
    outputId: initialRoute.outputId,
    alertId: initialRoute.alertId,
    recordingId: initialRoute.recordingId,
    page: initialRoute.page,
    section: initialRoute.section,
    mode: initialRoute.mode
  }, true);
  offerResumeListening();
  setInterval(refresh, 1000);
  startSettingsEvents();
  window.addEventListener("online", retryLiveAudioReconnectNow);
  setInterval(async () => {
    try {
      const status = await request(statusRequestPath());
      if (status.account && status.account.read_only) {
        setText("device-errors", "");
        return;
      }
      // A reset briefly takes the RTL-SDR off USB; the reset message says how it is
      // going, as it does for a remote SDR. The list is checked again once it ends.
      if (sdrResetRequestPending || sdrResetInProgress) return;
      await loadDevices(status.settings.serial);
    } catch (error) {
      setText("device-errors", error.message);
    }
  }, 5000);
})();

document.getElementById("archive_enabled").addEventListener("change", () => sendArchiveSettings());
document.getElementById("archive_retention").addEventListener("change", () => sendArchiveSettings());
document.getElementById("stream_archive_stream").addEventListener("change", event => {
  streamArchiveStreamId = event.target.value;
  replaceCurrentRoute("stream_archives", {streamId: streamArchiveStreamId});
  renderStreamArchives(true);
});
document.getElementById("stream_archive_download_form").addEventListener("submit", () => {
  setStreamArchiveResult("Download started.", "success");
});
for (const id of ["stream_archive_open_delete", "stream_archive_open_delete_link"]) {
  document.getElementById(id).addEventListener("click", event => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigateTo("stream_archive_delete", {streamId: streamArchiveStreamId});
  });
}
document.getElementById("stream_archive_delete_audio").addEventListener("click", () => {
  deleteStreamArchiveAudio().catch(error => setStreamArchiveDeleteResult(error.message, "error"));
});
document.getElementById("cancel_stream_archive_delete").addEventListener("click", () => {
  navigateTo("stream_archives", {streamId: streamArchiveStreamId}, true, true);
});
for (const [id, mode] of [["stream_archive_open_download", "download"], ["stream_archive_back_to_player", ""]]) {
  document.getElementById(id).addEventListener("click", event => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigateTo("stream_archives", {streamId: streamArchiveStreamId, mode});
  });
}
document.getElementById("stream_archive_play_pause").addEventListener("click", () => {
  if (streamArchiveIsPlaying) {
    pauseStreamArchive();
  } else {
    playStreamArchive().catch(error => setStreamArchivePlayerResult(`The archived audio couldn't start: ${error.message}`, "error"));
  }
});
document.getElementById("stream_archive_back").addEventListener("click", () => skipStreamArchive(-STREAM_ARCHIVE_SKIP_SECONDS));
document.getElementById("stream_archive_forward").addEventListener("click", () => skipStreamArchive(STREAM_ARCHIVE_SKIP_SECONDS));
document.getElementById("stream_archive_volume").addEventListener("input", event => {
  streamArchivePlaybackVolume = Number(event.target.value);
  document.getElementById("stream_archive_audio").volume = playbackVolumeScalar(streamArchivePlaybackVolume);
});
(() => {
  const slider = document.getElementById("stream_archive_position");
  // The keys are no use on a phone or tablet with only a touch screen, so they're neither
  // shown nor read there; a screen reader reads a description even when it's hidden.
  const touchOnly = window.matchMedia ? window.matchMedia("(hover: none) and (pointer: coarse)") : null;
  const syncKeyHint = () => {
    const hidden = Boolean(touchOnly && touchOnly.matches);
    setHidden("stream_archive_position_hint", hidden);
    if (hidden) slider.removeAttribute("aria-describedby");
    else slider.setAttribute("aria-describedby", "stream_archive_position_hint");
  };
  syncKeyHint();
  if (touchOnly && touchOnly.addEventListener) touchOnly.addEventListener("change", syncKeyHint);
  // Dragging shows the time it's at; letting go goes there.
  slider.addEventListener("input", () => {
    streamArchiveSliderActive = true;
    renderStreamArchivePosition(Number(slider.value), {fromUser: true});
  });
  slider.addEventListener("change", () => {
    streamArchiveSliderActive = false;
    seekStreamArchive(Number(slider.value));
  });
  slider.addEventListener("keydown", event => {
    const steps = {
      ArrowLeft: -STREAM_ARCHIVE_SKIP_SECONDS,
      ArrowDown: -STREAM_ARCHIVE_SKIP_SECONDS,
      ArrowRight: STREAM_ARCHIVE_SKIP_SECONDS,
      ArrowUp: STREAM_ARCHIVE_SKIP_SECONDS,
      PageDown: -STREAM_ARCHIVE_PAGE_SECONDS,
      PageUp: STREAM_ARCHIVE_PAGE_SECONDS
    };
    if (event.key in steps) {
      event.preventDefault();
      skipStreamArchive(steps[event.key]);
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      seekStreamArchive(event.key === "Home" ? 0 : streamArchiveDuration());
    }
  });
  slider.addEventListener("blur", () => {
    streamArchiveSliderActive = false;
  });
})();
(() => {
  const audio = document.getElementById("stream_archive_audio");
  audio.addEventListener("play", () => {
    setStreamArchivePlayerResult("");
    setStreamArchivePlaybackState(true);
  });
  audio.addEventListener("pause", () => setStreamArchivePlaybackState(false));
  audio.addEventListener("loadedmetadata", () => {
    if (Math.abs(audio.currentTime - streamArchiveWantedSeconds) > 0.5) audio.currentTime = streamArchiveWantedSeconds;
  });
  audio.addEventListener("timeupdate", () => {
    if (audio.readyState >= 1 && !audio.seeking) streamArchiveWantedSeconds = audio.currentTime;
    renderStreamArchivePosition(streamArchiveCurrentSeconds());
    if (streamArchiveIsPlaying) updateStreamArchiveMediaSession();
    else updateStreamArchivePositionState();
  });
  audio.addEventListener("ended", () => {
    continueStreamArchive();
  });
  audio.addEventListener("error", () => {
    if (!audio.error || !streamArchivePlayer) return;
    logStreamArchiveEvent("error", {code: audio.error.code, message: audio.error.message || ""});
    // Not while the page is hidden: changing the audio then ends the lock screen's controls.
    if (document.visibilityState === "hidden") {
      streamArchiveNeedsRecovery = true;
      return;
    }
    recoverStreamArchivePlayer();
  });
  // What the audio does around a pause, for finding out what iOS does with it.
  for (const name of ["play", "pause", "abort", "emptied", "stalled", "ended"]) {
    audio.addEventListener(name, () => logStreamArchiveEvent(name));
  }
  document.addEventListener("visibilitychange", () => {
    if (!streamArchivePlayer) return;
    logStreamArchiveEvent(`page ${document.visibilityState}`);
  });
})();
document.getElementById("stream_archive_jump_form").addEventListener("submit", event => {
  event.preventDefault();
  jumpStreamArchive().catch(error => setStreamArchivePlayerResult(error.message, "error"));
});
