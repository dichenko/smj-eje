(() => {
  const teacher = document.querySelector('input[name="teacher"]');
  const city = document.querySelector('input[name="city"]');
  const coordinator = document.querySelector('select[name="coordinator_id"]');
  const moduleSelect = document.querySelector('select[name="module"]');
  const moduleHint = document.querySelector('#video-module-hint');
  const options = Array.from(document.querySelectorAll('#video-teachers option'));
  let previousTeacher = teacher.value.trim();

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
    moduleHint.textContent = !name ? 'Выберите преподавателя, чтобы увидеть его модули.' :
      modules.length ? 'Только модули выбранного преподавателя.' :
        'Модули появятся после загрузки занятий этого преподавателя.';
    previousTeacher = name;
  };

  teacher.addEventListener('input', updateCity);
  teacher.addEventListener('change', updateCity);
  updateCity();
})();
