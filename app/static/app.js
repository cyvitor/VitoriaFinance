const storedArea = localStorage.getItem('vitoriafinance.activePersonId');
document.querySelectorAll('select[name="person_id"]:not([data-context-area])').forEach((select) => {
  select.required = true;
  const empty = select.querySelector('option[value=""]');
  if (empty) empty.textContent = 'Selecione uma área';
  const label = select.closest('div')?.querySelector('label');
  if (label) label.textContent = 'Área financeira';
  const defaultArea = document.body.dataset.defaultArea;
  const preferredArea = select.value || storedArea || defaultArea;
  if (preferredArea && select.querySelector(`option[value="${preferredArea}"]`)) select.value = preferredArea;
});

document.querySelectorAll('[data-area-resource-form]').forEach((form) => {
  const areaSelect = form.querySelector('select[name="person_id"]');
  const resourceSelects = form.querySelectorAll('select[data-area-resource]');
  if (!areaSelect || !resourceSelects.length) return;
  const catalogs = new Map();
  resourceSelects.forEach((select) => {
    catalogs.set(select, Array.from(select.querySelectorAll('option[data-area-id]')).map((option) => option.cloneNode(true)));
  });
  const refreshResources = () => {
    const areaId = areaSelect.value;
    resourceSelects.forEach((select) => {
      const current = select.value;
      select.querySelectorAll('option[data-area-id]').forEach((option) => option.remove());
      let restored = false;
      catalogs.get(select).forEach((option) => {
        if (!areaId || option.dataset.areaId !== areaId) return;
        const copy = option.cloneNode(true);
        if (copy.value === current) {
          copy.selected = true;
          restored = true;
        }
        select.appendChild(copy);
      });
      if (!restored) select.value = '';
    });
  };
  areaSelect.addEventListener('change', refreshResources);
  refreshResources();
});

document.querySelector('select[data-context-area]')?.addEventListener('change', (event) => {
  localStorage.setItem('vitoriafinance.activePersonId', event.target.value);
});

const sidebar = document.querySelector('.sidebar');
const cardsMenu = sidebar?.querySelector('nav a[href="/cards"]');
if (cardsMenu && !sidebar.querySelector('nav a[href="/vehicles"]')) {
  cardsMenu.insertAdjacentHTML('afterend', '<a href="/vehicles"><i class="bi bi-car-front"></i> Veículos</a>');
}
const activeMenuItem = sidebar?.querySelector('nav a.active');
if (activeMenuItem) activeMenuItem.scrollIntoView({block: 'nearest'});

sidebar?.querySelectorAll('nav a').forEach((link) => {
  link.addEventListener('click', () => {
    if (window.matchMedia('(max-width: 760px)').matches) sidebar.classList.remove('open');
  });
});

// A mesma tela atende financiamentos, empréstimos e negociações parceladas.
const financingForm = document.querySelector('#newFinancing form .modal-body');
if (financingForm) {
  financingForm.insertAdjacentHTML('afterbegin', '<div class="col-md-4"><label class="form-label">Tipo de contrato</label><select class="form-select" name="contract_type"><option value="financing">Financiamento</option><option value="negotiated_debt">Dívida negociada</option><option value="personal_loan">Empréstimo pessoal</option></select></div>');
}
