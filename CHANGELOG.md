# Changelog

## [Critical & High Priority Fixes] - 2026-07-31

### 🔴 CRITICAL FIXES (4 fixes)

[See critical fixes section below for detailed information]

### 🟠 HIGH PRIORITY FIXES (4 additional fixes)

#### **High Priority #1: Unvalidated Custom LLM Provider URL**

**Issue:** Admin could accidentally (or attacker with admin access could intentionally) configure the app to send LLM requests to malicious servers, private IP addresses, or non-existent URLs.

**Root Cause:** Custom provider URLs were accepted without validation, only truncated to 500 chars.

**Fix:**
- Added `_validate_custom_provider_url()` function in `app/main.py`
- Validates that URL uses http/https scheme
- Rejects private/internal IP addresses (127.*, 192.168.*, 10.*, ::1, etc.)
- Rejects malformed URLs without domain
- Called from `update_config()` endpoint before saving

**Files Changed:**
- `app/main.py`: Added URL validation function and integrated into config update

**Impact:**
- Prevents redirecting LLM requests to untrusted endpoints
- Blocks attempts to access internal services through LLM API calls
- Admin gets clear error message if URL is invalid

---

#### **High Priority #2: Missing Rate Limiting on Knowledge Base Uploads**

**Issue:** Knowledge base upload endpoint lacks rate limiting, allowing DOS attacks via rapid large file uploads that exhaust disk space.

**Root Cause:** While file size is limited (8 MB), an admin could upload many files rapidly without throttling.

**Fix:**
- Added `@limiter.limit("10/hour")` decorator to `upload_knowledge()` endpoint
- Limits uploads to 10 per hour per IP address
- Returns 429 Too Many Requests if limit exceeded
- Uses existing slowapi rate limiter already configured in app

**Files Changed:**
- `app/main.py`: Added rate limiter to upload-knowledge endpoint

**Impact:**
- Prevents DOS via disk exhaustion
- Allows legitimate admin usage while blocking abuse
- Rate limit (10/hour) is reasonable for typical operations

---

#### **High Priority #3: Google Calendar API Timeout**

**Issue:** Google Calendar API calls without explicit timeouts could hang indefinitely if Google API is slow/unresponsive, blocking ASGI worker threads and making the entire app unresponsive.

**Root Cause:** Google API client built without timeout configuration. Network issues or slow responses would block the worker.

**Fix:**
- Added `httplib2.Http(timeout=10)` configuration when building Google Calendar service
- Sets 10-second timeout on all Google Calendar API requests
- If API doesn't respond within 10 seconds, request fails gracefully
- Logs warning and returns None (graceful degradation)

**Files Changed:**
- `app/gcal.py`: Added timeout configuration in `_get_service()`

**Impact:**
- Prevents server hangs on slow/unresponsive Google API
- App remains responsive even if Google Calendar is experiencing issues
- Appointments still booked locally, just won't sync to calendar immediately

---

#### **High Priority #4: Strftime Format Incompatibility (Windows)**

**Issue:** Appointment time labels didn't render correctly on Windows deployments due to POSIX-only `%-I` format specifier.

**Root Cause:** Used `strftime("%-I:%M %p")` which works on Linux/Mac but not Windows. Fallback only worked if first attempt raised ValueError, but could fail silently.

**Fix:**
- Changed to use `strftime("%I:%M %p").lstrip("0")` - works on all platforms
- Removes leading zeros robustly on Windows, Linux, and Mac
- No try-except needed, just straightforward string manipulation

**Files Changed:**
- `app/scheduling.py`: Fixed time formatting in `available_slots()`

**Impact:**
- Appointment slots display correctly on Windows deployments
- No platform-specific exceptions or edge cases
- Time labels render consistently across all OSes

---

## Summary of All Changes

### Critical Fixes (4)
✅ Appointment booking race condition  
✅ Google Calendar thread-safety  
✅ JSON parsing error handling  
✅ Lost updates in daily stats  

### High Priority Fixes (4)
✅ Custom LLM provider URL validation  
✅ Rate limiting on KB uploads  
✅ Google Calendar timeout configuration  
✅ Windows strftime compatibility  

### Additional Fix (1)
✅ Orphaned temporary file cleanup  

**Total: 9 fixes applied**

## Testing Recommendations

### High Priority Fix Tests

1. **URL Validation Test**
   ```bash
   # Should accept valid URLs
   curl -X POST http://localhost:8000/api/admin/config \
     -H "X-CSRF-Token: $(csrf_token)" \
     -d '{"baseUrl": "https://api.example.com/v1"}' \
     # Expected: 200 OK

   # Should reject private IPs
   curl -X POST http://localhost:8000/api/admin/config \
     -H "X-CSRF-Token: $(csrf_token)" \
     -d '{"baseUrl": "https://192.168.1.1:8000"}' \
     # Expected: 400 Bad Request - "Private/internal IP addresses not allowed"

   # Should reject localhost
   curl -X POST http://localhost:8000/api/admin/config \
     -d '{"baseUrl": "http://localhost:3000"}' \
     # Expected: 400 Bad Request
   ```

