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
import shutil

# Configuration
DRIVER_POOL_SIZE = getattr(Cofiguration, 'driver_pool_size', 5)

# Global driver pool
_driver_pool = []
_driver_queue = None


class FetchRequest(BaseModel):
    url: str
    timeout: int = 30


class FetchResponse(BaseModel):
    success: bool
    html: str | None
    final_url: str | None
    cloudflare_bypassed: bool
    error: str | None


def create_chrome_driver(driver_id):
    """Create Chrome driver with your working undetected-chromedriver config"""
    try:
        print(f"[*] Initializing Chrome driver #{driver_id}...")
        
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
        
        # IMPORTANT: Use shared profile for all drivers to reuse Cloudflare cookies
        user_data_dir = '/tmp/chrome_profile_fastapi'
        
        # Create if doesn't exist, preserve if exists
        if not os.path.exists(user_data_dir):
            os.makedirs(user_data_dir, exist_ok=True)
        
        options.add_argument(f'--user-data-dir={user_data_dir}')
        
        driver = uc.Chrome(
            options=options,
            version_main=None,
            use_subprocess=True
        )
        
        driver.set_page_load_timeout(100)
        print(f"[+] Chrome driver #{driver_id} initialized")
        return {'id': driver_id, 'driver': driver}
        
    except Exception as e:
        print(f"[-] Failed to initialize Chrome driver #{driver_id}: {e}")
        return None


def initialize_driver_pool():
    """Initialize pool of Chrome drivers"""
    global _driver_pool, _driver_queue
    
    print(f"[*] Initializing driver pool with {DRIVER_POOL_SIZE} drivers...")
    print(f"[*] All drivers will share profile: /tmp/chrome_profile")
    
    for i in range(DRIVER_POOL_SIZE):
        driver_obj = create_chrome_driver(i)
        if driver_obj:
            _driver_pool.append(driver_obj)
        time.sleep(2)  # Small delay between driver launches
    
    print(f"[+] Driver pool initialized with {len(_driver_pool)}/{DRIVER_POOL_SIZE} drivers")
    
    # Create async queue
    _driver_queue = asyncio.Queue()
    for driver_obj in _driver_pool:
        _driver_queue.put_nowait(driver_obj)
    
    return len(_driver_pool) > 0


def cleanup_driver_pool():
    """Cleanup all Chrome drivers"""
    global _driver_pool
    
    print(f"[*] Closing {len(_driver_pool)} Chrome drivers...")
    
    for driver_obj in _driver_pool:
        try:
            driver_obj['driver'].quit()
            print(f"[+] Driver #{driver_obj['id']} closed")
        except Exception as e:
            print(f"[!] Error closing driver #{driver_obj['id']}: {e}")
    
    _driver_pool.clear()
    print("[+] All drivers closed")


def bypass_cloudflare(driver, max_attempts=20):
    """Bypass Cloudflare using your working Tab+Space technique"""
    print("[*] Checking for Cloudflare challenge...")
    
    attempts = 0
    last_check_time = time.time()
    
    while attempts < max_attempts:
        try:
            current_time = time.time()
            
            if current_time - last_check_time >= 3:
                attempts += 1
                
                page_source = driver.page_source
                
                # Check for Cloudflare
                if "Verifying you are human" not in page_source and "Just a moment" not in page_source:
                    print("[+] No Cloudflare challenge or already bypassed")
                    return True
                
                print(f"[*] Cloudflare detected - Attempt {attempts}/{max_attempts}: Sending Tab + Space...")
                
                actions = ActionChains(driver)
                actions.send_keys(Keys.TAB).perform()
                time.sleep(0.3)
                actions.send_keys(Keys.SPACE).perform()
                
                time.sleep(2)
                
                # Check if bypassed
                page_source = driver.page_source
                if "Verifying you are human" not in page_source and "Just a moment" not in page_source:
                    print("[+] Cloudflare challenge bypassed!")
                    return True
                
                last_check_time = current_time
            
            time.sleep(0.5)
            
        except Exception as e:
            print(f"[-] Cloudflare bypass error: {e}")
            return False
    
    print(f"[-] Failed to bypass Cloudflare after {max_attempts} attempts")
    return False


async def fetch_url_with_driver(url: str, timeout: int):
    """Fetch URL using a driver from the pool"""
    global _driver_queue
    
    if _driver_queue is None:
        return FetchResponse(
            success=False,
            html=None,
            final_url=None,
            cloudflare_bypassed=False,
            error="Driver pool not initialized"
        )
    
    # Get a driver from the pool (waits if all busy)
    driver_obj = await _driver_queue.get()
    driver = driver_obj['driver']
    driver_id = driver_obj['id']
    
    try:
        print(f"[Driver-{driver_id}] Acquired for: {url}")
        
        def _fetch():
            # Navigate
            print(f"[Driver-{driver_id}] Navigating...")
            driver.get(url)
            time.sleep(3)
            
            # Check for Cloudflare
            cloudflare_bypassed = False
            page_source = driver.page_source
            
            if "Verifying you are human" in page_source or "Just a moment" in page_source:
                max_attempts = getattr(Cofiguration, 'cloudflare_max_attempts', 20)
                cloudflare_bypassed = bypass_cloudflare(driver, max_attempts)
            
            # Wait for page load
            start_time = time.time()
            while time.time() - start_time < timeout:
                time.sleep(1)
                try:
                    ready_state = driver.execute_script("return document.readyState")
                    if ready_state == "complete":
                        break
                except:
                    break
            
            # Get final content
            final_html = driver.page_source
            final_url = driver.current_url
            
            return final_html, final_url, cloudflare_bypassed
        
        # Execute in thread pool
        result = await asyncio.to_thread(_fetch)
        html, final_url, cloudflare_bypassed = result
        
        print(f"[Driver-{driver_id}] Success - HTML length: {len(html)}")
        
        return FetchResponse(
            success=True,
            html=html,
            final_url=final_url,
            cloudflare_bypassed=cloudflare_bypassed,
            error=None
        )
        
    except Exception as e:
        print(f"[Driver-{driver_id}] Error: {e}")
        
        # Try to get partial content
        try:
            partial_html = await asyncio.to_thread(lambda: driver.page_source)
            partial_url = await asyncio.to_thread(lambda: driver.current_url)
            
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
        # Return driver to pool
        await _driver_queue.put(driver_obj)
        print(f"[Driver-{driver_id}] Returned to pool")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    print("[*] Starting FastAPI server with undetected-chromedriver pool...")
    
    if not initialize_driver_pool():
        print("[-] Failed to initialize driver pool")
    
    print(f"[+] Server ready with {len(_driver_pool)} drivers")
    print(f"[+] Can handle {len(_driver_pool)} concurrent requests")
    
    yield
    
    print("[*] Shutting down...")
    cleanup_driver_pool()


app = FastAPI(lifespan=lifespan)


@app.post("/fetch", response_model=FetchResponse)
async def fetch_page(request: FetchRequest):
    """
    Fetch a URL with Cloudflare bypass
    
    - **url**: The URL to fetch
    - **timeout**: Timeout in seconds (default: 30)
    """
    print(f"[API] Request for: {request.url}")
    response = await fetch_url_with_driver(request.url, request.timeout)
    print(f"[API] Completed - Success: {response.success}")
    return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)