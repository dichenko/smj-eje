(() => {
  const input = document.querySelector("#tutor-search");
  const table = document.querySelector("#tutors-table");
  if (!input || !table) return;

  const rows = [...table.querySelectorAll("[data-tutor-row]")];
  const count = document.querySelector("#tutor-count");
  const noResults = table.querySelector("[data-no-search-results]");
  const normalize = (value) => value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase("ru");

  input.addEventListener("input", () => {
    const terms = normalize(input.value.trim()).split(/\s+/).filter(Boolean);
    let visibleCount = 0;

    for (const row of rows) {
      const words = normalize(`${row.dataset.city} ${row.dataset.name}`).split(/\s+/);
      const matches = terms.every((term) => words.some((word) => word.startsWith(term)));
      row.hidden = !matches;
      if (matches) visibleCount += 1;
    }

    if (count) count.textContent = visibleCount;
    if (noResults) noResults.hidden = visibleCount !== 0 || rows.length === 0;
  });
})();
