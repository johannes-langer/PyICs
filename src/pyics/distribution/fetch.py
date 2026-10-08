"""
Downloading indexes and course archives: over HTTPS, or from local files and ``file:``
URLs, e.g. on a network share. Plain HTTP is used only where it is allowed explicitly.
"""

import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pyics.distribution.dependencies import pyics_version

TIMEOUT = 30  # seconds
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+://")


class FetchError(Exception):
    """A URL can't, or mustn't, be fetched."""


def is_url(location: str) -> bool:
    """Whether ``location`` is a URL, rather than a path."""
    return bool(_URL.match(location))


def to_url(location: str) -> str:
    """``location`` as a URL: URLs are kept as they are, and paths (e.g.
    ``./courses/index.json``) become ``file:`` URLs."""
    if is_url(location):
        return location
    return Path(location).expanduser().absolute().as_uri()


def scheme(url: str) -> str:
    return urllib.parse.urlsplit(url).scheme.lower()


def check_url(url: str, *, allow_http: bool = False, allow_file: bool = True) -> None:
    """
    Raise ``FetchError`` if ``url`` mustn't be fetched: only HTTPS URLs, HTTP URLs if
    ``allow_http``, and ``file:`` URLs if ``allow_file`` may be.
    """
    match scheme(url):
        case "https":
            return
        case "http" if allow_http:
            return
        case "file" if allow_file:
            return
        case "http":
            raise FetchError(
                f"{url} isn't secure: use an https URL, or allow plain http with "
                "--allow_http"
            )
        case "file":
            raise FetchError(f"{url} is a local file, which a web source can't link to")
        case _:
            raise FetchError(f"{url} can't be fetched: only https URLs are supported")


def _ssl_context() -> ssl.SSLContext:
    """The system's certificates, through ``truststore`` if it is installed (some
    Python installations, e.g. from python.org on macOS, don't find them otherwise)."""
    try:
        import truststore
    except ImportError:
        return ssl.create_default_context()
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _read_file(url: str, max_bytes: int) -> bytes:
    """The contents of the ``file:`` URL ``url``, also of a network share on Windows
    (e.g. ``file://server/share/index.json``), which urllib can't open."""
    try:
        with open(Path.from_uri(url), "rb") as f:
            data = f.read(max_bytes + 1)
    except (OSError, ValueError) as err:
        raise FetchError(f"Can't fetch {url}: {err}") from err
    if len(data) > max_bytes:
        raise FetchError(f"{url} is larger than {max_bytes // 2**20} MB")
    return data


def fetch(
    url: str, *, max_bytes: int, allow_http: bool = False, allow_file: bool = True
) -> bytes:
    """
    The contents of ``url``, at most ``max_bytes`` of them (see ``check_url`` for the
    URLs that may be fetched, also after redirects).

    Raises
    ------
    FetchError
        If it mustn't or can't be fetched, or is larger than ``max_bytes``.
    """
    check_url(url, allow_http=allow_http, allow_file=allow_file)
    if scheme(url) == "file":
        return _read_file(url, max_bytes)
    request = urllib.request.Request(
        url, headers={"User-Agent": f"pyics/{pyics_version()}"}
    )
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT, context=_ssl_context()
        ) as response:
            # redirects may lead elsewhere, e.g. from https to http
            check_url(response.geturl(), allow_http=allow_http, allow_file=False)
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as err:
        raise FetchError(f"Can't fetch {url}: {err.code} {err.reason}") from err
    except urllib.error.URLError as err:
        if isinstance(err.reason, ssl.SSLCertVerificationError):
            host = urllib.parse.urlsplit(url).hostname
            raise FetchError(
                f"Can't fetch {url}: the certificate of {host} can't be verified "
                f"({err.reason.verify_message}). If your network's or your system's "
                "certificates are the problem, `pip install truststore` makes pyics "
                "use your system's certificates. With Python from python.org on macOS, "
                "run its 'Install Certificates.command' instead."
            ) from err
        raise FetchError(f"Can't fetch {url}: {err.reason}") from err
    except (OSError, ValueError) as err:  # e.g. a timeout, or a malformed URL
        raise FetchError(f"Can't fetch {url}: {err}") from err
    if len(data) > max_bytes:
        raise FetchError(f"{url} is larger than {max_bytes // 2**20} MB")
    return data
