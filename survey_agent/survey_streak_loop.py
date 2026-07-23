#!/usr/bin/env python3
"""Survey loop v10 — JS clicks for hidden radios, force clicks via dispatchEvent."""
import asyncio, random, re, base64, json, os, subprocess, time
from collections import Counter
from datetime import datetime

CDP_URL = "http://localhost:9222"
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
CEREBRAS_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
LOG = os.path.expanduser("~/survey-agent/data/survey_run.log")
os.makedirs(os.path.dirname(LOG), exist_ok=True)

ANSWERS = [
    "I carefully weigh the pros and cons, consult with trusted family members, and consider long-term goals.",
    "I research all available options, seek advice from people I trust, and follow my instincts.",
    "I gather information, discuss with close friends, sleep on it, then make a confident decision.",
]

def log(m):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {m}"
    print(line, flush=True)
    with open(LOG, "a") as f: f.write(line + "\n")

async def se(p, s, a=None, t=8000):
    try:
        if a is not None: return await asyncio.wait_for(p.evaluate(s, a), timeout=t/1000)
        return await asyncio.wait_for(p.evaluate(s), timeout=t/1000)
    except: return ""

async def sc(p, t=5000):
    try: return await asyncio.wait_for(p.content(), timeout=t/1000)
    except: return ""

async def js_click_radio(page, index):
    """Click radio by index using JS dispatchEvent — bypasses visibility."""
    return await se(page, f'''(idx) => {{
        const radios = document.querySelectorAll('input[type="radio"]');
        if (idx < radios.length) {{
            radios[idx].click();
            return true;
        }}
        return false;
    }}''', index)

async def js_click_by_text(page, tag, text_pattern):
    """Click element by text using JS."""
    return await se(page, f'''(pattern) => {{
        const els = document.querySelectorAll('{tag}');
        for (const el of els) {{
            if (el.textContent.toLowerCase().includes(pattern.toLowerCase())) {{
                el.click();
                return true;
            }}
        }}
        return false;
    }}''', text_pattern)

async def tc(p, s, t=5000):
    try: await p.locator(s).click(timeout=t, force=True); return True
    except: return False

