# ReelSeat: Movie Booking Website with a Load Balancer

A movie ticket booking website that runs on **three servers behind a load balancer**. You can switch servers off, slow them down or make them fail, and watch the load balancer keep the website running.

Built with plain Python (no extra installs) and HTML, CSS and JavaScript.

## Features

**Booking website**
- Browse movies, filter by language, and search
- Pick a date, cinema and showtime
- Live seat map: seats you pick are held for 5 minutes, and other people see them as taken within 2 seconds
- Sign up, log in, and see your tickets in **My bookings**
- Cancel a booking up to 2 hours before the show

**Load balancer**
- Three ways to share work between servers:
  - **Round robin**: servers take turns
  - **Least connections**: the least busy server gets the next request
  - **Weighted**: stronger servers get more requests
- Health checks every 2 seconds
- If a server is down, the request is retried on another server
- Circuit breaker: a server with 3 errors in a row is taken out of rotation
- Shows an "all servers down" page if every server is off

**Server panel** (`/admin`)
- Switch each server on or off
- Test modes: **Slow** (1.5 s per request) and **Errors** (fails about half its requests)
- Send test traffic and see which server answered and how fast
- Live event log

**Owner dashboard**
- Revenue, tickets sold and recent bookings
- Bookings handled by each server
- Add and edit movies, upload posters
- Add and delete showtimes

## How it works

```
Browser  -->  Load balancer (port 8080)  -->  Server 1 (port 8001)
                                         -->  Server 2 (port 8002)
                                         -->  Server 3 (port 8003)
                                                    |
                                          Shared database (SQLite)
```

All three servers use the same database, so your login, seats and bookings work no matter which server handles your request.

## How to run

You need **Python 3.9 or newer**. Nothing else to install.

```
python run.py
```

Then open:
- Website: http://localhost:8080
- Server panel: http://localhost:8080/admin

On the first run, an owner account is created. Its email and password are shown in the terminal and saved in `owner_login.txt`. Log in with them to open the **Owner dashboard**.

Press `Ctrl+C` to stop.

## Things to try

1. Open the server panel and click **Send 30 requests**. Each server gets 10.
2. Put Server 2 in **Slow** mode, switch to **Least connections**, and send again. Server 2 gets far fewer requests.
3. Choose **Weighted** and give Server 1 weight 3. It gets 3 out of every 5 requests.
4. Put Server 3 in **Errors** mode. Its failed requests are retried elsewhere, and it is soon taken out.
5. Switch a server off and book a ticket. It still works.
6. Open the same showtime in two browsers (one normal, one private window) with two accounts. Pick a seat in one and watch it become unavailable in the other.

## Project files

| File | What it does |
|---|---|
| `run.py` | Starts everything |
| `loadbalancer.py` | The load balancer, strategies, health checks and server panel |
| `app.py` | The booking servers (all three run this code) |
| `database.py` | The shared database and starting data |
| `static/` | The web pages (website, styles, server panel) |

## Notes

- This is a learning project. No real payments are taken.
- Movie details are based on films released around Deepavali 2026. Running times and certificates can be added from the owner dashboard.
- Movie posters are not included because they are copyrighted. The owner can upload posters they have permission to use.
