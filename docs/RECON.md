# George Wright — recon findings

Date: 2026-08-10. All read-only, no authentication used.

## Platform

**CPS Golf** (`cps.golf`), Angular SPA at `/onlineresweb`, build `26.1.2.26379768992`.
Boston runs two courses off the same tenant:

| courseId | siteId | course              | abbrev |
|----------|--------|---------------------|--------|
| 2        | 2      | George Wright       | GW     |
| 1        | 1      | William J. Devine   | WJD    |

`courseIds=2` is the "George Wright only" filter. Timezone comes from the API
(`timezoneId: "America/New_York"`) — don't hardcode it.

## The booking rule, straight from the server

`GET /onlineres/onlineapi/api/v1/onlinereservation/BookingRuleModels?classcode=R&courseIds=2&searchDate=...`

```json
{"bookingRuleByClass":[{"classCode":"R","bookingRuleByCourse":[
  {"bookingRuleId":6,"courseId":2,"daysInAdvance":4,
   "daysInAdvanceWeekend":0,"time":"2024-07-16T07:00:00"}]}],
 "bookingRuleByCourses":[{"courseId":2,"noShowLimit":2,
   "maximumDailyBookings":1,"daysInAdvance":4}]}
```

- `daysInAdvance: 4` + `time: 07:00` → **Tuesday 07:00 releases Saturday.** The
  spec was right, and this is now machine-readable rather than folklore. The
  agent should *read* this endpoint rather than assume it — if the course
  changes the window, the agent adapts instead of silently missing.
- **`maximumDailyBookings: 1`** — the server enforces one booking per day.
  The ledger idempotency guard is aligned with a real constraint, not just
  defensive coding.
- **`noShowLimit: 2`** — two no-shows and the account gets restricted. This is
  the actual argument for the Friday weather re-check: an auto-booked tee time
  you skip in the rain costs you something.

## Read API — working, unauthenticated

Anonymous short-lived JWT from `POST /identityapi/myconnect/token/short`,
cached by the SPA in `localStorage["online-reservation-v5-short_lived_token"]`.

Required headers (missing any → `400 Invalid componentid request header`):

```
Authorization: Bearer <short-lived JWT>
x-websiteid: 50827848-110c-4067-0beb-08da6c0028fc
x-productid: 1
x-componentid: 1
x-siteid: 2
x-ismobile: false
x-timezone-offset: <minutes>
x-timezoneid: America/New_York
```

Then, per search: generate a client-side GUID, register it, use it.

```
POST /onlineres/onlineapi/api/v1/onlinereservation/RegisterTransactionId
     {"transactionId": "<uuid4>"}

GET  /onlineres/onlineapi/api/v1/onlinereservation/TeeTimes
     ?searchDate=Fri Aug 14 2026   <- JS Date.toDateString() format
     &courseIds=2&holes=0&numberOfPlayer=0&searchTimeType=0
     &transactionId=<uuid>&teeOffTimeMin=0&teeOffTimeMax=23
     &isChangeTeeOffTime=true&teeSheetSearchView=5&classCode=R
     &defaultOnlineRate=N&isUseCapacityPricing=false
     &memberStoreId=1&searchType=1
```

Response is a wrapper, not a bare array:
`{transactionId, isSuccess, content}` where `content` is either the slot array
or `{messageKey: "NO_TEETIMES", ...}`. **Handle both** — an empty sheet is not
an error.

### Slot schema (fields that matter)

```json
{"teeSheetId": 570335, "courseTimeId": 10951, "courseId": 2,
 "startTime": "2026-08-13T14:00:00", "courseDate": "2026-08-13T00:00:00",
 "holes": 9, "is9HoleOnly": true, "isDisabled18Hole": true,
 "participants": 4, "minPlayer": 1, "maxPlayer": 4,
 "availableParticipantNo": [1,2,3,4],
 "defaultRateCode": "NON-RES",
 "shItemPrices": [{"itemCode":"NON-RES WEEKDAY 9","price":40, ...}]}
```

Mapping to our `Slot`:

| ours      | theirs                                              |
|-----------|-----------------------------------------------------|
| `id`      | `teeSheetId` (+ `courseTimeId` for the book call)    |
| `tee_time`| `startTime`                                          |
| `holes`   | `holes`, gated on `is9HoleOnly` / `isDisabled18Hole` |
| `spots`   | `max(availableParticipantNo)`                        |
| `price`   | `shItemPrices[0].price`                              |

`startTime` carries **no timezone suffix**. Course tz is America/New_York and
the client sends `x-timezone-offset`, so it is almost certainly course-local —
verify before trusting it near a DST boundary.

`defaultRateCode: "NON-RES"` — there is a resident rate. If you're a Boston
resident the account's class code should change this; worth checking what the
logged-in session returns.

## The complication

Cloudflare bot detection is live on this origin:

```
GET  /cdn-cgi/challenge-platform/h/b/scripts/jsd/<id>/main.js
POST /cdn-cgi/challenge-platform/h/b/jsd/oneshot/<id>/<token>/<hash>
```

That's Cloudflare's JS-detection handshake. Plain `httpx` against the JSON API
may work today and get challenged tomorrow, especially at 07:00:00 on a Tuesday
when the traffic pattern is least ordinary. I'm not going to build anything to
get around it.

**Recommendation:** drive it with Playwright against a real browser profile and
issue the API calls *from page context* (`page.evaluate` + `fetch`). You get
the challenge cookie handled by the browser for free, and still get the ~100ms
JSON call instead of DOM-scraping a tee sheet. Best of both.

