#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
device=""
target=""
workspace=""
session_id=""
lease_root="/tmp/running-mobile-simulators-$(id -u)"
observation_timeout="10"
adb_port="5037"
maestro_probe=""
require_maestro_process=0
require_viewer_process=0

usage() {
  cat <<'EOF'
Usage: check-readiness.sh --device ID --target ID --workspace PATH --session-id ID [options]

Options:
  --timeout SECONDS                 Observation budget for each external probe (default: 10)
  --lease-root PATH                 Existing lease namespace (default: per-user /tmp namespace)
  --adb-port PORT                   Existing loopback ADB server port (default: 5037)
  --maestro-probe PATH|-            Exact JSON result from session-local list_devices({})
  --require-maestro-process         Require a recorded Maestro MCP process fingerprint
  --require-viewer-process          Require a recorded Viewer process fingerprint
  -h, --help                        Show this help
EOF
}

invalid() {
  printf 'check-readiness: %s\n' "$1" >&2
  exit 2
}

while (($#)); do
  case "$1" in
    --device|--target|--workspace|--session-id|--timeout|--lease-root|--adb-port|--maestro-probe)
      (($# >= 2)) || invalid "$1 requires a value"
      case "$1" in
        --device) device="$2" ;;
        --target) target="$2" ;;
        --workspace) workspace="$2" ;;
        --session-id) session_id="$2" ;;
        --timeout) observation_timeout="$2" ;;
        --lease-root) lease_root="$2" ;;
        --adb-port) adb_port="$2" ;;
        --maestro-probe) maestro_probe="$2" ;;
      esac
      shift 2
      ;;
    --require-maestro-process)
      require_maestro_process=1
      shift
      ;;
    --require-viewer-process)
      require_viewer_process=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) invalid "unknown argument: $1" ;;
  esac
done

test -n "$device" || invalid "--device is required"
test -n "$target" || invalid "--target is required"
test -n "$workspace" || invalid "--workspace is required"
test -n "$session_id" || invalid "--session-id is required"
[[ "$observation_timeout" =~ ^([0-9]+([.][0-9]+)?|[.][0-9]+)$ ]] || invalid "--timeout must be a positive number"
[[ ! "$observation_timeout" =~ ^0*([.]0*)?$ ]] || invalid "--timeout must be greater than zero"
[[ "$adb_port" =~ ^[0-9]+$ ]] || invalid "--adb-port must be an integer"
adb_port="$((10#$adb_port))"
((adb_port >= 1 && adb_port <= 65535)) || invalid "--adb-port must be between 1 and 65535"
command -v jq >/dev/null 2>&1 || invalid "jq is required"
command -v python3 >/dev/null 2>&1 || invalid "python3 is required"
command -v shasum >/dev/null 2>&1 || invalid "shasum is required"
test -d "$workspace" || invalid "workspace does not exist: $workspace"
workspace="$(cd "$workspace" && pwd -P)"

case "$device" in
  android-avd:*)
    platform="android"
    platform_device="${device#android-avd:}"
    test -n "$platform_device" || invalid "Android AVD name is empty"
    [[ "$target" =~ ^emulator-[0-9]+$ ]] || invalid "Android target must be an emulator serial"
    ;;
  ios-simulator:*)
    platform="ios"
    platform_device="${device#ios-simulator:}"
    test -n "$platform_device" || invalid "iOS simulator UDID is empty"
    test "$target" = "$platform_device" || invalid "iOS target must match the canonical UDID"
    ;;
  *) invalid "device must use android-avd: or ios-simulator:" ;;
esac

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/simulator-readiness.XXXXXX")"
trap 'rm -rf "$tmp_dir"' EXIT
checks_file="$tmp_dir/checks.jsonl"
: >"$checks_file"

jstr() {
  jq -cn --arg value "$1" '$value'
}

check_pass() {
  jq -cn --arg check "$1" '{check:$check,status:"pass"}' >>"$checks_file"
}

check_not_required() {
  jq -cn --arg check "$1" '{check:$check,status:"not-required"}' >>"$checks_file"
}

check_failure() {
  local check="$1" status="$2" expected="$3" observed="$4"
  jq -cn \
    --arg check "$check" \
    --arg status "$status" \
    --argjson expected "$expected" \
    --argjson observed "$observed" \
    '{check:$check,status:$status,expected:$expected,observed:$observed}' \
    >>"$checks_file"
}

