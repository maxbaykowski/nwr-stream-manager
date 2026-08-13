# nwr-stream-manager
The new and improved utility for streaming your local NOAA Weather Radio Station to online streaming services!

## Installation from GitHub

Install the system packages first. Package names vary by distribution, but you
need:

- `librtlsdr` and its command-line tools/udev rules for RTL-SDR access
- `multimon-ng` for EAS/SAME decoding support
- PortAudio development/runtime libraries for soundcard output
- Ogg/Vorbis development/runtime libraries for OGG streaming

Then install the Python package directly from GitHub:

```bash
python3 -m pip install 'nwr-stream-manager @ git+https://github.com/maxbaykowski/nwr-stream-manager.git@testing'
```

Run the web interface:

```bash
nwr-stream-manager --host 0.0.0.0 --port 8080
```

The web interface will print the local and LAN URLs it is listening on.

## Running under systemd

An example system service is provided at
`packaging/systemd/nwr-stream-manager.service`. It is intended to run as a
dedicated non-root user and to let systemd create the persistent state and log
directories.

Create the service user:

```bash
sudo useradd --system --home /var/lib/nwr-stream-manager --shell /usr/sbin/nologin nwr-stream-manager
```

Give that user access to the RTL-SDR device. The exact group depends on your
distribution and udev rules; many systems use `plugdev`:

```bash
sudo usermod -aG plugdev nwr-stream-manager
```

Install and start the example service:

```bash
sudo cp packaging/systemd/nwr-stream-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nwr-stream-manager
```

The example unit uses:

- `StateDirectory=nwr-stream-manager`, so persistent settings, stream configs,
  and EAS alert indexes/audio are under `/var/lib/nwr-stream-manager`.
- `LogsDirectory=nwr-stream-manager`, so the rotating server log is
  `/var/log/nwr-stream-manager/nwr-stream-manager.log`.
- `ExecStart=/usr/local/bin/nwr-stream-manager --host 0.0.0.0 --port 8080`.

If your Python package manager installs console scripts somewhere else, edit the
`ExecStart` path before enabling the service. You can inspect logs with:

```bash
journalctl -u nwr-stream-manager
```
