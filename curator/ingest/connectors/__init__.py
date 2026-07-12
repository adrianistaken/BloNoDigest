from .html_config import HTMLConfigConnector
from .ics import ICSConnector
from .jsonld import JSONLDConnector
from .manual import ManualConnector
from .playwright_browser import PlaywrightConnector
from .rss import RSSConnector
from .ticketmaster import TicketmasterConnector
from .whereabouts import WhereaboutsConnector

CONNECTORS = {
    "ticketmaster_api": TicketmasterConnector,
    "ics": ICSConnector,
    "rss": RSSConnector,
    "json_ld": JSONLDConnector,
    "html_config": HTMLConfigConnector,
    "playwright_config": PlaywrightConnector,
    "whereabouts_api": WhereaboutsConnector,
    "manual": ManualConnector,
}


def get_connector(source):
    connector_class = CONNECTORS.get(source.source_type)
    if connector_class is None:
        raise ValueError(f"No connector for source_type={source.source_type!r}")
    return connector_class(source)
