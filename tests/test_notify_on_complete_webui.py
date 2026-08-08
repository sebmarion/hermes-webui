from pathlib import Path


def test_webui_drains_only_matching_background_completion_events():
    src = Path("api/streaming.py").read_text(encoding="utf-8")

    assert "def _drain_webui_process_notifications(" in src
    assert "claimed_events: list[dict] | None = None" in src
    assert "from tools.process_registry import process_registry" in src
    assert "evt.get('session_key')" in src
    assert "process_registry.get(evt_sid)" not in src
    assert "skipped_events.append(evt)" in src
    assert "completion_queue.put(evt)" in src


def test_webui_injects_process_notifications_without_persisting_them_as_user_text():
    src = Path("api/streaming.py").read_text(encoding="utf-8")

    assert "_process_notifications = _drain_webui_process_notifications(" in src
    assert "claimed_events=_process_completion_claims" in src
    assert "[*_process_notifications, msg_text]" in src
    assert "_build_native_multimodal_message(workspace_ctx, _agent_msg_text" in src
    assert "persist_user_message=msg_text" in src


def test_webui_binds_gateway_session_platform_without_process_mirror():
    src = Path("api/streaming.py").read_text(encoding="utf-8")

    assert "'HERMES_SESSION_PLATFORM': 'webui'" in src
    assert "full_context=True" in src
    assert "os.environ['HERMES_SESSION_PLATFORM'] = 'webui'" not in src
    assert "old_session_platform = os.environ.get('HERMES_SESSION_PLATFORM')" not in src


def test_webui_age_gates_stale_background_completion_events():
    """Issue #4029: drain must drop completions older than the configured cap
    so stale notifications can't be prepended to an unrelated later turn."""
    src = Path("api/streaming.py").read_text(encoding="utf-8")

    # The age-gate helper + its env override exist.
    assert "def _stale_completion_max_age_seconds()" in src
    assert "HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS" in src
    # The stable persisted event uses created_at and drops over-age events.
    assert "created_at = evt.get('created_at')" in src
    assert "age = time.time() - created_at" in src
    assert "if age > stale_completion_max_age:" in src
    # Over-age events are consumed (marked), not requeued, so they vanish.
    assert "_finish_process_completion_delivery(" in src
    assert "committed=True," in src


def test_webui_acks_exact_stable_event_only_after_durable_writeback():
    src = Path("api/streaming.py").read_text(encoding="utf-8")

    assert "def _finish_process_completion_delivery(" in src
    assert "process_registry.finish_notification_delivery(event, committed)" in src
    assert "_finalize_process_completion_claims(" in src
    assert "committed=_success_writeback_committed" in src
    assert "mark_completion_consumed" not in src
