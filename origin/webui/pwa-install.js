/* Origin — one-tap install helper.
   Shows a floating "Install app" button on every page.
   - Android/Chrome/Edge: taps the real install prompt.
   - iPhone/Safari: shows a tiny 2-step hint (Share -> Add to Home Screen).
   Hides itself once the app is already installed. */
(function () {
  // Already running as an installed app? Do nothing.
  var standalone = window.matchMedia && window.matchMedia('(display-mode: standalone)').matches;
  if (standalone || window.navigator.standalone === true) return;

  var deferred = null;      // saved Android/Chrome install event
  var btn, hint;

  var isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream;

  function makeBtn() {
    if (btn) return;
    btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = '\uD83D\uDCF2 Install app';
    btn.setAttribute('aria-label', 'Install the Origin app on this device');
    btn.style.cssText =
      'position:fixed;left:50%;transform:translateX(-50%);bottom:18px;z-index:99999;' +
      'background:#E8551F;color:#fff;border:0;border-radius:999px;padding:13px 22px;' +
      'font:700 15px -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;' +
      'box-shadow:0 6px 20px rgba(0,0,0,.28);cursor:pointer;';
    btn.onclick = onInstallClick;
    document.body.appendChild(btn);
  }

  function removeBtn() {
    if (btn && btn.parentNode) btn.parentNode.removeChild(btn);
    btn = null;
  }

  function onInstallClick() {
    if (deferred) {                     // Android / Chrome / Edge
      deferred.prompt();
      deferred.userChoice.then(function () { deferred = null; removeBtn(); });
      return;
    }
    if (isIOS) { showIosHint(); return; }
    // Fallback for browsers with no prompt event yet
    showGenericHint();
  }

  function overlay(innerHtml) {
    var o = document.createElement('div');
    o.style.cssText =
      'position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,.55);' +
      'display:flex;align-items:center;justify-content:center;padding:24px;';
    var card = document.createElement('div');
    card.style.cssText =
      'background:#fff;color:#2B2F36;border-radius:14px;max-width:340px;width:100%;' +
      'padding:22px 22px 18px;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;' +
      'box-shadow:0 12px 40px rgba(0,0,0,.35);';
    card.innerHTML = innerHtml;
    o.appendChild(card);
    o.onclick = function (e) { if (e.target === o) document.body.removeChild(o); };
    var close = card.querySelector('[data-close]');
    if (close) close.onclick = function () { document.body.removeChild(o); };
    document.body.appendChild(o);
  }

  function showIosHint() {
    overlay(
      '<div style="font-size:17px;font-weight:800;color:#1C1F24;margin-bottom:10px">Add Origin to your Home Screen</div>' +
      '<div style="margin-bottom:8px">1. Tap the <b>Share</b> button ' +
      '<span style="display:inline-block;border:1px solid #ccc;border-radius:5px;padding:0 6px">\u2191</span> ' +
      'at the bottom of Safari.</div>' +
      '<div style="margin-bottom:16px">2. Tap <b>\u201CAdd to Home Screen.\u201D</b></div>' +
      '<button data-close style="width:100%;background:#E8551F;color:#fff;border:0;border-radius:9px;' +
      'padding:12px;font-weight:700;font-size:15px;cursor:pointer">Got it</button>'
    );
  }

  function showGenericHint() {
    overlay(
      '<div style="font-size:17px;font-weight:800;color:#1C1F24;margin-bottom:10px">Install Origin</div>' +
      '<div style="margin-bottom:16px">Open your browser menu and choose ' +
      '<b>\u201CInstall app\u201D</b> or <b>\u201CAdd to Home screen.\u201D</b></div>' +
      '<button data-close style="width:100%;background:#E8551F;color:#fff;border:0;border-radius:9px;' +
      'padding:12px;font-weight:700;font-size:15px;cursor:pointer">Got it</button>'
    );
  }

  // Android/Chrome/Edge fire this when the app is installable.
  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();
    deferred = e;
    makeBtn();
  });

  // Once installed, drop the button.
  window.addEventListener('appinstalled', function () { deferred = null; removeBtn(); });

  // iPhone/Safari never fires beforeinstallprompt — show the button so they
  // still get the 2-step hint. (Only when not already installed.)
  if (isIOS) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', makeBtn);
    } else {
      makeBtn();
    }
  }
})();
