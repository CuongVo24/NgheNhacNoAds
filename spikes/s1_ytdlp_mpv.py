"""Spike S1: resolve YouTube audio with yt-dlp (as a library) and play it with mpv.

    resolve   search + resolve a few songs, measure latency, show the chosen format
    play      resolve one song and play it through mpv's JSON IPC, testing controls

    uv run --group spikes python spikes/s1_ytdlp_mpv.py resolve --runtime node
    uv run --group spikes python spikes/s1_ytdlp_mpv.py play --query "..." [--silent]

--runtime picks the JavaScript runtime yt-dlp uses for YouTube's JS challenges
(deno is yt-dlp's default; node needs to be enabled explicitly).
"""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from typing import Any
from urllib.parse import urlparse

from _common import Stopwatch, md_table, out_path, save_json, setup_console
from yt_dlp import YoutubeDL

QUERIES = [
    "Sơn Tùng M-TP Nơi Này Có Anh",
    "Đen Bài Này Chill Phết",
    "Hoàng Dũng Nàng Thơ",
    "Ed Sheeran Shape of You",
    "lofi hip hop radio beats to relax study to",  # usually a livestream
]
AUDIO_FORMAT = "bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio/best"
EXPIRE_RE = re.compile(r"(?:[?&]expire=|/expire/)(\d+)")


