# SPDX-License-Identifier: GPL-3.0-only
"""HA-generated media must be read inside HA, independently of external routing."""

from types import SimpleNamespace

import aiohttp
import pytest
from yarl import URL

from custom_components.aqara_p3.media_player import audio_source_url
from custom_components.aqara_p3.playback import playback_failure


@pytest.mark.parametrize(
    "hosts,expected",
    [
        (None, "127.0.0.1"),
        (["0.0.0.0"], "127.0.0.1"),
        (["::"], "::1"),
        (["127.0.0.2"], "127.0.0.2"),
    ],
)
async def test_local_tts_uses_actual_listener_not_configured_external_url(
    hass, hosts, expected
):
    hass.http = SimpleNamespace(
        ssl_certificate=None, server_host=hosts, server_port=9123
    )
    hass.config.external_url = "https://external.example.invalid"
    hass.config.internal_url = "http://unreachable.example.invalid:9999"
    url = URL(audio_source_url(hass, "/api/tts_proxy/example.mp3"))
    assert url.scheme == "http"
    assert url.host == expected
    assert url.port == 9123
    assert url.path == "/api/tts_proxy/example.mp3"


async def test_absolute_ha_url_preserves_path_query_and_signing(hass):
    from aiohttp import web
    from homeassistant.components.http.auth import async_setup_auth

    await async_setup_auth(hass, web.Application())
    hass.http = SimpleNamespace(
        ssl_certificate=None, server_host=None, server_port=9123
    )
    hass.config.external_url = "https://external.example.invalid"
    url = URL(
        audio_source_url(
            hass,
            "https://external.example.invalid/api/tts_proxy/example.mp3?key=example",
        )
    )
    assert url.host == "127.0.0.1"
    assert url.query["key"] == "example"
    media = URL(audio_source_url(hass, "/media/local/a%20b.wav"))
    assert media.host == "127.0.0.1"
    assert media.path == "/media/local/a b.wav"
    assert "authSig" in media.query


async def test_external_media_keeps_its_own_origin(hass):
    hass.http = SimpleNamespace(
        ssl_certificate=None, server_host=None, server_port=9123
    )
    hass.config.external_url = "https://external.example.invalid"
    external = "https://media.example.invalid/song.mp3?key=example"
    assert audio_source_url(hass, external) == external


async def test_native_https_retains_certificate_hostname(hass):
    hass.http = SimpleNamespace(
        ssl_certificate="example.pem", server_host=None, server_port=9123
    )
    hass.config.external_url = "https://ha.example.invalid:9123"
    url = URL(audio_source_url(hass, "/api/tts_proxy/example.mp3"))
    assert url.scheme == "https"
    assert url.host == "ha.example.invalid"


@pytest.mark.parametrize(
    "error,key",
    [
        (
            aiohttp.ClientConnectorError(
                None, ConnectionRefusedError(111, "private?authSig=secret")
            ),
            "media_source_connection",
        ),
        (
            aiohttp.ClientResponseError(
                None, (), status=403, message="private?authSig=secret"
            ),
            "media_http_error",
        ),
        (FileNotFoundError("private?authSig=secret"), "media_ffmpeg_missing"),
        (OSError("private?authSig=secret"), "media_source_error"),
        (TimeoutError(), "media_source_timeout"),
    ],
)
def test_actionable_errors_do_not_reveal_source_urls(error, key):
    result = playback_failure(error, "source")
    assert result.translation_key == key
    assert "secret" not in str(result)
    assert "authSig" not in str(result)
    assert "private" not in str(result)


def test_http_status_is_preserved():
    result = playback_failure(
        aiohttp.ClientResponseError(None, (), status=404), "source"
    )
    assert result.translation_placeholders == {"status": "404"}
    assert "404" in str(result)
