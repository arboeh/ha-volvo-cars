"""The Volvo Cars integration."""

from datetime import UTC, date, datetime, time
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_registry import async_get
from homeassistant.helpers.event import async_track_utc_time_change

from .config_flow import VolvoCarsFlowHandler, get_setting
from .const import (
    CONF_VCC_API_KEY,
    CONF_VIN,
    OPT_FUEL_CONSUMPTION_UNIT,
    OPT_UNIT_LITER_PER_100KM,
    PLATFORMS,
)
from .coordinator import (
    TokenCoordinator,
    VolvoCarsConfigEntry,
    VolvoCarsData,
    VolvoCarsDataCoordinator,
)
from .entity import get_entity_id
from .store import VolvoCarsStoreManager
from .volvo.api import VolvoCarsApi
from .volvo.auth import VolvoCarsAuthApi

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: VolvoCarsConfigEntry) -> bool:
    """Set up Volvo Cars integration."""
    _LOGGER.debug("%s - Loading entry", entry.entry_id)

    # Load store
    assert entry.unique_id is not None
    store = VolvoCarsStoreManager(hass, entry.unique_id)
    await store.async_load()

    # Create APIs
    client = async_get_clientsession(hass)
    api = VolvoCarsApi(
        client,
        get_setting(entry, CONF_VIN),
        get_setting(entry, CONF_VCC_API_KEY),
    )
    auth_api = VolvoCarsAuthApi(client, api.update_access_token)

    # Setup token refresh
    token_coordinator = TokenCoordinator(hass, entry, store, auth_api)
    await token_coordinator.async_schedule_refresh(True)

    # Setup data coordinator
    coordinator = VolvoCarsDataCoordinator(hass, entry, store, api)

    # Reset API count if it the auto-reset was missed
    await _async_reset_request_count_if_missed(
        store.data["api_requests_reset_time"], coordinator
    )

    # Setup entry
    entry.runtime_data = VolvoCarsData(coordinator, token_coordinator, store)
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register events
    entry.async_on_unload(entry.add_update_listener(_options_update_listener))
    entry.async_on_unload(
        async_track_utc_time_change(
            hass, coordinator.async_reset_request_count, hour=0, minute=0, second=0
        )
    )

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: VolvoCarsConfigEntry) -> bool:
    """Migrate entry."""
    _LOGGER.debug(
        "%s - Migrating configuration from version %s.%s",
        entry.entry_id,
        entry.version,
        entry.minor_version,
    )

    if entry.version > VolvoCarsFlowHandler.VERSION:
        # This means the user has downgraded from a future version
        return False

    if entry.version == 1:
        new_data = {**entry.data}
        new_options = {**entry.options}

        if entry.minor_version < 2:
            new_options[OPT_FUEL_CONSUMPTION_UNIT] = OPT_UNIT_LITER_PER_100KM
            _remove_old_entities(hass, entry.runtime_data.coordinator)

        if entry.minor_version < 3:
            if CONF_ACCESS_TOKEN in new_data and "refresh_token" in new_data:
                assert entry.unique_id is not None
                store = VolvoCarsStoreManager(hass, entry.unique_id)
                await store.async_update(
                    access_token=new_data.pop(CONF_ACCESS_TOKEN),
                    refresh_token=new_data.pop("refresh_token"),
                )

            if CONF_PASSWORD in new_data:
                new_data.pop(CONF_PASSWORD)

        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            options=new_options,
            version=VolvoCarsFlowHandler.VERSION,
            minor_version=VolvoCarsFlowHandler.MINOR_VERSION,
        )

    _LOGGER.debug(
        "%s - Migration to configuration version %s.%s successful",
        entry.entry_id,
        entry.version,
        entry.minor_version,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VolvoCarsConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("%s - Unloading entry", entry.entry_id)
    entry.runtime_data.token_coordinator.cancel_refresh()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove a config entry."""
    _LOGGER.debug("%s - Removing entry", entry.entry_id)

    # entry.runtime_data does not exist at this time. Creating a new
    # store manager to delete it the storage data.
    if entry.unique_id:
        store = VolvoCarsStoreManager(hass, entry.unique_id)
        await store.async_remove()


async def _async_reset_request_count_if_missed(
    last_reset_time: str | None, coordinator: VolvoCarsDataCoordinator
) -> None:
    if not last_reset_time:
        return

    now = datetime.now(UTC)
    most_recent_midnight = datetime.combine(
        date(now.year, now.month, now.day), time(0, 0, 0, tzinfo=UTC)
    )
    reset_time = datetime.fromisoformat(last_reset_time)

    if reset_time < most_recent_midnight:
        await coordinator.async_reset_request_count()


async def _options_update_listener(
    hass: HomeAssistant, entry: VolvoCarsConfigEntry
) -> None:
    """Reload entry after config changes."""
    await hass.config_entries.async_reload(entry.entry_id)


def _remove_old_entities(
    hass: HomeAssistant, coordinator: VolvoCarsDataCoordinator
) -> None:
    old_entities: tuple[tuple[Platform, str], ...] = (
        (Platform.BINARY_SENSOR, "availability"),
        (Platform.BINARY_SENSOR, "front_left_door"),
        (Platform.BINARY_SENSOR, "front_right_door"),
        (Platform.BINARY_SENSOR, "rear_left_door"),
        (Platform.BINARY_SENSOR, "rear_right_door"),
        (Platform.BINARY_SENSOR, "front_left_tyre"),
        (Platform.BINARY_SENSOR, "front_right_tyre"),
        (Platform.BINARY_SENSOR, "rear_left_tyre"),
        (Platform.BINARY_SENSOR, "rear_right_tyre"),
        (Platform.BINARY_SENSOR, "front_left_window"),
        (Platform.BINARY_SENSOR, "front_right_window"),
        (Platform.BINARY_SENSOR, "rear_left_window"),
        (Platform.BINARY_SENSOR, "rear_right_window"),
        (Platform.SENSOR, "engine_hours_to_service"),
    )

    er = async_get(hass)

    for old_entity in old_entities:
        old_id = get_entity_id(coordinator, old_entity[0], old_entity[1])
        entry = er.async_get(old_id)

        if entry:
            _LOGGER.debug("Removing %s", entry.entity_id)
            er.async_remove(entry.entity_id)
