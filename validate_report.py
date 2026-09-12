#!/usr/bin/env python3
"""校验一份进展汇报是否符合 kickstart/04 约定的格式。

单独使用:
    python bin/validate_report.py share/reports/2026-09-10T1800.yaml

被 bin/publish 自动调用。校验只在本地进行，不联网。
"""
from __future__ import annotations

import pathlib
import sys

LEVEL_KEYS = [
    "survey",
    "modeling",
    "architecture",
    "blocks",
    "verification",
    "integration",
]
SIMPLE_LEVELS = [k for k in LEVEL_KEYS if k != "blocks"]

TRIGGERS = {"requested", "proactive", "handoff"}
CONFIDENCE = {"low", "medium", "high"}
STAGES = {"concept", "behavioral", "circuit", "layout", "post_layout"}
EVIDENCE = {
    "none",
    "estimate",
    "behavioral_sim",
    "circuit_sim",
    "pvt_sim",
    "post_layout_sim",
    "measured",
}
SPEC_STATUS = {
    "unknown",
    "on_track",
    "at_risk",
    "met",
    "infeasible",
    "redefine_proposed",
}

# 每份汇报必须覆盖的指标 ID（来自 02 号文档第 4 节表格）。
# WORK-* 是工作定义，只在建议修改其定义时才出现，不在必填之列。
REQUIRED_SPEC_IDS = [
    "ASP-001", "ASP-002", "ASP-003", "ASP-004", "ASP-005",
    "ASP-006", "ASP-007", "ASP-008", "ASP-009", "CON-001",
]


def _enum(problems, path, value, allowed):
    if value not in allowed:
        problems.append(
            f"{path}: 取值 {value!r} 不在允许范围 {sorted(allowed)} 内"
        )


def _maturity(problems, path, value):
    if not isinstance(value, int) or not 0 <= value <= 4:
        problems.append(f"{path}: maturity 必须是 0-4 的整数，当前为 {value!r}")