2. **Rate Limiting Test**
   ```bash
   # Send 11 uploads in quick succession
   for i in {1..11}; do
     curl -X POST http://localhost:8000/api/admin/upload-knowledge \
       -F "file=@test.pdf"
   done
   # Expected: 10th succeeds (200), 11th fails (429 Too Many Requests)
   ```

3. **Google Calendar Timeout Test**
   ```bash
   # Test that slow Google API doesn't hang the app
   # Mock slow response, verify app responds in <15 seconds
   curl -v http://localhost:8000/api/appointments/available \
     -d '{"date": "2026-08-15"}'
   # Expected: Responds within timeout (not hanging)
   ```

4. **Windows Strftime Test**
   ```bash
   # On Windows, run:
   curl http://localhost:8000/api/appointments/available \
     -d '{"date": "2026-08-15"}' | jq '.slots[0].label'
   # Expected: "10:00 AM" (no leading zeros, no errors)
   ```

## Files Modified Summary

| File | Changes | Type |
|------|---------|------|
| `app/main.py` | URL validation function + rate limiter | High Priority |
| `app/gcal.py` | Timeout configuration | High Priority |
| `app/scheduling.py` | Strftime fix for Windows | High Priority |
| `app/storage.py` | 4 critical fixes + cleanup | Critical |
| `app/llm.py` | JSON error handling | Critical |
| `CHANGELOG.md` | Updated (this file) | Documentation |

## Deployment Notes

- ✅ **Backward Compatible:** All fixes are backward compatible
- ✅ **No New Dependencies:** Only uses stdlib modules (httplib2 is already available via Google API client)
- ✅ **Non-Breaking:** All API responses unchanged
- ✅ **Zero Configuration:** No new env vars or settings needed

## Changes from Original

### New Features
- URL validation prevents misconfiguration
- Rate limiting on uploads prevents DOS
- Timeouts prevent hangs
- Cross-platform compatibility

### No Changes Needed
- Configuration files
- Database schema
- API contracts
- Dependencies (httplib2 already included with google-api-python-client)

### 🔴 CRITICAL - Race Condition in Appointment Booking

**Issue:** Double-booking vulnerability where concurrent requests could book the same time slot despite client-side availability checks.

**Root Cause:** Availability check (`slot_is_available()`) happened outside the storage lock. Between the check and the write operation, another request could book the same slot.

**Fix:**
- Moved slot availability verification into the atomic `add_appointment()` operation in `storage.py`
- The entire check-and-add operation now happens within a single lock critical section
- If a concurrent request wins the race, the loser gets a 409 Conflict error with message: "That slot is no longer available. Please pick another."
- Updated `main.py` to catch the `ValueError` raised by `add_appointment()` and return proper HTTP 409 response

**Files Changed:**
- `app/storage.py`: Modified `add_appointment()` to verify slot availability atomically
- `app/main.py`: Added error handling for concurrent booking attempts

**Testing:**
```bash
# Test concurrent bookings to verify race condition is fixed
# Before: Both requests succeed (double-booking)
# After: One succeeds, one gets 409 error
```

---

### 🔴 CRITICAL - Thread-Safety Bug in Google Calendar Module

**Issue:** Unprotected global variables in `gcal.py` caused race conditions under concurrent load when initializing the Google Calendar service.

**Root Cause:** The module-level globals `_service` and `_service_load_attempted` were accessed and modified without synchronization. Multiple worker threads could simultaneously enter the service initialization block, causing:
- Duplicate Google API client initialization
- Inconsistent state and unpredictable behavior
- Race conditions during service account credential loading

**Fix:**
- Added `threading.Lock()` to protect access to global `_service` variable
- Implemented double-check pattern: fast path for already-initialized service (no lock), slow path acquires lock before initialization
- Ensures only one thread initializes the service, others wait and reuse the result

**Files Changed:**
- `app/gcal.py`: Added `_lock = threading.Lock()` and thread-safe `_get_service()` function

**Performance Impact:**
- Negligible: Lock only acquired once during app startup. Subsequent calls use fast path without lock contention.

---

### 🔴 CRITICAL - Unhandled JSON Parsing Exception in LLM Module

**Issue:** Invalid JSON responses from LLM providers crashed the chat endpoint with an unhandled `JSONDecodeError` exception.

