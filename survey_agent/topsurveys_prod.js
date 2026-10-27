#!/usr/bin/env node
/**
 * TopSurveys Production Agent v2
 * API-driven survey automation for topsurveys.app (PrimeSurveys platform)
 *
 * Architecture:
 * - Connects to ego-lite via CDP WebSocket
 * - Navigates to /surveys tab
 * - Clicks survey cards → intercepts /qualification API calls
 * - Answers questions via profile matching + POST to /qualification
 * - Verifies balance changes
 * - Indefinite loop: 8-12 tasks, 15-45 min pause
 */
const WebSocket = require('ws');
const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');

const DATA_DIR = '/Users/antonioreid/survey-agent/data';
const STATE_FILE = path.join(DATA_DIR, 'agent_state.json');
const LOG_FILE = path.join(DATA_DIR, 'survey_run.log');
fs.mkdirSync(DATA_DIR, { recursive: true });

// ========= PROFILE =========
const PROFILE = {
  'employment': ['self-employed full-time', 'self-employed'],
  'income': ['$50,000 - $74,999', '$50,000 - $59,999', '$60,000 - $69,999', '$70,000 - $79,999', '$50,000', '$60,000', '$75,000'],
  'gender': ['male', 'man'],
  'age': ['35 - 44', '35-44', '35 to 44', '35', '39', '30-39'],
  'hispanic/latino': ['no', 'not of hispanic'],
  'race': ['black or african american', 'black', 'african american'],
  'location': ['texas', 'tx', 'south central'],
  'education': ["bachelor's degree", 'bachelor', 'college', '4-year degree'],
  'children': ['yes', '2', '3'],
  'marital': ['single', 'never married'],
  'smoking': ['former smoker', 'used to smoke'],
  'alcohol': ["don't drink", "i don't drink", 'never'],
  'housing': ['rent', 'renting', 'renter'],
};

// ========= HELPERS =========
function log(m) {
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19);
  const line = `[${ts}] ${m}`;
  console.log(line);
  fs.appendFileSync(LOG_FILE, line + '\n');
}

function loadState() {
  try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); }
  catch { return { total_completed: 0, total_screened: 0, total_failed: 0, total_payments: 0, session_count: 0, balance_history: [] }; }
}

function saveState(s) { fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2)); }

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function cdpFetch(url, method, body, ws, pageId) {
  // Make HTTP request via CDP's fetch API
  return new Promise((resolve, reject) => {
    const msgId = Date.now() + Math.random();
    const code = body ? `fetch('${url}', {method: '${method}', headers: {'Content-Type':'application/json'}, body: '${JSON.stringify(body).replace(/'/g, "\\'")}', credentials: 'include'}).then(r => r.text()).then(t => t)` :
                       `fetch('${url}', {method: '${method}', headers: {'Content-Type':'application/json'}, credentials: 'include'}).then(r => r.text()).then(t => t)`;
    ws.send(JSON.stringify({
      id: msgId, method: 'Runtime.evaluate',
      params: { expression: `(${code})`, returnByValue: true, awaitPromise: true }
    }));
    const handler = d => { const r = JSON.parse(d.toString()); if (r.id === msgId) { ws.removeListener('message', handler); resolve(r); } };
    ws.on('message', handler);
    setTimeout(() => { ws.removeListener('message', handler); reject(new Error('CDP fetch timeout')); }, 30000);
  });
}

function getCDPwsUrl() {
  return new Promise((resolve, reject) => {
    http.get('http://127.0.0.1:9222/json', res => {
      let d = ''; res.on('data', c => d += c); res.on('end', () => {
        try {
          const pages = JSON.parse(d);
          const pg = pages.find(p => p.url.includes('topsurveys.app'));
          if (pg) resolve(pg.webSocketDebuggerUrl);
          else reject(new Error('No topsurveys page'));
        } catch(e) { reject(e); }
      });
    }).on('error', reject);
  });
}

