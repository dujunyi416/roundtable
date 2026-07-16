# Roundtable domain language

- **Participant**: one independent stateful agent conversation in a run. Two Participants may use the same provider.
- **Provider**: the CLI transport used by a Participant, currently `claude` or `codex`.
- **Roster**: the two Participant specifications assigned to leader and reviewer roles.
- **Collaboration run**: one task moving through drafting, review, revision, and finalization.
- **Human gate**: a checkpoint after an agent turn where the run waits for a person to continue or intervene.
- **Human intervention**: guidance recorded in the transcript and relayed verbatim to the next Participant.
- **Project Room**: long-lived project mission, goals, constraints, and decisions shared by future runs.
- **Workbench**: the local Web interface used to create, observe, and control runs.
- **Run artifacts**: the auditable files stored under `.roundtable/runs/<run-id>`.
