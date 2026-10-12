"""Reaching this PC from outside the home network — no third party.

The phone connects straight to the home router's public address, which
forwards one TCP port to this PC. Pieces:

* TLS: a self-signed certificate made on first run. The phone learns its
  SHA-256 fingerprint while it is on the LAN and pins it, so no CA (and no
  outside service) is involved and nobody in between can pose as the PC.
* UPnP IGD: asks the router to forward the port and reads the router's
  public IPv4 address. Plain SSDP + SOAP over the LAN, standard library only.
* Who may connect from outside is decided by the relay: only phones that
  already hold a trust token from a pairing on the LAN (see EmbeddedRelay).
  FailureLimiter slows down token guessing per source address.
"""

import datetime
import hashlib
import ipaddress
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

DEFAULT_PORT = 8443
CERT_NAME = "remote_cert.pem"
KEY_NAME = "remote_key.pem"


# ---------------------------------------------------------------- TLS

def ensure_certificate(folder: Path, common_name: str):
    """(cert path, key path, SHA-256 fingerprint hex) — made on first use.
    EC P-256, valid 20 years: the phone pins the exact certificate, so
    expiry would only break pinning for no gain."""
    cert_path, key_path = folder / CERT_NAME, folder / KEY_NAME
    if not (cert_path.exists() and key_path.exists()):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                             common_name[:60] or "Sudo PC")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=365 * 20))
                .sign(key, hashes.SHA256()))
        folder.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        try:
            key_path.chmod(0o600)  # owner-only where the OS honours it
        except OSError:
            pass
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    der = ssl.PEM_cert_to_DER_cert(cert_path.read_text())
    return cert_path, key_path, hashlib.sha256(der).hexdigest()


def server_ssl_context(cert_path, key_path):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


# ---------------------------------------------------------------- abuse

class FailureLimiter:
    """Per-source-address failed-authentication budget: after MAX failures
    within WINDOW seconds the address is refused for BAN seconds. A trust
    token is 122 random bits, so this is about noise and load, not about
    making guessing feasible."""

    MAX = 5
    WINDOW = 600.0
    BAN = 600.0

    def __init__(self):
        self._fails = {}   # ip -> [timestamps]
        self._banned = {}  # ip -> until

    def banned(self, ip):
        until = self._banned.get(ip)
        if until is None:
            return False
        if time.monotonic() >= until:
            del self._banned[ip]
            return False
        return True

    def _prune(self, now):
        """Forget addresses whose failures and bans have run out, so months
        of internet scanning can't grow these tables without bound."""
        self._fails = {ip: ts for ip, ts in self._fails.items()
                       if ts and now - ts[-1] < self.WINDOW}
        self._banned = {ip: t for ip, t in self._banned.items() if t > now}

    def failed(self, ip):
        now = time.monotonic()
        if len(self._fails) + len(self._banned) > 256:
            self._prune(now)
        recent = [t for t in self._fails.get(ip, []) if now - t < self.WINDOW]
        recent.append(now)
        self._fails[ip] = recent
        if len(recent) >= self.MAX:
            self._banned[ip] = now + self.BAN
            self._fails.pop(ip, None)
            print(f"[agent] remote: {ip} blocked for "
                  f"{int(self.BAN)}s after {self.MAX} failed attempts")

    def succeeded(self, ip):
        self._fails.pop(ip, None)


def is_lan_address(ip):
    """True for loopback/private/link-local peers (the LAN side)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    # 100.64/10 is carrier-grade NAT space, also used by overlay VPNs
    # (Tailscale): not reachable from the open internet.
    return (a.is_private or a.is_loopback or a.is_link_local
            or (a.version == 4 and a in ipaddress.ip_network("100.64.0.0/10")))


# ---------------------------------------------------------------- UPnP IGD

_SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:2",
             "urn:schemas-upnp-org:service:WANIPConnection:1",
             "urn:schemas-upnp-org:service:WANPPPConnection:1")


class UpnpError(Exception):
    pass


def _soap(control_url, service, action, args, timeout=4.0):
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args)
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service}">{body}</u:{action}>'
        '</s:Body></s:Envelope>')
    req = urllib.request.Request(control_url, envelope.encode(), {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{service}#{action}"'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        code = re.search(r"<errorCode>(\d+)</errorCode>", text)
        desc = re.search(r"<errorDescription>(.*?)</errorDescription>", text)
        raise UpnpError(f"{action}: UPnP error {code.group(1) if code else e.code}"
                        f" {desc.group(1) if desc else ''}".strip()) from None


def discover_igd(local_ip, timeout=3.0):
    """(control URL, service type) of the router's WAN connection service,
    found with an SSDP M-SEARCH sent from `local_ip` (the LAN interface; a
    VPN adapter must not swallow the multicast)."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\nMX: 2\r\n'
           "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((local_ip, 0))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                     socket.inet_aton(local_ip))
        s.settimeout(timeout)
        s.sendto(msg.encode(), ("239.255.255.250", 1900))
        locations = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                break
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m and m.group(1).decode() not in locations:
                locations.append(m.group(1).decode())
    finally:
        s.close()
    for loc in locations:
        try:
            with urllib.request.urlopen(loc, timeout=timeout) as resp:
                xml = resp.read().decode(errors="replace")
        except OSError:
            continue
        for service in _SERVICES:
            m = re.search(r"<serviceType>" + re.escape(service)
                          + r"</serviceType>.*?<controlURL>(.*?)</controlURL>",
                          xml, re.S)
            if m:
                return urljoin(loc, m.group(1).strip()), service
    raise UpnpError("no UPnP router answered (is UPnP on in the router?)")


