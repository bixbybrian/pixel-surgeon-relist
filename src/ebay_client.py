"""
eBay API wrapper.

Everything in here targets the specific calls confirmed against eBay's
official Trading API documentation on 2026-09-11 (not yet tested against a
real account - see README "Before this runs live"):

  - OAuth: standard REST token exchange (unchanged from before) - the same
    OAuth user access token is also accepted by the classic Trading API via
    the X-EBAY-API-IAF-TOKEN header, per eBay's "Using the XML version of
    the Trading API" guide.
  - Listing age: GetItem -> Item/ListingDetails/StartTime.
  - End + relist: EndFixedPriceItem (ends an *active* listing) followed by
    RelistFixedPriceItem (relists an *ended* listing, within 90 days, as a
    brand-new listing with a new ItemID - title/description/price can all
    be changed in this same call).

STILL UNCONFIRMED (needs a real Sandbox test, not guessable from docs alone):
  - Exact OAuth scope required for Trading API calls under X-EBAY-API-IAF-TOKEN
    - eBay's per-method docs don't list one for these legacy calls. Defaulting
      to the base scope below; widen if a Sandbox call comes back Unauthorized.
  - Which EndingReason value eBay actually wants for "ending on purpose to
    relist," since none of the documented enum values describe that exact
    scenario (NotAvailable, Incorrect, LostOrBroken, OtherListingError, ...).
    Defaulting to NotAvailable as the closest fit; reconsider after reading
    eBay's response Errors on a real test call, and see the README caution
    about eBay's own Search team publicly questioning whether frequent
    end-and-relist helps at all.
  - The current X-EBAY-API-COMPATIBILITY-LEVEL (set below to a documented
    example value, 1131) should be bumped to whatever eBay's docs list as
    current before going live - this number increments over time.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

EBAY_ENV = os.getenv("EBAY_ENV", "sandbox").lower()  # "sandbox" or "production"

if EBAY_ENV == "production":
    OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
    TRADING_API_URL = "https://api.ebay.com/ws/api.dll"
else:
    OAUTH_URL = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
    TRADING_API_URL = "https://api.sandbox.ebay.com/ws/api.dll"

EBAY_SELL_INVENTORY_URL = (
    "https://api.ebay.com/sell/inventory/v1/inventory_item"
    if EBAY_ENV == "production"
    else "https://api.sandbox.ebay.com/sell/inventory/v1/inventory_item"
)

EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")
EBAY_REFRESH_TOKEN = os.getenv("EBAY_REFRESH_TOKEN")

# Base scope is what's documented as needed for OAuth-authenticated Trading
# API calls in general; sell.inventory/sell.account are kept too since
# fetch_inventory() below still uses the REST Sell API. UNCONFIRMED against
# a real account - see module docstring.
SCOPES = (
    "https://api.ebay.com/oauth/api_scope "
    "https://api.ebay.com/oauth/api_scope/sell.inventory "
    "https://api.ebay.com/oauth/api_scope/sell.account"
)

TRADING_API_COMPATIBILITY_LEVEL = os.getenv("EBAY_TRADING_API_COMPATIBILITY_LEVEL", "1131")
TRADING_API_SITE_ID = os.getenv("EBAY_TRADING_API_SITE_ID", "0")  # 0 = ebay.com (US)

# See module docstring - genuinely unconfirmed, closest documented fit.
DEFAULT_ENDING_REASON = os.getenv("EBAY_ENDING_REASON", "NotAvailable")

NS = "{urn:ebay:apis:eBLBaseComponents}"


def credentials_present() -> bool:
    return bool(EBAY_CLIENT_ID and EBAY_CLIENT_SECRET and EBAY_REFRESH_TOKEN)


def get_access_token() -> str:
    """Exchanges the long-lived refresh token for a short-lived access token."""
    if not credentials_present():
        raise RuntimeError(
            "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET / EBAY_REFRESH_TOKEN are not set. "
            "See README.md 'eBay Developer setup' before running live."
        )
    response = requests.post(
        OAUTH_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": EBAY_REFRESH_TOKEN,
            "scope": SCOPES,
        },
        auth=(EBAY_CLIENT_ID, EBAY_CLIENT_SECRET),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["access_token"]


@dataclass
class RelistResult:
    old_item_id: str | None
    new_item_id: str
    success: bool


@dataclass
class ItemDetails:
    item_id: str
    title: str
    start_time: str | None  # ISO-ish string straight from eBay, or None if absent
    price: float | None
    description: str | None = None


class EbayApiError(RuntimeError):
    """Raised when a Trading API call returns Ack=Failure. Carries eBay's own
    error messages so a failed run's logs actually explain what happened."""


