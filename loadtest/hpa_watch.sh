#!/usr/bin/env bash
# Capture HPA + pod scaling during a load test into a log you can screenshot.
set -euo pipefail
echo "Watching agent HPA + pods. Ctrl-C to stop."
( kubectl get hpa agent -w & kubectl get pods -l app=agent -w & wait ) \
  | tee loadtest/hpa_scaling_$(date +%s).log
