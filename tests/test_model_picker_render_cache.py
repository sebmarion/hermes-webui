"""Behavioral regression coverage for the composer model-picker render cache."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function_body(name: str) -> str:
    source = UI_JS.read_text(encoding="utf-8")
    marker = f"function {name}("
    start = source.index(marker)
    opening = source.index("){", start) + 1
    depth = 1
    cursor = opening + 1
    while depth:
        if source[cursor] == "{":
            depth += 1
        elif source[cursor] == "}":
            depth -= 1
        cursor += 1
    return source[start:cursor]


def _run_node(source: str) -> dict:
    completed = subprocess.run(
        [NODE, "-e", source, str(UI_JS)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_repeat_open_reuses_composer_picker_dom():
    driver = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[1], 'utf8');
function extract(name) {
  let start = ui.indexOf('async function ' + name + '(');
  if (start < 0) start = ui.indexOf('function ' + name + '(');
  if (start < 0) throw new Error(name + ' missing');
  let i = ui.indexOf('{', start) + 1, depth = 1;
  while (depth && i < ui.length) {
    if (ui[i] === '{') depth++;
    else if (ui[i] === '}') depth--;
    i++;
  }
  return ui.slice(start, i);
}
const open = new Set();
const classList = {
  contains(name) { return open.has(name); },
  add(name) { open.add(name); },
  remove(name) { open.delete(name); },
};
let renderedNode = null;
let firstNode = null;
let renderCount = 0;
const dd = {
  classList,
  dataset: {},
  querySelector(selector) { return selector === '.model-opt.active' ? renderedNode : null; },
};
const chip = {classList: {add(){}, remove(){}}};
const sel = {id: 'modelSelect'};
function $(id) {
  if (id === 'composerModelDropdown') return dd;
  if (id === 'composerModelChip') return chip;
  if (id === 'modelSelect') return sel;
  if (id === 'composerMobileModelAction') return null;
  return null;
}
const window = {_ensureModelDropdownReady(){ return Promise.resolve(); }};
function closeProfileDropdown() {}
function closeWsDropdown() {}
function closeReasoningDropdown() {}
function closeToolsetsDropdown() {}
function renderModelDropdown() {
  renderCount++;
  renderedNode = {scrollIntoView(){}};
  if (!firstNode) firstNode = renderedNode;
}
function _positionModelDropdown() {}
function closeModelDropdown() { dd.classList.remove('open'); }
eval(extract('_renderComposerModelDropdownIfDirty'));
eval(extract('toggleModelDropdown'));
(async () => {
  await toggleModelDropdown();
  const nodeAfterFirstOpen = renderedNode;
  await toggleModelDropdown();
  await toggleModelDropdown();
  process.stdout.write(JSON.stringify({
    renderCount,
    sameNode: nodeAfterFirstOpen === renderedNode,
  }));
})();
"""
    result = _run_node(driver)
    assert result == {"renderCount": 1, "sameNode": True}


def test_closed_composer_selection_mutations_invalidate_render_cache():
    apply_body = _function_body("_applyModelToDropdown")
    ensure_body = _function_body("_ensureModelOptionInDropdown")
    fallback_body = _function_body("_applySessionModelFallback")

    assert "sel.id==='modelSelect'" in apply_body
    assert "_invalidateComposerModelDropdown()" in apply_body
    assert "sel.id==='modelSelect'" in ensure_body
    assert "_invalidateComposerModelDropdown()" in ensure_body
    assert "_invalidateComposerModelDropdown()" in fallback_body


def test_closed_catalog_mutations_invalidate_composer_render_cache():
    populate_body = _function_body("populateModelDropdown")
    live_body = _function_body("_addLiveModelsToSelect")

    assert "_invalidateComposerModelDropdown()" in populate_body
    assert "sel.id==='modelSelect'" in live_body
    assert "added>0" in live_body
    assert "_invalidateComposerModelDropdown()" in live_body


def test_model_catalog_restores_profile_scoped_browser_cache_before_fetch():
    populate_body = _function_body("populateModelDropdown")
    restore_body = _function_body("_restoreCachedModelCatalog")
    persist_body = _function_body("_persistModelCatalogCache")

    assert "_restoreCachedModelCatalog(sel)" in populate_body
    assert populate_body.index("_restoreCachedModelCatalog(sel)") < populate_body.index("await fetch(")
    assert "hermes-webui-model-catalog:" in _function_body("_modelCatalogStorageKey")
    assert "localStorage.getItem" in restore_body
    assert "sel.innerHTML=cached.html" in restore_body
    assert "localStorage.setItem" in persist_body
    assert "_persistModelCatalogCache(sel)" in populate_body


def test_open_compound_invalidations_coalesce_to_one_render():
    driver = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[1], 'utf8');
