import os

bind = f"0.0.0.0:{int(os.environ.get('PORT', '8000'))}"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
worker_class = "gthread"
timeout = 180
graceful_timeout = 30
keepalive = 5
max_requests = 1000
max_requests_jitter = 100
accesslog = "-"
errorlog = "-"
# Never log query strings containing verification/reset tokens.
access_log_format = "%(m)s %(U)s %(H)s %(s)s %(L)s"
forwarded_allow_ips = "*"  # Railway's ingress terminates HTTPS.
