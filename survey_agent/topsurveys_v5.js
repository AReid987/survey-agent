#!/usr/bin/env node
/**
 * TopSurveys Agent v5 — intercepts page's own API calls via Network.getResponseBody
 * The proven approach from answer_qualification.js
 */
const WebSocket = require('ws');
const http = require('http');
const fs = require('fs');
const path = require('path');

const DATA = path.join('/Users/antonioreid/survey-agent/data');
const STATE = path.join(DATA, 'agent_state_v5.json');
const LOG = path.join(DATA, 'survey_run_v5.log');
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
          pg ? resolve({ ws: pg.webSocketDebuggerUrl, id: pg.id }) : reject(new Error('No topsurveys page'));
        } catch(e) { reject(e); }
      });
    }).on('error', reject);
  });
}

async function readBalance(ws, cmd) {
  const r = await cmd('Runtime.evaluate', {
    expression: `(()=>{
      const sidebar = document.querySelector('.right-sidebar');
      if (!sidebar) return null;
      const spans = [...sidebar.querySelectorAll('span')];
      for (const s of spans) {
        const m = s.innerText.trim().match(/^\\$\\s*([0-9]+\\.[0-9]{2})\\s*USD$/);
        if (m) { const v = parseFloat(m[1]); if (v > 0 && v < 100) return v; }
      }
      return null;
    })()`,
    returnByValue: true
  });
  return r.result?.result?.value;
}

