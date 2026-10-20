import json
from hashlib import sha256
from typing import Literal, Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.evidence.evidence_fragment_reference import EvidenceFragmentReference
from app.domain.evidence.numeric_plan_task import NumericPlanTask


class NumericDependencyPlan(DomainModel):
    plan_version: Literal[1, 2] = 2
    run_id: NonEmptyString
    session_id: NonEmptyString
    target_comid: NonEmptyString
    method_registry_digest: NonEmptyString
    source_fragment_refs: tuple[EvidenceFragmentReference, ...] = ()
    tasks: tuple[NumericPlanTask, ...]
    plan_digest: NonEmptyString

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        task_ids = tuple(task.task_id for task in self.tasks)
        ensure_unique_tuple(task_ids, field_name="numeric plan tasks")
        ensure_unique_tuple(
            self.source_fragment_refs, field_name="numeric plan source evidence"
        )
        by_id = {task.task_id: task for task in self.tasks}
        if any(task.target_comid != self.target_comid for task in self.tasks):
            raise ValueError("numeric plan task left its waterbody")
        for task in self.tasks:
            for input_ in task.inputs:
                if input_.dependency_task_id is None:
                    continue
                predecessor = by_id.get(input_.dependency_task_id)
                if (
                    predecessor is None
                    or predecessor.output_parameter != input_.parameter
                    or predecessor.output_unit != input_.unit
                    or predecessor.window_id != input_.window_id
                ):
                    raise ValueError("numeric plan dependency is missing or mismatched")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("numeric plan contains a dependency cycle")
            if task_id in visited:
                return
            visiting.add(task_id)
            for input_ in by_id[task_id].inputs:
                if input_.dependency_task_id is not None:
                    visit(input_.dependency_task_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        if self.plan_digest != self.compute_digest():
            raise ValueError("numeric plan digest does not match its tasks")
        return self

    def compute_digest(self) -> str:
        value = self.model_dump(mode="json", exclude={"plan_digest"})
        if self.plan_version == 1:
            for task in value["tasks"]:
                task.pop("method_kind", None)
                for binding in task["inputs"]:
                    binding.pop("required", None)
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def referenced_item_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *(reference.item_id for reference in self.source_fragment_refs),
                    *(
                        input_.evidence_ref.item_id
                        for task in self.tasks
                        for input_ in task.inputs
                        if input_.evidence_ref is not None
                    ),
                )
            )
        )

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        session_id: str,
        target_comid: str,
        method_registry_digest: str,
        tasks: tuple[NumericPlanTask, ...],
        source_fragment_refs: tuple[EvidenceFragmentReference, ...] = (),
    ) -> "NumericDependencyPlan":
        values = {
            "run_id": run_id,
            "session_id": session_id,
            "target_comid": target_comid,
            "method_registry_digest": method_registry_digest,
            "source_fragment_refs": source_fragment_refs,
            "tasks": tasks,
        }
        encoded = json.dumps(
            {
                "plan_version": 2,
                **{
                    key: (
                        [item.model_dump(mode="json") for item in value]
                        if key in {"tasks", "source_fragment_refs"}
                        else value
                    )
                    for key, value in values.items()
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return cls(**values, plan_digest=sha256(encoded).hexdigest())
