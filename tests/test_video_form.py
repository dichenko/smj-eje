"""Exercise the video form's real JavaScript, including the first input event."""
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="Node.js is unavailable")
SCRIPT = Path(__file__).resolve().parents[1] / "smj" / "static" / "video-form.js"
HARNESS = """
const fs = require('node:fs'), vm = require('node:vm');
const assert = require('node:assert/strict');
const element = (value = '') => ({value, dataset: {}, hidden: true, children: [],
  attributes: {}, events: {}, textContent: '', replacements: 0,
  setAttribute(name, value) {this.attributes[name] = value;},
  removeAttribute(name) {delete this.attributes[name];},
  addEventListener(name, callback) {(this.events[name] ||= []).push(callback);},
  replaceChildren(...children) {this.children = children; this.replacements++;},
  scrollIntoView() {},
  fire(name, data = {}) {
    const event = {prevented: false, preventDefault() {this.prevented = true;}, ...data};
    for (const callback of this.events[name] || []) callback(event);
    return event;
  }
});
const teacher = element(), city = element(), coordinator = element();
const moduleSelect = element(), moduleHint = element(), hint = element();
const suggestions = element(); suggestions.id = 'video-teacher-suggestions';
teacher.attributes.list = 'video-teachers';
const options = [
  ['Анна Иванова', 'Анна Иванова', 'Москва', ['Kids', 'Junior']],
  ['Кирилл Шишканов — Минск', 'Кирилл Шишканов', 'Минск', ['Matata']],
  ['Кирилл Шишканов — Москва', 'Кирилл Шишканов', 'Москва', ['Junior']],
  ['Фёдор Семёнов', 'Фёдор Семёнов', 'Ёлкино', []],
].map(([value, teacher, city, modules]) => ({value, dataset: {
  teacher, city, modules: JSON.stringify(modules)
}}));
const elements = {'input[name="teacher"]': teacher, 'input[name="city"]': city,
  'select[name="coordinator_id"]': coordinator, 'select[name="module"]': moduleSelect,
  '#video-module-hint': moduleHint, '#video-teacher-suggestions': suggestions,
  '#video-teacher-hint': hint};
const run = () => vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {document: {
  querySelector: selector => elements[selector], querySelectorAll: () => options,
  createElement: () => element(),
}});
const type = value => {teacher.value = value; teacher.fire('input');};
const values = () => suggestions.children.map(item => item.textContent);
"""


def run_script(test):
    subprocess.run([shutil.which("node"), "-e", HARNESS + test, str(SCRIPT)],
                   check=True, capture_output=True, text=True, encoding="utf-8")


def test_first_keystroke_opens_suggestions_and_filters_name_prefixes():
    run_script("""
run();
assert.equal(teacher.attributes.list, undefined);
assert.equal(suggestions.hidden, true);
teacher.fire('focus'); type('а');
assert.equal(suggestions.hidden, false);
assert.equal(teacher.attributes['aria-expanded'], 'true');
assert.deepEqual(values(), ['Анна Иванова']);
type('  ИВ АН  '); assert.deepEqual(values(), ['Анна Иванова']);
type('ван'); assert.equal(suggestions.hidden, true);
assert.equal(hint.textContent, 'Совпадений не найдено.');
type('фе се'); assert.deepEqual(values(), ['Фёдор Семёнов']);
type(''); assert.equal(suggestions.hidden, true);
assert.equal(teacher.attributes['aria-expanded'], 'false');
""")


def test_click_selects_teacher_city_and_modules_without_losing_focus():
    run_script("""
run(); type('шиш');
assert.equal(values().length, 2);
const item = suggestions.children[0];
assert.equal(item.fire('mousedown').prevented, true);
item.fire('click');
assert.equal(teacher.value, 'Кирилл Шишканов — Минск');
assert.equal(city.value, 'Минск'); assert.equal(city.readOnly, true);
assert.equal(moduleSelect.value, 'Matata'); assert.equal(moduleSelect.disabled, false);
assert.deepEqual(moduleSelect.children.map(item => item.value), ['Matata']);
assert.equal(suggestions.hidden, true);
coordinator.value = '42'; type('ан');
assert.equal(city.value, ''); assert.equal(coordinator.value, '');
assert.equal(city.readOnly, false); assert.equal(moduleSelect.disabled, true);
suggestions.children[0].fire('click');
assert.equal(city.value, 'Москва');
assert.deepEqual(moduleSelect.children.map(item => item.value), ['Kids', 'Junior']);
moduleSelect.value = 'Junior'; teacher.fire('change');
assert.equal(moduleSelect.value, 'Junior');
""")


def test_keyboard_selection_escape_blur_and_reopening():
    run_script("""
run(); type('ки');
assert.equal(teacher.fire('keydown', {key: 'Enter'}).prevented, false);
assert.equal(teacher.fire('keydown', {key: 'ArrowUp', isComposing: true}).prevented, false);
assert.equal(teacher.fire('keydown', {key: 'ArrowUp'}).prevented, true);
assert.equal(suggestions.children[1].attributes['aria-selected'], 'true');
assert.equal(teacher.attributes['aria-activedescendant'], 'video-teacher-option-1');
assert.equal(teacher.fire('keydown', {key: 'Enter'}).prevented, true);
assert.equal(teacher.value, 'Кирилл Шишканов — Москва');
assert.equal(city.value, 'Москва'); assert.equal(moduleSelect.value, 'Junior');
assert.equal(suggestions.hidden, true);
type('ан'); teacher.fire('keydown', {key: 'Escape'});
assert.equal(suggestions.hidden, true);
teacher.fire('click'); assert.equal(suggestions.hidden, false);
teacher.fire('blur'); assert.equal(suggestions.hidden, true);
teacher.fire('focus'); assert.equal(suggestions.hidden, false);
teacher.fire('keydown', {key: 'Tab'}); assert.equal(suggestions.hidden, true);
teacher.fire('keydown', {key: 'ArrowDown'});
assert.equal(suggestions.hidden, false);
assert.equal(suggestions.children[0].attributes['aria-selected'], 'true');
""")


def test_editing_preserves_selected_module_and_manual_city():
    run_script("""
teacher.value = 'Анна Иванова'; city.value = 'Москва';
moduleSelect.value = 'Junior'; coordinator.value = '42'; run();
assert.equal(moduleSelect.value, 'Junior'); assert.equal(coordinator.value, '42');
const replacements = moduleSelect.replacements;
teacher.fire('change'); assert.equal(moduleSelect.replacements, replacements);
type('новый'); city.value = 'Томск'; teacher.fire('change');
assert.equal(city.value, 'Томск'); assert.equal(city.readOnly, false);
type('фе'); suggestions.children[0].fire('click');
assert.equal(city.value, 'Ёлкино'); assert.equal(moduleSelect.disabled, true);
""")