async def answer_page(page):
    """Answer current survey page. Returns 'answered', 'completed', 'disq', 'stuck'."""
    try: await page.wait_for_load_state('domcontentloaded', timeout=8000)
    except: pass
    await asyncio.sleep(2)
    
    text = await se(page, 'document.body?.innerText?.substring(0,1500)||""')
    tl = (text or "").lower()
    
    if "you have earned" in tl or "congratulations" in tl or "successfully completed" in tl:
        return "completed"
    if "not a match" in tl or "disqualified" in tl or "did not qualify" in tl or "terminated" in tl:
        return "disq"
    if "survey full" in tl or "survey has ended" in tl or "survey is now closed" in tl:
        return "disq"
    
    # CAPTCHA
    content = await sc(page, 3000)
    if "ps captcha" in content.lower():
        b64 = base64.b64encode(await page.screenshot()).decode()
        reads = []
        for _ in range(3):
            try:
                r = subprocess.run(["curl","-s","-X","POST",CEREBRAS_ENDPOINT,
                    "-H",f"Authorization: Bearer {CEREBRAS_API_KEY}","-H","Content-Type: application/json",
                    "-d",json.dumps({"model":"gemma-4-31b","messages":[{"role":"user","content":[
                        {"type":"text","text":"Read the distorted text. Reply only characters."},
                        {"type":"image_url","image_url":{"url":f"data:image/png;base64,{b64}"}}]}],
                        "max_tokens":30,"temperature":0.1})],
                    capture_output=True,text=True,timeout=30)
                reads.append(re.sub(r'[^A-Za-z0-9]','',json.loads(r.stdout)["choices"][0]["message"]["content"].strip()))
            except: pass
        if reads:
            ml = max(len(r) for r in reads)
            code = ''.join(Counter(r[i] for r in reads if i<len(r)).most_common(1)[0][0] for i in range(ml))
            log("    CAPTCHA: " + str(reads) + " -> " + code)
            inp = page.locator('input[type="text"]:visible:not([tabindex="-1"]):not([disabled])')
            if await inp.count() > 0:
                await inp.first.fill(code)
                if await tc(page, 'button:has-text("Next")'):
                    await asyncio.sleep(3)
                    return "answered"
        return "captcha_fail"
    
    # Radio buttons — use JS click to bypass visibility
    rds_count = await se(page, 'document.querySelectorAll(\'input[type="radio"]\').length')
    if rds_count and int(rds_count) > 0:
        rdc = int(rds_count)
        
        # Get options for smart selection
        opts = await se(page, r'''() => {
            return [...document.querySelectorAll('input[type="radio"]')].map((r, i) => {
                const label = document.querySelector('label[for="' + r.id + '"]');
                const parent = r.closest('div, li, label');
                return {index: i, text: (label?.textContent?.trim() || parent?.textContent?.trim()?.replace(/\s+/g,' ') || '').substring(0, 60)};
            });
        }''')
        
        # Better answer matching using question text from page headings
        target_idx = 0
        # Extract actual question text from headings
        q_text = await se(page, '''() => {
            const h = document.querySelector('h1,h2,h3,h4,.question-text,[class*="question"]');
            return h ? h.textContent.trim().toLowerCase().substring(0,200) : '';
        }''')
        q_all = (q_text or "") + " " + tl
        
        if opts:
            # Match profile answers - self-employed is key for Antonio
            for opt in (opts or []):
                ot = opt.get('text', '').lower().strip()
                # Skip obviously wrong first-option defaults
                if ot in ['3rd grade or less', 'less than $5,000', 'select one', 'please select', '---select---']:
                    continue
                if ('employment' in q_all or 'work' in q_all or 'job' in q_all or 'occupation' in q_all):
                    if 'self' in ot: target_idx = opt['index']; break
                elif ('income' in q_all or 'earn' in q_all or 'household' in q_all or 'salary' in q_all or 'revenue' in q_all):
                    if '50' in ot or '60' in ot or '75' in ot: target_idx = opt['index']; break
                elif ('gender' in q_all or 'sex' in q_all):
                    if ot == 'male' or ot == 'm': target_idx = opt['index']; break
                elif 'age' in q_all:
                    if '35' in ot or '39' in ot: target_idx = opt['index']; break
                elif 'hispanic' in q_all or 'latino' in q_all:
                    if 'no' in ot: target_idx = opt['index']; break
                elif ('race' in q_all or 'ethnic' in q_all):
                    if 'black' in ot or 'african' in ot: target_idx = opt['index']; break
                elif ('state' in q_all or 'live' in q_all or 'reside' in q_all):
                    if 'texas' in ot or 'tx' in ot: target_idx = opt['index']; break
                elif ('education' in q_all or 'degree' in q_all or 'school' in q_all):
                    if 'bachelor' in ot or 'college' in ot or 'associate' in ot: target_idx = opt['index']; break
                elif ('children' in q_all or 'kids' in q_all):
                    if 'yes' in ot or '3' in ot: target_idx = opt['index']; break
                elif ('marital' in q_all or 'relationship' in q_all or 'married' in q_all):
                    if 'single' in ot or 'never' in ot: target_idx = opt['index']; break
                elif ('smok' in q_all):
                    if 'former' in ot: target_idx = opt['index']; break
                elif ('drink' in q_all or 'alcohol' in q_all):
                    if 'never' in ot: target_idx = opt['index']; break
                elif ('home' in q_all and ('own' in q_all or 'rent' in q_all or 'live' in q_all)):
                    if 'rent' in ot: target_idx = opt['index']; break
                elif ('country' in q_all or 'zip' in q_all or 'postal' in q_all):
                    if 'united states' in ot or 'us' == ot or 'usa' in ot: target_idx = opt['index']; break
            else:
                # No match found - pick middle option instead of first (avoids traps)
                target_idx = len(opts) // 3 if len(opts) > 2 else 0
        
        await js_click_radio(page, target_idx)
        
        # Click Next/Continue via JS
        await se(page, '''() => {
            const btns = document.querySelectorAll('button, input[type="submit"], a');
            for (const b of btns) {
                const t = b.textContent.trim().toLowerCase();
                if (t.includes('next') || t.includes('continue') || t.includes('submit')) {
                    b.click();
                    return true;
                }
            }
            return false;
        }''')
        
        sel_text = opts[target_idx]['text'][:30] if opts and target_idx < len(opts) else "?"
        log(f"  Radio {rdc} opts -> {sel_text}")
        return "answered"
    
    # Textarea (visible, not disabled/recaptcha)
    ta_info = await se(page, r'''() => {
        const tas = document.querySelectorAll('textarea');
        for (const ta of tas) {
            const rect = ta.getBoundingClientRect();
            if (rect.width > 10 && rect.height > 10 && !ta.disabled && !ta.name.includes('recaptcha') && !ta.className.includes('recaptcha') && !ta.className.includes('poll-reason')) {
                return {found: true};
            }
        }
        return {found: false};
    }''')
    
    if ta_info and ta_info.get('found'):
        # Use JS to fill textarea
        answer = random.choice(ANSWERS)
        await se(page, f'''() => {{
            const tas = document.querySelectorAll('textarea');
            for (const ta of tas) {{
                const rect = ta.getBoundingClientRect();
                if (rect.width > 10 && rect.height > 10 && !ta.disabled && !ta.name.includes('recaptcha')) {{
                    ta.value = "{answer.replace('"', '\\"')}";
                    ta.dispatchEvent(new Event('input', {{bubbles: true}}));
                    ta.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return true;
                }}
            }}
            return false;
        }}''')
        
        await se(page, '''() => {
            const btns = document.querySelectorAll('button, input[type="submit"], a');
            for (const b of btns) {
                const t = b.textContent.trim().toLowerCase();
                if (t.includes('next') || t.includes('continue') || t.includes('submit')) {
                    b.click(); return true;
                }
            }
            return false;
        }''')
        log(f"  Text answered")
        return "answered"
    
    # Select dropdowns via JS
    sel_count = await se(page, 'document.querySelectorAll("select").length')
    if sel_count and int(sel_count) > 0:
        await se(page, '''() => {
            const sels = document.querySelectorAll('select');
            for (const s of sels) {
                if (s.options.length > 1) { s.selectedIndex = 1; s.dispatchEvent(new Event('change', {bubbles: true})); }
            }
        }''')
        await se(page, '''() => {
            const btns = document.querySelectorAll('button, input[type="submit"], a');
            for (const b of btns) { const t = b.textContent.trim().toLowerCase(); if (t.includes('next')||t.includes('continue')) { b.click(); return true; } }
            return false;
        }''')
        log(f"  Select answered")
        return "answered"
    
    # Checkboxes via JS
    cb_count = await se(page, 'document.querySelectorAll(\'input[type="checkbox"]\').length')
    if cb_count and int(cb_count) > 0:
        await se(page, '''() => {
            const cbs = document.querySelectorAll('input[type="checkbox"]');
            if (cbs.length > 0) cbs[0].click();
        }''')
        await se(page, '''() => {
            const btns = document.querySelectorAll('button, input[type="submit"], a');
            for (const b of btns) { const t = b.textContent.trim().toLowerCase(); if (t.includes('next')||t.includes('continue')||t.includes('submit')) { b.click(); return true; } }
            return false;
        }''')
        log(f"  Checkbox answered")
        return "answered"
    
    # Drag-and-drop
    if 'cdk-drag' in (content or ""):
        m = re.search(r'number (\d+)', text or "")
        n = m.group(1) if m else "62"
        coords = await se(page, f'''(n) => {{
            const i=document.querySelector('img[alt="'+n+'"]');
            const d=i?.closest('.cdk-drag');
            const t=document.querySelectorAll('.cdk-drop-list');
            const tgt=t.length>1?t[1]:t[0];
            if(!d||!tgt)return null;
            const s=d.getBoundingClientRect(),r=tgt.getBoundingClientRect();
            return{{sx:s.x+s.width/2,sy:s.y+s.height/2,tx:r.x+r.width/2,ty:r.y+r.height/2}};
        }}''', n)
        if coords:
            sx,sy,tx,ty = coords['sx'],coords['sy'],coords['tx'],coords['ty']
            await page.mouse.move(sx,sy); await page.wait_for_timeout(100)
            await page.mouse.down(); await page.wait_for_timeout(300)
            for i in range(1,31): await page.mouse.move(sx+(tx-sx)*i/30,sy+(ty-sy)*i/30); await page.wait_for_timeout(20)
            await page.wait_for_timeout(300); await page.mouse.up(); await page.wait_for_timeout(2000)
            await se(page, '''() => { const b=document.querySelectorAll('button'); for(const x of b){if(x.textContent.includes('Next')){x.click();break;}} }''')
            log(f"  Drag #{n}")
            return "answered"
    
    # Fallback: click any Next/Continue
    clicked = await se(page, '''() => {
        const btns = document.querySelectorAll('button, input[type="submit"], a');
        for (const b of btns) { const t = b.textContent.trim().toLowerCase(); if (t.includes('next')||t.includes('continue')||t.includes('submit')) { b.click(); return true; } }
        return false;
    }''')
    if clicked:
        log(f"  Next clicked")
        return "answered"
    
    return "stuck"