read_value() {
  sed -n '1p' "$1"
}

snapshot_lease() {
  local name path
  if ! test -d "$lease_dir"; then
    printf 'lease:missing\n'
    return
  fi
  printf 'lease:directory\n'
  for name in \
    session-id workspace device serial ownership-mode launcher-disposition \
    launcher-pid launcher-start-time launcher-command launcher-cwd launcher-uid \
    maestro-mcp-pid maestro-mcp-start-time maestro-mcp-command maestro-mcp-cwd maestro-mcp-uid \
    viewer-pid viewer-start-time viewer-command viewer-cwd viewer-uid; do
    path="$lease_dir/$name"
    if test -f "$path"; then
      printf 'file:%s:' "$name"
      shasum -a 256 "$path" | awk '{print $1}'
    elif test -e "$path"; then
      printf 'other:%s\n' "$name"
    else
      printf 'missing:%s\n' "$name"
    fi
  done
}

run_probe() {
  local stdout_path="$1" stderr_path="$2"
  shift 2
  python3 "$script_dir/probe-command.py" --timeout "$observation_timeout" -- "$@" \
    >"$stdout_path" 2>"$stderr_path"
}

probe_reason() {
  local stderr_path="$1" status="$2" detail
  detail="$(sed -n '1p' "$stderr_path")"
  test ! -s "$stderr_path" || cat "$stderr_path" >&2
  if test -n "$detail"; then
    printf '%s' "$detail"
  else
    printf 'probe exited %s' "$status"
  fi
}

expected_object() {
  jq -cn "$@"
}

valid_ios_device_list() {
  jq -e '
    (.devices | type) == "object" and
    all(.devices[];
      if type == "array" then
        all(.[];
          if type == "object" then
            (.udid | type) == "string" and
            (.isAvailable | type) == "boolean" and
            (.state | type) == "string"
          else false end
        )
      else false end
    )
  ' "$1" >/dev/null 2>&1
}

valid_maestro_devices() {
  jq -e '
    type == "array" and
    all(.[];
      if type == "object" then
        (.device_id | type) == "string" and
        (.platform | type) == "string" and
        (.type | type) == "string" and
        (.connected | type) == "boolean"
      else false end
    )
  ' "$1" >/dev/null 2>&1
}

device_hash="$(printf '%s' "$device" | shasum -a 256 | awk '{print $1}')"
lease_dir="$lease_root/$device_hash.lease"
initial_snapshot="$(snapshot_lease)"
owner_session=""
owner_workspace=""
owner_device=""
ownership_established=1

if ! test -d "$lease_dir"; then
  check_failure "lease.exists" "fail" \
    "$(expected_object --arg path "$lease_dir" '{path:$path,state:"present"}')" \
    "$(expected_object --arg path "$lease_dir" '{path:$path,state:"missing"}')"
  ownership_established=0
else
  check_pass "lease.exists"
  test -f "$lease_dir/session-id" && owner_session="$(read_value "$lease_dir/session-id")"
  test -f "$lease_dir/workspace" && owner_workspace="$(read_value "$lease_dir/workspace")"
  test -f "$lease_dir/device" && owner_device="$(read_value "$lease_dir/device")"

  for field in session-id workspace device; do
    case "$field" in
      session-id) expected="$session_id"; observed="$owner_session" ;;
      workspace) expected="$workspace"; observed="$owner_workspace" ;;
      device) expected="$device"; observed="$owner_device" ;;
    esac
    if ! test -f "$lease_dir/$field" || test -z "$observed"; then
      check_failure "lease.$field" "unmet" "$(jstr "$expected")" "null"
      ownership_established=0
    elif test "$observed" != "$expected"; then
      check_failure "lease.$field" "fail" "$(jstr "$expected")" "$(jstr "$observed")"
      ownership_established=0
    else
      check_pass "lease.$field"
    fi
  done

  if test "$platform" = "android"; then
    if ! test -f "$lease_dir/serial" || test -z "$(read_value "$lease_dir/serial" 2>/dev/null || true)"; then
      check_failure "lease.serial" "unmet" "$(jstr "$target")" "null"
      ownership_established=0
    else
      observed_serial="$(read_value "$lease_dir/serial")"
      if test "$observed_serial" = "$target"; then
        check_pass "lease.serial"
      else
        check_failure "lease.serial" "fail" "$(jstr "$target")" "$(jstr "$observed_serial")"
        ownership_established=0
      fi
    fi
  fi
