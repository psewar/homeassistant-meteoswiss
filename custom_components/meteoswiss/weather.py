"""Support for the MeteoSwiss service."""

from __future__ import annotations

import datetime
import logging
from typing import Any, cast

from hamsclientfork.client import CurrentCondition, DayForecast, HourlyForecast
from homeassistant.components.weather import (
    ATTR_FORECAST_CONDITION,
    ATTR_FORECAST_NATIVE_PRECIPITATION,
    ATTR_FORECAST_NATIVE_TEMP,
    ATTR_FORECAST_NATIVE_TEMP_LOW,
    ATTR_FORECAST_TIME,
    Forecast,
    SingleCoordinatorWeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import sun
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.meteoswiss import (
    MeteoSwissDataUpdateCoordinator,
)
from custom_components.meteoswiss.const import (
    CODE_TO_CONDITION_MAP,
    CONF_FORECAST_NAME,
    CONF_POSTCODE,
    CONF_PRECIPITATION_STATION,
    CONF_REAL_TIME_NAME,
    CONF_REAL_TIME_PRECIPITATION_NAME,
    CONF_STATION,
    Condition,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# The forecast API occasionally reports this value (0x7FFF == SHRT_MAX) as a
# "no data" sentinel for the current-weather icon.
NO_ICON_SENTINEL = 32767


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up weather entity."""
    c: MeteoSwissDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MeteoSwissWeather(entry.entry_id, c)], True)


def condition_from_icon(icon: int | None) -> str | None:
    """Map a MeteoSwiss icon id to a Home Assistant condition.

    Returns ``None`` when the icon is missing, the ``32767`` sentinel, or
    otherwise not present in the mapping, so callers can fall back gracefully
    instead of surfacing an invalid condition.
    """
    if icon is None or icon == NO_ICON_SENTINEL:
        return None
    mapped = CODE_TO_CONDITION_MAP.get(icon)
    if mapped is None:
        return None
    return str(mapped[0]) or None


def condition_name_to_first_value(
    condition: None | list[CurrentCondition], name: str
) -> float | None:
    if not condition:
        # Real-time weather station provides no data.
        _LOGGER.debug("Current condition is empty for all stations: %s", condition)
        return None
    for n, row in enumerate(condition):
        try:
            value = row[name]  # type:ignore[literal-required]
        except Exception:
            _LOGGER.exception(
                "Current condition %s (%s) has no value for %s", n, row, name
            )
            continue
        if value is None or value == "-":
            _LOGGER.debug(
                "Value %s of current condition %s (%s) is %s, so not available",
                name,
                n,
                row,
                value,
            )
            continue
        try:
            return float(value)
        except Exception:
            _LOGGER.exception(
                "Error converting %s to float for condition in row %s (%s)",
                value,
                n,
                row,
            )
            continue
    return None


class MeteoSwissWeather(
    SingleCoordinatorWeatherEntity[MeteoSwissDataUpdateCoordinator],
):
    _attr_has_entity_name = True
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_DAILY | WeatherEntityFeature.FORECAST_HOURLY
    )

    def __init__(
        self,
        integration_id: str,
        coordinator: MeteoSwissDataUpdateCoordinator,
    ):
        super().__init__(coordinator)
        self._attr_unique_id = "weather.%s" % integration_id
        self._attr_post_code = coordinator.data[CONF_POSTCODE]
        self._attr_station = coordinator.data[CONF_STATION]
        self._attr_weather_station = self._attr_station
        self._attr_weather_station_name = coordinator.data[CONF_REAL_TIME_NAME]
        self._attr_precipitation_station = coordinator.data[CONF_PRECIPITATION_STATION]
        self._attr_precipitation_station_name = coordinator.data[
            CONF_REAL_TIME_PRECIPITATION_NAME
        ]

    # Data is read live from the coordinator so the entity always reflects the
    # latest poll.  SingleCoordinatorWeatherEntity takes care of writing state
    # and notifying forecast subscribers on every coordinator update, so there
    # is no need to override _handle_coordinator_update or cache anything.

    @property
    def _forecast_data(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if not data:
            return None
        return data.get("forecast") or None

    @property
    def _condition_for_all_stations(self) -> list[CurrentCondition] | None:
        data = self.coordinator.data
        return data.get("condition") if data else None

    @property
    def name(self) -> Any:
        data = self.coordinator.data
        return data.get(CONF_FORECAST_NAME) if data else None

    @property
    def native_temperature(self) -> float | None:
        return condition_name_to_first_value(
            self._condition_for_all_stations, "tre200s0"
        )

    @property
    def native_pressure(self) -> float | None:
        return condition_name_to_first_value(
            self._condition_for_all_stations, "prestas0"
        )

    @property
    def pressure_qff(self) -> float | None:
        return condition_name_to_first_value(
            self._condition_for_all_stations, "pp0qffs0"
        )

    @property
    def pressure_qnh(self) -> float | None:
        return condition_name_to_first_value(
            self._condition_for_all_stations, "pp0qnhs0"
        )

    @property
    def humidity(self) -> float | None:
        return condition_name_to_first_value(
            self._condition_for_all_stations, "ure200s0"
        )

    @property
    def native_wind_speed(self) -> float | None:
        return condition_name_to_first_value(
            self._condition_for_all_stations, "fu3010z0"
        )

    @property
    def wind_bearing(self) -> float | None:
        return condition_name_to_first_value(
            self._condition_for_all_stations, "dkl010z0"
        )

    # FIXME add precipitation conditions above.

    @property
    def condition(self) -> str | None:
        """Return the current weather condition.

        Uses the current-weather icon when available.  When the API does not
        provide one (e.g. the 32767 sentinel) we fall back to today's daily
        forecast icon so the entity still reports a sensible condition rather
        than going unavailable.
        """
        forecast = self._forecast_data
        if not forecast:
            return None
        current = forecast.get("currentWeather") or {}
        cond = condition_from_icon(current.get("icon"))
        if cond is None:
            region = forecast.get("regionForecast") or []
            if region:
                today = cast(DayForecast, region[0])
                cond = condition_from_icon(today.get("iconDay"))
                # The daily icon is always a day icon; correct it at night.
                if cond == Condition.sunny and not sun.is_up(self.hass):
                    cond = str(Condition.clear_night)
        return cond

    @property
    def attribution(self) -> str:
        a = "Data provided by MeteoSwiss."
        a += "  Forecasts from postal code %s." % (self._attr_post_code,)
        if self._attr_weather_station:
            a += "  Real-time weather data from weather station %s (%s)." % (
                self._attr_weather_station,
                self._attr_weather_station_name,
            )
        if self._attr_precipitation_station:
            a += "  Real-time weather data from weather station %s (%s)." % (
                self._attr_precipitation_station,
                self._attr_precipitation_station_name,
            )
        if self._attr_weather_station or self._attr_precipitation_station:
            url = "https://rudd-o.com/meteostations"
            a += "  Stations available at %s ." % (url,)
        else:
            a += "  No real-time stations used by this weather entry."
        return a

    def _condition_by_day(self) -> dict[str, str | None]:
        """Map each forecast day (YYYY-MM-DD) to its HA condition."""
        forecast = self._forecast_data
        if not forecast:
            return {}
        result: dict[str, str | None] = {}
        for untyped in forecast.get("regionForecast") or []:
            day = cast(DayForecast, untyped)
            result[str(day["dayDate"])[:10]] = condition_from_icon(day.get("iconDay"))
        return result

    def _daily_forecast(self) -> list[Forecast] | None:
        forecast = self._forecast_data
        if not forecast:
            return None
        fcdata_out: list[Forecast] = []
        for untyped_forecast in forecast.get("regionForecast") or []:
            day = cast(DayForecast, untyped_forecast)
            data_out: Forecast = {
                ATTR_FORECAST_TIME: day["dayDate"],
                ATTR_FORECAST_NATIVE_TEMP: day["temperatureMax"],
                ATTR_FORECAST_NATIVE_TEMP_LOW: day["temperatureMin"],
                ATTR_FORECAST_NATIVE_PRECIPITATION: day["precipitation"],
            }
            cond = condition_from_icon(day.get("iconDay"))
            if cond is not None:
                data_out[ATTR_FORECAST_CONDITION] = cond
            fcdata_out.append(data_out)
        _LOGGER.debug("Daily forecast has %d items", len(fcdata_out))
        return fcdata_out

    def _hourly_forecast(self) -> list[Forecast] | None:
        forecast = self._forecast_data
        if not forecast:
            return None
        hourly = cast(
            list[HourlyForecast], forecast.get("regionHourlyForecast") or []
        )
        if not hourly:
            return []
        # The hourly data carries no icon, so derive each hour's condition from
        # the daily forecast for the same calendar day.
        cond_by_day = self._condition_by_day()
        now = datetime.datetime.now(datetime.timezone.utc)
        # Start one entry before the first future hour so the current hour is
        # included in the forecast.
        future = [i for i, f in enumerate(hourly) if f["time"] > now]
        start = max(0, future[0] - 1) if future else 0
        fcdata_out: list[Forecast] = []
        for forecast_hour in hourly[start:]:
            when = forecast_hour["time"]
            data_out: Forecast = {
                ATTR_FORECAST_TIME: when.isoformat(),
                ATTR_FORECAST_NATIVE_TEMP: forecast_hour["temperatureMean"],
                ATTR_FORECAST_NATIVE_PRECIPITATION: forecast_hour["precipitationMean"],
            }
            cond = cond_by_day.get(when.date().isoformat())
            if cond is not None:
                data_out[ATTR_FORECAST_CONDITION] = cond
            fcdata_out.append(data_out)
        _LOGGER.debug("Hourly forecast has %d items", len(fcdata_out))
        return fcdata_out

    @property
    def available(self) -> bool:
        """Return True if the coordinator holds a forecast for this entity."""
        forecast = self._forecast_data
        return (
            self.coordinator.last_update_success
            and forecast is not None
            and bool(forecast.get("regionForecast"))
        )

    @callback
    def _async_forecast_daily(self) -> list[Forecast] | None:
        """Return the daily forecast in native units."""
        return self._daily_forecast()

    @callback
    def _async_forecast_hourly(self) -> list[Forecast] | None:
        """Return the hourly forecast in native units."""
        return self._hourly_forecast()
