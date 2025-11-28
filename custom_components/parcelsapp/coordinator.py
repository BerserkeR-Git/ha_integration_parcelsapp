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


class ParcelsAppCoordinator(DataUpdateCoordinator):
    """Custom coordinator for Parcels App."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
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

        # Use the first two letters of HA language as API language
        language_code = (hass.config.language or "en")[:2].lower()
        self.language = language_code

    # ---------- helpers for shipment data ----------

    def _get_eta_ranges(self, shipment: dict):
        """Extract ETA day-range and date-range from shipment data."""
        eta = shipment.get("eta") or {}
        eta_period = eta.get("period") or []
        eta_remaining = eta.get("remaining") or []

        eta_days_range = None
        if (
            len(eta_remaining) >= 2
            and eta_remaining[0] is not None
            and eta_remaining[1] is not None
        ):
            eta_days_range = f"{eta_remaining[0]}-{eta_remaining[1]}"

        eta_date_range = None
        if len(eta_period) >= 2 and eta_period[0] and eta_period[1]:
            eta_date_range = f"{eta_period[0]}/{eta_period[1]}"

        return eta_days_range, eta_date_range

    def _get_expected_delivery(self, shipment: dict):
        """Extract expected delivery window string from attributes, if present."""
        for attr in shipment.get("attributes", []):
            if attr.get("l") == "eta":
                return attr.get("val")
        return None

    def _get_location(self, shipment: dict) -> str:
        """Get the best current location for the shipment.

        Priority:
        1. lastState.location, if present
        2. last state in states[] that has a location field
        3. 'undefined' if nothing is available
        """
        last_state = shipment.get("lastState") or {}
        loc = last_state.get("location")
        if loc:
            return loc

        # Fallback: iterate states in reverse to find last location
        for state in reversed(shipment.get("states", [])):
            loc2 = state.get("location")
            if loc2:
                return loc2

        return "undefined"

    # ---------- persistence ----------

    async def async_init(self):
        """Initialize the coordinator."""
        await self._load_tracked_packages()

    async def _load_tracked_packages(self):
        """Load tracked packages from persistent storage."""
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
        """Save tracked packages to persistent storage."""
        for package in self.tracked_packages.values():
            if (
                "uuid_timestamp" in package
                and isinstance(package["uuid_timestamp"], datetime)
            ):
                package["uuid_timestamp"] = package["uuid_timestamp"].isoformat()
        await self.store.async_save(self.tracked_packages)
        await self.async_request_refresh()

    # ---------- API operations ----------

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
                    # New tracking request – shipment not yet resolved
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
                    # Shipment data returned directly
                    shipment = data["shipments"][0]
                    eta_days_range, eta_date_range = self._get_eta_ranges(shipment)
                    expected_delivery = self._get_expected_delivery(shipment)
                    location = self._get_location(shipment)

                    package_data = {
                        **existing_package_data,
                        "status": shipment.get("status", "unknown"),
                        "uuid": None,
                        "uuid_timestamp": None,
                        "message": shipment.get("lastState", {}).get(
                            "status", "No status available"
                        ),
                        "location": location,
                        "origin": shipment.get("origin"),
                        "destination": shipment.get("destination"),
                        "eta_days_range": eta_days_range,
                        "eta_date_range": eta_date_range,
                        "expected_delivery": expected_delivery,
                        "carrier": shipment.get("detectedCarrier", {}).get("name"),
                        "days_in_transit": next(
                            (
                                attr.get("val")
                                for attr in shipment.get("attributes", [])
                                if attr.get("l") == "days_transit"
                            ),
                            None,
                        ),
                        "last_updated": datetime.now().isoformat(),
                        "name": name or existing_package_data.get("name"),
                    }
                    self.tracked_packages[tracking_id] = package_data

                else:
                    _LOGGER.error(
                        "Unexpected API response for tracking ID %s. Response: %s",
                        tracking_id,
                        response_text,
                    )
                    return

            await self._save_tracked_packages()

        except aiohttp.ClientError as err:
            _LOGGER.error("Error tracking package %s: %s", tracking_id, err)
        except json.JSONDecodeError:
            _LOGGER.error(
                "Failed to parse API response for tracking ID %s. Response: %s",
                tracking_id,
                response_text,
            )

    async def remove_package(self, tracking_id: str) -> None:
        """Remove a package from tracking."""
        if tracking_id in self.tracked_packages:
            del self.tracked_packages[tracking_id]
            await self._save_tracked_packages()
        else:
            _LOGGER.warning(
                "Tracking ID %s not found in tracked packages.", tracking_id
            )

    async def update_package(
        self, tracking_id: str, uuid: str | None, uuid_timestamp: datetime | None
    ) -> None:
        """Update a single package."""
        if isinstance(uuid_timestamp, str):
            uuid_timestamp = datetime.fromisoformat(uuid_timestamp)

        uuid_expired = False
        if uuid_timestamp:
            time_since_uuid = datetime.now() - uuid_timestamp
            if time_since_uuid > timedelta(minutes=30):
                uuid_expired = True
                _LOGGER.debug("UUID for %s is expired.", tracking_id)
        else:
            uuid_expired = True

        if uuid_expired or not uuid:
            new_uuid, new_uuid_timestamp, shipment_data = await self.get_new_uuid(
                tracking_id
            )
            if shipment_data:
                eta_days_range, eta_date_range = self._get_eta_ranges(shipment_data)
                expected_delivery = self._get_expected_delivery(shipment_data)
                location = self._get_location(shipment_data)

                existing_package_data = self.tracked_packages.get(tracking_id, {})
                package_data = {
                    **existing_package_data,
                    "status": shipment_data.get("status", "unknown"),
                    "message": shipment_data.get("lastState", {}).get(
                        "status", "No status available"
                    ),
                    "location": location,
                    "origin": shipment_data.get("origin"),
                    "destination": shipment_data.get("destination"),
                    "eta_days_range": eta_days_range,
                    "eta_date_range": eta_date_range,
                    "expected_delivery": expected_delivery,
                    "carrier": shipment_data.get("detectedCarrier", {}).get("name"),
                    "days_in_transit": next(
                        (
                            attr.get("val")
                            for attr in shipment_data.get("attributes", [])
                            if attr.get("l") == "days_transit"
                        ),
                        None,
                    ),
                    "last_updated": datetime.now().isoformat(),
                }
                self.tracked_packages[tracking_id] = package_data
                await self._save_tracked_packages()
                return
            elif new_uuid:
                package_data = self.tracked_packages.get(tracking_id, {})
                package_data["uuid"] = new_uuid
                package_data["uuid_timestamp"] = new_uuid_timestamp
                self.tracked_packages[tracking_id] = package_data
                await self._save_tracked_packages()
                uuid = new_uuid
                uuid_timestamp = new_uuid_timestamp
            else:
                _LOGGER.error(
                    "Failed to get new UUID or shipment data for %s", tracking_id
                )
                return

        await self._fetch_shipment_data(tracking_id, uuid)

    async def update_tracked_packages(self) -> None:
        """Update all tracked packages."""
        for tracking_id, package_data in self.tracked_packages.items():
            if package_data.get("status") not in ["delivered", "archived"]:
                await self.update_package(
                    tracking_id,
                    package_data.get("uuid"),
                    package_data.get("uuid_timestamp"),
                )

    async def _async_update_data(self):
        """Fetch data from API endpoint and update tracked packages."""
        status_data = await self._fetch_parcels_app_status()
        await self.update_tracked_packages()
        return {
            "parcels_app_status": status_data,
            "tracked_packages": self.tracked_packages,
        }

    async def _fetch_parcels_app_status(self):
        """Fetch Parcels App status."""
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
            raise UpdateFailed(f"Error communicating with API: {err}") from err

    async def get_new_uuid(self, tracking_id: str):
        """Get a new UUID for a tracking ID or return shipment data if already available."""
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
                elif "shipments" in data and data["shipments"]:
                    shipment = data["shipments"][0]
                    return None, None, shipment
                else:
                    _LOGGER.error(
                        "Unexpected API response when getting new UUID for %s. Response: %s",
                        tracking_id,
                        response_text,
                    )
                    return None, None, None
        except aiohttp.ClientError as err:
            _LOGGER.error("Error getting new UUID for %s: %s", tracking_id, err)
            return None, None, None

    async def _fetch_shipment_data(self, tracking_id: str, uuid: str) -> None:
        """Fetch shipment data using UUID and update package data."""
        url = (
            f"https://parcelsapp.com/api/v3/shipments/tracking?"
            f"uuid={uuid}&apiKey={self.api_key}&language={self.language}"
        )

        try:
            async with self.session.get(url) as response:
                response.raise_for_status()
                data = await response.json()

                if data.get("done") and data.get("shipments"):
                    shipment = data["shipments"][0]
                    eta_days_range, eta_date_range = self._get_eta_ranges(shipment)
                    expected_delivery = self._get_expected_delivery(shipment)
                    location = self._get_location(shipment)

                    existing_package_data = self.tracked_packages.get(tracking_id, {})
                    package_data = {
                        **existing_package_data,
                        "status": shipment.get("status", "unknown"),
                        "message": shipment.get("lastState", {}).get(
                            "status", "No status available"
                        ),
                        "location": location,
                        "origin": shipment.get("origin"),
                        "destination": shipment.get("destination"),
                        "eta_days_range": eta_days_range,
                        "eta_date_range": eta_date_range,
                        "expected_delivery": expected_delivery,
                        "carrier": shipment.get("detectedCarrier", {}).get("name"),
                        "days_in_transit": next(
                            (
                                attr.get("val")
                                for attr in shipment.get("attributes", [])
                                if attr.get("l") == "days_transit"
                            ),
                            None,
                        ),
                        "last_updated": datetime.now().isoformat(),
                    }
                    self.tracked_packages[tracking_id] = package_data
                    await self._save_tracked_packages()
                else:
                    _LOGGER.debug("Tracking data not yet available for %s", tracking_id)
        except aiohttp.ClientError as err:
            _LOGGER.error("Error updating package %s: %s", tracking_id, err)
