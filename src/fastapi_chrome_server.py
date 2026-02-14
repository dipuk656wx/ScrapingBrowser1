from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import undetected_chromedriver as uc
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
import asyncio
import time
import atexit
import signal
from config import Cofiguration
from contextlib import asynccontextmanager

# Global Chrome driver
_global_driver = None
_semaphore = asyncio.Semaphore(10)  # Max 10 concurrent requests


class FetchRequest(BaseModel):
    url: str
    timeout: int = 30  # Default 30 seconds


class FetchResponse(BaseModel):
    success: bool
    html: str | None
    final_url: str | None
    cloudflare_bypassed: bool
    error: str | None


def initialize_chrome():
    """Initialize persistent Chrome driver"""
    global _global_driver
    
    try:
        print("[*] Initializing persistent Chrome driver...")
        
        options = uc.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-software-rasterizer")
        options.add_argument("--disable-plugins")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--no-first-run")
        options.add_argument("--window-size=1920,1080")
        
        user_data_dir = '/tmp/chrome_profile_fastapi'
        options.add_argument(f'--user-data-dir={user_data_dir}')
        
        _global_driver = uc.Chrome(
            options=options,
            version_main=None,
            use_subprocess=True
        )
        
        _global_driver.set_page_load_timeout(100)
        print("[+] Chrome driver initialized successfully")
        return True
        
    except Exception as e:
        print(f"[-] Failed to initialize Chrome driver: {e}")
        return False


def cleanup_chrome():
    """Cleanup Chrome driver"""
    global _global_driver
    
    try:
        if _global_driver:
            print("[*] Closing Chrome driver...")
            _global_driver.quit()
            _global_driver = None
            print("[+] Chrome driver closed")
    except Exception as e:
        print(f"[!] Error closing driver: {e}")


def bypass_cloudflare(driver, max_attempts=20):
    """Bypass Cloudflare challenge using Tab+Space technique"""
    print("[*] Checking for Cloudflare challenge...")
    
    attempts = 0
    last_check_time = time.time()
    
    while attempts < max_attempts:
        try:
            current_time = time.time()
            
            if current_time - last_check_time >= 3:
                attempts += 1
                
                page_source = driver.page_source
                
                # Check if Cloudflare challenge is present
                if "Verifying you are human" not in page_source and "Just a moment" not in page_source:
                    print("[+] No Cloudflare challenge detected or already bypassed")
                    return True
                
                print(f"[*] Cloudflare detected - Attempt {attempts}/{max_attempts}: Sending Tab + Space...")
                
                actions = ActionChains(driver)
                actions.send_keys(Keys.TAB).perform()
                time.sleep(0.3)
                actions.send_keys(Keys.SPACE).perform()
                
                time.sleep(2)
                
                # Check if challenge passed
                page_source = driver.page_source
                if "Verifying you are human" not in page_source and "Just a moment" not in page_source:
                    print("[+] Cloudflare challenge bypassed!")
                    return True
                
                last_check_time = current_time
            
            time.sleep(0.5)
            
        except Exception as e:
            print(f"[-] Error during Cloudflare bypass: {e}")
            return False
    
    print(f"[-] Failed to bypass Cloudflare after {max_attempts} attempts")
    return False


async def fetch_url_with_tab(url: str, timeout: int):
    """Fetch URL in a new tab with Cloudflare bypass"""
    global _global_driver
    
    if _global_driver is None:
        return FetchResponse(
            success=False,
            html=None,
            final_url=None,
            cloudflare_bypassed=False,
            error="Chrome driver not initialized"
        )
    
    tab_handle = None
    original_window = None
    
    try:
        # Run Selenium operations in thread pool
        def _fetch():
            nonlocal tab_handle, original_window
            
            # Store original window
            original_window = _global_driver.current_window_handle
            
            # Open new tab
            _global_driver.switch_to.new_window('tab')
            tab_handle = _global_driver.current_window_handle
            print(f"[*] Opened new tab: {tab_handle}")
            
            # Navigate to URL
            print(f"[*] Navigating to: {url}")
            _global_driver.get(url)
            
            # Initial wait for page load
            time.sleep(3)
            
            # Check for Cloudflare and bypass if needed
            cloudflare_bypassed = False
            page_source = _global_driver.page_source
            
            if "Verifying you are human" in page_source or "Just a moment" in page_source:
                max_attempts = getattr(Cofiguration, 'cloudflare_max_attempts', 20)
                cloudflare_bypassed = bypass_cloudflare(_global_driver, max_attempts)
            
            # Wait for remaining timeout
            start_time = time.time()
            while time.time() - start_time < timeout:
                time.sleep(1)
                # Check if page is still loading
                ready_state = _global_driver.execute_script("return document.readyState")
                if ready_state == "complete":
                    break
            
            # Get final HTML and URL
            final_html = _global_driver.page_source
            final_url = _global_driver.current_url
            
            return final_html, final_url, cloudflare_bypassed
        
        # Execute in thread pool to avoid blocking event loop
        result = await asyncio.to_thread(_fetch)
        html, final_url, cloudflare_bypassed = result
        
        return FetchResponse(
            success=True,
            html=html,
            final_url=final_url,
            cloudflare_bypassed=cloudflare_bypassed,
            error=None
        )
        
    except Exception as e:
        print(f"[-] Error fetching URL: {e}")
        
        # Try to get partial content
        try:
            partial_html = await asyncio.to_thread(lambda: _global_driver.page_source)
            partial_url = await asyncio.to_thread(lambda: _global_driver.current_url)
            
            return FetchResponse(
                success=False,
                html=partial_html,
                final_url=partial_url,
                cloudflare_bypassed=False,
                error=str(e)
            )
        except:
            return FetchResponse(
                success=False,
                html=None,
                final_url=None,
                cloudflare_bypassed=False,
                error=str(e)
            )
    
    finally:
        # Always close the tab
        try:
            if tab_handle:
                def _close_tab():
                    if tab_handle in _global_driver.window_handles:
                        _global_driver.switch_to.window(tab_handle)
                        _global_driver.close()
                        print(f"[*] Closed tab: {tab_handle}")
                    
                    # Switch back to original window if it exists
                    if original_window and original_window in _global_driver.window_handles:
                        _global_driver.switch_to.window(original_window)
                
                await asyncio.to_thread(_close_tab)
        except Exception as e:
            print(f"[!] Error closing tab: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    # Startup
    print("[*] Starting FastAPI server...")
    if not initialize_chrome():
        print("[-] Failed to initialize Chrome, server may not work properly")
    
    # Setup cleanup handlers
    atexit.register(cleanup_chrome)
    signal.signal(signal.SIGINT, lambda sig, frame: (cleanup_chrome(), exit(0)))
    signal.signal(signal.SIGTERM, lambda sig, frame: (cleanup_chrome(), exit(0)))
    
    print("[+] Server ready to accept requests")
    
    yield
    
    # Shutdown
    print("[*] Shutting down server...")
    cleanup_chrome()


app = FastAPI(lifespan=lifespan)


@app.post("/fetch", response_model=FetchResponse)
async def fetch_page(request: FetchRequest):
    """
    Fetch a URL with Cloudflare bypass capability
    
    - **url**: The URL to fetch
    - **timeout**: Timeout in seconds (default: 30)
    """
    async with _semaphore:  # Limit to 10 concurrent requests
        print(f"[*] Received request for URL: {request.url} (timeout: {request.timeout}s)")
        response = await fetch_url_with_tab(request.url, request.timeout)
        print(f"[+] Request completed - Success: {response.success}")
        return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
