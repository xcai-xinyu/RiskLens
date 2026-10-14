"""Public-only HTTP(S), pinned IP sockets and bounded redirect/body handling."""
import http.client
import ipaddress
import socket
import ssl
from time import monotonic
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 5_000_000


class FetchError(ValueError):
    pass


def public_target(url, resolver=socket.getaddrinfo):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise FetchError('url_not_allowed')
        if parsed.port and parsed.port != (443 if parsed.scheme=='https' else 80):
            raise FetchError('port_not_allowed')
        if any(ch in url for ch in ('\r', '\n', '\\')):
            raise FetchError('url_not_allowed')
        host = parsed.hostname.encode('idna').decode('ascii')
        if host.lower() in ('localhost', 'metadata.google.internal') or host.lower().endswith('.localhost'):
            raise FetchError('private_address')
        port = 443 if parsed.scheme=='https' else 80
        addresses = sorted({row[4][0] for row in resolver(host, port, type=socket.SOCK_STREAM)})
        if not addresses or any(not ipaddress.ip_address(addr).is_global for addr in addresses):
            raise FetchError('private_address')
        return parsed, host, port, addresses[0]
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, FetchError):
            raise
        raise FetchError('url_not_allowed') from None


def request_once(url, timeout, method='GET', body=None, headers=None):
    parsed, host, port, ip = public_target(url)
    deadline = monotonic()+timeout
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    # Connect to the validated address; never perform a second hostname resolution.
    sock = socket.create_connection((ip, port), timeout=timeout)
    if parsed.scheme == 'https':
        try:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        except Exception:
            sock.close()
            raise
    connection.sock = sock
    try:
        target = parsed.path or '/'
        if parsed.query:
            target += '?' + parsed.query
        connection.request(method, target, body=body,
            headers={'User-Agent':'RiskLens-P1/1.0', 'Accept-Encoding':'identity', **(headers or {})})
        response = connection.getresponse()
        if response.status in (301,302,303,307,308):
            return response.status, dict(response.getheaders()), b''
        if not 200 <= response.status < 300:
            raise FetchError('http_'+str(response.status))
        if response.getheader('Content-Encoding', 'identity') not in ('', 'identity'):
            raise FetchError('unsupported_encoding')
        length = response.getheader('Content-Length')
        if length and int(length) > MAX_BYTES:
            raise FetchError('document_too_large')
        pieces = []
        total = 0
        while True:
            remaining = deadline-monotonic()
            if remaining <= 0:
                raise FetchError('network_timeout')
            sock.settimeout(min(5,remaining))
            piece = response.read1(min(65536,MAX_BYTES+1-total))
            if not piece:
                break
            total += len(piece)
            if total > MAX_BYTES:
                raise FetchError('document_too_large')
            pieces.append(piece)
        return response.status, dict(response.getheaders()), b''.join(pieces)
    finally:
        connection.close()


def fetch_public(url, timeout=15, transport=request_once):
    deadline = monotonic()+timeout
    for _ in range(4):
        remaining = deadline-monotonic()
        if remaining <= 0:
            raise FetchError('network_timeout')
        # Validate even when transport is a fixture in security tests.
        public_target(url)
        status, headers, raw = transport(url, remaining)
        if status in (301,302,303,307,308):
            location = next((v for k,v in headers.items() if k.lower()=='location'), None)
            if not location:
                raise FetchError('invalid_redirect')
            url = urljoin(url, location)
            continue
        if len(raw) > MAX_BYTES:
            raise FetchError('document_too_large')
        return url, headers, raw
    raise FetchError('redirect_limit')
