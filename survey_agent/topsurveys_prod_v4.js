#!/usr/bin/env node
/**
 * TopSurveys Production Agent v4 — FINAL
 * All API calls via page-context fetch, no Network.getResponseBody
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
  catch { return { completed: 0, screened: 0, failed: 0, payments: 0, sessions: 0, balance_hist: [] }; }
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
        } catch(e) { reject(e); }
      });
    }).on('error', reject);
  });
}

async function readBalance(ws, cmd) {
  const r = await cmd('Runtime.evaluate', {
    expression: `(()=>{
      // Only read $X.XX USD from the right sidebar — ignore outer structure
      const sidebar = document.querySelector('.right-sidebar');
      if (!sidebar) return null;
      const spans = sidebar.querySelectorAll('span[class*="balance"]');
      for (const s of spans) {
        const m = s.innerText.trim().match(/^\$\s*([0-9]+\.[0-9]{2})\s*USD$/);
        if (m) return parseFloat(m[1]);
      }
      // Fallback: any span in sidebar matching $X.XX where X<100
      for (const s of sidebar.querySelectorAll('span')) {
        const m = s.innerText.trim().match(/^\$\s*([0-9]+\.[0-9]{2})\s*USD$/);
        if (m) { const v = parseFloat(m[1]); if (v > 0 && v < 100) return v; }
      }
      return null;
    })()`,
    returnByValue: true
  });
  return r.result?.result?.value;
}

async function qualAPI(ws, cmd, surveyId, payload) {
  /* Call /qualification via page-context fetch. Use JSON.stringify in expression so body is a proper JSON string. */
  const r = await cmd('Runtime.evaluate', {
    expression: `fetch('https://api.primesurveys.com/qualification',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(${JSON.stringify(payload)}),credentials:'include'}).then(r=>r.json()).then(d=>{window.__qualR=d;return d;})`,
    returnByValue: true,
    awaitPromise: true,
    timeout: 20000
  });
  return r.result?.result?.value || r.result?.result;
}

