# Let a new playbook delegate inspect its real candidate files

## Evidence

In dojoP's 17 September crash-barrier trial, the background builder created and ran a v3 candidate, then called `file_read` on the saved batch output. Its toolset had been frozen before the playbook existed and contained only authoring/status tools plus tools referenced by any pre-existing target. The core rejected the valid `file_read` call until pydantic-ai exhausted two tool retries; the delegation ended with a generic failure before the seven-output kill barrier. The server warning names `file_read` and the max-retry error; the delegation event feed shows the successful candidate run immediately before the unanswered read.

## Change

Always include read-only `file_list` and `file_read` in a playbook author's bounded delegation toolset. A new builder can then independently inspect actual candidate output as requested by the 0.57.6 output-semantics guidance. Do not add Files mutations, chat sends, or broad dynamic plugin access. The owner approval and Files access controls still apply at invocation.

## Verification

The new unit test is red on the old toolset and green with these two reads. Run the full Playbooks suite and the relevant Luna integration suite, then use a fresh isolated crash mission on this version. The prior inconclusive live trial remains unchanged; the new run must actually record a SIGKILL and restart before claiming recovery coverage.
