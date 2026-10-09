---
name: running-mobile-simulators
description: Manage Android emulators and iOS simulators on shared development hosts. Use when an agent needs a virtual mobile device for app execution, UI automation, evidence capture, recovery, cleanup, or Maestro.
---

# Running mobile simulators

A virtual mobile device exists outside the workspace that uses it. Bind each
agent session to one workspace, one exact device, and the processes that the
agent starts. Agents that share a worktree still need separate devices and
leases.

## Establish the session boundary

Before a device state change, resolve the workspace's real path and inventory:

- connected Android emulator serials and iOS simulator UDIDs;
- active emulator, Simulator, Maestro, MCP, and Viewer processes; and
- active leases or other repository-specific device coordination.

Read [Device leases](references/device-leases.md) before selecting or starting
a device. On a host that can run more than one workspace, an exclusive device
lease is mandatory. Inventory is read-only; do not launch a device, install or
start an app, run automation, or change device state until the lease is held.

This skill manages virtual devices only. Stop if the requested target is a
physical device or a cloud device.

Choose one ownership mode:

- **Attached device.** Select an existing device by its exact identifier. Its
  owner retains lifecycle responsibility.
- **Owned device.** Select one named device that this session will start. The
  session owns its launcher, recovery, and cleanup.

The agent that controls the device owns its session. A coordinator may delegate
the device work, but it must not acquire, mutate, recover, or release the
worker's device. Assign at most one device to each worker. If the task needs
several devices, delegate one device to each worker.

Create one unique session ID and reuse it for the life of the agent session.
Start one session record before launch. Record the agent session ID, workspace,
platform, ownership mode, selected device, launcher intent, owned processes,
and required app build. For an attached device, record its exact serial or UDID
now. For a new owned Android emulator, record the AVD name and add its serial
immediately after launch. Record an owned iOS simulator's UDID before launch.

Acquire the lease atomically under the device's canonical identity. The lease
is the host-visible session record. It must include the workspace's real path
and the agent session ID. An attached device still requires a lease even though
its lifecycle remains externally owned. Hold the lease through app execution,
automation, evidence, recovery, and cleanup. If another session holds it,
select a different device or stop. Never wait while mutating the contested
device.

This step is complete when the session record names one device and ownership
mode, and any required lease is held.

## Check readiness without changing state

Run `scripts/check-readiness.sh` when a workflow needs reusable preflight after
it has selected and leased one device. Pass the canonical device, exact runtime
target, real workspace, and agent session ID:

```bash
scripts/check-readiness.sh \
  --device android-avd:<avd-name> \
  --target <emulator-serial> \
  --workspace "$(git rev-parse --show-toplevel)" \
  --session-id <agent-session-id>
```

For iOS, use `ios-simulator:<udid>` and pass the same UDID as the target. The
command reads the existing lease, the selected virtual device, and the fixed
process fingerprints described in [Device leases](references/device-leases.md).
It never claims or repairs a lease. It also leaves device launch and recovery
to the later lifecycle steps.

The JSON result contains one `checks` array. Each check reports `pass`,
`fail`, `unmet`, or `not-required`. A failed or unmet check includes the
expected and observed values. Exit status `0` means that every required check
passed. Exit status `1` means that a precondition failed or lacked evidence.
Exit status `2` means that the request or checker was invalid. The default
budget for each external observation is 10 seconds. Use `--timeout` to change
that budget.

The checker covers generic lease, device, process, and optional Maestro MCP
readiness. The consuming project still owns its Expo, Metro, build, application
identity, authentication, and data environment checks.

This step is complete when the result names the selected device and observed
lease owner, and every check required by the consuming workflow passes.

## Prepare the exact device

Read the reference for the selected platform before launch, readiness checks,
automation, recovery, or cleanup:

- [Android Emulator](references/android.md)
- [iOS Simulator](references/ios.md)

If the task uses Maestro as a launcher or controller, also read
[Maestro](references/maestro.md) before starting it.

For an attached device, skip launch and continue with its platform identity and
readiness checks. For an owned device, use the caller's requested launcher or
the platform launcher for the named device. After launch, add the exact device
identifier, launcher command, and launcher PID to the session record. If the
launcher exits, record that it is not persistent.

This step is complete when the selected device passes its platform identity and
readiness checks under the exact recorded serial or UDID.

## Bind automation and evidence

Use the recorded identifier for every targeted device and automation command.
The platform reference defines the exact command binding. The Maestro reference
defines its CLI, MCP, file, and Viewer binding.

Before evidence capture, prove that the selected device runs the app build from
the required workspace or deployment target. Record the device identifier and
app build identity with the evidence.

This step is complete when automation and evidence name the recorded device
and current app build.

## Recover within the boundary

Use this bounded order:

1. Reconnect automation once.
2. Restart the app once if the failure remains.
3. Restart the device once only when the session owns it.

After each recovery action, repeat the platform identity and readiness checks.
Also recheck the app build after an app or device restart.

If an attached device needs a lifecycle restart, stop and report that its owner
must recover it. If owned-device recovery fails, stop and report the exact
failure.

Recovery is complete when the same recorded device is ready, the required app
build is running, and automation reconnects. Otherwise, stop after the bounded
sequence and report the failed check.

## Release owned resources

Follow the platform and Maestro cleanup instructions that apply to the session.
Stop only the processes and device recorded as owned by the current session.
Release the lease by its unique session ID after those resources exit.

Preserve attached devices, shared ADB, unrelated simulators, and other
workspaces' processes.

Cleanup is complete when every owned resource has exited and every attached or
unrelated resource remains available.
