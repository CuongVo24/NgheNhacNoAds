"""Spike S3: Windows media keys and the media flyout (SMTC).

    winrt  option A: our own process owns an SMTC session (pywinrt MediaPlayer trick)
    mpv    option B: mpv >= 0.39 provides SMTC itself (--media-controls=yes)

Both are verified through GlobalSystemMediaTransportControlsSessionManager, the
same API the Windows media flyout uses: find our session, read what Windows
shows, then press next/previous/play-pause through it and check we receive them.

    uv run --group spikes python spikes/s3_smtc.py winrt [--wait 30]
    uv run --group spikes python spikes/s3_smtc.py mpv [--mpv PATH] [--silent]

With --wait N the script stays alive N seconds so you can try the real media
keys, headset buttons or the flyout next to the volume indicator.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from typing import Any

from _common import Stopwatch, out_path, save_json, setup_console

TITLE = "Nơi Này Có Anh"
ARTIST = "Sơn Tùng M-TP"
ALBUM = "NgheNhacNoAds spike"


async def find_session(title: str, timeout: float = 5.0) -> Any:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as Manager,
    )

    manager = await Manager.request_async()
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        for session in manager.get_sessions():
            with contextlib.suppress(Exception):
                props = await session.try_get_media_properties_async()
                if props.title and title in props.title:
                    return session
        if asyncio.get_running_loop().time() > deadline:
            return None
        await asyncio.sleep(0.25)


async def describe_session(session: Any) -> dict[str, Any]:
    props = await session.try_get_media_properties_async()
    info = session.get_playback_info()
    controls = info.controls
    return {
        "app_id": session.source_app_user_model_id,
        "title": props.title,
        "artist": props.artist,
        "album": props.album_title,
        "has_thumbnail": props.thumbnail is not None,
        "status": str(info.playback_status),
        "next_enabled": controls.is_next_enabled,
        "previous_enabled": controls.is_previous_enabled,
        "play_pause_enabled": controls.is_play_pause_toggle_enabled,
    }


def thumbnail_url(query: str) -> str | None:
    """Borrow a real YouTube thumbnail so the flyout shows cover art."""
    from yt_dlp import YoutubeDL

    with contextlib.suppress(Exception), YoutubeDL(
        {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist"}
    ) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        entry = (info or {}).get("entries", [None])[0] or {}
        thumbs = entry.get("thumbnails") or []
        return thumbs[-1]["url"] if thumbs else None
    return None


async def spike_winrt(wait: float) -> dict[str, Any]:
    from winrt.windows.foundation import Uri
    from winrt.windows.media import MediaPlaybackStatus, MediaPlaybackType
    from winrt.windows.media.playback import MediaPlayer
    from winrt.windows.storage.streams import RandomAccessStreamReference

    loop = asyncio.get_running_loop()
    pressed: asyncio.Queue[str] = asyncio.Queue()
    result: dict[str, Any] = {}

    player = MediaPlayer()
    player.command_manager.is_enabled = False  # we drive SMTC ourselves
    smtc = player.system_media_transport_controls
    smtc.is_enabled = True
    smtc.is_play_enabled = True
    smtc.is_pause_enabled = True
    smtc.is_next_enabled = True
    smtc.is_previous_enabled = True

    def on_button(_sender: Any, args: Any) -> None:
        name = getattr(args.button, "name", str(args.button))
        loop.call_soon_threadsafe(pressed.put_nowait, name)

    token = smtc.add_button_pressed(on_button)
    updater = smtc.display_updater
    updater.type = MediaPlaybackType.MUSIC
    updater.music_properties.title = TITLE
    updater.music_properties.artist = ARTIST
    updater.music_properties.album_title = ALBUM
    thumb = await asyncio.to_thread(thumbnail_url, f"{ARTIST} {TITLE}")
    if thumb:
        updater.thumbnail = RandomAccessStreamReference.create_from_uri(Uri(thumb))
    updater.update()
    smtc.playback_status = MediaPlaybackStatus.PLAYING
    result["thumbnail_source"] = "youtube" if thumb else None

    try:
        sw = Stopwatch()
        session = await find_session(TITLE)
        result["session_found_ms"] = sw.ms() if session else None
        if session is None:
            result["error"] = "our SMTC session did not show up"
            return result
        result["seen_by_windows"] = await describe_session(session)
        for label, action in (
            ("next", session.try_skip_next_async),
            ("previous", session.try_skip_previous_async),
            ("toggle_play_pause", session.try_toggle_play_pause_async),
        ):
            accepted = await action()
            try:
                button = await asyncio.wait_for(pressed.get(), 3)
            except TimeoutError:
                button = None
            result[f"press_{label}"] = {"accepted": accepted, "we_received": button}
        if wait > 0:
            print(f"Đang giữ phiên SMTC {wait:.0f} giây: thử phím media thật hoặc bảng media...")
            end = loop.time() + wait
            manual: list[str] = []
            while (remaining := end - loop.time()) > 0:
                with contextlib.suppress(TimeoutError):
                    button = await asyncio.wait_for(pressed.get(), remaining)
                    manual.append(button)
                    print(f"  nhận nút: {button}")
            result["manual_presses"] = manual
    finally:
        smtc.remove_button_pressed(token)
        smtc.is_enabled = False
    return result


async def spike_mpv(mpv_path: str, silent: bool, wait: float) -> dict[str, Any]:
    from _mpv import Mpv, find_mpv, mpv_version
    from s1_ytdlp_mpv import resolve_query

    mpv = find_mpv(mpv_path)
    if mpv is None:
        return {"error": f"mpv not found ({mpv_path})"}
    result: dict[str, Any] = {"mpv_version": mpv_version(mpv)}
    input_conf = out_path("s3-mpv-input.conf")
    input_conf.write_text(
        "PLAYPAUSE cycle pause\n"
        "PLAY set pause no\n"
        "PAUSE set pause yes\n"
        "NEXT script-message nghenhacnoads next\n"
        "PREV script-message nghenhacnoads prev\n"
        "STOP script-message nghenhacnoads stop\n",
        encoding="utf-8",
    )
    extra = ["--media-controls=yes", f"--input-conf={input_conf}"]
    if silent:
        extra.append("--ao=null")
    stream = await asyncio.to_thread(resolve_query, f"{ARTIST} {TITLE}", "node", None)
    player = Mpv(mpv, extra)
    await player.start()
    try:
        await player.command(
            {
                "name": "loadfile",
                "url": stream["url"],
                "flags": "replace",
                "options": {
                    "user-agent": stream["user_agent"],
                    "force-media-title": f"{TITLE} — {ARTIST}",
                },
            }
        )
        await player.wait_event("playback-restart", 30)
        session = await find_session(TITLE)
        if session is None:
            result["error"] = "mpv SMTC session did not show up"
            return result
        result["seen_by_windows_one_file"] = await describe_session(session)
        # A second playlist entry: does the flyout enable "next" then?
        await player.command({"name": "loadfile", "url": stream["url"], "flags": "append"})
        await asyncio.sleep(1)
        result["seen_by_windows_two_files"] = await describe_session(session)
        since = len(player.events)
        result["press_next_accepted"] = await session.try_skip_next_async()
        try:
            _, event = await player.wait_event("client-message", 3, since=since)
            result["press_next_mpv_event"] = event.get("args")
        except TimeoutError:
            result["press_next_mpv_event"] = None
            result["playlist_pos_after_next"] = await player.get("playlist-pos")
        paused_before = await player.get("pause")
        result["press_toggle_accepted"] = await session.try_toggle_play_pause_async()
        await asyncio.sleep(1)
        result["pause_toggled"] = (await player.get("pause")) != paused_before
        if wait > 0:
            print(f"Giữ mpv {wait:.0f} giây để thử phím media thật...")
            await asyncio.sleep(wait)
    finally:
        await player.quit()
    return result


def main() -> None:
    setup_console()
    if sys.platform != "win32":
        raise SystemExit("S3 only applies to Windows.")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_winrt = sub.add_parser("winrt")
    p_winrt.add_argument("--wait", type=float, default=0)
    p_mpv = sub.add_parser("mpv")
    p_mpv.add_argument("--mpv", default="mpv")
    p_mpv.add_argument("--silent", action="store_true", help="use --ao=null (no sound)")
    p_mpv.add_argument("--wait", type=float, default=0)
    args = parser.parse_args()

    if args.command == "winrt":
        result = asyncio.run(spike_winrt(args.wait))
    else:
        result = asyncio.run(spike_mpv(args.mpv, args.silent, args.wait))
    for key, value in result.items():
        print(f"{key}: {value}")
    print(f"Đã lưu: {save_json(f's3_{args.command}.json', result)}")


if __name__ == "__main__":
    main()
