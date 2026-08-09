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
