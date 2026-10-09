# Maestro

Maestro is an optional launcher and controller. The session still gets its
ownership, device identity, readiness, recovery, and cleanup rules from the
main skill and the selected platform reference.

## Start a standard device

Use device specifications that the current host reports:

```bash
maestro list-devices --platform <android-or-ios>
maestro start-device \
  --platform <android-or-ios> \
  --device-model <reported-model> \
  --device-os <reported-os>
```

On a shared host, use `start-device` only when the inventory maps the requested
model and OS to one canonical AVD name or Simulator UDID that the session has
already leased. If Maestro cannot reveal that identity before startup, use the
platform launcher with a pre-leased device instead. Do not let `start-device`
discover or reuse a device after mutation has begun.

Reserve `--force-create` for explicit device maintenance because it can replace
a canonical device. After startup, follow the platform reference to resolve
the exact serial or UDID and prove readiness.

## Bind CLI and MCP

Select the recorded device for each CLI flow:

```bash
maestro --device <serial-or-udid> test <flow-or-directory>
```

Do not pass several identifiers or use local sharding across agent-owned
devices. Delegate one device to each agent and run one explicitly targeted
Maestro process per agent.

For Maestro MCP, call `list_devices` first and verify the recorded identifier.
Pass that identifier as `device_id` to every device tool call.

To include the MCP binding in read-only preflight, call `list_devices({})` in
the current agent session and preserve the tool invocation and result with the
verification evidence. Pass the complete returned JSON tool result to the
readiness checker:

```bash
scripts/check-readiness.sh <selection-arguments> --maestro-probe <result-file>
```

Use `--maestro-probe -` to read the result from standard input. The checker
decodes Maestro's `CallToolResult` with its single JSON `TextContent` payload.
It also accepts an already decoded device array or a `devices` array in the
root or `structuredContent`. The checker requires one entry for the exact
target with the expected platform, a virtual device type, and
`connected: true`. An explicit tool error, unavailable, empty, malformed,
disconnected, or wrong-target result fails. A JSON file alone does not prove
that the current session called the MCP tool, so retain the session-local tool
receipt. If the workflow omits `--maestro-probe`, the result reports the MCP
check as `not-required` instead of claiming that automation passed.

Use `--require-maestro-process` when this session started the MCP host process.
Use `--require-viewer-process` when this session started the Viewer process.
These flags require the fixed fingerprints in the device lease. A process
fingerprint does not replace the `list_devices({})` binding result.

When the session starts Maestro MCP directly, bind file operations to the
workspace:

```bash
maestro mcp --working-dir "$(git rev-parse --show-toplevel)"
```

The installed launcher replaces itself with the Java runtime. Record that
runtime process and its working directory in the session record. Its full
command contains `maestro.cli.AppKt mcp` after the Java and classpath arguments.

## Keep the Viewer connected

Let Maestro select a free Viewer port by omitting `--viewer-port`. Capture the
Viewer URL from MCP startup and surface it to the operator before the run. Open
the URL when the host supports an embedded browser.

Use the recorded `device_id` in the first device tool call. Confirm that the
Viewer connects to that device before starting the flow. Keep the same Viewer
and device connection throughout the run.

This step is complete when the operator has the Viewer URL and the Viewer shows
the recorded device before the run. When the host has an embedded browser, that
browser must also show the Viewer before the run.

## Cleanup

Stop only the Maestro MCP and Viewer processes that this session started. Use
their recorded PIDs. Maestro cleanup is complete when those owned processes
exit and unrelated Maestro sessions remain available.

The command forms follow Maestro's device selection reference:

- [Specify and start devices](https://docs.maestro.dev/cli/start-device)
