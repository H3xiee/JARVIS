"""Experience-to-lesson conversion: look back over episodes and procedures and write down what was learned.

  * a failure that was later fixed  -> lesson "When <problem>: <fix>"
  * the same kind of failure, again -> insight "<agent> keeps failing at <goal> (N times)"
  * a procedure that fails more than it works -> lesson "Don't rely on <procedure>"
Each lesson cites the episodes it came from, and re-running the reflector only
reinforces existing lessons (it never piles up duplicates).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

REPEAT_THRESHOLD = 2


def _problem(ep) -> str:
    """'<goal> failed (<error>)' when both are known, so the lesson is found by what was being done."""
    goal, error = (ep.data.get("goal") or "").strip().rstrip("."), (ep.data.get("error") or "").strip().rstrip(".")
    if goal and error and error.lower() not in goal.lower():
        return f"'{goal}' failed ({error})"
    return (error or goal or ep.content).strip().rstrip(".")


def reflect(mm, limit: int = 200) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {"lessons": [], "insights": []}
    failures = mm.episodic.failures(limit=limit)

    # fixed failures become lessons
    for ep in failures:
        fix = ep.data.get("resolution")
        if not fix:
            continue
        agent = ep.data.get("agent")
        lesson = mm.reflective.learn(f"When {_problem(ep)}: {fix.rstrip('.')}.", context=agent or "general",
                                     agent=agent, evidence=[ep.memory_id], source="agent",
                                     reference="memory_reflector")
        out["lessons"].append(lesson.memory_id)

    # the same failure, more than once, becomes an insight
    groups = defaultdict(list)
    for ep in failures:
        key = (ep.data.get("agent") or "jarvis", (ep.data.get("goal") or ep.data.get("error") or "").lower().strip())
        if key[1]:
            groups[key].append(ep)
    for (agent, what), eps in groups.items():
        if len(eps) >= REPEAT_THRESHOLD:
            fixed = [e for e in eps if e.data.get("resolution")]
            text = f"{agent} has failed at '{what}' {len(eps)} times" + (
                f"; what fixed it before: {fixed[0].data['resolution'].rstrip('.')}" if fixed else "") + "."
            ins = mm.reflective.learn(text, context=agent, agent=agent, kind="insight",
                                      evidence=[e.memory_id for e in eps], source="agent",
                                      reference="memory_reflector")
            out["insights"].append(ins.memory_id)

    # unreliable procedures
    for proc in mm.procedural.all(limit=1000):
        s, f = int(proc.data.get("success_count") or 0), int(proc.data.get("failure_count") or 0)
        if f >= REPEAT_THRESHOLD and f > s:
            name = proc.data.get("name") or proc.content[:40]
            lesson = mm.reflective.learn(f"Don't rely on the '{name}' workflow: it failed {f} of {s + f} times.",
                                         context="procedures", evidence=[proc.memory_id], source="agent",
                                         reference="memory_reflector")
            out["lessons"].append(lesson.memory_id)
    return out
