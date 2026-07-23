#!/usr/bin/env python3
"""Survey agent v13 — CUA + Playwright hybrid.
Playwright reads DOM. CUA does interactions (background mouse/keyboard).
Realistic delays and mouse movement to avoid bot detection."""
import asyncio, json, os, re, subprocess, time, random
from datetime import datetime

CDP_URL = "http://localhost:9222"
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
CEREBRAS_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
LOG = os.path.expanduser("~/survey-agent/data/survey_run.log")
os.makedirs(os.path.dirname(LOG), exist_ok=True)

# Profile for answer matching
PROFILE = {
    'employment': ['self-employed', 'self employed'], 'income': ['50', '60', '75'],
    'gender': ['male'], 'age': ['35', '39'], 'hispanic': ['no'], 'race': ['black', 'african'],
    'state': ['texas', 'tx'], 'education': ['bachelor', 'college'], 'children': ['yes'],
    'marital': ['single', 'never'], 'smoke': ['former'], 'drink': ['never'], 'home': ['rent'],
}

def log(m):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {m}"
    print(line, flush=True)
    with open(LOG, "a") as f: f.write(line + "\n")

def human_delay(min_s=0.5, max_s=2.5):
    """Random delay to simulate human behavior."""
    return random.uniform(min_s, max_s)

async def se(p, s, a=None, t=8000):
    try:
        if a is not None: return await asyncio.wait_for(p.evaluate(s, a), timeout=t/1000)
        return await asyncio.wait_for(p.evaluate(s), timeout=t/1000)
    except: return ""

def match_answer(q, opts):
    q = q.lower()
    blacklist = ['select one', 'please select', '---select---', '3rd grade or less', 'less than $5,000']
    valid = [o for o in opts if o.get('text', '').strip() and o.get('text', '').strip().lower() not in blacklist]
    if not valid: return 0
    
    # Profile-based matching
    profile_map = {
        'income': ['50', '60', '75', '50,000', '60,000', '75,000'],
        'gender': ['male'], 'age': ['35', '39'], 'hispanic': ['no'],
        'race': ['black', 'african'], 'state': ['texas', 'tx'],
        'education': ['bachelor', 'college'], 'children': ['yes'],
        'marital': ['single', 'never married'], 'smoke': ['former', 'no'],
        'drink': ['never', 'rarely', 'occasionally'], 'home': ['rent'],
        'employment': ['self-employed', 'self employed', 'full-time'],
        'agree': ['yes', 'agree'], 'accurately': ['yes'],
    }
    
    # Never answer "never" to frequency questions about buying/shopping
    frequency_q = any(k in q for k in ['how often', 'frequency', 'buy food', 'purchase', 'shop', 'eating out', 'restaurant'])
    
    for opt in valid:
        ot = opt.get('text', '').lower().strip()
        # Check profile match
        for cat, kw in profile_map.items():
            if cat in q or any(k in q for k in kw):
                if any(k in ot for k in kw):
                    return opt['index']
        # For frequency questions, pick a moderate option
        if frequency_q and ot and 'never' not in ot:
            if any(k in ot for k in ['once', 'twice', 'sometimes', 'occasionally', 'monthly', 'weekly']):
                return opt['index']
    
    # For frequency questions, skip "never" and pick a moderate option
    if frequency_q:
        for opt in valid:
            ot = opt.get('text', '').lower().strip()
            if 'never' not in ot and 'daily' not in ot:
                return opt['index']
    
    # Default: pick second option (moderate)
    return valid[min(1, len(valid)-1)]['index']