def _trading_headers(access_token: str, call_name: str) -> dict:
    return {
        "X-EBAY-API-COMPATIBILITY-LEVEL": TRADING_API_COMPATIBILITY_LEVEL,
        "X-EBAY-API-CALL-NAME": call_name,
        "X-EBAY-API-SITEID": TRADING_API_SITE_ID,
        "X-EBAY-API-IAF-TOKEN": access_token,
        "Content-Type": "text/xml",
    }


def _trading_call(access_token: str, call_name: str, request_element: ET.Element) -> ET.Element:
    """POSTs one Trading API XML request and returns the parsed response root.
    Raises EbayApiError if eBay reports Ack=Failure."""
    body = b'<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(
        request_element, encoding="utf-8"
    )
    response = requests.post(
        TRADING_API_URL,
        headers=_trading_headers(access_token, call_name),
        data=body,
        timeout=30,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)

    ack = root.findtext(f"{NS}Ack")
    if ack == "Failure":
        errors = []
        for err in root.findall(f"{NS}Errors"):
            short = err.findtext(f"{NS}ShortMessage", default="")
            long = err.findtext(f"{NS}LongMessage", default="")
            errors.append(short or long or "unknown error")
        raise EbayApiError(f"{call_name} failed: " + "; ".join(errors))
    return root


def get_item(access_token: str, item_id: str) -> ItemDetails:
    """GetItem -> pulls the real listing start time (and current title/price)
    for one item. This is the doc-confirmed source of listing age (see
    module docstring) - the Sell Inventory API's inventory_item resource
    does not carry this."""
    req = ET.Element("GetItemRequest", xmlns="urn:ebay:apis:eBLBaseComponents")
    ET.SubElement(req, "ItemID").text = item_id
    ET.SubElement(req, "DetailLevel").text = "ReturnAll"

    root = _trading_call(access_token, "GetItem", req)
    item = root.find(f"{NS}Item")
    if item is None:
        raise EbayApiError(f"GetItem returned no Item for {item_id}")

    title = item.findtext(f"{NS}Title", default="")
    start_time = item.findtext(f"{NS}ListingDetails/{NS}StartTime")
    price_text = item.findtext(f"{NS}StartPrice")
    price = float(price_text) if price_text else None
    description = item.findtext(f"{NS}Description")

    return ItemDetails(
        item_id=item_id, title=title, start_time=start_time, price=price,
        description=description,
    )


