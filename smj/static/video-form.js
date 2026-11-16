(() => {
  const teacher = document.querySelector('input[name="teacher"]');
  const city = document.querySelector('input[name="city"]');
  const coordinator = document.querySelector('select[name="coordinator_id"]');
  const moduleSelect = document.querySelector('select[name="module"]');
  const moduleHint = document.querySelector('#video-module-hint');
  const options = Array.from(document.querySelectorAll('#video-teachers option'));
  const suggestions = document.querySelector('#video-teacher-suggestions');
  const teacherHint = document.querySelector('#video-teacher-hint');
  let previousTeacher = teacher.value.trim();
  let previousSelection;

  const updateCity = () => {
    const name = teacher.value.trim();
    const option = options.find(item => item.value === name);
    const sameName = options.filter(item => item.dataset.teacher === name);
    const selected = option || (sameName.length === 1 ? sameName[0] : null);
    const known = Boolean(selected || sameName.length);
    const nextCity = selected ? selected.dataset.city : '';
    if (known || name !== previousTeacher) {
      if (city.value !== nextCity) {
        city.value = nextCity;
        coordinator.value = '';
      }
    }
    city.readOnly = known;
    const modules = selected ? JSON.parse(selected.dataset.modules) : [];
    moduleHint.textContent = !name ? 'Выберите преподавателя, чтобы увидеть его модули.' :
      modules.length ? 'Только модули выбранного преподавателя.' :
        'Модули появятся после загрузки занятий этого преподавателя.';
    if (selected === previousSelection) {
      previousTeacher = name;
      return;
    }
    previousSelection = selected;
    const previousModule = moduleSelect.value;
    const moduleOptions = (modules.length ? modules : ['']).map(module => {
      const item = document.createElement('option');
      item.value = module;
      item.textContent = module || 'Нет доступных модулей';
      return item;
    });
    moduleSelect.replaceChildren(...moduleOptions);
    moduleSelect.disabled = !modules.length;
    moduleSelect.value = modules.includes(previousModule) ? previousModule : (modules[0] || '');
    previousTeacher = name;
  };

  // A native datalist decides when to open its popup. Render our own suggestions
  // on input so the first keystroke works without another click or focus change.
  teacher.removeAttribute('list');
  teacher.setAttribute('role', 'combobox');
  teacher.setAttribute('aria-autocomplete', 'list');
  teacher.setAttribute('aria-controls', suggestions.id);
  teacher.setAttribute('aria-expanded', 'false');
  const normalize = value => value.normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .toLocaleLowerCase('ru').trim();
  let filtered = [];
  let activeIndex = -1;
  const closeSuggestions = () => {
    suggestions.hidden = true;
    teacher.setAttribute('aria-expanded', 'false');
    teacher.removeAttribute('aria-activedescendant');
    activeIndex = -1;
  };
  const choose = option => {
    teacher.value = option.value;
    updateCity();
    closeSuggestions();
    teacherHint.textContent = 'Начните вводить имя или фамилию.';
  };
  const showSuggestions = () => {
    const terms = normalize(teacher.value).split(/\s+/).filter(Boolean);
    filtered = terms.length ? options.filter(option => {
      const words = normalize(option.value).split(/\s+/);
      return terms.every(term => words.some(word => word.startsWith(term)));
    }) : [];
    activeIndex = -1;
    teacher.removeAttribute('aria-activedescendant');
    suggestions.replaceChildren(...filtered.map((option, index) => {
      const item = document.createElement('div');
      item.id = `video-teacher-option-${index}`;
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', 'false');
      item.textContent = option.value;
      // Keep focus on the input until the click selects an option.
      item.addEventListener('mousedown', event => event.preventDefault());
      item.addEventListener('click', () => choose(option));
      return item;
    }));
    suggestions.hidden = !filtered.length;
    teacher.setAttribute('aria-expanded', String(!suggestions.hidden));
    teacherHint.textContent = terms.length && !filtered.length
      ? 'Совпадений не найдено.' : 'Начните вводить имя или фамилию.';
  };
  teacher.addEventListener('input', () => { updateCity(); showSuggestions(); });
  teacher.addEventListener('focus', showSuggestions);
  teacher.addEventListener('click', showSuggestions);
  teacher.addEventListener('blur', closeSuggestions);
  teacher.addEventListener('change', () => { updateCity(); closeSuggestions(); });
  teacher.addEventListener('keydown', event => {
    if (event.isComposing) return;
    if (event.key === 'Escape' || event.key === 'Tab') {
      closeSuggestions();
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (suggestions.hidden) showSuggestions();
      if (!filtered.length) return;
      event.preventDefault();
      activeIndex = event.key === 'ArrowDown' ? (activeIndex + 1) % filtered.length
        : (activeIndex <= 0 ? filtered.length : activeIndex) - 1;
      Array.from(suggestions.children).forEach((item, index) => {
        item.setAttribute('aria-selected', String(index === activeIndex));
      });
      const active = suggestions.children[activeIndex];
      teacher.setAttribute('aria-activedescendant', active.id);
      active.scrollIntoView({block: 'nearest'});
    } else if (event.key === 'Enter' && !suggestions.hidden && activeIndex >= 0) {
      event.preventDefault();
      choose(filtered[activeIndex]);
    }
  });
  updateCity();
})();
