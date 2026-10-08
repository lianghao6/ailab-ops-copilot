# GPU device assertion investigation

A CUDA device assertion reports a failed check inside a GPU kernel. It differs
from an allocator's out-of-memory error and from a host process being killed.
The reported Python stack may point to a later operation because kernels execute
asynchronously. An indexing assertion can suggest an invalid index, but it does
not by itself establish which sample, preprocessing step, or kernel introduced it.

Collect stderr for every rank and build a timestamped sequence, accounting for
clock skew. Look for a rank with a local assertion before peers report collective
timeouts. A missing participant can leave peers waiting until their configured
timeout; the loudest watchdog report need not be the initiating event. Check that
the elapsed milliseconds and wall-clock gap agree with the configured timeout.

Compare GPU memory samples with capacity and inspect explicit allocator errors
and container termination reasons. A low sampled value alone cannot exclude a
short memory spike. Treat exit codes as symptoms: a launcher can signal peers
after another worker fails. Interface errors require their own timestamped
corroboration before assigning the problem to the network.

Preserve the original input shard, checkpoint, per-rank logs, and software
versions. Propose a bounded reproduction using the failing batch, input-range
validation, and synchronous kernel reporting in an isolated environment. Obtain
authorization before changing a live job. An incident annotation or collection
request can first be previewed in a simulator; a preview must not claim that a
production action occurred.

If early rank logs are missing, report what is observed, identify the uncertainty,
and request those logs. A peer timeout alone cannot prove a device assertion.
