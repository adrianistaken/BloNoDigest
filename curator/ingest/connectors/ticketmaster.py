"""Ticketmaster Discovery API connector.

Baseline coverage for concerts, sports, theater, arena events.
Config: {"city": "Bloomington", "stateCode": "IL", "radius": 25, "unit": "miles", "keyword": ""}
Requires TICKETMASTER_API_KEY in the environment.
"""

from django.conf import settings

from ..fetch import fetch_url
from .base import BaseConnector, RawEvent

API_URL = "https://app.ticketmaster.com/discovery/v2/events.json"


class TicketmasterConnector(BaseConnector):
    def fetch_and_extract(self):
        api_key = settings.TICKETMASTER_API_KEY
        if not api_key:
            raise RuntimeError("TICKETMASTER_API_KEY is not set")

        params = {
            "apikey": api_key,
            "size": str(self.config.get("size", 100)),
            "sort": "date,asc",
            "city": self.config.get("city", "Bloomington"),
            "stateCode": self.config.get("stateCode", "IL"),
            "radius": str(self.config.get("radius", 25)),
            "unit": self.config.get("unit", "miles"),
        }
        if self.config.get("keyword"):
            params["keyword"] = self.config["keyword"]
        query = "&".join(f"{k}={v}" for k, v in params.items())
        response = fetch_url(f"{API_URL}?{query}")
        data = response.json()

        events = []
        for item in data.get("_embedded", {}).get("events", []):
            venue = (item.get("_embedded", {}).get("venues") or [{}])[0]
            address = venue.get("address", {})
            city = venue.get("city", {}).get("name", "")
            state = venue.get("state", {}).get("stateCode", "")
            location = venue.get("location", {})
            start_info = item.get("dates", {}).get("start", {})
            start = start_info.get("dateTime") or start_info.get("localDate")

            price_text, price_min, price_max = "", None, None
            ranges = item.get("priceRanges") or []
            if ranges:
                price_min = ranges[0].get("min")
                price_max = ranges[0].get("max")
                if price_min is not None:
                    price_text = f"${price_min:g}" + (
                        f"-${price_max:g}" if price_max and price_max != price_min else ""
                    )

            genres = []
            for cls in item.get("classifications", []):
                for key in ("segment", "genre"):
                    name = (cls.get(key) or {}).get("name", "")
                    if name and name.lower() not in ("undefined", "other") and name not in genres:
                        genres.append(name)

            def to_float(v):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            # Smallest 16:9 image at least ~300px wide reads well as a thumbnail
            image_url = ""
            candidates = sorted(
                (i for i in item.get("images", []) if i.get("url") and (i.get("width") or 0) >= 300),
                key=lambda i: (i.get("ratio") != "16_9", i.get("width") or 0),
            )
            if candidates:
                image_url = candidates[0]["url"]

            events.append(
                RawEvent(
                    title=item.get("name", ""),
                    description=item.get("info", "") or item.get("pleaseNote", ""),
                    start=start,
                    url=item.get("url", ""),
                    venue_name=venue.get("name", ""),
                    address_line=address.get("line1", ""),
                    city=city,
                    state=state,
                    postal_code=venue.get("postalCode", ""),
                    latitude=to_float(location.get("latitude")),
                    longitude=to_float(location.get("longitude")),
                    price_text=price_text,
                    price_min=price_min,
                    price_max=price_max,
                    image_url=image_url,
                    tags=genres,
                    payload={"id": item.get("id"), "name": item.get("name"), "url": item.get("url")},
                )
            )
        return events
