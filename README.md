# star-wars-70mm-watch

Watches AMC Lincoln Square 13 for the 50th anniversary re-release of the
original 1977 *Star Wars* (opening Feb 19, 2027) in IMAX 70mm.

A GitHub Actions workflow runs every 20 minutes, scans Feb 19-23, 2027, and
opens a labeled issue when new showtimes appear. First run seeds state
silently. Fetch failures are transient (AMC's traffic protection); the next
run retries automatically.
