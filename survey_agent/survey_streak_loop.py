#!/usr/bin/env python3
"""Survey loop v12 — clickSurvey -> answer screening -> Start Survey -> answer questions."""
import asyncio, random, re, base64, json, os, subprocess
from collections import Counter
from datetime import datetime

CDP_URL = "http://localhost:9222"
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
CEREBRAS_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
LOG = os.path.expanduser("~/survey-agent/data/survey_run.log")
os.makedirs(os.path.dirname(LOG), exist_ok=True)

PROFILE = {
    'employment': ['self-employed', 'self employed'], 'income': ['50', '60', '75', '74,999'],
    'gender': ['male'], 'age': ['35', '39'], 'hispanic': ['no'], 'race': ['black', 'african'],
    'state': ['texas', 'tx'], 'education': ['bachelor', 'college'], 'children': ['yes'],
    'marital': ['single', 'never'], 'smoke': ['former'], 'drink': ['never'], 'home': ['rent'],
    'country': ['united states', 'usa'], 'grocery': ['yes'], 'shop': ['yes'],
    'device': ['smartphone', 'laptop', 'desktop'], 'online': ['daily', 'weekly', 'yes'],
    'owner': ['rent'], 'live': ['texas'], 'how many': ['3'],
}

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

def match_answer(q, opts):
    q = q.lower()
    for opt in opts:
        ot = opt.get('text', '').lower().strip()
        if not ot or ot in ['select one', 'please select', '---select---']:
            continue
        if ot in ['3rd grade or less', 'less than $5,000']:
            continue
        for cat, kw in PROFILE.items():
            if cat in q or any(k in q for k in kw):
                if any(k in ot for k in kw):
                    return opt['index']
    valid = [o for o in opts if o.get('text', '').strip()]
    return valid[len(valid)//3]['index'] if len(valid) > 2 else 0

async def answer_screening(page, max_q=20):
    """Answer screening questions in SurveyStreak modal. Returns True if qualified."""
    for sq in range(max_q):
        await asyncio.sleep(3)
        
        # Check if Start Survey is visible (qualified) — multiple methods
        start_vis = await page.evaluate('document.querySelector("#start-survey-button")?.offsetParent !== null || (document.querySelector("#start-survey-button") && getComputedStyle(document.querySelector("#start-survey-button")).display !== "none")')
        if start_vis:
            return True
        # Also check modal text for "qualified"  
        modal_text = await page.evaluate('document.querySelector("#survey-modal")?.innerText?.toLowerCase() || ""')
        if modal_text and ('qualified' in modal_text or 'begin the survey' in modal_text):
            return True
        
        # Check for not-qualified
        modal_text = await se(page, 'document.querySelector("#survey-modal")?.innerText?.substring(0,300) || ""')
        if modal_text and ('not a match' in modal_text.lower() or 'not qualified' in modal_text.lower() or 'unfortunately' in modal_text.lower()):
            return False
        
        # Check for radio buttons in survey modal
        rds_info = await se(page, r'''() => {
            const m = document.querySelector('#survey-modal');
            if (!m || getComputedStyle(m).display === 'none') return null;
            const radios = m.querySelectorAll('input[type="radio"]');
            if (radios.length === 0) return null;
            return {
                q: m.innerText?.substring(0, 200) || '',
                opts: [...radios].map((r, i) => {
                    const label = m.querySelector('label[for="' + r.id + '"]');
                    return {index: i, text: (label?.textContent?.trim() || r.closest('div')?.textContent?.trim()?.replace(/\s+/g,' ') || '').substring(0, 80)};
                })
            };
        }''')
        
        if rds_info:
            target = match_answer(rds_info.get('q', ''), rds_info.get('opts', []))
            await se(page, f'''(idx) => {{
                const m = document.querySelector('#survey-modal');
                const r = m?.querySelectorAll('input[type="radio"]');
                if (r && idx < r.length) r[idx].click();
            }}''', target)
            # Click Submit
            await se(page, '''() => {
                const b = document.querySelector('#submit-button-cpx');
                if (b && getComputedStyle(b).display !== 'none') { b.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true})); return true; }
                const btns = document.querySelectorAll('#survey-modal button');
                for (const x of btns) { if (getComputedStyle(x).display !== 'none') { x.click(); return true; } }
                return false;
            }''')
            opts = rds_info.get('opts', [])
            sel = opts[target]['text'][:25] if target < len(opts) else "?"
            log(f"    Screen Q{sq+1}: radio -> {sel}")
            continue
        
        # Check for checkboxes in modal — select only 2-3 relevant ones
        cb_count = await se(page, r'''() => {
            const m = document.querySelector('#survey-modal');
            if (!m || getComputedStyle(m).display === 'none') return 0;
            const cbs = m.querySelectorAll('input[type="checkbox"]');
            if (cbs.length === 0) return 0;
            // Check for "None of these" first — skip it
            const labels = [...cbs].map(cb => {
                const label = m.querySelector('label[for="' + cb.id + '"]');
                return {cb, text: (label?.textContent?.trim() || cb.closest('div')?.textContent?.trim() || '').toLowerCase()};
            });
            // Select relevant items (2-3 max)
            const keywords = ['savings', 'checking', 'retirement', '401k', 'ira', 'credit card', 'investment', 'stock', 'bond', 'mortgage', 'none of these'];
            let selected = 0;
            for (const item of labels) {
                if (selected >= 3) break;
                if (item.text.includes('none of')) { item.cb.click(); selected++; break; }
                for (const kw of keywords) {
                    if (item.text.includes(kw)) { item.cb.click(); selected++; break; }
                }
            }
            // If nothing matched, select first 2
            if (selected === 0) {
                for (let i = 0; i < Math.min(2, cbs.length); i++) {
                    if (!cbs[i].checked) { cbs[i].click(); selected++; }
                }
            }
            return cbs.length;
        }''')
        
        if cb_count and int(cb_count) > 0:
            await se(page, '''() => {
                const b = document.querySelector('#submit-button-cpx');
                if (b && getComputedStyle(b).display !== 'none') {
                    b.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                    return true;
                }
                return false;
            }''')
            log(f"    Screen Q{sq+1}: {cb_count} checkboxes")
            continue
        
        # Textarea in modal
        ta_filled = await page.evaluate(r'''() => {
            const m = document.querySelector('#survey-modal');
            if (!m || getComputedStyle(m).display === 'none') return false;
            const tas = m.querySelectorAll('textarea');
            for (const ta of tas) {
                if (ta.disabled) continue;
                // Postal code
                const q = m.innerText?.toLowerCase() || '';
                if (q.includes('postal') || q.includes('zip')) {
                    ta.value = '78701';
                } else if (q.includes('name')) {
                    ta.value = 'Antonio';
                } else {
                    ta.value = 'I carefully weigh the pros and cons, consult with family, and consider long-term goals.';
                }
                ta.dispatchEvent(new Event('input', {bubbles: true}));
                ta.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
            return false;
        }''')
        
        if ta_filled:
            await page.evaluate('''() => {
                const b = document.querySelector('#submit-button-cpx');
                if (b && getComputedStyle(b).display !== 'none') {
                    b.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                }
            }''')
            log(f"    Screen Q{sq+1}: textarea filled")
            continue
        
        # Number/text input in modal
        input_filled = await se(page, r'''() => {
            const m = document.querySelector('#survey-modal');
            if (!m || getComputedStyle(m).display === 'none') return false;
            const nums = m.querySelectorAll('input[type="number"]');
            for (const n of nums) {
                // Income question — fill with 55000
                n.value = 55000;
                n.dispatchEvent(new Event('input', {bubbles: true}));
                n.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
            const texts = m.querySelectorAll('input[type="text"]:not([type="hidden"])');
            for (const t of texts) {
                if (t.type === 'hidden') continue;
                t.value = '55000';
                t.dispatchEvent(new Event('input', {bubbles: true}));
                t.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
            // Also try selects in modal
            const sels = m.querySelectorAll('select');
            for (const s of sels) {
                if (s.options.length > 1) { s.selectedIndex = 1; s.dispatchEvent(new Event('change', {bubbles: true})); }
                return true;
            }
            return false;
        }''')
        
        if input_filled:
            # Click Next
            await se(page, '''() => {
                const b = document.querySelector('#submit-button-cpx');
                if (b && getComputedStyle(b).display !== 'none') {
                    b.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                    return true;
                }
                return false;
            }''')
            log(f"    Screen Q{sq+1}: number/text input filled")
            continue
        
        # No question found
        log(f"    Screen Q{sq+1}: waiting...")
    
    # Final check
    return bool(await se(page, '''() => {
        const b = document.querySelector('#start-survey-button');
        return b && getComputedStyle(b).display !== 'none';
    }'''))

async def get_survey(ctx, page):
    """Get survey via clickSurvey -> screening -> Start Survey."""
    await page.bring_to_front()
    try: await page.wait_for_load_state('domcontentloaded', timeout=10000)
    except: pass
    await asyncio.sleep(3)
    
    if 'surveystreak' not in page.url:
        return None
    
    surveys = await se(page, '()=>Array.isArray(acctualSurveyList)?acctualSurveyList.map(s=>({id:s.id,payout:s.payout,loi:s.loi})):[]')
    if not surveys:
        log("  No surveys")
        return None
    
    sorted_s = sorted(surveys, key=lambda x: float(x.get('payout', 0)), reverse=True)
    
    for s in sorted_s[:8]:
        sid, payout, loi = s['id'], s['payout'], s['loi']
        log(f"  Trying #{sid} (${payout}, {loi}min)")
        
        initial = len(ctx.pages)
        
        try:
            await page.evaluate(f'clickSurvey("{sid}")')
        except:
            log(f"  clickSurvey error")
            continue
        await asyncio.sleep(8)
        
        # Answer screening questions
        qualified = await answer_screening(page)
        
        if qualified:
            log(f"  Qualified!")
            # Click Start Survey
            await se(page, '''() => {
                const b = document.querySelector('#start-survey-button');
                if (b) b.click();
            }''')
            await asyncio.sleep(10)
            
            if len(ctx.pages) > initial:
                np = ctx.pages[-1]
                try: await np.wait_for_load_state('domcontentloaded', timeout=15000)
                except: pass
                await np.wait_for_timeout(3000)
                url = np.url
                log(f"  Survey: {url[:60]}")
                
                if 'researchnow' in url or 'surveymyopinion' in url:
                    log(f"  Skipping researchnow")
                    try: await np.close()
                    except: pass
                    continue
                
                return np
        else:
            log(f"  Not qualified")
        
        await asyncio.sleep(2)
    
    return None

async def answer_survey(page):
    """Answer a question on the survey page."""
    try: await page.wait_for_load_state('domcontentloaded', timeout=8000)
    except: pass
    await asyncio.sleep(2)
    
    text = await se(page, 'document.body?.innerText?.substring(0,1500)||""')
    tl = (text or "").lower()
    url = page.url
    
    if "you have earned" in tl or "congratulations" in tl or "successfully completed" in tl:
        return "completed"
    if "not a match" in tl or "disqualified" in tl or "did not qualify" in tl or "terminated" in tl:
        return "disq"
    if "survey full" in tl or "survey has ended" in tl:
        return "disq"
    if 'researchnow' in url or 'surveymyopinion' in url:
        return "stuck"
    
    content = await se(page, 'document.documentElement?.outerHTML?.substring(0,5000)||""')
    
    # CAPTCHA
    if "ps captcha" in (content or "").lower():
        b64 = base64.b64encode(await page.screenshot()).decode()
        reads = []
        for _ in range(3):
            try:
                r = subprocess.run(["curl","-s","-X","POST",CEREBRAS_ENDPOINT,
                    "-H","Authorization: Bearer " + CEREBRAS_API_KEY,"-H","Content-Type: application/json",
                    "-d",json.dumps({"model":"gemma-4-31b","messages":[{"role":"user","content":[
                        {"type":"text","text":"This image contains a CAPTCHA code with 5-7 characters (letters and numbers). Read EVERY character carefully, including any that might be hard to see. Reply ONLY with the characters, nothing else. If unsure about a character, make your best guess."},
                        {"type":"image_url","image_url":{"url":"data:image/png;base64,"+b64}}]}],
                        "max_tokens":30,"temperature":0.1})],
                    capture_output=True,text=True,timeout=30)
                reads.append(re.sub(r'[^A-Za-z0-9]','',json.loads(r.stdout)["choices"][0]["message"]["content"].strip()))
            except: pass
        if reads:
            ml = max(len(r) for r in reads)
            code = ''.join(Counter(r[i] for r in reads if i<len(r)).most_common(1)[0][0] for i in range(ml))
            log("    CAPTCHA: " + str(reads) + " -> " + code)
            await se(page, '(c)=>{const i=document.querySelector(\'input[type="text"]:not([tabindex="-1"]):not([disabled])\');if(i){i.value=c;i.dispatchEvent(new Event("input",{bubbles:true}))}}', code)
            await se(page, '''()=>{const b=document.querySelectorAll('button');for(const x of b){if(x.textContent.toLowerCase().includes('next')){x.click();break}}}''')
            await asyncio.sleep(3)
            return "answered"
        return "captcha_fail"
    
    # PureSpectrum CAPTCHA (alpha-numeric-input)
    captcha_input = await se(page, r'''() => {
        const inp = document.querySelector('input.alpha-numeric-input, input[type="text"][class*="alpha"]');
        if (!inp || getComputedStyle(inp).display === 'none') return false;
        const body = document.body?.innerText?.toLowerCase() || '';
        return body.includes('type the following code') || body.includes('please type');
    }''')
    if captcha_input:
        b64 = base64.b64encode(await page.screenshot()).decode()
        reads = []
        for _ in range(3):
            try:
                r = subprocess.run(["curl","-s","-X","POST",CEREBRAS_ENDPOINT,
                    "-H","Authorization: Bearer " + CEREBRAS_API_KEY,"-H","Content-Type: application/json",
                    "-d",json.dumps({"model":"gemma-4-31b","messages":[{"role":"user","content":[
                        {"type":"text","text":"This image contains a CAPTCHA code with 5-7 characters (letters and numbers). Read EVERY character carefully, including any that might be hard to see. Reply ONLY with the characters, nothing else. If unsure about a character, make your best guess."},
                        {"type":"image_url","image_url":{"url":"data:image/png;base64,"+b64}}]}],
                        "max_tokens":30,"temperature":0.1})],
                    capture_output=True,text=True,timeout=30)
                reads.append(re.sub(r'[^A-Za-z0-9]','',json.loads(r.stdout)["choices"][0]["message"]["content"].strip()))
            except: pass
        if reads:
            ml = max(len(r) for r in reads)
            code = ''.join(Counter(r[i] for r in reads if i<len(r)).most_common(1)[0][0] for i in range(ml))
            log("    PS CAPTCHA: " + str(reads) + " -> " + code)
            # Use keyboard.press() — OS-level events that Angular recognizes
            try:
                inp = page.locator('input.alpha-numeric-input')
                await inp.click(force=True)
                await asyncio.sleep(0.3)
                await page.keyboard.press('Meta+a')
                await page.keyboard.press('Backspace')
                await asyncio.sleep(0.2)
                for ch in code:
                    await page.keyboard.press(ch)
                    await asyncio.sleep(0.05)
            except:
                try:
                    await page.locator('input.alpha-numeric-input').fill(code)
                except:
                    await se(page, '(c)=>{const i=document.querySelector("input.alpha-numeric-input");if(i){i.focus();i.value=c;i.dispatchEvent(new Event("input",{bubbles:true}))}}', code)
            await asyncio.sleep(1)
            await se(page, '''()=>{const b=document.querySelectorAll('button');for(const x of b){if(x.textContent.trim().toLowerCase()==='next'){x.click();break}}}''')
            await asyncio.sleep(3)
            return "answered"
    
    # Consent page
    if 'agree and continue' in tl or 'accept all' in tl or 'consent' in tl:
        await se(page, '''()=>{const b=document.querySelectorAll('button,a,input[type="submit"]');for(const x of b){const t=x.textContent.trim().toLowerCase();if(t.includes('agree')||t.includes('accept')||t.includes('consent')||t.includes('continue')){x.click();return true}}return false}''')
        log(f"    Consent accepted")
        return "answered"
    
    # Radio via JS
    rds_info = await se(page, r'''() => {
        const r = document.querySelectorAll('input[type="radio"]');
        if (r.length === 0) return null;
        const h = document.querySelector('h1,h2,h3,h4,.question-text,[class*="question"]');
        return {count: r.length, q: (h?.textContent?.trim()||'').substring(0,200),
            opts: [...r].map((x,i) => ({index:i, text: (document.querySelector('label[for="'+x.id+'"]')?.textContent?.trim()||x.closest('div,li,label')?.textContent?.trim()?.replace(/\s+/g,' ')||'').substring(0,80)}))};
    }''')
    
    if rds_info:
        q_text = rds_info.get('q', '') + ' ' + tl
        opts = rds_info.get('opts', [])
        target = match_answer(q_text, opts)
        await se(page, f'(i)=>{{const r=document.querySelectorAll("input[type=\\"radio\\"]");if(i<r.length)r[i].click()}}', target)
        await se(page, '''()=>{const b=document.querySelectorAll('button,input[type="submit"],a');for(const x of b){const t=x.textContent.trim().toLowerCase();if(t.includes('next')||t.includes('continue')||t.includes('submit')){x.click();return true}}return false}''')
        sel = opts[target]['text'][:25] if target < len(opts) else "?"
        log(f"    Radio {rds_info['count']} -> {sel}")
        return "answered"
    
    # Iframe radios
    for frame in page.frames[1:]:
        try:
            frds_info = await asyncio.wait_for(frame.evaluate(r'''() => {
                const r = document.querySelectorAll('input[type="radio"]');
                if (r.length === 0) return null;
                return {count: r.length, opts: [...r].map((x,i) => ({index:i, text: (x.closest('div,li,label')?.textContent?.trim()?.replace(/\s+/g,' ')||'').substring(0,80)}))};
            }'''), timeout=3)
            if frds_info:
                target = match_answer(tl, frds_info.get('opts', []))
                await frame.evaluate('(i)=>{const r=document.querySelectorAll("input[type=\\"radio\\"]");if(i<r.length)r[i].click()}', target)
                await se(page, '''()=>{const b=document.querySelectorAll('button,input[type="submit"],a');for(const x of b){const t=x.textContent.trim().toLowerCase();if(t.includes('next')||t.includes('continue')){x.click();return true}}return false}''')
                log(f"    iframe radio answered")
                return "answered"
        except: pass
    
    # Textarea
    ta_ok = await se(page, r'''()=>{const t=document.querySelectorAll('textarea');for(const x of t){const r=x.getBoundingClientRect();if(r.width>10&&r.height>10&&!x.disabled&&!x.name.includes('recaptcha')&&!x.className.includes('recaptcha')&&!x.className.includes('poll-reason')){x.value="I carefully weigh the pros and cons, consult with family, and consider goals.";x.dispatchEvent(new Event('input',{bubbles:true}));return true}}return false}''')
    if ta_ok:
        await se(page, '''()=>{const b=document.querySelectorAll('button,input[type="submit"],a');for(const x of b){const t=x.textContent.trim().toLowerCase();if(t.includes('next')||t.includes('continue')){x.click();return true}}return false}''')
        log(f"    Text answered")
        return "answered"
    
    # Select
    sel_ok = await se(page, r'''()=>{const s=document.querySelectorAll('select');if(s.length===0)return false;for(const x of s){if(x.options.length>1){x.selectedIndex=1;x.dispatchEvent(new Event('change',{bubbles:true}))}}return true}''')
    if sel_ok:
        await se(page, '''()=>{const b=document.querySelectorAll('button,input[type="submit"],a');for(const x of b){const t=x.textContent.trim().toLowerCase();if(t.includes('next')||t.includes('continue')){x.click();return true}}return false}''')
        log(f"    Select answered")
        return "answered"
    
    # Drag-and-drop — check BEFORE checkboxes (cookie consent interferes)
    if 'cdk-drag' in (content or "") or 'drag and drop' in tl or 'draggable' in (content or ""):
        m = re.search(r'(?:number|drag)\s+(\d+)', text or "")
        n = m.group(1) if m else "62"
        # Try finding image with matching alt text
        coords = await se(page, '(n)=>{const i=document.querySelector(\'img[alt="\'+n+\'"]\');const d=i?.closest(".cdk-drag")||i;const t=document.querySelectorAll(".cdk-drop-list,[cdkdroplist]");const tgt=t.length>1?t[1]:t[0];if(!d||!tgt)return null;const s=d.getBoundingClientRect(),r=tgt.getBoundingClientRect();return{sx:s.x+s.width/2,sy:s.y+s.height/2,tx:r.x+r.width/2,ty:r.y+r.height/2}}', n)
        if coords:
            sx,sy,tx,ty = coords['sx'],coords['sy'],coords['tx'],coords['ty']
            log(f"    Drag #{n} ({sx:.0f},{sy:.0f})->({tx:.0f},{ty:.0f})")
            await page.mouse.move(sx,sy); await page.wait_for_timeout(100)
            await page.mouse.down(); await page.wait_for_timeout(300)
            for i in range(1,31): await page.mouse.move(sx+(tx-sx)*i/30,sy+(ty-sy)*i/30); await page.wait_for_timeout(20)
            await page.wait_for_timeout(300); await page.mouse.up(); await page.wait_for_timeout(2000)
            await se(page, '''()=>{const b=document.querySelectorAll('button');for(const x of b){if(x.textContent.includes('Next')){x.click();break}}}''')
            log(f"    Drag #{n} done")
            return "answered"
    
    # Checkbox — select ALL unchecked (skip cookie consent)
    cb_ok = await se(page, r'''()=>{const c=document.querySelectorAll('input[type="checkbox"]');if(c.length===0)return false;let n=0;for(const x of c){if(!x.checked&&!x.closest('.cky-consent-container,.cookie-consent')){x.click();n++}}return n>0}''')
    if cb_ok:
        await se(page, '''()=>{const b=document.querySelectorAll('button,input[type="submit"],a');for(const x of b){const t=x.textContent.trim().toLowerCase();if(t.includes('next')||t.includes('continue')||t.includes('submit')){x.click();return true}}return false}''')
        log(f"    Checkbox answered")
        return "answered"
    
    # Next fallback
    clicked = await se(page, '''()=>{const b=document.querySelectorAll('button,input[type="submit"],a');for(const x of b){const t=x.textContent.trim().toLowerCase();if(t.includes('next')||t.includes('continue')||t.includes('submit')){x.click();return true}}return false}''')
    if clicked:
        log(f"    Next clicked")
        return "answered"
    
    return "stuck"

async def main():
    from playwright.async_api import async_playwright
    log("="*60); log("SURVEY LOOP v12"); log("="*60)
    
    for retry in range(10):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(CDP_URL)
                ctx = browser.contexts[0]
                log("Connected (retry " + str(retry) + ")")
                
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
                    
                    survey_page = await get_survey(ctx, ss_page)
                    if not survey_page:
                        log("No survey, wait 60s...")
                        await asyncio.sleep(60)
                        continue
                    
                    log("  Running: " + survey_page.url[:60])
                    
                    completed = False
                    next_only_count = 0
                    same_answer_count = 0
                    last_answer = ""
                    zero_pct_count = 0
                    captcha_fail_count = 0
                    last_captcha_code = ""
                    last_prog = ""
                    last_log = ""
                    for q in range(200):
                        result = await answer_survey(survey_page)
                        if result == "answered":
                            # Check if it was a real answer or just Next
                            last_log = ""
                            try:
                                with open(LOG) as f:
                                    lines = f.readlines()
                                    last_log = lines[-1] if lines else ""
                            except: pass
                            if "Next clicked" in last_log:
                                next_only_count += 1
                                if next_only_count >= 5:
                                    log("  Too many Next-only clicks, ending survey")
                                    try: await survey_page.close()
                                    except: pass
                                    result = "stuck"
                            else:
                                next_only_count = 0
                            # Bot trap detection: same answer 10+ times or 0% for 15+ questions
                            if "Radio" in last_log and last_log == last_answer:
                                same_answer_count += 1
                                if same_answer_count >= 10:
                                    log("  Bot trap: same answer 10x, ending survey")
                                    try: await survey_page.close()
                                    except: pass
                                    result = "stuck"
                            else:
                                same_answer_count = 0
                            last_answer = last_log
                        prog = await se(survey_page, 'document.body?.innerText?.match(/(\\d+)%/)?.[1]||"?"')
                        # CAPTCHA rejection detection: same code, same progress
                        if "PS CAPTCHA" in (last_log if 'last_log' in dir() else ""):
                            captcha_code = last_log.split("-> ")[-1].strip() if "-> " in (last_log if 'last_log' in dir() else "") else ""
                            if captcha_code and captcha_code == last_captcha_code and prog == last_prog:
                                captcha_fail_count += 1
                                if captcha_fail_count >= 3:
                                    log("  CAPTCHA rejected 3x (same code+progress), ending survey")
                                    try: await survey_page.close()
                                    except: pass
                                    result = "stuck"
                            else:
                                captcha_fail_count = 0
                            last_captcha_code = captcha_code
                            last_prog = prog
                        if prog == "0" or prog == "0%":
                            zero_pct_count += 1
                            if zero_pct_count >= 15:
                                log("  Bot trap: 0% for 15 questions, ending survey")
                                try: await survey_page.close()
                                except: pass
                                result = "stuck"
                        else:
                            zero_pct_count = 0
                        log("  Q" + str(q+1) + " (" + str(prog) + "%): " + result)
                        
                        if result == "completed":
                            log("*** SURVEY COMPLETED! ***")
                            completed = True
                            try: await survey_page.close()
                            except: pass
                            break
                        elif result in ("disq","full","captcha_fail","stuck"):
                            log("  Ended: " + result)
                            try: await survey_page.close()
                            except: pass
                            break
                    
                    # Close all extra tabs (prevent tab leaking)
                    for extra_pg in ctx.pages:
                        if 'surveystreak' not in extra_pg.url and 'fivesurveys' not in extra_pg.url and 'topsurveys' not in extra_pg.url:
                            try: await extra_pg.close()
                            except: pass
                    
                    if completed:
                        log("DONE!")
                        return
                    
                    try: await ss_page.bring_to_front()
                    except: pass
                    await asyncio.sleep(5)
                    log("Waiting 30s...")
                    await asyncio.sleep(30)
        
        except Exception as e:
            log("Error: " + str(e))
        log("Reconnect in 15s (" + str(retry+1) + "/10)...")
        await asyncio.sleep(15)
    
    log("Max retries.")

if __name__ == "__main__":
    asyncio.run(main())
