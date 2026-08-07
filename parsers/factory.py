"""
Parser factory.

Site-specific parsers are registered in config.SUPPORTED_SITES as
"module.ClassName" strings and imported lazily (so adding a new site never
requires touching this file - just add one config entry). Unknown domains
fall back to the experimental UniversalParser instead of raising, since a
best-effort attempt is more useful than a hard failure for most simple
novel sites.
"""

import importlib
from urllib.parse import urlparse

from config import SUPPORTED_SITES
from parsers.universal import UniversalParser


class ParserFactory:
    """
    Create the correct parser based on the novel URL.
    """

    @staticmethod
    def create(url: str):
        """
        Return a parser instance.
        """
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        if not domain:
            raise ValueError(f"Not a valid URL: {url}")

        for site_domain, dotted_path in SUPPORTED_SITES.items():
            if domain.endswith(site_domain):
                module_name, class_name = dotted_path.rsplit(".", 1)
                module = importlib.import_module(module_name)
                parser_class = getattr(module, class_name)
                return parser_class()

        print(
            f"No dedicated parser for '{domain}' - using the experimental "
            "Universal Parser. Results may be less accurate than a "
            "site-specific parser."
        )

        base_url = f"{parsed.scheme}://{parsed.netloc}"
        return UniversalParser(base_url)
