"""
Load test for the research endpoint.

Drives concurrent research requests so you can watch the custom-metric HPA scale
the agent deployment on `inflight_requests`. Run against a deployed cluster or
local stack.

    pip install locust
    locust -f loadtest/locustfile.py --host http://localhost:8000 \
           --users 25 --spawn-rate 5 --run-time 5m --headless

While it runs, in another terminal:
    kubectl get hpa agent -w
    kubectl get pods -l app=agent -w

You should see inflight_requests climb, the HPA raise replica count toward
maxReplicas, then scale back down after the run. Capture this for the README /
a short screen recording -- it converts "I configured autoscaling" into
"I load-tested it and watched it scale."
"""
from __future__ import annotations

import random

from locust import HttpUser, between, task

QUESTIONS = [
    "What are the latest developments in AI regulation?",
    "Summarize recent technology earnings reports.",
    "What is happening with global interest rate policy?",
    "What are the most recent developments in renewable energy?",
    "Summarize the latest major geopolitical events.",
]


class ResearchUser(HttpUser):
    wait_time = between(1, 3)

    @task
    def research(self):
        self.client.post(
            "/research",
            json={"question": random.choice(QUESTIONS), "recency_days": 30},
            timeout=180,
            name="POST /research",
        )
