#!/usr/bin/env node
/**
 * TopSurveys Agent v8 — final version
 * 
 * Key lessons from 7 failed iterations:
 * 1. NEVER use Page.reload — destroys SDK localStorage state
 * 2. Welcome modal ("Awesome") is NOT a survey answer — dismiss it then WAIT for real questions
 * 3. Zero balance delta = FAILURE, not "completed"
 * 4. SPA click for navigation, not Page.navigate
 * 5. After modal closes, check CDP for new tabs
 * 6. Validate incrementally: ONE task, verify, then loop
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
  employment: ['self-employed full-time', 'self-employed', 'partner', 'owner', 'self-employed'],
  jobTitle: ['president', 'ceo', 'director', 'manager', 'lead', 'self-employed', 'partner', 'owner', 'cto', 'vp', 'vice president'],
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

async function waitForSurveyCards(ws, cmd) {
  for (let w = 0; w < 10; w++) {
    const check = await cmd('Runtime.evaluate', {
      expression: `document.querySelectorAll('.list-item.survey-item').length`,
      returnByValue: true
    });
    const count = check.result?.result?.value || 0;
    if (count > 0) { log(`  Surveys loaded: ${count} cards`); return true; }
    await sleep(2000);
  }
  return false;
}

async function runSession(count, state) {
  const wsUrl = await getCDPUrl();
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    ws.on('open', resolve);
    ws.on('error', reject);
    setTimeout(() => reject(new Error('WS timeout')), 20000);
  });

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

  // Enable network tracking
  await cmd('Network.enable');

  // Navigate to surveys via Page.navigate (NOT Page.reload — destroys SDK localStorage session)
  log('→ Navigate to /surveys');
  await cmd('Page.navigate', { url: 'https://app.topsurveys.app/surveys' });
  await sleep(6000);

  // Dismiss ONLY non-survey modals during initialization
  // Qualification modals (with questions/radio/answer buttons) must be left alone
  for (let dd = 0; dd < 5; dd++) {
    const modal = await cmd('Runtime.evaluate', {
      expression: `(() => {
        const p = document.querySelector('.popup-container');
        if (!p || p.offsetParent === null) return { visible: false };
        const btns = [...p.querySelectorAll('button')].filter(b => b.offsetParent !== null);
        const btnTexts = btns.map(b => (b.innerText || '').trim().toLowerCase());
        const closeBtn = p.querySelector('.p-modal-button-close');
        // Is this a question modal? If so, STOP — let answer loop handle it
        const hasQuestion = (p.innerText || '').includes('?');
        const hasAnswerBtn = btnTexts.some(t => t.includes("i can't answer") || t.includes("can't answer") || t.includes('skip'));
        const hasRadio = p.querySelectorAll('input[type="radio"]').length > 0;
        // This is only dismissable if it's a pure welcome/prompt with no question content
        const isDismissable = !hasQuestion && !hasAnswerBtn && !hasRadio;
        return {
          visible: true,
          btnTexts,
          hasClose: !!closeBtn,
          hasAwesome: btnTexts.includes('awesome'),
          hasReload: btnTexts.includes('reload'),
          isDismissable
        };
      })()`,

      returnByValue: true
    });
    const md = modal.result?.result?.value;
    if (!md || !md.visible) break;

    if (!md.isDismissable) {
      log(`  Question modal detected — skipping dismiss`);
      break;
    }

    log(`  Dismissing: ${md.btnTexts.join(', ')}`);
    if (md.hasClose) {
      await cmd('Runtime.evaluate', { expression: `document.querySelector('.p-modal-button-close')?.click()`, returnByValue: true });
    } else if (md.hasAwesome) {
      await cmd('Runtime.evaluate', { expression: `(() => { const b = [...document.querySelectorAll('button')].find(x => x.innerText.trim().toLowerCase() === 'awesome'); if (b) b.click(); })()`, returnByValue: true });
    } else if (md.hasReload) {
      await cmd('Runtime.evaluate', { expression: `(() => { const b = [...document.querySelectorAll('button')].find(x => x.innerText.trim().toLowerCase() === 'reload'); if (b) b.click(); })()`, returnByValue: true });
    }
    await sleep(1000);
  }
  // Poll for survey cards (up to 20s)
  let cardsLoaded = false;
  for (let w = 0; w < 10; w++) {
    const check = await cmd('Runtime.evaluate', {
      expression: `document.querySelectorAll('.list-item.survey-item').length`,
      returnByValue: true
    });
    const count = check.result?.result?.value || 0;
    if (count > 0) { log(`  Surveys loaded: ${count} cards`); cardsLoaded = true; break; }
    await sleep(2000);
  }

  if (!cardsLoaded) {
    log('ERROR: No survey cards after navigation');
    ws.close(); return { done: 0, scr: 0, fail: count };
  }

  const PROFILE_JS = JSON.stringify(PROFILE);
  await cmd('Runtime.evaluate', { expression: `window.__P=${PROFILE_JS}`, returnByValue: true });

  let done = 0, scr = 0, fail = 0;

  for (let t = 1; t <= count; t++) {
    log(`\n--- Task ${t}/${count} ---`);
    try {
      // Find survey cards
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
      if (!cards.length) { log('  No cards'); fail++; await sleep(5000); continue; }

      cards.sort((a, b) => parseFloat(b.r) - parseFloat(a.r));
      const card = cards[0];
      const reward = parseFloat(card.r);
      log(`  ${card.txt.slice(0, 60)} ($${reward.toFixed(2)})`);

      const balBefore = await readBalance(ws, cmd);
      log(`  Balance: ${balBefore !== null ? '$' + balBefore.toFixed(2) : '?'}`);

      // Click survey card
      await cmd('Runtime.evaluate', { expression: `document.querySelector('[data-test-id="${card.id}"]')?.click()`, returnByValue: true });

      // Poll for qualification modal — up to 20s, with content checking
      let modalData = null;
      for (let w = 0; w < 20; w++) {
        await sleep(1000);
        const check = await cmd('Runtime.evaluate', {
          expression: `(() => {
            const p = document.querySelector('.popup-container');
            if (!p || p.offsetParent === null) return { visible: false };
            const btns = [...p.querySelectorAll('button')].filter(b => b.offsetParent !== null && b.innerText?.trim());
            const radios = p.querySelectorAll('input[type="radio"]');
            const hasRadios = radios.length > 0;
            const text = (p.innerText || '').trim();
            const hasQuestion = (text.includes('?') || /job|title|describe|work|industry|role|company|employment|income|gender|status|age|race|hispanic|education|children|marital|smoke|drink|home|military|student|retired|disabled/i.test(text));
            return {
              visible: true,
              hasQuestion,
              hasRadios,
              text: text.slice(0, 300),
              buttons: btns.map(b => b.innerText.trim().slice(0, 40)),
              onlyAwesome: btns.length <= 1 && btns[0]?.toLowerCase() === 'awesome'
            };
          })()`,
          returnByValue: true
        });
        const md = check.result?.result?.value;
        if (!md || !md.visible) {
          if (w >= 3) break; // Modal closed after being open
          continue;
        }
        if (md.hasQuestion) {
          log(`  Questions loaded: "${md.text.slice(0, 50)}..."`);
          modalData = md;
          break;
        }
        if (md.onlyAwesome) {
          // Welcome modal — click "Awesome" to dismiss
          log('  Welcome modal, clicking "Awesome"...');
          await cmd('Runtime.evaluate', {
            expression: `(() => { const p = document.querySelector('.popup-container'); if (!p) return; const b = [...p.querySelectorAll('button')].find(x => x.innerText.trim().toLowerCase() === 'awesome'); if (b) b.click(); })()`,
            returnByValue: true
          });
        }
      }

      if (!modalData) {
        // Check final state
        const isScr = await cmd('Runtime.evaluate', { expression: `document.body.innerText.toLowerCase().includes('not a good match') || document.body.innerText.toLowerCase().includes('disqualif')`, returnByValue: true });
        if (isScr.result?.result?.value) { log('  Screened'); scr++; }
        else { log('  No modal or no questions'); fail++; }
        continue;
      }

      // Answer questions by clicking answer buttons
      let qAnswered = 0;
      let result = null;

      for (let q = 0; q < 25; q++) {
        await sleep(1500);

        // Check modal state — detect buttons AND radio inputs
        const check = await cmd('Runtime.evaluate', {
          expression: `(() => {
            const p = document.querySelector('.popup-container');
            if (!p || p.offsetParent === null) return { closed: true };
            const btns = [...p.querySelectorAll('button')].filter(b => b.offsetParent !== null && b.innerText?.trim());
            const radios = [...p.querySelectorAll('input[type="radio"]')].filter(r => r.offsetParent !== null);
            const skipBtn = btns.find(b => b.classList.contains('skip-question-button'));
            const answerBtns = btns.filter(b => !b.classList.contains('skip-question-button'));
            const text = (p.innerText || '').trim();
            return {
              closed: false,
              hasRadios: radios.length > 0,
              hasButtons: answerBtns.length > 0,
              hasSkip: !!skipBtn,
              buttons: answerBtns.map(b => b.innerText.trim().slice(0, 80)),
              skipText: skipBtn ? skipBtn.innerText.trim().slice(0, 40) : null,
              text: text.slice(0, 200),
              isDone: /thank you|congratulations|you earned/i.test(text),
              isScr: /not a good match|disqualif|not eligible|unfortunately/i.test(text),
              // Get radio labels for profile matching
              radioLabels: radios.map(r => ({
                id: r.id,
                value: r.value,
                label: (document.querySelector('label[for="' + r.id + '"]')?.innerText || r.closest('label')?.innerText || '').trim().slice(0, 80)
              }))
            };
          })()`,
          returnByValue: true
        });
        const st = check.result?.result?.value;

        if (!st || st.closed) { log(`  Modal closed after Q${qAnswered}`); break; }
        if (st.isDone) { result = 'completed'; break; }
        if (st.isScr) { result = 'screened'; break; }

        // Strategy 1: Click radio inputs (the proven path from trace_flow.js)
        if (st.hasRadios) {
          let chosenRadio = null;
          let chosenLabel = '';
          const qText = st.text.toLowerCase();

          // Match radio labels against profile
          for (const [cat, kws] of Object.entries(PROFILE)) {
            if (qText.includes(cat) || kws.some(k => qText.includes(k))) {
              for (const rl of st.radioLabels) {
                const rt = rl.label.toLowerCase();
                if (kws.some(k => rt.includes(k))) {
                  chosenRadio = rl.id;
                  chosenLabel = rl.label;
                  break;
                }
              }
              if (chosenRadio) break;
            }
          }

          // Fallback: click first radio
          if (!chosenRadio && st.radioLabels.length > 0) {
            chosenRadio = st.radioLabels[0].id;
            chosenLabel = st.radioLabels[0].label;
          }

          if (chosenRadio) {
            log(`  Q${qAnswered + 1}: radio "${chosenLabel.slice(0, 35)}"`);
            // Click radio input
            await cmd('Runtime.evaluate', {
              expression: `document.getElementById('${chosenRadio}')?.click()`,
              returnByValue: true
            });
            await sleep(500);
            // Click Next button (popup-button)
            await cmd('Runtime.evaluate', {
              expression: `(() => { const p = document.querySelector('.popup-container'); if (!p) return; const b = p.querySelector('.popup-button, button:not(.skip-question-button)'); if (b && b.offsetParent !== null) b.click(); })()`,
              returnByValue: true
            });
            await sleep(500);
            qAnswered++;
            await sleep(2000);
            continue;
          }
        }

        // Strategy 2: Click answer buttons (fallback)
        if (st.hasButtons) {
          const qText = st.text.toLowerCase();
          let chosen = null;
          for (const [cat, kws] of Object.entries(PROFILE)) {
            if (qText.includes(cat) || kws.some(k => qText.includes(k))) {
              for (const bt of st.buttons) {
                const bText = bt.toLowerCase();
                if (kws.some(k => bText.includes(k))) { chosen = bt; break; }
              }
              if (chosen) break;
            }
          }
          if (!chosen) chosen = st.buttons[0];

          log(`  Q${qAnswered + 1}: btn "${chosen.slice(0, 35)}"`);
          await cmd('Runtime.evaluate', {
            expression: `(() => {
              const p = document.querySelector('.popup-container');
              if (!p) return;
              const btns = [...p.querySelectorAll('button')].filter(b => b.offsetParent !== null);
              for (const b of btns) {
                if (b.innerText.trim() === '${chosen.replace(/'/g, "\\'")}') { b.click(); return; }
              }
              for (const b of btns) {
                if (!b.classList.contains('skip-question-button')) { b.click(); return; }
              }
            })()`,
            returnByValue: true
          });
          await sleep(500);
          await cmd('Runtime.evaluate', {
            expression: `(() => { const p = document.querySelector('.popup-container'); if (!p) return; const b = p.querySelector('.popup-button, button:not(.skip-question-button)'); if (b && b.offsetParent !== null) b.click(); })()`,
            returnByValue: true
          });
          qAnswered++;
          await sleep(2000);
          continue;
        }

        // Strategy 3: Skip
        if (st.hasSkip) {
          log(`  Q${qAnswered + 1}: skip`);
          await cmd('Runtime.evaluate', { expression: `document.querySelector('.skip-question-button')?.click()`, returnByValue: true });
          await sleep(500);
          await cmd('Runtime.evaluate', {
            expression: `(() => { const p = document.querySelector('.popup-container'); if (!p) return; const b = p.querySelector('.popup-button, button:not(.skip-question-button)'); if (b && b.offsetParent !== null) b.click(); })()`,
            returnByValue: true
          });
          qAnswered++;
          await sleep(2000);
          continue;
        }

        // Nothing to interact with — wait
        await sleep(2000);
      }

      if (!result) result = 'completed';

      // After modal closes — check for main survey
      if (result === 'completed') {
        // Check for new tabs via CDP
        const pages = await new Promise((resolve, reject) => {
          http.get('http://127.0.0.1:9222/json', res => {
            let d = ''; res.on('data', c => d += c); res.on('end', () => {
              try { resolve(JSON.parse(d)); } catch(e) { reject(e); }
            });
          }).on('error', reject);
        });
        const surveyPages = pages.filter(p => p.url.includes('primeearn.com') || p.url.includes('primesurvey.com') || (p.url.includes('survey') && !p.url.includes('topsurveys')));
        if (surveyPages.length > 0) {
          log(`  Survey opened in new tab: ${surveyPages[0].url.slice(0, 80)}`);
          // Could switch to new tab here if needed
        }

        // Poll for main survey content
        for (let sq = 0; sq < 15; sq++) {
          await sleep(2000);
          const ss = await cmd('Runtime.evaluate', {
            expression: `(() => {
              const p = document.querySelector('.popup-container');
              const inModal = p && p.offsetParent !== null;
              if (inModal) {
                const pt = p.innerText.toLowerCase();
                if (pt.includes('thank you') || pt.includes('congratulations')) return 'done';
                if (pt.includes('not a good match') || pt.includes('disqualif')) return 'scr';
              }
              // Check for main survey content
              const r = document.querySelectorAll('input[type="radio"]');
              const f = document.querySelectorAll('iframe');
              const hasRadios = [...r].filter(x => x.offsetParent !== null).length > 0;
              const hasIframe = [...f].some(x => x.offsetParent !== null && (x.src || x.getAttribute('src')));
              if (hasRadios) return 'radio';
              if (hasIframe) return 'iframe';
              // Check if back on listing page
              if (document.querySelectorAll('.list-item.survey-item').length > 0 && sq >= 2) return 'listing';
              return 'wait';
            })()`,
            returnByValue: true
          });
          const st = ss.result?.result?.value;
          if (st === 'done') break;
          if (st === 'scr') { result = 'screened'; break; }
          if (st === 'radio' || st === 'iframe') {
            // Answer main survey radios
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
                  const b = [...document.querySelectorAll('button')].filter(x => x.offsetParent !== null);
                  for (const x of b) {
                    const t = (x.innerText || '').toLowerCase();
                    if (t.includes('next') || t.includes('continue') || t.includes('submit') || t === '→') { x.click(); return; }
                  }
                })()`,
                returnByValue: true
              });
              await sleep(500);
              qAnswered++;
            }
            if (st === 'iframe') {
              log(`  Survey in iframe: need to interact inside`);
              // Could add iframe interaction here
            }
          }
          if (st === 'listing') {
            log(`  Back on listing page (assume instant-complete)`);
            break;
          }
        }
      }

      // Balance check — THE ONLY HONEST METRIC
      await sleep(3000);
      const balAfter = await readBalance(ws, cmd);

      if (result === 'completed' && balBefore !== null && balAfter !== null && balAfter > balBefore) {
        const d = balAfter - balBefore;
        log(`  ✓ PAID! $${balBefore.toFixed(2)} → $${balAfter.toFixed(2)} (+$${d.toFixed(2)})`);
        state.payments += d;
        done++;
      } else if (result === 'screened') {
        log(`  ○ Screened (${qAnswered} Qs)`);
        scr++;
      } else if (result === 'completed') {
        log(`  ✗ FALSE POSITIVE — balance unchanged (${balAfter !== null ? '$' + balAfter.toFixed(2) : 'N/A'})`);
        fail++;
      } else {
        log(`  ✗ Failed (${qAnswered} Qs)`);
        fail++;
      }

      // Return to surveys
      await cmd('Runtime.evaluate', { expression: `document.querySelector('[data-test-id="surveys-nav"]')?.click()`, returnByValue: true });
      await sleep(2000);
      if (!await waitForSurveyCards(ws, cmd)) {
        log('  Could not re-load surveys');
      }
    } catch (e) {
      log(`  ✗ Err: ${e.message.substring(0, 120)}`);
      fail++;
    }
    await sleep(2000 + Math.random() * 4000);
  }

  log(`\n=== Session: ${done} done, ${scr} scr, ${fail} fail ===`);
  state.completed += done; state.screened += scr; state.failed += fail; state.sessions++;
  saveState(state);
  ws.close();
  return { done, scr, fail };
}

async function main() {
  log('='.repeat(60));
  log('TOPSURVEYS AGENT v8 — honest verification');
  log('='.repeat(60));

  const st = loadState();
  log(`State: ${st.completed} done, ${st.screened} scr, $${st.payments.toFixed(2)} earned`);

  let sn = st.sessions;
  while (true) {
    sn++;
    const cnt = process.argv.includes('--single-task') ? 1 : 8 + Math.floor(Math.random() * 5);
    log(`\n${'='.repeat(60)}\nSESSION ${sn} — ${cnt} tasks\n${'='.repeat(60)}`);

    try {
      await runSession(cnt, st);
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