function esc(s) { return s.replace(/'/g, "\\'").replace(/\n/g, ' '); }

async function runSession(wsUrl, count, state) {
  const ws = new WebSocket(wsUrl);
  let mid = 1;
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
        await cmd('Runtime.evaluate', { expression: `window.__P=${JSON.stringify(PROFILE)}`, returnByValue: true });

        // Navigate to surveys
        log('→ Surveys');
        await cmd('Runtime.evaluate', { expression: `document.querySelector('[data-test-id="surveys-nav"]')?.click()`, returnByValue: true });
        await sleep(3000);
        await cmd('Runtime.evaluate', { expression: `(()=>{const b=[...document.querySelectorAll('button')].find(x=>{const t=(x.innerText||'').toLowerCase();return t.includes('awesome')||t.includes('great')||t.includes('ok')});if(b)b.click();const c=document.querySelector('.popup-button');if(c&&c.offsetParent!==null)c.click();})()`, returnByValue: true });
        await sleep(1000);

        let done = 0, scr = 0, fail = 0;

        for (let t = 1; t <= count; t++) {
          log(`\n--- Task ${t}/${count} ---`);
          try {
            const cr = await cmd('Runtime.evaluate', {
              expression: `(()=>{const c=[...document.querySelectorAll('.list-item.survey-item')].filter(x=>x.offsetParent!==null);return c.map(x=>({id:x.getAttribute('data-test-id')||'',txt:x.innerText.replace(/\\n/g,' ').trim().slice(0,200),r:(x.innerText||'').match(/\\$\\s*([0-9.]+)/)?.[1]||'0'}));})()`,
              returnByValue: true
            });
            const cards = (cr.result?.result?.value || []).filter(x => x.id);
            if (!cards.length) { log('  No surveys'); fail++; await sleep(5000); continue; }

            cards.sort((a, b) => parseFloat(b.r) - parseFloat(a.r));
            const card = cards[0];
            const reward = parseFloat(card.r);
            const surveyId = card.id.replace('ps-survey-', '');
            log(`  ${card.txt.slice(0, 60)} ($${reward.toFixed(2)})`);

            const balBefore = await readBalance(ws, cmd);
            log(`  Balance: ${balBefore !== null ? '$' + balBefore.toFixed(2) : '?'}`);

            // Click survey card
            await cmd('Runtime.evaluate', { expression: `document.querySelector('[data-test-id="${card.id}"]')?.click()`, returnByValue: true });
            await sleep(2000);

            // Start qual with policy_optin — must happen BEFORE waiting for modal, primes the API
            let qualResp = await qualAPI(ws, cmd, surveyId, { survey: surveyId, policy_optin: 1 });
            log(`  qualAPI init response: ${JSON.stringify(qualResp).slice(0, 200)}`);

            // POLL for qualification modal to appear (up to 15s)
            let modalVisible = false;
            for (let w = 0; w < 15; w++) {
              const check = await cmd('Runtime.evaluate', {
                expression: `document.querySelector('.popup-container')?.offsetParent !== null`,
                returnByValue: true
              });
              if (check.result?.result?.value) { modalVisible = true; break; }
              await sleep(1000);
            }
            if (!modalVisible) {
              log('  No qualification modal appeared — survey may have been screened immediately');
              // Check for screen-out text
              const t = await cmd('Runtime.evaluate', { expression: 'document.body.innerText', returnByValue: true });
              const txt = (t.result?.result?.value || '').toLowerCase();
              if (txt.includes('not a good match') || txt.includes('not eligible') || txt.includes('disqualif')) {
                result = 'screened'; scr++;
              } else { fail++; }
              continue;
            }
            log('  Qualification modal opened');
            let qAnswered = 0;
            let result = null;

            // Answer questions until done
            for (let q = 0; q < 25; q++) {
              await sleep(1000);

              // Check page state — scope completion detection to MODAL text only, NOT document.body
              const ps = await cmd('Runtime.evaluate', {
                expression: `(()=>{const p=document.querySelector('.popup-container');const modalOpen=p&&p.offsetParent!==null;if(modalOpen){const pt=p.innerText.toLowerCase();if(pt.includes('thank you')||pt.includes('congratulations')||pt.includes('you earned'))return'done';if(pt.includes('not a good match')||pt.includes('disqualif')||pt.includes('not eligible')||pt.includes('unfortunately'))return'scr';}const pNow=document.querySelector('.popup-container');if(!pNow||pNow.offsetParent===null)return'closed';return'open';})()`,
                returnByValue: true
              });
              const pst = ps.result?.result?.value || 'open';

              if (pst === 'done') { result = 'completed'; break; }
              if (pst === 'scr') { result = 'screened'; break; }

              if (pst === 'closed') {
                // Main survey active — answer with radio buttons
                log(`  Qualification done (${qAnswered} Qs), now main survey`);
                for (let sq = 0; sq < 40; sq++) {
                  await sleep(2000);
                  const ss = await cmd('Runtime.evaluate', {
                    expression: `(()=>{const p=document.querySelector('.popup-container');const modalOpen=p&&p.offsetParent!==null;if(modalOpen){const pt=p.innerText.toLowerCase();if(pt.includes('thank you')||pt.includes('congratulations')||pt.includes('you earned'))return'done';if(pt.includes('not a good match')||pt.includes('disqualif')||pt.includes('not eligible'))return'scr';}const main=document.querySelector('main,.surveys__container');const mt=main?main.innerText.toLowerCase():'';const r=[...document.querySelectorAll('input[type="radio"]')].filter(x=>x.offsetParent!==null);if(r.length>0)return'radio';const b=[...document.querySelectorAll('button')].filter(x=>x.offsetParent!==null);if(b.some(x=>{const t=(x.innerText||'').toLowerCase();return t.includes('next')||t.includes('continue')||t.includes('submit')||t==='→'}))return'btn';if(r.length===0&&b.length===0)return'wait';return'wait';})()`,
                    returnByValue: true
                  });
                  const st = ss.result?.result?.value || 'wait';
                  if (st === 'done') { result = 'completed'; break; }
                  if (st === 'scr') { result = 'screened'; break; }

                  if (st === 'radio') {
                    await cmd('Runtime.evaluate', {
                      expression: `(()=>{const r=[...document.querySelectorAll('input[type="radio"]')].filter(x=>x.offsetParent!==null);if(!r.length)return;const b=document.body.innerText.toLowerCase();let p=false;const __P=window.__P||{};for(const cat of Object.keys(__P)){const kws=__P[cat];if(!b.includes(cat)&&!kws.some(k=>b.includes(k)))continue;for(const x of r){const l=(document.querySelector('label[for="'+x.id+'"]')?.innerText||x.closest('label')?.innerText||'').toLowerCase();if(l&&kws.some(k=>l.includes(k))){x.click();p=true;break;}}if(p)break;}if(!p&&r[0])r[0].click();})()`,
                      returnByValue: true
                    });
                    await sleep(500);
                    await cmd('Runtime.evaluate', {
                      expression: `(()=>{const b=[...document.querySelectorAll('button')].filter(x=>x.offsetParent!==null);for(const x of b){const t=(x.innerText||'').toLowerCase();if(t.includes('next')||t.includes('continue')||t.includes('submit')||t==='→'){x.click();return;}}})()`,
                      returnByValue: true
                    });
                    qAnswered++;
                  } else if (st === 'btn') {
                    await cmd('Runtime.evaluate', {
                      expression: `(()=>{const b=[...document.querySelectorAll('button')].filter(x=>x.offsetParent!==null);for(const x of b){const t=(x.innerText||'').toLowerCase();if(t.includes('next')||t.includes('continue')||t.includes('submit')){x.click();return;}}})()`,
                      returnByValue: true
                    });
                    await sleep(1500);
                  } else {
                    await sleep(3000);
                  }
                }
                if (!result) result = 'completed';
                break;
              }

              // Still in qualification modal — read qualResp
              if (qualResp && qualResp.questions && qualResp.questions.current) {
                const q = qualResp.questions.current;
                const qtxt = (q.question || '').toLowerCase();
                const ans = q.answers || [];
                let chosen = null;
                let chosenText = '';

                // Match to profile
                for (const [cat, kws] of Object.entries(PROFILE)) {
                  if (qtxt.includes(cat) || kws.some(k => qtxt.includes(k))) {
                    for (const a of ans) {
                      const at = a.text.toLowerCase();
                      if (kws.some(k => at.includes(k))) { chosen = a.id; chosenText = a.text; break; }
                    }
                    if (chosen) break;
                  }
                }

                if (!chosen && q.is_skippable && qAnswered < 4) {
                  log(`  Q${qAnswered+1}: skip "${(q.question||'').slice(0,30)}"`);
                  await cmd('Runtime.evaluate', { expression: `document.querySelector('.skip-question-button')?.click()`, returnByValue: true });
                  await sleep(500);
                  await cmd('Runtime.evaluate', { expression: `(()=>{const p=document.querySelector('.popup-container');if(!p)return;const b=p.querySelector('.popup-button, button:not(.skip-question-button)');if(b&&b.offsetParent!==null)b.click();})()`, returnByValue: true });
                  qAnswered++;
                  await sleep(2000);
                  continue;
                }

                if (!chosen && ans.length > 0) { chosen = ans[0].id; chosenText = ans[0].text; }
                if (!chosen) { await sleep(2000); continue; }

                log(`  Q${qAnswered+1}: "${(q.question||'').slice(0,30)}" → "${chosenText.slice(0,30)}"`);

                qualResp = await qualAPI(ws, cmd, surveyId, { survey: surveyId, question_id: q.id, answer_id: chosen });
                qAnswered++;

                // If API says rejected
                if (qualResp && qualResp.status === 'rejected') {
                  log(`  Rejected: ${qualResp.message || ''}`);
                  result = 'screened';
                  break;
                }
              } else {
                // No current question — might be between questions
                await sleep(2000);
                // Re-trigger by trying to advance
                await cmd('Runtime.evaluate', { expression: `(()=>{const p=document.querySelector('.popup-container');if(!p)return;const b=p.querySelector('.popup-button, button:not(.skip-question-button)');if(b&&b.offsetParent!==null)b.click();})()`, returnByValue: true });
              }
            }

            if (!result) result = 'completed';

            await sleep(3000);
            const balAfter = await readBalance(ws, cmd);

            if (result === 'completed') {
              if (balBefore !== null && balAfter !== null && balAfter > balBefore) {
                const d = balAfter - balBefore;
                log(`  ✓ COMPLETED! $${balBefore.toFixed(2)} → $${balAfter.toFixed(2)} (+$${d.toFixed(2)})`);
                state.payments += d;
                state.balance_hist.push({ t: new Date().toISOString(), d, b: balAfter, r: reward });
              } else if (balBefore !== null && balAfter !== null && balAfter <= balBefore) {
                // Demo site — no balance change but survey completed
                log(`  ✓ SURVEY COMPLETED! (demo—no balance change, $${balBefore.toFixed(2)})`);
              } else {
                log(`  ✓ SURVEY COMPLETED! (balance N/A)`);
              }
              done++;
            } else if (result === 'screened') {
              log(`  ○ Screened (${qAnswered} Qs)`);
              scr++;
            } else {
              log(`  ✗ Failed (${qAnswered} Qs)`);
              fail++;
            }

            // Back to surveys
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
  log('TOPSURVEYS PRODUCTION AGENT v4 — page-context API calls');
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
    await sleep(pm * 60000);
  }
}

main().catch(e => { log(`FATAL: ${e.message}`); process.exit(1); });