function extract(name) {
  const start = ui.indexOf('function ' + name + '(');
  if (start < 0) throw new Error(name + ' missing');
  let i = ui.indexOf('){', start) + 2, depth = 1;
  while (depth && i < ui.length) {
    if (ui[i] === '{') depth++;
    else if (ui[i] === '}') depth--;
    i++;
  }
  return ui.slice(start, i);
}
let renderCount = 0;
const dd = {
  dataset: {renderCacheClean: '1'},
  classList: {contains(name){ return name === 'open'; }},
};
function $(id) { return id === 'composerModelDropdown' ? dd : null; }
function renderModelDropdown() { renderCount++; }
function _positionModelDropdown() {}
eval(extract('_renderComposerModelDropdownIfDirty'));
eval(extract('_invalidateComposerModelDropdown'));
_invalidateComposerModelDropdown();
_invalidateComposerModelDropdown();
Promise.resolve().then(() => {
  process.stdout.write(JSON.stringify({renderCount, clean: dd.dataset.renderCacheClean}));
});
"""
    result = _run_node(driver)
    assert result == {"renderCount": 1, "clean": "1"}


def test_compound_catalog_paths_do_not_render_composer_directly():
    populate_body = _function_body("populateModelDropdown")
    refresh_body = _function_body("_refreshOpenModelDropdown")

    assert "renderModelDropdown()" not in populate_body
    assert "_invalidateComposerModelDropdown()" in refresh_body


def test_settings_picker_refresh_remains_uncached():
    refresh_body = _function_body("_refreshOpenModelDropdown")

    assert "settingsModelDropdown" in refresh_body
    assert "selectId:'settingsModel'" in refresh_body
    assert "renderModelDropdown({" in refresh_body


def test_real_selection_helper_dirties_closed_cache_and_updates_active_row():
    driver = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[1], 'utf8');
function extract(name) {
  let start = ui.indexOf('async function ' + name + '(');
  if (start < 0) start = ui.indexOf('function ' + name + '(');
  if (start < 0) throw new Error(name + ' missing');
  let i = ui.indexOf('){', start) + 2, depth = 1;
  while (depth && i < ui.length) {
    if (ui[i] === '{') depth++;
    else if (ui[i] === '}') depth--;
    i++;
  }
  return ui.slice(start, i);
}
const open = new Set();
let renderCount = 0;
let activeModel = '';
const dd = {
  dataset: {},
  classList: {
    contains(name){ return open.has(name); },
    add(name){ open.add(name); },
    remove(name){ open.delete(name); },
  },
  querySelector(selector){
    return selector === '.model-opt.active' ? {scrollIntoView(){}, model: activeModel} : null;
  },
};
const chip = {classList:{add(){},remove(){}}};
const sel = {id:'modelSelect', value:'model-a'};
function $(id){
  if(id === 'composerModelDropdown') return dd;
  if(id === 'composerModelChip') return chip;
  if(id === 'modelSelect') return sel;
  return null;
}
function _findModelInDropdown(model){ return model; }
function _modelStateForSelect(select, model){ return {model:model || select.value, model_provider:null}; }
function syncModelChip() {}
function renderModelDropdown(){ renderCount++; activeModel=sel.value; }
function _positionModelDropdown() {}
function closeProfileDropdown() {}
function closeWsDropdown() {}
function closeReasoningDropdown() {}
function closeToolsetsDropdown() {}
function closeModelDropdown(){ dd.classList.remove('open'); }
const window = {_ensureModelDropdownReady(){ return Promise.resolve(); }};
eval(extract('_renderComposerModelDropdownIfDirty'));
eval(extract('_invalidateComposerModelDropdown'));
eval(extract('_applyModelToDropdown'));
eval(extract('toggleModelDropdown'));
(async()=>{
  await toggleModelDropdown();
  await toggleModelDropdown();
  _applyModelToDropdown('model-b', sel, null);
  await toggleModelDropdown();
  process.stdout.write(JSON.stringify({renderCount, activeModel}));
})();
"""
    result = _run_node(driver)
    assert result == {"renderCount": 2, "activeModel": "model-b"}


def test_microtask_close_and_reopen_races_preserve_dirty_contract():
    driver = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[1], 'utf8');
function extract(name) {
  let start = ui.indexOf('async function ' + name + '(');
  if (start < 0) start = ui.indexOf('function ' + name + '(');
  if (start < 0) throw new Error(name + ' missing');
  let i = ui.indexOf('){', start) + 2, depth = 1;
  while (depth && i < ui.length) {
    if (ui[i] === '{') depth++;
    else if (ui[i] === '}') depth--;
    i++;
  }
  return ui.slice(start, i);
}
const open = new Set(['open']);
let renderCount = 0;
const dd = {
  dataset:{renderCacheClean:'1'},
  classList:{
    contains(name){return open.has(name);},
    add(name){open.add(name);},
    remove(name){open.delete(name);},
  },
  querySelector(){return null;},
};
const chip={classList:{add(){},remove(){}}};
const sel={id:'modelSelect'};
function $(id){
  if(id==='composerModelDropdown') return dd;
  if(id==='composerModelChip') return chip;
  if(id==='modelSelect') return sel;
  return null;
}
function renderModelDropdown(){renderCount++;}
function _positionModelDropdown(){}
function closeProfileDropdown(){}
function closeWsDropdown(){}
function closeReasoningDropdown(){}
function closeToolsetsDropdown(){}
function closeModelDropdown(){dd.classList.remove('open');}
const window={_ensureModelDropdownReady(){return Promise.resolve();}};
eval(extract('_renderComposerModelDropdownIfDirty'));
eval(extract('_invalidateComposerModelDropdown'));
eval(extract('toggleModelDropdown'));
(async()=>{
  _invalidateComposerModelDropdown();
  dd.classList.remove('open');
  await Promise.resolve();
  const closeBeforeFlush={renderCount,clean:dd.dataset.renderCacheClean,pending:dd.dataset.renderRefreshPending};

  dd.dataset.renderCacheClean='1';
  dd.classList.add('open');
  _invalidateComposerModelDropdown();
  dd.classList.remove('open');
  await toggleModelDropdown();
  await Promise.resolve();
  const reopenBeforeFlush={renderCount,clean:dd.dataset.renderCacheClean,pending:dd.dataset.renderRefreshPending};
  process.stdout.write(JSON.stringify({closeBeforeFlush,reopenBeforeFlush}));
})();
"""
    result = _run_node(driver)
    assert result == {
        "closeBeforeFlush": {"renderCount": 0, "clean": "0", "pending": "0"},
        "reopenBeforeFlush": {"renderCount": 1, "clean": "1", "pending": "0"},
    }
