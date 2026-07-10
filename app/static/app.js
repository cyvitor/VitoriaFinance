document.querySelectorAll('select[name="person_id"]').forEach((select) => {
  select.required = true;
  const empty = select.querySelector('option[value=""]');
  if (empty) empty.textContent = 'Selecione uma área';
  const label = select.closest('div')?.querySelector('label');
  if (label) label.textContent = 'Área financeira';
  const defaultArea = document.body.dataset.defaultArea;
  if (defaultArea && select.querySelector(`option[value="${defaultArea}"]`)) select.value = defaultArea;
});
