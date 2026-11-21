"""로컬 구조/오동작 방지 테스트. AWS 호출이나 리소스 변경 없음."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

from design import wrapper, template, OLD, CORE, PARENT

HERE = Path(__file__).resolve().parent
SOURCE = json.loads((HERE / "reference-parent.json").read_text(encoding="utf-8"))


def edges(node):
    result = []
    if "Next" in node:
        result.append(node["Next"])
    if "Default" in node:
        result.append(node["Default"])
    for choice in node.get("Choices", []):
        result.append(choice["Next"])
    for catch in node.get("Catch", []):
        result.append(catch["Next"])
    return result


def check_graph(machine, all_names=None):
    if all_names is None:
        all_names = set()
    states = machine["States"]
    assert not (all_names & states.keys()), "duplicate state names: " + repr(all_names & states.keys())
    all_names.update(states)
    assert machine["StartAt"] in states
    seen = set()
    pending = [machine["StartAt"]]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        assert name in states, name
        seen.add(name)
        node = states[name]
        pending.extend(edges(node))
        if node["Type"] == "Task":
            assert ("Next" in node) != (node.get("End") is True), name
        for branch in node.get("Branches", []):
            check_graph(branch, all_names)
    assert seen == set(states), "unreachable: " + repr(set(states) - seen)


class DesignTests(unittest.TestCase):
    def test_graph_and_no_self_recursion(self):
        result = wrapper(SOURCE["definition"], "sg-00000000000006a09")
        check_graph(result)
        text = json.dumps(result)
        self.assertIn(CORE, text)
        self.assertNotIn('"StateMachineArn": "' + PARENT + '"', text)

    def test_source_unchanged_and_core_exact(self):
        original = copy.deepcopy(SOURCE["definition"])
        wrapper(original, "sg-00000000000006a09")
        self.assertEqual(original, SOURCE["definition"])
        infra = template(original, SOURCE["roleArn"])
        self.assertEqual(infra["Resources"]["WebCore"]["Properties"]["Definition"], original)
        self.assertNotIn("AWS::EC2::Instance", json.dumps(infra))

    def test_preview_and_isolation_only_skip_bastion(self):
        result = wrapper(SOURCE["definition"], "sg-00000000000006a09")["States"]
        self.assertEqual(result["ChooseExecutionMode"], SOURCE["definition"]["States"]["ChooseExecutionMode"])
        self.assertEqual(result["BackupPreflightNeeded"]["Default"], "RunWebOnly")
        self.assertEqual(result["PreflightRdsSnapshotReady"]["Choices"][0]["Next"], "PrepareBastion")
        self.assertEqual(result["PrepareBastion"]["Next"], "RespondInParallel")

    def test_quarantine_only_operating_bastion(self):
        result = wrapper(SOURCE["definition"], "sg-00000000000006a09")
        branch = result["States"]["RespondInParallel"]["Branches"][1]["States"]["BastionBranchRun"]["Branches"][0]
        self.assertEqual(branch["States"]["IsolateOperatingBastion"]["Parameters"], {
            "InstanceId": OLD, "Groups": ["sg-00000000000006a09"]
        })

    def test_failure_does_not_cancel_sibling(self):
        result = wrapper(SOURCE["definition"], "sg-00000000000006a09")["States"]
        for prefix, branch in zip(("WebBranch", "BastionBranch"), result["RespondInParallel"]["Branches"]):
            self.assertEqual(branch["States"][prefix + "Run"]["Catch"][0]["Next"], prefix + "Failed")
            self.assertTrue(branch["States"][prefix + "Failed"]["End"])
        self.assertEqual(result["RespondInParallel"]["Next"], "ReleaseResponseLock")
        self.assertIn("ConditionExpression", result["ReleaseResponseLock"]["Parameters"])

    def test_no_existing_state_or_shared_resources_managed(self):
        tf = (HERE / "runtime/terraform/main.tf").read_text(encoding="utf-8")
        self.assertEqual(tf.count('resource "'), 1)
        self.assertIn("prevent_destroy = true", tf)
        self.assertNotIn("bastion_test", tf)

    def test_already_wrapped_rejected(self):
        result = wrapper(SOURCE["definition"], "sg-00000000000006a09")
        with self.assertRaises(ValueError):
            wrapper(result, "sg-00000000000006a09")

    def test_duplicate_sibling_names_rejected(self):
        machine = {"StartAt": "Parallel", "States": {"Parallel": {
            "Type": "Parallel", "End": True, "Branches": [
                {"StartAt": "Duplicate", "States": {"Duplicate": {"Type": "Succeed"}}},
                {"StartAt": "Duplicate", "States": {"Duplicate": {"Type": "Succeed"}}}
            ]
        }}}
        with self.assertRaisesRegex(AssertionError, "duplicate state names"):
            check_graph(machine)


# 검증 함수는 AWS를 사용하지 않습니다. 로컬에 SDK가 없어도 순수 함수 테스트 가능.
sys.modules.setdefault("boto3", types.ModuleType("boto3"))
exceptions = types.ModuleType("botocore.exceptions")
exceptions.ClientError = type("ClientError", (Exception,), {})
sys.modules.setdefault("botocore", types.ModuleType("botocore"))
sys.modules.setdefault("botocore.exceptions", exceptions)
spec = importlib.util.spec_from_file_location("worker", HERE / "runtime/worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class PlanTests(unittest.TestCase):
    def plan(self):
        return {"resource_changes": [{"mode": "managed", "address": "aws_instance.replacement", "change": {
            "actions": ["create"], "after": {"ami": worker.AMI, "subnet_id": worker.SUBNET,
            "vpc_security_group_ids": [worker.NORMAL_SG, worker.METRICS_SG]}
        }}]}

    def test_creation_accepted(self):
        worker.check_plan(self.plan())

    def test_destroy_replace_update_noop_rejected(self):
        for action in (["delete"], ["delete", "create"], ["update"], ["no-op"]):
            plan = self.plan()
            plan["resource_changes"][0]["change"]["actions"] = action
            with self.assertRaises(RuntimeError):
                worker.check_plan(plan)

    def test_shared_resource_change_rejected(self):
        plan = self.plan()
        plan["resource_changes"].append({"mode": "managed", "address": "aws_iam_role_policy.shared"})
        with self.assertRaises(RuntimeError):
            worker.check_plan(plan)

    def test_wrong_sg_rejected(self):
        plan = self.plan()
        plan["resource_changes"][0]["change"]["after"]["vpc_security_group_ids"] = ["sg-other"]
        with self.assertRaises(RuntimeError):
            worker.check_plan(plan)


if __name__ == "__main__":
    unittest.main()
