#!/bin/bash
# Cron-триггер: запускает GitHub Actions workflow (обходит ненадёжный GitHub cron)
BASE=/opt/health-summary
TOKEN=$(cat $BASE/gh.token)
REPO=$(grep '^GITHUB_REPO=' $BASE/.env | cut -d= -f2)
MODE="${1:-auto}"
EXTRA="${2:-}"
curl -fsS -X POST -H "Authorization: token $TOKEN" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$REPO/actions/workflows/daily-summary.yml/dispatches" \
  -d "{\"ref\":\"main\",\"inputs\":{\"mode\":\"$MODE\",\"extra_args\":\"$EXTRA\"}}" \
  >> $BASE/logs/trigger.log 2>&1
echo "$(date -u) dispatched mode=$MODE extra=$EXTRA rc=$?" >> $BASE/logs/trigger.log
