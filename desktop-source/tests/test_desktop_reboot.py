from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import codex_telegram_bridge.desktop_booking as module
from codex_telegram_bridge.desktop_booking import BoardError, DesktopBooking
from test_local_mode import BOARD

BOOT = datetime(2026, 11, 27, 15, 54, 12, tzinfo=timezone.utc)
OLD = '2026-11-27T15:31:41+00:00'
NEW = '2026-11-27T16:00:00+00:00'


@pytest.fixture
def booking(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'host_boot_time', lambda: BOOT)
    (tmp_path / 'desktop-control.md').write_text(BOARD)
    return DesktopBooking('script:test-relay', tmp_path)


def busy(booking, *, owner='script:interrupted-worker', started=OLD, updated=OLD, rows=''):
    text = BOARD.replace('Status: FREE', 'Status: BUSY').replace('Owner: none', f'Owner: {owner}')
    text = text.replace('Started: none', f'Started: {started}').replace('Updated: never', f'Updated: {updated}')
    booking.board.write_text(text.replace('## Messages', rows + '## Messages'))


def row(owner, window='NEXT', requested=OLD):
    return f'| {requested} | {owner} | {window} | Work | Editor | 60s |\n'


def test_reboot_recovers_dead_claim_and_old_next_queue_preserving_history(booking):
    busy(booking, rows=row('script:old-waiter'))
    assert booking.try_claim()
    text = booking.board.read_text()
    assert 'Owner: script:test-relay' in text
    assert 'script:old-waiter | NEXT' not in text
    assert 'Keep this message.' in text
    assert 'Recovered prior-boot desktop coordination' in text
    booking.release()


def test_prior_boot_same_owner_is_recovered_not_mistaken_for_active_lease(booking):
    busy(booking, owner=booking.owner)
    assert booking.try_claim()


@pytest.mark.parametrize('started,updated', [(NEW, NEW), (NEW, OLD), ('none', OLD), ('2026-11-27T15:40:00', OLD)])
def test_current_or_unverifiable_claim_is_never_recovered(booking, started, updated):
    busy(booking, started=started, updated=updated)
    assert not booking.try_claim()
    assert 'Owner: script:interrupted-worker' in booking.board.read_text()
    assert 'Recovered' not in booking.board.read_text()


def test_postboot_reservation_update_does_not_renew_a_preboot_claim(booking):
    busy(booking, updated=NEW)
    assert booking.try_claim()
    assert 'Recovered prior-boot desktop coordination' in booking.board.read_text()


def test_pruning_old_waiter_does_not_release_current_owner(booking):
    busy(booking, started=NEW, updated=NEW, rows=row('script:old-waiter'))
    assert not booking.try_claim()
    text = booking.board.read_text()
    assert 'Owner: script:interrupted-worker' in text
    assert 'Status: BUSY' in text
    assert 'script:old-waiter | NEXT' not in text


def test_same_boot_own_claim_still_requires_review(booking):
    busy(booking, owner=booking.owner, started=NEW, updated=NEW)
    with pytest.raises(BoardError, match='earlier bridge booking'):
        booking.try_claim()


def test_unknown_boot_does_not_change_old_claim(booking, monkeypatch):
    monkeypatch.setattr(module, 'host_boot_time', lambda: None)
    busy(booking)
    assert not booking.try_claim()
    assert 'Owner: script:interrupted-worker' in booking.board.read_text()


def test_future_window_survives_reboot_and_blocks_conflicting_claim(booking):
    busy(booking, rows=row('script:scheduled', f'{OLD}/2099-01-01T00:00:00Z'))
    assert not booking.try_claim()
    text = booking.board.read_text()
    assert 'Status: FREE' in text
    assert 'script:scheduled' in text


def test_expired_preboot_window_removed_but_postboot_next_keeps_priority(booking):
    busy(booking, rows=row('script:expired', '2026-11-27T15:00:00Z/2026-11-27T15:30:00Z')
         + row('script:new-waiter', requested=NEW))
    assert not booking.try_claim()
    text = booking.board.read_text()
    assert 'script:expired' not in text
    assert 'script:new-waiter | NEXT' in text


def test_free_board_with_only_prior_boot_next_does_not_deadlock(booking):
    booking.board.write_text(BOARD.replace('Updated: never', f'Updated: {OLD}')
                             .replace('## Messages', row('script:old-waiter') + '## Messages'))
    assert booking.try_claim()


def test_malformed_reservations_are_not_silently_rewritten(booking):
    busy(booking, rows='| invalid |\n')
    original = booking.board.read_bytes()
    with pytest.raises(BoardError):
        booking.try_claim()
    assert booking.board.read_bytes() == original


@pytest.mark.parametrize('output,expected', [
    ('{ sec = 1795794852, usec = 770355 } Fri Nov 27 10:54:12 2026\n', True),
    ('unknown', False), ('{ sec = 1795794852, usec = 1000000 }', False),
    ('{ sec = 9999999999, usec = 0 }', False),
])
def test_kernel_boot_parser(output, expected, monkeypatch):
    # Exercise the real cached function independently of the booking fixture.
    module.host_boot_time.cache_clear()
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=output))
    try:
        value = module.host_boot_time()
        assert (value is not None) == expected
        if expected:
            assert value.timestamp() == pytest.approx(1795794852.770355)
    finally:
        module.host_boot_time.cache_clear()
