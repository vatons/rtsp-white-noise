"""Finding a camera's RTSP stream addresses from its IP, over ONVIF.

So nobody has to know their camera's stream path: GetCapabilities to find
the media service, GetProfiles, then GetStreamUri for each. A port of
Peekaboo's OnvifClient and OnvifSoap - just enough ONVIF for this, not an
ONVIF library.

Blocking; Home Assistant runs it in an executor.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from xml.sax.saxutils import escape

from .errors import Failure, TalkbackError

# Where cameras put the ONVIF device service, likeliest first: 80 is the
# standard, 8000 and 2020 are what an "IPC" and a Tapo C120 use, and the
# rest are common enough to be worth a refused connection each.
DEVICE_SERVICE_PORTS = (80, 8000, 2020, 8080, 8899)
_PORT_PROBE_TIMEOUT_S = 0.8
_SOAP_TIMEOUT_S = 5

_DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
_MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
_SCHEMA_NS = "http://www.onvif.org/ver10/schema"


@dataclass(frozen=True)
class Stream:
    uri: str
    width: int = 0
    height: int = 0
    encoding: str | None = None


def find_streams(host: str, username: str | None, password: str | None) -> list[Stream]:
    """Every stream `host` serves, largest first.

    Raises TalkbackError: UNREACHABLE when nothing answers ONVIF on the
    usual ports, REJECTED for a refused login, UNKNOWN for a reply that
    could not be read.
    """
    device = _locate(host, username, password)
    media = _service_xaddr(device, "Media", username, password) or device
    profiles = _profiles(_post(media, username, password, f'<GetProfiles xmlns="{_MEDIA_NS}"/>'))
    if not profiles:
        raise TalkbackError(Failure.UNKNOWN, "the camera listed no stream profiles")
    streams = []
    for token, width, height, encoding in profiles:
        uri = _stream_uri(media, token, username, password)
        if uri:
            # Cameras build this from their own idea of their address - a
            # stale lease, or 0.0.0.0 - where the host just reached is, by
            # definition, reachable.
            streams.append(Stream(_with_host(uri, host), width, height, encoding))
    if not streams:
        raise TalkbackError(Failure.UNKNOWN, "the camera gave no stream addresses")
    return sorted(streams, key=lambda s: s.width * s.height, reverse=True)


def _locate(host: str, username: str | None, password: str | None) -> str:
    """The device service URL. A refused login counts as found: something is
    listening, and the caller can ask for the right credentials."""
    for port in DEVICE_SERVICE_PORTS:
        try:
            with socket.create_connection((host, port), timeout=_PORT_PROBE_TIMEOUT_S):
                pass
        except OSError:
            continue
        xaddr = f"http://{host}:{port}/onvif/device_service"
        try:
            # An open port is not an ONVIF service - a web UI on 80 answers
            # with HTML - so it has to give a reply that can be read.
            if _service_xaddr(xaddr, "Media", username, password) is not None:
                return xaddr
        except TalkbackError as e:
            if e.failure is Failure.REJECTED:
                raise
    raise TalkbackError(Failure.UNREACHABLE, "no ONVIF service on the usual ports")


def _service_xaddr(device: str, category: str, username: str | None, password: str | None) -> str | None:
    body = (
        f'<GetCapabilities xmlns="{_DEVICE_NS}"><Category>{escape(category)}</Category></GetCapabilities>'
    )
    root = _parse(_post(device, username, password, body))
    for element in root.iter():
        if _local(element.tag) == category:
            for child in element:
                if _local(child.tag) == "XAddr" and child.text:
                    return child.text.strip()
    return None


def _stream_uri(media: str, token: str, username: str | None, password: str | None) -> str | None:
    body = (
        f'<GetStreamUri xmlns="{_MEDIA_NS}">'
        f'<StreamSetup><Stream xmlns="{_SCHEMA_NS}">RTP-Unicast</Stream>'
        f'<Transport xmlns="{_SCHEMA_NS}"><Protocol>RTSP</Protocol></Transport></StreamSetup>'
        f"<ProfileToken>{escape(token)}</ProfileToken></GetStreamUri>"
    )
    root = _parse(_post(media, username, password, body))
    for element in root.iter():
        if _local(element.tag) == "Uri" and element.text:
            return element.text.strip()
    return None


def _profiles(xml: str) -> list[tuple[str, int, int, str | None]]:
    """(token, width, height, encoding) per profile, from the video encoder
    configuration only - audio has an Encoding too, and a camera listing
    audio first would otherwise report its video as AAC."""
    found = []
    for profile in _parse(xml).iter():
        if _local(profile.tag) != "Profiles" or not profile.get("token"):
            continue
        width = height = 0
        encoding = None
        for config in profile:
            if _local(config.tag) != "VideoEncoderConfiguration":
                continue
            for element in config.iter():
                name = _local(element.tag)
                if name == "Encoding":
                    encoding = (element.text or "").strip() or None
                elif name == "Width":
                    width = _int(element.text)
                elif name == "Height":
                    height = _int(element.text)
        found.append((profile.get("token"), width, height, encoding))
    return found


def _post(xaddr: str, username: str | None, password: str | None, body: str) -> str:
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f"<s:Header>{_security(username, password)}</s:Header>"
        f"<s:Body>{body}</s:Body></s:Envelope>"
    )
    request = urllib.request.Request(
        xaddr,
        data=envelope.encode(),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_SOAP_TIMEOUT_S) as response:
            text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise TalkbackError(Failure.REJECTED, f"the camera refused the login (HTTP {e.code})") from None
        # A SOAP fault often arrives as a 400 or 500 with a readable body.
        text = e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise TalkbackError(Failure.UNREACHABLE, str(e)) from None
    # ONVIF reports a refused login as a fault, not always as a 401.
    if "NotAuthorized" in text:
        raise TalkbackError(Failure.REJECTED, "the camera refused the login")
    return text


def _security(username: str | None, password: str | None) -> str:
    """A WS-Security UsernameToken: SHA-1 of nonce, time and password."""
    if not username or not password:
        return ""
    nonce = os.urandom(16)
    created = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (
        '<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"<wsse:UsernameToken><wsse:Username>{escape(username)}</wsse:Username>"
        '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
        f"{digest}</wsse:Password>"
        '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f"{base64.b64encode(nonce).decode()}</wsse:Nonce>"
        f"<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>"
    )


def _parse(xml: str) -> ET.Element:
    try:
        return ET.fromstring(xml)
    except ET.ParseError:
        raise TalkbackError(Failure.UNKNOWN, "the camera's ONVIF reply could not be read") from None


def _with_host(uri: str, host: str) -> str:
    parts = urllib.parse.urlsplit(uri)
    if not parts.hostname or parts.hostname == host:
        return uri
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _int(text: str | None) -> int:
    try:
        return int((text or "").strip())
    except ValueError:
        return 0