## Write path — from HAR capture, 2026-08-10

**Verified end-to-end 2026-08-10** — booked a test reservation and cancelled it.

Booking is a **multi-step flow with a hold**, not a single POST. Order matters:

```
1. POST /LockTeeTimes             {teeSheetIds:[id], email, action:"Online
                                   Reservation V5", sessionId:<uuid>, golferId,
                                   classCode, numberOfPlayer:0, navigateUrl:"",
                                   isSmartCard:false, isGroupBooking:false}
                                  -> {sessionId, error:""}   10-min hold starts
2. POST /CheckBookingLimit        once per seat
3. POST /TeeTimePricesCalculation -> returns the transactionId used below
4. POST /CheckRestrictReservation {teeSheetId, courseId, siteId:2, classCode:"RES"}
5. POST /RegisterTransactionId    {transactionId: <uuid>}
6. POST /ReserveTeeTimes          -> reservationId, bookingIds[]
```

### Required headers — the expensive lesson

Every endpoint takes the same tenant block. Omitting `x-terminalid` makes
`ReserveTeeTimes` fail with `400 "Value cannot be null. (Parameter 's')"` —
a null `int.Parse` on the missing header, reported three calls after the
mistake and with no hint which field is at fault.

```
client-id: onlineresweb      x-productid: 1     x-componentid: 1
x-moduleid: 7                x-terminalid: 3    x-siteid: 1
x-websiteid: <guid>          x-requestid: <fresh uuid per request>
x-ismobile: false            x-timezone-offset / x-timezoneid
```

`x-siteid` is **1** — the tenant site. The course's `siteId` is **2** and
belongs in request *bodies*. Reads tolerate the wrong value; writes do not.

### Three more traps in the same error

- **`ibxCardOnFile`** in step 3 must be the **entire card record** from
  `GetAllCreditCardOnFile`, not null and not just the token.
- **`bookingTransactionId`** in step 6 is **minted by the server** in step 3's
  response. Generating your own earns `"Not found a transaction"`.
- **`participantNo`** must come from the slot's `availableParticipantNo`, which
  is a list of *seat positions* — `[3, 4]` means two seats free, not four.
  `len()` is the seat count; `max()` is a seat number.

### Payment

Step 3's response reports **`totalDueAtCourse`** — the card is held against a
no-show, not charged at booking. Combined with `noShowLimit: 2`, the cost of an
auto-booked round you skip is a strike, not a charge.

**This changes the race design.** The 7:00:00 contest is won at step 1, not
step 4 — `LockTeeTimes` takes the slot out of circulation and gives you ten
minutes to finish. The hot path only needs `search → lock`. Steps 2–4 can run
at a relaxed pace afterwards. `UnLockTeeTimes` releases a hold you decide not
to use, and should run on any abort so you don't sit on a slot for ten minutes.

### ReserveTeeTimes (step 4)

```json
{"lockedTeeTimesSessionId": "<from step 1>",
 "bookingTransactionId": "<uuid>", "transactionId": "<uuid>",
 "cancelReservationLink": "...", "homePageLink": "...",
 "affiliateId": null, "sessionGuid": null,
 "finalizeSaleModel": {"acct": "<account no>", "playerId": 0, "isGuest": false,
                       "creditCardInfo": {"cardToken": "<32 chars>",
                                          "email": "<email>",
                                          "cardNumber": null, "cvv": null,
                                          "expireMM": null, "expireYY": null,
                                          "cardHolder": null},
                       "monerisCC": null, "ibxCC": null}}
```

→ `{"reservationId": ..., "bookingIds": [...], "confirmationKey": "...",
    "reservationResult": 1, "bookingGolferId": ...}`

**Payment is a stored token only** — `cardNumber` and `cvv` are null, the card
is referenced by a 32-char `cardToken` from `GET /GetAllCreditCardOnFile`.
The agent never touches card data: the token is fetched per booking and held
in memory only, and the account number lives in the gitignored
`config/preferences.toml`, never in this repo.

Open question: whether `ReserveTeeTimes` *captures* against the token or only
holds it for no-show enforcement. `CancelReservation` returned an empty
`approvalCode`, which hints at no capture, but confirm against a statement
before turning on unattended booking.

### get_existing_bookings

`GET /UpcomingReservation` → `{items: [...], totalItems}`, each item carrying
`reservationId`, `bookingIds` (via `reservationDetail[].bookingId`),
`teeSheetId`, `startTime`, `holes`, `numberOfPlayer`, `courseId`, `courseName`.
After a cancel it returns `{items: []}` — clean idempotency signal.

### cancel_booking

```json
{"bookingIds": [...], "courseId": 2, "golferId": ..., "reasonId": 0,
 "cancellationIds": [],
 "cancelDetail": {"teeTime": "...", "holes": 9, "reservationId": ...,
                  "teeSheetId": ..., "numberOfPlayer": 2,
                  "playerNameList": ["...", "..."]}}
```

→ `{"isSuccess": true, "errors": [], "data": {"cancellationIds": [...]}}`

### Member class

`GET /GetUserInformation` returns `memberClassCode` — `NRES` for a non-resident
account, which prices at the `NON-RES` rate (observed $40 for 9 holes, $61 for
18). Boston residents get a lower rate at city courses; the class code is what
changes `defaultRateCode` and the price on every run.

## How the write path was captured

Log in, book one tee time manually with DevTools open, and save the Network tab
as a HAR. That HAR has everything the live adapter needs — and it contains your
session, so keep it out of the repo (`*.har` is gitignored) and out of chat
transcripts.