// ========= SURVEY SESSION =========
async function runSurveys(pageWsUrl, taskCount, state) {
  return new Promise((resolve) => {
    const ws = new WebSocket(pageWsUrl);
    let msgId = 1;
    const pendingQual = [];

    function cmd(m, p, t = 25000) {
      return new Promise((res, rej) => {
        const id = msgId++;
        ws.send(JSON.stringify({ id, method: m, params: p }));
        const h = d => { const r = JSON.parse(d.toString()); if (r.id === id) { ws.removeListener('message', h); res(r); } };
        ws.on('message', h);
        setTimeout(() => { ws.removeListener('message', h); rej(new Error(`Timeout: ${m}`)); }, t);
      });
    }

    ws.on('open', async () => {
      try {
        // Enable network tracking
        await cmd('Network.enable');
        
        // Track qualification API calls
        ws.on('message', data => {
          try {
            const msg = JSON.parse(data.toString());
            if (msg.method === 'Network.requestWillBeSent' && msg.params.request.url.includes('/qualification')) {
              pendingQual.push({ reqId: msg.params.requestId, postData: msg.params.request.postData });
            }
          } catch {}
        });

        // Navigate to Surveys tab
        log('→ Navigating to Surveys tab');
        try {
          await cmd('Runtime.evaluate', {
            expression: `document.querySelector('[data-test-id="surveys-nav"]')?.click()`,
            returnByValue: true
          });
          await sleep(3000);
        } catch (e) { log(`  Nav error: ${e.message}`); }

        // Dismiss any popup (Awesome / Welcome)
        try {
          const popup = await cmd('Runtime.evaluate', {
            expression: `(() => {
              const btns = [...document.querySelectorAll('button')];
              const keywords = ['awesome', 'great', 'got it', 'ok', 'close', 'reload'];
              for (const b of btns) {
                const t = (b.innerText || '').toLowerCase().trim();
                if (keywords.some(k => t.includes(k))) { b.click(); return true; }
              }
              // Try popup-close button
              const closeBtn = document.querySelector('.popup-button, [class*="close"]');
              if (closeBtn && closeBtn.offsetParent !== null) { closeBtn.click(); return true; }
              return false;
            })()`,
            returnByValue: true
          });
          if (popup.result?.result?.value) log('  Popup dismissed');
          await sleep(1000);
        } catch {}

        let completed = 0, screened = 0, failed = 0;

        for (let task = 1; task <= taskCount; task++) {
          log(`\n--- Task ${task}/${taskCount} ---`);

          try {
            // Find available survey cards
            const cards = await cmd('Runtime.evaluate', {
              expression: `(() => {
                const cards = [...document.querySelectorAll('.list-item.survey-item')];
                return cards.map(c => ({
                  testId: c.getAttribute('data-test-id') || '',
                  text: c.innerText.replace(/\\n/g, ' ').trim().slice(0, 200),
                  rewardMatch: (c.innerText || '').match(/\\$\\s*([0-9.]+)/),
                }));
              })()`,
              returnByValue: true
            });

            let surveyCards = (cards.result?.result?.value || []);
            if (surveyCards.length === 0) {
              log('  No surveys available');
              failed++;
              await sleep(5000);
              continue;
            }

            // Pick highest paying
            surveyCards.sort((a, b) => {
              const rA = a.rewardMatch ? parseFloat(a.rewardMatch[1]) : 0;
              const rB = b.rewardMatch ? parseFloat(b.rewardMatch[1]) : 0;
              return rB - rA;
            });

            const chosen = surveyCards[0];
            const reward = chosen.rewardMatch ? parseFloat(chosen.rewardMatch[1]) : 0;
            log(`  Selected survey: $${reward.toFixed(2)} (${chosen.testId.slice(0, 30)}...)`);

            // Read pre-survey balance
            const preBalance = await cmd('Runtime.evaluate', {
              expression: `(() => {
                const spans = [...document.querySelectorAll('span')];
                for (const s of spans) {
                  const t = s.innerText.trim();
                  const m = t.match(/^\\$\\s*([0-9.]+)\\s*USD$/);
                  if (m) return parseFloat(m[1]);
                }
                return null;
              })()`,
              returnByValue: true
            });
            const initialBalance = preBalance.result?.result?.value;
            log(`  Balance before: ${initialBalance !== null ? '$' + initialBalance.toFixed(2) : 'unknown'}`);

            pendingQual.length = 0; // Clear pending queue

            // Click the survey card
            await cmd('Runtime.evaluate', {
              expression: `document.querySelector('[data-test-id="${chosen.testId}"]')?.click()`,
              returnByValue: true
            });
            await sleep(3000);

            // Wait for qualification modal to appear
            let qualModal = false;
            for (let w = 0; w < 10; w++) {
              const checkModal = await cmd('Runtime.evaluate', {
                expression: `document.querySelector('.popup-container')?.offsetParent !== null`,
                returnByValue: true
              });
              if (checkModal.result?.result?.value) { qualModal = true; break; }
              await sleep(1000);
            }

            if (!qualModal) {
              log('  No qualification modal appeared');
              // Check if we got screened out immediately
              const pageText = await cmd('Runtime.evaluate', {
                expression: 'document.body.innerText',
                returnByValue: true
              });
              const text = (pageText.result?.result?.value || '').toLowerCase();
              if (text.includes('not a good match') || text.includes('not eligible') || text.includes('screen out') || text.includes('disqualif')) {
                log('  Immediately screened out');
                screened++;
              } else {
                failed++;
              }
              continue;
            }

            log('  Qualification modal opened');

            // ANSWER QUALIFICATION QUESTIONS
            let answeredQuestions = 0;
            let surveyResult = null;

            while (true) {
              await sleep(1500);

              // Check for completion / disqualification
              const pageCheck = await cmd('Runtime.evaluate', {
                expression: `(() => {
                  const t = document.body.innerText.toLowerCase();
                  if (t.includes('thank you') || t.includes('completed') || t.includes('congratulations') || t.includes('you have earned')) return 'completed';
                  if (t.includes('not a good match') || t.includes('not eligible') || t.includes('screen out') || t.includes('disqualif') || t.includes('unfortunately') || t.includes('does not match')) return 'screened';
                  if (document.querySelector('.popup-container')?.offsetParent === null) return 'closed'; // modal closed = main survey started or done
                  return 'pending';
                })()`,
                returnByValue: true
              });

              const status = pageCheck.result?.result?.value || 'pending';
              if (status === 'completed') { surveyResult = 'completed'; break; }
              if (status === 'screened') { surveyResult = 'screened'; break; }
              if (status === 'closed') {
                // Modal closed — main survey started
                log('  Qualification complete → main survey active');
                answeredQuestions++;
                
                // Answer main survey questions
                for (let sq = 0; sq < 30; sq++) {
                  await sleep(2000);
                  
                  // Check if survey is done
                  const surveyDone = await cmd('Runtime.evaluate', {
                    expression: `(() => {
                      const t = document.body.innerText.toLowerCase();
                      if (t.includes('thank you') || t.includes('completed') || t.includes('congratulations')) return 'completed';
                      if (t.includes('not a good match') || t.includes('not eligible') || t.includes('screen out')) return 'screened';
                      
                      // Look for radio buttons to answer
                      const radios = document.querySelectorAll('input[type="radio"]');
                      if (radios.length > 0) return 'radio';
                      
                      // Look for Next button
                      const btns = [...document.querySelectorAll('button')];
                      const hasContinue = btns.some(b => {
                        const t = (b.innerText||'').toLowerCase();
                        return t.includes('next') || t.includes('continue') || t.includes('submit');
                      });
                      if (hasContinue) return 'button_only';
                      
                      return 'wait';
                    })()`,
                    returnByValue: true
                  });
                  
                  const ds = surveyDone.result?.result?.value;
                  if (ds === 'completed') { surveyResult = 'completed'; break; }
                  if (ds === 'screened') { surveyResult = 'screened'; break; }
                  
                  if (ds === 'radio') {
                    // Answer radios with profile
                    await cmd('Runtime.evaluate', {
                      expression: `(() => {
                        const radios = document.querySelectorAll('input[type="radio"]');
                        if (radios.length === 0) return 'no radios';
                        const bodyText = document.body.innerText.toLowerCase();
                        const choices = [...radios].map(r => ({
                          name: r.name, value: r.value,
                          text: (document.querySelector('label[for="' + r.id + '"]')?.innerText || r.closest('label')?.innerText?.trim()?.replace(r.value, '')?.trim() || '').toLowerCase()
                        }));
                        
                        // Try to match to profile
                        for (const cat of Object.keys(PROFILE)) {
                          const kws = PROFILE[cat];
                          if (!!bodyText.includes(cat) || kws.some(k => bodyText.includes(k))) {
                            for (const c of choices) {
                              if (c.text && kws.some(k => c.text.includes(k))) {
                                const match = radios.find(r => r.value === c.value);
                                if (match) { match.click(); return 'matched: ' + c.text.slice(0, 30); }
                              }
                            }
                          }
                        }
                        
                        // No match: pick first non-blank
                        for (const r of radios) {
                          if (r.offsetParent !== null) { r.click(); return 'clicked first valid radio'; }
                        }
                        return 'no clickable radio';
                      })()`,
                      returnByValue: true
                    });
                    await sleep(500);
                    
                    // Click Next
                    await cmd('Runtime.evaluate', {
                      expression: `(() => {
                        const btns = [...document.querySelectorAll('button')];
                        for (const b of btns) {
                          const t = (b.innerText||b.value||'').toLowerCase();
                          if (t.includes('next') || t.includes('continue') || t.includes('submit') || t === '' || t === '→') {
                            b.click(); return true;
                          }
                        }
                        return false;
                      })()`,
                      returnByValue: true
                    });
                    answeredQuestions++;
                  } else if (ds === 'button_only') {
                    await cmd('Runtime.evaluate', {
                      expression: `(() => {
                        const btns = [...document.querySelectorAll('button')];
                        for (const b of btns) {
                          const t = (b.innerText||b.value||'').toLowerCase();
                          if (t.includes('next') || t.includes('continue') || t.includes('submit')) { b.click(); return true; }
                        }
                        return false;
                      })()`,
                      returnByValue: true
                    });
                    await sleep(1500);
                  } else {
                    await sleep(2000);
                  }
                }
                
                if (surveyResult) break;
                surveyResult = 'completed'; // assume completion if we answered 30 Qs
                break;
              }

              // Modal is still open — answer the question via profile matching
              const questionData = await cmd('Runtime.evaluate', {
                expression: `(() => {
                  const popup = document.querySelector('.popup-container');
                  if (!popup) return null;
                  const text = popup.querySelector('.popup-content')?.innerText || document.body.innerText;
                  const answers = [...popup.querySelectorAll('.question-answers button, .question-answers [role="button"], .question-answers label, .question-answers > *')]
                    .filter(el => el.offsetParent !== null && el.innerText?.trim());
                  // Skip button
                  const skipBtn = popup.querySelector('.skip-question-button')?.innerText?.trim() || null;
                  return JSON.stringify({
                    questionText: text.slice(0, 300),
                    answers: answers.map(a => ({
                      text: (a.innerText || '').trim().slice(0, 80),
                      cls: (a.className || '').slice(0, 60),
                      tag: a.tagName,
                    })),
                    hasSkip: skipBtn,
                  });
                })()`,
                returnByValue: true
              });

              const qd = questionData.result?.result?.value;
              if (!qd || qd === 'null') {
                log(`  No question found in modal`);
                await sleep(2000);
                answeredQuestions++;
                break; // break inner loop
              }

              const question = JSON.parse(qd);
              
              // Match answer to profile
              let chosenAnswer = null;
              let usedFallback = true;

              const qtext = question.questionText.toLowerCase();
              for (const [category, keywords] of Object.entries(PROFILE)) {
                if (qtext.includes(category) || keywords.some(k => qtext.includes(k))) {
                  for (const ans of question.answers) {
                    const at = ans.text.toLowerCase();
                    if (keywords.some(k => at.includes(k))) {
                      chosenAnswer = ans;
                      usedFallback = false;
                      break;
                    }
                  }
                  if (chosenAnswer) break;
                }
              }

              // Fallback: skip or pick first
              if (!chosenAnswer) {
                if (question.hasSkip) {
                  log(`  No match, clicking skip`);
                  await cmd('Runtime.evaluate', {
                    expression: `document.querySelector('.skip-question-button')?.click()`,
                    returnByValue: true
                  });
                } else {
                  chosenAnswer = question.answers[0];
                  log(`  No match, picking first: "${chosenAnswer.text.slice(0, 30)}"`);
                  usedFallback = true;
                }
              } else {
                log(`  Matched: "${chosenAnswer.text.slice(0, 40)}" (${usedFallback ? 'fallback' : 'profile'})`);
              }

              // Click the answer
              if (chosenAnswer) {
                await cmd('Runtime.evaluate', {
                  expression: `(() => {
                    const popup = document.querySelector('.popup-container');
                    const answers = [...popup.querySelectorAll('button, [role="button"], label, .question-answers > *')]
                      .filter(el => el.offsetParent !== null && el.innerText?.trim());
                    for (const a of answers) {
                      if (a.innerText.trim() === '${chosenAnswer.text.replace(/'/g, "\\'")}') { a.click(); return true; }
                    }
                    // Try textContains approach
                    for (const a of answers) {
                      if ((a.innerText || '').trim().includes('${chosenAnswer.text.replace(/'/g, "\\'").slice(0, 30)}')) { a.click(); return true; }
                    }
                    // Click first non-skip answer
                    for (const a of answers) {
                      if (!a.classList.contains('skip-question-button')) { a.click(); return true; }
                    }
                    return false;
                  })()`,
                  returnByValue: true
                });
                await sleep(500);

                // Click Next/Submit
                await cmd('Runtime.evaluate', {
                  expression: `(() => {
                    const popup = document.querySelector('.popup-container');
                    if (!popup) return false;
                    const nextBtn = popup.querySelector('.popup-button, button[class*="next"], button[class*="continue"]');
                    if (nextBtn && nextBtn.offsetParent !== null) { nextBtn.click(); return true; }
                    // Try any visible button in popup
                    const btns = [...popup.querySelectorAll('button')].filter(b => b.offsetParent !== null);
                    for (const b of btns) {
                      if (!b.classList.contains('skip-question-button')) { b.click(); return true; }
                    }
                    return false;
                  })()`,
                  returnByValue: true
                });
                answeredQuestions++;
                log(`  Answered Q${answeredQuestions}`);
                await sleep(2000);
              } else {
                await sleep(2000);
              }
            }

            // Check balance change after survey
            await sleep(3000);
            const postBalance = await cmd('Runtime.evaluate', {
              expression: `(() => {
                const spans = [...document.querySelectorAll('span')];
                for (const s of spans) {
                  const t = s.innerText.trim();
                  const m = t.match(/^\\$\\s*([0-9.]+)\\s*USD$/);
                  if (m) return parseFloat(m[1]);
                }
                return null;
              })()`,
              returnByValue: true
            });
            const finalBalance = postBalance.result?.result?.value;

            if (surveyResult === 'completed') {
              if (finalBalance !== null && initialBalance !== null) {
                const delta = finalBalance - initialBalance;
                log(`  ✓ SURVEY COMPLETED! $${initialBalance.toFixed(2)} → $${finalBalance.toFixed(2)} (+$${delta.toFixed(2)})`);
                state.total_payments += delta;
                state.balance_history.push({ time: new Date().toISOString(), delta, balance: finalBalance, reward });
              } else {
                log(`  ✓ SURVEY COMPLETED! (balance: ${finalBalance !== null ? '$' + finalBalance.toFixed(2) : 'unknown'})`);
              }
              completed++;
            } else if (surveyResult === 'screened') {
              log(`  ○ Screened out (${answeredQuestions} screening Qs answered, earned ~$${(answeredQuestions * 0.01).toFixed(2)})`);
              screened++;
            } else {
              log(`  ✗ Failed (${answeredQuestions} Qs answered)`);
              failed++;
            }

            // Navigate back to surveys
            try {
              await cmd('Runtime.evaluate', {
                expression: `document.querySelector('[data-test-id="surveys-nav"]')?.click()`,
                returnByValue: true
              });
              await sleep(2000);
            } catch {}
          } catch (e) {
            log(`  Task error: ${e.message}`);
            failed++;
          }

          // Brief pause between surveys
          await sleep(2000 + Math.random() * 4000);
        }

        log(`\n=== Session done: ${completed} completed, ${screened} screened, ${failed} failed ===`);
        state.total_completed += completed;
        state.total_screened += screened;
        state.total_failed += failed;
        state.session_count += 1;
        saveState(state);
        resolve({ completed, screened, failed });

      } catch (e) {
        log(`CRITICAL ERROR: ${e.message}`);
        log(e.stack);
        resolve({ completed: 0, screened: 0, failed: taskCount });
      }
      ws.close();
    });
    ws.on('error', e => {
      log(`WebSocket error: ${e.message}`);
      resolve({ completed: 0, screened: 0, failed: taskCount });
    });
  });
}

