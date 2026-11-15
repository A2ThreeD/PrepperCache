#!/usr/bin/env bash
# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Shell helpers for loading the installer configuration.

load_preppercache_env() {
  local env_file="${1:?missing env file path}"
  # shellcheck disable=SC1090
  source "$env_file"
  export PREPPERCACHE_ENV_FILE="${PREPPERCACHE_ENV_FILE:-$env_file}"
}
