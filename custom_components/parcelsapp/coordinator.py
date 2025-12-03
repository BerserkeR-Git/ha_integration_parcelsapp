from datetime import datetime, timedelta
import logging
import time
import json
import aiohttp
import async_timeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.storage import Store

from .const import DOMAIN, DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

# Country code → readable country name
COUNTRY_MAP = {
    "NL": "Netherlands",
    "DE": "Germany",
    "BE": "Belgium",
    "FR": "France",
    "UK": "United Kingdom",
    "GB": "United Kingdom",
    "CN": "China",
    "AT": "Austria",
    "US": "United States",
    "ES": "Spain",
    "IT": "Italy",
    "PL": "Poland",
    "SE": "Sweden",
    "NO": "Norway",
    "FI": "Finland",
}


def _parse_iso(dt_str: str) -> datetime | None:
    """Parse ISO8601 date string safely, handling 'Z' timezone."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    try:
        if dt_str.endswith("Z"):
            dt_str = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


def resolve_location(shipment: dict) -> str | None:
    """
    Determine the most accurate current location for a shipment.

    Strategy:
      1. Find the newest state (by date) that has a 'location'.
      2. If none, and status is delivered/pickup/out_for_delivery, use destination.
      3. Otherwise use origin as fallback.
    """
    states = shipment.get("states", []) or []

    best_state = None
    best_time = None

    for state in states:
        loc = state.get("location")
        if not loc:
            continue

        dt = _parse_iso(state.get("date"))
        if dt is None:
            if best_state is None:
                best_state = state
            continue

        if best_time is None or dt > best_time:
            best_time = dt
            best_state = state

    if best_state:
        loc = best_state.get("location")
        if loc:
            if len(loc) == 2 and loc.isalpha():
                return COUNTRY_MAP.get(loc.upper(), loc)
            return loc

    status = (shipment.get("status") or "").lower()

    if status in ("delivered", "out_for_delivery", "pickup", "ready_for_pickup"):
        dest = shipment.get("destination")
        if dest:
            return dest

    origin = shipment.get("origin")
    if origin:
        return origin

    return None


class ParcelsAppCoordinator(DataUpdateCoordinator):
    """Custom coordinator for Parcels App."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.api_key = entry.data["api_key"]
        self.destination_country = entry.data["destination_country"]
        self.session = aiohttp.ClientSession()
        self.tracked_packages: dict[str, dict] = {}
        self.store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}_tracked_packages")

        language_code = (hass.config.language or "en")[:2].lower()
        self.language = language_code

    async def async_init(self):
        await self._load_tracked_packages()

    async def _load_tracked_packages(self):
        stored_data = await self.store.async_load()
        if stored_data:
            for package in stored_data.values():
                if (
                    "uuid_timestamp" in package
                    and isinstance(package["uuid_timestamp"], str)
                ):
                    package["uuid_timestamp"] = datetime.fromisoformat(
                        package["uuid_timestamp"]
                    )
            self.tracked_packages = stored_data
        else:
            self.tracked_packages = {}

    async def _save_tracked_packages(self):
        for package in self.tracked_packages.values():
            if (
                "uuid_timestamp" in package
                and isinstance(package["uuid_timestamp"], datetime)
            ):
                package["uuid_timestamp"] = package["uuid_timestamp"].isoformat()
        await self.store.async_save(self.tracked_packages)
        await self.async_request_refresh()

    async def track_package(self, tracking_id: str, name: str | None = None) -> None:
        """Track a new package or update an existing one."""
        url = "https://parcelsapp.com/api/v3/shipments/tracking"
        payload = json.dumps(
            {
                "shipments": [
                    {
                        "trackingId": tracking_id,
                        "destinationCountry": self.destination_country,
                    }
                ],
                "language": self.language,
                "apiKey": self.api_key,
            }
        )
        headers = {"Content-Type": "application/json"}

        try:
            async with self.session.post(
                url, headers=headers, data=payload
            ) as response:
                response_text = await response.text()
                response.raise_for_status()
                data = json.loads(response_text)

                existing_package_data = self.tracked_packages.get(tracking_id, {})

                if "uuid" in data:
                    package_data = {
                        **existing_package_data,
                        "status": "pending",
                        "uuid": data["uuid"],
                        "uuid_timestamp": datetime.now(),
                        "message": "Tracking initiated",
                        "last_updated": datetime.now().isoformat(),
                        "name": name or existing_package_data.get("name"),
                    }
                    self.tracked_packages[tracking_id] = package_data

                elif "shipments" in data and data["shipments"]:
                    shipment = data["shipments"][0]

                    resolved_location = resolve_location(shipment)

                    eta = shipment.get("eta") or {}
                    eta_period = eta.get("period", [])
                    eta_remaining = eta.get("remaining", [])

                    eta_days_range = (
                        f"{eta_remaining[0]}–{eta_remaining[1]}"
                        if eta_remaining and len(eta_remaining) == 2
                        else None
                    )

                    eta_date_range = (
                        f"{eta_period[0]}/{eta_period[1]}"
                        if eta_period and len(eta_period) == 2
                        else None
                    )

                    expected_delivery = None
                    for attr in shipment.get("attributes", []):
                        if attr.get("l") == "eta":
                            expected_delivery = attr.get("val")

                    package_data = {
                        **existing_package_data,
                        "status": shipment.get("status", "unknown"),
                        "message": shipment.get("lastState", {}).get(
                            "status", "No status available"
                        ),
                        "location": resolved_location,
                        "origin": shipment.get("origin"),
                        "destination": shipment.get("destination"),
                        "carrier": shipment.get("detectedCarrier", {}).get("name"),
                        "days_in_transit": next(
                            (
                                attr.get("val")
                                for attr in shipment.get("attributes", [])
                                if attr.get("l") == "days_transit"
                            ),
                            None,
                        ),
                        "eta_days_range": eta_days_range,
                        "eta_date_range": eta_date_range,
                        "expected_delivery": expected_delivery,
                        "last_updated": datetime.now().isoformat(),
                        "name": name or existing_package_data.get("name"),
                        "tracking_id": tracking_id,
                    }
                    self.tracked_packages[tracking_id] = package_data

                else:
                    _LOGGER.error("Unexpected API response: %s", response_text)
                    return

            await self._save_tracked_packages()

        except aiohttp.ClientError as err:
            _LOGGER.error("Error tracking package %s: %s", tracking_id, err)
        except json.JSONDecodeError:
            _LOGGER.error(
                "Failed to parse response for %s. Response: %s",
                tracking_id,
                response_text,
            )

    async def remove_package(self, tracking_id: str) -> None:
        if tracking_id in self.tracked_packages:
            del self.tracked_packages[tracking_id]
            await self._save_tracked_packages()
        else:
            _LOGGER.warning("Cannot remove package: not found: %s", tracking_id)

    async def update_package(
        self, tracking_id: str, uuid: str | None, uuid_timestamp: datetime | None
    ) -> None:
        if isinstance(uuid_timestamp, str):
            uuid_timestamp = datetime.fromisoformat(uuid_timestamp)

        uuid_expired = False
        if uuid_timestamp:
            if datetime.now() - uuid_timestamp > timedelta(minutes=30):
                uuid_expired = True
        else:
            uuid_expired = True

        if uuid_expired or not uuid:
            new_uuid, new_uuid_timestamp, shipment_data = await self.get_new_uuid(
                tracking_id
            )

            if shipment_data:
                await self._update_shipment(tracking_id, shipment_data)
                return

            if new_uuid:
                package = self.tracked_packages.get(tracking_id, {})
                package["uuid"] = new_uuid
                package["uuid_timestamp"] = new_uuid_timestamp
                self.tracked_packages[tracking_id] = package
                await self._save_tracked_packages()
                uuid = new_uuid
                uuid_timestamp = new_uuid_timestamp
            else:
                _LOGGER.error(
                    "No UUID and no shipment data for tracking ID %s", tracking_id
                )
                return

        await self._fetch_shipment_data(tracking_id, uuid)

    async def _update_shipment(self, tracking_id: str, shipment: dict) -> None:
        resolved_location = resolve_location(shipment)

        eta = shipment.get("eta") or {}
        eta_period = eta.get("period", [])
        eta_remaining = eta.get("remaining", [])

        eta_days_range = (
            f"{eta_remaining[0]}–{eta_remaining[1]}"
            if eta_remaining and len(eta_remaining) == 2
            else None
        )

        eta_date_range = (
            f"{eta_period[0]}/{eta_period[1]}"
            if eta_period and len(eta_period) == 2
            else None
        )

        expected_delivery = None
        for attr in shipment.get("attributes", []):
            if attr.get("l") == "eta":
                expected_delivery = attr.get("val")

        package_data = self.tracked_packages.get(tracking_id, {})
        package_data.update(
            {
                "status": shipment.get("status", "unknown"),
                "message": shipment.get("lastState", {}).get(
                    "status", "No status available"
                ),
                "location": resolved_location,
                "origin": shipment.get("origin"),
                "destination": shipment.get("destination"),
                "carrier": shipment.get("detectedCarrier", {}).get("name"),
                "days_in_transit": next(
                    (
                        attr.get("val")
                        for attr in shipment.get("attributes", [])
                        if attr.get("l") == "days_transit"
                    ),
                    None,
                ),
                "eta_days_range": eta_days_range,
                "eta_date_range": eta_date_range,
                "expected_delivery": expected_delivery,
                "last_updated": datetime.now().isoformat(),
            }
        )

        self.tracked_packages[tracking_id] = package_data
        await self._save_tracked_packages()

    async def update_tracked_packages(self) -> None:
        """Update all tracked packages (including delivered and archived)."""
        for tracking_id, package_data in self.tracked_packages.items():
            await self.update_package(
                tracking_id,
                package_data.get("uuid"),
                package_data.get("uuid_timestamp"),
            )

    async def _async_update_data(self):
        status_data = await self._fetch_parcels_app_status()
        await self.update_tracked_packages()
        return {
            "parcels_app_status": status_data,
            "tracked_packages": self.tracked_packages,
        }

    async def _fetch_parcels_app_status(self):
        try:
            start_time = time.time()
            async with async_timeout.timeout(10):
                async with self.session.get("https://parcelsapp.com/") as response:
                    await response.text()
                    response.raise_for_status()
                    end_time = time.time()
                    response_time = end_time - start_time
                    return {
                        "status": response.status == 200,
                        "response_time": response_time,
                        "response_code": response.status,
                    }
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error communicating with ParcelsApp: {err}")

    async def get_new_uuid(self, tracking_id: str):
        url = "https://parcelsapp.com/api/v3/shipments/tracking"
        payload = json.dumps(
            {
                "shipments": [
                    {
                        "trackingId": tracking_id,
                        "destinationCountry": self.destination_country,
                    }
                ],
                "language": self.language,
                "apiKey": self.api_key,
            }
        )
        headers = {"Content-Type": "application/json"}

        try:
            async with self.session.post(
                url, headers=headers, data=payload
            ) as response:
                response_text = await response.text()
                response.raise_for_status()
                data = json.loads(response_text)

                if "uuid" in data:
                    return data["uuid"], datetime.now(), None
                if "shipments" in data and data["shipments"]:
                    return None, None, data["shipments"][0]

                _LOGGER.error("Unexpected UUID response: %s", response_text)
                return None, None, None

        except aiohttp.ClientError as err:
            _LOGGER.error("UUID request error for %s: %s", tracking_id, err)
            return None, None, None

    async def _fetch_shipment_data(self, tracking_id: str, uuid: str) -> None:
        url = (
            "https://parcelsapp.com/api/v3/shipments/tracking"
            f"?uuid={uuid}&apiKey={self.api_key}&language={self.language}"
        )

        try:
            async with self.session.get(url) as response:
                response.raise_for_status()
                data = await response.json()

                if data.get("done") and data.get("shipments"):
                    shipment = data["shipments"][0]
                    await self._update_shipment(tracking_id, shipment)
                else:
                    _LOGGER.debug("No tracking data yet for %s", tracking_id)

        except aiohttp.ClientError as err:
            _LOGGER.error("Error updating shipment %s: %s", tracking_id, err)
