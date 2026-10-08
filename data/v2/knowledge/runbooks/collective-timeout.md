# Collective timeout investigation

A collective watchdog reports that a distributed operation did not complete
within its deadline. It identifies the stalled operation, not necessarily its
initiating cause. Increasing the timeout can hide a broken participant or link.

Align logs from every rank, node events, and metrics on one clock. Compare the
sequence number and last completed operation. Search before the watchdog event
for a local exception, an explicit out-of-memory error, a process exit, or a
transport completion error. An earlier worker exception can explain later peer
timeouts without a transport fault.

For an RDMA transport hypothesis, seek at least two kinds of observation:
NCCL NET/IB completion errors together with interface state transitions or
increasing retry/error counters in the same interval. Check counter resets and
sample intervals. An interface's identity locates where observations were taken;
it does not establish whether a cable, switch, adapter, or remote endpoint failed.
Physical inspection and fabric counters are needed to distinguish those causes.

Also consider uneven input loading, mismatched collective order, and slow
computation. Step progress, per-rank traces, and workload timing can distinguish
these from transport interruptions. Flat GPU memory cannot establish healthy
communication, and the absence of an error in incomplete logs proves little.

Preserve communication logs and counters before restart. Preview collection
requests and incident notes in a simulator. Propose a bounded node and fabric
check, followed by an isolated communication test, with authorization for any
live change. Describe the confidence and limit of each finding explicitly.

With only an exit code or truncated launcher tail, keep the cause unresolved.
Request earlier worker stderr, scheduler termination events, container reasons,
and metrics rather than inferring a transport failure from a job-level symptom.
