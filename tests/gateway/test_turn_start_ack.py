"""Tests for opt-in gateway turn-start acknowledgements."""

from gateway.run import _should_send_turn_start_ack


def test_turn_start_ack_is_opt_in():
    assert _should_send_turn_start_ack({}, "telegram", "hello") is False

    cfg = {"display": {"platforms": {"telegram": {"turn_start_ack": True}}}}
    assert _should_send_turn_start_ack(cfg, "telegram", "hello") is True


def test_turn_start_ack_skips_internal_gateway_messages():
    cfg = {"display": {"platforms": {"telegram": {"turn_start_ack": True}}}}

    assert _should_send_turn_start_ack(cfg, "telegram", "[IMPORTANT: background done]") is False
    assert _should_send_turn_start_ack(cfg, "telegram", "[ASYNC DELEGATION BATCH COMPLETE - abc]") is False
    assert _should_send_turn_start_ack(cfg, "telegram", "[Session was just handed off from CLI]") is False
    assert _should_send_turn_start_ack(cfg, "telegram", "[KANBAN TASK COMPLETE - t_1]") is False
    assert _should_send_turn_start_ack(cfg, "telegram", "[SYSTEM: resume pending turn]") is False
    assert _should_send_turn_start_ack(cfg, "telegram", "[RECOVERY EVENT - gateway restart]") is False
    assert _should_send_turn_start_ack(cfg, "telegram", "   ") is False
