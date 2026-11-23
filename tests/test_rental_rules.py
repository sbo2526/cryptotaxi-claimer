"""New rentals remain inert; historical rules are offline previews only."""
import copy
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import claimer
import dashboard
import runtime_config
import supervisor
from monitor_safety import SafetyStop, check_policy
from rental_rules import LEGACY_MAX_RENTED, MAX_DRAFT_LISTINGS, rental_preview


ENV = {'PRIMARY_ACCOUNT': runtime_config.ALLOWED_PRIMARY_ACCOUNT,
       'SITE_MONITORING_ENABLED': 'true', 'AUTO_START_TRIPS_ENABLED': 'true',
       'AUTO_RENT_ENABLED': 'true', 'RENT_ENABLED': 'true', 'NEW_RENTALS_ENABLED': 'true'}


def rented(count):
    return [{'rented': False}] + [{'rented': True} for _ in range(count)]


class RentalRulesTests(unittest.TestCase):
    def test_unknown_counts_are_not_zero_or_available_capacity(self):
        for cars in (None, {}, 'bad', [None], [{}], [{'rented': 1}], [{'rented': 'true'}]):
            with self.subTest(cars=cars):
                plan = rental_preview(cars)
                self.assertIsNone(plan['existing'])
                self.assertIsNone(plan['slots'])
                self.assertFalse(plan['enabled'])
                self.assertEqual(plan['drafts'], [])

    def test_legacy_cap_preserves_existing_rentals_even_above_limit(self):
        self.assertEqual(LEGACY_MAX_RENTED, 2)  # a0460df application policy, not game law.
        for count, slots in ((0, 2), (1, 1), (2, 0), (4, 0)):
            with self.subTest(count=count):
                cars = rented(count)
                before = copy.deepcopy(cars)
                plan = rental_preview(cars)
                self.assertEqual(plan['existing'], count)
                self.assertEqual(plan['slots'], slots)
                self.assertEqual(cars, before)
        self.assertEqual(rental_preview([])['existing'], 0)

    def test_offline_listing_order_and_slots_are_only_drafts_never_approved(self):
        market = {'listings': [{'id': 2, 'name': 'Second'}, {'id': 'first', 'name': 'First'},
                               {'id': 3, 'name': 'Third'}]}
        original = copy.deepcopy(market)
        plan = rental_preview(rented(0), market)
        self.assertEqual([row['listing_id'] for row in plan['drafts']], [2, 'first'])
        self.assertFalse(plan['enabled'])
        self.assertTrue(plan['requires_review'])
        self.assertEqual(plan['market_status'], 'provided_unverified')
        self.assertEqual(market, original)
        self.assertEqual(len(rental_preview(rented(1), market)['drafts']), 1)
        self.assertEqual(rental_preview(rented(4), market)['drafts'], [])
        self.assertEqual(rental_preview(None, market)['drafts'], [])

    def test_missing_market_is_distinct_from_supplied_empty_market(self):
        self.assertEqual(rental_preview([])['market_status'], 'not_checked')
        self.assertEqual(rental_preview([], {'listings': []})['market_status'], 'provided_unverified')
        self.assertEqual(rental_preview([], {})['market_status'], 'invalid')

    def test_untrusted_market_input_fails_closed_including_duplicate_ids(self):
        invalid = [[], {}, {'listings': None}, {'listings': [None]},
                   {'listings': [{'id': True}]}, {'listings': [{'id': '../bad'}]},
                   {'listings': [{'id': 1}, {'id': '1'}]},
                   {'listings': [{'id': i + 1} for i in range(MAX_DRAFT_LISTINGS + 1)]}]
        for market in invalid:
            with self.subTest(market=repr(market)[:100]):
                plan = rental_preview([], market)
                self.assertEqual(plan['market_status'], 'invalid')
                self.assertEqual(plan['drafts'], [])
                self.assertFalse(plan['enabled'])

    def test_only_display_fields_survive_no_price_or_contract_is_invented(self):
        plan = rental_preview([], {'listings': [{'id': 'car-1', 'name': 'Taxi',
                                               'token': 'private-token', 'price': 0, 'contract': {}}]})
        self.assertEqual(plan['drafts'], [{'listing_id': 'car-1', 'name': 'Taxi'}])
        self.assertNotIn('private-token', str(plan))
        self.assertNotIn('price', plan['drafts'][0])

    def test_preview_and_dashboard_never_fetch_market_or_rent(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('No network')), \
             patch.object(dashboard, 'load_json', side_effect=lambda path, default: default):
            rental_preview([], {'listings': [{'id': 1}]})
            page = dashboard.render()
        self.assertIn('id="rentals"', page)
        self.assertIn('فعلاً غیرفعال', page)
        self.assertIn('هیچ درخواست بازار، اجاره یا پرداختی ارسال نمی‌شود', page)
        self.assertIn('type="button" disabled aria-describedby="rentalNotice"', page)
        self.assertNotIn('<form', page)
        self.assertIn('بازار خوانده نشده', page)

    def test_env_flags_cannot_enable_market_worker_or_any_rent_request(self):
        with patch.dict(os.environ, ENV, clear=True):
            self.assertEqual(set(supervisor.service_commands()), {'dashboard', 'monitor'})
            self.assertFalse(rental_preview([])['enabled'])
            for endpoint in ('market', 'market/rent'):
                with self.subTest(endpoint=endpoint):
                    with self.assertRaises(SafetyStop):
                        check_policy(ENV['PRIMARY_ACCOUNT'], endpoint)
                    session, owner, pacer = Mock(), Mock(), Mock()
                    worker = claimer.ReadOnlyMonitor(ENV['PRIMARY_ACCOUNT'], owner, session, pacer)
                    with self.assertRaises(SafetyStop):
                        worker.request(endpoint, {'listingId': 1})
                    session.post.assert_not_called()
                    owner.before_request.assert_not_called()
                    pacer.wait.assert_not_called()

    def test_ui_keeps_rental_count_unknown_without_primary_snapshot(self):
        with patch.object(dashboard, 'load_json', side_effect=lambda path, default: default):
            view = dashboard.dashboard_view({})
        panel = dashboard.rentals_section(view)
        self.assertIn('data-rental-existing>نامشخص', panel)
        self.assertIn('data-rental-slots>نامشخص', panel)
        self.assertNotIn('data-rental-existing>0', panel)

    def test_ui_above_legacy_cap_preserves_current_rentals(self):
        panel = dashboard.rentals_section({'sample': {'cars': rented(4)}})
        self.assertIn('data-rental-existing>4', panel)
        self.assertIn('data-rental-slots>0', panel)
        self.assertIn('رنت‌های موجود حفظ می‌شوند', panel)
        self.assertIn('محدودیت تأییدشدهٔ بازی نیست', panel)
        self.assertIn('استارت تاکسی‌های از قبل رنت‌شده', panel)

    def test_new_pure_module_is_packaged_in_explicit_docker_allowlist(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn('rental_rules.py', (root / 'Dockerfile').read_text())
        self.assertIn('!rental_rules.py', (root / '.dockerignore').read_text())


if __name__ == '__main__':
    unittest.main()
