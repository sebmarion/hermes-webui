#!/usr/bin/env python3
"""
Headless browser smoke test — runtime page-load and critical-layout gate.

WHY THIS EXISTS
  `node --check`, ESLint, and the (mocked) pytest suite cannot see the class of
  bug that has actually bricked releases: JavaScript that parses fine but throws
  at *runtime* when a real browser executes the page. Examples that shipped:
    - a `const` reassigned at runtime (v0.51.168 "Failed to load conversation
      messages" — #3162)
    - a `function X(){}` colliding with a `window.X = {}` in classic scripts
      (#2715 / #2771)
  Every one of those throws on load or first interaction and produces a blank or
  broken page for *every* user. This smoke boots the real server.py and loads
  the key pages in headless Chromium, failing if ANY uncaught exception,
  console error, or pinned critical-layout regression occurs.

SCOPE
  Deliberately AGENT-FREE so it runs in CI (which does not install hermes-agent):
  it verifies the page loads, its JS initializes cleanly, and critical composer
  controls remain reachable — it does NOT drive a full chat (that needs the
  agent + mock provider and runs in the private QA harness's golden-path E2E).

USAGE
  python tests/browser_smoke.py
  (Requires: playwright + chromium. Boots server.py on an ephemeral port with an
  isolated temp state dir and no agent.)

EXIT CODES
  0 — all pages loaded with zero console errors / uncaught exceptions
  1 — a console error or uncaught exception was detected (regression)
  2 — environment/setup failure (server didn't boot, playwright missing, etc.)
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

PORT = int(os.getenv("SMOKE_PORT", "8796"))
BASE = f"http://127.0.0.1:{PORT}"

# Pages that must load cleanly. Hash routes are how the SPA exposes views.
PAGES = [
    "/",
    "/#settings",
    "/#sessions",
]

# Known-benign console noise (extend deliberately, each with a reason). Every
# entry here is a blind spot, so keep the list short.
BENIGN = [
    "favicon",          # favicon 404 in bare env — not app code
    "manifest.json",    # PWA manifest probe under headless http
    "serviceworker",    # SW registration noise under headless http
    "sw.js",            # service worker fetch noise
    "the server responded with a status of 404",  # static asset 404 in bare env
]

CLARIFY_LAYOUT_CASES = [
    ("wide", 1440, 900, True),
    ("laptop", 1262, 759, True),
    ("reported", 1262, 535, True),
    ("resize-phone", 390, 844, False),
    ("narrow", 900, 600, True),
    ("mobile-breakpoint", 640, 720, True),
    ("phone", 390, 844, True),
]

CLARIFY_CONTEXT_VISIBLE_CASES = {"reported"}
CLARIFY_OVERFLOW_OPTIONAL_CASES = {"reported", "narrow"}

CLARIFY_OVERLAP_CASES = [
    "queue",
    "terminal-open",
    "terminal-collapsed",
]

CLARIFY_LAYOUT_PROMPT = {
    "question": (
        "The reliability harness at ~/.local/share/hermes-live-reliability-20260723/ "
        "is consuming 308GB (39 test snapshots). No processes are running. "
        "Delete it to reclaim ~308GB?"
    ),
    "choices": [
        "Yes — delete the entire 308GB reliability harness (receipt-first, batch verify)",
        "Keep the newest 2-3 snapshots, delete the rest (~280GB)",
        "Not yet — investigate why the selector is still creating snapshots first",
        "Just show me the receipt/manifest, I'll decide after",
    ],
}


def _is_benign(text):
    t = text.lower()
    return any(p.lower() in t for p in BENIGN)


def _wait_for_health(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _measure_legacy_sidebar_visibility(page):
    return page.evaluate(
        """
        () => {
          if (
            typeof _legacyWebuiArchive === "undefined" ||
            typeof renderSessionListFromCache !== "function"
          ) {
            throw new Error("legacy sidebar fixture could not reach the session renderer");
          }
          const list = document.getElementById("sessionList");
          if (!list) throw new Error("legacy sidebar fixture is missing #sessionList");

          const previousArchive = _legacyWebuiArchive;
          _legacyWebuiArchive = [{
            session_id: "browser-smoke-legacy-session",
            canonical_id: "browser-smoke-legacy-session",
            title: "Browser smoke legacy conversation",
            archived: true,
            message_count: 3,
          }];
          renderSessionListFromCache();

          const result = {
            sectionCount: list.querySelectorAll(".legacy-webui-archive").length,
            rowCount: list.querySelectorAll(".legacy-webui-archive-item").length,
            headingCount: Array.from(list.querySelectorAll(".session-date-header"))
              .filter((node) => node.textContent.trim() === "Legacy WebUI Archive")
              .length,
          };

          _legacyWebuiArchive = previousArchive;
          renderSessionListFromCache();
          return result;
        }
        """
    )


def _show_clarify_layout_fixture(page, case_name):
    payload = {
        **CLARIFY_LAYOUT_PROMPT,
        "session_id": f"browser-smoke-clarify-{case_name}",
        "clarify_id": f"browser-smoke-clarify-{case_name}",
    }
    page.evaluate(
        """
        (payload) => {
          if (typeof S === "undefined" || typeof showClarifyCard !== "function") {
            throw new Error("clarification UI did not initialize");
          }
          const existing = document.getElementById("clarifyCard");
          if (existing && existing.classList.contains("visible")) {
            hideClarifyCard(true, "dismissed");
          }
          const priorInput = document.getElementById("clarifyInput");
          if (priorInput) {
            priorInput.blur();
            const priorInner = priorInput.closest(".clarify-inner");
            if (priorInner) priorInner.scrollTop = 0;
          }
          const composer = document.getElementById("msg");
          if (composer) composer.blur();
          S.session = {session_id: payload.session_id};
          showClarifyCard({
            _session_id: payload.session_id,
            clarify_id: payload.clarify_id,
            question: payload.question,
            choices_offered: payload.choices,
          });
        }
        """,
        payload,
    )
    page.wait_for_timeout(500)


def _set_clarify_overlap_fixture(page, obstruction):
    page.evaluate(
        """
        (obstruction) => {
          const queueCard = document.getElementById("queueCard");
          const queueChips = document.getElementById("queueChips");
          const terminalPanel = document.getElementById("composerTerminalPanel");
          const terminalDock = document.getElementById("composerTerminalDock");
          const composerWrap = document.getElementById("composerWrap");
          if (!queueCard || !queueChips || !terminalPanel || !terminalDock || !composerWrap) {
            throw new Error("clarification overlap fixture is incomplete");
          }

          queueCard.classList.remove("visible");
          queueChips.replaceChildren();
          terminalPanel.hidden = true;
          terminalPanel.classList.remove("is-open", "is-collapsed");
          terminalDock.hidden = true;
          composerWrap.classList.remove("terminal-dock-visible");

          if (obstruction === "queue") {
            for (let index = 0; index < 8; index += 1) {
              const row = document.createElement("div");
              row.className = "queue-card-row";
              row.textContent = `Queued message ${index + 1}`;
              queueChips.appendChild(row);
            }
            queueCard.classList.add("visible");
          } else if (obstruction === "terminal-open") {
            terminalPanel.hidden = false;
            terminalPanel.classList.add("is-open");
          } else if (obstruction === "terminal-collapsed") {
            terminalPanel.hidden = false;
            terminalDock.hidden = false;
            terminalPanel.classList.add("is-collapsed");
            composerWrap.classList.add("terminal-dock-visible");
          }
        }
        """,
        obstruction,
    )
    page.wait_for_timeout(500)


def _measure_clarify_layout(page):
    return page.evaluate(
        """
        () => {
          const card = document.getElementById("clarifyCard");
          const inner = card && card.querySelector(".clarify-inner");
          const header = card && card.querySelector(".clarify-header");
          const question = document.getElementById("clarifyQuestion");
          const input = document.getElementById("clarifyInput");
          const submit = document.getElementById("clarifySubmit");
          const hint = document.getElementById("clarifyHint");
          const composer = document.getElementById("composerWrap");
          const composerInput = document.getElementById("msg");
          if (
            !card || !inner || !header || !question || !input || !submit ||
            !hint || !composer || !composerInput
          ) {
            throw new Error("clarification layout fixture is incomplete");
          }
          const rect = (node) => {
            const value = node.getBoundingClientRect();
            return {
              top: value.top,
              right: value.right,
              bottom: value.bottom,
              left: value.left,
              width: value.width,
              height: value.height,
            };
          };
          const hitTarget = (node) => {
            const value = node.getBoundingClientRect();
            const target = document.elementFromPoint(
              value.left + (value.width / 2),
              value.bottom - 2
            );
            const control = target && target.closest("#clarifyInput, #clarifySubmit");
            return control && control.id;
          };
          return {
            activeElement: document.activeElement && document.activeElement.id,
            card: rect(card),
            inner: rect(inner),
            header: rect(header),
            question: rect(question),
            input: rect(input),
            submit: rect(submit),
            hint: rect(hint),
            composer: rect(composer),
            innerClientHeight: inner.clientHeight,
            innerScrollHeight: inner.scrollHeight,
            innerScrollTop: inner.scrollTop,
            composerInputDisabled: composerInput.disabled,
            hitTargets: {
              input: hitTarget(input),
              submit: hitTarget(submit),
            },
          };
        }
        """
    )


def _clarify_layout_failures(
    case_name,
    width,
    height,
    geometry,
    *,
    expect_input_focus=True,
    expect_context_visible=False,
    require_overflow=True,
):
    failures = []
    tolerance = 1.0
    prefix = (
        f"  [clarify-layout {case_name} {width}x{height}; "
        f"scroll={geometry['innerScrollTop']:.1f}/"
        f"{geometry['innerScrollHeight'] - geometry['innerClientHeight']:.1f}]"
    )

    if (
        require_overflow
        and geometry["innerScrollHeight"] <= geometry["innerClientHeight"]
    ):
        failures.append(
            f"{prefix} fixture did not overflow; the regression check is not meaningful"
        )
    if expect_input_focus and geometry["activeElement"] != "clarifyInput":
        failures.append(
            f"{prefix} expected clarifyInput focus, got {geometry['activeElement']!r}"
        )
    if not geometry["composerInputDisabled"]:
        failures.append(
            f"{prefix} expected the main composer input to remain locked"
        )
    visible_top = max(geometry["card"]["top"], geometry["inner"]["top"])
    visible_bottom = min(
        geometry["card"]["bottom"],
        geometry["inner"]["bottom"],
        geometry["composer"]["top"],
    )
    if expect_context_visible:
        for context_name in ("header", "question", "hint"):
            context = geometry[context_name]
            if context["top"] < visible_top - tolerance:
                failures.append(
                    f"{prefix} {context_name} top {context['top']:.1f}px is above "
                    f"the visible clarify boundary {visible_top:.1f}px"
                )
            if context["bottom"] > visible_bottom + tolerance:
                failures.append(
                    f"{prefix} {context_name} bottom {context['bottom']:.1f}px is below "
                    f"the visible clarify boundary {visible_bottom:.1f}px"
                )
    for control_name in ("input", "submit"):
        control = geometry[control_name]
        if control["top"] < visible_top - tolerance:
            failures.append(
                f"{prefix} {control_name} top {control['top']:.1f}px is above "
                f"the visible clarify boundary {visible_top:.1f}px"
            )
        if control["bottom"] > visible_bottom + tolerance:
            failures.append(
                f"{prefix} {control_name} bottom {control['bottom']:.1f}px is below "
                f"the visible clarify boundary {visible_bottom:.1f}px"
            )
        if geometry["hitTargets"][control_name] != (
            "clarifyInput" if control_name == "input" else "clarifySubmit"
        ):
            failures.append(
                f"{prefix} {control_name} lower edge hit "
                f"{geometry['hitTargets'][control_name]!r} instead of the control"
            )
    return failures


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    if not os.path.exists(server_py):
        print(f"SETUP FAIL: server.py not found at {server_py}", file=sys.stderr)
        return 2

    state_dir = tempfile.mkdtemp(prefix="hermes-browser-smoke-")
    env = os.environ.copy()
    # Strip real provider keys so nothing leaks into the smoke server.
    for k in list(env):
        if k.endswith("_API_KEY"):
            env.pop(k, None)
    env.update({
        "HERMES_WEBUI_PORT": str(PORT),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": state_dir,
        "HERMES_HOME": state_dir,
        "HERMES_BASE_HOME": state_dir,
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        # Point agent discovery at a path that doesn't exist — the server is
        # designed to boot and serve the UI even when the agent is absent.
        "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
    })

    log = open(os.path.join(state_dir, "server.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, server_py], cwd=repo_root, env=env,
        stdout=log, stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    try:
        if not _wait_for_health(timeout=30):
            print("SETUP FAIL: server did not become healthy in 30s", file=sys.stderr)
            log.flush()
            with open(os.path.join(state_dir, "server.log")) as f:
                print(f.read()[-2000:], file=sys.stderr)
            return 2

        failures = []
        with sync_playwright() as pw:
            launch_options = {
                "headless": True,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            browser_executable = os.getenv("SMOKE_BROWSER_EXECUTABLE", "").strip()
            if browser_executable:
                launch_options["executable_path"] = browser_executable
            browser = pw.chromium.launch(**launch_options)
            for path in PAGES:
                ctx = browser.new_context(base_url=BASE)
                page = ctx.new_page()
                errors = []
                page.on(
                    "console",
                    lambda m, errors=errors: errors.append(("console", m.text))
                    if m.type == "error"
                    else None,
                )
                page.on(
                    "pageerror",
                    lambda e, errors=errors: errors.append(("pageerror", str(e))),
                )

                page.goto(path, wait_until="domcontentloaded")
                # Give boot.js / view init time to run and throw if it's going to.
                try:
                    page.wait_for_selector("#msg, .app, body", timeout=10000)
                except Exception:
                    pass
                time.sleep(1.5)

                if path == "/":
                    legacy_visibility = _measure_legacy_sidebar_visibility(page)
                    if legacy_visibility != {
                        "sectionCount": 0,
                        "rowCount": 0,
                        "headingCount": 0,
                    }:
                        failures.append(
                            "  [legacy-sidebar] sidecar-only archive entered the sidebar DOM: "
                            f"{legacy_visibility!r}"
                        )
                    else:
                        print("OK  legacy WebUI archive remains hidden from the sidebar")

                    for case_name, width, height, show_prompt in CLARIFY_LAYOUT_CASES:
                        page.set_viewport_size({"width": width, "height": height})
                        if show_prompt:
                            _show_clarify_layout_fixture(page, case_name)
                        else:
                            page.wait_for_timeout(350)
                        geometry = _measure_clarify_layout(page)
                        layout_failures = _clarify_layout_failures(
                            case_name,
                            width,
                            height,
                            geometry,
                            expect_context_visible=(
                                case_name in CLARIFY_CONTEXT_VISIBLE_CASES
                            ),
                            require_overflow=(
                                case_name not in CLARIFY_OVERFLOW_OPTIONAL_CASES
                            ),
                        )
                        failures.extend(layout_failures)
                        if not layout_failures:
                            print(
                                f"OK  clarify layout {case_name} {width}x{height} "
                                f"(scrollTop={geometry['innerScrollTop']:.0f})"
                            )

                    page.set_viewport_size({"width": 1262, "height": 759})
                    _show_clarify_layout_fixture(page, "collapse-expand")
                    page.click("#clarifyCollapse")
                    page.wait_for_timeout(300)
                    collapsed_state = page.evaluate(
                        """
                        () => {
                          const card = document.getElementById("clarifyCard");
                          const button = document.getElementById("clarifyCollapse");
                          const response = card && card.querySelector(".clarify-response");
                          return {
                            collapsed: Boolean(card && card.classList.contains("collapsed")),
                            expanded: button && button.getAttribute("aria-expanded"),
                            responseDisplay: response && getComputedStyle(response).display,
                          };
                        }
                        """
                    )
                    if collapsed_state != {
                        "collapsed": True,
                        "expanded": "false",
                        "responseDisplay": "none",
                    }:
                        failures.append(
                            "  [clarify-layout collapse 1262x759] collapsed state "
                            f"was not preserved: {collapsed_state!r}"
                        )
                    page.click("#clarifyCollapse")
                    page.wait_for_timeout(500)
                    geometry = _measure_clarify_layout(page)
                    expand_failures = _clarify_layout_failures(
                        "collapse-expand",
                        1262,
                        759,
                        geometry,
                        expect_input_focus=False,
                    )
                    if geometry["activeElement"] != "clarifyCollapse":
                        expand_failures.append(
                            "  [clarify-layout collapse-expand 1262x759] "
                            "collapse toggle did not retain focus after expanding"
                        )
                    failures.extend(expand_failures)
                    if not expand_failures:
                        print(
                            "OK  clarify collapse/expand 1262x759 "
                            f"(scrollTop={geometry['innerScrollTop']:.0f})"
                        )

                    page.set_viewport_size({"width": 1262, "height": 759})
                    _show_clarify_layout_fixture(page, "reported-overlap")
                    for obstruction in CLARIFY_OVERLAP_CASES:
                        _set_clarify_overlap_fixture(page, obstruction)
                        geometry = _measure_clarify_layout(page)
                        overlap_failures = _clarify_layout_failures(
                            f"reported-with-{obstruction}", 1262, 759, geometry
                        )
                        failures.extend(overlap_failures)
                        if not overlap_failures:
                            print(
                                f"OK  clarify layout with {obstruction} 1262x759 "
                                f"(scrollTop={geometry['innerScrollTop']:.0f})"
                            )
                    _set_clarify_overlap_fixture(page, "none")

                meaningful = [(kind, txt) for (kind, txt) in errors if not _is_benign(txt)]
                if meaningful:
                    for kind, txt in meaningful:
                        failures.append(f"  [{path}] {kind}: {txt}")
                else:
                    print(f"OK  {path} — no console errors")
                ctx.close()
            browser.close()

        if failures:
            print("\nBROWSER SMOKE FAILED — browser regressions detected:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print(
            "\nBROWSER SMOKE PASSED — pages loaded cleanly and critical layout checks passed"
        )
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