async def cua_click(page, selector_or_coords, delay=True):
    """Click element using CUA (realistic mouse movement) or Playwright force click.
    Falls back to Playwright if CUA unavailable. 5s timeout."""
    if isinstance(selector_or_coords, tuple):
        # Coordinates — use Playwright mouse with human-like movement
        x, y = selector_or_coords
        # Move to approximate position first (with slight randomness)
        await page.mouse.move(x + random.randint(-5, 5), y + random.randint(-5, 5))
        if delay: await asyncio.sleep(human_delay(0.3, 1.0))
        # Small overshoot then correct
        await page.mouse.move(x + random.randint(-2, 2), y + random.randint(-2, 2))
        if delay: await asyncio.sleep(human_delay(0.1, 0.3))
        await page.mouse.click(x, y)
    else:
        # Selector — get coordinates and click with 5s timeout
        try:
            box = await page.locator(selector_or_coords).bounding_box(timeout=5000)
            if box:
                cx = box['x'] + box['width'] / 2 + random.randint(-3, 3)
                cy = box['y'] + box['height'] / 2 + random.randint(-3, 3)
                await page.mouse.move(cx + random.randint(-10, 10), cy + random.randint(-10, 10))
                if delay: await asyncio.sleep(human_delay(0.3, 1.0))
                await page.mouse.move(cx, cy)
                if delay: await asyncio.sleep(human_delay(0.1, 0.3))
                await page.mouse.click(cx, cy)
            else:
                await page.locator(selector_or_coords).click(force=True, timeout=5000)
        except:
            try: await page.locator(selector_or_coords).click(force=True, timeout=5000)
            except: pass
    
    if delay: await asyncio.sleep(human_delay(0.5, 1.5))

async def cua_type(page, text, delay=True):
    """Type text character by character with human-like timing."""
    for ch in text:
        await page.keyboard.press(ch)
        await asyncio.sleep(human_delay(0.03, 0.12))
    if delay: await asyncio.sleep(human_delay(0.3, 0.8))

