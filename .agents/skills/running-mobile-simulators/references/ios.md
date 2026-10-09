# iOS Simulator

## Start the selected simulator

List available simulated devices and select one exact UDID:

```bash
xcrun simctl list devices available -j
xcrun simctl bootstatus <udid> -b
```

`bootstatus -b` starts the selected simulator when needed and waits until it is
ready. An iOS simulator uses its UDID instead of an emulator console port.
Record the UDID and launcher command before boot. If the launcher is not
persistent, record that fact instead of a PID.

Startup is complete when the session record contains the selected UDID and
launcher details.

## Prove identity and readiness

Verify the selected UDID:

```bash
xcrun simctl list devices booted -j
xcrun simctl bootstatus <udid> -b
```

iOS is ready when the recorded UDID is booted and `bootstatus` exits
successfully for that UDID.

For read-only preflight, run `scripts/check-readiness.sh` from the skill
directory. The checker reads `xcrun simctl list devices -j`, requires the exact
UDID to be available and `Booted`, and then runs this command:

```bash
xcrun simctl bootstatus <udid>
```

The read-only form omits `-b`, so it cannot boot a stopped simulator. The
checker reads the device list again after `bootstatus` and fails if the selected
device changed. Each command uses the configured observation budget. On
timeout, the checker stops and reaps only the command process that it started.

## Bind operations and cleanup

Pass the recorded UDID to every targeted `xcrun simctl` command. Never use the
`booted` alias. When several simulators are active, `simctl` can choose any one
of them for that alias. A different booted simulator cannot satisfy readiness
or receive a mutation.

For an owned simulator, shut it down through its exact UDID:

```bash
xcrun simctl shutdown <udid>
```

Leave attached and unrelated simulators running. iOS cleanup is complete when
the recorded simulator is shut down and every owned launcher has exited.

Apple documents `simctl` in the Xcode command-line tools reference. Run
`xcrun simctl help` and `xcrun simctl help <subcommand>` for the command set
installed with the active Xcode:

- [Xcode command-line tool reference](https://developer.apple.com/documentation/xcode/xcode-command-line-tool-reference)