def get_active_listings(access_token: str, entries_per_page: int = 100) -> list[dict]:
    """GetMyeBaySelling -> every currently-active listing, regardless of how
    long ago it started (no time-window restriction, unlike GetSellerList -
    important since GTC fixed-price listings can be active for a very long
    time, and that's exactly what this tool needs to find). Paginates until
    all pages are read. Returns light records (ItemID/Title/StartTime/
    CurrentPrice/Quantity) - full Description isn't included here, see
    get_item() for that."""
    results: list[dict] = []
    page = 1
    while True:
        req = ET.Element("GetMyeBaySellingRequest", xmlns="urn:ebay:apis:eBLBaseComponents")
        active = ET.SubElement(req, "ActiveList")
        ET.SubElement(active, "Sort").text = "TimeLeft"
        pagination = ET.SubElement(active, "Pagination")
        ET.SubElement(pagination, "EntriesPerPage").text = str(entries_per_page)
        ET.SubElement(pagination, "PageNumber").text = str(page)

        root = _trading_call(access_token, "GetMyeBaySelling", req)
        active_list = root.find(f"{NS}ActiveList")
        if active_list is None:
            break

        item_array = active_list.find(f"{NS}ItemArray")
        if item_array is not None:
            for item in item_array.findall(f"{NS}Item"):
                item_id = item.findtext(f"{NS}ItemID")
                title = item.findtext(f"{NS}Title", default="")
                start_time = item.findtext(f"{NS}ListingDetails/{NS}StartTime")
                price_text = item.findtext(f"{NS}SellingStatus/{NS}CurrentPrice")
                price = float(price_text) if price_text else None
                results.append(
                    {
                        "item_id": item_id,
                        "title": title,
                        "start_time": start_time,
                        "price": price,
                    }
                )

        pagination_result = active_list.find(f"{NS}PaginationResult")
        total_pages = (
            int(pagination_result.findtext(f"{NS}TotalNumberOfPages", default="1"))
            if pagination_result is not None
            else 1
        )
        if page >= total_pages:
            break
        page += 1

    return results


def end_fixed_price_item(
    access_token: str, item_id: str, ending_reason: str = DEFAULT_ENDING_REASON
) -> None:
    """Ends an ACTIVE fixed-price listing early. Required before
    RelistFixedPriceItem can be called on it."""
    req = ET.Element("EndFixedPriceItemRequest", xmlns="urn:ebay:apis:eBLBaseComponents")
    ET.SubElement(req, "ItemID").text = item_id
    ET.SubElement(req, "EndingReason").text = ending_reason
    _trading_call(access_token, "EndFixedPriceItem", req)


def relist_fixed_price_item(
    access_token: str,
    ended_item_id: str,
    new_title: str,
    new_description: str,
    new_price: float,
) -> str:
    """Relists an ENDED listing (must be within 90 days of ending) as a
    brand-new listing with a new ItemID, with the new title/description/
    price applied. Returns the new ItemID."""
    req = ET.Element("RelistFixedPriceItemRequest", xmlns="urn:ebay:apis:eBLBaseComponents")
    item = ET.SubElement(req, "Item")
    ET.SubElement(item, "ItemID").text = ended_item_id
    ET.SubElement(item, "Title").text = new_title
    ET.SubElement(item, "Description").text = new_description
    ET.SubElement(item, "StartPrice").text = f"{new_price:.2f}"

    root = _trading_call(access_token, "RelistFixedPriceItem", req)
    new_item_id = root.findtext(f"{NS}ItemID")
    if not new_item_id:
        raise EbayApiError("RelistFixedPriceItem succeeded but returned no new ItemID")
    return new_item_id


def end_and_relist(
    access_token: str,
    listing: dict,
    new_title: str,
    new_description: str,
    new_price: float,
) -> RelistResult:
    """The full refresh: end the currently-active listing, then relist it
    fresh with the new title/description/price. NOT YET TESTED against a
    real (even Sandbox) eBay account - see module docstring for the two
    remaining unconfirmed details (OAuth scope, EndingReason value) that a
    real test call will settle. Run a single item through this manually
    against Sandbox before trusting the weekly cron with it."""
    old_item_id = listing["ebay_item_id"]
    end_fixed_price_item(access_token, old_item_id)
    new_item_id = relist_fixed_price_item(
        access_token, old_item_id, new_title, new_description, new_price
    )
    return RelistResult(old_item_id=old_item_id, new_item_id=new_item_id, success=True)


def fetch_inventory(access_token: str) -> list[dict]:
    """Sell REST API inventory list - still useful for bulk SKU/title
    discovery, just not for listing dates (see module docstring)."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    response = requests.get(
        f"{EBAY_SELL_INVENTORY_URL}?limit=100", headers=headers, timeout=30
    )
    response.raise_for_status()
    return response.json().get("inventoryItems", [])