def check_report(data, env_root: pathlib.Path | None = None) -> list[str]:
    """返回问题列表；空列表表示通过。"""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["顶层必须是一个映射（key: value 结构）"]

    if not str(data.get("schema", "")).startswith("mp-report/"):
        problems.append("schema: 必须是 mp-report/<版本>，例如 mp-report/0.1")

    rep = data.get("report")
    if not isinstance(rep, dict):
        problems.append("report: 缺失或不是映射")
    else:
        if not isinstance(rep.get("seq"), int) or rep.get("seq", 0) < 1:
            problems.append("report.seq: 必须是 >=1 的整数")
        if not isinstance(rep.get("time"), str) or not rep.get("time"):
            problems.append("report.time: 必须是带时区的时间字符串")
        _enum(problems, "report.trigger", rep.get("trigger"), TRIGGERS)

    ov = data.get("overall")
    if not isinstance(ov, dict):
        problems.append("overall: 缺失或不是映射")
    else:
        for key in ("phase", "summary"):
            if not isinstance(ov.get(key), str) or not ov.get(key, "").strip():
                problems.append(f"overall.{key}: 必须是非空字符串")
        if not isinstance(ov.get("since_last"), list):
            problems.append("overall.since_last: 必须是列表（首次汇报写 []）")
        _enum(problems, "overall.route_confidence", ov.get("route_confidence"), CONFIDENCE)

    artifacts: list[tuple[str, str]] = []
    lv = data.get("levels")
    if not isinstance(lv, dict):
        problems.append("levels: 缺失或不是映射")
    else:
        for key in LEVEL_KEYS:
            if key not in lv:
                problems.append(f"levels.{key}: 缺失（没有进展也要写，maturity: 0）")
        for key in SIMPLE_LEVELS:
            node = lv.get(key)
            if node is None:
                continue
            if not isinstance(node, dict):
                problems.append(f"levels.{key}: 必须是映射")
                continue
            _maturity(problems, f"levels.{key}", node.get("maturity"))
            arts = node.get("artifacts", [])
            if arts is None:
                arts = []
            if not isinstance(arts, list):
                problems.append(f"levels.{key}.artifacts: 必须是列表")
            else:
                artifacts += [(f"levels.{key}.artifacts", str(a)) for a in arts]
        blocks = lv.get("blocks")
        if blocks is not None:
            if not isinstance(blocks, list):
                problems.append("levels.blocks: 必须是列表（没有模块写 []）")
            else:
                for i, b in enumerate(blocks):
                    tag = f"levels.blocks[{i}]"
                    if not isinstance(b, dict):
                        problems.append(f"{tag}: 必须是映射")
                        continue
                    if not str(b.get("name", "")).strip():
                        problems.append(f"{tag}.name: 必填")
                    if not str(b.get("role", "")).strip():
                        problems.append(f"{tag}.role: 必填（用于跨方案对齐模块）")
                    _enum(problems, f"{tag}.stage", b.get("stage"), STAGES)
                    _maturity(problems, tag, b.get("maturity"))
                    arts = b.get("artifacts") or []
                    if isinstance(arts, list):
                        artifacts += [(f"{tag}.artifacts", str(a)) for a in arts]

    specs = data.get("spec_status")
    if not isinstance(specs, list) or not specs:
        problems.append("spec_status: 必须是非空列表，每条对应 02 文档里的一个指标 ID")
    else:
        seen = set()
        for i, s in enumerate(specs):
            tag = f"spec_status[{i}]"
            if not isinstance(s, dict):
                problems.append(f"{tag}: 必须是映射")
                continue
            sid = str(s.get("id", "")).strip()
            if not sid:
                problems.append(f"{tag}.id: 必填，例如 ASP-003")
            elif sid in seen:
                problems.append(f"{tag}.id: {sid} 重复")
            seen.add(sid)
            _enum(problems, f"{tag}.evidence", s.get("evidence"), EVIDENCE)
            _enum(problems, f"{tag}.status", s.get("status"), SPEC_STATUS)
            if s.get("current") not in (None, "") and not s.get("condition"):
                problems.append(
                    f"{tag}.condition: 给出了数值就必须说明测试条件"
                    "（频率/负载/幅度/工艺角等）"
                )
        missing = [sid for sid in REQUIRED_SPEC_IDS if sid not in seen]
        if missing:
            problems.append(
                "spec_status: 缺少必填指标 " + ", ".join(missing)
                + "（未评估的也要列出，写 evidence: none, status: unknown）"
            )

    qs = data.get("questions_for_human")
    if qs is None:
        problems.append("questions_for_human: 缺失（没有问题写 []）")
    elif not isinstance(qs, list):
        problems.append("questions_for_human: 必须是列表")
    else:
        for i, q in enumerate(qs):
            if not isinstance(q, dict):
                problems.append(f"questions_for_human[{i}]: 必须是映射")
                continue
            if not str(q.get("q", "")).strip():
                problems.append(f"questions_for_human[{i}].q: 必填")
            if not isinstance(q.get("blocking"), bool):
                problems.append(
                    f"questions_for_human[{i}].blocking: 必须是 true 或 false"
                )

    for key in ("risks", "next_steps"):
        if data.get(key) is None:
            problems.append(f"{key}: 缺失（没有内容写 []）")
        elif not isinstance(data[key], list):
            problems.append(f"{key}: 必须是列表")

    if env_root is not None:
        for where, rel in artifacts:
            rel = rel.strip()
            if not rel:
                continue
            if not rel.startswith("share/"):
                problems.append(f"{where}: {rel} 必须是以 share/ 开头的仓库内相对路径")
                continue
            if not (env_root / rel).exists():
                problems.append(f"{where}: 引用的文件不存在 -> {rel}")

    return problems


def validate_file(path: pathlib.Path, env_root: pathlib.Path | None = None) -> list[str]:
    try:
        import yaml
    except ImportError:
        return ["SKIP: 未安装 PyYAML，跳过格式校验（pip install pyyaml）"]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # YAML 语法错误
        return [f"YAML 解析失败: {exc}"]
    return check_report(data, env_root)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    bad = 0
    for arg in sys.argv[1:]:
        p = pathlib.Path(arg)
        # 猜测工作区根目录：包含 share/ 的最近祖先
        root = None
        for cand in [p.resolve()] + list(p.resolve().parents):
            if (cand / "share").is_dir():
                root = cand
                break
        problems = validate_file(p, root)
        if problems:
            bad += 1
            print(f"\n{p}")
            for m in problems:
                print(f"  - {m}")
        else:
            print(f"{p}: 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
