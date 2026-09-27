"""Spike S4: YouTube Music search, radio and lyrics without login, plus LRCLIB.

For each query: search YouTube Music (songs), look for lyrics on YouTube Music
(timed if possible), look for lyrics on LRCLIB, and for the first few songs
fetch the radio playlist that autoplay would use. Only counts and metadata are
printed; lyric text is never printed or saved.

    uv run --group spikes python spikes/s4_ytmusic_lyrics.py [--radio 5] [--location VN]
"""

from __future__ import annotations

import argparse
import time
from typing import Any

import httpx
from _common import USER_AGENT, Stopwatch, md_table, median, save_json, setup_console
from ytmusicapi import YTMusic

QUERIES = [
    "Sơn Tùng M-TP Nơi Này Có Anh",
    "Sơn Tùng M-TP Lạc Trôi",
    "Sơn Tùng M-TP Chúng Ta Không Thuộc Về Nhau",
    "Đen Bài Này Chill Phết",
    "Đen Đi Về Nhà",
    "Hoàng Dũng Nàng Thơ",
    "Hà Anh Tuấn Tháng Tư Là Lời Nói Dối Của Em",
    "Hoàng Thùy Linh See Tình",
    "MONO Waiting For You",
    "Vũ. Lạ Lùng",
    "Ed Sheeran Shape of You",
    "The Weeknd Blinding Lights",
    "Adele Someone Like You",
    "Queen Bohemian Rhapsody",
    "Eagles Hotel California",
    "Imagine Dragons Believer",
    "Dua Lipa Levitating",
    "Coldplay Viva La Vida",
    "The Beatles Yesterday",
    "Taylor Swift Anti-Hero",
]
LRCLIB = "https://lrclib.net/api"
PAUSE_S = 0.3  # be gentle with public services


def ytm_lyrics(yt: YTMusic, video_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "none"}
    sw = Stopwatch()
    watch = yt.get_watch_playlist(videoId=video_id, limit=5)
    browse_id = watch.get("lyrics")
    if not browse_id:
        result["ms"] = sw.ms()
        return result
    lyrics = yt.get_lyrics(browse_id, timestamps=True)
    result["ms"] = sw.ms()
    if not lyrics or not lyrics.get("lyrics"):
        return result
    timed = bool(lyrics.get("hasTimestamps"))
    body = lyrics["lyrics"]
    result.update(
        status="timed" if timed else "plain",
        lines=len(body) if timed else len(str(body).splitlines()),
        source=(lyrics.get("source") or "").replace("Source: ", ""),
    )
    return result


