from fastapi import FastAPI
from pydantic import BaseModel
import undetected_chromedriver as uc
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
import asyncio
import time
from config import Cofiguration
from contextlib import asynccontextmanager
import os
import threading

# Global Chrome driver - single instance
_global_driver = None
_driver_lock = threading.Lock()  # For thread-safe driver operations
_semaphore = asyncio.Semaphore(10)  # Max 10 concurrent requests
_request_counter = 0


class FetchRequest(BaseModel):
    url: str
    timeout: int = 30


class FetchResponse(BaseModel):
    success: bool
    html: str | None
    final_url: str | None
    cloudflare_bypassed: bool
    error: str | None


def initialize_chrome():
    """Initialize single Chrome driver with your working config"""
    global _global_driver
    
    try:
        print("[*] Initializing Chrome driver...")
        
        options = uc.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-software-rasterizer")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-plugins")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--no-first-run")
        options.add_argument("--window-size=1920,1080")
        
        # Use same profile as your working code
        user_data_dir = '/tmp/chrome_profile'
        
        # Preserve existing profile
        if not os.path.exists(user_data_dir):
            os.makedirs(user_data_dir, exist_ok=True)
            print(f"[*] Created profile: {user_data_dir}")
        else:
            print(f"[*] Using existing profile: {user_data_dir}")
        
        options.add_argument(f'--user-data-dir={user_data_dir}')
        
        _global_driver = uc.Chrome(
            options=options,
            version_main=None,
            use_subprocess=True
        )
        
        _global_driver.set_page_load_timeout(100)
        
        # Store home handle
        _global_driver._home_handle = _global_driver.current_window_handle
        
        print("[+] Chrome driver initialized successfully")
        print(f"[+] Home handle: {_global_driver._home_handle[:8]}...")
        return True
        
    except Exception as e:
        print(f"[-] Failed to initialize Chrome: {e}")
        import traceback
        traceback.print_exc()
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


def bypass_cloudflare_on_handle(driver, window_handle, max_attempts=20):
    """Bypass Cloudflare on specific window handle"""
    attempts = 0
    last_check_time = time.time()
    
    while attempts < max_attempts:
        try:
            current_time = time.time()
            
            if current_time - last_check_time >= 3:
                attempts += 1
                
                # Switch to this tab to check
                with _driver_lock:
                    if window_handle not in driver.window_handles:
                        return False
                    driver.switch_to.window(window_handle)
                    page_source = driver.page_source
                
                # Check for Cloudflare
                is_cloudflare = "Verifying you are human" in page_source or "Just a moment" in page_source
                
                if not is_cloudflare:
                    return True
                
                print(f"[{window_handle[:8]}] Cloudflare attempt {attempts}/{max_attempts}")
                
                # Send Tab + Space
                with _driver_lock:
                    if window_handle in driver.window_handles:
                        driver.switch_to.window(window_handle)
                        actions = ActionChains(driver)
                        actions.send_keys(Keys.TAB).perform()
                        time.sleep(0.3)
                        actions.send_keys(Keys.SPACE).perform()
                
                time.sleep(2)
                
                # Check if bypassed
                with _driver_lock:
                    if window_handle in driver.window_handles:
                        driver.switch_to.window(window_handle)
                        page_source = driver.page_source
                        if "Verifying you are human" not in page_source and "Just a moment" not in page_source:
                            print(f"[{window_handle[:8]}] Cloudflare bypassed!")
                            return True
                
                last_check_time = current_time
            
            time.sleep(0.5)
            
        except Exception as e:
            print(f"[{window_handle[:8]}] Cloudflare bypass error: {e}")
            return False
    
    return False


