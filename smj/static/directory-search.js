(() => {
  const normalize = value => value.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase('ru').trim();
  const matches = (value, query) => {
    const words = normalize(value).split(/\s+/);
    return normalize(query).split(/\s+/).filter(Boolean)
      .every(term => words.some(word => word.startsWith(term)));
  };

  const coordinatorInput = document.querySelector('#coordinator-search');
  if (coordinatorInput) {
    const table = document.querySelector('#coordinators-table');
    const rows = [...table.querySelectorAll('[data-coordinator-row]')];
    const noResults = table.querySelector('[data-coordinator-no-results]');
    const status = document.querySelector('#coordinator-search-status');
    const update = () => {
      let count = 0;
      for (const row of rows) {
        row.hidden = !matches(`${row.dataset.city} ${row.dataset.name}`, coordinatorInput.value);
        if (!row.hidden) count += 1;
      }
      noResults.hidden = count !== 0 || rows.length === 0;
      status.textContent = `Найдено записей: ${count}`;
    };
    coordinatorInput.addEventListener('input', update);
    update();
  }

  const cityList = document.querySelector('#city-search-options');
  const teacherList = document.querySelector('#teacher-search-options');
  if (!cityList) return;
  const cityInput = document.querySelector('input[name="city"]');
  const teacherInput = teacherList ? document.querySelector('input[name="teacher"]') : null;
  const cities = [...cityList.options].map(option => option.value);
  const teachers = teacherList ? [...teacherList.options].map(option => ({name: option.value, city: option.dataset.city})) : [];
  const exact = (values, value) => values.find(item => normalize(item) === normalize(value));
  const teacherNames = () => {
    const city = exact(cities, cityInput.value);
    return [...new Set(teachers.filter(teacher => !city || teacher.city === city).map(teacher => teacher.name))];
  };
  const updateOptions = (input, list, values, hint, prompt) => {
    const filtered = values.filter(value => matches(value, input.value));
    list.replaceChildren(...filtered.map(value => {
      const option = document.createElement('option');
      option.value = value;
      return option;
    }));
    hint.textContent = filtered.length ? prompt : 'Совпадений не найдено.';
    input.setCustomValidity(input.value.trim() && !exact(values, input.value)
      ? 'Выберите значение из подсказок или очистите поле.' : '');
  };
  const updateCities = () => updateOptions(cityInput, cityList, cities,
    document.querySelector('#city-search-hint'), 'Начните вводить название города.');
  const updateTeachers = () => {
    if (teacherInput) updateOptions(teacherInput, teacherList, teacherNames(),
      document.querySelector('#teacher-search-hint'), 'Начните вводить имя или фамилию.');
  };
  const canonicalize = (input, values) => {
    const value = exact(values, input.value);
    if (value) input.value = value;
  };
  cityInput.addEventListener('input', () => { updateCities(); updateTeachers(); });
  cityInput.addEventListener('change', () => {
    canonicalize(cityInput, cities);
    if (teacherInput && exact(cities, cityInput.value) && !exact(teacherNames(), teacherInput.value)) teacherInput.value = '';
    updateCities();
    updateTeachers();
  });
  if (teacherInput) {
    teacherInput.addEventListener('input', updateTeachers);
    teacherInput.addEventListener('change', () => {
      canonicalize(teacherInput, teacherNames());
      updateTeachers();
    });
  }
  cityInput.form.addEventListener('submit', event => {
    canonicalize(cityInput, cities);
    if (teacherInput) canonicalize(teacherInput, teacherNames());
    updateCities();
    updateTeachers();
    if (!cityInput.form.reportValidity()) event.preventDefault();
  });
  updateCities();
  updateTeachers();
})();
