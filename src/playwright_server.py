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
        
        # Use same profile path as your original code for consistency
        profile_path = '/tmp/chrome_profile'
        
        # Create if doesn't exist, but DON'T delete existing profile
        if not os.path.exists(profile_path):
            os.makedirs(profile_path, exist_ok=True)
            print(f"[*] Created new profile at: {profile_path}")
        else:
            print(f"[*] Using existing profile at: {profile_path}")
        
        # Remove stale lock file only
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
                '--disable-blink-features=AutomationControlled',  # Critical!
                '--exclude-switches=enable-automation',  # Hide automation
                '--disable-infobars',  # Remove info bars
                '--window-size=1920,1080',
                '--disable-popup-blocking',
                '--disable-background-networking',
                '--disable-default-apps',
                '--disable-sync',
                '--metrics-recording-only',
                '--mute-audio',
                '--no-first-run',
                '--disable-features=IsolateOrigins,site-per-process,Automation',  # Disable automation feature
                '--allow-running-insecure-content',
                '--disable-web-security',
                '--use-fake-ui-for-media-stream',
                '--use-fake-device-for-media-stream'
            ],
            ignore_https_errors=True,
            locale='en-US',
            timezone_id='UTC',
            chromium_sandbox=False
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
    """Bypass Cloudflare challenge using Tab+Space technique (matches JS code)"""
    print("[*] Checking for Cloudflare/security challenge...")
    
    attempts = 0
    last_check_time = time.time()
    security_detected = False
    
    while attempts < max_attempts:
        try:
            current_time = time.time()
            
            if current_time - last_check_time >= 3:
                attempts += 1
                
                # Get page content
                page_source = await page.content()
                try:
                    page_text = await page.evaluate("() => document.body.innerText")
                except:
                    page_text = ""
                
                # Check for Cloudflare/security challenges (same as JS code)
                is_challenge = (
                    "Verifying you are human" in page_source or 
                    "Just a moment" in page_source or
                    "challenges.cloudflare.com" in page_source or
                    "Performing security verification" in page_source or
                    "Incompatible browser" in page_text or
                    "security service" in page_text.lower()
                )
                
                if is_challenge:
                    security_detected = True
                    print(f"[*] Cloudflare/security challenge detected! Attempt {attempts}/{max_attempts}")
                    
                    # Try Tab + Space (first 5 attempts)
                    if attempts <= 5:
                        try:
                            await page.keyboard.press('Tab')
                            await asyncio.sleep(0.3)
                            await page.keyboard.press('Space')
                            print(f"[*] Tab + Space sent")
                        except Exception as e:
                            print(f"[*] Could not send keys, waiting for auto-bypass...")
                    
                    # Wait for Cloudflare to auto-verify (like JS code - 4 seconds)
                    await asyncio.sleep(4)
                    
                elif (security_detected or len(page_source) > 500) and len(page_text.strip()) > 100:
                    # Security was detected and now page has real content
                    print(f"[+] Cloudflare bypass successful!")
                    return True
                    
                elif len(page_source) < 200:
                    # Page still loading
                    print(f"[*] Page loading... (attempt {attempts})")
                    await asyncio.sleep(2)
                    
                else:
                    # Page has content, no challenge detected
                    print(f"[+] Page content loaded (no challenge or already bypassed)")
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
        
        # Enhanced stealth - remove all automation detection
        await page.add_init_script("""
            // Remove webdriver flag
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });
            
            // Remove automation property
            delete navigator.__proto__.webdriver;
            
            // Override plugins
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
            
            // Override languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });
            
            // Add chrome object
            window.chrome = {
                runtime: {},
                loadTimes: () => {},
                csi: () => {},
                app: {}
            };
            
            // Spoof permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
            
            // Hide automation in toString
            const originalToString = Function.prototype.toString;
            Function.prototype.toString = function() {
                if (this === window.navigator.constructor) {
                    return 'function Navigator() { [native code] }';
                }
                return originalToString.call(this);
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