"""Playlist of the chosen IPTV channels for Kodi (PVR IPTV Simple Client).

The channels left visible on the settings page, in the catalog order and numbered from 1, then the hidden
versions of these channels kept for the replay (FHD of a 4K channel), each with the guide id (tvg-id) of
the public guide and Xtream catch-up (catchup="xc"). The stream addresses carry the provider login: the
playlist is only served to Kodi's address.
"""

CATCHUP_DAYS = 7


def esc(text: str) -> str:
    return (text or "").replace('"', "'").replace("\n", " ").strip()


def build_m3u(lineup: list[tuple[str, dict]], ids: dict[int, str], stream_base: str, epg_url: str = "") -> str:
    head = f'#EXTM3U x-tvg-url="{epg_url}"' if epg_url else "#EXTM3U"
    lines = [head]
    for number, (group, ch) in enumerate(lineup, 1):
        lines.append(
            f'#EXTINF:-1 tvg-id="{esc(ids.get(ch["id"], ""))}" tvg-name="{esc(ch["name"])}" '
            f'tvg-logo="{esc(ch.get("logo", ""))}" tvg-chno="{number}" group-title="{esc(group)}" '
            f'catchup="xc" catchup-days="{CATCHUP_DAYS}",{esc(ch["name"])}'
        )
        lines.append(f"{stream_base.rstrip('/')}/{ch['id']}")
    return "\n".join(lines) + "\n"
