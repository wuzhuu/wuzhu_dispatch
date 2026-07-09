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

  async function apiPatch(path, body) {
    const csrf = getCSRF();
    const h = { 'Content-Type': 'application/json' };
    if (csrf) h['X-CSRF-Token'] = csrf;
    const r = await fetch(path, {
      method: 'PATCH', headers: h, credentials: 'include',
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
    if (text instanceof Node) { c.appendChild(text); }
    else { c.appendChild(textNode(String(text))); }
    return c;
  }

  function formatResource(usageStr, total, unit) {
    if (!total) return usageStr;
    return usageStr + ' (' + total + ' ' + unit + ')';
  }

  function btn(text, className, clickHandler) {
    var b = el('button', { 'class': className || 'btn btn-sm' });
    b.appendChild(textNode(text));
    if (clickHandler) b.addEventListener('click', clickHandler);
    return b;
  }

  // ── Toast notification ─────────────────────────────────────────
  function showToast(msg, type) {
    var existing = document.getElementById('toast');
    if (existing) existing.remove();
    var t = el('div', { id: 'toast', 'class': 'toast toast-' + (type || 'info') });
    t.appendChild(textNode(msg));
    document.body.appendChild(t);
    setTimeout(function () { t.classList.add('toast-show'); }, 10);
    setTimeout(function () {
      t.classList.remove('toast-show');
      setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 300);
    }, 3000);
  }

  // ── Router ─────────────────────────────────────────────────────
  function getPage() {
    var p = window.location.pathname;
    if (p === '/admin' || p === '/admin/') return 'overview';
    if (p.indexOf('/admin/nodes') === 0) return 'nodes';
    if (p.indexOf('/admin/tasks') === 0) return 'tasks';
    if (p.indexOf('/admin/users') === 0) return 'users';
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

    // Refresh button
    var toolbar = el('div', { 'class': 'toolbar' });
    var refreshBtn = btn('Refresh', 'btn btn-sm', function () { renderNodes(); });
    toolbar.appendChild(refreshBtn);
    main.appendChild(toolbar);

    var tbody = document.createElement('tbody');
    nodes.forEach(function (n) {
      var tr = document.createElement('tr');
      tr.id = 'node-row-' + n.node_id;
      // Node ID + name
      var td1 = el('td');
      var strongEl = el('strong', {});
      strongEl.appendChild(textNode(n.node_id));
      td1.appendChild(strongEl);
      td1.appendChild(el('br'));
      var nameSmall = el('small', {}, [textNode(n.name || '')]);
      td1.appendChild(nameSmall);
      tr.appendChild(td1);
      tr.appendChild(td(n.region || '-'));
      // Status badges
      var tdStatus = el('td');
      var badge = el('span', { 'class': n.online ? 'badge badge-online' : 'badge badge-offline' });
      badge.appendChild(textNode(n.online ? 'Online' : 'Offline'));
      tdStatus.appendChild(badge);
      if (!n.enabled) {
        var dbadge2 = el('span', { 'class': 'badge badge-disabled' });
        dbadge2.appendChild(textNode('Disabled'));
        tdStatus.appendChild(dbadge2);
      }
      tr.appendChild(tdStatus);
      tr.appendChild(td(formatResource(n.cpu_usage + '%', n.total_cpu_cores, 'cores')));
      tr.appendChild(td(formatResource(n.memory_usage + '%', n.total_memory_mb, 'MB')));
      tr.appendChild(td(formatResource(n.disk_usage + '%', n.total_disk_mb, 'MB')));
      tr.appendChild(td(n.running_tasks + '/' + n.max_parallel_tasks));
      tr.appendChild(td(n.rx_mbps.toFixed(1) + '/' + n.tx_mbps.toFixed(1)));
      tr.appendChild(td(n.last_heartbeat ? new Date(n.last_heartbeat).toLocaleString() : '-'));

      // Actions column
      var tdActions = el('td', { 'class': 'actions-cell' });
      // Enable/Disable toggle
      var toggleBtnText = n.enabled ? 'Disable' : 'Enable';
      var toggleBtn = btn(toggleBtnText, 'btn btn-sm btn-' + (n.enabled ? 'warning' : 'success'),
        function () { toggleNode(n.node_id, !n.enabled); });
      tdActions.appendChild(toggleBtn);
      // Edit button
      var editBtn = btn('Edit', 'btn btn-sm btn-primary',
        function () { showNodeEditForm(n.node_id, tr); });
      tdActions.appendChild(editBtn);
      tr.appendChild(tdActions);

      tbody.appendChild(tr);
    });
    main.appendChild(buildTable(['Node', 'Region', 'Status', 'CPU', 'Memory', 'Disk', 'Tasks', 'RX/TX', 'Last Heartbeat', 'Actions'], tbody));

    // Node detail / edit section at bottom
    main.appendChild(el('div', { id: 'node-edit-area' }));
  }

  // ── Node Edit Form ────────────────────────────────────────────
  function showNodeEditForm(nodeId, rowTr) {
    // Remove any existing edit row
    var existingEdit = document.getElementById('node-edit-row-' + nodeId);
    if (existingEdit) {
      existingEdit.parentNode.removeChild(existingEdit);
      return; // toggle off
    }
    var existing = document.querySelector('.node-edit-row');
    if (existing) existing.parentNode.removeChild(existing);

    // Fetch fresh node data
    apiGet('/api/v1/admin/nodes/' + nodeId).then(function (node) {
      if (!node) return;
      var editRow = document.createElement('tr');
      editRow.id = 'node-edit-row-' + nodeId;
      editRow.className = 'node-edit-row';
      var editCell = document.createElement('td');
      editCell.setAttribute('colspan', '11');

      var formDiv = el('div', { 'class': 'node-edit-form' });

      // Title
      var title = el('h3', {}, [textNode('Edit Node: ' + nodeId)]);
      formDiv.appendChild(title);

      // Two-column layout
      var cols = el('div', { 'class': 'edit-columns' });

      // Left column
      var left = el('div', { 'class': 'edit-col' });
      left.appendChild(formGroup('Name', 'text', 'edit-name-' + nodeId, node.name || ''));
      left.appendChild(formGroup('Region', 'text', 'edit-region-' + nodeId, node.region || ''));
      left.appendChild(formGroup('Provider', 'text', 'edit-provider-' + nodeId, node.provider || ''));
      cols.appendChild(left);

      // Right column
      var right = el('div', { 'class': 'edit-col' });
      right.appendChild(formGroup('Tags (comma-separated)', 'text', 'edit-tags-' + nodeId,
        (node.tags || []).join(', ')));
      // Static profile JSON
      var fg = el('div', { 'class': 'form-group' });
      var lbl = document.createElement('label');
      lbl.setAttribute('for', 'edit-profile-' + nodeId);
      lbl.appendChild(textNode('Static Profile (JSON)'));
      fg.appendChild(lbl);
      var ta = document.createElement('textarea');
      ta.id = 'edit-profile-' + nodeId;
      ta.rows = 6;
      ta.className = 'form-textarea';
      try { ta.value = JSON.stringify(node.static_profile || {}, null, 2); } catch (e) { ta.value = '{}'; }
      fg.appendChild(ta);
      right.appendChild(fg);
      cols.appendChild(right);

      formDiv.appendChild(cols);

      // Buttons
      var btnGroup = el('div', { 'class': 'edit-buttons' });

      var saveBtn = btn('Save', 'btn btn-sm btn-primary', function () {
        saveNodeEdit(nodeId, editRow);
      });
      btnGroup.appendChild(saveBtn);

      var cancelBtn = btn('Cancel', 'btn btn-sm', function () {
        if (editRow.parentNode) editRow.parentNode.removeChild(editRow);
      });
      btnGroup.appendChild(cancelBtn);

      // Delete node button (disable first, then remove)
      var deleteBtn = btn('Delete Node', 'btn btn-sm btn-danger', function () {
        if (confirm('Disable and remove node "' + nodeId + '"? This cannot be undone easily.')) {
          removeNode(nodeId, editRow);
        }
      });
      btnGroup.appendChild(deleteBtn);

      formDiv.appendChild(btnGroup);
      editCell.appendChild(formDiv);
      editRow.appendChild(editCell);

      // Insert after the current row
      if (rowTr.parentNode) {
        rowTr.parentNode.insertBefore(editRow, rowTr.nextSibling);
      }
    });
  }

  function formGroup(labelText, inputType, inputId, value) {
    var fg = el('div', { 'class': 'form-group' });
    var lbl = document.createElement('label');
    lbl.setAttribute('for', inputId);
    lbl.appendChild(textNode(labelText));
    fg.appendChild(lbl);
    var inp = document.createElement('input');
    inp.type = inputType;
    inp.id = inputId;
    inp.name = inputId;
    inp.value = value || '';
    inp.className = 'form-input';
    fg.appendChild(inp);
    return fg;
  }

  async function saveNodeEdit(nodeId, editRow) {
    var name = document.getElementById('edit-name-' + nodeId).value;
    var region = document.getElementById('edit-region-' + nodeId).value;
    var provider = document.getElementById('edit-provider-' + nodeId).value;
    var tagsStr = document.getElementById('edit-tags-' + nodeId).value;
    var profileStr = document.getElementById('edit-profile-' + nodeId).value;

    // Parse tags
    var tags = tagsStr.split(',').map(function (t) { return t.trim(); }).filter(function (t) { return t.length > 0; });

    // Parse JSON profile
    var static_profile = {};
    try {
      if (profileStr.trim()) {
        static_profile = JSON.parse(profileStr);
      }
    } catch (e) {
      showToast('Invalid JSON in Static Profile', 'error');
      return;
    }

    var body = {
      name: name,
      region: region,
      provider: provider,
      tags: tags,
      static_profile: static_profile,
    };

    // Remove empty values so we don't send null
    for (var k in body) {
      if (body[k] === '') body[k] = null;
    }

    var result = await apiPatch('/api/v1/admin/nodes/' + nodeId, body);
    if (result) {
      showToast('Node ' + nodeId + ' updated successfully', 'success');
      // Remove edit row and re-render nodes
      if (editRow.parentNode) editRow.parentNode.removeChild(editRow);
      renderNodes();
    } else {
      showToast('Failed to update node ' + nodeId, 'error');
    }
  }

  async function toggleNode(nodeId, enable) {
    var action = enable ? 'enable' : 'disable';
    var result = await apiPost('/api/v1/admin/nodes/' + nodeId + '/' + action);
    if (result) {
      showToast('Node ' + nodeId + ' ' + action + 'd', 'success');
      renderNodes();
    } else {
      showToast('Failed to ' + action + ' node ' + nodeId, 'error');
    }
  }

  async function removeNode(nodeId, editRow) {
    // First disable, then remove the node
    await apiPost('/api/v1/admin/nodes/' + nodeId + '/disable');
    // There's no DELETE endpoint, so we just disable and hide
    if (editRow.parentNode) editRow.parentNode.removeChild(editRow);
    showToast('Node ' + nodeId + ' disabled', 'success');
    renderNodes();
  }

  // ── Users Page ─────────────────────────────────────────────────
  async function renderUsers() {
    var users = await apiGet('/api/v1/admin/users');
    if (!users) { showLogin(); return; }

    var main = clearMain();
    main.appendChild(el('h1', {}, [textNode('Users')]));
    main.appendChild(el('p', { 'class': 'note' }, [textNode('Manage user accounts and permissions.')]));

    var tbody = document.createElement('tbody');
    users.forEach(function (u) {
      var tr = document.createElement('tr');
      tr.id = 'user-row-' + u.user_id;
      tr.appendChild(td(u.username));
      tr.appendChild(td(u.role));
      // Status
      var tdStatus2 = el('td');
      var statusBadge = el('span', { 'class': u.enabled ? 'badge badge-online' : 'badge badge-disabled' });
      statusBadge.appendChild(textNode(u.enabled ? 'Enabled' : 'Disabled'));
      tdStatus2.appendChild(statusBadge);
      tr.appendChild(tdStatus2);
      tr.appendChild(td(u.created_at ? new Date(u.created_at).toLocaleString() : '-'));
      // Actions
      var tdUA = el('td', { 'class': 'actions-cell' });

      // Enable/Disable toggle
      var toggleUserBtn = btn(u.enabled ? 'Disable' : 'Enable', 'btn btn-sm btn-' + (u.enabled ? 'warning' : 'success'),
        function () { toggleUser(u.user_id, !u.enabled); });
      tdUA.appendChild(toggleUserBtn);

      // Role change dropdown
      var roleForm = el('form', { 'class': 'inline-form' });
      var select = document.createElement('select');
      select.id = 'role-select-' + u.user_id;
      select.className = 'form-select';
      ['viewer', 'operator', 'admin', 'owner'].forEach(function (r) {
        var opt = document.createElement('option');
        opt.value = r;
        opt.appendChild(textNode(r));
        if (r === u.role) opt.selected = true;
        select.appendChild(opt);
      });
      roleForm.appendChild(select);
      var changeRoleBtn = btn('Set', 'btn btn-sm', function () {
        changeUserRole(u.user_id, select.value);
      });
      roleForm.appendChild(changeRoleBtn);
      tdUA.appendChild(roleForm);

      tr.appendChild(tdUA);
      tbody.appendChild(tr);
    });
    main.appendChild(buildTable(['Username', 'Role', 'Status', 'Created', 'Actions'], tbody));
  }

  async function toggleUser(userId, enable) {
    var result = await apiPatch('/api/v1/admin/users/' + userId, { enabled: enable });
    if (result) {
      showToast('User ' + (enable ? 'enabled' : 'disabled'), 'success');
      renderUsers();
    } else {
      showToast('Failed to update user', 'error');
    }
  }

  async function changeUserRole(userId, role) {
    var result = await apiPatch('/api/v1/admin/users/' + userId, { role: role });
    if (result) {
      showToast('Role changed to ' + role, 'success');
      renderUsers();
    } else {
      showToast('Failed to change role', 'error');
    }
  }

  // ── Tasks ─────────────────────────────────────────────────────
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

  // ── Login / Logout ────────────────────────────────────────────
  function showLogin() {
    var main = clearMain();
    main.appendChild(el('h1', {}, [textNode('Login')]));

    var form = document.createElement('form');
    form.id = 'loginForm';

    function formGroup2(labelText, inputType, inputId, autocompleteVal) {
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
    form.appendChild(formGroup2('Username', 'text', 'username', 'username'));
    form.appendChild(formGroup2('Password', 'password', 'password', 'current-password'));

    var errDiv = el('div', { 'id': 'loginError', 'class': 'error hidden' });
    form.appendChild(errDiv);

    var btnEl = el('button', { 'type': 'submit', 'class': 'btn' }, [textNode('Login')]);
    form.appendChild(btnEl);

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
      case 'users': await renderUsers(); break;
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
