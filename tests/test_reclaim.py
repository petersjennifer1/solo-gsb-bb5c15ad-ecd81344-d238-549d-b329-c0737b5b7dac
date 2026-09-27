"""部分回收（reclaim）接口测试：HTTP 端到端 + 区间重写不变量。

覆盖：
* 成功回收单条记录、跨越相邻多条记录、单条记录内部挖洞；
* 仍被占用的地址重写为**最少**的规范 CIDR（相邻碎片合并）；
* 409：版本过期、含未分配地址、含保留地址、越出父网段（地址池不变）；
* 422：非规范（主机位非零）CIDR、缺字段；
* 回收后池状态查询与后续分配预演立即反映释放空间；
* IPv6 行为；
* 随机区间不变量：回收结果 == 已分配合并区间减去目标区间。

直接运行：``python3 -m tests.test_reclaim``（需要 fastapi），
或 ``pytest tests/test_reclaim.py``。
"""
from __future__ import annotations

import ipaddress
import os
import random
import tempfile

import pytest

os.environ.setdefault("PARENT_CIDR", "10.10.0.0/16")
os.environ.setdefault("RESERVED_CIDRS", "10.10.0.0/20,10.10.255.0/24")

from fastapi.testclient import TestClient  # noqa: E402

from app.allocator import (  # noqa: E402
    CidrAllocationSolver,
    _collapse_ranges,
    _network_bounds,
    _ranges_to_cidrs,
)
from app.config import Settings  # noqa: E402
from app.db import ConflictError, Database  # noqa: E402
from app.db import _subtract_ranges_for_reclaim  # noqa: E402
from app.main import app, get_db  # noqa: E402

PARENT = "10.10.0.0/16"
RESERVED = "10.10.0.0/20,10.10.255.0/24"


def _fresh_db(parent: str = PARENT, reserved: str = RESERVED) -> Database:
    return Database(
        Settings(
            db_path=os.path.join(tempfile.mkdtemp(), "pool.db"),
            parent_cidr=parent,
            reserved_cidrs_raw=reserved,
        )
    )


@pytest.fixture
def client():
    db = _fresh_db()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _preview(c, prefixes):
    r = c.post("/api/allocations/preview", json={"prefix_lengths": prefixes})
    assert r.status_code == 200, r.text
    return r.json()


