(() => {
  const teacher = document.querySelector('input[name="teacher"]');
  const city = document.querySelector('input[name="city"]');
  const coordinator = document.querySelector('select[name="coordinator_id"]');
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
    previousTeacher = name;
  };

  teacher.addEventListener('input', updateCity);
  teacher.addEventListener('change', updateCity);
  updateCity();
})();