fi

current_process_identity() {
  local pid="$1" prefix="$2" stdout_path stderr_path status
  stdout_path="$tmp_dir/$prefix.out"
  stderr_path="$tmp_dir/$prefix.err"
  if run_probe "$stdout_path" "$stderr_path" env LC_ALL=C ps -ww -p "$pid" -o lstart=; then
    current_started_at="$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$stdout_path")"
  else
    status=$?
    current_error="$(probe_reason "$stderr_path" "$status")"
    return 1
  fi
  if run_probe "$stdout_path" "$stderr_path" env LC_ALL=C ps -ww -p "$pid" -o command=; then
    current_command="$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$stdout_path")"
  else
    status=$?
    current_error="$(probe_reason "$stderr_path" "$status")"
    return 1
  fi
  if run_probe "$stdout_path" "$stderr_path" env LC_ALL=C ps -ww -p "$pid" -o uid=; then
    current_uid="$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$stdout_path")"
  else
    status=$?
    current_error="$(probe_reason "$stderr_path" "$status")"
    return 1
  fi
  if run_probe "$stdout_path" "$stderr_path" env LC_ALL=C ps -ww -p "$pid" -o ppid=; then
    current_ppid="$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$stdout_path")"
  else
    status=$?
    current_error="$(probe_reason "$stderr_path" "$status")"
    return 1
  fi
  if run_probe "$stdout_path" "$stderr_path" lsof -a -p "$pid" -d cwd -Fn; then
    current_cwd="$(sed -n 's/^n//p' "$stdout_path" | sed -n '1p')"
    if test -n "$current_cwd" && test -d "$current_cwd"; then
      current_cwd="$(cd "$current_cwd" && pwd -P)"
    fi
  else
    status=$?
    current_error="$(probe_reason "$stderr_path" "$status")"
    return 1
  fi
  test -n "$current_started_at" && test -n "$current_command" && test -n "$current_uid" && test -n "$current_cwd" || {
    current_error="process observation was incomplete"
    return 1
  }
}