def _commit(c, version, allocations):
    r = c.post(
        "/api/allocations/commit",
        json={"pool_version": version, "allocations": allocations},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _reclaim(c, version, cidr):
    return c.post(
        "/api/allocations/reclaim",
        json={"pool_version": version, "cidr": cidr},
    )


# ---------------------------------------------------------------------------
# 1. 基本成功路径
# ---------------------------------------------------------------------------

def test_reclaim_single_record(client):
    """回收一整条分配记录：记录消失、版本自增、空闲空间立即可见。"""
    plan = _preview(client, [24])
    cidr = plan["allocations"][0]
    assert _commit(client, 1, [cidr])["pool_version"] == 2

    r = _reclaim(client, 2, cidr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "reclaimed"
    assert body["reclaimed_cidrs"] == [cidr]
    assert cidr not in body["allocated_cidrs"]
    assert body["pool_version"] == 3

    pool = client.get("/api/pool").json()
    assert pool["version"] == 3
    assert cidr not in pool["allocated_cidrs"]
    net = ipaddress.ip_network(cidr)
    assert any(net.subnet_of(ipaddress.ip_network(f)) for f in pool["free_cidrs"])


def test_preview_immediately_reuses_reclaimed_space(client):
    """回收后预演必须立即把释放出的最优地址重新给出。"""
    cidr = _preview(client, [24])["allocations"][0]
    _commit(client, 1, [cidr])
    assert _reclaim(client, 2, cidr).status_code == 200

    again = _preview(client, [24])
    assert again["pool_version"] == 3
    assert again["allocations"][0] == cidr


# ---------------------------------------------------------------------------
# 2. 跨越多条相邻记录 + 最少规范 CIDR 重写
# ---------------------------------------------------------------------------

def test_reclaim_spans_multiple_adjacent_records(client):
    """目标 /23 由两条相邻 /24 完全覆盖：两条一并回收，剩余碎片合并。"""
    allocs = _preview(client, [24, 24, 24, 24])["allocations"]
    _commit(client, 1, allocs)

    third = int(ipaddress.ip_network(allocs[2]).network_address)
    target = str(ipaddress.ip_network((third, 23)))
    r = _reclaim(client, 2, target)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reclaimed_cidrs"] == [target]
    assert allocs[2] not in body["allocated_cidrs"]
    assert allocs[3] not in body["allocated_cidrs"]
    # 前两条相邻 /24 被重写合并成单个 /23（最少规范 CIDR）。
    first = int(ipaddress.ip_network(allocs[0]).network_address)
    assert body["allocated_cidrs"] == [
        str(ipaddress.ip_network((first, 23)))
    ], body["allocated_cidrs"]
    assert body["pool_version"] == 3


def test_reclaim_hole_inside_single_record_rewrites_minimally(client):
    """回收一条 /24 的前半个 /25：剩余单个 /25（最少规范 CIDR）。"""
    cidr = _preview(client, [24])["allocations"][0]
    _commit(client, 1, [cidr])
    base = int(ipaddress.ip_network(cidr).network_address)
    half = str(ipaddress.ip_network((base, 25)))
    r = _reclaim(client, 2, half)
    assert r.status_code == 200, r.text
    assert r.json()["allocated_cidrs"] == [
        str(ipaddress.ip_network((base + 128, 25)))
    ]


# ---------------------------------------------------------------------------
# 3. 冲突路径：409 且地址池不变
# ---------------------------------------------------------------------------

def test_reclaim_stale_version_conflicts_without_mutation(client):
    cidr = _preview(client, [24])["allocations"][0]
    _commit(client, 1, [cidr])  # v1 -> v2
    # 制造版本推进：再分配一个 /28（v2 -> v3）。
    other = _preview(client, [28])["allocations"][0]
    _commit(client, 2, other)
    snapshot = client.get("/api/pool").json()

    r = _reclaim(client, 2, cidr)  # 过期版本
    assert r.status_code == 409
    assert "version" in r.json()["detail"]

    after = client.get("/api/pool").json()
    assert after["version"] == snapshot["version"] == 3
    assert after["allocated_cidrs"] == snapshot["allocated_cidrs"]


def test_reclaim_unallocated_target_conflicts(client):
    free_24 = str(
        ipaddress.ip_network(
            (
                int(ipaddress.ip_network(client.get("/api/pool").json()["free_cidrs"][0]).network_address),
                24,
            )
        )
    )
    r = _reclaim(client, 1, free_24)
    assert r.status_code == 409
    assert "allocated" in r.json()["detail"]
    assert client.get("/api/pool").json()["version"] == 1


def test_reclaim_reserved_and_outside_parent_conflict(client):
    # 含保留地址。
    r = _reclaim(client, 1, "10.10.0.0/24")
    assert r.status_code == 409
    assert "reserved" in r.json()["detail"]
    # 越出父网段。
    r = _reclaim(client, 1, "10.11.0.0/24")
    assert r.status_code == 409
    assert "outside" in r.json()["detail"]
    assert client.get("/api/pool").json()["version"] == 1


def test_reclaim_target_spanning_unallocated_gap_conflicts(client):
    """目标跨越“已分配 + 空闲 + 保留”时 409；完全覆盖的 /23 成功。"""
    allocs = _preview(client, [24, 24])["allocations"]
    # 最优位置贴保留段：252、253（254 空闲，255 保留）。
    assert allocs == ["10.10.252.0/24", "10.10.253.0/24"], allocs
    _commit(client, 1, allocs)

    # /22 = 252..255：含空闲 254 与保留 255。
    r = _reclaim(client, 2, "10.10.252.0/22")
    assert r.status_code == 409
    assert client.get("/api/pool").json()["version"] == 2

    # 合法的 /23（恰好覆盖 252-253 两条记录）成功且清空。
    ok = _reclaim(client, 2, "10.10.252.0/23")
    assert ok.status_code == 200, ok.text
    assert ok.json()["allocated_cidrs"] == []
    assert ok.json()["pool_version"] == 3


def test_reclaim_empty_pool_conflicts(client):
    r = _reclaim(client, 1, "10.10.16.0/24")
    assert r.status_code == 409
    assert client.get("/api/pool").json()["version"] == 1


# ---------------------------------------------------------------------------
# 4. 参数校验：422
# ---------------------------------------------------------------------------

def test_reclaim_validation_errors(client):
    # 非规范（主机位非零）。
    r = _reclaim(client, 1, "10.10.1.5/24")
    assert r.status_code == 422
    # 非法 CIDR 字符串。
    r = _reclaim(client, 1, "not-a-cidr")
    assert r.status_code == 422
    # 缺字段。
    r = client.post("/api/allocations/reclaim", json={"pool_version": 1})
    assert r.status_code == 422
    # 版本 < 1。
    r = client.post(
        "/api/allocations/reclaim",
        json={"pool_version": 0, "cidr": "10.10.1.0/24"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 5. IPv6
# ---------------------------------------------------------------------------

def test_reclaim_ipv6():
    db = _fresh_db(parent="2001:db8::/48", reserved="")
    plan = CidrAllocationSolver(db.get_state().parent, [], (64, 64)).solve()
    state = db.commit_plan(1, plan.networks)
    assert state.version == 2
    base = int(plan.networks[0].network_address)
    state = db.reclaim_allocation(2, ipaddress.ip_network((base, 63)))
    assert state.version == 3
    assert state.allocated == ()


def test_reclaim_ipv6_reserved_gap_conflicts():
    db = _fresh_db(parent="2001:db8::/48", reserved="2001:db8:0:1::/64")
    plan = CidrAllocationSolver(
        db.get_state().parent, list(db.get_state().reserved), (64, 64)
    ).solve()
    # 两个 /64 分置保留段两侧（::/64 与 0:2::/64），/63 跨过保留段。
    db.commit_plan(1, plan.networks)
    base = int(plan.networks[0].network_address)
    with pytest.raises(ConflictError):
        db.reclaim_allocation(2, ipaddress.ip_network((base, 63)))


# ---------------------------------------------------------------------------
# 6. 随机区间不变量（DB 层）
# ---------------------------------------------------------------------------

def test_reclaim_random_invariant():
    rng = random.Random(2026)
    checked = 0
    for _ in range(150):
        db = _fresh_db(parent="10.0.0.0/16", reserved="")
        prefs = [rng.randint(25, 29) for _ in range(rng.randint(1, 6))]
        try:
            plan = CidrAllocationSolver(
                db.get_state().parent, [], tuple(prefs)
            ).solve()
        except ConflictError:
            continue
        state = db.commit_plan(1, plan.networks)
        merged = _collapse_ranges([_network_bounds(n) for n in state.allocated])
        lo, hi = rng.choice(merged)
        plen = rng.choice((26, 27, 28))
        size = 1 << (32 - plen)
        starts = list(range((lo + size - 1) // size * size, hi - size + 2, size))
        if not starts:
            continue
        start = rng.choice(starts)
        target = ipaddress.ip_network((start, plen))
        hole = (start, start + size - 1)

        covered = all(
            any(a <= x <= b for a, b in merged) for x in range(start, start + size)
        )
        if covered:
            nxt = db.reclaim_allocation(2, target)
            assert nxt.version == 3
            expected_ranges = _collapse_ranges(
                _subtract_ranges_for_reclaim(merged, hole)
            )
            got_ranges = _collapse_ranges(
                [_network_bounds(n) for n in nxt.allocated]
            )
            assert got_ranges == expected_ranges
            expected_nets = {
                str(ipaddress.ip_network(x))
                for x in _ranges_to_cidrs(32, expected_ranges)
            }
            # 重写结果即最少规范 CIDR 集合。
            assert {str(n) for n in nxt.allocated} == expected_nets
            checked += 1
        else:
            with pytest.raises(ConflictError):
                db.reclaim_allocation(2, target)
    assert checked >= 50


def main():
    fns = [
        test_reclaim_single_record,
        test_preview_immediately_reuses_reclaimed_space,
        test_reclaim_spans_multiple_adjacent_records,
        test_reclaim_hole_inside_single_record_rewrites_minimally,
        test_reclaim_stale_version_conflicts_without_mutation,
        test_reclaim_unallocated_target_conflicts,
        test_reclaim_reserved_and_outside_parent_conflict,
        test_reclaim_target_spanning_unallocated_gap_conflicts,
        test_reclaim_empty_pool_conflicts,
        test_reclaim_validation_errors,
        test_reclaim_ipv6,
        test_reclaim_ipv6_reserved_gap_conflicts,
        test_reclaim_random_invariant,
    ]
    for fn in fns:
        if "client" in fn.__code__.co_varnames:
            db = _fresh_db()
            app.dependency_overrides[get_db] = lambda: db
            with TestClient(app) as c:
                fn(c)
            app.dependency_overrides.clear()
        else:
            fn()
        print(f"PASS {fn.__name__}")
    print("ALL RECLAIM TESTS PASSED")


if __name__ == "__main__":
    main()