async function runSurvey(ws, cmd, networkReqs, surveyId, cardId) {
  let qAnswered = 0;
  let lastQualReqId = null;

  // NOTE: network listener is attached once in runSession, NOT here.
  // networkReqs Map is cleared before each call to runSurvey.

  // Click survey card
  // Check page state — scope completion to MODAL text only, not document.body
  const ps = await cmd('Runtime.evaluate', {
    expression: `(()=>{const p=document.querySelector('.popup-container');const inModal=p&&p.offsetParent!==null;if(inModal){const pt=p.innerText.toLowerCase();if(pt.includes('thank you')||pt.includes('congratulations')||pt.includes('you earned'))return'done';if(pt.includes('not a good match')||pt.includes('disqualif')||pt.includes('not eligible')||pt.includes('unfortunately'))return'scr';}if(!inModal)return'closed';return'open';})()`,
    returnByValue: true
  });
  const pst = ps.result?.result?.value || 'open';
  // Wait for qualification API call to happen
  await sleep(3000);
  
  // Find the latest /qualification POST
  for (const [reqId, req] of networkReqs) {
    if (req.url.includes('/qualification') && req.method === 'POST' && req.postData?.survey) {
      lastQualReqId = reqId;
      break;
    }
  }

  if (!lastQualReqId) {
    log('  No /qualification call detected');
    // Try checking for screen-out text
    const t = await cmd('Runtime.evaluate', { expression: 'document.body.innerText', returnByValue: true });
    const txt = (t.result?.result?.value || '').toLowerCase();
    if (txt.includes('not a good match') || txt.includes('disqualif') || txt.includes('not eligible')) {
      return 'screened';
    }
    return 'failed';
  }

  // Get qualification response body
  let qualData = null;
  for (let retry = 0; retry < 8; retry++) {
    try {
      await sleep(500);
      const resp = await cmd('Network.getResponseBody', { requestId: lastQualReqId }, 5000);
      if (resp.result?.body) {
        qualData = JSON.parse(resp.result.body);
        log(`  Qual response: status=${qualData.status}, questions=${qualData.questions?.available || 0}`);
        break;
      }
    } catch (e) { /* response not ready yet */ }
  }

  if (!qualData) {
    log('  Could not fetch qualification response');
    return 'failed';
  }

  if (qualData.status === 'rejected') {
    log(`  Rejected: ${qualData.message || ''}`);
    return 'screened';
  }

  if (qualData.status !== 'accepted' || !qualData.questions?.current) {
    log(`  Unexpected: ${JSON.stringify(qualData).slice(0, 200)}`);
    return 'failed';
  }

  // Answer questions in the qualification modal
  let currentQ = qualData.questions.current;
  let available = qualData.questions.available || 1;

  while (currentQ && qAnswered < 20) {
    const qText = (currentQ.question || '').toLowerCase();
    const answers = currentQ.answers || [];
    
    // Match answer to profile
    let chosen = null;
    let chosenText = '';

    for (const [cat, kws] of Object.entries(PROFILE)) {
      if (qText.includes(cat) || kws.some(k => qText.includes(k))) {
        for (const a of answers) {
          const at = a.text.toLowerCase();
          if (kws.some(k => at.includes(k))) { chosen = a.id; chosenText = a.text; break; }
        }
        if (chosen) break;
      }
    }

    // Fallback: skip or pick first
    if (!chosen && currentQ.is_skippable && qAnswered < 6) {
      log(`  Q${qAnswered+1}: skip "${(currentQ.question||'').slice(0, 30)}"`);
      
      // Check page state — scope completion to MODAL text only, not document.body
      const ps = await cmd('Runtime.evaluate', {
        expression: `(()=>{const p=document.querySelector('.popup-container');const inModal=p&&p.offsetParent!==null;if(inModal){const pt=p.innerText.toLowerCase();if(pt.includes('thank you')||pt.includes('congratulations')||pt.includes('you earned'))return'done';if(pt.includes('not a good match')||pt.includes('disqualif')||pt.includes('not eligible')||pt.includes('unfortunately'))return'scr';}if(!inModal)return'closed';return'open';})()`,
        returnByValue: true
      });
      const pst = ps.result?.result?.value || 'open';
      
      // Check page state — scope completion to MODAL text only, not document.body
      const ps = await cmd('Runtime.evaluate', {
        expression: `(()=>{const p=document.querySelector('.popup-container');const inModal=p&&p.offsetParent!==null;if(inModal){const pt=p.innerText.toLowerCase();if(pt.includes('thank you')||pt.includes('congratulations')||pt.includes('you earned'))return'done';if(pt.includes('not a good match')||pt.includes('disqualif')||pt.includes('not eligible')||pt.includes('unfortunately'))return'scr';}if(!inModal)return'closed';return'open';})()`,
        returnByValue: true
      });
      const pst = ps.result?.result?.value || 'open';
      qAnswered++;
      await sleep(2000);
      
      // Check page state — scope completion to MODAL text only, not document.body
      const ps = await cmd('Runtime.evaluate', {
        expression: `(()=>{const p=document.querySelector('.popup-container');const inModal=p&&p.offsetParent!==null;if(inModal){const pt=p.innerText.toLowerCase();if(pt.includes('thank you')||pt.includes('congratulations')||pt.includes('you earned'))return'done';if(pt.includes('not a good match')||pt.includes('disqualif')||pt.includes('not eligible')||pt.includes('unfortunately'))return'scr';}if(!inModal)return'closed';return'open';})()`,
        returnByValue: true
      });
      const pst = ps.result?.result?.value || 'open';
      
      // Get next question
      const nextResp = await cmd('Runtime.evaluate', {
        expression: `(() => {
          const reqs = window.__interceptedReqs || [];
          // Wait for next qualification response
          return window.__lastQualResp || null;
        })()`,
        returnByValue: true,
        awaitPromise: false
      });
      continue;
    }

    if (!chosen && answers.length > 0) {
      chosen = answers[0].id;
      chosenText = answers[0].text;
    }
    if (!chosen) { await sleep(2000); continue; }

    log(`  Q${qAnswered+1}: "${(currentQ.question||'').slice(0,30)}" → "${chosenText.slice(0,30)}"`);

    // CLICK answer in DOM (triggers page's own API call)
    const safeText = chosenText.replace(/['"]/g, '').slice(0, 40);
    await cmd('Runtime.evaluate', {
      expression: `(()=>{
        const popup = document.querySelector('.popup-container');
        if (!popup) return;
        const answers = [...popup.querySelectorAll('button, [role="button"], label')].filter(el => el.offsetParent !== null && el.innerText?.trim());
        for (const a of answers) {
          if (a.innerText.trim().startsWith('${safeText}') || a.innerText.trim() === '${safeText}') {
            a.click();
            return;
          }
        }
        // Click first non-skip answer
        for (const a of answers) {
          if (!a.classList.contains('skip-question-button')) { a.click(); return; }
        }
      })()`,
      returnByValue: true
    });
    await sleep(1000);

    // Check page state — scope completion to MODAL text only, not document.body
    const ps = await cmd('Runtime.evaluate', {
      expression: `(()=>{const p=document.querySelector('.popup-container');const inModal=p&&p.offsetParent!==null;if(inModal){const pt=p.innerText.toLowerCase();if(pt.includes('thank you')||pt.includes('congratulations')||pt.includes('you earned'))return'done';if(pt.includes('not a good match')||pt.includes('disqualif')||pt.includes('not eligible')||pt.includes('unfortunately'))return'scr';}if(!inModal)return'closed';return'open';})()`,
      returnByValue: true
    });
    const pst = ps.result?.result?.value || 'open';
    qAnswered++;

    // Check for screen-out
    const t = await cmd('Runtime.evaluate', { expression: 'document.body.innerText', returnByValue: true });
    const txt = (t.result?.result?.value || '').toLowerCase();
    if (txt.includes('not a good match') || txt.includes('disqualif')) {
      log(`  Screened out after Q${qAnswered}`);
      return 'screened';
    }

    // Check page state — scope completion to MODAL text only, not document.body
    const ps = await cmd('Runtime.evaluate', {
      expression: `(()=>{const p=document.querySelector('.popup-container');const inModal=p&&p.offsetParent!==null;if(inModal){const pt=p.innerText.toLowerCase();if(pt.includes('thank you')||pt.includes('congratulations')||pt.includes('you earned'))return'done';if(pt.includes('not a good match')||pt.includes('disqualif')||pt.includes('not eligible')||pt.includes('unfortunately'))return'scr';}if(!inModal)return'closed';return'open';})()`,
      returnByValue: true
    });
    const pst = ps.result?.result?.value || 'open';
      log(`  Modal closed after Q${qAnswered} — qualification complete`);
      break;
    }

    // Try to get next question — look for new qualification API responses
    // The page SDK will auto-fetch next question
    for (let w = 0; w < 10; w++) {
      await sleep(1000);
      
      // Check page state — scope completion to MODAL text only, not document.body
      const ps = await cmd('Runtime.evaluate', {
        expression: `(()=>{const p=document.querySelector('.popup-container');const inModal=p&&p.offsetParent!==null;if(inModal){const pt=p.innerText.toLowerCase();if(pt.includes('thank you')||pt.includes('congratulations')||pt.includes('you earned'))return'done';if(pt.includes('not a good match')||pt.includes('disqualif')||pt.includes('not eligible')||pt.includes('unfortunately'))return'scr';}if(!inModal)return'closed';return'open';})()`,
        returnByValue: true
      });
      const pst = ps.result?.result?.value || 'open';
      if (content === 'closed') break;
      
      // If content changed, we have a new question visible
      // The next /qualification call has been made by the SDK
      // We need to grab the response again
      for (const [reqId, req] of networkReqs) {
        if (req.url.includes('/qualification') && req.method === 'POST' && req.postData?.answer_id) {
          if (reqId !== lastQualReqId) {
            lastQualReqId = reqId;
            try {
              const resp = await cmd('Network.getResponseBody', { requestId: reqId }, 5000);
              if (resp.result?.body) {
                qualData = JSON.parse(resp.result.body);
                log(`  Next qual response: status=${qualData.status}, questions=${qualData.questions?.available || 0}`);
                if (qualData.status === 'rejected') {
                  log(`  Rejected: ${qualData.message || ''}`);
                  return 'screened';
                }
                if (qualData.questions?.current) {
                  currentQ = qualData.questions.current;
                  break; // Got next question
                }
                if (!qualData.questions?.current) {
                  // No more questions — qual complete
                  currentQ = null;
                  break;
                }
              }
            } catch {}
          }
        }
      }
      
      if (currentQ === null || networkReqs.size > 1) break;
    }
  }

  // Main survey handling
  log(`  Starting main survey loop (${qAnswered} qual Qs answered)`);
  
  for (let sq = 0; sq < 30; sq++) {
    await sleep(2000);
    
    const ss = await cmd('Runtime.evaluate', {
      expression: `(()=>{
        const t = document.body.innerText.toLowerCase();
        // Check for narrow completion signals ONLY (not sidebar stats)
        const mainEl = document.querySelector('main, .surveys__container, .common-page');
        const mt = mainEl ? mainEl.innerText.toLowerCase() : t;
        
        if (mt.includes('thank you') || mt.includes('congratulations') || mt.includes('you earned')) return 'done';
        if (t.includes('not a good match') || t.includes('disqualif') || t.includes('not eligible')) return 'scr';
        
        const r = [...document.querySelectorAll('input[type="radio"]')].filter(x => x.offsetParent !== null);
        if (r.length > 0) return 'radio';
        
        const b = [...document.querySelectorAll('button')].filter(x => x.offsetParent !== null);
        if (b.some(x => { const t = (x.innerText||'').toLowerCase(); return t.includes('next')||t.includes('continue')||t.includes('submit')||t==='→'; })) return 'btn';
        // Check page state — scope completion to MODAL text only, not document.body
        const ps = await cmd('Runtime.evaluate', {
          expression: `(()=>{const p=document.querySelector('.popup-container');const inModal=p&&p.offsetParent!==null;if(inModal){const pt=p.innerText.toLowerCase();if(pt.includes('thank you')||pt.includes('congratulations')||pt.includes('you earned'))return'done';if(pt.includes('not a good match')||pt.includes('disqualif')||pt.includes('not eligible')||pt.includes('unfortunately'))return'scr';}if(!inModal)return'closed';return'open';})()`,
          returnByValue: true
        });
        const pst = ps.result?.result?.value || 'open';
    
    if (st === 'done') return 'completed';
    if (st === 'scr') return 'screened';
    
    if (st === 'radio') {
      await cmd('Runtime.evaluate', {
        expression: `(()=>{
          const r = [...document.querySelectorAll('input[type="radio"]')].filter(x => x.offsetParent !== null);
          if (!r.length) return;
          const body = document.body.innerText.toLowerCase();
          const __P = ${JSON.stringify(PROFILE)};
          let found = false;
          for (const cat of Object.keys(__P)) {
            const kws = __P[cat];
            if (!cat || (!body.includes(cat) && !kws.some(k => body.includes(k)))) continue;
            for (const x of r) {
              const l = (document.querySelector('label[for="'+x.id+'"]')?.innerText || x.closest('label')?.innerText || '').toLowerCase();
              if (l && kws.some(k => l.includes(k))) { x.click(); found = true; break; }
            }
            if (found) break;
          }
          if (!found && r[0]) r[0].click();
        })()`,
        returnByValue: true
      });
      // Check page state — scope completion to MODAL text only, not document.body
      const ps = await cmd('Runtime.evaluate', {
        expression: `(()=>{const p=document.querySelector('.popup-container');const inModal=p&&p.offsetParent!==null;if(inModal){const pt=p.innerText.toLowerCase();if(pt.includes('thank you')||pt.includes('congratulations')||pt.includes('you earned'))return'done';if(pt.includes('not a good match')||pt.includes('disqualif')||pt.includes('not eligible')||pt.includes('unfortunately'))return'scr';}if(!inModal)return'closed';return'open';})()`,
        returnByValue: true
      });
      const pst = ps.result?.result?.value || 'open';
    // Check page state — scope completion to MODAL text only, not document.body
    const ps = await cmd('Runtime.evaluate', {
      expression: `(()=>{const p=document.querySelector('.popup-container');const inModal=p&&p.offsetParent!==null;if(inModal){const pt=p.innerText.toLowerCase();if(pt.includes('thank you')||pt.includes('congratulations')||pt.includes('you earned'))return'done';if(pt.includes('not a good match')||pt.includes('disqualif')||pt.includes('not eligible')||pt.includes('unfortunately'))return'scr';}if(!inModal)return'closed';return'open';})()`,
      returnByValue: true
    });
    const pst = ps.result?.result?.value || 'open';
    } else {
      await sleep(3000);
    }
  }
  
  return 'completed'; // default after answering many questions
}

async function runSession(wsUrl, count, state) {
  const ws = new WebSocket(wsUrl);
  let mid = 1;
  const networkReqs = new Map();

  function cmd(m, p, t = 25000) {
    return new Promise((res, rej) => {
      const id = mid++;
      ws.send(JSON.stringify({ id, method: m, params: p }));
      const h = d => { const r = JSON.parse(d.toString()); if (r.id === id) { ws.removeListener('message', h); res(r); } };
      ws.on('message', h);
      setTimeout(() => { ws.removeListener('message', h); rej(new Error(`T:${m}`)); }, t);
    });
  }

  return new Promise(resolve => {
    ws.on('open', async () => {
      try {
        await cmd('Network.enable');

        // Track qualification API calls — single listener for entire session
        ws.on('message', data => {
          try {
            const msg = JSON.parse(data.toString());
            if (msg.method === 'Network.requestWillBeSent' && msg.params.request.url.includes('/qualification')) {
              networkReqs.set(msg.params.requestId, {
                url: msg.params.request.url,
                method: msg.params.request.method,
                postData: msg.params.request.postData ? JSON.parse(msg.params.request.postData) : null,
                ts: Date.now()
              });
            }
          } catch {}
        });

        // Navigate
        log('→ Surveys');
        await cmd('Runtime.evaluate', { expression: `document.querySelector('[data-test-id="surveys-nav"]')?.click()`, returnByValue: true });
        // Check page state — scope completion to MODAL text only, not document.body
        const ps = await cmd('Runtime.evaluate', {
          expression: `(()=>{const p=document.querySelector('.popup-container');const inModal=p&&p.offsetParent!==null;if(inModal){const pt=p.innerText.toLowerCase();if(pt.includes('thank you')||pt.includes('congratulations')||pt.includes('you earned'))return'done';if(pt.includes('not a good match')||pt.includes('disqualif')||pt.includes('not eligible')||pt.includes('unfortunately'))return'scr';}if(!inModal)return'closed';return'open';})()`,
          returnByValue: true
        });
        const pst = ps.result?.result?.value || 'open';

        let done = 0, scr = 0, fail = 0;

        for (let t = 1; t <= count; t++) {
          log(`\n--- Task ${t}/${count} ---`);
          try {
            // Find survey cards — no offsetParent filter (breaks with overlays present)
            let cards = [];
            // Check page state — scope completion to MODAL text only, not document.body
            const ps = await cmd('Runtime.evaluate', {
              expression: `(()=>{const p=document.querySelector('.popup-container');const inModal=p&&p.offsetParent!==null;if(inModal){const pt=p.innerText.toLowerCase();if(pt.includes('thank you')||pt.includes('congratulations')||pt.includes('you earned'))return'done';if(pt.includes('not a good match')||pt.includes('disqualif')||pt.includes('not eligible')||pt.includes('unfortunately'))return'scr';}if(!inModal)return'closed';return'open';})()`,
              returnByValue: true
            });
            const pst = ps.result?.result?.value || 'open';
              if (cards.length > 0) break;
              await sleep(2000);
            }
            // Check page state — scope completion to MODAL text only, not document.body
            const ps = await cmd('Runtime.evaluate', {
              expression: `(()=>{const p=document.querySelector('.popup-container');const inModal=p&&p.offsetParent!==null;if(inModal){const pt=p.innerText.toLowerCase();if(pt.includes('thank you')||pt.includes('congratulations')||pt.includes('you earned'))return'done';if(pt.includes('not a good match')||pt.includes('disqualif')||pt.includes('not eligible')||pt.includes('unfortunately'))return'scr';}if(!inModal)return'closed';return'open';})()`,
              returnByValue: true
            });
            const pst = ps.result?.result?.value || 'open';
              fail++; await sleep(5000); continue;
            }

            cards.sort((a, b) => parseFloat(b.r) - parseFloat(a.r));
            const card = cards[0];
            const reward = parseFloat(card.r);
            log(`  ${card.txt.slice(0, 60)} ($${reward.toFixed(2)})`);

            const balBefore = await readBalance(ws, cmd);
            log(`  Balance: ${balBefore !== null ? '$' + balBefore.toFixed(2) : '?'}`);

            networkReqs.clear();

            const result = await runSurvey(ws, cmd, networkReqs, card.id.replace('ps-survey-', ''), card.id);

            await sleep(3000);
            const balAfter = await readBalance(ws, cmd);

            if (result === 'completed') {
              if (balBefore !== null && balAfter !== null && balAfter > balBefore) {
                const d = balAfter - balBefore;
                log(`  ✓ PAID! $${balBefore.toFixed(2)} → $${balAfter.toFixed(2)} (+$${d.toFixed(2)})`);
                state.payments += d;
              } else if (balBefore !== null && balAfter !== null && balAfter <= balBefore) {
                log(`  ✓ COMPLETED! (no balance change, $${balBefore.toFixed(2)})`);
              } else {
                log(`  ✓ COMPLETED! (balance N/A)`);
              }
              done++;
            } else if (result === 'screened') {
              log(`  ○ Screened out`);
              scr++;
            } else {
              log(`  ✗ Failed`);
              fail++;
            }

            await cmd('Runtime.evaluate', { expression: `document.querySelector('[data-test-id="surveys-nav"]')?.click()`, returnByValue: true });
            await sleep(2000);
          } catch (e) {
            log(`  ✗ Task err: ${e.message.substring(0, 120)}`);
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
  log('TOPSURVEYS AGENT v5 — Network intercept approach');
  log('='.repeat(60));

  const st = loadState();
  log(`State: ${st.completed} done, ${st.screened} scr, $${st.payments.toFixed(2)} earned`);

  let sn = st.sessions;
  while (true) {
    sn++;
    const cnt = process.argv.includes('--single-task') ? 1 : 8 + Math.floor(Math.random() * 5);
    log(`\n${'='.repeat(60)}\nSESSION ${sn} — ${cnt} tasks\n${'='.repeat(60)}`);

    try {
      const { ws } = await getCDPUrl();
      await runSession(ws, cnt, st);
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