check_process() {
  local role="$1" required="$2" prefix="$3" field missing_fields="" any_recorded=0
  for field in pid start-time command cwd uid; do
    if test -e "$lease_dir/$prefix-$field"; then
      any_recorded=1
    fi
    if ! test -f "$lease_dir/$prefix-$field" || test -z "$(read_value "$lease_dir/$prefix-$field" 2>/dev/null || true)"; then
      missing_fields="${missing_fields}${missing_fields:+,}$prefix-$field"
    fi
  done
  if ((any_recorded == 0 && required == 0)); then
    check_not_required "process.$role"
    return
  fi
  if test -n "$missing_fields"; then
    check_failure "process.$role" "unmet" \
      "$(expected_object --arg fields "$prefix-pid,$prefix-start-time,$prefix-command,$prefix-cwd,$prefix-uid" '{fingerprintFiles:($fields|split(","))}')" \
      "$(expected_object --arg fields "$missing_fields" '{missing:($fields|split(","))}')"
    return
  fi

  recorded_pid="$(read_value "$lease_dir/$prefix-pid")"
  recorded_started_at="$(read_value "$lease_dir/$prefix-start-time")"
  recorded_command="$(read_value "$lease_dir/$prefix-command")"
  recorded_cwd="$(read_value "$lease_dir/$prefix-cwd")"
  recorded_uid="$(read_value "$lease_dir/$prefix-uid")"
  [[ "$recorded_pid" =~ ^[1-9][0-9]*$ ]] && [[ "$recorded_uid" =~ ^[0-9]+$ ]] || {
    check_failure "process.$role" "unmet" \
      '"positive PID and numeric UID"' \
      "$(expected_object --arg pid "$recorded_pid" --arg uid "$recorded_uid" '{pid:$pid,uid:$uid}')"
    return
  }
  if ! test -d "$recorded_cwd"; then
    check_failure "process.$role" "unmet" '"recorded real cwd"' "$(jstr "$recorded_cwd")"
    return
  fi
  recorded_cwd="$(cd "$recorded_cwd" && pwd -P)"
  current_error=""
  if ! current_process_identity "$recorded_pid" "$prefix"; then
    check_failure "process.$role" "unmet" \
      "$(expected_object --arg pid "$recorded_pid" '{pid:$pid,state:"running with recorded fingerprint"}')" \
      "$(expected_object --arg reason "$current_error" '{reason:$reason}')"
    return
  fi
  expected_identity="$(expected_object --arg pid "$recorded_pid" --arg uid "$recorded_uid" --arg startedAt "$recorded_started_at" --arg command "$recorded_command" --arg cwd "$recorded_cwd" '{pid:($pid|tonumber),uid:($uid|tonumber),startedAt:$startedAt,command:$command,cwd:$cwd}')"
  observed_identity="$(expected_object --arg pid "$recorded_pid" --arg uid "$current_uid" --arg ppid "$current_ppid" --arg startedAt "$current_started_at" --arg command "$current_command" --arg cwd "$current_cwd" '{pid:($pid|tonumber),ppid:(if $ppid == "" then null else ($ppid|tonumber?) end),uid:($uid|tonumber?),startedAt:$startedAt,command:$command,cwd:$cwd}')"
  if test "$recorded_started_at" != "$current_started_at" || \
      test "$recorded_command" != "$current_command" || \
      test "$recorded_uid" != "$current_uid" || \
      test "$recorded_cwd" != "$current_cwd"; then
    check_failure "process.$role" "fail" "$expected_identity" "$observed_identity"
    return
  fi

  case "$role" in
    launcher)
      if test "$platform" = "android"; then
        case " $recorded_command " in
          *" -avd $platform_device "*) ;;
          *)
            check_failure "process.$role" "fail" \
              "$(expected_object --arg avd "$platform_device" '{argument:"-avd",value:$avd}')" \
              "$(expected_object --arg command "$recorded_command" '{command:$command}')"
            return
            ;;
        esac
      elif [[ "$recorded_command" != *"$target"* ]]; then
        check_failure "process.$role" "fail" \
          "$(expected_object --arg target "$target" '{target:$target}')" \
          "$(expected_object --arg command "$recorded_command" '{command:$command}')"
        return
      fi
      ;;
    maestro-mcp)
      case " $recorded_command " in
        *" maestro mcp "*|*"/maestro mcp "*|*" maestro.cli.AppKt mcp "*) ;;
        *)
          check_failure "process.$role" "fail" '"Maestro MCP command or runtime"' "$(jstr "$recorded_command")"
          return
          ;;
      esac
      case "$recorded_command" in
        *" --working-dir $workspace"|*" --working-dir=$workspace") ;;
        *)
          check_failure "process.$role" "fail" \
            "$(expected_object --arg workspace "$workspace" '{argument:"--working-dir",value:$workspace}')" \
            "$(expected_object --arg command "$recorded_command" '{command:$command}')"
          return
          ;;
      esac
      ;;
  esac
  check_pass "process.$role"
}

if ((ownership_established == 0)); then
  check_failure "platform.readiness" "unmet" '"matching lease ownership"' '"lease ownership was not established"'
  check_failure "process.launcher" "unmet" '"matching lease ownership"' '"lease ownership was not established"'
  if ((require_maestro_process)); then
    check_failure "process.maestro-mcp" "unmet" '"matching lease ownership"' '"lease ownership was not established"'
  else
    check_not_required "process.maestro-mcp"
  fi
  if ((require_viewer_process)); then
    check_failure "process.viewer" "unmet" '"matching lease ownership"' '"lease ownership was not established"'
  else
    check_not_required "process.viewer"
  fi
  if test -n "$maestro_probe"; then
    check_failure "automation.maestro-mcp" "unmet" '"matching lease ownership"' '"lease ownership was not established"'
  else
    check_not_required "automation.maestro-mcp"
  fi
