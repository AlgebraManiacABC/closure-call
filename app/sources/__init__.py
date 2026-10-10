"""Clients for the ground-truth data sources: NWS (alerts, gridpoint forecast, observations),
NOAA NWPS (river gauges with official flood stages and crest forecasts) and USGS (raw gauge
readings). Plain code only, no model involved.

Every fetch returns a `Fetched` record holding the raw body, URL and UTC fetch time, so the
decision record can store exactly what was known and when.
"""
