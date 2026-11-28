------------------------- MODULE PublishReconciliation -------------------------
(***************************************************************************)
(* The publish step of src/foreman/steps/implement.py after commit has     *)
(* checkpointed the checked head. GitHub and other people act between any  *)
(* two of Foreman's steps. A read can fail, a push or create can fail      *)
(* before or after it lands, a new PR can be invisible to reads for a      *)
(* while, and Foreman can crash. A crash or a retry halt reruns publish    *)
(* from its first read, as the runner does.                                *)
(*                                                                         *)
(* Promise: Foreman reports a published PR only for its own PR at the      *)
(* checked head, never repeats a push that landed, never overwrites work   *)
(* it does not own, and publishes once nothing else interferes.            *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS MaxFaults, MaxCrashes

Retry == {"publish_failed", "publication_unreconciled", "git_failed"}
Terminal == {"published", "owned_head_moved"}

VARIABLES
    remote,     \* origin's delivery branch: "absent", "parent" (attempt head), "head" (checked), "foreign"
    pr,         \* GitHub's PR for the branch: "none", "owned" (this run's marker and base), "foreign"
    visible,    \* gh pr view already shows the PR
    seen,       \* the PR head Foreman's last gh pr view returned (headRefOid)
    interfered, \* someone else pushed the branch
    pc,         \* Foreman's next step in publish()
    error,      \* the last write raised
    outcome,    \* "running", a retry halt, or a terminal outcome
    redundant,  \* pushes issued when origin already held the checked head
    faults,
    crashes

vars == <<remote, pr, visible, seen, interfered, pc, error, outcome, redundant, faults, crashes>>

Init ==
    \* A PR needs its branch. An owned PR exists on a repair round.
    /\ \/ remote = "absent" /\ pr = "none"
       \/ remote = "parent" /\ pr \in {"none", "owned", "foreign"}
    /\ visible = (pr # "none")
    /\ seen = "none"
    /\ interfered = FALSE
    /\ pc = "read_remote"
    /\ error = FALSE
    /\ outcome = "running"
    /\ redundant = 0
    /\ faults = 0
    /\ crashes = 0

Halt(reason) ==
    /\ outcome' = reason
    /\ pc' = "stopped"

Fault == faults < MaxFaults /\ faults' = faults + 1

(* workspace.remote_head(): ls-remote. An unreadable remote may hide a landed push. *)
ReadRemote ==
    /\ pc = "read_remote"
    /\ \/ /\ Fault
          /\ Halt("publication_unreconciled")
       \/ /\ remote = "foreign"
          /\ pc' = "read_moved_pr"
          /\ UNCHANGED <<faults, outcome>>
       \/ /\ remote = "head"
          /\ pc' = "read_pr"
          /\ UNCHANGED <<faults, outcome>>
       \/ /\ remote \in {"absent", "parent"}
          /\ pc' = "push"
          /\ UNCHANGED <<faults, outcome>>
    /\ UNCHANGED <<seen, remote, pr, visible, interfered, error, redundant, crashes>>

(* A moved remote: only the owned PR at that head starts a new attempt, once Foreman has read the commits a person pushed
   on it (hand_fixes). That read writes nothing, and its failure stops git_failed, which a rerun retries. *)
ReadMovedPr ==
    /\ pc = "read_moved_pr"
    /\ \/ /\ Fault
          /\ Halt("publication_unreconciled")
       \/ /\ pr = "owned" /\ visible
          /\ Halt("owned_head_moved")
          /\ UNCHANGED faults
       \/ /\ pr = "owned" /\ visible
          /\ Fault
          /\ Halt("git_failed")
       \/ /\ ~(pr = "owned" /\ visible)
          /\ Halt("publish_failed")
          /\ UNCHANGED faults
    /\ UNCHANGED <<seen, remote, pr, visible, interfered, error, redundant, crashes>>

(* workspace.push(): git push without --force, so origin fast-forwards or rejects. *)
Push ==
    /\ pc = "push"
    /\ redundant' = redundant + (IF remote = "head" THEN 1 ELSE 0)
    /\ IF remote = "foreign"
       THEN /\ error' = TRUE                       \* rejected: not a fast-forward
            /\ UNCHANGED <<seen, remote, faults>>
       ELSE \/ /\ remote' = "head" /\ error' = FALSE /\ UNCHANGED faults
            \/ /\ Fault /\ remote' = "head" /\ error' = TRUE   \* landed, then the client failed
            \/ /\ Fault /\ error' = TRUE /\ UNCHANGED remote   \* failed before landing
    /\ pc' = "after_push"
    /\ UNCHANGED <<seen, pr, visible, interfered, outcome, crashes>>

(* After a push error, read origin again before deciding. *)
AfterPush ==
    /\ pc = "after_push"
    /\ IF ~error
       THEN /\ pc' = "read_pr" /\ UNCHANGED <<faults, outcome>>
       ELSE \/ /\ Fault
               /\ Halt("publication_unreconciled")
            \/ /\ remote = "head"
               /\ pc' = "read_pr"
               /\ UNCHANGED <<faults, outcome>>
            \/ /\ remote # "head"
               /\ Halt("publish_failed")
               /\ UNCHANGED faults
    /\ error' = FALSE
    /\ UNCHANGED <<seen, remote, pr, visible, interfered, redundant, crashes>>

(* forge.pull_request_for(): gh pr view. A new PR may not show yet. *)
ReadPr ==
    /\ pc = "read_pr"
    /\ \/ /\ Fault
          /\ Halt("publication_unreconciled")
          /\ UNCHANGED seen
       \/ /\ pr # "none" /\ visible
          /\ pc' = "check"
          /\ seen' = remote
          /\ UNCHANGED <<faults, outcome>>
       \/ /\ ~(pr # "none" /\ visible)
          /\ pc' = "create"
          /\ UNCHANGED <<faults, outcome, seen>>
    /\ UNCHANGED <<remote, pr, visible, interfered, error, redundant, crashes>>

(* forge.open_pull_request(): GitHub keeps one open PR per branch and rejects a second. *)
Create ==
    /\ pc = "create"
    /\ IF pr # "none"
       THEN /\ error' = TRUE /\ UNCHANGED <<seen, pr, visible, faults>>
       ELSE \/ /\ pr' = "owned" /\ error' = FALSE /\ visible' \in BOOLEAN /\ UNCHANGED faults
            \/ /\ Fault /\ pr' = "owned" /\ error' = TRUE /\ visible' \in BOOLEAN
            \/ /\ Fault /\ error' = TRUE /\ UNCHANGED <<seen, pr, visible>>
    /\ pc' = "after_create"
    /\ UNCHANGED <<seen, remote, interfered, outcome, redundant, crashes>>

(* Read the PR again. Accepted but unseen stays unresolved; a failed create with no PR is final. *)
AfterCreate ==
    /\ pc = "after_create"
    /\ \/ /\ Fault
          /\ Halt("publication_unreconciled")
          /\ UNCHANGED seen
       \/ /\ pr # "none" /\ visible
          /\ pc' = "check"
          /\ seen' = remote
          /\ UNCHANGED <<faults, outcome>>
       \/ /\ ~(pr # "none" /\ visible)
          /\ Halt(IF error THEN "publish_failed" ELSE "publication_unreconciled")
          /\ UNCHANGED <<faults, seen>>
    /\ error' = FALSE
    /\ UNCHANGED <<remote, pr, visible, interfered, redundant, crashes>>

(* owned(pr) and pr.headRefOid == head. A PR's head is its branch's head. *)
Check ==
    /\ pc = "check"
    /\ CASE pr # "owned"  -> Halt("publish_failed")
         [] seen # "head" -> Halt("publication_unreconciled")
         [] OTHER         -> Halt("published")
    /\ UNCHANGED <<remote, pr, visible, seen, interfered, error, redundant, faults, crashes>>

(* A retry halt or a crash reruns publish from its first read. *)
Rerun ==
    /\ outcome \in Retry
    /\ outcome' = "running"
    /\ pc' = "read_remote"
    /\ error' = FALSE
    /\ UNCHANGED <<seen, remote, pr, visible, interfered, redundant, faults, crashes>>

Crash ==
    /\ outcome = "running"
    /\ crashes < MaxCrashes
    /\ crashes' = crashes + 1
    /\ pc' = "read_remote"
    /\ error' = FALSE
    /\ UNCHANGED <<seen, remote, pr, visible, interfered, outcome, redundant, faults>>

(* Other actors: someone pushes the branch (and may own a PR for it), and GitHub shows a new PR. *)
ForeignPush ==
    /\ ~interfered
    /\ outcome \notin Terminal
    /\ interfered' = TRUE
    /\ remote' = "foreign"
    /\ UNCHANGED <<seen, pr, visible, pc, error, outcome, redundant, faults, crashes>>

Show ==
    /\ pr # "none" /\ ~visible
    /\ visible' = TRUE
    /\ UNCHANGED <<seen, remote, pr, interfered, pc, error, outcome, redundant, faults, crashes>>

Foreman == ReadRemote \/ ReadMovedPr \/ Push \/ AfterPush \/ ReadPr \/ Create \/ AfterCreate \/ Check \/ Rerun

Next == Foreman \/ Crash \/ ForeignPush \/ Show
    \/ (outcome \in Terminal /\ UNCHANGED vars)

Spec == Init /\ [][Next]_vars /\ WF_vars(Foreman) /\ WF_vars(Show)

-----------------------------------------------------------------------------
(* Properties *)

(* Published means GitHub showed this run's PR at the checked head. *)
PublishedIsChecked == outcome = "published" => seen = "head" /\ pr = "owned"

(* A push that already landed is never repeated, including after a crash or rerun. *)
NoRepeatedPush == redundant = 0

(* Foreman never writes over a branch someone else pushed. *)
ForeignPreserved == interfered => remote = "foreign"

(* With no one else pushing, publication finishes once faults and crashes run out. *)
Publishes == (pr # "foreign" /\ [] ~interfered) => <> (outcome = "published")
=============================================================================
