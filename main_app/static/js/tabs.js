// Traveling tab indicator (same idea as Secretary's nav-tabs): one accent bar
// that slides to the active tab — the edge facing the destination moves on
// the faster curve, the trailing edge settles behind it, so the bar stretches
// toward the target and contracts into place. Cross-page flow comes from the
// View Transitions API (the bar carries a view-transition-name), this script
// handles the in-page part: first paint without motion, slide on click.
(function () {
  var FAST = '220ms cubic-bezier(.2,.8,.2,1)', SLOW = '340ms cubic-bezier(.4,0,.2,1)';
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function setup(container, name) {
    var links = Array.prototype.slice.call(container.querySelectorAll('a'));
    if (!links.length) return;
    var active = container.querySelector('a.active') || null;
    var bar = document.createElement('span');
    bar.className = 'tab-indicator';
    bar.style.viewTransitionName = name;
    container.appendChild(bar);
    container.classList.add('has-indicator');
    var armed = false, current = active;

    function place(link, movingRight) {
      if (!link) { bar.style.opacity = '0'; return; }
      bar.style.opacity = '1';
      var left = link.offsetLeft, right = container.clientWidth - (link.offsetLeft + link.offsetWidth);
      var top = link.offsetTop + link.offsetHeight - 2;
      if (armed && !reduce) {
        bar.style.transition = movingRight
          ? 'right ' + FAST + ', left ' + SLOW + ', top 200ms ease'
          : 'left ' + FAST + ', right ' + SLOW + ', top 200ms ease';
      } else {
        bar.style.transition = 'none';
      }
      bar.style.left = left + 'px';
      bar.style.right = right + 'px';
      bar.style.top = top + 'px';
    }
    place(active, true);
    requestAnimationFrame(function () { requestAnimationFrame(function () { armed = true; }); });
    links.forEach(function (link) {
      link.addEventListener('click', function () {
        var movingRight = !current || link.offsetLeft > current.offsetLeft;
        current = link;
        links.forEach(function (l) { l.classList.remove('active'); });
        link.classList.add('active');
        place(link, movingRight);
      });
    });
    var ro = new ResizeObserver(function () { var a = armed; armed = false; place(current, true); armed = a; });
    ro.observe(container);
  }

  function init() {
    document.querySelectorAll('.tabs').forEach(function (el, i) { setup(el, 'tabs-indicator-' + i); });
    var nav = document.querySelector('.site-nav ul');
    if (nav) setup(nav, 'nav-indicator');
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
