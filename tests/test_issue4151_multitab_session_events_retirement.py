"""Browser regressions for multi-tab session-list refresh behavior.

The app keeps the gateway and active-session streams, but sidebar invalidation
uses the existing visible-page activity poll plus focus catch-up. Two loaded
WebUI pages therefore must not construct the retired global
``/api/sessions/events`` EventSource, and changes made in one page must replace
the other page's rendered sidebar state on those fallback paths.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

import pytest

from tests._pytest_port import BASE


_EVENT_SOURCE_SPY = """
(() => {
  const opened = [];
  let constructionCount = 0;
  class EventSourceSpy {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 2;

    constructor(url) {
      this.url = String(url);
      this.readyState = EventSourceSpy.OPEN;
      this.listeners = Object.create(null);
      constructionCount += 1;
      opened.push(this.url);
    }

    addEventListener(name, callback) {
      (this.listeners[name] ||= []).push(callback);
    }

    removeEventListener(name, callback) {
      const listeners = this.listeners[name] || [];
      this.listeners[name] = listeners.filter((item) => item !== callback);
    }

    close() {
      this.readyState = EventSourceSpy.CLOSED;
    }
  }

  window.__openedEventSourceUrls = opened;
  window.__eventSourceSpyInstalled = true;
  window.__eventSourceSpyConstructor = EventSourceSpy;
  Object.defineProperty(window, '__eventSourceConstructCount', {
    configurable: false,
    get: () => constructionCount,
  });
  window.EventSource = EventSourceSpy;
})();
"""


def _post_json_from_page(page, path: str, payload: dict) -> dict:
    return page.evaluate(
        """
        async ({path, payload}) => {
          const controller = new AbortController();
          const timeout = setTimeout(() => controller.abort(), 5000);
          try {
            const response = await fetch(path, {
              method: 'POST',
              credentials: 'same-origin',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify(payload),
              signal: controller.signal,
            });
            const text = await response.text();
            let body = null;
            try { body = JSON.parse(text); } catch (_) { body = {raw: text}; }
            return {status: response.status, body};
          } finally {
            clearTimeout(timeout);
          }
        }
        """,
        {"path": path, "payload": payload},
    )


def _wait_for_sidebar_title(page, session_id: str, title: str, timeout_ms: int) -> None:
    page.wait_for_function(
        """
        ({sessionId, title}) => {
          const row = [...document.querySelectorAll('.session-item[data-sid]')]
            .find((item) => item.dataset.sid === sessionId);
          const titleNode = row && row.querySelector('.session-title');
          return !!titleNode && titleNode.textContent === title;
        }
        """,
        arg={"sessionId": session_id, "title": title},
        timeout=timeout_ms,
    )


def test_two_webui_pages_construct_gateway_but_not_session_list_eventsource():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")

        context = browser.new_context(base_url=BASE)
        pages = [context.new_page(), context.new_page()]
        try:
            for page in pages:
                page.add_init_script(_EVENT_SOURCE_SPY)
                page.goto("/", wait_until="domcontentloaded")
                page.wait_for_function(
                    "typeof renderSessionList === 'function' && "
                    "typeof ensureSessionActivityPoll === 'function' && "
                    "typeof startGatewaySSE === 'function'",
                    timeout=10000,
                )
                page.bring_to_front()
                state = page.evaluate(
                    """
                    () => {
                      const constructorIntact =
                        window.EventSource === window.__eventSourceSpyConstructor;
                      window._showCliSessions = true;
                      startGatewaySSE();
                      return {
                        installed: window.__eventSourceSpyInstalled === true,
                        constructorIntact,
                        constructionCount: window.__eventSourceConstructCount,
                        opened: window.__openedEventSourceUrls.slice(),
                      };
                    }
                    """
                )
                assert state["installed"] is True
                assert state["constructorIntact"] is True
                assert state["constructionCount"] == len(state["opened"])
                assert state["constructionCount"] > 0, (
                    "the guard is meaningful only if production code constructs "
                    "at least one retained EventSource"
                )
                assert any(
                    "/api/sessions/gateway/stream" in f"/{url.lstrip('/')}"
                    for url in state["opened"]
                ), state["opened"]
                assert not any(
                    "/api/sessions/events" in f"/{url.lstrip('/')}"
                    for url in state["opened"]
                ), state["opened"]
        finally:
            context.close()
            browser.close()


def test_second_page_catches_up_create_and_rename_by_poll_then_focus(
    cleanup_test_sessions,
):
    """A sibling page replaces stale sidebar state without session-list SSE."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")

        context = browser.new_context(base_url=BASE)
        page_a = context.new_page()
        page_b = context.new_page()
        session_list_responses: list[float] = []

        def record_session_list_response(response) -> None:
            if urlparse(response.url).path == "/api/sessions":
                session_list_responses.append(time.monotonic())

        page_b.on("response", record_session_list_response)
        try:
            for page in (page_a, page_b):
                page.goto("/", wait_until="domcontentloaded")
                page.wait_for_function(
                    "typeof renderSessionList === 'function' && "
                    "typeof ensureSessionActivityPoll === 'function'",
                    timeout=10000,
                )

            page_b.bring_to_front()
            page_b.wait_for_function("document.hidden === false", timeout=5000)
            page_b.evaluate(
                """
                async () => {
                  ensureSessionActivityPoll();
                  await renderSessionList({deferWhileInteracting:false});
                }
                """
            )

            created_title = f"poll-created-{time.monotonic_ns()}"
            created = _post_json_from_page(
                page_a,
                "/api/session/import",
                {
                    "title": created_title,
                    "messages": [
                        {
                            "role": "user",
                            "content": "cross-page refresh test",
                            "timestamp": time.time(),
                        }
                    ],
                },
            )
            assert created["status"] == 200, created
            session_id = (created["body"].get("session") or {}).get("session_id")
            assert session_id, created
            cleanup_test_sessions.append(session_id)

            session_list_responses.clear()
            poll_wait_started = time.monotonic()
            # One 5s activity interval plus 3s for browser/server scheduling.
            _wait_for_sidebar_title(page_b, session_id, created_title, timeout_ms=8000)
            assert any(ts >= poll_wait_started for ts in session_list_responses), (
                "the replacement must follow a real /api/sessions response"
            )

            poll_state = page_b.evaluate(
                """
                () => {
                  const wasRunning = !!_sessionActivityPollTimer;
                  const focusHookInstalled =
                    document._hermesSidebarSseFocusHook === true;
                  if (_sessionActivityPollTimer) {
                    clearInterval(_sessionActivityPollTimer);
                    _sessionActivityPollTimer = null;
                  }
                  return {wasRunning, focusHookInstalled};
                }
                """
            )
            assert poll_state["wasRunning"] is True
            assert poll_state["focusHookInstalled"] is True

            focus_title = f"focus-renamed-{session_id[-8:]}"
            renamed = _post_json_from_page(
                page_a,
                "/api/session/rename",
                {"session_id": session_id, "title": focus_title},
            )
            assert renamed["status"] == 200, renamed
            current_title = page_b.evaluate(
                """
                (sessionId) => {
                  const row = [...document.querySelectorAll('.session-item[data-sid]')]
                    .find((item) => item.dataset.sid === sessionId);
                  const titleNode = row && row.querySelector('.session-title');
                  return titleNode && titleNode.textContent;
                }
                """,
                session_id,
            )
            assert current_title == created_title

            session_list_responses.clear()
            focus_started = time.monotonic()
            page_b.evaluate("window.dispatchEvent(new FocusEvent('focus'))")
            try:
                _wait_for_sidebar_title(page_b, session_id, focus_title, timeout_ms=3000)
            except playwright_api.TimeoutError as exc:
                diagnostics = page_b.evaluate(
                    """
                    async (sessionId) => {
                      const response = await fetch('/api/sessions' + _sessionListQueryString());
                      const body = await response.json();
                      const row = (body.sessions || []).find(
                        (item) => item.session_id === sessionId
                      ) || null;
                      return {status: response.status, row};
                    }
                    """,
                    session_id,
                )
                raise AssertionError(
                    "focus catch-up did not replace the title; "
                    f"responses={len(session_list_responses)} diagnostics={diagnostics!r}"
                ) from exc
            assert any(ts >= focus_started for ts in session_list_responses), (
                "focus catch-up must replace the sidebar after a real /api/sessions response"
            )
        finally:
            context.close()
            browser.close()
