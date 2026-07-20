/**
 * apply_answers.js — Iframe-aware answer application for survey agent.
 *
 * Recursively searches all iframes for elements matching selectors,
 * then applies click/fill/select actions.
 * 
 * Template variable: $_ANSWERS_JSON is substituted at runtime.
 */
(function() {
  "use strict";
  
  // Search element across main document and all iframes (recursive)
  function findInFrames(rootDoc, selector) {
    try {
      var el = rootDoc.querySelector(selector);
      if (el) return el;
      var iframes = rootDoc.querySelectorAll('iframe, frame');
      for (var i = 0; i < iframes.length; i++) {
        try {
          var doc = iframes[i].contentDocument || (iframes[i].contentWindow ? iframes[i].contentWindow.document : null);
          if (doc) {
            var found = findInFrames(doc, selector);
            if (found) return found;
          }
        } catch(e) {}
      }
    } catch(e) {}
    return null;
  }
  
  // Search across all frames for text content matching
  function findAllInFrames(rootDoc, allSelector) {
    var results = [];
    try {
      var items = rootDoc.querySelectorAll(allSelector);
      for (var i = 0; i < items.length; i++) results.push(items[i]);
      var iframes = rootDoc.querySelectorAll('iframe, frame');
      for (var i = 0; i < iframes.length; i++) {
        try {
          var doc = iframes[i].contentDocument || (iframes[i].contentWindow ? iframes[i].contentWindow.document : null);
          if (doc) {
            var found = findAllInFrames(doc, allSelector);
            for (var j = 0; j < found.length; j++) results.push(found[j]);
          }
        } catch(e) {}
      }
    } catch(e) {}
    return results;
  }
  
  var answers = $_ANSWERS_JSON;
  var applied = 0;
  for (var k = 0; k < answers.length; k++) {
    var ans = answers[k];
    var sel = ans.selector || '';
    var val = String(ans.value || '');
    var action = ans.action || 'fill';
    if (!sel) continue;
    try {
      // Search across main document AND all iframes
      var el = findInFrames(document, sel);
      // Fallback: find by text content for custom elements
      if (!el && action === 'click' && val) {
        var allClickable = findAllInFrames(document, '[role=radio], [role=option], [role=checkbox], [role=button], button, a, label, li, div[onclick], span[onclick]');
        for (var c = 0; c < allClickable.length; c++) {
          var candidate = allClickable[c];
          var text = (candidate.textContent || '').trim();
          if (text === val || text.toLowerCase() === val.toLowerCase()) {
            el = candidate;
            break;
          }
        }
      }
      if (!el) continue;
      if (action === 'click') {
        el.click(); applied++;
      } else if (action === 'fill') {
        el.focus();
        el.value = val;
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
        applied++;
      } else if (action === 'select') {
        el.value = val;
        el.dispatchEvent(new Event('change', {bubbles:true}));
        applied++;
      }
    } catch(e) {}
  }
  return 'APPLIED:' + applied + '/' + answers.length;
})();
