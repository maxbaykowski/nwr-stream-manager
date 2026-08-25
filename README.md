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
- `multimon-ng` for EAS/SAME decoding support
- ALSA runtime/development libraries for optional soundcard output support
- `libogg`, `libvorbis`, and `libvorbisenc` runtime libraries for OGG streaming
- `libopus` runtime libraries for browser stream monitoring and the weather
  radio receiver

On x86_64 systems, the Python dependency set may install `pyrtlsdrlib`. On ARM
systems such as Raspberry Pi, use the distribution-provided `librtlsdr` package
or another system-installed `librtlsdr`; `pyrtlsdrlib` is intentionally not
required there.

Then install the Python package directly from GitHub:

```bash
python3 -m pip install 'nwr-stream-manager @ git+https://github.com/maxbaykowski/nwr-stream-manager.git@testing'
```

Run the web interface:

```bash
nwr-stream-manager --host 0.0.0.0 --port 8080
```

The web interface will print the local and LAN URLs it is listening on.

## First-run setup and accounts

When no account database exists, the web interface starts in setup mode and asks
you to create the first account. That first account is the owner account.

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
- `ExecStart=/usr/local/bin/nwr-stream-manager --host 0.0.0.0 --port 8080`.
- `SupplementaryGroups=plugdev audio`, so the service user can access common
  RTL-SDR udev group permissions and ALSA playback devices.

If your Python package manager installs console scripts somewhere else, edit the
`ExecStart` path before enabling the service. You can inspect logs with:

```bash
journalctl -u nwr-stream-manager
```
