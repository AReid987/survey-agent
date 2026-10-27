#!/usr/bin/env node
/**
 * TopSurveys Agent v6 — DOM-click qualification (proven by answer_qualification.js)
 * 
 * Key design: qualification answers are sent by clicking answer buttons in the DOM,
 * which triggers the page's SDK to POST /qualification. We do NOT call the API directly.
 * We DO read /qualification responses via Network.getResponseBody for structured question data.
 */
const WebSocket = require('ws');
const http = require('http');
const fs = require('fs');
const path = require('path');

const DATA = path.join('/Users/antonioreid/survey-agent/data');
const STATE = path.join(DATA, 'agent_state.json');
const LOG = path.join(DATA, 'survey_run.log');
fs.mkdirSync(DATA, { recursive: true });

const PROFILE = {
  employment: ['self-employed full-time', 'self-employed'],
  income: ['$50,000 - $74,999', '$50,000 - $59,999', '$60,000 - $69,999', '$70,000 - $79,999'],
  gender: ['male'],
  age: ['35 - 44', '35-44', '30-39'],
  hispanic: ['no', 'not of hispanic'],
  race: ['black or african american', 'black', 'african american'],
  state: ['texas', 'tx'],
  education: ["bachelor's degree", 'bachelor', 'college', '4-year degree'],
  children: ['yes', '2', '3'],
  marital: ['single', 'never married'],
  smoke: ['former smoker', 'used to smoke'],
  drink: ["don't drink", "i don't drink", 'never'],
  home: ['rent', 'renting', 'renter'],
};

function log(m) {
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19);
  const l = `[${ts}] ${m}`;
  console.log(l);
  fs.appendFileSync(LOG, l + '\n');
}

function loadState() {
  try { return JSON.parse(fs.readFileSync(STATE, 'utf8')); }
  catch { return { completed: 0, screened: 0, failed: 0, payments: 0, sessions: 0 }; }
}
function saveState(s) { fs.writeFileSync(STATE, JSON.stringify(s, null, 2)); }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function getCDPUrl() {
  return new Promise((resolve, reject) => {
    http.get('http://127.0.0.1:9222/json', res => {
      let d = ''; res.on('data', c => d += c); res.on('end', () => {
        try {
          const p = JSON.parse(d);
          const pg = p.find(x => x.url.includes('topsurveys.app'));
          pg ? resolve(pg.webSocketDebuggerUrl) : reject(new Error('No topsurveys page'));
        } catch (e) { reject(e); }
      });
    }).on('error', reject);
  });
}

async function readBalance(ws, cmd) {
  const r = await cmd('Runtime.evaluate', {
    expression: `(() => {
      const sidebar = document.querySelector('.right-sidebar');
      if (!sidebar) return null;
      for (const s of sidebar.querySelectorAll('span')) {
        const m = s.innerText.trim().match(/^\\$\\s*([0-9]+\\.[0-9]{2})\\s*USD$/);
        if (m) { const v = parseFloat(m[1]); if (v > 0 && v < 100) return v; }
      }
      return null;
    })()`,
    returnByValue: true
  });
  return r.result?.result?.value;
}

// Inject profile into page for radio-answer matching during main survey
const PROFILE_JS = JSON.stringify(PROFILE);