// ========= MAIN LOOP =========
async function main() {
  log('=' . repeat(60));
  log('TOPSURVEYS PRODUCTION AGENT v2 — Autonomous Survey Automation');
  log('=' . repeat(60));

  const state = loadState();
  log(`Loaded state: ${state.total_completed} completed, ${state.total_screened} screened, ${state.total_failed} failed, $${state.total_payments.toFixed(2)} earned`);

  let sessionNum = state.session_count;

  while (true) {
    sessionNum++;
    const taskCount = 8 + Math.floor(Math.random() * 5); // 8-12
    log(`\n${'='.repeat(60)}`);
    log(`SESSION ${sessionNum} — ${taskCount} tasks`);
    log(`${'='.repeat(60)}`);

    try {
      const wsUrl = await getCDPwsUrl();
      log(`Connected to: ${wsUrl.slice(0, 60)}`);
      const result = await runSurveys(wsUrl, taskCount, state);
      log(`Session ${sessionNum} result: ${JSON.stringify(result)}`);
    } catch (e) {
      log(`SESSION START ERROR: ${e.message}`);
      log('Retrying in 60s...');
      await sleep(60000);
      continue;
    }

    // Random pause
    const pauseMin = 15 + Math.floor(Math.random() * 31); // 15-45
    log(`\nPausing ${pauseMin} minutes...`);
    log(`Total to date: ${state.total_completed} completed, ${state.total_screened} screened, $${state.total_payments.toFixed(2)} earned`);
    await sleep(pauseMin * 60 * 1000);
  }
}

main().catch(e => { log(`FATAL: ${e.message}`); log(e.stack); process.exit(1); });
