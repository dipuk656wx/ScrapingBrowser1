from fastapi import FastAPI
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser, BrowserContext
import asyncio
import time
from config import Cofiguration
from contextlib import asynccontextmanager
import os
import shutil

# Global browser context
_browser_context: BrowserContext = None
_playwright = None
_browser = None
_semaphore = asyncio.Semaphore(10)  # Max 10 concurrent requests


class FetchRequest(BaseModel):
    url: str
    timeout: int = 30


class FetchResponse(BaseModel):
    success: bool
    html: str | None
    final_url: str | None
    cloudflare_bypassed: bool
    error: str | None


async def initialize_browser():
    """Initialize single persistent browser context with system Chrome"""
    global _browser_context, _playwright, _browser
    
    try:
        print("[*] Initializing Playwright with system Chrome...")
        
        # Profile path
        profile_path = '/tmp/chrome_profile_playwright'
        if os.path.exists(profile_path):
            shutil.rmtree(profile_path, ignore_errors=True)
        os.makedirs(profile_path, exist_ok=True)
        
        # Remove stale lock file
        lock_file = os.path.join(profile_path, 'SingletonLock')
        if os.path.exists(lock_file):
            os.remove(lock_file)
            print("[*] Removed stale lock file")
        
        # Find system Chrome
        chrome_path = None
        possible_paths = [
            '/usr/bin/google-chrome-stable',
            '/usr/bin/google-chrome',
            '/usr/bin/chromium-browser',
            '/usr/bin/chromium',
            '/snap/bin/chromium'
        ]
        
        for path in possible_paths:
            if os.path.exists(path):
                chrome_path = path
                print(f"[+] Found system Chrome at: {chrome_path}")
                break
        
        if not chrome_path:
            raise Exception("Chrome not found. Install with: sudo apt-get install -y google-chrome-stable")
        
        # Start Playwright
        _playwright = await async_playwright().start()
        
        # Launch persistent browser context with system Chrome
        _browser_context = await _playwright.chromium.launch_persistent_context(
            profile_path,
            executable_path=chrome_path,  # Use system Chrome
            headless=False,
            viewport={'width': 1920, 'height': 1080},
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--window-size=1920,1080',
                '--disable-infobars',
                '--disable-popup-blocking',
                '--disable-extensions',
                '--disable-background-networking',
                '--disable-default-apps',
                '--disable-sync',
                '--metrics-recording-only',
                '--mute-audio',
                '--no-first-run'
            ],
            ignore_https_errors=True,
            locale='en-US',
            timezone_id='UTC'
        )
        
        print(f"[+] Browser context initialized with Chrome: {chrome_path}")
        print(f"[+] Ready to handle up to 10 concurrent requests")
        return True
        
    except Exception as e:
        print(f"[-] Failed to initialize browser: {e}")
        import traceback
        traceback.print_exc()
        return False


async def cleanup_browser():
    """Cleanup browser context"""
    global _browser_context, _playwright
    
    try:
        if _browser_context:
            print("[*] Closing browser context...")
            await _browser_context.close()
            _browser_context = None
        
        if _playwright:
            await _playwright.stop()
            _playwright = None
            
        print("[+] Browser closed successfully")
    except Exception as e:
        print(f"[!] Error closing browser: {e}")


async def bypass_cloudflare(page, max_attempts=20):
    """Bypass Cloudflare challenge using Tab+Space technique"""
    print("[*] Checking for Cloudflare challenge...")
    
    attempts = 0
    last_check_time = time.time()
    
    while attempts < max_attempts:
        try:
            current_time = time.time()
            
            if current_time - last_check_time >= 3:
                attempts += 1
                
                page_source = await page.content()
                page_text = await page.evaluate("() => document.body.innerText")
                
                # Check if Cloudflare challenge is present
                is_cloudflare = (
                    "Verifying you are human" in page_source or 
                    "Just a moment" in page_source or
                    "challenges.cloudflare.com" in page_source or
                    "Performing security verification" in page_source or
                    "Incompatible browser" in page_text
                )
                
                if not is_cloudflare:
                    print("[+] No Cloudflare challenge detected or already bypassed")
                    return True
                
                print(f"[*] Cloudflare detected - Attempt {attempts}/{max_attempts}")
                
                # Try Tab + Space
                if attempts <= 5:
                    try:
                        await page.keyboard.press('Tab')
                        await asyncio.sleep(0.3)
                        await page.keyboard.press('Space')
                        print("[*] Tab + Space sent")
                    except Exception as e:
                        print(f"[*] Could not send keys: {e}")
                
                # Wait for auto-bypass
                await asyncio.sleep(4)
                
                # Check if bypassed
                page_source = await page.content()
                page_text = await page.evaluate("() => document.body.innerText")
                
                is_cloudflare_now = (
                    "Verifying you are human" in page_source or 
                    "Just a moment" in page_source
                )
                
                if not is_cloudflare_now and len(page_text.strip()) > 100:
                    print("[+] Cloudflare challenge bypassed!")
                    return True
                
                last_check_time = current_time
            
            await asyncio.sleep(0.5)
            
        except Exception as e:
            print(f"[-] Error during Cloudflare bypass: {e}")
            return False
    
    print(f"[-] Failed to bypass Cloudflare after {max_attempts} attempts")
    return False


