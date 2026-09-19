# SPDX-License-Identifier: GPL-3.0-only
"""Local PCM speaker, including standard HA media sources and TTS."""

import ipaddress

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEnqueue,
    MediaPlayerEntity,
    MediaPlayerEntityDescription,
    MediaPlayerEntityFeature,
    async_process_play_media_url,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.network import is_hass_url
from homeassistant.helpers.restore_state import RestoreEntity
from yarl import URL

from .audio import AudioEntity

PARALLEL_UPDATES = 0


def audio_source_url(hass, media_id):
    """HA fetches the audio itself; prefer its actual local HTTP listener.

    Reverse-proxy/Nabu Casa addresses are intended for other devices and may
    not route back from Core. Keep the signed path/query, and leave external
    audio URLs and native-TLS listeners on HA's ordinary verified URL path.
    """
    parsed = URL(media_id)
    if parsed.user:
        return media_id
    local = (not parsed.is_absolute() and media_id.startswith("/")) or is_hass_url(
        hass, media_id
    )
    server = getattr(hass, "http", None)
    if local and server is not None and not server.ssl_certificate:
        hosts = server.server_host
        if isinstance(hosts, str):
            hosts = [hosts]
        if not hosts or "0.0.0.0" in hosts or "127.0.0.1" in hosts:
            host = "127.0.0.1"
        elif "::" in hosts or "::1" in hosts:
            host = "::1"
        else:
            host = None
            for candidate in hosts:
                try:
                    ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                host = candidate
                break
        if host:
            signed = async_process_play_media_url(
                hass, media_id, allow_relative_url=True
            )
            origin = URL.build(scheme="http", host=host, port=server.server_port)
            return str(origin) + URL(signed).raw_path_qs
    return async_process_play_media_url(hass, media_id)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([P3MediaPlayer(entry.runtime_data.audio)])


class P3MediaPlayer(AudioEntity, MediaPlayerEntity, RestoreEntity):
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
    )

    def __init__(self, coordinator):
        super().__init__(
            coordinator,
            MediaPlayerEntityDescription(key="speaker", translation_key="speaker"),
        )
        self.playback = coordinator.media

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if last := await self.async_get_last_state():
            volume = last.attributes.get("volume_level")
            if type(volume) in (int, float) and 0 <= volume <= 1:
                self.playback.volume = volume
            self.playback.muted = last.attributes.get("is_volume_muted") is True
        self.playback.listeners.add(self.async_write_ha_state)
        self.async_on_remove(
            lambda: self.playback.listeners.discard(self.async_write_ha_state)
        )

    @property
    def available(self):
        return self.coordinator.parent.last_update_success

    @property
    def state(self):
        return self.playback.state

    @property
    def volume_level(self):
        return self.playback.volume

    @property
    def is_volume_muted(self):
        return self.playback.muted

    @property
    def media_title(self):
        return self.playback.title

    @property
    def extra_state_attributes(self):
        return (
            {
                "playback_error": self.playback.error,
                "playback_error_detail": self.playback.error_detail,
                "playback_error_stage": self.playback.error_stage,
            }
            if self.playback.error
            else {}
        )

    async def async_set_volume_level(self, volume):
        self.playback.set_volume(volume)

    async def async_mute_volume(self, mute):
        self.playback.muted = bool(mute)
        self.playback.changed()

    async def async_media_stop(self):
        async with self.coordinator._command_lock:
            await self.playback.stop_locked()

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )

    async def async_play_media(self, media_type, media_id, **kwargs):
        if kwargs.get("enqueue") not in (None, MediaPlayerEnqueue.REPLACE):
            raise HomeAssistantError("This player replaces the current audio")
        # tts.speak always passes announce=True. Accept it as replacement;
        # MEDIA_ANNOUNCE (resume prior media) is deliberately not advertised.
        title = (kwargs.get("extra") or {}).get("title")
        if media_source.is_media_source_id(media_id):
            item = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = item.url
        if not isinstance(media_id, str) or not media_id:
            raise HomeAssistantError("Select audio to play")
        url = audio_source_url(self.hass, media_id)
        await self.playback.play(url, title)
