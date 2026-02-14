# FastAPI Chrome Server

A FastAPI server that manages a persistent Chrome instance to fetch web pages with automatic Cloudflare bypass capability. Handles up to 10 concurrent requests.

## Features

- **Persistent Chrome Instance**: Single Chrome browser runs for the lifetime of the server
- **Concurrent Tab Management**: Each request gets its own tab, up to 10 concurrent requests
- **Cloudflare Bypass**: Automatic detection and bypass using Tab+Space technique
- **Timeout Handling**: Returns partial content if page doesn't fully load within timeout
- **Simple API**: Single POST endpoint for fetching pages

## Requirements

```bash
pip install fastapi uvicorn undetected-chromedriver selenium pydantic
```

## Configuration

Edit `config.py` to configure Cloudflare bypass behavior:

```python
class Cofiguration:
    cloudflare_max_attempts = 20  # Maximum attempts to bypass Cloudflare
```

## Running the Server

With display server (xvfb):
```bash
xvfb-run -a python3 fastapi_chrome_server.py
```

Or if display is already set:
```bash
python3 fastapi_chrome_server.py
```

Server will start on `http://0.0.0.0:8000`

## API Usage

### Endpoint

**POST** `/fetch`

### Request Body

```json
{
  "url": "https://example.com",
  "timeout": 30
}
```

Parameters:
- `url` (required): The URL to fetch
- `timeout` (optional): Timeout in seconds (default: 30)

### Response

```json
{
  "success": true,
  "html": "<!DOCTYPE html>...",
  "final_url": "https://example.com",
  "cloudflare_bypassed": false,
  "error": null
}
```

Fields:
- `success`: Whether the fetch was successful
- `html`: HTML content of the page (or partial content on timeout)
- `final_url`: Final URL after redirects
- `cloudflare_bypassed`: Whether Cloudflare challenge was detected and bypassed
- `error`: Error message if any

## Example Usage

### Using cURL

```bash
curl -X POST "http://localhost:8000/fetch" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "timeout": 20}'
```

### Using Python

```python
import requests

response = requests.post(
    "http://localhost:8000/fetch",
    json={"url": "https://example.com", "timeout": 30}
)

result = response.json()
print(f"Success: {result['success']}")
print(f"HTML length: {len(result['html'])}")
```

### Using Test Client

```bash
python3 test_client.py
```

## How It Works

1. **Server Startup**: Initializes a single persistent Chrome instance
2. **Request Received**: Acquires semaphore slot (max 10 concurrent)
3. **New Tab**: Opens a new tab in the persistent Chrome browser
4. **Navigate**: Loads the requested URL
5. **Cloudflare Check**: Detects and bypasses Cloudflare challenge if present
6. **Wait**: Waits for page to load or timeout
7. **Extract**: Gets HTML content and final URL
8. **Cleanup**: Closes the tab and releases semaphore slot
9. **Response**: Returns result to client

## Concurrency

- Maximum 10 concurrent requests handled via `asyncio.Semaphore`
- Each request operates in its own browser tab
- Tabs are automatically cleaned up after each request
- Chrome instance persists across all requests

## Error Handling

- If page doesn't load completely within timeout, returns partial HTML
- If Chrome fails to initialize, server starts but requests will fail
- If tab closing fails, error is logged but doesn't affect response
- All tabs are isolated to prevent interference between concurrent requests

## Cloudflare Bypass

The server automatically detects Cloudflare challenges by checking for:
- "Verifying you are human"
- "Just a moment"

If detected, it uses the Tab+Space technique:
1. Sends TAB key
2. Waits 0.3 seconds
3. Sends SPACE key
4. Checks if challenge is bypassed
5. Repeats up to `cloudflare_max_attempts` times (configurable)

## Stopping the Server

Press `Ctrl+C` to gracefully shutdown. The server will:
1. Stop accepting new requests
2. Wait for ongoing requests to complete
3. Close all browser tabs
4. Quit the Chrome driver
5. Exit cleanly
