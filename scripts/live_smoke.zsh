#!/bin/zsh
# Explicitly launched by the operator. This script makes paid API requests.
unsetopt xtrace verbose
set -euo pipefail

project_dir=${0:A:h:h}
cd "$project_dir"
python_bin=${MULTI_AGENT_PYTHON:-python3}

print 'This test makes up to 2 probe calls plus 6 workflow calls to DeepSeek.'
print 'It tests text collaboration; it does not need Docker, Search or Git.'

if [[ -z ${DEEPSEEK_API_KEY:-} ]]; then
  if [[ ! -t 0 ]]; then
    print -u2 'Use an interactive terminal or configure DEEPSEEK_API_KEY in the environment.'
    exit 2
  fi
  read -rs 'DEEPSEEK_API_KEY?DeepSeek API key (hidden): '
  print
fi
export DEEPSEEK_API_KEY
trap 'unset DEEPSEEK_API_KEY' EXIT

if [[ -z ${DEEPSEEK_MODEL:-} ]]; then
  if [[ ! -t 0 ]]; then
    print -u2 'Set DEEPSEEK_MODEL explicitly in a noninteractive environment.'
    exit 2
  fi
  read 'selected_model?Model ID [deepseek-flash]: '
  export DEEPSEEK_MODEL=${selected_model:-deepseek-flash}
fi
export DEEPSEEK_BASE_URL=${DEEPSEEK_BASE_URL:-https://api.deepseek.com}
export DEEPSEEK_THINKING=${DEEPSEEK_THINKING:-true}
export DEEPSEEK_REASONING_EFFORT=${DEEPSEEK_REASONING_EFFORT:-low}
export DEEPSEEK_MAX_OUTPUT_TOKENS=${DEEPSEEK_MAX_OUTPUT_TOKENS:-4096}

mkdir -p data/live-tests
run_dir=$(mktemp -d "$project_dir/data/live-tests/run-XXXXXXXX")
print "Results will be saved in: $run_dir"
"$python_bin" -m maplatform doctor --require-live | tee "$run_dir/doctor-report.json"
"$python_bin" -m maplatform probe --report "$run_dir/probe-report.json"

"$python_bin" -m maplatform --db "$run_dir/text.sqlite" evaluate examples/text_smoke.json \
  --report "$run_dir/text-report.json" --max-calls 6 --max-tasks 2 --deadline 180
print "Completed. Review: $run_dir/text-report.json"
