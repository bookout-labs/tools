---------------------------- MODULE DispatchRecovery ----------------------------
(***************************************************************************)
(* One stage call through src/foreman/dispatch.py, with the attempt        *)
(* journal of src/foreman/worker.py (AttemptJournal). Foreman may crash    *)
(* between any two steps. A crash loses the process and keeps two stores:  *)
(* the LangGraph checkpoint (committed once per node, durability "sync")   *)
(* and the journal (committed by each journal call).                       *)
(*                                                                         *)
(* Promise: a paid worker call is launched at most once per reservation,   *)
(* and every launched call is charged exactly once in the checkpoint. An   *)
(* invalid answer gets one repair turn, itself a reserved and charged call. *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS
    Models,     \* Length of the dispatch plan's model list (provider fallback).
    MaxCrashes  \* Bound on process crashes.

\* Reservation identities. reserve() draws a fresh uuid4 on every run, and it runs
\* at most once per model, once for the repair turn and once per crash.
Ids == 1..(Models + 1 + MaxCrashes)

NoId == 0

VARIABLES
    journal,    \* worker_attempt row per id: "none", "reserved" (NULL), "sent" (X''), "finished" (result)
    result,     \* the WorkerResult kind stored by finish(): "none", "ran", "invalid", "quota"
    launched,   \* worker calls made for each id (the paid effect)
    node,       \* checkpoint: next dispatch node, "reserve", "reconcile" or "done"
    reservation,\* checkpoint: state["reservation"] id, or NoId
    index,      \* checkpoint: state["index"], the model being tried
    charges,    \* checkpoint: attempt records per id in state["attempts"]
    repaired,   \* checkpoint: "repair" in state, the one repair turn granted
    pc,         \* process: the step about to run inside the current node
    local,      \* process: id drawn by this reserve run, or the outcome read by reconcile
    crashes

vars == <<journal, result, launched, node, reservation, index, charges, repaired, pc, local, crashes>>

Init ==
    /\ journal = [i \in Ids |-> "none"]
    /\ result = [i \in Ids |-> "none"]
    /\ launched = [i \in Ids |-> 0]
    /\ node = "reserve"
    /\ reservation = NoId
    /\ index = 0
    /\ charges = [i \in Ids |-> 0]
    /\ repaired = FALSE
    /\ pc = "reserve.insert"
    /\ local = NoId
    /\ crashes = 0

(* reserve(): journal.reserve(identity) runs before the node returns. *)
ReserveInsert ==
    /\ pc = "reserve.insert"
    /\ \E i \in Ids :
        /\ journal[i] = "none"
        /\ journal' = [journal EXCEPT ![i] = "reserved"]
        /\ local' = i
    /\ pc' = "reserve.commit"
    /\ UNCHANGED <<result, launched, node, reservation, index, charges, repaired, crashes>>

(* The reserve node's update is checkpointed with the reservation. *)
ReserveCommit ==
    /\ pc = "reserve.commit"
    /\ node' = "reconcile"
    /\ reservation' = local
    /\ pc' = "reconcile.read"
    /\ local' = NoId
    /\ UNCHANGED <<journal, result, launched, index, charges, repaired, crashes>>

(* reconcile(): journal.read(). NULL means reserved and never sent, so launch.     *)
(* X'' means sent with no result: charge it as interrupted, never relaunch.        *)
(* A stored result is charged from the evidence, never relaunched.                 *)
ReconcileRead ==
    /\ pc = "reconcile.read"
    /\ LET i == reservation IN
        \/ /\ journal[i] = "reserved"
           /\ pc' = "launch.check"
           /\ local' = NoId
        \/ /\ journal[i] = "sent"
           /\ pc' = "reconcile.charge"
           /\ local' = "interrupted"
        \/ /\ journal[i] = "finished"
           /\ pc' = "reconcile.charge"
           /\ local' = result[i]
    /\ UNCHANGED <<journal, result, launched, node, reservation, index, charges, repaired, crashes>>

(* launch(): a moved plan or a changed worktree halts before send, uncharged. *)
LaunchCheck ==
    /\ pc = "launch.check"
    /\ \/ /\ pc' = "launch.send"
          /\ UNCHANGED <<node, reservation>>
       \/ /\ pc' = "done"
          /\ node' = "done"
          /\ reservation' = NoId
    /\ UNCHANGED <<journal, result, launched, index, charges, repaired, local, crashes>>

(* journal.send(): UPDATE ... SET evidence = X'' WHERE id = ? AND evidence IS NULL. *)
(* Any other row raises, which ends the process like a crash.                       *)
LaunchSend ==
    /\ pc = "launch.send"
    /\ journal[reservation] = "reserved"
    /\ journal' = [journal EXCEPT ![reservation] = "sent"]
    /\ pc' = "launch.call"
    /\ UNCHANGED <<result, launched, node, reservation, index, charges, repaired, local, crashes>>

(* self.handler(...): the paid worker call. It returns a valid or invalid answer, or quota-stops. *)
LaunchCall ==
    /\ pc = "launch.call"
    /\ launched' = [launched EXCEPT ![reservation] = @ + 1]
    /\ \E kind \in {"ran", "invalid", "quota"} : local' = kind
    /\ pc' = "launch.finish"
    /\ UNCHANGED <<journal, result, node, reservation, index, charges, repaired, crashes>>

(* journal.finish(): the result is stored before reconcile charges it. *)
LaunchFinish ==
    /\ pc = "launch.finish"
    /\ journal' = [journal EXCEPT ![reservation] = "finished"]
    /\ result' = [result EXCEPT ![reservation] = local]
    /\ pc' = "reconcile.charge"
    /\ UNCHANGED <<launched, node, reservation, index, charges, repaired, local, crashes>>

(* reconcile() returns attempts [record] with reservation None in one checkpointed update. *)
(* A quota outcome with models left falls back to reserve with index + 1. The first      *)
(* invalid answer sets repair in that same update and returns to reserve on the same     *)
(* model for the repair turn. A second invalid answer halts with invalid_output.         *)
ReconcileCharge ==
    /\ pc = "reconcile.charge"
    /\ charges' = [charges EXCEPT ![reservation] = @ + 1]
    /\ reservation' = NoId
    /\ local' = NoId
    /\ CASE local = "quota" /\ index + 1 < Models ->
             /\ node' = "reserve"
             /\ index' = index + 1
             /\ pc' = "reserve.insert"
             /\ UNCHANGED repaired
          [] local = "invalid" /\ ~repaired ->
             /\ node' = "reserve"
             /\ repaired' = TRUE
             /\ pc' = "reserve.insert"
             /\ UNCHANGED index
          [] OTHER ->
             /\ node' = "done"
             /\ pc' = "done"
             /\ UNCHANGED <<index, repaired>>
    /\ UNCHANGED <<journal, result, launched, crashes>>

(* A crash drops the process. The runner resumes at the checkpointed node. *)
Crash ==
    /\ pc # "done"
    /\ crashes < MaxCrashes
    /\ crashes' = crashes + 1
    /\ local' = NoId
    /\ pc' = CASE node = "reserve"   -> "reserve.insert"
               [] node = "reconcile" -> "reconcile.read"
               [] OTHER              -> "done"
    /\ UNCHANGED <<journal, result, launched, node, reservation, index, charges, repaired>>

Done ==
    /\ pc = "done"
    /\ UNCHANGED vars

Next ==
    \/ ReserveInsert \/ ReserveCommit \/ ReconcileRead \/ LaunchCheck
    \/ LaunchSend \/ LaunchCall \/ LaunchFinish \/ ReconcileCharge
    \/ Crash \/ Done

Spec == Init /\ [][Next]_vars /\ WF_vars(Next /\ ~Crash)

-----------------------------------------------------------------------------
(* Properties *)

TypeOK ==
    /\ journal \in [Ids -> {"none", "reserved", "sent", "finished"}]
    /\ launched \in [Ids -> 0..2]
    /\ node \in {"reserve", "reconcile", "done"}
    /\ charges \in [Ids -> 0..2]
    /\ repaired \in BOOLEAN

(* The paid worker call runs at most once for each reservation. *)
LaunchedAtMostOnce == \A i \in Ids : launched[i] <= 1

(* Every launched call is charged when the dispatch ends, so no paid call goes unbilled. *)
LaunchedIsCharged == pc = "done" => \A i \in Ids : launched[i] = 1 => charges[i] = 1

(* No call is charged twice, so a resume never bills again. *)
ChargedAtMostOnce == \A i \in Ids : charges[i] <= 1

(* Only a launched or possibly launched call is charged: never a reservation that was not sent. *)
ChargedWasSent == \A i \in Ids : charges[i] > 0 => journal[i] \in {"sent", "finished"}

(* The dispatch always ends. *)
Ends == <> (pc = "done")
=============================================================================
