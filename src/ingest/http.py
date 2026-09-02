"""Client HTTP prudent : allow/deny-list, robots.txt, cache, throttling.

La deny-list de ``config/sources.yaml`` est appliquée AVANT toute requête. Une
source qu'on a constatée inutilisable (403, robots.txt, pages sans détail) ne
doit jamais être rappelée, même si un collecteur construit son URL par erreur.
"""

from __future__ import annotations

import hashlib
import logging
import time
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from ..config import DATA_DIR, Config

log = logging.getLogger(__name__)


class SourceBlocked(RuntimeError):
    """URL interdite par la deny-list, l'allow-list ou robots.txt."""


@dataclass
class Response:
    url: str
    status: int
    text: str
    from_cache: bool = False


class Fetcher:
    def __init__(self, config: Config, cache_dir: Path | None = None, offline: bool = False):
        self.config = config
        http = (config.sources or {}).get("http", {})
        self.user_agent = http.get("user_agent", "veille-courses/1.0")
        self.timeout = float(http.get("timeout_s", 20))
        self.delay = float(http.get("delay_between_requests_s", 1.5))
        self.max_retries = int(http.get("max_retries", 3))
        self.cache_ttl_s = float(http.get("cache_ttl_h", 12)) * 3600
        self.respect_robots = bool(http.get("respect_robots_txt", True))
        self.allow = {d.lower() for d in (config.sources or {}).get("allow", [])}
        self.deny = {d.lower(): r for d, r in ((config.sources or {}).get("deny", {}) or {}).items()}
        self.offline = offline
        self.cache_dir = cache_dir or (DATA_DIR / "cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent, "Accept-Language": "fr-FR"})
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._last_request = 0.0
        # Coupe-circuit : un run interroge ~200 URL. Si un hôte est injoignable,
        # le marteler avec 3 tentatives et un backoff exponentiel chacune fait
        # durer le run des minutes pour rien.
        self._failures: dict[str, int] = {}
        self.failure_threshold = int(http.get("host_failure_threshold", 3))

    # ------------------------------------------------------------------ #
    def check_url(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            raise SourceBlocked(f"URL sans hôte : {url}")
        for denied, reason in self.deny.items():
            if host == denied or host.endswith("." + denied):
                raise SourceBlocked(f"{host} en deny-list ({reason})")
        if self.allow and not any(host == a or host.endswith("." + a) for a in self.allow):
            raise SourceBlocked(f"{host} hors allow-list")
        if self._failures.get(host, 0) >= self.failure_threshold:
            raise SourceBlocked(
                f"{host} écarté pour ce run : {self._failures[host]} échecs consécutifs"
            )
        if self.respect_robots and not self._robots_allow(url):
            raise SourceBlocked(f"{url} interdit par robots.txt")

    def _robots_allow(self, url: str) -> bool:
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._robots.get(root)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(root + "/robots.txt")
            try:
                parser.read()
            except Exception as exc:  # réseau indisponible : on ne bloque pas dessus
                log.debug("robots.txt illisible pour %s : %s", root, exc)
                parser = None  # type: ignore[assignment]
            self._robots[root] = parser  # type: ignore[assignment]
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    # ------------------------------------------------------------------ #
    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".html")

    def _read_cache(self, url: str) -> str | None:
        path = self._cache_path(url)
        if not path.exists():
            return None
        if not self.offline and (time.time() - path.stat().st_mtime) > self.cache_ttl_s:
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.time()

    # ------------------------------------------------------------------ #
    def get(self, url: str, use_cache: bool = True) -> Response:
        """GET avec cache disque, throttling, backoff et coupe-circuit par hôte."""
        self.check_url(url)
        host = (urlparse(url).hostname or "").lower()
        if use_cache:
            cached = self._read_cache(url)
            if cached is not None:
                return Response(url, 200, cached, from_cache=True)
        if self.offline:
            raise SourceBlocked(f"mode hors ligne et rien en cache pour {url}")

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(2**attempt)
                continue
            if response.status_code == 200:
                self._failures.pop(host, None)
                self._cache_path(url).write_text(response.text, encoding="utf-8")
                return Response(url, 200, response.text)
            if response.status_code in (403, 404, 410):
                # Inutile d'insister : c'est le cas e.leclerc du §6.
                log.warning("%s → %s, abandon", url, response.status_code)
                return Response(url, response.status_code, "")
            last_error = RuntimeError(f"HTTP {response.status_code}")
            time.sleep(2**attempt)

        self._failures[host] = self._failures.get(host, 0) + 1
        log.warning(
            "échec après %s tentatives sur %s : %s (%s échec(s) pour cet hôte)",
            self.max_retries, url, last_error, self._failures[host],
        )
        return Response(url, 0, "")
