# Write the acceptance tests

Foreman supplies the target checkout at the pinned base, the slice with its acceptance criteria and
their proofs, the plan's assumptions and the related repository checkouts. Nothing is built yet.
Write tests and fixtures that prove each criterion the plan marks as proven by test or contract,
following the repository's test conventions. Write only test and fixture files. Do not edit source,
the check command, CI workflows, the verify skill or the contract file. A contract criterion tests
against the related repository's real service, started as the contract says, or its recorded
contract at the pinned commit, never a mock of that boundary. Each test must fail at the base for
the reason its criterion names. Report each test's path and the criterion it proves. Foreman runs
the check, proves that the base passes without your tests, that the check collects them and that
they fail, and keeps them unchanged for the build.
