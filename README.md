# IPTV (Kodi) — Unfolded Circle Remote integration

Browse the IPTV channels of an Xtream codes provider by category on the Remote 3, with logos and the TV
guide, and watch them live or in replay in **Kodi** (PVR IPTV Simple Client) on an Android TV box (NVIDIA
Shield). Runs on the Remote.

- **Settings page** on the network (`http://<remote>:9300`): import the provider's M3U playlist (the stream
  login is kept in a private file of the integration, never in the catalog nor the logs), choose the
  categories and channels shown, "4K first" (a channel shown in 4K hides its other versions).
- **Kodi feed** on the same port, only for Kodi's address: `/kodi.m3u` (channels shown, then their other
  versions kept for the replay) and `/epg.xml.gz` (public French guide xmltvfr.fr reduced to these channels).
  Set them as the M3U and XMLTV addresses of PVR IPTV Simple Client, with catch-up enabled.
- **Player**: a tap on a channel plays it; programme page per channel with "Déjà diffusé" (replay, through
  the FHD version of a 4K channel when the provider does not record 4K); the browser opened from the player
  starts on the programme of the channel playing; ON = Kodi on the last channel watched.
- Kodi is brought to the front through the Remote's Android TV integration (Play Store page + "Ouvrir": the
  Shield refuses `market://launch` links), checked with that integration's log (`current_app`).

Build: push a `v*` tag; the archive is committed to the `build` branch (private repository).