def lrclib_lyrics(client: httpx.Client, song: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {"track_name": song["title"], "artist_name": song["artist"]}
    if song.get("album"):
        params["album_name"] = song["album"]
    if song.get("duration"):
        params["duration"] = song["duration"]
    sw = Stopwatch()
    hit: dict[str, Any] | None = None
    via = "get"
    response = client.get(f"{LRCLIB}/get", params=params)
    if response.status_code == 200:
        hit = response.json()
    else:
        via = "search"
        time.sleep(PAUSE_S)
        response = client.get(
            f"{LRCLIB}/search",
            params={"track_name": song["title"], "artist_name": song["artist"]},
        )
        response.raise_for_status()
        candidates = response.json()
        if song.get("duration"):
            candidates = [
                c
                for c in candidates
                if c.get("duration") and abs(c["duration"] - song["duration"]) <= 3
            ]
        hit = next((c for c in candidates if c.get("syncedLyrics")), None) or next(
            iter(candidates), None
        )
    result: dict[str, Any] = {"ms": sw.ms(), "via": via, "status": "none"}
    if hit:
        if hit.get("syncedLyrics"):
            result["status"] = "synced"
        elif hit.get("plainLyrics"):
            result["status"] = "plain"
        elif hit.get("instrumental"):
            result["status"] = "instrumental"
    return result


def main() -> None:
    setup_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--radio", type=int, default=5, help="fetch radio for the first N songs")
    parser.add_argument("--location", default="", help="YouTube Music location, e.g. VN")
    args = parser.parse_args()

    yt = YTMusic(location=args.location) if args.location else YTMusic()
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15, follow_redirects=True)
    records: list[dict[str, Any]] = []

    for index, query in enumerate(QUERIES):
        record: dict[str, Any] = {"query": query}
        records.append(record)
        try:
            sw = Stopwatch()
            results = yt.search(query, filter="songs", limit=5)
            record["search_ms"] = sw.ms()
            if not results:
                record["error"] = "no search results"
                continue
            top = results[0]
            song = {
                "video_id": top.get("videoId"),
                "title": top.get("title"),
                "artist": ", ".join(a["name"] for a in top.get("artists") or []),
                "album": (top.get("album") or {}).get("name"),
                "duration": top.get("duration_seconds"),
            }
            record["song"] = song
            time.sleep(PAUSE_S)
            record["ytm_lyrics"] = ytm_lyrics(yt, song["video_id"])
            time.sleep(PAUSE_S)
            record["lrclib"] = lrclib_lyrics(client, song)
            if index < args.radio:
                time.sleep(PAUSE_S)
                sw = Stopwatch()
                radio = yt.get_watch_playlist(videoId=song["video_id"], radio=True, limit=25)
                tracks = radio.get("tracks") or []
                record["radio"] = {
                    "ms": sw.ms(),
                    "tracks": len(tracks),
                    "playable": sum(1 for t in tracks if t.get("videoId")),
                    "sample": [
                        f"{t.get('title')} — {', '.join(a['name'] for a in t.get('artists') or [])}"
                        for t in tracks[1:4]
                    ],
                }
        except Exception as exc:  # a spike should report, not crash
            record["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[{index + 1:2}/{len(QUERIES)}] {query}: done", flush=True)
        time.sleep(PAUSE_S)

    rows = []
    for i, r in enumerate(records, 1):
        song = r.get("song") or {}
        rows.append(
            [
                i,
                r["query"],
                f"{song.get('title')} — {song.get('artist')}" if song else r.get("error"),
                (r.get("ytm_lyrics") or {}).get("status", "-"),
                (r.get("lrclib") or {}).get("status", "-"),
                (r.get("radio") or {}).get("tracks", ""),
            ]
        )
    print()
    print(md_table(["#", "Truy vấn", "Kết quả YT Music", "Lời YTM", "LRCLIB", "Radio"], rows))

    def count(key: str, *statuses: str) -> int:
        return sum(1 for r in records if (r.get(key) or {}).get("status") in statuses)

    timed_any = sum(
        1
        for r in records
        if (r.get("ytm_lyrics") or {}).get("status") == "timed"
        or (r.get("lrclib") or {}).get("status") == "synced"
    )
    any_lyrics = sum(
        1
        for r in records
        if (r.get("ytm_lyrics") or {}).get("status") in ("timed", "plain")
        or (r.get("lrclib") or {}).get("status") in ("synced", "plain")
    )
    summary = {
        "songs": len(records),
        "search_hits": sum(1 for r in records if r.get("song")),
        "errors": [r["error"] for r in records if r.get("error")],
        "ytm_timed": count("ytm_lyrics", "timed"),
        "ytm_plain": count("ytm_lyrics", "plain"),
        "lrclib_synced": count("lrclib", "synced"),
        "lrclib_plain": count("lrclib", "plain"),
        "timed_from_any_source": timed_any,
        "any_lyrics": any_lyrics,
        "median_ms": {
            "ytm_search": median([r["search_ms"] for r in records if "search_ms" in r]),
            "ytm_lyrics": median(
                [r["ytm_lyrics"]["ms"] for r in records if "ms" in (r.get("ytm_lyrics") or {})]
            ),
            "lrclib": median([r["lrclib"]["ms"] for r in records if "ms" in (r.get("lrclib") or {})]),
            "radio": median([r["radio"]["ms"] for r in records if "radio" in r]),
        },
    }
    print()
    for key, value in summary.items():
        print(f"{key}: {value}")
    path = save_json("s4_results.json", {"summary": summary, "records": records})
    print(f"\nĐã lưu: {path}")


if __name__ == "__main__":
    main()
