"""Browserless regression coverage for first-tab model catalog refresh (#4737)."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="node is required to execute the model catalog refresh harness",
)


_DRIVER = r"""
const fs = require('fs');
const scenario = JSON.parse(process.argv[2] || '{}');
const fnSource = fs.readFileSync(process.argv[3], 'utf8');

function serializeNode(node) {
  if (node.tagName === 'OPTION') {
    return `<option value="${node.value}">${node.textContent}</option>`;
  }
  if (node.tagName === 'OPTGROUP') {
    const provider = node.dataset.provider
      ? ` data-provider="${node.dataset.provider}"`
      : '';
    return `<optgroup label="${node.label}"${provider}>${node.children.map(serializeNode).join('')}</optgroup>`;
  }
  return '';
}

class FakeStorage {
  constructor(seed = {}) {
    this.store = { ...seed };
  }

  getItem(key) {
    return Object.prototype.hasOwnProperty.call(this.store, key)
      ? this.store[key]
      : null;
  }

  setItem(key, value) {
    this.store[key] = String(value);
  }

  removeItem(key) {
    delete this.store[key];
  }
}

class FakeNode {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.classList = { contains: () => false };
    this.style = {};
    this.textContent = '';
    this.label = '';
    this.value = '';
    this.parentNode = null;
  }

  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    return child;
  }
}

class FakeSelect extends FakeNode {
  constructor() {
    super('select');
    this.id = 'modelSelect';
    this._innerHTML = '';
    this._value = '';
  }

  set innerHTML(value) {
    this._innerHTML = value;
    this.children = [];
  }

  get innerHTML() {
    if (this.children.length) {
      return this.children.map(serializeNode).join('');
    }
    return this._innerHTML;
  }

  get options() {
    const options = [];
    for (const child of this.children) {
      if (child.tagName === 'OPTGROUP') {
        options.push(...child.children);
      } else if (child.tagName === 'OPTION') {
        options.push(child);
      }
    }
    return options;
  }

  set value(value) {
    this._value = value;
  }

  get value() {
    return this._value;
  }

  querySelector(selector) {
    if (selector === 'optgroup > option, option') {
      return this.options[0] || null;
    }
    return null;
  }
}

function buildFetchResponse(payload, status = 200) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}

function buildHarness(currentScenario) {
  const select = new FakeSelect();
  const dropdown = new FakeNode('div');
  const fetchCalls = [];
  const liveFetchCalls = [];
  const defaultRedirectCalls = [];
  const customRedirectCalls = [];
  const applyModelCalls = [];
  const counters = { invalidateCalls: 0, syncModelCalls: 0 };
  const fetchQueue = (currentScenario.fetchResponses || []).map((payload) => buildFetchResponse(
    payload && payload.body ? payload.body : payload,
    payload && payload.status ? payload.status : 200,
  ));
  const profile = currentScenario.profile || 'default';
  const storageSeed = {};
  if (currentScenario.cachedCatalog) {
    const key = `hermes-webui-model-catalog:${encodeURIComponent(String(profile))}`;
    storageSeed[key] = JSON.stringify(currentScenario.cachedCatalog);
  }

  globalThis.window = globalThis;
  globalThis.document = {
    baseURI: 'http://localhost/session/abc',
    createElement(tag) {
      return new FakeNode(tag);
    },
  };
  globalThis.location = { href: 'http://localhost/session/abc' };
  globalThis.sessionStorage = new FakeStorage();
  globalThis.localStorage = new FakeStorage(storageSeed);
  globalThis.S = { pendingFiles: [], activeProfile: profile };
  globalThis._dynamicModelLabels = {};
  globalThis._modelEndpointErrors = {};
  globalThis._defaultModel = null;
  globalThis._activeProvider = null;
  globalThis._configuredModelBadges = {};
  globalThis._modelDropdownRequestSeq = 0;
  globalThis._modelCatalogFallbackRetried = false;
  select.value = currentScenario.initialSelection || '';
  globalThis.$ = (id) => {
    if (id === 'modelSelect') return select;
    if (id === 'composerModelDropdown') return dropdown;
    return null;
  };
  globalThis.getModelLabel = (id) => `label:${id}`;
  globalThis._captureModelDropdownSelection = () => null;
  globalThis._applyModelToDropdown = (model, target, provider) => {
    applyModelCalls.push([model, target && target.id ? target.id : null, provider]);
    if (target) target.value = model;
    return model;
  };
  globalThis._reconcileModelDropdownSelection = (_sel, data) => {
    const firstGroup = Array.isArray(data.groups) && data.groups.length ? data.groups[0] : null;
    const firstModel = firstGroup && Array.isArray(firstGroup.models) && firstGroup.models.length
      ? firstGroup.models[0]
      : null;
    if (firstModel) _sel.value = firstModel.id;
  };
  globalThis._invalidateComposerModelDropdown = () => {
    counters.invalidateCalls += 1;
  };
  globalThis.syncModelChip = () => {
    counters.syncModelCalls += 1;
  };
  globalThis.renderModelDropdown = () => {};
  globalThis._positionModelDropdown = () => {};
  globalThis._redirectIfUnauth = (res) => {
    defaultRedirectCalls.push(res.status);
    return res.status === 401;
  };
  globalThis.console = { warn() {}, debug() {}, log() {} };
  globalThis._fetchLiveModels = (provider, sel, requestSeq) => {
    liveFetchCalls.push([provider, sel && sel.id ? sel.id : null, requestSeq]);
  };
  globalThis.fetch = async (url) => {
    fetchCalls.push(String(url));
    if (!fetchQueue.length) throw new Error(`unexpected fetch: ${url}`);
    return fetchQueue.shift();
  };

  return {
    select,
    fetchCalls,
    liveFetchCalls,
    defaultRedirectCalls,
    customRedirectCalls,
    applyModelCalls,
    counters,
  };
}