async def answer_survey(page):
    """Answer one question on the survey page with human-like behavior."""
    try: await page.wait_for_load_state('domcontentloaded', timeout=8000)
    except: pass
    await asyncio.sleep(human_delay(1.5, 3.0))  # Longer wait for JS-rendered content

    text = await se(page, 'document.body?.innerText?.substring(0,1500)||""')
    tl = (text or "").lower()
    url = page.url

    # Also scan ALL frames for text and interactive elements
    all_text = tl
    for frame in page.frames[1:]:
        try:
            ftext = await asyncio.wait_for(frame.evaluate('document.body?.innerText?.substring(0,500)||""'), timeout=3)
            if ftext and ftext.strip():
                all_text += " " + ftext.lower()
        except: pass

    # Completion/disqualification checks
    if "you have earned" in tl or "congratulations" in tl or "successfully completed" in tl:
        return "completed"
    if "not a match" in tl or "disqualified" in tl or "did not qualify" in tl or "terminated" in tl:
        return "disq"
    if "survey full" in tl or "survey has ended" in tl:
        return "disq"
    if 'researchnow' in url or 'surveymyopinion' in url:
        return "stuck"

    content = await se(page, 'document.documentElement?.outerHTML?.substring(0,5000)||""')

    # reCAPTCHA detection — can't solve, skip
    for frame in page.frames:
        if 'recaptcha' in (frame.url or '').lower():
            log("    reCAPTCHA detected — can't solve")
            return "recaptcha"

    # Landing page — "BEGIN SURVEY" link, "Start Survey", etc.
    if ('begin survey' in tl or 'survey length' in tl) and 'agree and' not in tl and 'i agree' not in tl:
        clicked = await se(page, r'''()=>{const a=document.querySelector('#takesurveybtn,a[href*="pstart"]');if(a){a.click();return true}const els=document.querySelectorAll('a,button');for(const el of els){const t=el.textContent?.trim()?.toLowerCase()||"";if(t.includes('begin survey')||t==='start'){el.click();return true}}return false}''')
        if clicked:
            log("    Landing page clicked")
            await asyncio.sleep(human_delay(2.0, 4.0))
            return "answered"
    
    # Consent page
    if 'agree' in tl or 'consent' in tl or 'accept' in tl:
        # Select checkboxes first
        await se(page, r'''() => {
            const cbs = document.querySelectorAll('input[type="checkbox"]');
            for (const cb of cbs) { if (!cb.checked && !cb.closest('.cky-consent-container')) cb.click(); }
        }''')
        await asyncio.sleep(human_delay(0.5, 1.0))
        # Click agree/accept button
        btn = page.locator('button:has-text("Agree"), button:has-text("Accept"), button:has-text("Consent"), button:has-text("Continue")')
        if await btn.count() > 0:
            await cua_click(page, 'button:has-text("Agree"), button:has-text("Accept"), button:has-text("Consent"), button:has-text("Continue")')
            log("    Consent accepted")
            return "answered"

    # PureSpectrum CAPTCHA
    captcha_input = await se(page, r'''() => {
        const inp = document.querySelector('input.alpha-numeric-input');
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
                        {"type":"text","text":"This image contains a CAPTCHA code with 5-7 characters (letters and numbers). Read EVERY character carefully. Reply ONLY with the characters."}]}],
                        "max_tokens":30,"temperature":0.1})],
                    capture_output=True,text=True,timeout=30)
                reads.append(re.sub(r'[^A-Za-z0-9]','',json.loads(r.stdout)["choices"][0]["message"]["content"].strip()))
            except: pass
        if reads:
            ml = max(len(r) for r in reads)
            code = ''.join(Counter(r[i] for r in reads if i<len(r)).most_common(1)[0][0] for i in range(ml))
            log("    CAPTCHA: " + str(reads) + " -> " + code)
            # Click input, then type char by char
            await cua_click(page, 'input.alpha-numeric-input')
            await page.keyboard.press('Meta+a')
            await page.keyboard.press('Backspace')
            await asyncio.sleep(human_delay(0.2, 0.5))
            await cua_type(page, code)
            await asyncio.sleep(human_delay(0.5, 1.0))
            await cua_click(page, 'button:has-text("Next")')
            await asyncio.sleep(human_delay(1.0, 2.0))
            return "answered"

    # Radio buttons
    rds_info = await se(page, r'''() => {
        const r = document.querySelectorAll('input[type="radio"]');
        if (r.length === 0) return null;
        const h = document.querySelector('h1,h2,h3,h4,.question-text,[class*="question"]');
        return {count: r.length, q: (h?.textContent?.trim()||'').substring(0,200),
            opts: [...r].map((x,i) => ({index:i, text: (document.querySelector('label[for="'+x.id+'"]')?.textContent?.trim()||x.closest('div,label,li')?.textContent?.trim()?.replace(/\s+/g,' ')||'').substring(0,80)}))};
    }''')

    if rds_info:
        q_text = rds_info.get('q', '') + ' ' + tl
        opts = rds_info.get('opts', [])
        target = match_answer(q_text, opts)
        # Click radio with human-like movement
        radio = page.locator(f'input[type="radio"]').nth(target)
        try:
            box = await radio.bounding_box(timeout=2000)
            if box:
                await cua_click(page, (box['x'] + box['width']/2, box['y'] + box['height']/2))
            else:
                await radio.click(force=True)
        except:
            await se(page, f'(i)=>{{const r=document.querySelectorAll("input[type=\\"radio\\"]");if(i<r.length)r[i].click()}}', target)
        sel = opts[target]['text'][:25] if target < len(opts) else "?"
        log("    Radio " + str(rds_info['count']) + " -> " + sel)
        # Click Next
        await asyncio.sleep(human_delay(0.5, 1.5))
        await cua_click(page, 'button:has-text("Next"), button:has-text("Continue"), button:has-text("Submit")')
        return "answered"

    # Iframe radios
    for frame in page.frames[1:]:
        try:
            fr_info = await asyncio.wait_for(frame.evaluate(r'''() => {
                const r = document.querySelectorAll('input[type="radio"]');
                if (r.length === 0) return null;
                return {count: r.length, opts: [...r].map((x,i) => ({index:i, text: (x.closest('div,label,li')?.textContent?.trim()?.replace(/\s+/g,' ')||'').substring(0,80)}))};
            }'''), timeout=3)
            if fr_info:
                target = match_answer(tl, fr_info.get('opts', []))
                await frame.evaluate('(i)=>{const r=document.querySelectorAll("input[type=\\"radio\\"]");if(i<r.length)r[i].click()}', target)
                await asyncio.sleep(human_delay(0.5, 1.0))
                await cua_click(page, 'button:has-text("Next"), button:has-text("Continue")')
                log("    iframe radio answered")
                return "answered"
        except: pass

    # Textarea
    ta_ok = await se(page, r'''()=>{const t=document.querySelectorAll('textarea');const body=document.body?.innerText?.toLowerCase()||"";for(const x of t){const r=x.getBoundingClientRect();if(r.width>10&&r.height>10&&!x.disabled&&!x.name.includes('recaptcha')){if(body.includes('abracadabra')){x.value='ABRACADABRA'}else if(body.includes('postal')||body.includes('zip')){x.value='78701'}else{x.value='I carefully weigh the pros and cons, consult with family, and consider goals.'}x.dispatchEvent(new Event('input',{bubbles:true}));return true}}return false}''')
    if ta_ok:
        await asyncio.sleep(human_delay(0.5, 1.0))
        await cua_click(page, 'button:has-text("Next"), button:has-text("Continue")')
        log("    Text answered")
        return "answered"

    # Select — use match_answer for profile-based selection
    sel_ok = await se(page, r'''(q)=>{
        const s=document.querySelectorAll('select');
        if(s.length===0)return false;
        for(const x of s){
            if(x.options.length>1){
                // Get option texts for matching
                const opts=[...x.options].map((o,i)=>({index:i,text:o.text.trim()}));
                // Try to match based on question context
                const body=document.body?.innerText?.toLowerCase()||"";
                let target=1; // default second option
                for(const o of opts){
                    const ot=o.text.toLowerCase();
                    if(body.includes('income')&&(ot.includes('50')||ot.includes('60')||ot.includes('75'))){target=o.index;break}
                    if(body.includes('age')&&(ot.includes('35')||ot.includes('39'))){target=o.index;break}
                    if(body.includes('gender')&&ot.includes('male')){target=o.index;break}
                    if(body.includes('education')&&(ot.includes('bachelor')||ot.includes('college'))){target=o.index;break}
                    if(body.includes('employment')&&(ot.includes('self')||ot.includes('full'))){target=o.index;break}
                    if(body.includes('state')&&ot.includes('texas')){target=o.index;break}
                    if(body.includes('how often')&&!ot.includes('never')&&!ot.includes('daily')){target=o.index;break}
                }
                x.selectedIndex=Math.min(target,x.options.length-1);
                x.dispatchEvent(new Event('change',{bubbles:true}));
            }
        }
        return true;
    }''')
    if sel_ok:
        await asyncio.sleep(human_delay(0.5, 1.0))
        await cua_click(page, 'button:has-text("Next"), button:has-text("Continue")')
        log("    Select answered")
        return "answered"

    # Drag-and-drop
    if 'cdk-drag' in (content or "") or 'drag and drop' in tl:
        m = re.search(r'(?:number|drag)\s+(\d+)', text or "")
        n = m.group(1) if m else "62"
        coords = await se(page, '(n)=>{const i=document.querySelector(\'img[alt="\'+n+\'"]\');const d=i?.closest(".cdk-drag")||i;const t=document.querySelectorAll(".cdk-drop-list,[cdkdroplist]");const tgt=t.length>1?t[1]:t[0];if(!d||!tgt)return null;const s=d.getBoundingClientRect(),r=tgt.getBoundingClientRect();return{sx:s.x+s.width/2,sy:s.y+s.height/2,tx:r.x+r.width/2,ty:r.y+r.height/2}}', n)
        if coords:
            sx,sy,tx,ty = coords['sx'],coords['sy'],coords['tx'],coords['ty']
            log("    Drag #" + n + " (" + str(int(sx)) + "," + str(int(sy)) + ")->(" + str(int(tx)) + "," + str(int(ty)) + ")")
            # Human-like drag
            await page.mouse.move(sx, sy)
            await asyncio.sleep(human_delay(0.3, 0.8))
            await page.mouse.down()
            await asyncio.sleep(human_delay(0.2, 0.5))
            # Smooth drag movement
            for i in range(1, 31):
                await page.mouse.move(sx + (tx-sx)*i/30 + random.uniform(-2, 2), sy + (ty-sy)*i/30 + random.uniform(-2, 2))
                await asyncio.sleep(human_delay(0.01, 0.04))
            await asyncio.sleep(human_delay(0.2, 0.5))
            await page.mouse.up()
            await asyncio.sleep(human_delay(0.5, 1.5))
            await cua_click(page, 'button:has-text("Next")')
            log("    Drag done")
            return "answered"

    # Checkbox — select "None of these" or first unchecked
    cb_ok = await se(page, r'''()=>{const c=document.querySelectorAll('input[type="checkbox"]');if(c.length===0)return false;let n=0;let hasNone=false;for(const x of c){const t=(x.closest('div,label,li')?.textContent?.trim()?.toLowerCase()||'');if(t.includes('none of')){x.click();hasNone=true;n++;break}}if(!hasNone)for(const x of c){if(!x.checked&&!x.closest('.cky-consent-container')){x.click();n++}}return n>0}''')
    if cb_ok:
        await asyncio.sleep(human_delay(0.5, 1.0))
        await cua_click(page, 'button:has-text("Next"), button:has-text("Continue"), button:has-text("Submit")')
        log("    Checkbox answered")
        return "answered"

    # Scan iframes for interactive elements (survey platforms often use iframes)
    for frame in page.frames[1:]:
        try:
            frame_result = await asyncio.wait_for(frame.evaluate(r'''() => {
                const rbs = document.querySelectorAll('input[type="radio"]');
                const cbs = document.querySelectorAll('input[type="checkbox"]');
                const tas = document.querySelectorAll('textarea:not([disabled])');
                const sels = document.querySelectorAll('select');
                const inps = document.querySelectorAll('input[type="text"]:not([type="hidden"]):not([disabled])');
                return {r: rbs.length, cb: cbs.length, ta: tas.length, sel: sels.length, inp: inps.length};
            }'''), timeout=3)
            
            if frame_result and (frame_result.get('r',0) > 0 or frame_result.get('cb',0) > 0 or frame_result.get('ta',0) > 0):
                # Found elements in iframe — answer there
                q_text = await asyncio.wait_for(frame.evaluate('document.body?.innerText?.substring(0,300)||""'), timeout=3)
                
                if frame_result.get('r',0) > 0:
                    target = match_answer(q_text.lower(), [])
                    await asyncio.wait_for(frame.evaluate('(i)=>{const r=document.querySelectorAll("input[type=\\"radio\\"]");if(i<r.length)r[i].click()}', target), timeout=3)
                    log("    iframe radio answered")
                    await asyncio.sleep(human_delay(0.5, 1.0))
                    # Click Next in frame or parent
                    await frame.evaluate(r'''()=>{const btns=document.querySelectorAll('button,input[type="submit"],a');for(const b of btns){const t=b.textContent?.trim()?.toLowerCase()||"";if(t.includes('next')||t.includes('continue')||t.includes('submit')){b.click();return true}}return false}''')
                    return "answered"
                
                if frame_result.get('cb',0) > 0:
                    await asyncio.wait_for(frame.evaluate(r'''()=>{const c=document.querySelectorAll('input[type="checkbox"]');for(const cb of c){if(!cb.checked)cb.click()}return c.length}'''), timeout=3)
                    log("    iframe checkbox answered")
                    await asyncio.sleep(human_delay(0.5, 1.0))
                    await frame.evaluate(r'''()=>{const btns=document.querySelectorAll('button,input[type="submit"],a');for(const b of btns){const t=b.textContent?.trim()?.toLowerCase()||"";if(t.includes('next')||t.includes('continue')||t.includes('submit')){b.click();return true}}return false}''')
                    return "answered"
                
                if frame_result.get('ta',0) > 0:
                    await asyncio.wait_for(frame.evaluate(r'''()=>{const ta=document.querySelector('textarea:not([disabled])');if(ta){ta.value="I carefully consider my options.";ta.dispatchEvent(new Event('input',{bubbles:true}))}return !!ta}'''), timeout=3)
                    log("    iframe textarea answered")
                    await asyncio.sleep(human_delay(0.5, 1.0))
                    return "answered"
        except: pass

    # Next fallback — JS click first (fast), then cua_click as backup
    clicked = await se(page, r'''()=>{const btns=document.querySelectorAll('button,input[type="submit"],a,input[type="button"]');for(const b of btns){const t=b.textContent?.trim()?.toLowerCase()||b.value?.trim()?.toLowerCase()||"";if(t.includes('next')||t.includes('continue')||t.includes('submit')||t.includes('>>')||t.includes('→')){b.scrollIntoView({block:'center'});b.click();return true}}return false}''')
    if clicked:
        log("    Next clicked")
        return "answered"

    return "stuck"

# Need base64 for CAPTCHA
import base64
from collections import Counter

async def main():
    from playwright.async_api import async_playwright
    log("="*60); log("SURVEY AGENT v13 — CUA + Playwright hybrid"); log("="*60)

    for retry in range(10):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(CDP_URL)
                ctx = browser.contexts[0]
                log("Connected (retry " + str(retry) + ")")

                # Find SurveyStreak
                ss_page = None
                for pg in ctx.pages:
                    if 'surveystreak' in pg.url:
                        ss_page = pg
                        break

                if not ss_page:
                    log("No SurveyStreak page!")
                    await asyncio.sleep(30)
                    continue

                # Refresh survey list
                await ss_page.goto('https://www.surveystreak.com/?page=dashboard', wait_until='domcontentloaded', timeout=20000)
                await ss_page.wait_for_timeout(8000)

                surveys = await se(ss_page, '()=>Array.isArray(acctualSurveyList)?acctualSurveyList.map(s=>({id:s.id,payout:s.payout,loi:s.loi})):[]')
                if not surveys:
                    log("No surveys, wait 60s...")
                    await asyncio.sleep(60)
                    continue

                sorted_s = sorted(surveys, key=lambda x: float(x.get('payout', 0)), reverse=True)
                log("Surveys: " + str(len(sorted_s)))

                for s in sorted_s[:5]:
                    sid, payout, loi = s['id'], s['payout'], s['loi']
                    log("\nTrying #" + sid + " ($" + str(payout) + ", " + str(loi) + "min)")

                    # Refresh page to get fresh survey list
                    await ss_page.goto('https://www.surveystreak.com/?page=dashboard', wait_until='domcontentloaded', timeout=20000)
                    await ss_page.wait_for_timeout(5000)

                    initial_pages = len(ctx.pages)

                    # clickSurvey
                    try:
                        await ss_page.evaluate(f'clickSurvey("{sid}")')
                    except:
                        log("  clickSurvey error")
                        continue
                    await asyncio.sleep(8)

                    # Answer screening questions until Start Survey appears or not qualified
                    start_vis = False
                    for sq in range(20):
                        await asyncio.sleep(3)
                        
                        # Check for Start Survey button
                        start_vis = await ss_page.evaluate('document.querySelector("#start-survey-button")?.offsetParent !== null || (document.querySelector("#start-survey-button") && getComputedStyle(document.querySelector("#start-survey-button")).display !== "none")')
                        if start_vis:
                            break
                        
                        # Check for "not qualified" modal
                        not_qualified = await ss_page.evaluate(r'''() => {
                            const m = document.querySelector('#survey-modal');
                            if (!m || getComputedStyle(m).display === 'none') return false;
                            const text = m.innerText?.toLowerCase() || '';
                            return text.includes('not a match') || text.includes('not qualified') || text.includes('unfortunately');
                        }''')
                        if not_qualified:
                            log("  Not qualified (screening)")
                            break
                        
                        # Answer screening question in modal
                        # Try radio buttons
                        radio_answered = await ss_page.evaluate(r'''() => {
                            const m = document.querySelector('#survey-modal');
                            if (!m || getComputedStyle(m).display === 'none') return null;
                            const r = m.querySelectorAll('input[type="radio"]');
                            if (r.length === 0) return null;
                            const opts = [...r].map((x,i) => ({i, t: (x.closest('div,label,li')?.textContent?.trim()?.replace(/\s+/g,' ')||'').substring(0,60)}));
                            const text = m.innerText?.toLowerCase() || '';
                            // Smart matching
                            let target = 0;
                            for (const o of opts) {
                                const ot = o.t.toLowerCase();
                                if (text.includes('device') && ot.includes('desktop')) { target = o.i; break; }
                                if (text.includes('ready') && ot.includes('yes')) { target = o.i; break; }
                                if (text.includes('industry') || text.includes('sector')) {
                                    if (ot.includes('manufacturing') || ot.includes('financial') || ot.includes('technology')) { target = o.i; break; }
                                }
                                if (text.includes('role') || text.includes('position') || text.includes('job title')) {
                                    if (ot.includes('c-level') || ot.includes('owner') || ot.includes('manager')) { target = o.i; break; }
                                }
                                if (text.includes('income') || text.includes('earn') || text.includes('household')) {
                                    if (ot.includes('50') || ot.includes('60') || ot.includes('75') || ot.includes('100')) { target = o.i; break; }
                                }
                                if (text.includes('age')) {
                                    if (ot.includes('35') || ot.includes('30') || ot.includes('25')) { target = o.i; break; }
                                }
                                if (text.includes('gender')) {
                                    if (ot.includes('male')) { target = o.i; break; }
                                }
                                if (text.includes('state') || text.includes('live')) {
                                    if (ot.includes('texas') || ot.includes('austin')) { target = o.i; break; }
                                }
                                if (text.includes('invite') || text.includes('participate') || ot.includes('yes')) {
                                    target = o.i; break;
                                }
                            }
                            r[target].click();
                            return 'Screen Q' + ': radio ' + opts[target]?.t?.substring(0, 25);
                        }''')
                        if radio_answered:
                            # Submit
                            await ss_page.evaluate('''()=>{const b=document.querySelector('#submit-button-cpx');if(b&&getComputedStyle(b).display!=='none')b.click()}''')
                            log("    " + radio_answered)
                            continue
                        
                        # Try checkboxes — select "None of these" specifically
                        cb_answered = await ss_page.evaluate(r'''() => {
                            const m = document.querySelector('#survey-modal');
                            if (!m || getComputedStyle(m).display === 'none') return 0;
                            const c = m.querySelectorAll('input[type="checkbox"]');
                            // First uncheck all
                            for (const cb of c) { if (cb.checked) cb.click(); }
                            // Then check "None of these"
                            let found = false;
                            for (const cb of c) {
                                const t = (cb.closest('div,label,li')?.textContent?.trim()?.toLowerCase()||'');
                                if (t.includes('none of')) { cb.click(); found = true; break; }
                            }
                            // If no "none", check first one
                            if (!found && c.length > 0) c[0].click();
                            return c.length;
                        }''')
                        if cb_answered and int(cb_answered) > 0:
                            await ss_page.evaluate('''()=>{const b=document.querySelector('#submit-button-cpx');if(b&&getComputedStyle(b).display!=='none')b.click()}''')
                            log("    Screen: " + str(cb_answered) + " checkboxes")
                            continue
                        
                        # Try textarea
                        ta_answered = await ss_page.evaluate(r'''() => {
                            const m = document.querySelector('#survey-modal');
                            if (!m || getComputedStyle(m).display === 'none') return false;
                            const ta = m.querySelector('textarea:not([disabled])');
                            if (!ta) return false;
                            const q = m.innerText?.toLowerCase() || '';
                            if (q.includes('postal') || q.includes('zip')) ta.value = '78701';
                            else if (q.includes('income') || q.includes('earn')) ta.value = '55000';
                            else ta.value = 'Technology and music';
                            ta.dispatchEvent(new Event('input', {bubbles: true}));
                            return true;
                        }''')
                        if ta_answered:
                            await ss_page.evaluate('''()=>{const b=document.querySelector('#submit-button-cpx');if(b&&getComputedStyle(b).display!=='none')b.click()}''')
                            log("    Screen: textarea")
                            continue
                        
                        # Try select
                        sel_answered = await ss_page.evaluate(r'''() => {
                            const m = document.querySelector('#survey-modal');
                            if (!m || getComputedStyle(m).display === 'none') return false;
                            const s = m.querySelector('select');
                            if (!s || s.options.length <= 1) return false;
                            s.selectedIndex = Math.min(2, s.options.length - 1);
                            s.dispatchEvent(new Event('change', {bubbles: true}));
                            return true;
                        }''')
                        if sel_answered:
                            await ss_page.evaluate('''()=>{const b=document.querySelector('#submit-button-cpx');if(b&&getComputedStyle(b).display!=='none')b.click()}''')
                            log("    Screen: select")
                            continue
                        
                        log("    Screen Q" + str(sq) + ": waiting...")
                    
                    # Now check for Start Survey button
                    if start_vis:
                        log("  Qualified!")
                        await cua_click(ss_page, '#start-survey-button')
                        await asyncio.sleep(12)

                        if len(ctx.pages) > initial_pages:
                            sp = ctx.pages[-1]
                            try: await sp.wait_for_load_state('domcontentloaded', timeout=15000)
                            except: pass
                            await sp.wait_for_timeout(5000)
                            log("  Survey: " + sp.url[:60])

                            # Handle consent
                            try:
                                cc = await se(sp, 'document.body?.innerText?.substring(0,500)||""')
                                if 'agree' in cc.lower() or 'consent' in cc.lower() or 'accept' in cc.lower():
                                    await se(sp, r'''() => {
                                        const cbs = document.querySelectorAll('input[type="checkbox"]');
                                        for (const cb of cbs) { if (!cb.checked) cb.click(); }
                                    }''')
                                    await asyncio.sleep(human_delay(0.5, 1.0))
                                    await cua_click(sp, 'button:has-text("Agree"), button:has-text("Accept"), button:has-text("Continue")')
                                    log("  Consent accepted")
                                    await asyncio.sleep(human_delay(2.0, 4.0))
                            except: pass

                            # Run survey with human-like behavior
                            completed = False
                            next_only_count = 0
                            same_answer_count = 0
                            last_answer_text = ""
                            for q in range(200):
                                result = await answer_survey(sp)
                                prog = await se(sp, 'document.body?.innerText?.match(/(\\d+)%/)?.[1]||"?"')
                                log("  Q" + str(q+1) + " (" + str(prog) + "%): " + result)

                                # Bot trap detection
                                if result == "answered":
                                    # Check last log line for "Next clicked"
                                    last_log_line = ""
                                    try:
                                        with open(LOG) as f:
                                            lines = f.readlines()
                                            last_log_line = lines[-2] if len(lines) >= 2 else ""
                                    except: pass
                                    
                                    if "Next clicked" in last_log_line:
                                        next_only_count += 1
                                        if next_only_count >= 5:
                                            log("  Bot trap: 5 Next-only clicks, ending")
                                            try: await sp.close()
                                            except: pass
                                            result = "stuck"
                                    else:
                                        next_only_count = 0
                                    
                                    # Same answer detection — extract answer text without timestamp
                                    import re as _re
                                    answer_text = _re.sub(r'^\[.*?\]\s*', '', last_log_line).strip()
                                    if "Radio" in answer_text:
                                        if answer_text == last_answer_text:
                                            same_answer_count += 1
                                            if same_answer_count >= 5:
                                                log("  Bot trap: same answer 5x, ending")
                                                try: await sp.close()
                                                except: pass
                                                result = "stuck"
                                        else:
                                            same_answer_count = 0
                                        last_answer_text = answer_text

                                if result == "completed":
                                    log("*** SURVEY COMPLETED! ***")
                                    completed = True
                                    break
                                elif result in ("disq", "full", "captcha_fail", "stuck", "recaptcha"):
                                    log("  Ended: " + result)
                                    break

                            try: await sp.close()
                            except: pass

                            if completed:
                                log("DONE!")
                                return

                    # Close extra tabs
                    for extra_pg in ctx.pages:
                        if 'surveystreak' not in extra_pg.url and 'fivesurveys' not in extra_pg.url and 'topsurveys' not in extra_pg.url:
                            try: await extra_pg.close()
                            except: pass

                    await asyncio.sleep(human_delay(2.0, 5.0))

        except Exception as e:
            log("Error: " + str(e))
        log("Reconnect in 15s (" + str(retry+1) + "/10)...")
        await asyncio.sleep(15)

    log("Max retries.")

if __name__ == "__main__":
    asyncio.run(main())
