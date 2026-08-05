const input = document.querySelector('#doc-search');
const status = document.querySelector('#search-status');
const sections = [...document.querySelectorAll('[data-searchable]')];

function filterGuide() {
  const query = input.value.trim().toLowerCase();
  let matches = 0;
  sections.forEach((section) => {
    const visible = !query || section.textContent.toLowerCase().includes(query);
    section.hidden = !visible;
    matches += Number(visible);
  });
  status.textContent = query ? `${matches} matching section${matches === 1 ? '' : 's'}.` : '';
}

input.addEventListener('input', filterGuide);
document.addEventListener('keydown', (event) => {
  if (event.key === '/' && document.activeElement !== input) {
    event.preventDefault();
    input.focus();
  }
});

document.querySelectorAll('[data-copy]').forEach((button) => {
  button.addEventListener('click', async () => {
    const code = document.querySelector(`#${button.dataset.copy}`)?.innerText;
    if (!code) return;
    await navigator.clipboard.writeText(code);
    button.textContent = 'Copied';
    setTimeout(() => { button.textContent = 'Copy'; }, 1400);
  });
});
