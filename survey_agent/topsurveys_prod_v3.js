#!/usr/bin/env node
/**
 * TopSurveys Production Agent v3 — fixed qualification flow
 * API-driven: intercept /qualification response, match answers by ID, POST back
 */
const WebSocket = require('ws');
const http = require('http');
const fs = require('fs');
const path = require('path');

const DATA_DIR = '/Users/antonioreid/survey-agent/data';
const STATE_FILE = path.join(DATA_DIR, 'agent_state.json');
const LOG_FILE = path.join(DATA_DIR, 'survey_run.log');
fs.mkdirSync(DATA_DIR, { recursive: true });

const PROFILE = {
  employment: ['self-employed full-time', 'self-employed'],
  income: ['$50,000 - $74,999', '$50,000 - $59,999', '$60,000 - $69,999', '$70,000 - $79,999'],
  gender: ['male', 'man'],
  age: ['35 - 44', '35-44', '35 to 44', '30-39'],
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

function getCDPwsUrl() {
  return new Promise((resolve, reject) => {
    http.get('http://127.0.0.1:9222/json', res => {
      let d = ''; res.on('data', c => d += c); res.on('end', () => {
        try {
          const pages = JSON.parse(d);
          const pg = pages.find(p => p.url.includes('topsurveys.app'));
          if (pg) resolve(pg.webSocketDebuggerUrl);
          else reject(new Error('No topsurveys page found'));
        } catch(e) { reject(e); }
      });
    }).on('error', reject);
  });
}

async function runSession(wsUrl, taskCount, state) {
  const ws = new WebSocket(wsUrl);
  let msgId = 1;
  const networkReqs = [];

  function cmd(m, p, t = 25000) {
    return new Promise((res, rej) => {
      const id = msgId++;
      ws.send(JSON.stringify({ id, method: m, params: p }));
      const h = d => { const r = JSON.parse(d.toString()); if (r.id === id) { ws.removeListener('message', h); res(r); } };
      ws.on('message', h);
      setTimeout(() => { ws.removeListener('message', h); rej(new Error(`Timeout: ${m}`)); }, t);
    });
  }

  return new Promise(resolve => {
    ws.on('open', async () => {
      try {
        // Inject PROFILE into page
        await cmd('Runtime.evaluate', {
          expression: `window.__PROFILE__ = ${JSON.stringify(PROFILE)}`,
          returnByValue: true
        });

        await cmd('Network.enable');

        // Track qualification API calls for response body capture
        ws.on('message', data => {
          try {
            const msg = JSON.parse(data.toString());
            if (msg.method === 'Network.requestWillBeSent' && msg.params.request.url.includes('api.primesurveys.com')) {
              networkReqs.push({ type: 'req', reqId: msg.params.requestId, url: msg.params.request.url, method: msg.params.request.method, postData: msg.params.request.postData, ts: Date.now() });
            }
            if (msg.method === 'Network.responseReceived' && msg.params.response.url.includes('api.primesurveys.com')) {
              networkReqs.push({ type: 'resp', reqId: msg.params.requestId, status: msg.params.response.status, url: msg.params.response.url, ts: Date.now() });
            }
          } catch {}
        });

        // Navigate to Surveys tab
        log('→ Navigating to Surveys tab');
        await cmd('Runtime.evaluate', {
          expression: `document.querySelector('[data-test-id="surveys-nav"]')?.click()`,
          returnByValue: true
        });
        await sleep(3000);

        // Dismiss popup if present
        await cmd('Runtime.evaluate', {
          expression: `(()=>{const b=[...document.querySelectorAll('button')].find(b=>{const t=(b.innerText||'').toLowerCase();return t.includes('awesome')||t.includes('great')||t.includes('got it')||t==='ok'});if(b){b.click();return true;}const c=document.querySelector('.popup-button');if(c&&c.offsetParent!==null){c.click();return true;}return false;})()`,
          returnByValue: true
        });
        await sleep(1000);

        let completed = 0, screened = 0, failed = 0;

        for (let task = 1; task <= taskCount; task++) {
          log(`\n--- Task ${task}/${taskCount} ---`);

          try {
            // Find survey cards
            const cardsResult = await cmd('Runtime.evaluate', {
              expression: `(()=>{const cards=[...document.querySelectorAll('.list-item.survey-item')];return cards.map(c=>({testId:c.getAttribute('data-test-id')||'',text:c.innerText.replace(/\\n/g,' ').trim().slice(0,200),reward:(c.innerText||'').match(/\\$\\s*([0-9.]+)/)?.[1]||'0'}));})()`,
              returnByValue: true
            });
            const surveyCards = (cardsResult.result?.result?.value || []).filter(c => c.testId);
            if (surveyCards.length === 0) { log('  No surveys'); failed++; await sleep(5000); continue; }

            // Pick highest paying
            surveyCards.sort((a, b) => parseFloat(b.reward) - parseFloat(a.reward));
            const chosen = surveyCards[0];
            const reward = parseFloat(chosen.reward) || 0;
            log(`  Selected: $${reward.toFixed(2)} (${chosen.testId.slice(0,30)}...)`);

            // Read balance before
            const preB = await cmd('Runtime.evaluate', {
              expression: `(()=>{const spans=[...document.querySelectorAll('span')];for(const s of spans){const m=s.innerText.trim().match(/^\\$\\s*([0-9.]+)\\s*USD$/);if(m)return parseFloat(m[1]);}return null;})()`,
              returnByValue: true
            });
            const initialBalance = preB.result?.result?.value;

            let answeredQuestions = 0;
            let surveyResult = null;
            let surveyId = chosen.testId.replace('ps-survey-', '');

            // Click survey card
            networkReqs.length = 0;
            await cmd('Runtime.evaluate', {
              expression: `document.querySelector('[data-test-id="${chosen.testId}"]')?.click()`,
              returnByValue: true
            });
            await sleep(3000);

            // Wait for qualification modal and ANSWER via API
            let qualAvailable = 0;
            for (let qLoop = 0; qLoop < 20; qLoop++) {
              await sleep(1500);

              // Check terminal states
              const pageStatus = await cmd('Runtime.evaluate', {
                expression: `(()=>{const t=document.body.innerText.toLowerCase();const p=document.querySelector('.popup-container');const pVisible=p&&p.offsetParent!==null;if(t.includes('thank you')||t.includes('completed')||t.includes('congratulations'))return'completed';if(t.includes('not a good match')||t.includes('disqualif')||t.includes('not eligible'))return'screened';if(!pVisible)return'modal_closed';return'pending';})()`,
                returnByValue: true
              });
              const ps = pageStatus.result?.result?.value || 'pending';

              if (ps === 'completed') { surveyResult = 'completed'; break; }
              if (ps === 'screened') { surveyResult = 'screened'; break; }

              if (ps === 'modal_closed') {
                log(`  Qualification complete! (${answeredQuestions} Qs answered)`);
                break; // Exit qualification loop, enter main survey loop below
              }

              // Read current question from API response
              // Find the latest /qualification response in network log
              let currentQuestion = null;
              for (let i = networkReqs.length - 1; i >= 0; i--) {
                const nr = networkReqs[i];
                if (nr.type === 'resp' && nr.url.includes('/qualification') && nr.status === 200) {
                  try {
                    // Get response body
                    const respBody = await cmd('Network.getResponseBody', { requestId: nr.reqId }, 5000);
                    const body = JSON.parse(respBody.result?.body || '{}');
                    if (body.status && body.questions?.current) {
                      currentQuestion = body.questions.current;
                      qualAvailable = body.questions.available || 0;
                      break;
                    }
                    if (body.status === 'rejected') {
                      log(`  Qual rejected: ${body.message || ''}`);
                      surveyResult = 'screened';
                      break;
                    }
                  } catch {}
                }
              }

              if (surveyResult) break;
              if (!currentQuestion) {
                log(`  Q${answeredQuestions+1}: No question data yet, waiting...`);
                continue;
              }

              const qText = (currentQuestion.question || '').toLowerCase();
              const answers = currentQuestion.answers || [];

              // Match question to profile category
              let chosenAnswerId = null;
              let chosenAnswerText = '';

              profileMatch:
              for (const [category, keywords] of Object.entries(PROFILE)) {
                if (qText.includes(category) || keywords.some(k => qText.includes(k))) {
                  for (const ans of answers) {
                    const at = ans.text.toLowerCase();
                    if (keywords.some(k => at.includes(k))) {
                      chosenAnswerId = ans.id;
                      chosenAnswerText = ans.text;
                      break profileMatch;
                    }
                  }
                }
              }

              if (!chosenAnswerId && currentQuestion.is_skippable) {
                // Skip if available
                log(`  Q${answeredQuestions+1}: No match, skipping`);
                await cmd('Runtime.evaluate', {
                  expression: `document.querySelector('.skip-question-button')?.click()`,
                  returnByValue: true
                });
                await sleep(1000);
                await cmd('Runtime.evaluate', {
                  expression: `(()=>{const p=document.querySelector('.popup-container');if(!p)return;const b=p.querySelector('.popup-button, button:not(.skip-question-button)');if(b&&b.offsetParent!==null)b.click();})()`,
                  returnByValue: true
                });
                answeredQuestions++;
                await sleep(2000);
                continue;
              }

              if (!chosenAnswerId) {
                // Fallback: pick first answer
                chosenAnswerId = answers[0]?.id;
                chosenAnswerText = answers[0]?.text || '';
              }

              log(`  Q${answeredQuestions+1}: "${currentQuestion.question?.slice(0, 40) || ''}" → "${chosenAnswerText.slice(0, 30)}"`);

              // POST answer via page fetch
              const postPayload = JSON.stringify({
                survey: surveyId,
                question_id: currentQuestion.id,
                answer_id: chosenAnswerId
              });

              await cmd('Runtime.evaluate', {
                expression: `fetch('https://api.primesurveys.com/qualification',{method:'POST',headers:{'Content-Type':'application/json'},body:'${postPayload.replace(/'/g, "\\'")}',credentials:'include'}).then(r=>r.text()).then(t=>t)`,
                returnByValue: true,
                awaitPromise: true,
                timeout: 15000
              });
              await sleep(1000);

              answeredQuestions++;
            }

            // Main survey loop (modal closed, survey active)
            if (!surveyResult) {
              for (let sq = 0; sq < 40; sq++) {
                await sleep(2000);

                const surveyStatus = await cmd('Runtime.evaluate', {
                  expression: `(()=>{const t=document.body.innerText.toLowerCase();if(t.includes('thank you')||t.includes('completed')||t.includes('congratulations')||t.includes('you have earned'))return'completed';if(t.includes('not a good match')||t.includes('disqualif')||t.includes('not eligible'))return'screened';const r=document.querySelectorAll('input[type=\\"radio\\"]');if(r.length>0&&[...r].some(x=>x.offsetParent!==null))return'radio';const b=[...document.querySelectorAll('button')].filter(x=>x.offsetParent!==null);if(b.some(x=>(x.innerText||'').toLowerCase().includes('next')||x.innerText.includes('→')))return'button';return'wait';})()`,
                  returnByValue: true
                });
                const ss = surveyStatus.result?.result?.value;

                if (ss === 'completed') { surveyResult = 'completed'; break; }
                if (ss === 'screened') { surveyResult = 'screened'; break; }

                if (ss === 'radio') {
                  await cmd('Runtime.evaluate', {
                    expression: `(()=>{const r=[...document.querySelectorAll('input[type=\\"radio\\"]')].filter(x=>x.offsetParent!==null);if(!r.length)return;const body=document.body.innerText.toLowerCase();let picked=false;for(const cat of Object.keys(window.__PROFILE__)){const kws=window.__PROFILE__[cat];if(!cat||!body.includes(cat)&&!kws.some(k=>body.includes(k)))continue;for(const radio of r){const lb=document.querySelector('label[for=\\"'+radio.id+'\\"]')?.innerText?.toLowerCase()||radio.closest('label')?.innerText?.toLowerCase()||'';if(lb&&kws.some(k=>lb.includes(k))){radio.click();picked=true;break;}}if(picked)break;}if(!picked&&r[0])r[0].click();})()`,
                    returnByValue: true
                  });
                  await sleep(500);
                  await cmd('Runtime.evaluate', {
                    expression: `(()=>{const b=[...document.querySelectorAll('button')].filter(x=>x.offsetParent!==null);for(const x of b){const t=(x.innerText||'').toLowerCase();if(t.includes('next')||t.includes('continue')||t.includes('submit')||t==='→'){x.click();return true;}}return false;})()`,
                    returnByValue: true
                  });
                  answeredQuestions++;
                } else if (ss === 'button') {
                  await cmd('Runtime.evaluate', {
                    expression: `(()=>{const b=[...document.querySelectorAll('button')].filter(x=>x.offsetParent!==null);for(const x of b){const t=(x.innerText||'').toLowerCase();if(t.includes('next')||t.includes('continue')||t.includes('submit')){x.click();return;}}})()`,
                    returnByValue: true
                  });
                  await sleep(1500);
                } else {
                  await sleep(3000);
                }
              }

              if (!surveyResult) surveyResult = 'completed';
            }

            // Check balance after
            await sleep(3000);
            const postB = await cmd('Runtime.evaluate', {
              expression: `(()=>{const spans=[...document.querySelectorAll('span')];for(const s of spans){const m=s.innerText.trim().match(/^\\$\\s*([0-9.]+)\\s*USD$/);if(m)return parseFloat(m[1]);}return null;})()`,
              returnByValue: true
            });
            const finalBalance = postB.result?.result?.value;

            if (surveyResult === 'completed') {
              if (finalBalance !== null && initialBalance !== null) {
                const delta = finalBalance - initialBalance;
                log(`  ✓ COMPLETED! $${initialBalance.toFixed(2)} → $${finalBalance.toFixed(2)} (+$${delta.toFixed(2)})`);
                state.total_payments += delta;
                state.balance_history.push({ time: new Date().toISOString(), delta, balance: finalBalance, reward });
              } else {
                log(`  ✓ COMPLETED! (balance check: ${finalBalance !== null ? '$' + finalBalance.toFixed(2) : 'N/A'})`);
              }
              completed++;
            } else if (surveyResult === 'screened') {
              log(`  ○ Screened out (${answeredQuestions} Qs)`);
              screened++;
            } else {
              log(`  ✗ Failed (${answeredQuestions} Qs)`);
              failed++;
            }

            // Return to surveys tab
            await cmd('Runtime.evaluate', {
              expression: `document.querySelector('[data-test-id="surveys-nav"]')?.click()`,
              returnByValue: true
            });
            await sleep(2000);
          } catch (e) {
            log(`  Task error: ${e.message}`);
            failed++;
          }

          await sleep(2000 + Math.random() * 4000);
        }

        log(`\n=== Session: ${completed} done, ${screened} screened, ${failed} failed ===`);
        state.total_completed += completed;
        state.total_screened += screened;
        state.total_failed += failed;
        state.session_count += 1;
        saveState(state);
        resolve({ completed, screened, failed });

      } catch (e) {
        log(`CRITICAL: ${e.message}`);
        resolve({ completed: 0, screened: 0, failed: taskCount });
      }
      try { ws.close(); } catch {}
    });
    ws.on('error', e => {
      log(`WS error: ${e.message}`);
      resolve({ completed: 0, screened: 0, failed: taskCount });
    });
  });
}

async function main() {
  log('='.repeat(60));
  log('TOPSURVEYS PRODUCTION AGENT v3 — API-driven qualification');
  log('='.repeat(60));

  const state = loadState();
  log(`State: ${state.total_completed} done, ${state.total_screened} screened, $${state.total_payments.toFixed(2)} earned`);

  let sessionNum = state.session_count;

  while (true) {
    sessionNum++;
    const taskCount = 8 + Math.floor(Math.random() * 5);
    log(`\n${'='.repeat(60)}\nSESSION ${sessionNum} — ${taskCount} tasks\n${'='.repeat(60)}`);

    try {
      const wsUrl = await getCDPwsUrl();
      log(`Connected to CDP`);
      await runSession(wsUrl, taskCount, state);
    } catch (e) {
      log(`Session error: ${e.message} — retrying in 60s`);
      await sleep(60000);
      continue;
    }

    const pauseMin = 15 + Math.floor(Math.random() * 31);
    log(`\nPausing ${pauseMin} minutes...`);
    log(`Total: ${state.total_completed} done, $${state.total_payments.toFixed(2)} earned`);
    await sleep(pauseMin * 60 * 1000);
  }
}

main().catch(e => { log(`FATAL: ${e.message}`); process.exit(1); });
