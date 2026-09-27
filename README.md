# NgheNhacNoAds

Ad-free YouTube / YouTube Music player for the terminal, with your phone as the remote over SSH.

[Tiếng Việt](README.vi.md)

> **Status:** early development, milestone M0 (technical spikes). There is nothing to install yet.

## What it will do

- Search YouTube Music (default) or YouTube and play audio only, with no ads and no browser.
- Run as a full-screen TUI on the laptop.
- Let you SSH in from an Android phone (Termux, Termius, JuiceSSH) to control playback. Audio always plays on the laptop.
- Local playlists, favorites, listening history, autoplay radio, synced lyrics, and Windows media keys.
- Windows first, Linux next.

## How it works

- A background daemon on the laptop uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) (as a Python library) to find each song's audio stream, and [mpv](https://mpv.io/) to play it.
- The TUI on the laptop talks to the daemon over localhost.
- An SSH server built into the daemon ([asyncssh](https://github.com/ronf/asyncssh)) serves the same TUI to your phone. It only accepts connections from your LAN, and only from keys paired with a one-time code.

## Documentation

- [Technical specification](docs/SPEC.md) (Vietnamese)
- [Spike notes for milestone M0](docs/spikes/README.md)

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync
uv run pytest
```

## Disclaimer

This project uses yt-dlp to access YouTube content outside the official player, which may violate YouTube's Terms of Service. It is meant for personal and educational use. Do not download or redistribute content. This project is not affiliated with YouTube or Google.

## License

[MIT](LICENSE)
