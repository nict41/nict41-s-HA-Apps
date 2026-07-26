# Home Assistant App: CPU Performance Governor

![CPU Performance Governor logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/cpu_governor/logo.png)

Sets the host CPU [cpufreq scaling
governor](https://www.kernel.org/doc/html/latest/admin-guide/pm/cpufreq.html)
- `performance` by default - so CPU-bound jobs get the full clock ceiling
instead of being held down by the host's default `powersave` governor. It
applies the governor on start and **re-applies it automatically on every
host boot** (`boot: auto`, `startup: system`).

## Why

This repo's Home Assistant host is an **Intel N100** mini PC (4 physical
cores, no hyperthreading, 3.4 GHz boost). It ships with the `cpufreq`
governor set to `powersave`, which was holding cores around **2.5 GHz**
under sustained load instead of letting them reach the **3.4 GHz** ceiling.
The workloads that suffer most are CPU-bound backlog jobs - most notably
this repo's [`immich_ml`](../immich_ml) sidecar and the Immich server it
serves, which peg the CPU while working through a face-detection / smart
-search backlog. Switching to the `performance` governor gets that clock
headroom back, and finishes those jobs measurably faster.

## The privilege caveat (read this before "improving" the design)

The obvious design - a normal add-on that just writes
`/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor` itself - **does not
work on this HA install**, and it's worth writing down why so it doesn't get
rediscovered the hard way:

- A regular Supervisor add-on, **even with `full_access: true` and
  `privileged: [SYS_ADMIN, SYS_RAWIO, SYS_RESOURCE, SYS_MODULE]` declared**
  (this repo's Samba NAS add-on's actual config), still gets
  `/sys/devices/system/cpu/...` mounted **read-only**. Writing
  `scaling_governor` from inside it fails with `Read-only file system`.
- Supervisor's add-on privilege model is **not** a 1:1 mapping to Docker's
  real `--privileged` flag. Real `--privileged` also remounts `/sys`
  read-write and drops the capability/AppArmor restrictions; declaring
  `privileged:` capabilities and `full_access: true` in `config.yaml` is
  **not** sufficient to get that.

The hand-proven fix that *does* work is a container run with Docker's real
flags directly on the host's Docker socket:

```sh
docker run -d \
  --name cpu-governor-performance \
  --restart unless-stopped \
  --privileged --pid=host \
  alpine sh -c '
    for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
      echo performance > "$f"
    done
    exec sleep infinity'
```

`--privileged --pid=host` gives a genuine read-write view of the host's
`scaling_governor`, and `--restart unless-stopped` re-applies it on every
Docker daemon start (i.e. every host boot).

## How this add-on works

Rather than assume, the add-on **tries both at runtime on the actual host**
and reports which path it took in its log:

1. **Strategy A - direct write.** It declares `host_pid: true`,
   `full_access: true` and `privileged: [SYS_ADMIN, SYS_RAWIO, SYS_RESOURCE,
   SYS_MODULE]`, then tries to write `scaling_governor` for every core and
   read it back. If a (future) Supervisor ever grants a writable `/sys`,
   this succeeds and **no sidecar is used** - the add-on just sets the
   value and idles. On this install it fails (read-only `/sys`, as above)
   and it moves on.
2. **Strategy B - privileged sidecar (the path that actually runs here).**
   With `docker_api: true` for host Docker API access, the add-on becomes
   the versioned, documented **owner** of exactly the sidecar container
   shown above. On every start it force-removes any existing
   `cpu-governor-performance` container and recreates it, baking in the
   configured `governor`. The actual privilege escalation still happens one
   level down, in a container the add-on spawns - which is the only place
   Docker's real `--privileged`/`--pid=host` semantics are available.

Either way the result is the same: all cores get the requested governor,
and it survives reboots.

### Idempotency

The sidecar is always `docker rm -f`'d and recreated on start, so
restarting or reinstalling the add-on never leaves duplicate containers
behind. The sidecar reuses the name `cpu-governor-performance`, so
installing this add-on **cleanly adopts/replaces** any hand-created
container of that name - there's never a moment with two of them fighting
over the governor.

## Configuration

| Option | Description |
|---|---|
| `governor` | The cpufreq scaling governor to apply to every core. Default `performance`. Set to `powersave` to revert, or try `ondemand`/`conservative` if the host kernel's cpufreq driver supports them - no code edit needed. |

## Verifying it worked

Watch the add-on's log on start. On this host you'll see the fallback path:

```
Requested CPU scaling governor: performance
Direct write to scaling_governor unavailable (read-only /sys, as expected on this install) - using a privileged Docker sidecar instead.
Launching privileged sidecar 'cpu-governor-performance' to set governor=performance...
Sidecar 'cpu-governor-performance' started.
cpu0 scaling_governor now reads: performance (target: performance)
```

`/sys` is readable (just not writable) from the add-on, so the
`cpu0 scaling_governor now reads: performance` line is a real confirmation
the governor actually changed on the host.

To confirm from the host directly (SSH / Terminal add-on):

```sh
cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor   # every line: performance
# under load, clocks should now reach the boost ceiling:
grep MHz /proc/cpuinfo
```

### The real test: a full host reboot

The point of this add-on is **persistence across a host reboot**, not just
an add-on restart. After it's installed and running, actually reboot the HA
host (**Settings → System → Hardware → power menu → Reboot Host**, or
`ha host reboot`), wait for it to come back, and re-check
`scaling_governor`. It should read `performance` again with no manual
action - restored by this add-on's `boot: auto` (and, as a backup, the
sidecar's own `--restart unless-stopped`).

## Retiring the original hand-created container

If you previously created the `cpu-governor-performance` container by hand
(outside Supervisor), you don't need to remove it manually first - this
add-on force-removes and recreates a container of that same name on its
first start, so it takes ownership automatically. Once you've confirmed the
governor is still `performance` **after a full host reboot** with only this
add-on managing it, there's nothing left to clean up. (If you'd made a
*differently* named hand-run container, remove that one once you're
satisfied, to avoid two things doing the same job.)

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps) to
add this repository to Home Assistant, then install **CPU Performance
Governor** from the app store. Leave `boot: auto` on so it self-starts on
every host boot.

## Notes & caveats

- **This add-on is intentionally highly privileged** (`full_access`,
  `docker_api`, host PID). That's inherent to changing a host-level kernel
  setting from within a container; it's why it lives in a personal repo and
  not a public one.
- `docker_api: true` gives the add-on access to the **host Docker socket**.
  The add-on only uses it to manage the single `cpu-governor-performance`
  sidecar, but be aware that access is broad by nature.
- The available governors depend on the host's cpufreq **driver**
  (`intel_pstate` on the N100 exposes `performance` and `powersave`; the
  generic `acpi-cpufreq`/`cpufreq_*` drivers also expose `ondemand` and
  `conservative`). Setting an unsupported value will be rejected by the
  kernel - check the log's `cpu0 scaling_governor now reads:` line to
  confirm what actually took.