async function runScenario(currentScenario) {
  const {
    select,
    fetchCalls,
    liveFetchCalls,
    defaultRedirectCalls,
    customRedirectCalls,
    applyModelCalls,
    counters,
  } = buildHarness(currentScenario);
  let _modelDropdownRequestSeq = 0;
  let _modelCatalogFallbackRetried = false;
  let _modelCatalogBrowserCacheRestored = false;
  eval(fnSource);
  const callOpts = { ...(currentScenario.opts || {}) };
  if (currentScenario.useCustomRedirect) {
    callOpts.redirectIfUnauth = (res) => {
      customRedirectCalls.push(res.status);
      return res.status === 401;
    };
  }
  await populateModelDropdown(callOpts);
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
  return {
    customRedirectCalls,
    defaultRedirectCalls,
    fetchCalls,
    liveFetchCalls,
    optionValues: select.options.map((opt) => opt.value),
    selectInnerHTML: select.innerHTML,
    storage: { ...localStorage.store },
    dynamicModelLabels: { ...globalThis._dynamicModelLabels },
    activeProvider: globalThis._activeProvider,
    defaultModel: globalThis._defaultModel,
    configuredModelBadges: { ...globalThis._configuredModelBadges },
    applyModelCalls,
    invalidateCalls: counters.invalidateCalls,
    syncModelCalls: counters.syncModelCalls,
  };
}

runScenario(scenario).then((result) => {
  process.stdout.write(JSON.stringify(result));
}).catch((error) => {
  process.stderr.write(String(error && error.stack || error));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("model-catalog-refresh-driver") / "driver.js"
    path.write_text(_DRIVER, encoding="utf-8")
    return str(path)


def _extract_js_function(name):
    markers = (f"function {name}(", f"async function {name}(")
    starts = [UI_JS.find(marker) for marker in markers]
    start = min(index for index in starts if index >= 0)
    marker = next(marker for marker in markers if UI_JS.startswith(marker, start))
    paren_depth = 1
    idx = start + len(marker)
    while idx < len(UI_JS) and paren_depth > 0:
        char = UI_JS[idx]
        if char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth -= 1
        idx += 1
    if paren_depth != 0:
        raise AssertionError("could not locate populateModelDropdown signature")
    brace_start = UI_JS.index("{", idx)
    depth = 0
    for idx in range(brace_start, len(UI_JS)):
        char = UI_JS[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                fn_source = UI_JS[start : idx + 1]
                break
    else:
        raise AssertionError(f"could not extract {name}")
    return fn_source


def _run(driver_path, scenario):
    fn_source = "\n\n".join(
        _extract_js_function(name)
        for name in (
            "_modelCatalogStorageKey",
            "_restoreCachedModelCatalog",
            "_persistModelCatalogCache",
            "populateModelDropdown",
        )
    )
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".js", delete=False) as handle:
        handle.write(fn_source)
        fn_source_path = handle.name
    try:
        process = subprocess.run(
            [NODE, driver_path, json.dumps(scenario), fn_source_path],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        Path(fn_source_path).unlink(missing_ok=True)
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip())
    return json.loads(process.stdout)


def test_populate_model_dropdown_refetches_once_when_server_returns_empty_groups(driver_path):
    payload = _run(
        driver_path,
        {
            "fetchResponses": [
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [],
                },
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [
                        {
                            "provider": "Anthropic",
                            "provider_id": "anthropic",
                            "models": [
                                {"id": "claude-sonnet-4", "label": "Claude Sonnet 4"},
                                {"id": "claude-opus-4", "label": "Claude Opus 4"},
                            ],
                        }
                    ],
                },
            ],
        },
    )

    assert len(payload["fetchCalls"]) == 2
    assert "freshness=session_visit" in payload["fetchCalls"][1]
    assert payload["liveFetchCalls"] == [["anthropic", "modelSelect", 2]]
    assert "claude-opus-4" in payload["optionValues"]


