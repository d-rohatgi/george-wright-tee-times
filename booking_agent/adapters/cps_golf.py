"""Live CPS Golf adapter for George Wright.

Read path is fully working and needs no browser — plain stdlib HTTP gets an
anonymous short-lived token and queries the JSON API directly. See
docs/RECON.md for how this was derived.

Write path (book / cancel / list my bookings) needs an authenticated session
and is not implemented yet: capture a HAR of one manual booking and the
payloads drop straight in below.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta

from dataclasses import dataclass

from booking_agent.adapters.base import (
    AdapterError,
    AuthExpired,
    NotYetReleased,
    RateLimited,
    SlotUnavailable,
    TransientError,
)
from booking_agent.models import Booking, Slot

HOST = "https://georgewright.cps.golf"
API = f"{HOST}/onlineres/onlineapi/api/v1/onlinereservation"
TOKEN_URL = f"{HOST}/identityapi/myconnect/token/short"

WEBSITE_ID = "50827848-110c-4067-0beb-08da6c0028fc"
SHORT_LIVED_CLIENT_ID = "onlinereswebshortlived"

GEORGE_WRIGHT_COURSE_ID = 2
GEORGE_WRIGHT_SITE_ID = 2  # goes in request BODIES

# Tenant-level header values, identical on every endpoint in the capture.
TENANT_SITE_ID = "1"
TERMINAL_ID = "3"
MODULE_ID = "7"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)

# The server's phrasing when the date is beyond daysInAdvance. Matched loosely
# because vendors reword these; the 400 + "not able to book" shape is stable.
# Failures that arrive as something other than urllib's URLError — a read
# timeout or a reset mid-response comes straight out of resp.read().
_NETWORK_ERRORS = (TimeoutError, ConnectionError, http.client.HTTPException)

_NOT_RELEASED_MARKERS = ("not able to book", "membership only")


@dataclass
class Identity:
    """Non-sensitive account facts. Safe to keep in preferences.toml.

    Note what is *not* here: no password (Keychain), no card token (fetched
    per-booking and held in memory only). `card_id` and `card_last4` are just
    lookup keys — an account can hold two cards sharing a last-4, so both
    are required and must agree before anything is charged.
    """

    email: str
    acct: str
    card_id: int
    card_last4: str
    member_class: str = "NRES"
    rate_code: str = "NON-RES"
    golfer_id: int | None = None


class CPSGolfAdapter:
    def __init__(
        self,
        *,
        course_id: int = GEORGE_WRIGHT_COURSE_ID,
        site_id: int = GEORGE_WRIGHT_SITE_ID,
        timeout: float = 10.0,
        auth=None,
        identity: Identity | None = None,
    ) -> None:
        self.course_id = course_id
        self.site_id = site_id
        self.timeout = timeout
        self.auth = auth
        self.identity = identity
        self._token: str | None = None
        self._token_fetched_at = 0.0
        self.search_calls = 0
        # Raw API payloads from the last search, keyed by teeSheetId. Booking
        # needs fields the shared Slot model deliberately doesn't carry —
        # chiefly availableParticipantNo, which is NOT simply 1..N.
        self._raw_slots: dict[str, dict] = {}

    # -- adapter surface ---------------------------------------------------

    def search_tee_times(self, day: date) -> list[Slot]:
        self.search_calls += 1
        content = self._fetch_sheet(day)
        if not content:
            # Under load the server sometimes answers NO_TEETIMES for a sheet
            # that has tee times — 6 of 212 polls on 2026-10-06, and it made
            # `peek` show Oct 4 as empty. Confirm once, with a fresh
            # transaction, before believing it.
            content = self._fetch_sheet(day)

        slots: list[Slot] = []
        for raw in content or []:
            slot = _to_slot(raw)
            if slot is None:
                continue
            self._raw_slots[slot.id] = raw
            slots.append(slot)
        return slots

    def _fetch_sheet(self, day: date) -> list[dict] | None:
        """One sheet read: register a transaction id, then query with it.
        None when the server answers with a message instead of a list."""
        tx = str(uuid.uuid4())
        self._post("/RegisterTransactionId", {"transactionId": tx})

        qs = urllib.parse.urlencode(
            {
                "searchDate": _cps_date(day),
                "holes": 0,
                "numberOfPlayer": 0,
                "courseIds": self.course_id,
                "searchTimeType": 0,
                "transactionId": tx,
                "teeOffTimeMin": 0,
                "teeOffTimeMax": 23,
                "isChangeTeeOffTime": "true",
                "teeSheetSearchView": 5,
                "classCode": "R",
                "defaultOnlineRate": "N",
                "isUseCapacityPricing": "false",
                "memberStoreId": 1,
                "searchType": 1,
            }
        )
        payload = self._get(f"/TeeTimes?{qs}")
        content = payload.get("content")

        # An empty sheet is a *successful* response with a message object
        # (NO_TEETIMES), not an error and not a list.
        return content if isinstance(content, list) else None

    def _participant_numbers(self, slot_id: str, players: int) -> list[int]:
        """The exact seat numbers to book.

        A tee time with two seats already taken exposes
        availableParticipantNo == [3, 4], and the server rejects a booking that
        asks for seats 1 and 2 — it fails late, at ReserveTeeTimes, with
        "Value cannot be null. (Parameter 's')". Blindly sending 1..N works
        only on a completely empty tee time.
        """
        raw = self._raw_slots.get(str(slot_id)) or {}
        available = [int(n) for n in (raw.get("availableParticipantNo") or [])]
        if not available:
            available = list(range(1, players + 1))
        if len(available) < players:
            raise SlotUnavailable(
                f"slot {slot_id} has {len(available)} seats, need {players}"
            )
        return available[:players]

    def booking_rules(self, class_code: str = "R") -> dict:
        """Release window straight from the server: daysInAdvance + time.

        Read this instead of hardcoding "Tuesday 7am for Saturday" — if the
        course moves the window, the agent follows instead of silently missing.
        The window depends on member class: R/RES/NRES get 4 days, MEM gets 5
        (checked 2026-10-06), so ask for the account's own class.
        """
        qs = urllib.parse.urlencode(
            {
                "classcode": class_code,
                "courseIds": self.course_id,
                "searchDate": _cps_date(date.today()),
            }
        )
        data = self._get(f"/BookingRuleModels?{qs}")
        by_class = data.get("bookingRuleByClass") or []
        rule = {}
        if by_class:
            courses = by_class[0].get("bookingRuleByCourse") or []
            rule = next(
                (c for c in courses if c.get("courseId") == self.course_id), {}
            )
        by_course = next(
            (
                c
                for c in (data.get("bookingRuleByCourses") or [])
                if c.get("courseId") == self.course_id
            ),
            {},
        )
        release = rule.get("time", "")
        return {
            "days_in_advance": rule.get("daysInAdvance"),
            "release_time": release.split("T")[-1] if "T" in release else release,
            "max_daily_bookings": by_course.get("maximumDailyBookings"),
            "no_show_limit": by_course.get("noShowLimit"),
        }

    def target_date(self, today: date | None = None, class_code: str = "R") -> date:
        """The furthest-out date the server will currently let us book."""
        today = today or date.today()
        days = self.booking_rules(class_code).get("days_in_advance") or 4
        return today + timedelta(days=days)

    # -- write path --------------------------------------------------------

    def _require_auth(self) -> None:
        if self.auth is None:
            raise AuthExpired(
                "this operation needs a signed-in session; construct "
                "CPSGolfAdapter(auth=AuthSession(<email>), identity=<Identity>)"
            )

    @property
    def golfer_id(self) -> int:
        self._require_auth()
        if self.identity.golfer_id is None:
            info = self._get("/GetUserInformation")
            self.identity.golfer_id = int(info["golferId"])
        return self.identity.golfer_id

    def member_class_code(self) -> str:
        """The account's member class as the server sees it (e.g. NRES)."""
        self._require_auth()
        code = self._get("/GetUserInformation").get("memberClassCode", "")
        if isinstance(code, dict):  # the live API nests the whole class record
            code = code.get("class", "")
        return str(code)

    def _cards(self) -> list[dict]:
        self._require_auth()
        cards = self._get("/GetAllCreditCardOnFile")
        if isinstance(cards, dict):
            cards = cards.get("data") or cards.get("items") or []
        return cards

    @staticmethod
    def _last4(card: dict) -> str:
        masked = str(card.get("ccMaskedNumber", ""))
        return "".join(ch for ch in masked if ch.isdigit())[-4:]

    def saved_cards(self) -> list[dict]:
        """Cards on file WITHOUT their tokens — the lookup keys a new user
        copies into preferences.toml (card_id, card_last4, acct)."""
        return [
            {
                "id": c.get("id"),
                "last4": self._last4(c),
                "type": c.get("cardType"),
                "expires": c.get("cardExpire"),
                "default": bool(c.get("isDefault")),
                "acct": c.get("acct"),
            }
            for c in self._cards()
        ]

    def resolve_card(self) -> dict:
        """Look up the saved card by id, assert its last 4, return the token.

        The token is fetched fresh for each booking and held only for the
        duration of the request — never written to disk or config. Config
        stores the non-sensitive card id and last 4 only.
        """
        cards = self._cards()
        want_id = self.identity.card_id
        match = next((c for c in cards if c.get("id") == want_id), None)
        if match is None:
            available = ", ".join(
                f"{c.get('id')} (…{self._last4(c)})" for c in cards if c.get("id")
            )
            raise AdapterError(
                f"card id {want_id} is not on file (available: {available or 'none'}). "
                "Re-check config/preferences.toml."
            )

        last4 = self._last4(match)
        if last4 != str(self.identity.card_last4):
            # Refuse rather than silently charge a different card. Two cards on
            # one account can share a last-4, so id and last-4 must agree.
            raise AdapterError(
                f"card id {want_id} ends in {last4}, config expects "
                f"{self.identity.card_last4} — refusing to book"
            )
        if not match.get("ccToken"):
            raise AdapterError(f"card id {want_id} has no token")
        return match

    def resolve_card_token(self) -> str:
        return self.resolve_card()["ccToken"]

    def lock_tee_time(self, slot_id: str) -> str:
        """Claim the hold. THIS is the 07:00:00 race — it pulls the slot out of
        circulation and starts a ~10 minute window to finish the booking.

        Failure comes back as a 200 with a non-empty `error` field, not an HTTP
        error status. Checking the status code alone would silently 'succeed'
        on a lost slot and then fail confusingly three calls later.
        """
        self._require_auth()
        session_guid = str(uuid.uuid4())
        resp = self._post(
            "/LockTeeTimes",
            {
                "teeSheetIds": [int(slot_id)],
                "email": self.identity.email,
                "action": "Online Reservation V5",
                "sessionId": session_guid,
                "golferId": self.golfer_id,
                "classCode": self.identity.member_class,
                "numberOfPlayer": 0,
                "navigateUrl": "",
                "isSmartCard": False,
                "isGroupBooking": False,
            },
        )
        if resp.get("error"):
            raise SlotUnavailable(str(resp["error"])[:160])
        return resp.get("sessionId") or session_guid

    def unlock_tee_time(self, slot_id: str) -> None:
        """Release a hold we decided not to use. Always call this on abort —
        otherwise the slot sits locked for ten minutes and nobody, including
        us on a retry, can take it."""
        try:
            self._post(
                "/UnLockTeeTimes",
                {
                    "teeSheetIds": [int(slot_id)],
                    "email": self.identity.email,
                    "golferId": self.golfer_id,
                    "sessionId": None,
                },
            )
        except AdapterError:
            pass  # best-effort; the hold expires on its own

    def book_tee_time(self, slot_id: str, players: int, holes: int) -> Booking:
        self._require_auth()
        slot_key = int(slot_id)
        locked_session = self.lock_tee_time(slot_id)

        try:
            # Call order mirrors the real client exactly: limit checks, then
            # pricing, then the restriction check. Reordering these fails late
            # and opaquely at ReserveTeeTimes.
            seats = self._participant_numbers(slot_id, players)
            card = self.resolve_card()

            # Mirrors the real client: one limit check per seat, before pricing.
            for seat in seats:
                self._post(
                    "/CheckBookingLimit",
                    {
                        "golferId": self.golfer_id,
                        "playerId": "0",
                        "dependentId": "0",
                        "teesheetId": slot_key,
                        "isBuddy": False,
                        "isWriteIn": False,
                        "participantNo": seat,
                        "reservationId": 0,
                        "acct": self.identity.acct,
                        "memberClass": self.identity.member_class,
                        "transactionId": None,
                        "isPriorPlayingPartner": False,
                    },
                )

            # One entry per seat. The first is us; the rest are unassigned
            # placeholders, which is how the site books a foursome without
            # names. Unassigned entries omit dependentId and carry memberClass
            # "RES" — quirks of the real client, mirrored deliberately.
            booking_list = []
            for i, seat in enumerate(seats):
                mine = i == 0
                entry = {
                        "teeSheetId": slot_key,
                        "holes": holes,
                        "participantNo": seat,
                        "golferId": self.golfer_id,
                        "rateCode": self.identity.rate_code,
                        "isUnAssignedPlayer": not mine,
                        "memberClassCode": (
                            self.identity.member_class if mine else "RES"
                        ),
                        "memberStoreId": "1",
                        "cartType": 0,
                        "playerId": "0",
                        "acct": self.identity.acct,
                        "isGuestOf": False,
                        "isUseCapacityPricing": False,
                        "isSmartCard": False,
                }
                if mine:
                    entry["dependentId"] = "0"
                booking_list.append(entry)

            pricing = self._post(
                "/TeeTimePricesCalculation",
                {
                    "selectedTeeSheetId": slot_key,
                    "bookingList": booking_list,
                    "holes": holes,
                    "numberOfPlayer": players,
                    "numberOfRider": 0,
                    "cartType": 0,
                    "coupon": None,
                    "selectedValuePackageCode": None,
                    "isUseCapacityPricing": False,
                    "thirdPartyId": None,
                    # The FULL card record, not null and not just the token.
                    # Sending null here is accepted, and then ReserveTeeTimes
                    # fails three calls later with the useless
                    # "Value cannot be null. (Parameter 's')".
                    "ibxCardOnFile": card,
                    "advancedBookingFee": None,
                    "transactionId": None,
                    "isPrepayDeposit": False,
                },
            )

            self._post(
                "/CheckRestrictReservation",
                {
                    "teeSheetId": slot_key,
                    "courseId": self.course_id,
                    "siteId": self.site_id,
                    "classCode": "RES",
                },
            )

            # The server mints the booking transaction during pricing and hands
            # it back. Inventing a GUID here earns
            # "Not found a transaction" from ReserveTeeTimes.
            booking_tx = pricing.get("transactionId")
            if not booking_tx:
                raise AdapterError(
                    "pricing response carried no transactionId; cannot reserve"
                )

            reserve_tx = str(uuid.uuid4())
            self._post("/RegisterTransactionId", {"transactionId": reserve_tx})

            result = self._post(
                "/ReserveTeeTimes",
                {
                    "cancelReservationLink": (
                        f"{HOST}/onlineresweb/auth/verify-email"
                        "?returnUrl=cancel-booking"
                    ),
                    "homePageLink": f"{HOST}/onlineresweb/",
                    "affiliateId": None,
                    "sessionGuid": None,
                    "lockedTeeTimesSessionId": locked_session,
                    "bookingTransactionId": booking_tx,
                    "transactionId": reserve_tx,
                    "finalizeSaleModel": {
                        "acct": self.identity.acct,
                        "playerId": 0,
                        "isGuest": False,
                        "creditCardInfo": {
                            "cardToken": card["ccToken"],
                            "email": self.identity.email,
                            "cardNumber": None,
                            "cardHolder": None,
                            "expireMM": None,
                            "expireYY": None,
                            "cvv": None,
                        },
                        "monerisCC": None,
                        "ibxCC": None,
                    },
                },
            )
        except Exception:
            self.unlock_tee_time(slot_id)
            raise

        if not result.get("reservationId"):
            self.unlock_tee_time(slot_id)
            raise AdapterError(f"reservation not confirmed: {str(result)[:200]}")

        return Booking(
            id=str(result["reservationId"]),
            slot=Slot(
                id=slot_id,
                course="George Wright Golf Course",
                tee_time=datetime.now(),  # replaced by the caller's known slot
                holes=holes,
                spots=players,
            ),
            players=players,
            confirmed_at=datetime.now(),
        )

    def get_existing_bookings(self) -> list[Booking]:
        self._require_auth()
        payload = self._get("/UpcomingReservation")
        out: list[Booking] = []
        for item in payload.get("items") or []:
            try:
                tee = datetime.fromisoformat(item["startTime"])
            except (KeyError, ValueError):
                continue
            out.append(
                Booking(
                    id=str(item["reservationId"]),
                    slot=Slot(
                        id=str(item.get("teeSheetId") or ""),
                        course=item.get("courseName") or "",
                        tee_time=tee,
                        holes=int(item.get("holes") or 0),
                        spots=int(item.get("numberOfPlayer") or 0),
                    ),
                    players=int(item.get("numberOfPlayer") or 0),
                    confirmed_at=tee,
                )
            )
        return out

    def cancel_booking(self, booking_id: str) -> None:
        """Cancel by reservationId. Looks the reservation up first so the
        payload carries the exact bookingIds the server expects."""
        self._require_auth()
        payload = self._get("/UpcomingReservation")
        item = next(
            (
                i
                for i in (payload.get("items") or [])
                if str(i.get("reservationId")) == str(booking_id)
            ),
            None,
        )
        if item is None:
            raise AdapterError(f"reservation {booking_id} not found in upcoming")

        details = item.get("reservationDetail") or []
        booking_ids = [d["bookingId"] for d in details if d.get("bookingId")]
        names = [d.get("fullName") or "" for d in details]

        result = self._post(
            "/CancelReservation",
            {
                "bookingIds": booking_ids,
                "courseId": item.get("courseId", self.course_id),
                "golferId": self.golfer_id,
                "reasonId": 0,
                "cancellationIds": [],
                "cancelDetail": {
                    "teeTime": item["startTime"],
                    "holes": item.get("holes"),
                    "reservationId": int(booking_id),
                    "teeSheetId": item.get("teeSheetId"),
                    "numberOfPlayer": item.get("numberOfPlayer"),
                    "playerNameList": names,
                },
            },
        )
        if result.get("isSuccess") is False:
            raise AdapterError(f"cancel failed: {result.get('errors')}")

    # -- http --------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        offset = -int(
            (datetime.now().astimezone().utcoffset() or timedelta()).total_seconds()
            // 60
        )
        # Verified against the capture: every endpoint gets the same tenant
        # header block. Note x-siteid is 1 — the *tenant* site — while the
        # course's siteId (2) belongs in request bodies. And x-terminalid is
        # required: omit it and ReserveTeeTimes fails with the unhelpful
        # "Value cannot be null. (Parameter 's')" from an int.Parse on the
        # missing header, three calls after the mistake was made.
        return {
            "Authorization": f"Bearer {self._bearer()}",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": _UA,
            "Origin": HOST,
            "Referer": f"{HOST}/onlineresweb/search-teetime",
            "client-id": "onlineresweb",
            "x-websiteid": WEBSITE_ID,
            "x-productid": "1",
            "x-componentid": "1",
            "x-moduleid": "7",
            "x-siteid": TENANT_SITE_ID,
            "x-terminalid": TERMINAL_ID,
            "x-requestid": str(uuid.uuid4()),
            "x-ismobile": "false",
            "x-timezone-offset": str(offset),
            "x-timezoneid": "America/New_York",
        }

    def _bearer(self) -> str:
        # A signed-in session takes precedence: search works anonymously, but
        # every write call needs the real account token.
        if self.auth is not None:
            return self.auth.bearer
        # Short-lived by name; refresh every 10 minutes rather than trusting it
        # to survive a 90-second poll that straddles an expiry.
        if self._token and (time.monotonic() - self._token_fetched_at) < 600:
            return self._token
        body = urllib.parse.urlencode({"client_id": SHORT_LIVED_CLIENT_ID}).encode()
        req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("User-Agent", _UA)
        req.add_header("Origin", HOST)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self._token = json.loads(resp.read())["access_token"]
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise TransientError(f"token endpoint {exc.code}") from exc
            raise AuthExpired(f"could not mint short-lived token: {exc}") from exc
        except (KeyError, ValueError) as exc:
            raise AuthExpired(f"could not mint short-lived token: {exc}") from exc
        except (urllib.error.URLError, *_NETWORK_ERRORS) as exc:
            raise TransientError(f"token: network: {getattr(exc, 'reason', exc)}") from exc
        self._token_fetched_at = time.monotonic()
        return self._token

    def _get(self, path: str) -> dict:
        return self._request(API + path, method="GET")

    def _post(self, path: str, body: dict) -> dict:
        return self._request(
            API + path, method="POST", data=json.dumps(body).encode()
        )

    def _request(self, url: str, *, method: str, data: bytes | None = None) -> dict:
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in self._headers().items():
            req.add_header(k, v)
        if data is not None:
            req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            if exc.code == 400 and any(
                m in body.lower() for m in _NOT_RELEASED_MARKERS
            ):
                raise NotYetReleased(body.strip()[:160]) from exc
            if exc.code in (401, 403):
                self._token = None
                raise AuthExpired(f"{exc.code}: {body[:160]}") from exc
            if exc.code == 429:
                raise RateLimited(body[:160]) from exc
            if exc.code >= 500:
                raise TransientError(f"{exc.code}: {body[:160]}") from exc
            raise AdapterError(f"{exc.code}: {body[:200]}") from exc
        except urllib.error.URLError as exc:
            raise TransientError(f"network: {exc.reason}") from exc
        except _NETWORK_ERRORS as exc:
            raise TransientError(f"network: {type(exc).__name__}: {exc}") from exc

        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}


