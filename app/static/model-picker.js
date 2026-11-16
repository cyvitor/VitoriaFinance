// Seletor de modelos da DeepInfra: carrega o catálogo ao clicar, filtra enquanto digita
// e só deixa escolher um modelo que existe. Não há texto livre: se a consulta falhar, o campo
// fica bloqueado com a mensagem de erro.
(function () {
  const pickers = document.querySelectorAll('[data-model-picker]');
  if (!pickers.length) return;
  let catalog = null, pending = null, failure = '';
  const listeners = new Set();

  function loadCatalog(force) {
    if (catalog && !force) return Promise.resolve(catalog);
    if (pending) return pending;
    failure = '';
    pending = fetch('/system/deepinfra/models', {credentials: 'same-origin', headers: {Accept: 'application/json'}})
      .then(async (response) => {
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !Array.isArray(data.models)) throw new Error(data.error || 'Não foi possível carregar os modelos da DeepInfra.');
        catalog = data.models;
        return catalog;
      })
      .catch((error) => { failure = error.message || 'Não foi possível carregar os modelos da DeepInfra.'; throw error; })
      .finally(() => { pending = null; listeners.forEach((fn) => fn()); });
    listeners.forEach((fn) => fn());
    return pending;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function parseList(raw) {
    const result = [];
    (raw || '').split(/[\n,]/).map((item) => item.trim()).filter(Boolean).forEach((item) => { if (!result.includes(item)) result.push(item); });
    return result;
  }

  pickers.forEach((root, index) => {
    const multiple = root.hasAttribute('data-multiple');
    const initial = multiple ? parseList(root.dataset.initial) : [(root.dataset.initial || '').trim()].filter(Boolean);
    let selected = initial.slice();
    let activeIndex = -1;
    let typed = false; // só filtra depois que o usuário digita; ao abrir, mostra o catálogo inteiro
    let visible = [];

    const hidden = el('input'); hidden.type = 'hidden'; hidden.name = root.dataset.name;
    const chips = el('div', 'model-chips');
    const input = el('input', 'form-control model-input');
    input.type = 'text'; input.autocomplete = 'off'; input.spellcheck = false;
    input.setAttribute('role', 'combobox'); input.setAttribute('aria-autocomplete', 'list'); input.setAttribute('aria-expanded', 'false');
    const listId = 'modelList' + index + '_' + root.dataset.name;
    input.setAttribute('aria-controls', listId);
    const list = el('ul', 'model-list'); list.id = listId; list.setAttribute('role', 'listbox'); list.hidden = true;
    const status = el('div', 'model-status'); status.setAttribute('aria-live', 'polite');
    const retry = el('button', 'btn btn-sm btn-light model-retry', 'Tentar novamente'); retry.type = 'button'; retry.hidden = true;
    root.append(hidden, chips, input, list, status, retry);

    function syncHidden() { hidden.value = selected.join(', '); }
    function placeholder() {
      if (multiple) return selected.length ? 'Adicionar outro modelo…' : (root.dataset.placeholder || 'Adicionar modelo…');
      return root.dataset.placeholder || 'Escolha um modelo';
    }
    function renderChips() {
      if (!multiple) return;
      chips.replaceChildren();
      selected.forEach((model, position) => {
        const chip = el('span', 'model-chip');
        const missing = catalog && !catalog.includes(model);
        if (missing) { chip.classList.add('missing'); chip.title = 'Este modelo não existe mais no catálogo da DeepInfra. Troque por outro.'; }
        chip.append(el('span', 'model-chip-order', String(position + 1)), el('span', 'model-chip-name', model));
        const actions = [['‹', 'Mover para antes', -1], ['›', 'Mover para depois', 1], ['×', 'Remover', 0]];
        actions.forEach(([label, title, delta]) => {
          if (delta === -1 && position === 0) return;
          if (delta === 1 && position === selected.length - 1) return;
          const button = el('button', 'model-chip-btn', label); button.type = 'button'; button.title = title; button.setAttribute('aria-label', title + ' ' + model);
          button.addEventListener('click', () => {
            if (delta === 0) selected.splice(position, 1);
            else { const [item] = selected.splice(position, 1); selected.splice(position + delta, 0, item); }
            syncHidden(); renderChips(); input.placeholder = placeholder();
          });
          chip.append(button);
        });
        chips.append(chip);
      });
    }
    function showSelectedInInput() { if (!multiple) input.value = selected[0] || ''; else input.value = ''; }
    function commitTyped() {
      // Texto digitado só vale se for exatamente um modelo do catálogo; qualquer outro é descartado.
      const typed = input.value.trim();
      if (!typed || !catalog || !catalog.includes(typed)) return;
      if (multiple) { if (!selected.includes(typed)) selected.push(typed); }
      else selected = [typed];
      syncHidden(); renderChips(); input.placeholder = placeholder();
    }
    function closeList() { list.hidden = true; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); activeIndex = -1; }
    function setActive(position) {
      activeIndex = position;
      Array.from(list.children).forEach((item, i) => { item.classList.toggle('active', i === position); if (i === position) { input.setAttribute('aria-activedescendant', item.id); item.scrollIntoView({block: 'nearest'}); } });
    }
    function choose(model) {
      typed = false;
      if (multiple) { if (!selected.includes(model)) selected.push(model); input.value = ''; }
      else { selected = [model]; input.value = model; }
      syncHidden(); renderChips(); input.placeholder = placeholder(); closeList(); filter();
      if (!multiple) input.blur();
    }
    function filter() {
      list.replaceChildren(); visible = [];
      if (failure) { status.textContent = failure; status.className = 'model-status error'; retry.hidden = false; closeList(); return; }
      retry.hidden = true;
      if (!catalog) { status.textContent = pending ? 'Carregando modelos da DeepInfra…' : ''; status.className = 'model-status'; return; }
      const term = typed ? input.value.trim().toLowerCase() : '';
      const matches = catalog.filter((model) => !(multiple && selected.includes(model)) && (!term || model.toLowerCase().includes(term)));
      visible = matches.slice(0, 200);
      visible.forEach((model, i) => {
        const item = el('li', 'model-option'); item.id = listId + '_' + i; item.setAttribute('role', 'option');
        const at = term ? model.toLowerCase().indexOf(term) : -1;
        if (at >= 0) { item.append(document.createTextNode(model.slice(0, at)), el('mark', '', model.slice(at, at + term.length)), document.createTextNode(model.slice(at + term.length))); }
        else item.textContent = model;
        item.addEventListener('mousedown', (event) => { event.preventDefault(); choose(model); });
        list.append(item);
      });
      if (!visible.length) { const empty = el('li', 'model-empty', 'Nenhum modelo encontrado na DeepInfra.'); list.append(empty); }
      status.className = 'model-status';
      status.textContent = matches.length > visible.length ? `Mostrando ${visible.length} de ${matches.length} modelos; continue digitando para filtrar.` : `${catalog.length} modelos na DeepInfra.`;
      list.hidden = false; input.setAttribute('aria-expanded', 'true'); setActive(visible.length && term ? 0 : -1);
    }
    function open() {
      input.disabled = false;
      const promise = loadCatalog(false);
      filter();
      promise.then(() => { renderChips(); if (document.activeElement === input) filter(); }).catch(() => {});
    }
    function syncState() {
      input.disabled = !!failure && !catalog;
      if (failure && !catalog) { status.textContent = failure; status.className = 'model-status error'; retry.hidden = false; }
      else if (pending && !catalog) { status.textContent = 'Carregando modelos da DeepInfra…'; status.className = 'model-status'; retry.hidden = true; }
      else if (list.hidden) { status.textContent = ''; status.className = 'model-status'; retry.hidden = true; }
      renderChips();
    }
    listeners.add(syncState);

    input.addEventListener('focus', () => { typed = false; input.select(); open(); });
    input.addEventListener('click', open);
    input.addEventListener('input', () => { typed = true; if (!list.hidden || catalog) filter(); else open(); });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown') { event.preventDefault(); if (list.hidden) open(); else setActive(Math.min(activeIndex + 1, visible.length - 1)); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); setActive(Math.max(activeIndex - 1, 0)); }
      else if (event.key === 'Enter') { if (!list.hidden && activeIndex >= 0 && visible[activeIndex]) { event.preventDefault(); choose(visible[activeIndex]); } else if (!list.hidden) event.preventDefault(); }
      else if (event.key === 'Escape') { closeList(); showSelectedInInput(); }
      else if (event.key === 'Backspace' && multiple && !input.value && selected.length) { selected.pop(); syncHidden(); renderChips(); input.placeholder = placeholder(); }
    });
    input.addEventListener('blur', () => { commitTyped(); typed = false; closeList(); showSelectedInInput(); status.textContent = failure && !catalog ? failure : ''; if (!failure) status.className = 'model-status'; });
    retry.addEventListener('click', () => { loadCatalog(true).then(() => { renderChips(); filter(); }).catch(() => {}); input.disabled = false; input.focus(); });

    syncHidden(); renderChips(); showSelectedInInput(); input.placeholder = placeholder();
    const form = root.closest('form');
    if (form) form.addEventListener('submit', () => {
      // O texto digitado nunca vira modelo por si só: só segue no envio o que foi escolhido na lista.
      commitTyped(); showSelectedInInput();
    });
  });
})();