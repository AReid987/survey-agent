#!/usr/bin/env python3
"""Survey agent v8 — handles both /router/go and /surveys/pre/ URLs.
Clicks TAKE SURVEY from dashboard, follows new tab, handles all page types."""
import os
import asyncio, base64, json, os, re, subprocess, time
from collections import Counter
from datetime import datetime

CDP_URL = "http://localhost:9222"
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
CEREBRAS_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
LOG_PATH = os.path.expanduser("~/survey-agent/data/survey_run.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f: f.write(line + "\n")

def read_captcha_once(b64):
    try:
        payload = {"model": "gemma-4-31b", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Read the distorted text in this image. The characters may include letters and numbers. Return only the characters, nothing else."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}], "max_tokens": 30, "temperature": 0.1}
        result = subprocess.run(["curl", "-s", "-X", "POST", CEREBRAS_ENDPOINT,
            "-H", f"Authorization: Bearer {CEREBRAS_API_KEY}", "-H", "Content-Type: application/json",
            "-d", json.dumps(payload)], capture_output=True, text=True, timeout=30)
        return re.sub(r'[^A-Za-z0-9]', '', json.loads(result.stdout)["choices"][0]["message"]["content"].strip())
    except: return ""

def solve_captcha(image_bytes):
    b64 = base64.b64encode(image_bytes).decode()
    reads = [r for r in (read_captcha_once(b64) for _ in range(3)) if r]
    if not reads: return ""
    ml = max(len(r) for r in reads)
    solved = ''.join(Counter(r[pos] for r in reads if pos < len(r)).most_common(1)[0][0] for pos in range(ml))
    log(f"    CAPTCHA: {reads} -> {solved}")
    return solved

async def safe_content(page, timeout=5000):
    try: return await asyncio.wait_for(page.content(), timeout=timeout/1000)
    except: return ""

async def safe_eval(page, script, arg=None, timeout=5000):
    try:
        if arg is not None: return await asyncio.wait_for(page.evaluate(script, arg), timeout=timeout/1000)
        return await asyncio.wait_for(page.evaluate(script), timeout=timeout/1000)
    except: return ""

async def cdk_drag(page, num):
    coords = await safe_eval(page, '''(n) => {
        const img = document.querySelector('img[alt="'+n+'"]');
        const d = img?.closest('.cdk-drag'); const t = document.querySelectorAll('.cdk-drop-list');
        const tgt = t.length>1 ? t[1] : t[0];
        if(!d||!tgt) return null;
        const s=d.getBoundingClientRect(), r=tgt.getBoundingClientRect();
        return {sx:s.x+s.width/2,sy:s.y+s.height/2,tx:r.x+r.width/2,ty:r.y+r.height/2};
    }''', arg=num, timeout=5000)
    if not coords: return False
    sx,sy,tx,ty = coords['sx'],coords['sy'],coords['tx'],coords['ty']
    log(f"    Drag ({sx:.0f},{sy:.0f}) -> ({tx:.0f},{ty:.0f})")
    await page.mouse.move(sx,sy); await page.wait_for_timeout(100)
    await page.mouse.down(); await page.wait_for_timeout(300)
    for i in range(1,31): await page.mouse.move(sx+(tx-sx)*i/30,sy+(ty-sy)*i/30); await page.wait_for_timeout(20)
    await page.wait_for_timeout(300); await page.mouse.up(); await page.wait_for_timeout(2000)
    return True

# Known survey platform domains
SURVEY_DOMAINS = ['screener.purespectrum', 'fusion.spectrumsurveys', 'dkr1.ssisurveys',
                  'focusvision', 'samplicio', 'yoursurveynow', 'confirmit',
                  'surveys.insights-today', 'decipher', 'qualtrics']
SKIP_DOMAINS = ['branded', 'gobranded']

def is_survey_page(url):
    """Check if URL is an actual survey page (not dashboard/router)."""
    return any(d in url for d in SURVEY_DOMAINS)

def is_dashboard(url):
    from urllib.parse import urlparse
    path = urlparse(url).path
    return any(d in url for d in SKIP_DOMAINS) and ('dashboard' in path or 'members' in path or 'login' in path)

async def wait_q(page, mw=15):
    """Wait for detectable question content. Handles iframes."""
    for _ in range(mw):
        try:
            # Check main frame
            c = await safe_content(page, timeout=5000)
            if any(k in c for k in ['input[type="radio"]', 'cdk-drag', 'PS Captcha']):
                return c
            # Check for visible textarea (not recaptcha)
            ta = page.locator('textarea:visible:not([name*="recaptcha"]):not([disabled])')
            if await ta.count() > 0: return c
            
            # Check iframes
            for frame in page.frames[1:]:  # skip main frame
                try:
                    fc = await asyncio.wait_for(frame.content(), timeout=3)
                    if any(k in fc for k in ['input[type="radio"]', 'textarea', 'select', 'cdk-drag', 'PS Captcha']):
                        log(f"    Found question in iframe: {frame.url[:60]}")
                        return fc
                except: pass
            
            # Check for headings (SSISurveys uses h2 for questions)
            if '<h1' in c or '<h2' in c:
                return c
            
            # Check for select elements
            if '<select' in c:
                return c
            
            # Check text for completion/disqualification
            t = await safe_eval(page, 'document.body?.innerText?.substring(0,1000)||""', timeout=3000)
            tl = t.lower() if t else ""
            if "you have earned" in tl or "points earned" in tl or "congratulations" in tl:
                return "completed"
            if "not a match" in tl or "disqualified" in tl or "did not qualify" in tl:
                return "disq"
            if "survey full" in tl:
                return "full"
            
            # Check for Google reCAPTCHA iframe
            for frame in page.frames:
                if 'recaptcha' in (frame.url or '').lower():
                    log(f"    reCAPTCHA iframe detected: {frame.url[:60]}")
                    return "recaptcha"
        except: pass
        await asyncio.sleep(1)
    return None

async def tc(p,s,t=5000,f=False):
    try: await p.locator(s).click(timeout=t,force=f); return True
    except: return False

async def ensure_logged_in(page):
    await page.goto('https://surveys.gobranded.com/users/dashboard', wait_until='domcontentloaded', timeout=20000)
    await page.wait_for_timeout(5000)
    if 'login' in page.url:
        log("  Logging in...")
        await page.fill('#UserEmail', 'read.musik@gmail.com')
        await page.wait_for_timeout(300)
        await page.fill('#UserPassword', os.environ["BRANDED_SURVEYS_PASSWORD"])
        await page.wait_for_timeout(300)
        await page.evaluate('document.querySelector("form").submit()')
        await page.wait_for_timeout(8000)
    log(f"  Logged in: {page.url[:60]}")

async def handle_survey_page(page):
    """Handle a loaded survey page — consent, BEGIN, then run questions."""
    # Consent
    try:
        cc = await safe_content(page)
        if 'Agree and Continue' in cc:
            await tc(page, 'button:has-text("Agree and Continue")')
            await page.wait_for_timeout(5000)
            log("  Consent OK")
    except: pass
    
    # BEGIN SURVEY
    try:
        if await tc(page, 'a:has-text("BEGIN SURVEY"), button:has-text("BEGIN SURVEY")'):
            await page.wait_for_timeout(8000)
            log("  BEGIN OK")
    except: pass
    
    # Wait for question content
    c = await wait_q(page, mw=20)
    
    if c is None:
        log("  Q1: Timeout (no question detected)")
        return "timeout"
    if c == "completed":
        log("  COMPLETED!")
        return "completed"
    if c == "disq":
        log("  Disqualified")
        return "disq"
    if c == "full":
        log("  Survey full")
        return "full"
    
    # Run questions
    return await run_questions(page, c)

async def run_questions(page, first_content=None):
    """Run through survey questions starting with first_content."""
    content = first_content
    for q in range(200):
        if content is None:
            content = await wait_q(page)
        if content == "completed": log(f"  Q{q+1}: COMPLETED!"); return "completed"
        if content == "disq": log(f"  Q{q+1}: Disq"); return "disq"
        if content == "full": log(f"  Q{q+1}: Full"); return "full"
        if content is None: log(f"  Q{q+1}: Timeout"); return "timeout"
        
        text = await safe_eval(page, 'document.body.innerText.substring(0,2000)', timeout=5000) or ""
        
        # reCAPTCHA — can't solve
        if content == "recaptcha":
            log(f"  Q{q+1}: reCAPTCHA detected — skipping (can't solve)")
            return "recaptcha"
        
        # CAPTCHA
        if "PS Captcha" in (content or ""):
            for a in range(5):
                code = solve_captcha(await page.screenshot())
                log(f"  Q{q+1}: CAPTCHA {a+1}: {code}")
                if not code: await tc(page,'button:has-text("Refresh")'); await page.wait_for_timeout(2000); continue
                inp = page.locator('input[type="text"]:visible:not([tabindex="-1"])')
                if await inp.count()>0: await inp.first.fill(code); await page.wait_for_timeout(500)
                if await tc(page,'button:has-text("Next")'):
                    await page.wait_for_timeout(3000)
                    if "PS Captcha" not in (await safe_content(page)): log("    OK!"); break
                await tc(page,'button:has-text("Refresh")'); await page.wait_for_timeout(2000)
            else: return "captcha_fail"
            content = None; continue
        
        # Drag-and-drop
        if 'cdk-drag' in (content or ""):
            m = re.search(r'number (\d+)', text); n = m.group(1) if m else "62"
            log(f"  Q{q+1}: DRAG #{n}")
            if await cdk_drag(page, n):
                if await tc(page,'button:has-text("Next")'): await page.wait_for_timeout(3000); log("    Drag OK!")
                else: return "drag_fail"
            else: return "drag_fail"
            content = None; continue
        
        # Select dropdown
        if '<select' in (content or ""):
            selects = page.locator('select:visible')
            if await selects.count() > 0:
                # Select first option
                await selects.first.select_option(index=1)
                log(f"  Q{q+1}: Select")
                if await tc(page,'button:has-text("Next"), button:has-text("Continue")'): await page.wait_for_timeout(3000)
            content = None; continue
        
        # Visible textarea
        ta = page.locator('textarea:visible:not([name*="recaptcha"]):not([disabled])')
        try: ta_count = await ta.count()
        except: ta_count = 0
        if ta_count > 0:
            if "ABRACADABRA" in text: await ta.first.fill("ABRACADABRA")
            else: await ta.first.fill("My family and children bring me the most happiness. I enjoy creating music and exploring new technology in Austin Texas.")
            if await tc(page,'button:has-text("Next"), button:has-text("Continue")'): await page.wait_for_timeout(3000); log(f"  Q{q+1}: Text")
            content = None; continue
        
        # Visible text input
        ti = page.locator('input[type="text"]:visible:not([tabindex="-1"]):not([name*="recaptcha"])')
        try: ti_count = await ti.count()
        except: ti_count = 0
        if ti_count > 0:
            await ti.first.fill("My family and children bring me the most happiness.")
            if await tc(page,'button:has-text("Next"), button:has-text("Continue")'): await page.wait_for_timeout(3000); log(f"  Q{q+1}: TextInp")
            content = None; continue
        
        # Radio buttons — check main frame AND iframes
        radio_clicked = False
        # Main frame
        rds = page.locator('input[type="radio"]')
        try: rds_count = await rds.count()
        except: rds_count = 0
        if rds_count > 0:
            await rds.first.click(force=True)
            radio_clicked = True
        # Iframes
        if not radio_clicked:
            for frame in page.frames[1:]:
                try:
                    frds = frame.locator('input[type="radio"]')
                    if await frds.count() > 0:
                        await frds.first.click(force=True)
                        radio_clicked = True
                        break
                except: pass
        if radio_clicked:
            if await tc(page,'button:has-text("Next"), button:has-text("Continue"), button:has-text("Submit")'): await page.wait_for_timeout(3000); log(f"  Q{q+1}: Radio")
            content = None; continue
        
        # Buttons — check if page actually changes after click
        btn_clicked = False
        for sel in ['button:has-text("Continue")', 'button:has-text("Submit")', 'button:has-text("Next")', 'a:has-text("Next")']:
            if await tc(page, sel):
                old_url = page.url
                await page.wait_for_timeout(3000)
                if page.url != old_url:
                    log(f"  Q{q+1}: Btn -> navigated")
                    btn_clicked = True
                    break
                # Check if content changed
                new_text = await safe_eval(page, 'document.body?.innerText?.substring(0,200)||""', timeout=3000)
                if new_text and new_text != text[:200]:
                    log(f"  Q{q+1}: Btn -> content changed")
                    btn_clicked = True
                    break
                # Page didn't change — stuck
                log(f"  Q{q+1}: Btn clicked but page stuck")
                return "stuck"
        if not btn_clicked:
            log(f"  Q{q+1}: Unknown"); return "unknown"
        content = None
    return "max"

async def main():
    from playwright.async_api import async_playwright
    log("="*60); log("SURVEY AGENT v8"); log("="*60)
    
    for retry in range(10):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(CDP_URL)
                log(f"Connected (retry {retry})")
                ctx = browser.contexts[0]
                while len(ctx.pages) > 1:
                    try: await ctx.pages[-1].close()
                    except: break
                page = ctx.pages[0]
                await ensure_logged_in(page)
                
                for poll in range(60):
                    log(f"\n--- Poll {poll+1} ---")
                    
                    # Refresh dashboard
                    await page.goto('https://surveys.gobranded.com/users/dashboard', wait_until='domcontentloaded', timeout=20000)
                    await page.wait_for_timeout(10000)
                    
                    # Get ALL TAKE SURVEY visible buttons
                    take_btns = page.locator('a:has-text("TAKE SURVEY"):visible')
                    btn_count = await take_btns.count()
                    log(f"TAKE SURVEY buttons: {btn_count}")
                    
                    if btn_count == 0:
                        log("None, wait 60s")
                        await asyncio.sleep(60)
                        continue
                    
                    found = False
                    for i in range(btn_count):
                        # Refresh dashboard each time
                        await page.goto('https://surveys.gobranded.com/users/dashboard', wait_until='domcontentloaded', timeout=20000)
                        await page.wait_for_timeout(8000)
                        
                        take_btns = page.locator('a:has-text("TAKE SURVEY"):visible')
                        if i >= await take_btns.count():
                            break
                        
                        href = await take_btns.nth(i).get_attribute('href')
                        log(f"\n  Survey {i+1}: {href[:60]}...")
                        
                        # Skip qualification links
                        if 'qualifications' in (href or ''):
                            log("  Qualification, skip")
                            continue
                        
                        initial_pages = len(ctx.pages)
                        
                        # Click TAKE SURVEY
                        try:
                            await take_btns.nth(i).click(timeout=10000)
                        except Exception as e:
                            log(f"  Click err: {str(e)[:60]}")
                            continue
                        
                        # Wait for new tab
                        sp = None
                        for w in range(20):
                            await asyncio.sleep(1)
                            if len(ctx.pages) > initial_pages:
                                sp = ctx.pages[-1]
                                # Wait for it to settle
                                try:
                                    await sp.wait_for_load_state('domcontentloaded', timeout=10000)
                                except: pass
                                await sp.wait_for_timeout(3000)
                                break
                        
                        if sp is None:
                            log(f"  No new tab")
                            continue
                        
                        url = sp.url
                        log(f"  URL: {url[:80]}")
                        
                        # If redirected to dashboard, survey is full
                        # If on /router/go, wait for redirect
                        if '/router/go' in url:
                            for rw in range(15):
                                await asyncio.sleep(2)
                                url = sp.url
                                if '/router/go' not in url:
                                    break
                            log(f"  After router wait: {url[:60]}")
                            if is_dashboard(url):
                                log(f"  Full/expired")
                                try: await sp.close()
                                except: pass
                                continue
                            if '/router/go' in url:
                                log(f"  Router stuck, skip")
                                try: await sp.close()
                                except: pass
                                continue
                        elif is_dashboard(url):
                            log(f"  Full/expired")
                            try: await sp.close()
                            except: pass
                            continue
                        
                        # If landed on a pre-page (on Branded Surveys domain), get TAKE href
                        if '/surveys/pre/' in url:
                            try:
                                take_href = await sp.locator('a:has-text("TAKE SURVEY")').first.get_attribute('href', timeout=5000)
                                if take_href:
                                    take_url = f"https://surveys.gobranded.com{take_href}" if take_href.startswith('/') else take_href
                                    log(f"  Pre-page -> navigating to: {take_url[:60]}")
                                    await sp.goto(take_url, wait_until='domcontentloaded', timeout=30000)
                                    await sp.wait_for_timeout(8000)
                                    url = sp.url
                                    log(f"  After nav: {url[:80]}")
                                    if is_dashboard(url):
                                        log(f"  Full after nav")
                                        try: await sp.close()
                                        except: pass
                                        continue
                            except: pass
                        
                        # Handle the survey page
                        result = await handle_survey_page(sp)
                        log(f"  Result: {result}")
                        
                        if result == "completed":
                            log(f"\n*** SURVEY COMPLETED! ***")
                            try: await sp.close()
                            except: pass
                            found = True
                            break
                        
                        try: await sp.close()
                        except: pass
                        await asyncio.sleep(5)
                    
                    if found:
                        log("DONE")
                        return
                    
                    log("All tried, wait 90s...")
                    await asyncio.sleep(90)
        
        except Exception as e:
            log(f"Error: {e}")
        
        log(f"Reconnect in 15s ({retry+1}/10)...")
        await asyncio.sleep(15)
        if retry >= 3:
            log("Restarting ego-lite...")
            subprocess.run(["osascript", "-e", 'quit app "ego lite"'], capture_output=True)
            await asyncio.sleep(3)
            subprocess.run(["open", "-a", "ego lite", "--args", "--remote-debugging-port=9222"], capture_output=True)
            await asyncio.sleep(8)
    
    log("Max retries.")

if __name__ == "__main__":
    asyncio.run(main())