async def fetch_url_with_page(url: str, timeout: int, request_id: int):
    """Fetch URL in a new page - each request gets its own page object"""
    global _browser_context
    
    if _browser_context is None:
        return FetchResponse(
            success=False,
            html=None,
            final_url=None,
            cloudflare_bypassed=False,
            error="Browser context not initialized"
        )
    
    page = None
    
    try:
        # Create new page (like page1 = await browserContext.newPage() in JS)
        page = await _browser_context.new_page()
        print(f"[Req-{request_id}] Created new page")
        
        # Set realistic headers
        await page.set_extra_http_headers({
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-User': '?1',
            'Sec-Fetch-Dest': 'document'
        })
        
        # Stealth mode
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });
            window.chrome = {
                runtime: {},
                loadTimes: () => {},
                csi: () => {}
            };
        """)
        
        print(f"[Req-{request_id}] Navigating to: {url}")
        
        # Navigate
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=15000)
        except Exception as nav_error:
            print(f"[Req-{request_id}] Initial navigation timeout, continuing...")
        
        # Wait for network idle
        try:
            await page.wait_for_load_state('networkidle', timeout=30000)
        except:
            print(f"[Req-{request_id}] Network idle timeout, checking content...")
        
        # Check for Cloudflare and bypass if needed
        cloudflare_bypassed = False
        page_source = await page.content()
        
        if "Verifying you are human" in page_source or "Just a moment" in page_source:
            max_attempts = getattr(Cofiguration, 'cloudflare_max_attempts', 20)
            cloudflare_bypassed = await bypass_cloudflare(page, max_attempts)
        
        # Additional wait for dynamic content
        await asyncio.sleep(2)
        
        # Get final HTML and URL
        final_html = await page.content()
        final_url = page.url
        
        print(f"[Req-{request_id}] Successfully fetched - HTML length: {len(final_html)}")
        
        return FetchResponse(
            success=True,
            html=final_html,
            final_url=final_url,
            cloudflare_bypassed=cloudflare_bypassed,
            error=None
        )
        
    except Exception as e:
        print(f"[Req-{request_id}] Error: {e}")
        
        # Try to get partial content
        try:
            if page:
                partial_html = await page.content()
                partial_url = page.url
                
                return FetchResponse(
                    success=False,
                    html=partial_html,
                    final_url=partial_url,
                    cloudflare_bypassed=False,
                    error=str(e)
                )
        except:
            pass
        
        return FetchResponse(
            success=False,
            html=None,
            final_url=None,
            cloudflare_bypassed=False,
            error=str(e)
        )
    
    finally:
        # Always close the page (like await page1.close() in JS)
        if page:
            try:
                await page.close()
                print(f"[Req-{request_id}] Page closed")
            except Exception as e:
                print(f"[Req-{request_id}] Error closing page: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    print("[*] Starting FastAPI server with Playwright...")
    
    if not await initialize_browser():
        print("[-] Failed to initialize browser")
    
    print("[+] Server ready to accept requests")
    
    yield
    
    print("[*] Shutting down server...")
    await cleanup_browser()


app = FastAPI(lifespan=lifespan)

# Request counter
_request_counter = 0


@app.post("/fetch", response_model=FetchResponse)
async def fetch_page(request: FetchRequest):
    """
    Fetch a URL with Cloudflare bypass capability
    
    - **url**: The URL to fetch
    - **timeout**: Timeout in seconds (default: 30)
    """
    global _request_counter
    
    async with _semaphore:
        _request_counter += 1
        request_id = _request_counter
        
        print(f"[Req-{request_id}] Received request for: {request.url}")
        response = await fetch_url_with_page(request.url, request.timeout, request_id)
        print(f"[Req-{request_id}] Completed - Success: {response.success}")
        return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)