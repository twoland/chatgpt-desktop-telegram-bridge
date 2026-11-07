from pathlib import Path

import pytest

from codex_telegram_bridge.chat_routing import ChatRouting
from codex_telegram_bridge.desktop_booking import BoardError, DesktopBooking, initialize_legacy_board
from codex_telegram_bridge.local_mode import relay_owner


def test_missing_registry_never_selects_an_installation_default(tmp_path):
    routing = ChatRouting.load(42, tmp_path / 'chat-routing.json')
    assert routing.agents_for(42) == {}
    assert routing.default_private_thread(42) is None
    assert routing.resolve(42, 'chores') is None


def test_new_home_board_can_be_created_then_claimed_and_released(tmp_path):
    board = initialize_legacy_board(tmp_path)
    booking = DesktopBooking('script:test-relay', tmp_path)
    assert booking.try_claim()
    booking.verify()
    booking.release()
    assert 'Status: FREE' in board.read_text()
    assert 'Owner: none' in board.read_text()
    assert board.stat().st_mode & 0o077 == 0
    original = board.read_bytes()
    with pytest.raises(FileExistsError):
        initialize_legacy_board(tmp_path)
    assert board.read_bytes() == original


def test_installed_cli_cannot_be_replaced_with_a_free_legacy_board(tmp_path):
    cli = tmp_path / '.local/bin/board'
    cli.parent.mkdir(parents=True)
    cli.write_text('installed')
    with pytest.raises(BoardError, match='installed board'):
        initialize_legacy_board(tmp_path)
    assert not (tmp_path / 'desktop-control.md').exists()


def test_missing_board_fails_closed_with_setup_guidance(tmp_path):
    with pytest.raises(BoardError, match='missing'):
        DesktopBooking('script:test-relay', tmp_path).try_claim()
    assert not (tmp_path / 'desktop-control.md').exists()


def test_relay_identity_is_stable_and_scoped_to_each_installation(tmp_path):
    first, second = tmp_path / 'First Bridge', tmp_path / 'Second Bridge'
    assert relay_owner(first) == relay_owner(first)
    assert relay_owner(first) != relay_owner(second)
    assert relay_owner(first).startswith('script:telegram-relay-')
