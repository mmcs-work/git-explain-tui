document.querySelectorAll('[data-copy-target]').forEach((button) => {
  button.addEventListener('click', async () => {
    const command = document.getElementById(button.dataset.copyTarget)?.innerText;
    const message = button.nextElementSibling;
    if (!command || !message) return;

    try {
      await navigator.clipboard.writeText(command);
      message.textContent = 'Copied — paste it into your terminal.';
    } catch {
      message.textContent = 'Copy failed. Select the command above instead.';
    }
  });
});
