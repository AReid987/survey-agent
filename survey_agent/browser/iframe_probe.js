/**
 * iframe_probe.js — Recursive iframe detector for survey agent.
 *
 * Finds ALL iframes at any depth (including nested, shadow DOM, framesets),
 * extracts their text content and form elements, and returns structured data.
 *
 * With --disable-web-security active, this can access cross-origin iframes
 * via contentDocument. When not, it reports the frame but marks cross-origin.
 */
(function() {
  var results = [];

  function probeFrame(frame, depth, parentPath) {
    var items = [];
    var iframes;
    try {
      iframes = frame.querySelectorAll ? frame.querySelectorAll('iframe, frame, object[type="text/html"], embed[type="text/html"]') : [];
    } catch(e) {
      return items;
    }

    for (var i = 0; i < iframes.length; i++) {
      var el = iframes[i];
      var rect;
      try { rect = el.getBoundingClientRect(); } catch(e) { rect = {width: 0, height: 0}; }

      var frameInfo = {
        idx: i,
        parentPath: parentPath,
        depth: depth,
        visible: rect.width > 0 && rect.height > 0,
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        src: (el.src || el.data || '').substring(0, 500),
        id: el.id || '',
        name: el.name || '',
        cls: (el.className || '').substring(0, 100),
        sandbox: el.sandbox ? el.sandbox.value : '',
      };

      // Try to access contentDocument
      var doc = null;
      try {
        doc = el.contentDocument || (el.contentWindow ? el.contentWindow.document : null);
        frameInfo.access = doc ? 'same-origin' : 'no-doc';
      } catch(e) {
        frameInfo.access = 'cross-origin';
        frameInfo.accessError = e.message ? e.message.substring(0, 100) : 'unknown';
      }

      if (doc && doc.body) {
        // Read text content
        var bodyText = doc.body.innerText || '';
        frameInfo.textLength = bodyText.length;
        frameInfo.text = bodyText.substring(0, 5000);

        // Find interactive elements
        var els = [];
        var formEls;
        try {
          formEls = doc.querySelectorAll('input, select, textarea, button, [role=button], [role=radio], [role=option], [role=checkbox], [role=switch]');
        } catch(e) {
          formEls = [];
        }
        for (var j = 0; j < formEls.length; j++) {
          var fe = formEls[j];
          // Skip hidden non-radio/checkbox elements
          if (fe.offsetParent === null && fe.type !== 'radio' && fe.type !== 'checkbox') continue;
          var tag = fe.tagName.toLowerCase();
          var feText = (fe.textContent || fe.value || '').trim().substring(0, 100);
          if ((fe.type === 'radio' || fe.type === 'checkbox') && !feText) {
            var label = fe.getAttribute('aria-label') || '';
            if (!label && fe.id) {
              var lblEl = doc.querySelector('label[for="' + fe.id + '"]');
              if (lblEl) label = lblEl.textContent.trim();
            }
            if (!label) {
              var parent = fe.closest('label') || fe.parentElement;
              if (parent) label = (parent.textContent || '').trim().replace(/\s+/g, ' ').substring(0, 100);
            }
            feText = label;
          }
          var type = fe.type || fe.getAttribute('role') || '';
          var name = fe.name || '';
          var feId = fe.id || '';
          var checked = fe.checked || false;
          var placeholder = fe.placeholder || '';
          var opts = [];
          if (tag === 'select') {
            try {
              for (var k = 0; k < fe.options.length && k < 20; k++) {
                opts.push({v: fe.options[k].value, t: (fe.options[k].text || '').trim()});
              }
            } catch(e) {}
          }
          els.push({tag: tag, type: type, name: name, id: feId, text: feText, checked: checked, placeholder: placeholder, opts: opts});
        }
        frameInfo.elements = els;
        frameInfo.elementCount = els.length;

        // Recurse into nested iframes
        var nested = probeFrame(doc, depth + 1, parentPath !== null ? parentPath + '-' + i : '' + i);
        frameInfo.nestedFrames = nested;
      }

      items.push(frameInfo);
    }
    return items;
  }

  // Start from the top document
  var allFrames = probeFrame(document, 0, null);

  // Check shadow DOM roots for iframes
  function probeShadowRoots(root, depth, parentPath) {
    var shadowItems = [];
    var all;
    try {
      all = root.querySelectorAll('*');
    } catch(e) {
      return shadowItems;
    }
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (el.shadowRoot) {
        var shadowDoc = el.shadowRoot;
        var shadowIframes;
        try {
          shadowIframes = shadowDoc.querySelectorAll('iframe');
        } catch(e) {
          continue;
        }
        for (var j = 0; j < shadowIframes.length; j++) {
          var iframe = shadowIframes[j];
          var rect;
          try { rect = iframe.getBoundingClientRect(); } catch(e) { rect = {width: 0, height: 0}; }
          var info = {
            idx: j,
            parentPath: parentPath,
            depth: depth,
            inShadowDOM: true,
            visible: rect.width > 0 && rect.height > 0,
            width: Math.round(rect.width),
            height: Math.round(rect.height),
            src: (iframe.src || '').substring(0, 500),
            id: iframe.id || '',
            name: iframe.name || '',
          };
          try {
            var doc = iframe.contentDocument || (iframe.contentWindow ? iframe.contentWindow.document : null);
            info.access = doc ? 'same-origin' : 'no-doc';
            if (doc && doc.body) {
              info.text = (doc.body.innerText || '').substring(0, 3000);
              info.textLength = (doc.body.innerText || '').length;
            }
          } catch(e) {
            info.access = 'cross-origin';
          }
          shadowItems.push(info);
        }
      }
    }
    return shadowItems;
  }

  var shadowFrames = probeShadowRoots(document, 0, '');

  return JSON.stringify({
    totalFrames: allFrames.length,
    totalShadowFrames: shadowFrames.length,
    frames: allFrames,
    shadowFrames: shadowFrames,
  });
})();
