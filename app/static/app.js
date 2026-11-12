document.querySelectorAll('form[data-confirm]').forEach(form => {
  form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});
document.querySelectorAll('input[readonly]').forEach(input => {
  input.addEventListener('click', () => input.select());
});

(() => {
  const card = document.getElementById('replay');
  if (!card) return;
  const form = document.getElementById('replay-form');
  const result = document.getElementById('replay-result');
  const storageKey = 'replay:endpoint:' + card.dataset.endpointId;
  const fields = ['url', 'method', 'auth', 'query_name', 'query_value', 'bearer', 'basic_user', 'basic_pass', 'header_name', 'header_value', 'include_headers'];
  const allowedHosts = new Set(['localhost', '127.0.0.1']);

  const field = name => form.elements[name];
  const value = name => field(name).value.trim();

  const methodSelect = field('method');
  if (![...methodSelect.options].some(o => o.value === card.dataset.method)) methodSelect.add(new Option(card.dataset.method));
  methodSelect.value = card.dataset.method;

  const toggleAuth = () => {
    form.querySelectorAll('[data-auth]').forEach(el => { el.hidden = el.dataset.auth !== field('auth').value; });
  };

  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
    if (saved) {
      fields.forEach(name => {
        if (!(name in saved)) return;
        if (field(name).type === 'checkbox') field(name).checked = !!saved[name];
        else field(name).value = saved[name];
      });
      field('remember').checked = true;
    }
  } catch (e) { /* storage unavailable */ }
  field('auth').addEventListener('change', toggleAuth);
  toggleAuth();

  const show = (ok, title, text) => {
    result.hidden = false;
    result.replaceChildren();
    const badge = document.createElement('span');
    badge.className = 'badge ' + (ok ? 'good' : 'bad');
    badge.textContent = title;
    result.append(badge);
    if (text) {
      const pre = document.createElement('pre');
      pre.textContent = text;
      result.append(pre);
    }
  };

  const decodeBase64 = b64 => Uint8Array.from(atob(b64), c => c.charCodeAt(0));

  form.addEventListener('submit', async event => {
    event.preventDefault();
    let target;
    try {
      target = new URL(value('url'));
    } catch (e) {
      return show(false, 'URL inválida', 'Informe uma URL completa, como http://localhost:8080/caminho');
    }
    if (!['http:', 'https:'].includes(target.protocol) || !allowedHosts.has(target.hostname)) {
      return show(false, 'Destino não permitido', 'Use apenas http(s)://localhost ou http(s)://127.0.0.1.');
    }

    try {
      if (field('remember').checked) {
        const data = {};
        fields.forEach(name => { data[name] = field(name).type === 'checkbox' ? field(name).checked : field(name).value; });
        localStorage.setItem(storageKey, JSON.stringify(data));
      } else {
        localStorage.removeItem(storageKey);
      }
    } catch (e) { /* storage unavailable */ }

    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    show(true, 'Enviando…');
    try {
      const sourceResponse = await fetch(card.dataset.replayUrl, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
      if (!sourceResponse.ok || !(sourceResponse.headers.get('content-type') || '').includes('json')) {
        return show(false, 'Falha ao carregar a requisição', 'Recarregue a página; a sessão pode ter expirado.');
      }
      const source = await sourceResponse.json();

      source.query.forEach(([k, v]) => { if (!target.searchParams.has(k)) target.searchParams.append(k, v); });
      const headers = new Headers();
      if (field('include_headers').checked) {
        Object.entries(source.headers).forEach(([k, v]) => { try { headers.set(k, v); } catch (e) { /* forbidden header */ } });
      }
      if (source.content_type) headers.set('Content-Type', source.content_type);

      const auth = field('auth').value;
      if (auth === 'query') {
        if (!value('query_name')) return show(false, 'Informe o nome do parâmetro do token');
        target.searchParams.set(value('query_name'), value('query_value'));
      } else if (auth === 'bearer') {
        headers.set('Authorization', 'Bearer ' + value('bearer'));
      } else if (auth === 'basic') {
        const raw = new TextEncoder().encode(value('basic_user') + ':' + field('basic_pass').value);
        headers.set('Authorization', 'Basic ' + btoa(String.fromCharCode(...raw)));
      } else if (auth === 'header') {
        if (!value('header_name')) return show(false, 'Informe o nome do header');
        headers.set(value('header_name'), value('header_value'));
      }

      const method = field('method').value;
      const options = { method, headers, mode: 'cors', credentials: 'omit', redirect: 'follow', cache: 'no-store' };
      if (!['GET', 'HEAD'].includes(method)) options.body = decodeBase64(source.body_b64);

      const started = performance.now();
      const response = await fetch(target.toString(), options);
      const elapsed = Math.round(performance.now() - started);
      let text = await response.text();
      if (text.length > 10240) text = text.slice(0, 10240) + '\n… (resposta truncada)';
      show(response.ok, `HTTP ${response.status} · ${elapsed} ms`, `${method} ${target.toString()}\n\n${text || '(resposta vazia)'}`);
    } catch (e) {
      show(false, 'Não foi possível enviar', 'O navegador bloqueou ou não alcançou o destino. Verifique se a aplicação local está rodando, se responde ao preflight CORS (OPTIONS) liberando esta origem e os headers enviados, e se o acesso à rede local foi permitido no navegador.\n\nDetalhe: ' + e.message);
    } finally {
      button.disabled = false;
    }
  });
})();

document.querySelectorAll('button[data-copy]').forEach(button => {
  const label = button.textContent;
  let timer;
  const flash = text => {
    button.textContent = text;
    clearTimeout(timer);
    timer = setTimeout(() => { button.textContent = label; }, 2000);
  };
  const fallbackCopy = text => {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.append(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    if (!ok) throw new Error('copy failed');
  };
  button.addEventListener('click', async () => {
    const source = document.querySelector(button.dataset.copy);
    if (!source) return;
    const text = source.textContent;
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
      else fallbackCopy(text);
      flash('Copiado ✓');
    } catch (e) {
      try { fallbackCopy(text); flash('Copiado ✓'); } catch (e2) { flash('Falha ao copiar'); }
    }
  });
});