else
  ownership_mode=""
  if test -f "$lease_dir/ownership-mode"; then
    ownership_mode="$(read_value "$lease_dir/ownership-mode")"
  fi
  case "$ownership_mode" in
    owned|attached) check_pass "lease.ownership-mode" ;;
    "") check_failure "lease.ownership-mode" "unmet" '"owned or attached"' "null" ;;
    *) check_failure "lease.ownership-mode" "fail" '"owned or attached"' "$(jstr "$ownership_mode")" ;;
  esac

  if test "$platform" = "android"; then
    adb_stdout="$tmp_dir/adb.json"
    adb_stderr="$tmp_dir/adb.err"
    if python3 "$script_dir/adb-readonly.py" --serial "$target" --port "$adb_port" --timeout "$observation_timeout" >"$adb_stdout" 2>"$adb_stderr" && jq -e '.schemaVersion == 1 and (.queries | type == "object")' "$adb_stdout" >/dev/null 2>&1; then
      test ! -s "$adb_stderr" || cat "$adb_stderr" >&2
      for spec in 'state:device' 'bootCompleted:1' "avdName:$platform_device"; do
        query="${spec%%:*}"
        expected="${spec#*:}"
        query_status="$(jq -r --arg query "$query" '.queries[$query].status // "unmet"' "$adb_stdout")"
        check_name="platform.android.$query"
        if test "$query_status" != "pass"; then
          reason="$(jq -r --arg query "$query" '.queries[$query].reason // "missing query result"' "$adb_stdout")"
          check_failure "$check_name" "unmet" "$(jstr "$expected")" "$(expected_object --arg reason "$reason" '{reason:$reason}')"
        else
          observed="$(jq -r --arg query "$query" '.queries[$query].value' "$adb_stdout")"
          if test "$observed" = "$expected"; then
            check_pass "$check_name"
          else
            check_failure "$check_name" "fail" "$(jstr "$expected")" "$(jstr "$observed")"
          fi
        fi
      done
    else
      reason="$(sed -n '1p' "$adb_stderr")"
      test ! -s "$adb_stderr" || cat "$adb_stderr" >&2
      test -n "$reason" || reason="ADB adapter returned invalid output"
      check_failure "platform.android" "unmet" '"complete fixed ADB observations"' "$(expected_object --arg reason "$reason" '{reason:$reason}')"
    fi
  else
    ios_list_before="$tmp_dir/ios-before.json"
    ios_list_before_err="$tmp_dir/ios-before.err"
    if run_probe "$ios_list_before" "$ios_list_before_err" xcrun simctl list devices -j; then
      if ! valid_ios_device_list "$ios_list_before"; then
        check_failure "platform.ios.device" "unmet" '"valid simctl device list"' '"malformed simctl JSON"'
        ios_ready=0
      else
        ios_match_count="$(jq --arg target "$target" '[.devices[][] | select(.udid == $target)] | length' "$ios_list_before")"
        if test "$ios_match_count" != "1"; then
          check_failure "platform.ios.device" "fail" \
            "$(expected_object --arg udid "$target" '{udid:$udid,count:1,isAvailable:true,state:"Booted"}')" \
            "$(expected_object --argjson count "$ios_match_count" '{count:$count}')"
          ios_ready=0
        else
          ios_available="$(jq -r --arg target "$target" '[.devices[][] | select(.udid == $target)][0].isAvailable' "$ios_list_before")"
          ios_state="$(jq -r --arg target "$target" '[.devices[][] | select(.udid == $target)][0].state' "$ios_list_before")"
          if test "$ios_available" = "true" && test "$ios_state" = "Booted"; then
            check_pass "platform.ios.device"
            ios_ready=1
          else
            check_failure "platform.ios.device" "fail" \
              "$(expected_object --arg udid "$target" '{udid:$udid,isAvailable:true,state:"Booted"}')" \
              "$(expected_object --arg udid "$target" --arg available "$ios_available" --arg state "$ios_state" '{udid:$udid,isAvailable:($available == "true"),state:$state}')"
            ios_ready=0
          fi
        fi
      fi
    else
      status=$?
      reason="$(probe_reason "$ios_list_before_err" "$status")"
      check_failure "platform.ios.device" "unmet" '"simctl device list"' "$(expected_object --arg reason "$reason" '{reason:$reason}')"
      ios_ready=0
    fi
    if ((ios_ready)); then
      ios_boot_out="$tmp_dir/ios-bootstatus.out"
      ios_boot_err="$tmp_dir/ios-bootstatus.err"
      if run_probe "$ios_boot_out" "$ios_boot_err" xcrun simctl bootstatus "$target"; then
        check_pass "platform.ios.bootstatus"
        ios_after="$tmp_dir/ios-after.json"
        ios_after_err="$tmp_dir/ios-after.err"
        if run_probe "$ios_after" "$ios_after_err" xcrun simctl list devices -j; then
          if ! valid_ios_device_list "$ios_after"; then
            check_failure "platform.ios.recheck" "unmet" '"valid second simctl device list"' '"malformed simctl JSON"'
          else
            after_count="$(jq --arg target "$target" '[.devices[][] | select(.udid == $target)] | length' "$ios_after")"
            after_available="$(jq -r --arg target "$target" '[.devices[][] | select(.udid == $target)][0].isAvailable // false' "$ios_after")"
            after_state="$(jq -r --arg target "$target" '[.devices[][] | select(.udid == $target)][0].state // "missing"' "$ios_after")"
            if test "$after_count" = "1" && test "$after_available" = "true" && test "$after_state" = "Booted"; then
              check_pass "platform.ios.recheck"
            else
              check_failure "platform.ios.recheck" "fail" \
                "$(expected_object --arg udid "$target" '{udid:$udid,isAvailable:true,state:"Booted"}')" \
                "$(expected_object --argjson count "$after_count" --arg available "$after_available" --arg state "$after_state" '{count:$count,isAvailable:($available == "true"),state:$state}')"
            fi
          fi
        else
          status=$?
          reason="$(probe_reason "$ios_after_err" "$status")"
          check_failure "platform.ios.recheck" "unmet" '"second simctl device list"' "$(expected_object --arg reason "$reason" '{reason:$reason}')"
        fi
      else
        status=$?
        reason="$(probe_reason "$ios_boot_err" "$status")"
        check_failure "platform.ios.bootstatus" "unmet" '"successful read-only bootstatus"' "$(expected_object --arg reason "$reason" '{reason:$reason}')"
        check_failure "platform.ios.recheck" "unmet" '"successful bootstatus"' '"bootstatus did not complete"'
      fi
    else
      check_failure "platform.ios.bootstatus" "unmet" '"available Booted device"' '"device readiness was not established"'
      check_failure "platform.ios.recheck" "unmet" '"successful bootstatus"' '"bootstatus was not run"'
    fi
  fi

  launcher_disposition=""
  test -f "$lease_dir/launcher-disposition" && launcher_disposition="$(read_value "$lease_dir/launcher-disposition")"
  case "$launcher_disposition" in
    persistent)
      if test "$ownership_mode" = "owned"; then
        check_process "launcher" 1 "launcher"
      else
        check_failure "process.launcher" "fail" '"owned device for a persistent launcher"' "$(jstr "$ownership_mode")"
      fi
      ;;
    completed)
      if test "$ownership_mode" = "owned"; then
        check_not_required "process.launcher"
      else
        check_failure "process.launcher" "fail" '"owned device for a completed launcher"' "$(jstr "$ownership_mode")"
      fi
      ;;
    external)
      if test "$ownership_mode" = "attached"; then
        check_not_required "process.launcher"
      else
        check_failure "process.launcher" "fail" '"attached device for an external launcher"' "$(jstr "$ownership_mode")"
      fi
      ;;
    "") check_failure "process.launcher" "unmet" '"persistent, completed, or external launcher disposition"' "null" ;;
    *) check_failure "process.launcher" "fail" '"persistent, completed, or external launcher disposition"' "$(jstr "$launcher_disposition")" ;;
  esac
  check_process "maestro-mcp" "$require_maestro_process" "maestro-mcp"
  check_process "viewer" "$require_viewer_process" "viewer"

  if test -z "$maestro_probe"; then
    check_not_required "automation.maestro-mcp"
  else
    probe_input="$tmp_dir/maestro-probe.json"
    if test "$maestro_probe" = "-"; then
      cat >"$probe_input"
    elif test -f "$maestro_probe"; then
      cp "$maestro_probe" "$probe_input"
    else
      : >"$probe_input"
    fi
    if ! test -s "$probe_input"; then
      check_failure "automation.maestro-mcp" "unmet" \
        "$(expected_object --arg device_id "$target" '{device_id:$device_id,connected:true}')" \
        '"list_devices probe unavailable"'
    elif ! jq -e . "$probe_input" >/dev/null 2>&1; then
      check_failure "automation.maestro-mcp" "unmet" \
        "$(expected_object --arg device_id "$target" '{device_id:$device_id,connected:true}')" \
        '"malformed list_devices result"'
    else
      maestro_devices="$tmp_dir/maestro-devices.json"
      if jq -e 'type == "object" and .isError == true' "$probe_input" >/dev/null 2>&1; then
        check_failure "automation.maestro-mcp" "unmet" \
          "$(expected_object --arg device_id "$target" '{device_id:$device_id,connected:true}')" \
          '"list_devices tool returned an error"'
      elif ! jq -ce '
        def devices:
          if type == "array" then .
          elif type == "object" and (.devices | type) == "array" then .devices
          elif type == "object" and (.structuredContent | type) == "object" and (.structuredContent.devices | type) == "array" then .structuredContent.devices
          elif type == "object" and (.content | type) == "array" and (.content | length) == 1 and
              (.content[0] | type) == "object" and .content[0].type == "text" and (.content[0].text | type) == "string" then
            (.content[0].text | fromjson | devices)
          else error("missing devices array") end;
        devices
      ' "$probe_input" >"$maestro_devices" 2>/dev/null; then
        check_failure "automation.maestro-mcp" "unmet" \
          "$(expected_object --arg device_id "$target" '{device_id:$device_id,connected:true}')" \
          '"list_devices result has no device array"'
      elif ! valid_maestro_devices "$maestro_devices"; then
        check_failure "automation.maestro-mcp" "unmet" \
          "$(expected_object --arg device_id "$target" '{device_id:$device_id,connected:true}')" \
          '"list_devices result has malformed device entries"'
      else
        maestro_matches="$(jq --arg target "$target" '[.[] | select(.device_id == $target)] | length' "$maestro_devices")"
        expected_type="emulator"
        test "$platform" = "ios" && expected_type="simulator"
        if test "$maestro_matches" != "1"; then
          check_failure "automation.maestro-mcp" "fail" \
            "$(expected_object --arg id "$target" --arg platform "$platform" --arg type "$expected_type" '{device_id:$id,platform:$platform,type:$type,connected:true,count:1}')" \
            "$(expected_object --argjson count "$maestro_matches" '{count:$count}')"
        else
          maestro_observed="$(jq -c --arg target "$target" '[.[] | select(.device_id == $target)][0] | {device_id,platform,type,connected}' "$maestro_devices")"
          if jq -e --arg target "$target" --arg platform "$platform" --arg type "$expected_type" \
              '.device_id == $target and .platform == $platform and .type == $type and .connected == true' \
              <<<"$maestro_observed" >/dev/null; then
            check_pass "automation.maestro-mcp"
          else
            check_failure "automation.maestro-mcp" "fail" \
              "$(expected_object --arg id "$target" --arg platform "$platform" --arg type "$expected_type" '{device_id:$id,platform:$platform,type:$type,connected:true}')" \
              "$maestro_observed"
          fi
        fi
      fi
    fi
  fi
fi

final_snapshot="$(snapshot_lease)"
if test "$initial_snapshot" = "$final_snapshot"; then
  check_pass "lease.unchanged"
else
  check_failure "lease.unchanged" "fail" '"unchanged lease evidence"' '"lease evidence changed during inspection"'
fi

owner_json="$(jq -cn \
  --arg session "$owner_session" \
  --arg workspace "$owner_workspace" \
  --arg device "$owner_device" \
  '{sessionId:(if $session == "" then null else $session end),workspace:(if $workspace == "" then null else $workspace end),device:(if $device == "" then null else $device end)}')"
if jq -se 'all(.[]; .status == "pass" or .status == "not-required")' "$checks_file" >/dev/null; then
  ok=true
  exit_status=0
else
  ok=false
  exit_status=1
fi
jq -cn \
  --argjson ok "$ok" \
  --arg canonical "$device" \
  --arg target "$target" \
  --arg platform "$platform" \
  --argjson owner "$owner_json" \
  --slurpfile checks "$checks_file" \
  '{schemaVersion:1,ok:$ok,device:{canonical:$canonical,target:$target,platform:$platform},owner:$owner,checks:$checks}'
exit "$exit_status"
