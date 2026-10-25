"""Form discovery, filling and submission."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

from playwright.async_api import Page, Request, Response

from ..datagen import hint_from_validation
from .clickables import unique_locators
from .locators import LOCATOR_JS, rank_locators, resolve
from .settle import wait_settled

if TYPE_CHECKING:
    from ..datagen import DataGen

log = logging.getLogger(__name__)

Widget = Literal["input", "select", "multiselect", "checkbox", "radio", "textarea", "date",
                 "combobox", "richtext", "file"]
_TEXT_WIDGETS = {"input", "textarea", "richtext", "date"}
_ACTION_TIMEOUT = 4000


@dataclass
class FieldInfo:
    """One fillable control. For ``file`` fields ``options`` holds the ``accept`` entries."""
    name: str
    type: str
    widget: Widget
    required: bool = False
    pattern: str | None = None
    min: str | None = None
    max: str | None = None
    maxlength: int | None = None
    options: list[str] = field(default_factory=list)
    locators: list[dict] = field(default_factory=list)
    label: str = ""


@dataclass
class FormInfo:
    inferred: bool
    method: str | None
    action: str | None
    fields: list[FieldInfo]
    submit_locators: list[dict]
    has_confirm: bool = False
    label: str = ""


@dataclass
class SubmitResult:
    success: bool
    attempts: int
    validation_messages: dict[str, list[str]]
    exchange_ids: list[int]
    to_url: str


_DISCOVER_JS = """() => {
  const locate = (__LOCATOR__);
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const FIELD = 'input,select,textarea,[contenteditable]:not([contenteditable=false]),[role=combobox]';
  const SUBMIT_RX = /save|submit|create|add|update|send|search|next|continue/i;
  const NONFIELD = ['hidden', 'submit', 'button', 'reset', 'image'];
  const DATES = ['date', 'datetime-local', 'month', 'time', 'week'];
  const visible = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const isField = (el) => {
    if (el.tagName === 'INPUT') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (NONFIELD.includes(t)) return false;
      if (t !== 'file' && !visible(el)) return false;
    } else if (!visible(el)) return false;
    if (el.disabled) return false;
    // A combobox wrapper that contains a real input is represented by that input.
    if (el.getAttribute('role') === 'combobox' && el.querySelector('input,textarea')) return false;
    if (el.hasAttribute('contenteditable') && el.closest('[role=combobox]') !== null
        && el.closest('[role=combobox]') !== el && !el.matches('[role=combobox]')) return false;
    return true;
  };
  const fieldsIn = (root, outsideForms) => [...root.querySelectorAll(FIELD)]
    .filter(el => isField(el) && (!outsideForms || !el.closest('form')));
  const labelOf = (el, raw) => raw.label || norm(el.getAttribute('aria-label'))
    || norm(el.getAttribute('placeholder')) || norm(el.getAttribute('title'))
    || (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA' ? '' : norm(el.innerText).slice(0, 40));
  const isCombo = (el) => el.getAttribute('role') === 'combobox' || (el.closest('[role=combobox]') !== null);
  const describe = (el) => {
    const tag = el.tagName.toLowerCase();
    const typ = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tag;
    let widget;
    if (isCombo(el)) widget = 'combobox';
    else if (tag === 'select') widget = el.multiple ? 'multiselect' : 'select';
    else if (tag === 'textarea') widget = 'textarea';
    else if (tag === 'input') widget = typ === 'checkbox' ? 'checkbox' : typ === 'radio' ? 'radio'
      : typ === 'file' ? 'file' : DATES.includes(typ) ? 'date' : 'input';
    else widget = 'richtext';
    const raw = locate(el);
    const maxlen = parseInt(el.getAttribute('maxlength') || '', 10);
    let options = [];
    if (tag === 'select') options = [...el.options].filter(o => !o.disabled).map(o => o.value);
    else if (widget === 'file') options = (el.getAttribute('accept') || '').split(',').map(s => s.trim()).filter(Boolean);
    else if (widget === 'combobox') {
      const ctl = el.getAttribute('aria-controls');
      const root = (ctl && document.getElementById(ctl)) || el;
      options = [...root.querySelectorAll('[role=option]')].map(o => norm(o.innerText));
    } else if (widget === 'radio' || widget === 'checkbox') options = [el.getAttribute('value') || 'on'];
    const name = el.getAttribute('name') || el.id || norm(el.getAttribute('aria-label'))
      || raw.label || norm(el.getAttribute('placeholder')) || (el.classList[0] || '') || tag;
    return {name, type: typ, widget, raw, label: labelOf(el, raw),
      required: el.required === true || el.getAttribute('aria-required') === 'true',
      pattern: el.getAttribute('pattern'), min: el.getAttribute('min'), max: el.getAttribute('max'),
      maxlength: Number.isNaN(maxlen) ? null : maxlen, options};
  };
  const group = (els) => {
    const out = [], byName = new Map();
    for (const el of els) {
      const d = describe(el);
      if ((d.widget === 'radio' || d.widget === 'checkbox') && el.getAttribute('name')) {
        const key = d.widget + ':' + d.name;
        if (byName.has(key)) { byName.get(key).options.push(...d.options); continue; }
        byName.set(key, d);
      }
      out.push(d);
    }
    for (const d of out) if (d.widget === 'checkbox' && d.options.length > 1) d.widget = 'multiselect';
    return out;
  };
  const btnText = (b) => norm(b.getAttribute('aria-label')) || norm(b.innerText) || norm(b.value);
  const hasConfirm = (root, buttons) => {
    const attrs = ['onsubmit', 'onclick'];
    const els = [root, ...buttons];
    return els.some(e => e && (/confirm\\s*\\(/.test(e.getAttribute('onsubmit') || '')
      || /confirm\\s*\\(/.test(e.getAttribute('onclick') || '')
      || e.hasAttribute('data-confirm') || e.hasAttribute('data-turbo-confirm')));
  };
  const absUrl = (a) => { try { return new URL(a === null ? location.href : a, document.baseURI).href; }
    catch (e) { return a === null ? location.href : a; } };
  const out = [];
  for (const form of document.forms) {
    if (!visible(form)) continue;
    let buttons = [...form.querySelectorAll(
      'button:not([type=button]):not([type=reset]), input[type=submit], input[type=image]')];
    if (!buttons.length) buttons = [...form.querySelectorAll('button, [role=button]')];
    const override = form.querySelector('input[type=hidden][name=_method]');
    const methodAttr = (form.getAttribute('method') || 'get');
    const act = form.getAttribute('action');
    out.push({
      inferred: false,
      method: ((override && override.value) || methodAttr).toUpperCase(),
      action: absUrl(act),
      fields: group(fieldsIn(form, false)).map(d => d),
      submit: buttons.filter(visible).map(b => locate(b)),
      hasConfirm: hasConfirm(form, buttons),
      label: norm(form.getAttribute('aria-label')) || form.id || form.getAttribute('name')
        || (buttons[0] ? btnText(buttons[0]) : '') || (act || ''),
    });
  }
  const containers = new Map();
  const btns = document.querySelectorAll('button, input[type=submit], input[type=button], [role=button]');
  for (const b of btns) {
    if (b.closest('form') || !visible(b) || !SUBMIT_RX.test(btnText(b))) continue;
    let c = b.parentElement;
    while (c && !fieldsIn(c, true).length) c = c.parentElement;
    if (!c) continue;
    if (!containers.has(c)) containers.set(c, []);
    containers.get(c).push(b);
  }
  for (const [c, bs] of containers) {
    out.push({inferred: true, method: null, action: null,
      fields: group(fieldsIn(c, true)), submit: bs.map(b => locate(b)),
      hasConfirm: hasConfirm(null, bs), label: btnText(bs[0])});
  }
  return out;
}""".replace("__LOCATOR__", LOCATOR_JS)


async def discover_forms(page: Page) -> list[FormInfo]:
    raw_forms = await page.evaluate(_DISCOVER_JS)
    raws = [r for f in raw_forms for r in [d["raw"] for d in f["fields"]] + f["submit"]]
    dups = Counter((l["strategy"], l["value"]) for r in raws for l in rank_locators(r))
    forms: list[FormInfo] = []
    for f in raw_forms:
        fields = []
        for d in f["fields"]:
            locs = await unique_locators(page, d["raw"], dups)
            fields.append(FieldInfo(
                name=d["name"], type=d["type"], widget=d["widget"], required=d["required"],
                pattern=d["pattern"], min=d["min"], max=d["max"], maxlength=d["maxlength"],
                options=d["options"], locators=locs, label=d["label"]))
        submit_locs = []
        for r in f["submit"]:
            locs = await unique_locators(page, r, dups)
            if locs:
                submit_locs = locs
                break
        forms.append(FormInfo(f["inferred"], f["method"], f["action"], fields, submit_locs,
                              f["hasConfirm"], f["label"]))
    return forms


# --- filling --------------------------------------------------------------------------------

async def _fill_field(page: Page, fld: FieldInfo, value) -> bool:
    loc = await resolve(page, fld.locators)
    if loc is None:
        return False
    w = fld.widget
    try:
        if w == "input" and fld.type in ("range", "color"):
            await loc.fill(str(value), timeout=_ACTION_TIMEOUT)
        elif w in ("input", "textarea"):
            await loc.fill("", timeout=_ACTION_TIMEOUT)
            await loc.press_sequentially(str(value), timeout=_ACTION_TIMEOUT)
        elif w == "date":
            await loc.fill(str(value), timeout=_ACTION_TIMEOUT)
        elif w == "richtext":
            await loc.click(timeout=_ACTION_TIMEOUT)
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.press("Delete")
            await loc.press_sequentially(str(value), timeout=_ACTION_TIMEOUT)
        elif w in ("select", "multiselect"):
            vals = value if isinstance(value, list) else [value]
            if w == "multiselect" and fld.type == "checkbox":
                await loc.set_checked(True, timeout=_ACTION_TIMEOUT)  # checkbox group: tick the first
            elif vals and vals[0] != "":
                await loc.select_option(vals, timeout=_ACTION_TIMEOUT)
        elif w == "checkbox":
            await loc.set_checked(bool(value), timeout=_ACTION_TIMEOUT)
        elif w == "radio":
            target = page.locator(f"input[type=radio][name={json.dumps(fld.name)}][value={json.dumps(str(value))}]")
            if await target.count() == 1:
                loc = target
            await loc.check(timeout=_ACTION_TIMEOUT)
        elif w == "file":
            await loc.set_input_files(str(Path(value)), timeout=_ACTION_TIMEOUT)
        elif w == "combobox":
            await _fill_combobox(page, loc, str(value))
        return True
    except Exception as e:  # one unfillable control must not abort the whole form
        log.warning("fill %s (%s) failed: %s", fld.name, w, str(e).splitlines()[0])
        return False


async def _fill_combobox(page: Page, loc, value: str) -> None:
    typeable = await loc.evaluate(
        "e => ['INPUT', 'TEXTAREA'].includes(e.tagName) || e.isContentEditable")
    if typeable:
        await loc.fill("", timeout=_ACTION_TIMEOUT)
        await loc.press_sequentially(value, timeout=_ACTION_TIMEOUT)
    else:
        await loc.click(timeout=_ACTION_TIMEOUT)
    option = page.locator("[role=option]:visible").first
    try:
        await option.wait_for(state="visible", timeout=1500)
    except Exception:
        return  # free-text combobox: the typed value stands
    await option.click(timeout=_ACTION_TIMEOUT)


# --- validation / error discovery -----------------------------------------------------------

_ERR_JS = """(els) => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const STRICT = '.error, .errors, .error-message, .field-error, .invalid-feedback, .help-block.error,'
    + ' .has-error .help-block, .text-danger, .alert-danger, .errorlist li, .form-error';
  const SEL = STRICT + ', [role=alert]';
  const ERRLOOK = /error|invalid|required|must|failed/i;
  const visible = (e) => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const fields = els.map(e => ({invalid: !!e && e.getAttribute('aria-invalid') === 'true',
    css: !!e && e.matches(':invalid'), msg: e ? norm(e.validationMessage) : '', msgs: []}));
  const pageTexts = [], all = [], weak = [];
  const nodes = [...document.querySelectorAll(SEL)].filter(n => visible(n) && norm(n.innerText));
  const leaf = nodes.filter(n => !nodes.some(m => m !== n && n.contains(m)));
  for (const n of leaf) {
    const t = norm(n.innerText);
    all.push(t);
    const isWeak = !n.matches(STRICT) && !ERRLOOK.test(t);
    if (isWeak) weak.push(t);
    let owner = -1;
    for (let a = n, d = 0; a && d < 4; a = a.parentElement, d++) {
      const inside = els.map((e, i) => (e && a.contains(e)) ? i : -1).filter(i => i >= 0);
      if (inside.length === 1) { owner = inside[0]; break; }
      if (inside.length > 1) break;
    }
    if (owner >= 0) { fields[owner].msgs.push(t); if (!isWeak) fields[owner].invalid = true; } else pageTexts.push(t);
  }
  const dm = els.map((e, i) => e && e.getAttribute('aria-describedby') ? i : -1).filter(i => i >= 0);
  for (const i of dm) {
    if (!fields[i].invalid) continue;
    for (const id of els[i].getAttribute('aria-describedby').split(/\\s+/)) {
      const n = document.getElementById(id);
      if (n && norm(n.innerText) && !fields[i].msgs.includes(norm(n.innerText))) fields[i].msgs.push(norm(n.innerText));
    }
  }
  return {fields, pageTexts, all, weak};
}"""


async def _inspect(page: Page, form: FormInfo) -> dict:
    handles: list = []
    try:
        for f in form.fields:
            loc = await resolve(page, f.locators)
            handles.append(await loc.element_handle(timeout=1000) if loc is not None else None)
        return await page.evaluate(_ERR_JS, handles)
    except Exception:  # page navigated away / detached mid-inspection
        return {"fields": [{"invalid": False, "css": False, "msg": "", "msgs": []} for _ in form.fields],
                "pageTexts": [], "all": [], "weak": []}
    finally:
        for h in handles:
            if h is not None:
                try:
                    await h.dispose()
                except Exception:
                    pass


def _norm_words(s: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s or "")
    return " " + re.sub(r"[^a-z0-9]+", " ", s.lower()).strip() + " "


def _assign_page_message(text: str, fields: list[FieldInfo]) -> str:
    msg = _norm_words(text)
    for f in fields:
        for ident in (f.name, f.label):
            n = _norm_words(ident)
            if n.strip() and n in msg:
                return f.name
    return ""


def _json_errors(data) -> dict[str, list[str]]:
    """Field errors from a JSON error body: {errors: {field: msg|[msgs]}}, {error: msg}, ..."""
    out: dict[str, list[str]] = {}
    if not isinstance(data, dict):
        return out

    def add(k: str, v) -> None:
        for m in (v if isinstance(v, list) else [v]):
            if isinstance(m, (str, int, float)) and str(m):
                out.setdefault(k, []).append(str(m))

    for key in ("errors", "error", "message", "detail", "details"):
        v = data.get(key)
        if isinstance(v, dict):
            for fk, fv in v.items():
                add(str(fk), fv)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    add(str(item.get("field") or item.get("param") or ""), item.get("message") or item.get("msg") or "")
                else:
                    add("", item)
        elif v is not None:
            add("", v)
    return out


# --- submit -----------------------------------------------------------------------------------

_TRIGGER_TYPES = {"document", "xhr", "fetch"}


def _merge(into: dict[str, list[str]], key: str, msgs: list[str]) -> None:
    for m in msgs:
        if m and m not in into.get(key, []):
            into.setdefault(key, []).append(m)


async def _submit_once(page: Page, form: FormInfo, settle_ms: int) -> tuple[list[Response], list[Response]]:
    """Click submit; return (all triggered responses, submission responses)."""
    triggered: list[Response] = []
    pending: set[Request] = set()

    def on_request(r: Request) -> None:
        if r.resource_type in _TRIGGER_TYPES:
            pending.add(r)

    def on_done(r: Request) -> None:
        pending.discard(r)

    def on_response(r: Response) -> None:
        if r.request.resource_type in _TRIGGER_TYPES:
            triggered.append(r)

    async def on_dialog(d) -> None:
        await d.accept()

    page.on("request", on_request)
    page.on("requestfinished", on_done)
    page.on("requestfailed", on_done)
    page.on("response", on_response)
    if form.has_confirm:
        page.on("dialog", on_dialog)
    try:
        submit = await resolve(page, form.submit_locators) if form.submit_locators else None
        if submit is not None:
            await submit.click(timeout=_ACTION_TIMEOUT * 2)
        else:
            await _submit_without_button(page, form)
        await asyncio.sleep(0.05)
        for _ in range(200):  # <= ~10s for triggered requests to finish
            if not pending:
                break
            await asyncio.sleep(0.05)
        await wait_settled(page, settle_ms)
    finally:
        page.remove_listener("request", on_request)
        page.remove_listener("requestfinished", on_done)
        page.remove_listener("requestfailed", on_done)
        page.remove_listener("response", on_response)
        if form.has_confirm:
            page.remove_listener("dialog", on_dialog)
    non_get = [r for r in triggered if r.request.method != "GET"]
    return triggered, non_get or triggered


async def _submit_without_button(page: Page, form: FormInfo) -> None:
    for f in reversed(form.fields):
        if f.widget in ("input", "textarea"):
            loc = await resolve(page, f.locators)
            if loc is not None:
                await loc.press("Enter", timeout=_ACTION_TIMEOUT)
                return
    raise RuntimeError("form has no submit control")


async def fill_and_submit(page: Page, form: FormInfo, gen: "DataGen", *, max_retries: int,
                          settle_ms: int,
                          exchange_ids_since: Callable[[], list[int]] | None = None) -> SubmitResult:
    """Fill ``form`` and submit; on validation failure retry (up to ``max_retries`` times) with hints."""
    messages: dict[str, list[str]] = {}
    hints: dict[str, str] = {}
    baseline = set((await _inspect(page, form))["all"])
    success, attempts = False, 0

    def finish() -> SubmitResult:
        ids = exchange_ids_since() if exchange_ids_since else []
        return SubmitResult(success, attempts, messages, ids, page.url)

    for attempt in range(1, max_retries + 2):
        attempts = attempt
        filled = 0
        for f in form.fields:
            filled += await _fill_field(page, f, gen.value_for(f, attempt, hints.get(f.name)))
        if form.fields and not filled:
            _merge(messages, "", ["no form fields could be located"])
            break
        try:
            triggered, submissions = await _submit_once(page, form, settle_ms)
        except Exception as e:
            _merge(messages, "", [f"submit failed: {str(e).splitlines()[0]}"])
            break
        seen: dict[str, list[str]] = {}
        insp = await _inspect(page, form)
        invalid = False
        bad_any = any(r.status >= 400 for r in submissions) or any(
            r.status >= 400 for r in triggered if r.request.resource_type == "document")
        for f, st in zip(form.fields, insp["fields"]):
            # :invalid counts only if the browser blocked the submit; a form reset after a
            # successful create leaves required inputs :invalid.
            flagged = st["invalid"] or (st["css"] and not submissions)
            invalid |= flagged
            msgs = [m for m in st["msgs"] if m not in set(insp["weak"])] if not (bad_any) else st["msgs"]
            _merge(seen, f.name, msgs or ([st["msg"]] if flagged and st["msg"] else []))
        bad = [r for r in submissions if r.status >= 400] + [
            r for r in triggered if r.request.resource_type == "document" and r.status >= 400]
        weak = set(insp["weak"])
        new_errors = [t for t in insp["all"] if t not in baseline
                      and (t not in weak or bad or invalid)]
        for t in insp["pageTexts"]:
            if t in new_errors:
                _merge(seen, _assign_page_message(t, form.fields), [t])
        for r in bad:
            try:
                for k, v in _json_errors(await r.json()).items():
                    key = k if any(k == f.name for f in form.fields) else _assign_page_message(" ".join(v), form.fields)
                    _merge(seen, key, v)
            except Exception:
                pass
            if not seen:
                _merge(seen, "", [f"HTTP {r.status} {r.request.method} {r.url}"])
        for k, v in seen.items():
            _merge(messages, k, v)
        success = bool(submissions) and not bad and not invalid and not new_errors
        if not submissions and not seen:
            _merge(messages, "", ["submit triggered no request"])
        if success or attempt > max_retries:
            break
        new_hints = _derive_hints(seen, form.fields)
        if not new_hints or all(hints.get(k) == v for k, v in new_hints.items()):
            break
        hints.update(new_hints)
    return finish()


def _derive_hints(seen: dict[str, list[str]], fields: list[FieldInfo]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, msgs in seen.items():
        hint = next((h for h in (hint_from_validation(m) for m in msgs) if h), None)
        if not hint:
            continue
        targets = [key] if key else [f.name for f in fields if f.widget in _TEXT_WIDGETS]
        for t in targets:
            out.setdefault(t, hint)
    return out
