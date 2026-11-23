"""Offline-only reconstruction of rental planning from claimer.py at a0460df.

Legacy: market -> listings; market/rent accepted {listingId}; a local policy
capped new rentals at two, preserving existing rentals even above that count.
This is historical evidence, NOT a verified current API contract or game limit.
No session, network, payment, worker, or executable rent operation exists here.
Prices, availability and contract terms were not verified by that old planner;
any supplied listings below are drafts for review, never permission to rent.
"""

from trip_rules import valid_car_id

LEGACY_MAX_RENTED = 2
MAX_DRAFT_LISTINGS = 100  # Bound untrusted offline input; not a game limit.


def rental_preview(cars=None, market=None):
    """Pure local preview, always disabled. None means unknown, never zero.

    The caller supplies an already identity-scoped, validated account snapshot.
    No market is fetched. Even supplied valid-looking listings remain unverified.
    Unknown rental markers or malformed/duplicate listings fail closed.
    """
    count = None
    if isinstance(cars, list) and all(
        isinstance(car, dict) and isinstance(car.get('rented'), bool) for car in cars
    ):
        count = sum(car['rented'] for car in cars)
    slots = max(0, LEGACY_MAX_RENTED - count) if count is not None else None
    result = {'enabled': False, 'existing': count, 'legacy_limit': LEGACY_MAX_RENTED,
              'slots': slots, 'market_status': 'not_checked', 'drafts': [],
              'requires_review': True}
    if market is None:
        return result
    result['market_status'] = 'invalid'
    if not isinstance(market, dict):
        return result
    listings = market.get('listings')
    if not isinstance(listings, list) or len(listings) > MAX_DRAFT_LISTINGS:
        return result
    seen, drafts = set(), []
    for listing in listings:
        if not isinstance(listing, dict) or not valid_car_id(listing.get('id')):
            return result
        key = str(listing['id'])
        if key in seen:
            return result
        seen.add(key)
        name = listing.get('name')
        # Whitelist public display fields; never copy tokens/cookies or raw rows.
        drafts.append({'listing_id': listing['id'],
                       'name': name[:160] if isinstance(name, str) and name.strip() else 'تاکسی بدون نام'})
    result['market_status'] = 'provided_unverified'
    if slots:
        result['drafts'] = drafts[:slots]  # Original listing order, not profitability ranking.
    return result