class CollectingLogger:
    """Keeps yt-dlp warnings and errors so the spike can report them."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def error(self, msg: str) -> None:
        self.errors.append(msg)


def base_options(runtime: str, runtime_path: str | None, logger: CollectingLogger) -> dict[str, Any]:
    config: dict[str, Any] = {"path": runtime_path} if runtime_path else {}
    return {
        "quiet": True,
        "noprogress": True,
        "skip_download": True,
        "socket_timeout": 15,
        "extractor_retries": 2,
        "cachedir": str(out_path("yt-dlp-cache")),
        "js_runtimes": {runtime: config},
        "logger": logger,
    }


def search(query: str, options: dict[str, Any]) -> dict[str, Any]:
    with YoutubeDL({**options, "extract_flat": "in_playlist"}) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    entries = (info or {}).get("entries") or []
    if not entries:
        raise LookupError(f"no results for {query!r}")
    return entries[0]


def resolve(video_id: str, options: dict[str, Any]) -> dict[str, Any]:
    with YoutubeDL({**options, "format": AUDIO_FORMAT, "noplaylist": True}) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    assert info is not None
    url = info["url"]
    headers = info.get("http_headers") or {}
    match = EXPIRE_RE.search(url)
    expires_in_h = round((int(match.group(1)) - time.time()) / 3600, 1) if match else None
    return {
        "id": video_id,
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "music_track": info.get("track"),
        "music_artist": info.get("artist") or info.get("creator"),
        "duration": info.get("duration"),
        "is_live": bool(info.get("is_live")),
        "format_id": info.get("format_id"),
        "ext": info.get("ext"),
        "acodec": info.get("acodec"),
        "abr": info.get("abr"),
        "protocol": info.get("protocol"),
        "host": urlparse(url).hostname,
        "expires_in_h": expires_in_h,
        "header_names": sorted(headers),
        "user_agent": headers.get("User-Agent", ""),
        "url": url,  # never printed or saved: it is signed and tied to this IP
    }


def resolve_query(query: str, runtime: str, runtime_path: str | None) -> dict[str, Any]:
    logger = CollectingLogger()
    options = base_options(runtime, runtime_path, logger)
    return resolve(search(query, options)["id"], options)


# ---------------------------------------------------------------------- resolve


def run_resolve(args: argparse.Namespace) -> None:
    logger = CollectingLogger()
    options = base_options(args.runtime, args.runtime_path, logger)
    records: list[dict[str, Any]] = []
    for query in args.query or QUERIES:
        record: dict[str, Any] = {"query": query}
        try:
            sw = Stopwatch()
            entry = search(query, options)
            record["search_ms"] = sw.ms()
            sw = Stopwatch()
            record.update(resolve(entry["id"], options))
            record["resolve_ms"] = sw.ms()
            sw = Stopwatch()
            resolve(entry["id"], options)  # same video again: warm caches
            record["resolve_again_ms"] = sw.ms()
        except Exception as exc:  # report, don't crash
            record["error"] = f"{type(exc).__name__}: {exc}"
        record.pop("url", None)
        records.append(record)
        print(f"- {query}: {record.get('error') or 'ok'}", flush=True)

    rows = [
        [
            r["query"],
            r.get("title", r.get("error")),
            f"{r.get('format_id')} {r.get('acodec')} {r.get('abr') or ''}".strip(),
            r.get("protocol"),
            "live" if r.get("is_live") else r.get("expires_in_h"),
            r.get("search_ms"),
            r.get("resolve_ms"),
            r.get("resolve_again_ms"),
        ]
        for r in records
    ]
    print()
    print(
        md_table(
            [
                "Truy vấn",
                "Video",
                "Định dạng",
                "Giao thức",
                "Hết hạn sau (giờ)",
                "Tìm (ms)",
                "Phân giải (ms)",
                "Lần 2 (ms)",
            ],
            rows,
        )
    )
    print("\nCảnh báo của yt-dlp:", *(logger.warnings or ["(không có)"]), sep="\n  ")
    result = {
        "runtime": args.runtime,
        "records": records,
        "warnings": logger.warnings,
        "errors": logger.errors,
    }
    print(f"\nĐã lưu: {save_json(f's1_resolve_{args.runtime}.json', result)}")


# ------------------------------------------------------------------------- play


async def run_play(args: argparse.Namespace) -> dict[str, Any]:
    from _mpv import Mpv, find_mpv, mpv_version

    mpv = find_mpv(args.mpv)
    if mpv is None:
        raise SystemExit(f"mpv not found: {args.mpv}")
    result: dict[str, Any] = {"mpv_version": mpv_version(mpv), "runtime": args.runtime}

    sw = Stopwatch()
    stream = await asyncio.to_thread(resolve_query, args.query, args.runtime, args.runtime_path)
    result["resolve_ms"] = sw.ms()
    result["stream"] = {k: v for k, v in stream.items() if k not in ("url", "user_agent")}

    extra = ["--ao=null"] if args.silent else []
    extra += [f"--volume={args.volume}", f"--log-file={out_path('s1-mpv.log')}"]
    player = Mpv(mpv, extra)
    sw = Stopwatch()
    await player.start()
    result["mpv_start_ms"] = sw.ms()
    try:
        for index, prop in enumerate(("pause", "time-pos", "duration", "paused-for-cache"), 1):
            await player.command(["observe_property", index, prop])
        sw = Stopwatch()
        await player.command(
            {
                "name": "loadfile",
                "url": stream["url"],
                "flags": "replace",
                "options": {
                    "user-agent": stream["user_agent"],
                    "force-media-title": f"{stream['title']}",
                },
            }
        )
        await player.wait_event("file-loaded", 30)
        result["loadfile_to_file_loaded_ms"] = sw.ms()
        await player.wait_event("playback-restart", 30)
        result["loadfile_to_audio_ms"] = sw.ms()
        result["time_to_audio_total_ms"] = result["resolve_ms"] + result["loadfile_to_audio_ms"]
        result["media_title"] = await player.get("media-title")
        result["audio_codec"] = await player.get("audio-codec-name")
        result["audio_output"] = await player.get("current-ao")

        start = await player.wait_property("time-pos", lambda v: v is not None and v > 1, 15)
        await asyncio.sleep(2)
        result["position_advances"] = (await player.get("time-pos")) > start + 1

        await player.set("pause", True)
        await asyncio.sleep(1)
        paused_at = await player.get("time-pos")
        await asyncio.sleep(1.5)
        result["pause_holds_position"] = abs((await player.get("time-pos")) - paused_at) < 0.2
        await player.set("pause", False)

        if not stream["is_live"]:
            before = await player.get("time-pos")
            await player.command(["seek", 30, "relative"])
            after = await player.wait_property("time-pos", lambda v: v and v > before + 25, 10)
            result["seek_plus_30s"] = round(after - before, 1)

        await player.set("volume", 50)
        result["volume_set"] = await player.get("volume")

        since = len(player.events)
        await player.command(["script-message", "nghenhacnoads", "ping"])
        _, event = await player.wait_event("client-message", 5, since=since)
        result["script_message_roundtrip"] = event.get("args")

        if args.seconds > 0:
            print(f"Đang phát thêm {args.seconds} giây...")
            await asyncio.sleep(args.seconds)
        result["cache_stalls"] = sum(
            1
            for _, e in player.events
            if e.get("event") == "property-change"
            and e.get("name") == "paused-for-cache"
            and e.get("data") is True
        )
    finally:
        await player.quit()
    return result


def main() -> None:
    setup_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", default="deno", choices=["deno", "node", "quickjs", "bun"])
    parser.add_argument("--runtime-path", help="path to the runtime executable")
    sub = parser.add_subparsers(dest="command", required=True)
    p_resolve = sub.add_parser("resolve")
    p_resolve.add_argument("--query", action="append", help="repeatable; default: built-in list")
    p_play = sub.add_parser("play")
    p_play.add_argument("--query", default=QUERIES[0])
    p_play.add_argument("--mpv", default="mpv")
    p_play.add_argument("--seconds", type=int, default=10, help="keep playing this long at the end")
    p_play.add_argument("--silent", action="store_true", help="use --ao=null (no sound)")
    p_play.add_argument("--volume", type=int, default=30, help="starting volume 0-100")
    args = parser.parse_args()

    if args.command == "resolve":
        run_resolve(args)
    else:
        result = asyncio.run(run_play(args))
        for key, value in result.items():
            print(f"{key}: {value}")
        print(f"Đã lưu: {save_json(f's1_play_{args.runtime}.json', result)}")


if __name__ == "__main__":
    main()
