// Theme toggle, mobile menu, confirm buttons, HTMX CSRF.
(function () {
  var root = document.documentElement;
  var toggle = document.getElementById('theme-toggle');
  if (toggle) {
    toggle.addEventListener('click', function () {
      var next = root.dataset.theme === 'dark' ? 'light' : 'dark';
      root.dataset.theme = next;
      try { localStorage.setItem('theme', next); } catch (e) {}
      document.dispatchEvent(new CustomEvent('theme:change', { detail: next }));
    });
  }
  var menu = document.getElementById('menu-toggle');
  var mobile = document.getElementById('mobile-menu');
  if (menu && mobile) {
    menu.addEventListener('click', function () {
      var open = mobile.hasAttribute('hidden');
      if (open) mobile.removeAttribute('hidden'); else mobile.setAttribute('hidden', '');
      menu.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (form.dataset && form.dataset.confirm && !window.confirm(form.dataset.confirm)) ev.preventDefault();
  });
  document.body.addEventListener('htmx:configRequest', function (ev) {
    var token = document.querySelector('meta[name="csrf-token"]');
    if (token) ev.detail.headers['X-CSRFToken'] = token.content;
  });
  // Strategy-specific param blocks on launch forms.
  document.querySelectorAll('[data-strategy-select]').forEach(function (sel) {
    var show = function () {
      document.querySelectorAll('[data-params-for]').forEach(function (el) {
        el.hidden = el.dataset.paramsFor !== sel.value;
      });
    };
    sel.addEventListener('change', show);
    show();
  });
  document.querySelectorAll('[data-method-select]').forEach(function (sel) {
    var show = function () {
      document.querySelectorAll('[data-walk-forward-only]').forEach(function (el) {
        el.hidden = sel.value !== 'walk_forward';
      });
    };
    sel.addEventListener('change', show);
    show();
  });
})();