# -- mapping ---------------------------------------------------------------


def _cps_date(day: date) -> str:
    """CPS wants JavaScript's Date.toDateString(): 'Sat Aug 15 2026'."""
    return day.strftime("%a %b %d %Y")


def _to_slot(raw: dict) -> Slot | None:
    try:
        tee_time = datetime.fromisoformat(raw["startTime"])
    except (KeyError, ValueError):
        return None

    # Trust the flags over the `holes` field: a slot can report holes=18 while
    # is9HoleOnly/isDisabled18Hole say you may only play 9.
    holes = int(raw.get("holes") or 0)
    if raw.get("is9HoleOnly") or raw.get("isDisabled18Hole"):
        holes = 9

    # COUNT of free seats, not the highest seat number. availableParticipantNo
    # is a list of seat positions still open — [3, 4] means two seats free on a
    # tee time whose first two are already taken. Using max() here reported
    # that slot as having room for four and the booking failed after the lock.
    available = raw.get("availableParticipantNo") or []
    spots = len(available) if available else int(raw.get("participants") or 0)

    prices = raw.get("shItemPrices") or []
    price = prices[0].get("price") if prices else None

    return Slot(
        id=str(raw.get("teeSheetId") or raw.get("courseTimeId") or ""),
        course=raw.get("courseName") or "George Wright Golf Course",
        tee_time=tee_time,
        holes=holes,
        spots=spots,
        price_usd=float(price) if price is not None else None,
    )
