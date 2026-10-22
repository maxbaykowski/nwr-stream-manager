# nwr-stream-manager
The new and improved utility for streaming your local NOAA Weather Radio Station to online streaming services!

NWR Stream Manager is managed from a web interface. It controls one RTL-SDR
tuned to the NOAA Weather Radio band, runs configured NWR streams, can stream to
Icecast-compatible services, records EAS alerts, supports browser monitoring,
and stores its persistent configuration locally.

## Installation from GitHub

Install the system packages first. Package names vary by distribution, but you
need:

- `librtlsdr` and its command-line tools/udev rules for RTL-SDR access
  (`librtlsdr0` and `rtl-sdr` on Debian and Raspberry Pi OS, `rtl-sdr` on Fedora)
- `multimon-ng` for EAS/SAME decoding support
- ALSA runtime/development libraries for optional soundcard output support
- `libogg`, `libvorbis`, and `libvorbisenc` runtime libraries for OGG streaming
- `libmp3lame` runtime library for MP3 streaming (`libmp3lame0` on Debian and
  Raspberry Pi OS, `lame-libs` on Fedora)
- `libopus` runtime libraries for browser stream monitoring and the weather
  radio receiver
- `liquid-dsp` runtime library for signal processing (`libliquid1` on Debian and
  Raspberry Pi OS, `liquid-dsp` on Fedora)
- `libsoxr` runtime library for sample-rate conversion (`libsoxr0` on Debian and
  Raspberry Pi OS, `soxr` on Fedora)
- `libsndfile` runtime library for custom fallback announcements (`libsndfile1`
  on Debian and Raspberry Pi OS, `libsndfile` on Fedora)

Then install the Python package directly from GitHub:

```bash
python3 -m pip install 'nwr-stream-manager @ git+https://github.com/maxbaykowski/nwr-stream-manager.git@testing'
```

Run the web interface:

```bash
nwr-stream-manager
```

The web interface will print the local and LAN URLs it is listening on. It
listens on port 8080 on every network address unless you change that in
Settings, under Advanced; changes there take effect the next time it starts.

These options override the Advanced settings for as long as they're given:

- `--port PORT` listens on another port.
- `--localhost-only` only accepts connections from the same computer, for use
  behind a reverse proxy or Tailscale Serve.
- `--bind-to-all-interfaces` accepts connections on every network address. If
  you've locked yourself out with the Advanced settings, start with this (and
  `--port` if needed) to get back in.
- `--host ADDRESS` listens on one particular address.

## Browser monitoring notes

NWR Stream Manager can monitor streams and the weather radio receiver directly
in the browser using low-latency Opus audio over a WebSocket transport. This
path provides more predictable latency than WebRTC and supports Media Session
controls on supported browsers.

Background playback for stream monitoring and the weather radio receiver is
supported on Android browsers that keep web media sessions active in the
background. Background playback for those realtime features is not supported on
iPhone or iPad: iOS may stop the WebSocket/Web Audio playback path when Safari
is backgrounded. Reopen Safari and press Play in NWR Stream Manager to resume
listening.

## Development UI layout checks

The repository includes a Playwright-based layout smoke test for checking common
phone, tablet, and desktop viewport sizes. It is intended for development only
and is not part of the runtime dependency set.

Install the development tools and Chromium browser support:

```bash
python3 -m pip install -e '.[dev]'
python3 -m playwright install chromium
```

Start NWR Stream Manager, then run:

```bash
NWRSM_LAYOUT_USER=owner NWRSM_LAYOUT_PASSWORD=password \
  python3 tools/check_ui_layout.py --url http://127.0.0.1:8080
```

The checker visits the dashboard, RTL-SDR configuration, streams, add-stream
wizard, receiver, I/Q recorder, new I/Q recording, EAS alerts, EAS export/delete,
and logs pages across several viewport sizes. When credentials are supplied, it
also discovers configured streams, stream settings tabs, editable outputs, EAS
alert detail pages, and I/Q recording download pages through the web API. It
fails if pages overflow horizontally, controls overlap, controls are too small,
or the receiver previous/play/next buttons wrap or become different sizes.

