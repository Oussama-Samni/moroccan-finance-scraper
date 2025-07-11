# Failure Log

## Persistent fetch failures or timeouts  
**Date added:** 2025-07-11  
**Scenario:** All retry attempts in `get_session()` time out (e.g., DNS outage). The script exits silently without notification.  
**Mitigation:** Track consecutive fetch errors in `fetch_failures.json`; if failures exceed a threshold (e.g. 3), send an alert to Telegram and reset the counter.  
**Commit/PR:** TBD
