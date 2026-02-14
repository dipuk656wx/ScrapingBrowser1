import requests
import json

# API endpoint
API_URL = "http://localhost:8000/fetch"


def test_fetch(url, timeout=30):
    """Test the fetch API"""
    print(f"\n{'='*60}")
    print(f"Testing URL: {url}")
    print(f"Timeout: {timeout}s")
    print(f"{'='*60}")
    
    payload = {
        "url": url,
        "timeout": timeout
    }
    
    try:
        response = requests.post(API_URL, json=payload)
        response.raise_for_status()
        
        result = response.json()
        
        print(f"\nResponse:")
        print(f"  Success: {result['success']}")
        print(f"  Final URL: {result['final_url']}")
        print(f"  Cloudflare Bypassed: {result['cloudflare_bypassed']}")
        print(f"  Error: {result['error']}")
        
        if result['html']:
            print(f"  HTML Length: {len(result['html'])} characters")
            print(f"  HTML Preview: {result['html'][:200]}...")
        else:
            print(f"  HTML: None")
        
        return result
        
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}")
        return None


def test_concurrent_requests():
    """Test multiple concurrent requests"""
    import concurrent.futures
    
    urls = [
        "https://example.com",
        "https://httpbin.org/html",
        "https://jsonplaceholder.typicode.com",
        "https://www.google.com",
        "https://github.com",
    ]
    
    print(f"\n{'='*60}")
    print(f"Testing {len(urls)} concurrent requests")
    print(f"{'='*60}")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(test_fetch, url, 15) for url in urls]
        results = [future.result() for future in concurrent.futures.as_completed(futures)]
    
    print(f"\n{'='*60}")
    print(f"All requests completed")
    print(f"Successful: {sum(1 for r in results if r and r['success'])}/{len(results)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    # Test single request
    test_fetch("https://example.com", timeout=20)
    
    # Uncomment to test concurrent requests
    # test_concurrent_requests()
