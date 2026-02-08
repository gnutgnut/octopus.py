"""Octopus Energy REST + GraphQL API client."""

import re
import logging
from datetime import datetime, timezone, timedelta

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.octopus.energy/v1"
GQL_URL = "https://api.octopus.energy/v1/graphql/"

# E-1R-VAR-22-11-01-C  ->  VAR-22-11-01
# E-1R-AGILE-FLEX-22-11-25-C  ->  AGILE-FLEX-22-11-25
TARIFF_RE = re.compile(r"^[EG]-[12]R-(.+)-[A-P]$")


class OctopusAPIError(Exception):
    """Raised when an API request fails."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def extract_product_code(tariff_code: str) -> str:
    m = TARIFF_RE.match(tariff_code)
    if not m:
        raise ValueError(f"Cannot extract product code from tariff: {tariff_code}")
    return m.group(1)


class OctopusAPI:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.auth = (api_key, "")
        self.session.headers["Accept"] = "application/json"

    def _get(self, url: str, params: dict | None = None) -> dict:
        log.debug("GET %s params=%s", url, params)
        resp = self.session.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            raise OctopusAPIError(
                f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        return resp.json()

    def _get_paginated(self, url: str, params: dict | None = None) -> list[dict]:
        """Fetch all pages of a paginated endpoint, returning combined results."""
        all_results = []
        while url:
            data = self._get(url, params=params)
            all_results.extend(data.get("results", []))
            url = data.get("next")
            # After the first request, next URL includes query params already
            params = None
        log.debug("Paginated fetch returned %d total results", len(all_results))
        return all_results

    # ── Account details ──────────────────────────────────────────────

    def get_account(self, account_number: str) -> dict:
        return self._get(f"{BASE_URL}/accounts/{account_number}/")

    def get_electricity_details(self, account_number: str) -> dict:
        """Extract first electricity meter point details from account.

        Returns dict with keys: mpan, serial, tariff_code
        """
        account = self.get_account(account_number)
        for prop in account.get("properties", []):
            for mp in prop.get("electricity_meter_points", []):
                mpan = mp.get("mpan")
                meters = mp.get("meters", [])
                agreements = mp.get("agreements", [])
                if not (mpan and meters and agreements):
                    continue
                serial = meters[-1].get("serial_number")
                # Find current agreement
                now = datetime.now(timezone.utc).isoformat()
                tariff_code = None
                for ag in sorted(agreements, key=lambda a: a.get("valid_from", ""),
                                 reverse=True):
                    valid_to = ag.get("valid_to")
                    if valid_to is None or valid_to > now:
                        tariff_code = ag.get("tariff_code")
                        break
                if not tariff_code and agreements:
                    tariff_code = agreements[-1].get("tariff_code")
                return {
                    "mpan": mpan,
                    "serial": serial,
                    "tariff_code": tariff_code,
                }
        raise OctopusAPIError("No electricity meter points found on account")

    # ── Consumption ──────────────────────────────────────────────────

    def get_consumption(self, mpan: str, serial: str,
                        period_from: str | None = None,
                        period_to: str | None = None) -> list[dict]:
        url = f"{BASE_URL}/electricity-meter-points/{mpan}/meters/{serial}/consumption/"
        params = {"page_size": 25000, "order_by": "period"}
        if period_from:
            params["period_from"] = period_from
        if period_to:
            params["period_to"] = period_to
        return self._get_paginated(url, params)

    # ── Unit rates ───────────────────────────────────────────────────

    def get_unit_rates(self, tariff_code: str,
                       period_from: str | None = None,
                       period_to: str | None = None) -> list[dict]:
        product = extract_product_code(tariff_code)
        url = (f"{BASE_URL}/products/{product}/"
               f"electricity-tariffs/{tariff_code}/standard-unit-rates/")
        params = {"page_size": 25000}
        if period_from:
            params["period_from"] = period_from
        if period_to:
            params["period_to"] = period_to
        return self._get_paginated(url, params)

    # ── Standing charges ─────────────────────────────────────────────

    def get_standing_charges(self, tariff_code: str,
                             period_from: str | None = None,
                             period_to: str | None = None) -> list[dict]:
        product = extract_product_code(tariff_code)
        url = (f"{BASE_URL}/products/{product}/"
               f"electricity-tariffs/{tariff_code}/standing-charges/")
        params = {"page_size": 25000}
        if period_from:
            params["period_from"] = period_from
        if period_to:
            params["period_to"] = period_to
        return self._get_paginated(url, params)

    # ── GraphQL (Home Mini live telemetry) ────────────────────────────

    def get_graphql_token(self) -> str:
        """Obtain a Kraken JWT token using the REST API key."""
        query = """
        mutation obtainKrakenToken($input: ObtainJSONWebTokenInput!) {
            obtainKrakenToken(input: $input) { token }
        }
        """
        resp = requests.post(GQL_URL, json={
            "query": query,
            "variables": {"input": {"APIKey": self.session.auth[0]}},
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        errors = data.get("errors")
        if errors:
            raise OctopusAPIError(f"GraphQL error: {errors[0].get('message', errors)}")
        token = data["data"]["obtainKrakenToken"]["token"]
        log.debug("Obtained GraphQL token (%d chars)", len(token))
        return token

    def get_live_demand(self, gql_token: str, device_id: str) -> dict | None:
        """Query last 5 min of smartMeterTelemetry, return latest reading.

        Returns dict with keys 'demand' (watts) and 'readAt' (ISO timestamp),
        or None if no data available.
        """
        now = datetime.now(timezone.utc)
        start = (now - timedelta(minutes=5)).isoformat()
        end = now.isoformat()

        query = """
        query smartMeterTelemetry($deviceId: String!, $start: DateTime!, $end: DateTime!) {
            smartMeterTelemetry(deviceId: $deviceId, grouping: TEN_SECONDS, start: $start, end: $end) {
                readAt
                demand
                consumptionDelta
            }
        }
        """
        resp = requests.post(GQL_URL, json={
            "query": query,
            "variables": {"deviceId": device_id, "start": start, "end": end},
        }, headers={"Authorization": gql_token}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        errors = data.get("errors")
        if errors:
            raise OctopusAPIError(f"GraphQL error: {errors[0].get('message', errors)}")

        readings = data["data"]["smartMeterTelemetry"]
        if not readings:
            log.debug("No live telemetry data in last 5 minutes")
            return None

        latest = readings[-1]
        demand = float(latest["demand"])
        log.debug("Live demand: %sW at %s", demand, latest["readAt"])
        return {"demand": demand, "readAt": latest["readAt"]}