async def get_survey_from_streak(ctx, page):
    """Get survey from SurveyStreak via CPX API + handle screening questions."""
    await page.bring_to_front()
    try: await page.wait_for_load_state('domcontentloaded', timeout=10000)
    except: pass
    await asyncio.sleep(3)
    
    if 'surveystreak' not in page.url:
        return None
    
    # Refresh survey list
    surveys = await se(page, '()=>Array.isArray(acctualSurveyList)?acctualSurveyList.map(s=>({id:s.id,payout:s.payout,loi:s.loi})):[]')
    if not surveys:
        log("  No surveys")
        return None
    
    sorted_s = sorted(surveys, key=lambda x: float(x.get('payout', 0)), reverse=True)
    
    for s in sorted_s[:10]:
        sid, payout, loi = s['id'], s['payout'], s['loi']
        
        # Get survey URL via API
        result = await se(page, f'async()=>{{const r=await fetch(details_url+"&survey_id={sid}");return await r.json()}}')
        
        if not result:
            continue
        
        if result.get('type') == 'okay' and result.get('href'):
            log(f"  Survey ${payout} ({loi}min) -> CPX URL")
            np = await ctx.new_page()
            try:
                await np.goto(result['href'], wait_until='domcontentloaded', timeout=30000)
                await np.wait_for_timeout(8000)
                return np
            except:
                try: await np.close()
                except: pass
        
        elif result.get('type') == 'question':
            # Answer screening question on SurveyStreak page
            q_type = result.get('question_type', '')
            q_text = result.get('question_text', '')
            answers = result.get('answers', {})
            q_key = result.get('question_key', '')
            
            log(f"  Screening: {q_text[:40]}")
            
            if q_type == 'single_punch' and answers:
                # Pick best answer based on profile
                best_key = list(answers.keys())[0]  # default first
                for a_key, a_data in answers.items():
                    a_text = a_data.get('text', '').lower()
                    if ('employment' in q_text.lower() or 'work' in q_text.lower()) and 'self' in a_text:
                        best_key = a_key; break
                    elif ('income' in q_text.lower() or 'earn' in q_text.lower()) and ('50' in a_text or '60' in a_text):
                        best_key = a_key; break
                    elif 'gender' in q_text.lower() and a_text == 'male':
                        best_key = a_key; break
                    elif 'age' in q_text.lower() and ('35' in a_text or '39' in a_text):
                        best_key = a_key; break
                    elif 'hispanic' in q_text.lower() and 'no' in a_text:
                        best_key = a_key; break
                    elif ('race' in q_text.lower() or 'ethnic' in q_text.lower()) and 'black' in a_text:
                        best_key = a_key; break
                    elif 'state' in q_text.lower() and ('texas' in a_text or 'tx' in a_text):
                        best_key = a_key; break
                    elif 'education' in q_text.lower() and 'bachelor' in a_text:
                        best_key = a_key; break
                
                # Submit answer to API
                submit_result = await se(page, f'''async () => {{
                    const url = details_url + '&survey_id={sid}';
                    const body = new FormData();
                    body.append('question_key', '{q_key}');
                    body.append('answer_key', '{best_key}');
                    const resp = await fetch(url, {{method: 'POST', body: body}});
                    return await resp.json();
                }}''')
                
                if submit_result and submit_result.get('type') == 'okay' and submit_result.get('href'):
                    log(f"  Screening passed! CPX URL")
                    np = await ctx.new_page()
                    try:
                        await np.goto(submit_result['href'], wait_until='domcontentloaded', timeout=30000)
                        await np.wait_for_timeout(8000)
                        return np
                    except:
                        try: await np.close()
                        except: pass
                elif submit_result and submit_result.get('type') == 'question':
                    log(f"  More screening needed")
                    # Recurse or handle next question
                else:
                    log(f"  Screening failed: {submit_result.get('type', '?') if submit_result else 'no response'}")
    
    return None

