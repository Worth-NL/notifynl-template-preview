import os

from notifications_utils.gunicorn.defaults import set_gunicorn_defaults

set_gunicorn_defaults(globals())


workers = 5
timeout = int(os.getenv("HTTP_SERVE_TIMEOUT_SECONDS", 30))

# k8s readinessProbe/livenessProbe (see templatePreviewApi in notifynl-charts-private)
# hit /_status?simple=1 continuously for the pod's entire lifetime, not just until
# first-ready, at periodSeconds=3/10 respectively (~1560 requests/hour/pod). That's
# real request volume from gunicorn's perspective, so a low max_requests budget gets
# consumed almost entirely by probes rather than actual rendering - measured live in
# notifynl-test, worker restarts were ~100% probe-driven when this was 10. Keep this
# high enough that real render traffic (not probes) is what drives recycling; jitter
# staggers the 5 workers so they don't all hit the ceiling on the same request.
max_requests = 10000
max_requests_jitter = 1000
