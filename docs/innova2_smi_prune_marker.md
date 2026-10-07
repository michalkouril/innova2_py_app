<!-- SPDX-FileCopyrightText: 2026 the innova2 contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# A prune marker in the xclbin (proposal, not implemented)

`innova2-smi memory` reports, for every memory bank of the loaded xclbin, whether the link's prune hook replaced it
with a zero slave. It says **PRUNED** only where that can be checked. This note records what can be checked today
and proposes a small marker the hooks could add so that the remaining banks can be checked too. The hooks are not
changed by this release.

## What innova2-smi can check today

| bank | source | what `memory` shows when the xclbin does not use the bank |
|---|---|---|
| bank0 (DDR), route V | `RV_INFO[0]` (ddr_health 0x1C0), driven by the RM itself: the MIG reports `ddr_present=1`, and `prune_ddr.tcl`'s filler reports 0 | `PRUNED`, verified |
| bank0 (DDR), fp8c | the MIG is in the static region; `prune_unused.tcl` never removes bank0 | `unused (static DDR: never pruned)` |
| PLRAM0/1/2, HOST, PEER | only `used=0` in the xclbin's MEM_TOPOLOGY | `unused (may be pruned)` |

The per-bank data comes from xocl's sysfs copies of the xclbin sections, under
`/sys/bus/pci/devices/<user PF>/icap.u.*/` (`mem_topology`, `ip_layout`, `connectivity`; XRT 2.19). These are read
passively, with no XRT context and no `xrt-smi`. `used=0` shows that no kernel argument is connected to the bank,
not that the hook deleted its path. A link without the hook, or a hook that failed partway, also leaves `used=0`.

What the hooks record now:

* `plram_hook/prune_unused.tcl` prints `PRUNE-HOOK:` lines in the v++ log and leaves the cells it creates in the
  dynamic-region netlist (`prune_zero_<bank>`, `prune_plram_split`). It writes nothing that reaches the xclbin.
* `prune_ddr_spike/scripts/prune_ddr.tcl` writes `prune_ddr_marker.json` next to `$dr_bd_tcl` and in
  `$vpl_output_dir` (in the build tree only), and adds a BD marker cell `prune_ddr_state_{KEPT,PRUNED}` to the
  netlist. Neither reaches the xclbin either. The DDR decision is still visible on the card through `RV_INFO`.

## Proposal: a KEYVALUE_METADATA entry

After the link, add one user key-value pair to the xclbin with xclbinutil, for example:

```
xclbinutil --input k.xclbin --output k.marked.xclbin \
  --key-value 'USER:innova2_prune:{"v":1,"hook":"prune_unused.tcl","pruned":["PLRAM2","HOST","PEER"],"kept":["bank0","PLRAM0","PLRAM1"],"ddr":"n/a"}'
```

The value would be compact JSON with these fields:

* `v` — the format version.
* `hook` — the hook that ran (`prune_unused.tcl` or `prune_ddr.tcl`).
* `pruned` — the bank tags whose paths the hook replaced with zero slaves (or, for HOST/PEER, deleted behind the
  idle stub). Only banks the hook actually changed go here.
* `kept` — the bank tags it left alone.
* `ddr` — `KEPT`, `PRUNED` or `n/a`.

The hooks already know all of this when they finish: `::pr_used` / `$deleted` in `prune_unused.tcl`, and `$dec` /
`$deleted` in `prune_ddr.tcl`. They cannot write it into the xclbin themselves, because the xclbin does not exist yet
when they run. Two ways to get it there:

1. The hook writes the value to a file, for example `prune_marker.kv` next to `prune_ddr_marker.json`, and the build
   script runs the `xclbinutil --key-value` step above after `v++ -l`. This is the simplest option and needs no v++
   internals.
2. The same step goes into the existing post-link patch step (`patch_xclbin`), which already rewrites the xclbin.

Checked with XRT 2.19's xclbinutil on an E7a3 xclbin (2026-10-06):

* `--key-value USER:...` adds a KEYVALUE_METADATA section;
* `--info` lists it under "User Added Key Value Pairs";
* the xclbin **UUID changes** (`c676b251...` to `56b9222a...`).

Because the UUID changes, the marker must be added before anything records the UUID, such as kernel-side
`xrt::uuid` constants or a validate directory. The file that is loaded must be the marked file.

## How innova2-smi would read it (without loading anything)

xocl does not keep KEYVALUE_METADATA, so the marker is not in sysfs. To keep the read passive, innova2-smi would read
the marker from the xclbin **file**:

1. `innova2-smi memory --xclbin FILE` reads the axlf header and section table from the file only.
2. It checks that the file's UUID (header offset 416) equals the user PF's sysfs `xclbinuuid`. If they differ, it
   ignores the marker and says so.
3. It shows `PRUNED (marker)` for the banks in `pruned` that also have `used=0` in mem_topology. A bank in `pruned`
   with `used=1` is a contradiction and gets a FAIL.

A route-V DDR marker would be cross-checked against `RV_INFO`, the same way `prune_ddr_gate.py` cross-checks
`prune_ddr_marker.json` against the link's MEM_TOPOLOGY.

## A fully passive alternative (hardware)

Route V already shows how to do this: the RM drives a level that the static region latches in a register
(`ddr_present`, which becomes `RV_INFO[0]`). Spare bits could carry a prune mask for PLRAM0/1/2, HOST and PEER, with
the constants driven by the hook's `prune_zero_*` / stub cells. Then the card itself reports what was pruned, with no
file needed. This needs a static-region change (new boundary port bits and a ddr_health register field). It belongs
in the next shell revision, not in innova2-smi.
