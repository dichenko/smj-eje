"""Run the real browser search script against a small DOM simulation."""
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="Node.js is unavailable")
SCRIPT = Path(__file__).resolve().parents[1] / "smj" / "static" / "directory-search.js"
HARNESS = """
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const element = (value = '') => ({value, dataset: {}, hidden: false, textContent: '',
  events: {}, validity: '', options: [],
  addEventListener(event, callback) {this.events[event] = callback;},
  setCustomValidity(message) {this.validity = message;},
  replaceChildren(...options) {this.options = options;},
  fire(event) {this.events[event]();}
});
const run = elements => vm.runInNewContext(source, {document: {
  querySelector: selector => elements[selector] || null,
  createElement: () => element(),
}});
"""


def run_script(test):
    subprocess.run([shutil.which("node"), "-e", HARNESS + test, str(SCRIPT)],
                   check=True, capture_output=True, text=True)


def test_city_suggestions_match_prefixes_and_teachers_follow_city():
    run_script("""
const city = element('Москва'), teacher = element('');
const cityList = element(), teacherList = element();
cityList.options = ['Москва', 'Минск'].map(value => ({value}));
teacherList.options = [
  ['Учитель 1', 'Москва'], ['Анна Иванова', 'Москва'],
  ['Анна Иванова', 'Минск'], ['Кирилл Шишканов', 'Минск'],
].map(([value, city]) => ({value, dataset: {city}}));
const form = element();
form.reportValidity = () => !city.validity && !teacher.validity;
city.form = form;
run({'#city-search-options': cityList, '#teacher-search-options': teacherList,
  'input[name="city"]': city, 'input[name="teacher"]': teacher,
  '#city-search-hint': element(), '#teacher-search-hint': element()});
const values = list => list.options.map(option => option.value);
assert.deepEqual(values(teacherList), ['Учитель 1', 'Анна Иванова']);
teacher.value = 'ив'; teacher.fire('input');
assert.deepEqual(values(teacherList), ['Анна Иванова']);
teacher.value = 'ван'; teacher.fire('input');
assert.deepEqual(values(teacherList), []);
assert.notEqual(teacher.validity, '');
teacher.value = 'Учитель 1'; teacher.fire('change');
city.value = 'ми'; city.fire('input');
assert.deepEqual(values(cityList), ['Минск']);
assert.notEqual(city.validity, '');
city.value = 'МИНСК'; city.fire('change');
assert.equal(city.value, 'Минск'); assert.equal(teacher.value, '');
assert.deepEqual(values(teacherList), ['Анна Иванова', 'Кирилл Шишканов']);
teacher.value = 'шиш'; teacher.fire('input');
assert.deepEqual(values(teacherList), ['Кирилл Шишканов']);
teacher.value = 'кирилл шишканов'; teacher.fire('change');
assert.equal(teacher.value, 'Кирилл Шишканов');
let prevented = false;
form.events.submit({preventDefault() {prevented = true;}});
assert.equal(prevented, false);
teacher.value = ''; teacher.fire('input'); city.value = ''; city.fire('input');
assert.deepEqual(values(cityList), ['Москва', 'Минск']);
assert.equal(values(teacherList).length, 3);
""")


def test_coordinator_search_matches_city_or_name_prefixes_and_clears():
    run_script("""
const input = element(), status = element(), noResults = element();
const rows = [['Минск', 'Кирилл Шишканов'], ['Москва', 'Анна Иванова'],
  ['Минск', 'Анна Петрова'], ['Ёлкино', 'Фёдор Семёнов']]
  .map(([city, name]) => ({dataset: {city, name}, hidden: false}));
const table = {querySelectorAll: () => rows, querySelector: () => noResults};
run({'#coordinator-search': input, '#coordinators-table': table,
  '#coordinator-search-status': status});
input.value = 'мин кир'; input.fire('input');
assert.deepEqual(rows.map(row => row.hidden), [false, true, true, true]);
input.value = 'шиш'; input.fire('input');
assert.equal(rows[0].hidden, false);
input.value = 'ван'; input.fire('input');
assert.equal(rows.every(row => row.hidden), true);
assert.equal(noResults.hidden, false);
input.value = 'ел фе'; input.fire('input');
assert.equal(rows[3].hidden, false);
assert.equal(status.textContent, 'Найдено записей: 1');
input.value = ''; input.fire('input');
assert.equal(rows.every(row => !row.hidden), true);
assert.equal(noResults.hidden, true);
""")