**Root Cause:** When an LLM API returned a non-JSON response (e.g., HTML error page, gateway error), the code called `resp.json()` without try-except, causing an unhandled exception that would crash the ASGI worker thread and return a 500 error to the user.

**Fix:**
- Wrapped both `resp.json()` calls in try-except blocks (one for Anthropic, one for OpenAI-compatible providers)
- Catches `ValueError` and `httpx.ResponseNotRead` exceptions
- Returns meaningful 502 Bad Gateway error with message: "Invalid JSON response from {provider} API: {error}"
- Provides better debugging information in logs

**Files Changed:**
- `app/llm.py`: Added try-except around `resp.json()` calls with proper error context

**User Experience:**
- Before: Chat crashes with 500 error, no message
- After: User sees friendly message: "Invalid JSON response from Anthropic API" with proper HTTP 502 status code

---

### 🔴 CRITICAL - Lost Updates in Daily Stats Recording

**Issue:** Concurrent requests recording interactions could lose updates due to race condition in read-modify-write operation.

**Root Cause:** The `record_interaction()` and `record_appointment_stat()` functions did atomic reads and writes separately:
1. `_load_stats()` acquires lock, reads data, releases lock
2. Data is modified in-memory (no lock)
3. `_save_stats()` acquires lock, writes data, releases lock

If two concurrent requests executed steps 1-3, both would read the same state, both would increment, and the second write would overwrite the first write, causing one update to be lost.

**Fix:**
- Moved the entire read-modify-write operation inside a single `with _lock:` block
- `_load_stats()` and `_save_stats()` no longer hold locks internally
- `record_interaction()` and `record_appointment_stat()` acquire the lock once and perform all operations atomically
- Ensures every update is preserved, even under high concurrency

**Files Changed:**
- `app/storage.py`: Refactored `_load_stats()`, `_save_stats()`, `record_interaction()`, and `record_appointment_stat()` functions

**Impact:**
- Daily interaction counts and session lists are now accurate under load
- Analytics data no longer loses updates during peak traffic

---

### 🟠 HIGH - Orphaned Temporary Files on Atomic Write Failure

**Issue:** If `os.replace()` failed during atomic write operations, temporary files (`.tmp-*`) were left behind, causing gradual disk space waste.

**Root Cause:** The `_atomic_write_json()` function wrote to a temporary file and called `os.replace()` to move it to the target path. If the rename failed (e.g., permission denied, disk full), the temporary file was never cleaned up.

**Fix:**
- Wrapped the entire atomic write operation in try-finally
- If any exception occurs, the temporary file is explicitly deleted with `unlink(missing_ok=True)`
- Original exception is re-raised so callers know the write failed

**Files Changed:**
- `app/storage.py`: Modified `_atomic_write_json()` with cleanup logic

**Impact:**
- Prevents disk space exhaustion from accumulated `.tmp-*` files
- Better error handling ensures the filesystem is always in a consistent state

---

## Summary of Changes

| Component | Issue | Severity | Status |
|-----------|-------|----------|--------|
| `storage.py` | Appointment booking race condition | Critical | ✅ Fixed |
| `gcal.py` | Thread-safety in service initialization | Critical | ✅ Fixed |
| `llm.py` | Unhandled JSON parse errors | Critical | ✅ Fixed |
| `storage.py` | Lost updates in daily stats | Critical | ✅ Fixed |
| `storage.py` | Orphaned temp files | High | ✅ Fixed |

## Testing Recommendations

1. **Appointment Booking (Race Condition Test)**
   ```bash
   # Use load testing tool to send concurrent booking requests for same slot
   # Expected: 1 succeeds with 200, others get 409
   ab -n 100 -c 50 -p booking.json http://localhost:8000/api/appointments/book
   ```

2. **Google Calendar (Concurrency Test)**
   ```bash
   # Start app with Google Calendar configured
   # Send concurrent requests to /api/appointments/available
   # Expected: No crashes, consistent responses, single service instance
   ```

3. **Stats Recording (Correctness Test)**
   ```bash
   # Send many concurrent chat requests
   # Check daily_stats.json for message count
   # Expected: message count = total requests sent (no lost updates)
   ```

4. **JSON Error Handling (Error Test)**
   ```bash
   # Mock LLM provider that returns invalid JSON
   # Send chat request with invalid base URL
   # Expected: Get 502 error with message, not 500 crash
   ```

## Deployment Notes

- **Backward Compatible:** All fixes are backward compatible. No configuration changes needed.
- **No Database Migration:** File format unchanged, only internal locking behavior improved.
- **Testing:** Run full test suite before deploying. Consider load testing on staging environment.
- **Rollout:** Can be deployed to production immediately. Fixes only improve correctness under concurrent load.

## Related Documentation

See `BUG_REPORT.md` for detailed analysis of all identified issues (critical, high, medium, low priority).
