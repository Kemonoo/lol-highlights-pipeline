#!/usr/bin/env bash
# Review a few of the filter's decisions per night (POSIX counterpart to review.bat).
#   ./review.sh           opens http://localhost:8766
#   ./review.sh --stats   prints the per-gate results
cd "$(dirname "$0")" && exec ./venv/bin/python -m pipeline.tools.review_queue "$@"
