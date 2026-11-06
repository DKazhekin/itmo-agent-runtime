# Recorded evaluation

The results in this directory describe the experiments recorded on 2026-11-06. They accompany the [checkpoint requirements and demonstration](../docs/checkpoint-1.md); they are not rerun automatically when documentation or source files change.

## Real-model attempts

All four recorded B1–B5 attempts are included. Each directory contains its original task results, event streams, collected synthetic artifacts, independent checker output and source hashes.

| Attempt | Result | Observation |
| --- | --- | --- |
| [20261106T124427Z-beceee4d](baf/real/20261106T124427Z-beceee4d/summary.json) | 0/1 | The provider request failed before task completion. |
| [20261106T124843Z-82091413](baf/real/20261106T124843Z-82091413/summary.json) | 1/1 | The single-task connection check completed. |
| [20261106T124926Z-68548bad](baf/real/20261106T124926Z-68548bad/summary.json) | 12/15 | File checks passed; the three B5 tasks lacked verifiable direct command-execution evidence. |
| [20261106T125440Z-e6a11cdd](baf/real/20261106T125440Z-e6a11cdd/summary.json) | 15/15 | File checks and B5 execution checks passed, with no omitted tasks. |

The complete runs used DeepSeek V4.1 Flash 1017 with `high` reasoning through DeepInfra FP8. The model, route and index references are recorded in each manifest. The recorded total provider cost across the experiment was $0.018821183. See [model configuration and evaluation conditions](../docs/reference/model.md).

The [recorded event audit](verification/real-run-audit.json) validated 322 events for the successful run. The only runtime change between the first and second full attempts was general tool guidance for direct program execution with separate stdout, stderr and exit-code fields. Task fixtures and checkers were unchanged.

## Infrastructure and mounting

- [Mac/Docker verification](verification/checkpoint-final-mac.txt) and [Linux verification](verification/checkpoint-final-linux.txt): 244 tests and 48 task-package cases in each environment, with no failures or skipped tests. Two warnings concern the deprecated FastAPI startup-event interface.
- [Cleanup](verification/checkpoint-final-cleanup.json): no remaining containers, sandboxes, networks or volumes in the isolated test project.
- [Restart](verification/restart.json): the session container was removed and repeated provisioning remained released.
- [Budget storage](verification/budget-storage.json): non-root persistence across restart and refusal after journal deletion, without model requests.
- [Mounting manifest](mounting/manifest.json): the two failed attempts and the successful [17-check attempt](mounting/attempt-3/result.json), including bidirectional file access, permissions and disconnect behavior.

The Linux checks used a separate source tree and empty project volumes in an existing prepared virtual machine. They do not represent a newly installed operating system. Mounting remains a separate prototype. See [environment prerequisites](../docs/reference/environment.md), [mounting comparison](../docs/reference/mounting.md), [system comparison](../docs/reference/systems.md), and [pinned course requirements](../docs/reference/course.md).

## Scope and integrity

The per-run `source_sha256` values identify the source snapshot evaluated at that time. They are retained unchanged and must not be interpreted as hashes of every later checkout. The mounting manifest likewise records the tested prototype revision. Use the current verification commands to assess current source files.

Task result files, event streams and collected synthetic artifacts are byte-identical to the recorded model attempts. Operator approval text, credential references and budget-account identifiers are omitted from public manifests. These manifests are experiment records, not ready-to-use authorization files for another paid run. Mounting records replace the operator's home directory with `<HOME>` and the machine's public SSH host key with `<HOST_KEY>`; check outcomes, timings and error observations are preserved. Account snapshots and budget databases are not part of the public evidence.

A machine-readable overview is available in [verification/summary.json](verification/summary.json).

## Clean source verification

A separate copy containing only public repository files was built and checked without local settings or generated output. The [source verification record](verification/public-source-check.json) identifies the checked files and commands; the [test log](verification/public-source-check.txt) records 244 passing tests and 48 task-package cases. No model requests were made, and all resources in the isolated test project were removed. The three host verification helpers also create their output directories in an empty checkout.