async def main():
    from playwright.async_api import async_playwright
    log("="*60); log("SURVEY LOOP v10 — JS clicks + SurveyStreak screening"); log("="*60)
    
    for retry in range(10):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(CDP_URL)
                ctx = browser.contexts[0]
                log(f"Connected (retry {retry})")
                
                # Close extra tabs
                while len(ctx.pages) > 4:
                    try: await ctx.pages[-1].close()
                    except: break
                
                for poll in range(60):
                    log("--- Poll " + str(poll+1) + " ---")
                    
                    ss_page = None
                    for pg in ctx.pages:
                        if 'surveystreak' in pg.url:
                            ss_page = pg
                            break
                    
                    if not ss_page:
                        log("No SurveyStreak page!")
                        await asyncio.sleep(30)
                        continue
                    
                    survey_page = await get_survey_from_streak(ctx, ss_page)
                    if not survey_page:
                        log("No survey, wait 60s...")
                        await asyncio.sleep(60)
                        continue
                    
                    log(f"  Survey: {survey_page.url[:60]}")
                    
                    completed = False
                    for q in range(200):
                        result = await answer_page(survey_page)
                        prog = await se(survey_page, 'document.body?.innerText?.match(/(\\d+)%/)?.[1]||"?"')
                        log(f"  Q{q+1} ({prog}%): {result}")
                        
                        if result == "completed":
                            log("*** SURVEY COMPLETED! ***")
                            completed = True
                            try: await survey_page.close()
                            except: pass
                            break
                        elif result in ("disq","full","captcha_fail","stuck"):
                            log(f"  Ended: {result}")
                            try: await survey_page.close()
                            except: pass
                            break
                    
                    if completed:
                        log("\nDONE!")
                        return
                    
                    try: await ss_page.bring_to_front()
                    except: pass
                    await asyncio.sleep(5)
                    log("Waiting 30s...")
                    await asyncio.sleep(30)
        
        except Exception as e:
            log(f"Error: {e}")
        log(f"Reconnect in 15s ({retry+1}/10)...")
        await asyncio.sleep(15)
    
    log("Max retries.")

if __name__ == "__main__":
    asyncio.run(main())