async function runSession(wsUrl, count, state) {
  const ws = new WebSocket(wsUrl);
  let mid = 1;
  const qualReqs = new Map();

  // Wait for WebSocket to actually open before any cmd()
  const wsOpen = new Promise((resolve, reject) => {
    ws.on('open', resolve);
    ws.on('error', e => reject(e));
    setTimeout(() => reject(new Error('WS connect timeout')), 15000);
  });
  await wsOpen;

  function cmd(m, p, t = 25000) {
    return new Promise((res, rej) => {
      const id = mid++;
      ws.send(JSON.stringify({ id, method: m, params: p }));
      const h = d => { const r = JSON.parse(d.toString()); if (r.id === id) { ws.removeListener('message', h); res(r); } };
      ws.on('message', h);
      setTimeout(() => { ws.removeListener('message', h); rej(new Error(`T:${m}`)); }, t);
    });
  }

  // Enable network tracking
  await cmd('Network.enable');

  // Single network listener for entire session
  ws.on('message', data => {
        try {
          const msg = JSON.parse(data.toString());
          if (msg.method === 'Network.requestWillBeSent' && msg.params.request.url.includes('/qualification')) {
            qualReqs.set(msg.params.requestId, {
              url: msg.params.request.url,
              method: msg.params.request.method,
              postData: msg.params.request.postData ? JSON.parse(msg.params.request.postData) : null,
              ts: Date.now()
            });
          }
        } catch {}
      });

      try {
        await cmd('Runtime.evaluate', { expression: `window.__P=${PROFILE_JS}`, returnByValue: true });

        // Navigate to Surveys tab
        log('→ Surveys');
        await cmd('Runtime.evaluate', { expression: `document.querySelector('[data-test-id="surveys-nav"]')?.click()`, returnByValue: true });
        await sleep(3000);

        // Dismiss popups
        await cmd('Runtime.evaluate', {
          expression: `(() => { const b = [...document.querySelectorAll('button')].find(x => { const t = (x.innerText || '').toLowerCase(); return t.includes('awesome') || t.includes('great') || t.includes('ok'); }); if (b) b.click(); const c = document.querySelector('.popup-button'); if (c && c.offsetParent !== null) c.click(); })()`,
          returnByValue: true
        });
        await sleep(1000);

        let done = 0, scr = 0, fail = 0;

        for (let t = 1; t <= count; t++) {
          log(`\n--- Task ${t}/${count} ---`);
          try {
            // Find survey cards (use data-test-id, no offsetParent filter)
            let cards = [];
            for (let a = 0; a < 5; a++) {
              const cr = await cmd('Runtime.evaluate', {
                expression: `(() => { const c = [...document.querySelectorAll('[data-test-id^="ps-survey-"]')]; return c.map(x => ({ id: x.getAttribute('data-test-id') || '', txt: x.innerText.replace(/\\n/g, ' ').trim().slice(0, 200), r: (x.innerText || '').match(/\\$\\s*([0-9.]+)/)?.[1] || '0' })); })()`,
                returnByValue: true
              });
              cards = (cr.result?.result?.value || []).filter(x => x.id);
              if (cards.length > 0) break;
              await sleep(2000);
            }

            if (!cards.length) {
              const diag = await cmd('Runtime.evaluate', {
                expression: `JSON.stringify({ url: window.location.href, popup: !!document.querySelector('.popup-container'), surveyCount: document.querySelectorAll('[data-test-id^="ps-survey-"]').length, mainText: document.body.innerText.slice(0, 300) })`,
                returnByValue: true
              });
              log(`  DOM: ${diag.result?.result?.value}`);
              fail++; await sleep(5000); continue;
            }

            cards.sort((a, b) => parseFloat(b.r) - parseFloat(a.r));
            const card = cards[0];
            const reward = parseFloat(card.r);
            log(`  ${card.txt.slice(0, 60)} ($${reward.toFixed(2)})`);

            const balBefore = await readBalance(ws, cmd);
            log(`  Balance: ${balBefore !== null ? '$' + balBefore.toFixed(2) : '?'}`);

            // Click survey card
            qualReqs.clear();
            await cmd('Runtime.evaluate', { expression: `document.querySelector('[data-test-id="${card.id}"]')?.click()`, returnByValue: true });
            await sleep(3000);

            // Wait for qualification modal (popup-container)
            let modalFound = false;
            for (let w = 0; w < 10; w++) {
              const check = await cmd('Runtime.evaluate', { expression: `document.querySelector('.popup-container')?.offsetParent !== null`, returnByValue: true });
              if (check.result?.result?.value) { modalFound = true; break; }
              await sleep(1000);
            }

            if (!modalFound) {
              const t = await cmd('Runtime.evaluate', { expression: 'document.body.innerText', returnByValue: true });
              const txt = (t.result?.result?.value || '').toLowerCase();
              if (txt.includes('not a good match') || txt.includes('disqualif') || txt.includes('not eligible')) {
                log('  Screened immediately'); scr++;
              } else { log('  No modal, assuming failed'); fail++; }
              await cmd('Runtime.evaluate', { expression: `document.querySelector('[data-test-id="surveys-nav"]')?.click()`, returnByValue: true });
              await sleep(2000);
              continue;
            }

            log('  Qual modal open');

            // Answer qualification questions via DOM clicks
            let qAnswered = 0;
            let result = null;

            for (let q = 0; q < 25; q++) {
              await sleep(1000);

              // Check state — scope to modal text ONLY, NOT document.body
              const check = await cmd('Runtime.evaluate', {
                expression: `(() => {
                  const p = document.querySelector('.popup-container');
                  const inModal = p && p.offsetParent !== null;
                  if (!inModal) return 'closed';
                  const pt = p.innerText.toLowerCase();
                  if (pt.includes('thank you') || pt.includes('congratulations') || pt.includes('you earned')) return 'done';
                  if (pt.includes('not a good match') || pt.includes('disqualif') || pt.includes('not eligible') || pt.includes('unfortunately')) return 'scr';
                  return 'open';
                })()`,
                returnByValue: true
              });
              const st = check.result?.result?.value || 'open';

              if (st === 'done') { result = 'completed'; break; }
              if (st === 'scr') { result = 'screened'; break; }
              if (st === 'closed') { log(`  Qual modal closed after Q${qAnswered}`); break; }

              // Try to read the current question from the last /qualification response
              let currentQ = null;
              let lastRespId = null;
              for (const [reqId] of qualReqs) lastRespId = reqId;
              if (lastRespId) {
                try {
                  const resp = await cmd('Network.getResponseBody', { requestId: lastRespId }, 5000);
                  if (resp.result?.body) {
                    const data = JSON.parse(resp.result.body);
                    if (data.status === 'rejected') { log(`  Rejected: ${data.message || ''}`); result = 'screened'; break; }
                    if (data.questions?.current) currentQ = data.questions.current;
                  }
                } catch {}
              }

              // Find visible answer buttons in the modal
              const answerButtons = await cmd('Runtime.evaluate', {
                expression: `(() => {
                  const p = document.querySelector('.popup-container');
                  if (!p) return [];
                  const btns = [...p.querySelectorAll('button, [role="button"]')].filter(el => el.offsetParent !== null && el.innerText?.trim());
                  return btns.map(b => ({ text: b.innerText.trim().slice(0, 80), cls: b.className.slice(0, 60), isSkip: b.classList.contains('skip-question-button') }));
                })()`,
                returnByValue: true
              });
              const btns = answerButtons.result?.result?.value || [];

              if (btns.length === 0) { await sleep(2000); continue; }

              // Match to profile if we have structured question data
              let chosenText = null;
              let isSkip = false;

              if (currentQ) {
                const qtxt = (currentQ.question || '').toLowerCase();
                const ans = currentQ.answers || [];
                profileMatch:
                for (const [cat, kws] of Object.entries(PROFILE)) {
                  if (qtxt.includes(cat) || kws.some(k => qtxt.includes(k))) {
                    for (const a of ans) {
                      const at = a.text.toLowerCase();
                      if (kws.some(k => at.includes(k))) { chosenText = a.text; break profileMatch; }
                    }
                  }
                }
                if (!chosenText && currentQ.is_skippable && qAnswered < 5) { isSkip = true; }
                if (!chosenText && ans.length > 0) chosenText = ans[0].text;
              }

              // If no structured data, match against button text directly
              if (!chosenText && !isSkip) {
                const bodyText = await cmd('Runtime.evaluate', { expression: `document.querySelector('.popup-content')?.innerText?.toLowerCase() || ''`, returnByValue: true });
                const qt = (bodyText.result?.result?.value || '').toLowerCase();
                for (const [cat, kws] of Object.entries(PROFILE)) {
                  if (qt.includes(cat) || kws.some(k => qt.includes(k))) {
                    for (const b of btns) {
                      if (!b.isSkip && kws.some(k => b.text.toLowerCase().includes(k))) { chosenText = b.text; break; }
                    }
                    if (chosenText) break;
                  }
                }
                if (!chosenText) {
                  const firstNonSkip = btns.find(b => !b.isSkip);
                  if (firstNonSkip) chosenText = firstNonSkip.text;
                }
              }

              if (isSkip) {
                log(`  Q${qAnswered + 1}: skip`);
                await cmd('Runtime.evaluate', { expression: `document.querySelector('.skip-question-button')?.click()`, returnByValue: true });
              } else if (chosenText) {
                log(`  Q${qAnswered + 1}: "${(currentQ?.question || '').slice(0, 25)}" → "${chosenText.slice(0, 30)}"`);
                await cmd('Runtime.evaluate', {
                  expression: `(() => {
                    const p = document.querySelector('.popup-container');
                    if (!p) return;
                    const btns = [...p.querySelectorAll('button, [role="button"]')].filter(el => el.offsetParent !== null && el.innerText?.trim());
                    for (const b of btns) {
                      if (b.innerText.trim() === '${chosenText.replace(/'/g, "\\'")}') { b.click(); return; }
                    }
                    for (const b of btns) {
                      if (b.innerText.trim().startsWith('${chosenText.replace(/'/g, "\\'").slice(0, 30)}')) { b.click(); return; }
                    }
                    for (const b of btns) { if (!b.classList.contains('skip-question-button')) { b.click(); return; } }
                  })()`,
                  returnByValue: true
                });
              } else {
                // Click first non-skip button
                await cmd('Runtime.evaluate', {
                  expression: `(() => { const b = document.querySelector('.popup-container button:not(.skip-question-button)'); if (b && b.offsetParent !== null) b.click(); })()`,
                  returnByValue: true
                });
              }

              await sleep(500);

              // Click Next / submit button in modal
              await cmd('Runtime.evaluate', {
                expression: `(() => { const p = document.querySelector('.popup-container'); if (!p) return; const b = p.querySelector('.popup-button, button:not(.skip-question-button)'); if (b && b.offsetParent !== null) b.click(); })()`,
                returnByValue: true
              });

              qAnswered++;
              await sleep(2000);
            }

            if (!result) result = 'completed';

            // Main survey loop
            if (result === 'completed') {
              for (let sq = 0; sq < 35; sq++) {
                await sleep(2000);
                const ss = await cmd('Runtime.evaluate', {
                  expression: `(() => {
                    const t = document.body.innerText.toLowerCase();
                    if (t.includes('thank you') || t.includes('congratulations') || t.includes('you earned')) return 'done';
                    if (t.includes('not a good match') || t.includes('disqualif') || t.includes('not eligible') || t.includes('unfortunately')) return 'scr';
                    const r = [...document.querySelectorAll('input[type="radio"]')].filter(x => x.offsetParent !== null);
                    if (r.length > 0) return 'radio';
                    const b = [...document.querySelectorAll('button')].filter(x => x.offsetParent !== null);
                    if (b.some(x => { const t = (x.innerText || '').toLowerCase(); return t.includes('next') || t.includes('continue') || t.includes('submit') || t === '→'; })) return 'btn';
                    if (r.length === 0 && b.length === 0 && t.length > 100) return 'done';
                    return 'wait';
                  })()`,
                  returnByValue: true
                });
                const st = ss.result?.result?.value || 'wait';
                if (st === 'done') { result = 'completed'; break; }
                if (st === 'scr') { result = 'screened'; break; }

                if (st === 'radio') {
                  await cmd('Runtime.evaluate', {
                    expression: `(() => {
                      const r = [...document.querySelectorAll('input[type="radio"]')].filter(x => x.offsetParent !== null);
                      if (!r.length) return;
                      const body = document.body.innerText.toLowerCase();
                      const __P = ${PROFILE_JS};
                      let found = false;
                      for (const cat of Object.keys(__P)) {
                        const kws = __P[cat];
                        if (!body.includes(cat) && !kws.some(k => body.includes(k))) continue;
                        for (const x of r) {
                          const l = (document.querySelector('label[for="' + x.id + '"]')?.innerText || x.closest('label')?.innerText || '').toLowerCase();
                          if (l && kws.some(k => l.includes(k))) { x.click(); found = true; break; }
                        }
                        if (found) break;
                      }
                      if (!found && r[0]) r[0].click();
                      // Click Next
                      const b = [...document.querySelectorAll('button')].filter(x => x.offsetParent !== null);
                      for (const x of b) { const t = (x.innerText || '').toLowerCase(); if (t.includes('next') || t.includes('continue') || t.includes('submit') || t === '→') { x.click(); return; } }
                    })()`,
                    returnByValue: true
                  });
                  qAnswered++;
                } else if (st === 'btn') {
                  await cmd('Runtime.evaluate', {
                    expression: `(() => { const b = [...document.querySelectorAll('button')].filter(x => x.offsetParent !== null); for (const x of b) { const t = (x.innerText || '').toLowerCase(); if (t.includes('next') || t.includes('continue') || t.includes('submit')) { x.click(); return; } } })()`,
                    returnByValue: true
                  });
                  await sleep(1500);
                } else {
                  await sleep(3000);
                }
              }
            }

            // Balance check
            await sleep(3000);
            const balAfter = await readBalance(ws, cmd);

            if (result === 'completed') {
              if (balBefore !== null && balAfter !== null && balAfter > balBefore) {
                const d = balAfter - balBefore;
                log(`  ✓ PAID! $${balBefore.toFixed(2)} → $${balAfter.toFixed(2)} (+$${d.toFixed(2)})`);
                state.payments += d;
              } else {
                log(`  ✓ COMPLETED! (balance: ${balAfter !== null ? '$' + balAfter.toFixed(2) : 'N/A'})`);
              }
              done++;
            } else if (result === 'screened') {
              log(`  ○ Screened (${qAnswered} Qs)`);
              scr++;
            } else {
              log(`  ✗ Failed (${qAnswered} Qs)`);
              fail++;
            }

            // Return to surveys
            await cmd('Runtime.evaluate', { expression: `document.querySelector('[data-test-id="surveys-nav"]')?.click()`, returnByValue: true });
            await sleep(2000);
          } catch (e) {
            log(`  ✗ Err: ${e.message.substring(0, 120)}`);
            fail++;
          }
          await sleep(2000 + Math.random() * 4000);
        }

        log(`\n=== Session: ${done} done, ${scr} scr, ${fail} fail ===`);
        state.completed += done; state.screened += scr; state.failed += fail; state.sessions++;
        saveState(state);
        resolve({ done, scr, fail });
      } catch (e) {
        log(`CRIT: ${e.message.substring(0, 120)}`);
        resolve({ done: 0, scr: 0, fail: count });
      }
      try { ws.close(); } catch {}
    });
    ws.on('error', e => { log(`WS err: ${e.message.substring(0, 60)}`); resolve({ done: 0, scr: 0, fail: count }); });
  });
}

async function main() {
  log('='.repeat(60));
  log('TOPSURVEYS AGENT v6 — DOM-click qualification');
  log('='.repeat(60));

  const st = loadState();
  log(`State: ${st.completed} done, ${st.screened} scr, $${st.payments.toFixed(2)} earned`);

  let sn = st.sessions;
  while (true) {
    sn++;
    const cnt = process.argv.includes('--single-task') ? 1 : 8 + Math.floor(Math.random() * 5);
    log(`\n${'='.repeat(60)}\nSESSION ${sn} — ${cnt} tasks\n${'='.repeat(60)}`);

    try {
      const u = await getCDPUrl();
      await runSession(u, cnt, st);
    } catch (e) {
      log(`Sess err: ${e.message.substring(0, 80)} — retry 60s`);
      await sleep(60000);
      continue;
    }

    const pm = cnt <= 1 ? 0 : 15 + Math.floor(Math.random() * 31);
    log(`\nPause ${pm}min... Total: ${st.completed} done, $${st.payments.toFixed(2)}`);
    if (pm > 0) await sleep(pm * 60000);
  }
}

main().catch(e => { log(`FATAL: ${e.message}`); process.exit(1); });