async def fetch_url_with_tab(url: str, timeout: int, request_id: int):
    """Fetch URL in new tab with independent polling"""
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
    start_time = time.time()
    
    try:
        # Step 1: Open new tab (thread-safe)
        def _open_tab():
            nonlocal tab_handle
            with _driver_lock:
                _global_driver.switch_to.new_window('tab')
                tab_handle = _global_driver.current_window_handle
                print(f"[Req-{request_id}] Opened tab: {tab_handle[:8]}...")
                
                # Navigate
                _global_driver.get(url)
                print(f"[Req-{request_id}] Started loading: {url}")
        
        await asyncio.to_thread(_open_tab)
        
        if not tab_handle:
            raise Exception("Failed to create tab")
        
        # Step 2: Poll this tab until loaded (without blocking other tabs)
        cloudflare_bypassed = False
        page_loaded = False
        
        while time.time() - start_time < timeout:
            await asyncio.sleep(1)  # Check every second
            
            # Check page status
            def _check_page():
                nonlocal cloudflare_bypassed, page_loaded
                
                with _driver_lock:
                    # Make sure tab still exists
                    if tab_handle not in _global_driver.window_handles:
                        raise Exception("Tab was closed")
                    
                    # Switch to this tab
                    _global_driver.switch_to.window(tab_handle)
                    
                    # Get page source
                    page_source = _global_driver.page_source
                    
                    # Check for Cloudflare
                    if "Verifying you are human" in page_source or "Just a moment" in page_source:
                        return 'cloudflare'
                    
                    # Check if page loaded
                    ready_state = _global_driver.execute_script("return document.readyState")
                    if ready_state == "complete" and len(page_source) > 500:
                        return 'loaded'
                    
                    return 'loading'
            
            try:
                status = await asyncio.to_thread(_check_page)
                
                if status == 'cloudflare':
                    print(f"[Req-{request_id}] Cloudflare detected, bypassing...")
                    # Bypass Cloudflare on this specific tab
                    bypassed = await asyncio.to_thread(
                        bypass_cloudflare_on_handle,
                        _global_driver,
                        tab_handle,
                        getattr(Cofiguration, 'cloudflare_max_attempts', 20)
                    )
                    if bypassed:
                        cloudflare_bypassed = True
                        print(f"[Req-{request_id}] Cloudflare bypassed, continuing load...")
                    
                elif status == 'loaded':
                    page_loaded = True
                    print(f"[Req-{request_id}] Page loaded successfully")
                    break
                    
            except Exception as e:
                print(f"[Req-{request_id}] Error checking page: {e}")
                break
        
        # Step 3: Extract final HTML and URL
        def _extract_content():
            with _driver_lock:
                if tab_handle in _global_driver.window_handles:
                    _global_driver.switch_to.window(tab_handle)
                    final_html = _global_driver.page_source
                    final_url = _global_driver.current_url
                    return final_html, final_url
                return None, None
        
        html, final_url = await asyncio.to_thread(_extract_content)
        
        if not html:
            raise Exception("Could not extract content")
        
        print(f"[Req-{request_id}] Successfully fetched - HTML length: {len(html)}")
        
        return FetchResponse(
            success=True,
            html=html,
            final_url=final_url,
            cloudflare_bypassed=cloudflare_bypassed,
            error=None
        )
        
    except Exception as e:
        print(f"[Req-{request_id}] Error: {e}")
        
        # Try to get partial content
        try:
            def _get_partial():
                with _driver_lock:
                    if tab_handle and tab_handle in _global_driver.window_handles:
                        _global_driver.switch_to.window(tab_handle)
                        return _global_driver.page_source, _global_driver.current_url
                return None, None
            
            partial_html, partial_url = await asyncio.to_thread(_get_partial)
            
            if partial_html:
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
        # Step 4: Close tab and return to home
        if tab_handle:
            def _close_tab():
                try:
                    with _driver_lock:
                        if tab_handle in _global_driver.window_handles:
                            _global_driver.switch_to.window(tab_handle)
                            _global_driver.close()
                            print(f"[Req-{request_id}] Closed tab: {tab_handle[:8]}...")
                        
                        # Return to home handle
                        if hasattr(_global_driver, '_home_handle') and _global_driver._home_handle in _global_driver.window_handles:
                            _global_driver.switch_to.window(_global_driver._home_handle)
                        elif _global_driver.window_handles:
                            _global_driver.switch_to.window(_global_driver.window_handles[0])
                except Exception as e:
                    print(f"[Req-{request_id}] Error closing tab: {e}")
            
            await asyncio.to_thread(_close_tab)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    print("[*] Starting FastAPI server with undetected-chromedriver...")
    
    if not initialize_chrome():
        print("[-] Failed to initialize Chrome")
    
    print("[+] Server ready - accepts 10 concurrent requests with true parallel tab loading!")
    
    yield
    
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
    global _request_counter
    
    async with _semaphore:
        _request_counter += 1
        request_id = _request_counter
        
        print(f"[Req-{request_id}] Received request for: {request.url}")
        response = await fetch_url_with_tab(request.url, request.timeout, request_id)
        print(f"[Req-{request_id}] Completed - Success: {response.success}")
        return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)