To save screenshots for review:

```bash
python3 tools/check_ui_layout.py --url http://127.0.0.1:8080 --screenshots /tmp/nwr-layout
```

## First-run setup and accounts

When no account database exists, the web interface starts in setup mode and asks
you to create the first account. That first account is the owner account.

Setup has to be done directly, from the computer itself or the local network.
Requests that arrive through a tunnel or reverse proxy (anything that adds headers
such as `X-Forwarded-For`, `Forwarded`, `X-Real-IP`, `CF-Connecting-IP` or
`Tailscale-User-Login`) are shown a page saying so, and can't create the owner
account. Once it exists, signing in through a tunnel works as usual.

When a tunnel or proxy reports that the browser connected over HTTPS
(`X-Forwarded-Proto: https`, `Forwarded: proto=https`, or Cloudflare's
`CF-Visitor`), sign-in cookies are marked `Secure` and an HSTS header
(`Strict-Transport-Security: max-age=31536000`) is sent. Browsers then use HTTPS
for that address for a year, so don't later serve the same address over plain
HTTP. Plain HTTP on the local network is unaffected.

Account types:

- Owner: the first account created. It can manage all accounts.
- Administrator: can configure SDR settings, streams, outputs, EAS recording,
  audio effects, and I/Q recordings. It cannot manage accounts.
- Read-only: can view streams, monitor streams, use the weather radio receiver,
  view/export EAS alerts, view/download I/Q recordings, and view logs. It cannot
  change server configuration or delete recordings/alerts.

Passwords are stored in `accounts.db` using versioned `scrypt` hashes. Browser
sessions are cached so the password hash is not recalculated for every API
request. If the owner password is lost, stop the service and delete
`accounts.db`; the setup page will appear again on the next start.

Default state location when running manually:

```text
~/.local/state/nwr-stream-manager/
```

That directory contains settings, stream configuration files, EAS alert indexes
and audio, I/Q recording metadata/audio, and `accounts.db`.

## User install troubleshooting

For a user install, pip must be able to write to `~/.local` and its Python
site-packages directory. If a previous `sudo pip` command made those paths
root-owned, repair them before installing:

```bash
sudo chown -R "$USER:$USER" ~/.local ~/.cache/pip
```

If a local source checkout fails while copying files under `build/lib`, remove
the build artifacts and try again:

```bash
rm -rf build dist nwr_stream_manager.egg-info
python3 -m pip install --user .
```

## Running under systemd

An example system service is provided at
`packaging/systemd/nwr-stream-manager.service`. It is intended to run as a
dedicated non-root user and to let systemd create the persistent state and log
directories.

Create the service user:

```bash
sudo useradd --system --home /var/lib/nwr-stream-manager --shell /usr/sbin/nologin nwr-stream-manager
```

Give that user access to the RTL-SDR device and ALSA sound cards. The exact
groups depend on your distribution and udev rules; many systems use `plugdev`
for USB device access and `audio` for sound card playback:

```bash
sudo usermod -aG plugdev,audio nwr-stream-manager
```

Install and start the example service:

```bash
sudo cp packaging/systemd/nwr-stream-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nwr-stream-manager
```

The example unit uses:

- `StateDirectory=nwr-stream-manager`, so persistent settings, stream configs,
  account database, EAS alert indexes/audio, and I/Q recording metadata/files
  are under `/var/lib/nwr-stream-manager`.
- `LogsDirectory=nwr-stream-manager`, so the rotating server log is
  `/var/log/nwr-stream-manager/nwr-stream-manager.log`.
- `ExecStart=/usr/local/bin/nwr-stream-manager`, so the port and address come
  from Settings, under Advanced. A unit that still passes `--host` or `--port`
  overrides those settings; remove them to use Advanced.
- `SupplementaryGroups=plugdev audio`, so the service user can access common
  RTL-SDR udev group permissions and ALSA playback devices.

If your Python package manager installs console scripts somewhere else, edit the
`ExecStart` path before enabling the service. You can inspect logs with:

```bash
journalctl -u nwr-stream-manager
```
