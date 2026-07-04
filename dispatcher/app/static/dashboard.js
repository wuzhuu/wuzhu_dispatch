// wuzhu-dispatch Dashboard — CSP-compliant, no innerHTML for user data, no localStorage.
// All user-controlled values use textContent, not innerHTML.

(function () {
  'use strict';

  // ── API helpers ────────────────────────────────────────────────
  async function apiGet(path) {
    const r = await fetch(path, { credentials: 'include' });
    if (r.status === 401) return null;
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  async function apiPost(path, body) {
    const csrf = getCSRF();
    const h = { 'Content-Type': 'application/json' };
    if (csrf) h['X-CSRF-Token'] = csrf;
    const r = await fetch(path, {
      method: 'POST', headers: h, credentials: 'include',
      body: body ? JSON.stringify(body) : undefined,
    });
    if (r.status === 401) return null;
    return r.json();
  }

  function getCSRF() {
    var m = document.cookie.match(/csrf_token=([^;]+)/);
    return m ? m[1] : '';
  }

  // ── DOM helpers (safe — no innerHTML for user data) ────────────
  function clearMain() {
    var el = document.getElementById('mainContent');
    while (el.firstChild) el.removeChild(el.firstChild);
    return el;
  }

  function textNode(text) { return document.createTextNode(text); }

  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    if (attrs) for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (children) for (var i = 0; i < children.length; i++) e.appendChild(children[i]);
    return e;
  }

  function td(text, className) {
    var c = el('td');
    if (className) c.className = className;
    c.appendChild(textNode(text));
    return c;
  }

  function formatResource(usageStr, total, unit) {
    if (!total) return usageStr;
    return usageStr + ' (' + total + ' ' + unit + ')';
  }

  // ── Router ─────────────────────────────────────────────────────
  function getPage() {
    var p = window.location.pathname;
    if (p === '/admin' || p === '/admin/') return 'overview';
    if (p.indexOf('/admin/nodes') === 0) return 'nodes';
    if (p.indexOf('/admin/tasks') === 0) return 'tasks';
    if (p.indexOf('/admin/login') === 0) return 'login';
    if (p.indexOf('/admin/logout') === 0) return 'logout';
    return 'overview';
  }

  async function navigateTo(page) {
    var url = page === 'overview' ? '/admin' : '/admin/' + page;
    window.history.pushState({ page: page }, '', url);
    await renderPage(page);
  }

  // ── Pages ──────────────────────────────────────────────────────

  async function renderOverview() {
    var summary = await apiGet('/api/v1/admin/dashboard/summary');
    if (!summary) { showLogin(); return; }

    var main = clearMain();
    main.appendChild(el('h1', {}, [textNode('Overview')]));

    // Cards
    var cards = el('div', { 'class': 'cards' });
    var cardData = [
      ['Online Nodes', summary.nodes.online + ' / ' + summary.nodes.total, ''],
      ['Pending Tasks', '' + summary.tasks.pending, ''],
      ['Running Tasks', '' + summary.tasks.running, ''],
      ['Success Today', '' + summary.tasks.success_today, 'card-success'],
      ['Failed Today', '' + summary.tasks.failed_today, 'card-danger'],
      ['Avg CPU', summary.resources.avg_cpu_usage + '%', ''],
      ['Avg Memory', summary.resources.avg_memory_usage + '%', ''],
      ['Total RX', summary.resources.total_rx_mbps + ' Mbps', ''],
      ['Total TX', summary.resources.total_tx_mbps + ' Mbps', ''],
    ];
    cardData.forEach(function (d) {
      var c = el('div', { 'class': 'card' + (d[2] ? ' ' + d[2] : '') });
      c.appendChild(el('div', { 'class': 'card-label' }, [textNode(d[0])]));
      c.appendChild(el('div', { 'class': 'card-value' }, [textNode(d[1])]));
      cards.appendChild(c);
    });
    main.appendChild(cards);

    // Recent tasks
    main.appendChild(el('h2', {}, [textNode('Recent Tasks')]));
    var tbody = document.createElement('tbody');
    var tasks = await apiGet('/api/v1/admin/dashboard/recent-tasks?limit=10');
    if (tasks) {
      tasks.forEach(function (t) { tbody.appendChild(buildTaskRow(t)); });
      main.appendChild(buildTable(['ID', 'Type', 'Status', 'Priority', 'Node', 'Retry', 'Created'], tbody));
    }
  }

  async function renderNodes() {
    var nodes = await apiGet('/api/v1/admin/dashboard/nodes');
    if (!nodes) { showLogin(); return; }

    var main = clearMain();
    main.appendChild(el('h1', {}, [textNode('Compute Nodes')]));

    var tbody = document.createElement('tbody');
    nodes.forEach(function (n) {
      var tr = document.createElement('tr');
      // Node
      var td1 = el('td');
      td1.appendChild(el('strong', {}, [textNode(n.node_id)]));
      td1.appendChild(el('br'));
      td1.appendChild(el('small', {}, [textNode(n.name || '')]));
      tr.appendChild(td1);
      tr.appendChild(td(n.region || '-'));
      // Status badges
      var tdStatus = el('td');
      var badge = el('span', { 'class': n.online ? 'badge badge-online' : 'badge badge-offline' });
      badge.appendChild(textNode(n.online ? 'Online' : 'Offline'));
      tdStatus.appendChild(badge);
      if (!n.enabled) {
        var dbadge = el('span', { 'class': 'badge badge-disabled' });
        dbadge.appendChild(textNode('Disabled'));
        tdStatus.appendChild(dbadge);
      }
      tr.appendChild(tdStatus);
      tr.appendChild(td(formatResource(n.cpu_usage + '%', n.total_cpu_cores, 'cores')));
      tr.appendChild(td(formatResource(n.memory_usage + '%', n.total_memory_mb, 'MB')));
      tr.appendChild(td(formatResource(n.disk_usage + '%', n.total_disk_mb, 'MB')));
      tr.appendChild(td(n.running_tasks + '/' + n.max_parallel_tasks));
      tr.appendChild(td(n.rx_mbps.toFixed(1) + '/' + n.tx_mbps.toFixed(1)));
      tr.appendChild(td(n.last_heartbeat ? new Date(n.last_heartbeat).toLocaleString() : '-'));
      tbody.appendChild(tr);
    });
    main.appendChild(buildTable(['Node', 'Region', 'Status', 'CPU', 'Memory', 'Disk', 'Tasks', 'RX/TX', 'Last Heartbeat'], tbody));
  }

  async function renderTasks() {
    var tasks = await apiGet('/api/v1/admin/tasks');
    if (!tasks) { showLogin(); return; }
    var main = clearMain();
    main.appendChild(el('h1', {}, [textNode('All Tasks')]));
    var tbody = document.createElement('tbody');
    tasks.forEach(function (t) { tbody.appendChild(buildTaskRow(t)); });
    main.appendChild(buildTable(['ID', 'Type', 'Status', 'Priority', 'Node', 'Retry', 'Created'], tbody));
  }

  function buildTaskRow(t) {
    var tr = document.createElement('tr');
    tr.appendChild(el('td', {}, [el('code', {}, [textNode(t.task_id.slice(0, 12) + '...')])]));
    tr.appendChild(td(t.type));
    var statusClass = '';
    if (t.status === 'success') statusClass = 'status-success';
    else if (t.status === 'failed' || t.status === 'timeout') statusClass = 'status-failed';
    else if (t.status === 'running') statusClass = 'status-running';
    tr.appendChild(td(t.status, statusClass));
    tr.appendChild(td('' + t.priority));
    tr.appendChild(td(t.assigned_node_id || '-'));
    tr.appendChild(td(t.retry_count + '/' + t.max_retries));
    tr.appendChild(td(t.created_at ? new Date(t.created_at).toLocaleString() : '-'));
    return tr;
  }

  function buildTable(headers, tbody) {
    var table = document.createElement('table');
    var thead = document.createElement('thead');
    var tr = document.createElement('tr');
    headers.forEach(function (h) {
      var th = document.createElement('th');
      th.appendChild(textNode(h));
      tr.appendChild(th);
    });
    thead.appendChild(tr);
    table.appendChild(thead);
    table.appendChild(tbody);
    return table;
  }

  function showLogin() {
    var main = clearMain();
    main.appendChild(el('h1', {}, [textNode('Login')]));

    var form = document.createElement('form');
    form.id = 'loginForm';

    function formGroup(labelText, inputType, inputId, autocompleteVal) {
      var fg = el('div', { 'class': 'form-group' });
      var lbl = document.createElement('label');
      lbl.setAttribute('for', inputId);
      lbl.appendChild(textNode(labelText));
      fg.appendChild(lbl);
      var inp = document.createElement('input');
      inp.type = inputType;
      inp.id = inputId;
      inp.name = inputId;
      inp.required = true;
      inp.setAttribute('autocomplete', autocompleteVal);
      fg.appendChild(inp);
      return fg;
    }
    form.appendChild(formGroup('Username', 'text', 'username', 'username'));
    form.appendChild(formGroup('Password', 'password', 'password', 'current-password'));

    var errDiv = el('div', { 'id': 'loginError', 'class': 'error hidden' });
    form.appendChild(errDiv);

    var btn = el('button', { 'type': 'submit', 'class': 'btn' }, [textNode('Login')]);
    form.appendChild(btn);

    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      var username = document.getElementById('username').value;
      var password = document.getElementById('password').value;
      var r = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username, password: password }),
        credentials: 'include',
      });
      if (r.ok) {
        document.getElementById('loginLink').classList.add('hidden');
        document.getElementById('logoutLink').classList.remove('hidden');
        await navigateTo('overview');
      } else {
        var err = document.getElementById('loginError');
        err.textContent = 'Invalid username or password';
        err.classList.remove('hidden');
      }
    });
    main.appendChild(form);

    var note = el('p', { 'class': 'note' });
    note.appendChild(textNode('No default credentials. Create an owner user via API first.'));
    main.appendChild(note);
  }

  async function handleLogout() {
    // Use apiPost which includes CSRF token from cookie
    await apiPost('/api/v1/auth/logout');
    document.getElementById('loginLink').classList.remove('hidden');
    document.getElementById('logoutLink').classList.add('hidden');
    showLogin();
  }

  async function renderPage(page) {
    var links = document.querySelectorAll('.nav-link');
    for (var i = 0; i < links.length; i++) links[i].classList.remove('active');
    var active = document.querySelector('[data-page="' + page + '"]');
    if (active) active.classList.add('active');

    switch (page) {
      case 'overview': await renderOverview(); break;
      case 'nodes': await renderNodes(); break;
      case 'tasks': await renderTasks(); break;
      case 'login': showLogin(); break;
      case 'logout': await handleLogout(); break;
      default:
        var main = clearMain();
        main.appendChild(el('h1', {}, [textNode('404')]));
        main.appendChild(el('p', {}, [textNode('Page not found.')]));
    }
  }

  // ── Init ────────────────────────────────────────────────────────
  window.addEventListener('popstate', async function () {
    await renderPage(getPage());
  });

  (async function () {
    await renderPage(getPage());
  })();

})();