def remove_mapping(port, local_ip):
    """Best effort: delete our forward of `port` (remote access switched
    off, or a permanent mapping from an earlier run)."""
    try:
        control, service = discover_igd(local_ip)
        _soap(control, service, "DeletePortMapping", [
            ("NewRemoteHost", ""), ("NewExternalPort", port),
            ("NewProtocol", "TCP")], timeout=2)
    except Exception:
        pass


class RemoteAccess:
    """Keeps the router's port forward and the public address current, on
    a background thread. `info()` is what the phone stores to reach the PC
    from outside."""

    REFRESH = 20 * 60   # re-check the mapping and public IP
    LEASE = 3600        # mapping lifetime; renewed every REFRESH

    def __init__(self, port, fingerprint, local_ip_fn):
        self.port = port
        self.fingerprint = fingerprint
        self._local_ip = local_ip_fn
        self.host = None          # router's public IPv4
        self.mapped = False
        self.error = "starting"
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self._mapping = None      # (control url, service) of a mapping we made
        self._mapped_ip = None    # the LAN address that mapping points at
        self._op = threading.Lock()  # one router operation at a time
        self._thread = None
        self.on_change = None     # callback(info) when host/mapping changes

    def info(self):
        with self._lock:
            return {"available": bool(self.host and self.mapped),
                    "host": self.host, "port": self.port,
                    "fingerprint": self.fingerprint, "error": self.error}

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="sudo-upnp",
                                        daemon=True)
        self._thread.start()

    def refresh_now(self):
        self._wake.set()

    def stop(self):
        """Best effort: remove the forward we made (agent shutting down)."""
        self._stop = True
        self._wake.set()
        # Wait for a refresh in progress, or it could re-add the mapping
        # right after we delete it.
        if not self._op.acquire(timeout=10):
            return
        try:
            mapping = self._mapping
            if mapping:
                try:
                    _soap(*mapping, "DeletePortMapping", [
                        ("NewRemoteHost", ""), ("NewExternalPort", self.port),
                        ("NewProtocol", "TCP")], timeout=2)
                except Exception:
                    pass
                self._mapping = None
        finally:
            self._op.release()

    def _loop(self):
        while not self._stop:
            before = self.info()
            try:
                self._refresh()
            except Exception as e:
                with self._lock:
                    self.mapped = False
                    self.error = str(e)
            after = self.info()
            if after != before:
                print("[agent] remote access: "
                      + ("ready on port %d" % self.port if after["available"]
                         else f"unavailable ({after['error']})"))
                cb = self.on_change
                if cb:
                    try:
                        cb(after)
                    except Exception:
                        pass
            # Retry sooner while it isn't working (UPnP just switched on,
            # the network came back).
            self._wake.wait(self.REFRESH if after["available"] else 60)
            self._wake.clear()

    def _refresh(self):
        with self._op:
            if not self._stop:
                self._refresh_locked()

    def _refresh_locked(self):
        local_ip = self._local_ip()
        control, service = discover_igd(local_ip)
        resp = _soap(control, service, "GetExternalIPAddress", [])
        m = re.search(r"<NewExternalIPAddress>(.*?)</NewExternalIPAddress>",
                      resp)
        host = m.group(1).strip() if m else ""
        try:
            public = ipaddress.ip_address(host)
        except ValueError:
            raise UpnpError(f"router reported no public address ({host!r})")
        if public.is_private or public in ipaddress.ip_network("100.64.0.0/10"):
            raise UpnpError(f"the router's internet address {host} is shared "
                            "by the provider (CGNAT): unreachable from outside")
        delete = [("NewRemoteHost", ""), ("NewExternalPort", self.port),
                  ("NewProtocol", "TCP")]
        if self._mapped_ip and self._mapped_ip != local_ip:
            # This PC's LAN address changed: drop the forward to the old
            # one (another device may get that address).
            try:
                _soap(control, service, "DeletePortMapping", delete)
            except UpnpError:
                pass
        args = [("NewRemoteHost", ""), ("NewExternalPort", self.port),
                ("NewProtocol", "TCP"), ("NewInternalPort", self.port),
                ("NewInternalClient", local_ip), ("NewEnabled", 1),
                ("NewPortMappingDescription", "Sudo remote access")]
        try:
            _soap(control, service, "AddPortMapping",
                  args + [("NewLeaseDuration", self.LEASE)])
        except UpnpError as e:
            if "718" in str(e):
                # ConflictInMappingEntry: a stale forward of this port to
                # another address (e.g. ours before a DHCP change).
                _soap(control, service, "DeletePortMapping", delete)
                _soap(control, service, "AddPortMapping",
                      args + [("NewLeaseDuration", self.LEASE)])
            elif "725" in str(e):  # OnlyPermanentLeasesSupported
                _soap(control, service, "AddPortMapping",
                      args + [("NewLeaseDuration", 0)])
            else:
                raise
        with self._lock:
            self.host = host
            self.mapped = True
            self.error = None
            self._mapping = (control, service)
            self._mapped_ip = local_ip

