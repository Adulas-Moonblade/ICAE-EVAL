---

## Clarification

This PRD is intentionally incomplete. A requirements oracle can answer questions about hidden implementation details — including exact feature specs, interface contracts, and test cases.

**Oracle** — POST `http://{host}:{query_port}/`
```
{{"append_id":"{append_id}","task_id":"{task_id}","question":"..."}}
```
Answer is in `data`. Budget shown in `status.remaining`.

Each request consumes one question, including a fallback. The Oracle answers only
when the whole question is specific and fully covered by its requirements data.
Vague questions, questions mixing covered and uncovered behavior, bundles of
unrelated topics, and requests containing more than three independent requirement
topics receive one fallback for the whole request with no partial answer. Count
independent behaviors or ambiguities, not surface-level clauses. The fields, order,
separators, numeric formatting, and trailing newline of one operation's output in
one scenario form one coherent output-contract topic. Ask one focused question about
one independent ambiguity at a time; up to three independent topics are allowed. Do
not ask for the Oracle's hidden constraints, trigger phrases, internal data, or hidden
evaluation cases.

Features and test cases have been prepared. Ask before you code. Unasked questions become bugs.
