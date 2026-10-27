#!/usr/bin/env node
/**
 * TopSurveys Agent v9 — agent-browser based
 * Uses ref-based snapshots instead of raw CDP
 */
const { execSync } = require('child_process');
const fs = require('fs');

const BALANCE_FILE = __dirname + '/../../data/ab_balance.json';
const LOG_FILE = __dirname + '/../../data/ab_run.log';

// Survey profile for answering qualification questions
const PROFILE = {
  jobTitle: 'C-Level',        // CTO
  household: 'Rented house',   // Austin TX, renting
  age: '35-44',                // 39M
  gender: 'Male',
  ethnicity: 'Black or African American',
  income: '$50,000 - $74,999',
  education: "Bachelor's degree",
  employment: 'Self-employed',
  children: 'Yes',
  maritalStatus: 'Single',
};

function log(msg) {
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19);
  const line = `[${ts}] ${msg}`;
  console.log(line);
  fs.appendFileSync(LOG_FILE, line + '\n');
}

function ab(cmd, timeout = 15000) {
  try {
    return execSync(`agent-browser ${cmd}`, { encoding: 'utf8', timeout }).trim();
  } catch (e) {
    return e.stdout?.trim() || `ERROR: ${e.message.slice(0, 100)}`;
  }
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function randomDelay(min = 1000, max = 3000) {
  return sleep(min + Math.random() * (max - min));
}

// Parse survey cards from snapshot
function parseCards(snapshot) {
  const cards = [];
  const lines = snapshot.split(/\r?\n/);
  for (const line of lines) {
    const m = line.match(/generic "(.+?)" \[ref=(e\d+)\] clickable/);
    if (m) {
      const text = m[1];
      const ref = m[2];
      // Parse: "8 min$ 0.90USDNew" or "5 min$ 0.18USD5 (42)"
      const pm = text.match(/(\d+)\s*min/);
      const dm = text.match(/\$\s*([\d.]+)/);
      if (pm && dm) {
        cards.push({
          ref,
          text,
          minutes: parseInt(pm[1]),
          payout: parseFloat(dm[1]),
        });
      }
    }
  }
  return cards.sort((a, b) => b.payout - a.payout);
}

// Answer a qualification question
function answerQualification(snapshot) {
  const lines = snapshot.split(/\r?\n/);
  
  // Get the question
  let question = '';
  for (const line of lines) {
    const qm = line.match(/heading "(.+?)" \[level=2/);
    if (qm) { question = qm[1]; break; }
  }
  
  // Get radio options
  const options = [];
  for (const line of lines) {
    const om = line.match(/radio "(.+?)" \[checked=false, ref=(e\d+)\]/);
    if (om) options.push({ text: om[1], ref: om[2] });
  }
  
  // If no radios, try checkboxes
  if (!options.length) {
    for (const line of lines) {
      const cm = line.match(/checkbox "(.+?)" \[checked=false, ref=(e\d+)\]/);
      if (cm) options.push({ text: cm[1], ref: cm[2] });
    }
  }
  
  if (!options.length) return null;
  
  // Simple keyword matching based on profile
  const q = question.toLowerCase();
  let bestRef = options[0].ref; // default to first option
  
  if (q.includes('job title') || q.includes('responsibility')) {
    const opt = options.find(o => /c-level|ceo|cto|owner|partner|president/i.test(o.text));
    if (opt) bestRef = opt.ref;
  } else if (q.includes('household') || q.includes('housing')) {
    const opt = options.find(o => /rented house/i.test(o.text));
    if (opt) bestRef = opt.ref;
  } else if (q.includes('age') || q.includes('old')) {
    const opt = options.find(o => /35-44|35.*44/i.test(o.text));
    if (opt) bestRef = opt.ref;
  } else if (q.includes('gender') || q.includes('sex')) {
    const opt = options.find(o => /^male$/i.test(o.text));
    if (opt) bestRef = opt.ref;
  } else if (q.includes('income') || q.includes('earn')) {
    const opt = options.find(o => /50.*74|50,000.*74,999/i.test(o.text));
    if (opt) bestRef = opt.ref;
  } else if (q.includes('education') || q.includes('degree')) {
    const opt = options.find(o => /bachelor/i.test(o.text));
    if (opt) bestRef = opt.ref;
  } else if (q.includes('employ') || q.includes('work')) {
    const opt = options.find(o => /self-employ/i.test(o.text));
    if (opt) bestRef = opt.ref;
  } else if (q.includes('children') || q.includes('kid')) {
    const opt = options.find(o => /^yes$/i.test(o.text));
    if (opt) bestRef = opt.ref;
  } else if (q.includes('marital') || q.includes('married')) {
    const opt = options.find(o => /single/i.test(o.text));
    if (opt) bestRef = opt.ref;
  } else if (q.includes('race') || q.includes('ethnic')) {
    const opt = options.find(o => /black|african/i.test(o.text));
    if (opt) bestRef = opt.ref;
  } else if (q.includes('phone') || q.includes('streaming') || q.includes('app')) {
    const opt = options.find(o => /^yes$/i.test(o.text));
    if (opt) bestRef = opt.ref;
  } else if (q.includes('bank')) {
    const opt = options.find(o => /chase|wells fargo|bank of america|citibank/i.test(o.text));
    if (opt) bestRef = opt.ref;
    else bestRef = options[Math.floor(options.length / 2)].ref;
  } else if (q.includes('grocery') || q.includes('shop')) {
    const opt = options.find(o => /most|all|almost all/i.test(o.text));
    if (opt) bestRef = opt.ref;
  } else if (q.includes('language') || q.includes('speak')) {
    const opt = options.find(o => /english/i.test(o.text));
    if (opt) bestRef = opt.ref;
  } else if (q.includes('hispanic') || q.includes('latino')) {
    const opt = options.find(o => /^no$/i.test(o.text));
    if (opt) bestRef = opt.ref;
  } else {
    // Default: pick a middle option (not first, not last)
    const mid = Math.floor(options.length / 2);
    bestRef = options[mid].ref;
  }
  
  return { question, bestRef, optionCount: options.length };
}

// Find submit button in snapshot
function findSubmitButton(snapshot) {
  const lines = snapshot.split(/\r?\n/);
  // Look for unlabeled button (no text, e.g., "button [ref=e59]")
  for (const line of lines) {
    const m = line.match(/button\s+\[ref=(e\d+)\]/);
    if (m) return m[1];
  }
  // Look for "Next" or "Submit" or "Continue" buttons
  for (const line of lines) {
    const m = line.match(/button "(Next|Submit|Continue)" \[ref=(e\d+)\]/);
    if (m) return m[2];
  }
  return null;
}

// Main survey loop
async function runSession(taskCount = 8) {
  log(`Starting session with ${taskCount} tasks`);
  
  // Read balance
  let balance = 0;
  try {
    balance = JSON.parse(fs.readFileSync(BALANCE_FILE, 'utf8')).balance || 0;
  } catch {}
  log(`Current balance: $${balance.toFixed(2)}`);
  
  let completed = 0, screened = 0, failed = 0;
  
  for (let i = 0; i < taskCount; i++) {
    log(`\n--- Task ${i + 1}/${taskCount} ---`);
    
    try {
      // Refresh surveys (just navigate, don't create new tab)
      ab('open https://app.topsurveys.app/surveys');
      await randomDelay(4000, 6000);
      
      const snapshot = ab('snapshot -i');
      const cards = parseCards(snapshot);
      
      if (!cards.length) {
        log('No survey cards found');
        failed++;
        continue;
      }
      
      // Pick a random card from top 10
      const pick = cards[Math.floor(Math.random() * Math.min(10, cards.length))];
      log(`Picked: ${pick.minutes}min $${pick.payout.toFixed(2)}`);
      
      // Click the card
      ab(`click @${pick.ref}`);
      
      // Wait for qualification modal (up to 10s)
      let qualSnap = '';
      for (let w = 0; w < 5; w++) {
        await randomDelay(1500, 2500);
        qualSnap = ab('snapshot -i');
        if (qualSnap.includes('Qualification') || qualSnap.includes('403') || 
            qualSnap.includes('Forbidden') || qualSnap.includes('qualified') ||
            qualSnap.includes('Qualified')) break;
      }
      
      if (qualSnap.includes('403') || qualSnap.includes('Forbidden')) {
        log('Blocked by survey provider (403)');
        failed++;
        continue;
      }
      
      // Handle direct qualification (no questions asked)
      if (qualSnap.includes('Participate')) {
        log('Directly qualified!');
        const pLines = qualSnap.split(/\r?\n/);
        let participateRef = null;
        for (const line of pLines) {
          const m = line.match(/button "Participate" \[ref=(e\d+)\]/);
          if (m) { participateRef = m[1]; break; }
        }
        if (participateRef) {
          ab(`click @${participateRef}`);
          await randomDelay(5000, 8000);
          const surveyResult = await handleSurvey();
          if (surveyResult === 'completed') { completed++; log('Survey completed!'); }
          else if (surveyResult === 'screened') { screened++; log('Screened out'); }
          else { failed++; log(`Survey failed: ${surveyResult}`); }
        }
        continue;
      }
      
      if (qualSnap.includes('Qualification')) {
        const answer = answerQualification(qualSnap);
        if (!answer) {
          log('Could not parse qualification question');
          // Try to close modal
          ab(`eval 'document.querySelector("[class*=close]")?.click()'`);
          await randomDelay(500, 1000);
          failed++;
          continue;
        }
        
        log(`Q: ${answer.question.slice(0, 60)}... (${answer.optionCount} opts)`);
        
        // Click the answer
        ab(`click @${answer.bestRef}`);
        await randomDelay(1000, 2000);
        
        // Find and click submit (re-snapshot to get fresh refs)
        const afterSnap = ab('snapshot -i');
        const submitRef = findSubmitButton(afterSnap);
        if (submitRef) {
          log(`  Submit: @${submitRef}`);
          ab(`click @${submitRef}`);
          await randomDelay(2000, 4000);
        } else {
          log('  No submit button found');
        }
        
        // Check for "Qualified" or disqualification
        const resultSnap = ab('snapshot -i');
        
        if (resultSnap.includes("qualified") || resultSnap.includes("Qualified")) {
          log('Qualified! Clicking Participate...');
          
          // Find Participate button
          const pLines = resultSnap.split(/\r?\n/);
          let participateRef = null;
          for (const line of pLines) {
            const m = line.match(/button "Participate" \[ref=(e\d+)\]/);
            if (m) { participateRef = m[1]; break; }
          }
          
          if (participateRef) {
            ab(`click @${participateRef}`);
            await randomDelay(5000, 8000);
            
            // Now we're in the actual survey
            const surveyResult = await handleSurvey();
            if (surveyResult === 'completed') {
              completed++;
              log('Survey completed!');
            } else if (surveyResult === 'screened') {
              screened++;
              log('Screened out');
            } else {
              failed++;
              log('Survey failed');
            }
          }
        } else {
          log('Did not qualify');
          screened++;
        }
      } else if (qualSnap.includes('403') || qualSnap.includes('Forbidden')) {
        log('Blocked by survey provider (403)');
        failed++;
      } else {
        log('No qualification modal — unexpected state');
        failed++;
      }
      
    } catch (e) {
      log(`Error: ${e.message.slice(0, 100)}`);
      failed++;
    }
    
    await randomDelay(2000, 5000);
  }
  
  log(`\nSession done: ${completed} completed, ${screened} screened, ${failed} failed`);
  return { completed, screened, failed };
}

// Handle actual survey questions (after qualifying)
async function handleSurvey(maxQuestions = 30) {
  for (let q = 0; q < maxQuestions; q++) {
    await randomDelay(2000, 4000);
    
    const url = ab('get url');
    const snapshot = ab('snapshot -i');
    
    // Check if redirected back to TopSurveys (survey complete)
    if (url.includes('topsurveys.app/surveys')) {
      return 'completed';
    }
    
    // Check if redirected to a random page (screened out)
    if (!url.includes('topsurveys') && !url.includes('xurway') && 
        !url.includes('questionlab') && !url.includes('itrafficcenter')) {
      // Might be a redirect/screenout
      if (snapshot.includes('here') || snapshot.length < 100) {
        return 'screened';
      }
    }
    
    // Check for CAPTCHA
    if (snapshot.includes('enter the text') || snapshot.includes('captcha') || 
        snapshot.includes('Enter Text Here')) {
      log('CAPTCHA detected — taking screenshot...');
      const shotPath = `/tmp/captcha_${Date.now()}.png`;
      ab(`screenshot ${shotPath}`);
      // TODO: solve CAPTCHA with vision
      log(`CAPTCHA screenshot saved: ${shotPath}`);
      return 'captcha';
    }
    
    // Check for radio buttons
    const radios = [];
    const lines = snapshot.split(/\r?\n/);
    for (const line of lines) {
      const m = line.match(/radio "(.+?)" \[checked=false, ref=(e\d+)\]/);
      if (m) radios.push({ text: m[1], ref: m[2] });
    }
    
    if (radios.length) {
      // Pick a reasonable answer (middle option for now)
      const pick = radios[Math.floor(radios.length / 2)];
      log(`  Q${q + 1}: ${radios.length} options, picking "${pick.text.slice(0, 40)}"`);
      ab(`click @${pick.ref}`);
      await randomDelay(500, 1000);
      
      // Find submit/next
      const afterSnap = ab('snapshot -i');
      const submitRef = findSubmitButton(afterSnap);
      if (submitRef) {
        ab(`click @${submitRef}`);
      }
      continue;
    }
    
    // Check for checkboxes
    const checks = [];
    for (const line of lines) {
      const m = line.match(/checkbox "(.+?)" \[checked=false, ref=(e\d+)\]/);
      if (m) checks.push({ text: m[1], ref: m[2] });
    }
    
    if (checks.length) {
      // Select 2-3 random options
      const count = Math.min(3, checks.length);
      const shuffled = checks.sort(() => Math.random() - 0.5);
      for (let c = 0; c < count; c++) {
        ab(`click @${shuffled[c].ref}`);
        await randomDelay(300, 600);
      }
      const afterSnap = ab('snapshot -i');
      const submitRef = findSubmitButton(afterSnap);
      if (submitRef) ab(`click @${submitRef}`);
      continue;
    }
    
    // Check for text inputs
    const textInputs = [];
    for (const line of lines) {
      const m = line.match(/textbox "(.+?)" \[ref=(e\d+)\]/);
      if (m) textInputs.push({ label: m[1], ref: m[2] });
    }
    
    if (textInputs.length) {
      // Skip text input surveys (too complex)
      log('Text input survey — skipping');
      return 'screened';
    }
    
    // Check for select/dropdown
    const selects = [];
    for (const line of lines) {
      const m = line.match(/combobox "(.+?)" \[ref=(e\d+)\]/);
      if (m) selects.push({ label: m[1], ref: m[2] });
    }
    
    if (selects.length) {
      log('Dropdown survey — skipping');
      return 'screened';
    }
    
    // Unknown question type
    log(`Unknown question type at Q${q + 1}`);
    await randomDelay(1000, 2000);
  }
  
  return 'timeout';
}

// Auth setup
function setupAuth() {
  log('Setting up auth cookies...');
  // Go directly to surveys page (cookies from prior session persist)
  ab('open https://app.topsurveys.app/surveys');
  execSync('sleep 5');
  
  const url = ab('get url');
  if (url.includes('app-login')) {
    log('On login page — injecting cookies...');
    ab(`eval 'document.cookie = "auth-token=10943745%7CX1ARRCAR8zbFIhHt9VBAIKibSzFgqoWwRgAXUy4O44235d09; path=/; SameSite=Lax"; "1"'`);
    ab(`eval 'document.cookie = "auth-provider=google; path=/; SameSite=Lax"; "2"'`);
    ab(`eval 'document.cookie = "ps-uuid=a0763cb5-72e3-4099-a97c-84de8bccc4d4; path=/; SameSite=Lax"; "3"'`);
    ab(`eval 'document.cookie = "ps-shash=8b3c00397ae31b0993cfb58df347b235; path=/; SameSite=Lax"; "4"'`);
    ab(`eval 'document.cookie = "ps-uhash=9a48cc12-6a2d-4583-9c70-6dcfabae7576; path=/; SameSite=Lax"; "5"'`);
    ab(`eval 'document.cookie = "ps-app=Y8wsEPawxK; path=/; SameSite=Lax"; "6"'`);
    ab(`eval 'document.cookie = "ps-di=397479eaf414ce0cb93d9ca747c05dc21853596066d6c01a390ec3dfa22e11a370052548f; path=/; SameSite=Lax"; "7"'`);
    ab('open https://app.topsurveys.app/surveys');
    execSync('sleep 5');
  }
  log('Auth complete');
}

async function main() {
  log('='.repeat(60));
  log('TOPSURVEYS AGENT v9 — agent-browser');
  log('='.repeat(60));
  
  setupAuth();
  
  // Verify we're logged in
  const snap = ab('snapshot -i');
  if (!snap.includes('Cashout')) {
    log('ERROR: Not logged in — auth failed');
    process.exit(1);
  }
  log('Logged in successfully');
  
  // Run sessions in a loop
  let session = 0;
  while (true) {
    session++;
    const taskCount = 8 + Math.floor(Math.random() * 5);
    log(`\n${'='.repeat(60)}\nSESSION ${session} — ${taskCount} tasks\n${'='.repeat(60)}`);
    
    try {
      await runSession(taskCount);
    } catch (e) {
      log(`Session error: ${e.message.slice(0, 100)}`);
    }
    
    const pauseMin = 15 + Math.floor(Math.random() * 31);
    log(`\nPausing ${pauseMin} minutes...`);
    await sleep(pauseMin * 60 * 1000);
  }
}

main().catch(e => { log(`FATAL: ${e.message}`); process.exit(1); });