def test_populate_model_dropdown_does_not_refetch_when_server_groups_already_populated(driver_path):
    payload = _run(
        driver_path,
        {
            "fetchResponses": [
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [
                        {
                            "provider": "Anthropic",
                            "provider_id": "anthropic",
                            "models": [
                                {"id": "claude-sonnet-4", "label": "Claude Sonnet 4"},
                            ],
                        }
                    ],
                }
            ],
        },
    )

    assert len(payload["fetchCalls"]) == 1
    assert payload["optionValues"] == ["claude-sonnet-4"]


def test_populate_model_dropdown_retries_at_most_once_even_if_refetch_is_still_empty(driver_path):
    payload = _run(
        driver_path,
        {
            "fetchResponses": [
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [],
                },
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [],
                },
            ],
        },
    )

    assert len(payload["fetchCalls"]) == 2
    assert payload["fetchCalls"][1].endswith("freshness=session_visit")
    assert payload["optionValues"] == ["claude-sonnet-4"]


def test_populate_model_dropdown_retries_when_synth_fallback_is_empty(driver_path):
    payload = _run(
        driver_path,
        {
            "fetchResponses": [
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "@anthropic:claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [],
                },
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [
                        {
                            "provider": "Anthropic",
                            "provider_id": "anthropic",
                            "models": [
                                {"id": "claude-sonnet-4", "label": "Claude Sonnet 4"},
                            ],
                        }
                    ],
                },
            ],
        },
    )

    assert len(payload["fetchCalls"]) == 2
    assert payload["fetchCalls"][1].endswith("freshness=session_visit")
    assert payload["optionValues"] == ["claude-sonnet-4"]


def test_populate_model_dropdown_retry_preserves_custom_redirect_handler(driver_path):
    payload = _run(
        driver_path,
        {
            "useCustomRedirect": True,
            "fetchResponses": [
                {
                    "active_provider": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "configured_model_badges": {
                        "claude-sonnet-4": {"provider": "anthropic"},
                    },
                    "groups": [],
                },
                {
                    "status": 401,
                    "body": {
                        "active_provider": "anthropic",
                        "default_model": "claude-sonnet-4",
                        "configured_model_badges": {
                            "claude-sonnet-4": {"provider": "anthropic"},
                        },
                        "groups": [],
                    },
                },
            ],
        },
    )

    assert payload["fetchCalls"][1].endswith("freshness=session_visit")
    assert payload["customRedirectCalls"] == [200, 401]
    assert payload["defaultRedirectCalls"] == []


def test_populate_model_dropdown_restores_profile_cache_before_failed_refresh(
    driver_path,
):
    cached = {
        "version": 1,
        "html": '<optgroup label="Cached"><option value="cached-model">Cached</option></optgroup>',
        "labels": {"cached-model": "Cached Model"},
        "activeProvider": "cached-provider",
        "defaultModel": "cached-model",
        "badges": {"cached-model": {"provider": "cached-provider"}},
    }

    payload = _run(
        driver_path,
        {
            "profile": "deep work",
            "initialSelection": "cached-model",
            "cachedCatalog": cached,
            "fetchResponses": [],
        },
    )

    assert payload["selectInnerHTML"] == cached["html"]
    assert payload["dynamicModelLabels"] == cached["labels"]
    assert payload["activeProvider"] == "cached-provider"
    assert payload["defaultModel"] == "cached-model"
    assert payload["configuredModelBadges"] == cached["badges"]
    assert payload["applyModelCalls"] == [
        ["cached-model", "modelSelect", None],
    ]
    assert payload["invalidateCalls"] >= 1
    assert json.loads(
        payload["storage"]["hermes-webui-model-catalog:deep%20work"]
    ) == cached


def test_populate_model_dropdown_persists_complete_profile_catalog(driver_path):
    payload = _run(
        driver_path,
        {
            "profile": "deep work",
            "fetchResponses": [
                {
                    "active_provider": "anthropic",
                    "default_model": "model-a",
                    "configured_model_badges": {
                        "model-a": {"provider": "anthropic"},
                    },
                    "groups": [
                        {
                            "provider": "Anthropic",
                            "provider_id": "anthropic",
                            "models": [
                                {"id": "model-a", "label": "Model A"},
                            ],
                        }
                    ],
                }
            ],
        },
    )

    cached = json.loads(
        payload["storage"]["hermes-webui-model-catalog:deep%20work"]
    )
    assert cached["version"] == 1
    assert "model-a" in cached["html"]
    assert cached["labels"] == {"model-a": "Model A"}
    assert cached["activeProvider"] == "anthropic"
    assert cached["defaultModel"] == "model-a"
    assert cached["badges"] == {"model-a": {"provider": "anthropic"}}
