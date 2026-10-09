# Android Emulator

## Start the selected AVD

List named Android Virtual Devices before selecting one:

```bash
emulator -list-avds
```

For ordinary startup, let the Android Emulator select an available console and
ADB port pair:

```bash
emulator -avd <avd-name>
```

Add explicit port allocation only when parallel device creation requires it.
Record the launcher PID and resolve the exact serial before another device
operation.

Give each agent a different AVD. Do not start a second instance of an agent's
AVD for another agent. Android supports `-read-only` instances of one AVD, but
they still share the AVD's base images and their changes disappear on exit.
Use separate AVDs for agent-owned state.

Startup is complete when the session record contains the selected AVD name,
exact serial, launcher command, and launcher PID.

## Prove identity and readiness

Verify the selected serial and AVD:

```bash
adb devices -l
adb -s <serial> emu avd name
adb -s <serial> get-state
adb -s <serial> shell getprop sys.boot_completed
```

Android is ready when the selected serial reports the intended AVD,
`get-state` returns `device`, and `sys.boot_completed` returns `1`.

Complete this check only when every result came from the recorded serial.

For read-only preflight, run `scripts/check-readiness.sh` from the skill
directory. The checker connects to the existing loopback ADB server and sends
only fixed requests for the selected serial, `get-state`,
`sys.boot_completed`, and `ro.boot.qemu.avd_name`. It does not start an `adb`
process. A missing server or incomplete protocol response is unmet evidence.
Use `--adb-port` only when the existing server listens on a nondefault local
port.

## Bind operations and cleanup

Scope every targeted ADB command with `adb -s <serial>`. Do not use an
unqualified `adb` command, `adb -e`, `adb -d`, or `ANDROID_SERIAL` for a device
mutation. Inventory can list all devices, but a different serial cannot satisfy
readiness or receive a mutation.

For an owned emulator, shut it down through its exact serial:

```bash
adb -s <serial> emu kill
```

Leave shared ADB and every attached or unrelated device running. Android
cleanup is complete when the recorded emulator and launcher exit.

The command forms follow the Android Emulator command-line and ADB device
selection references:

- [Start the emulator from the command line](https://developer.android.com/studio/run/emulator-commandline)
- [Send commands to a specific device](https://developer.android.com/tools/adb#devicestatus)
