#!/usr/bin/env python3
"""校验一份进展汇报是否符合项目约定的格式。

单独使用:
    python bin/validate_report.py share/reports/2026-09-10T1800.yaml

被 bin/publish 自动调用。校验只在本地进行，不联网。

字段结构（schema: mp-report/<版本>）是平台约定；层次、模块阶段、证据等级、
必填指标这些取值由项目决定：接入时写在工作区的 .mp-publish/profile.json（这个 run 自己的格式），
或打包 publish 时写进 publish_runtime/release.json 的 profile 一节。工作区里的优先。
两处都没有时只查结构，不查取值。
"""
from __future__ import annotations

import json
import pathlib
import sys

SCHEMA_PREFIX = "mp-report/"
DEFAULT_MATURITY_MAX = 4


LOCAL_PROFILE = (".mp-publish", "profile.json")


def local_profile(env_root) -> dict | None:
    """工作区自己的汇报格式（接入时写入）；没有时为 None。"""
    if env_root is None:
        return None
    cand = pathlib.Path(env_root).joinpath(*LOCAL_PROFILE)
    if not cand.is_file():
        return None
    try:
        data = json.loads(cand.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_profile(env_root=None) -> dict:
    """汇报格式：工作区 .mp-publish/profile.json 优先，否则本工具所在 release 的（bin/ 下的副本读基线 release）。"""
    own = local_profile(env_root)
    if own is not None:
        return own
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here / "release.json", here / "publish_runtime" / "release.json"):
        if cand.is_file():
            try:
                return json.loads(cand.read_text(encoding="utf-8")).get("profile") or {}
            except (OSError, ValueError, AttributeError):
                return {}
    return {}


def _keys(value) -> set:
    """枚举可以写成列表或 {取值: 显示名} 映射。"""
    if isinstance(value, dict):
        return set(value)
    return {str(v) for v in value or []}


def rules(profile: dict | None) -> dict:
    p = profile or {}
    levels = [lv for lv in p.get("levels") or [] if isinstance(lv, dict) and lv.get("key")]
    specs = p.get("specs") or {}
    maturity = p.get("maturity")
    return {
        "levels": [lv["key"] for lv in levels],
        "list_levels": {lv["key"]: lv.get("label") or "" for lv in levels if lv.get("list")},
        "maturity_max": len(maturity) - 1 if isinstance(maturity, list) and maturity else DEFAULT_MATURITY_MAX,
        "triggers": _keys(p.get("triggers")),
        "confidence": _keys(p.get("confidence")),
        "stages": _keys(p.get("stages")),
        "evidence": _keys(p.get("evidence")),
        "spec_status": _keys(p.get("spec_status")),
        "required_specs": [str(s["id"] if isinstance(s, dict) else s) for s in specs.get("required") or []],
        "spec_source": specs.get("source") or "",
        "spec_example": specs.get("example") or "",
        "condition_hint": specs.get("condition_hint") or "",
    }


def _enum(problems, path, value, allowed):
    if allowed and value not in allowed:
        problems.append(
            f"{path}: 取值 {value!r} 不在允许范围 {sorted(allowed)} 内"
        )


def _maturity(problems, path, value, top):
    if not isinstance(value, int) or not 0 <= value <= top:
        problems.append(f"{path}: maturity 必须是 0-{top} 的整数，当前为 {value!r}")


def check_report(data, env_root: pathlib.Path | None = None, profile: dict | None = None) -> list[str]:
    """返回问题列表；空列表表示通过。profile 缺省时用工作区或本工具 release 里的项目格式。"""
    r = rules(load_profile(env_root) if profile is None else profile)
    top = r["maturity_max"]
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["顶层必须是一个映射（key: value 结构）"]

    if not str(data.get("schema", "")).startswith(SCHEMA_PREFIX):
        problems.append("schema: 必须是 mp-report/<版本>，例如 mp-report/0.1")

    rep = data.get("report")
    if not isinstance(rep, dict):
        problems.append("report: 缺失或不是映射")
    else:
        if not isinstance(rep.get("seq"), int) or rep.get("seq", 0) < 1:
            problems.append("report.seq: 必须是 >=1 的整数")
        if not isinstance(rep.get("time"), str) or not rep.get("time"):
            problems.append("report.time: 必须是带时区的时间字符串")
        _enum(problems, "report.trigger", rep.get("trigger"), r["triggers"])

    ov = data.get("overall")
    if not isinstance(ov, dict):
        problems.append("overall: 缺失或不是映射")
    else:
        for key in ("phase", "summary"):
            if not isinstance(ov.get(key), str) or not ov.get(key, "").strip():
                problems.append(f"overall.{key}: 必须是非空字符串")
        if not isinstance(ov.get("since_last"), list):
            problems.append("overall.since_last: 必须是列表（首次汇报写 []）")
        _enum(problems, "overall.route_confidence", ov.get("route_confidence"), r["confidence"])

    artifacts: list[tuple[str, str]] = []
    lv = data.get("levels")
    if not isinstance(lv, dict):
        problems.append("levels: 缺失或不是映射")
    else:
        for key in r["levels"]:
            if key not in lv:
                problems.append(f"levels.{key}: 缺失（没有进展也要写，maturity: 0）")
        # 项目定义了层次时按定义区分；没定义时按写法区分（列表即列表型层次）
        names = r["levels"] or list(lv)
        list_levels = r["list_levels"] if r["levels"] else {k: "" for k in lv if isinstance(lv[k], list)}
        for key in names:
            if key in list_levels:
                continue
            node = lv.get(key)
            if node is None:
                continue
            if not isinstance(node, dict):
                problems.append(f"levels.{key}: 必须是映射")
                continue
            _maturity(problems, f"levels.{key}", node.get("maturity"), top)
            arts = node.get("artifacts", [])
            if arts is None:
                arts = []
            if not isinstance(arts, list):
                problems.append(f"levels.{key}.artifacts: 必须是列表")
            else:
                artifacts += [(f"levels.{key}.artifacts", str(a)) for a in arts]
        for key, label in list_levels.items():
            items = lv.get(key)
            if items is None:
                continue
            if not isinstance(items, list):
                problems.append(f"levels.{key}: 必须是列表（没有{label or '内容'}写 []）")
                continue
            for i, b in enumerate(items):
                tag = f"levels.{key}[{i}]"
                if not isinstance(b, dict):
                    problems.append(f"{tag}: 必须是映射")
                    continue
                if not str(b.get("name", "")).strip():
                    problems.append(f"{tag}.name: 必填")
                if not str(b.get("role", "")).strip():
                    problems.append(f"{tag}.role: 必填（用于跨方案对齐{label or '条目'}）")
                _enum(problems, f"{tag}.stage", b.get("stage"), r["stages"])
                _maturity(problems, tag, b.get("maturity"), top)
                arts = b.get("artifacts") or []
                if isinstance(arts, list):
                    artifacts += [(f"{tag}.artifacts", str(a)) for a in arts]

    specs = data.get("spec_status")
    source = f" {r['spec_source']}里的一个" if r["spec_source"] else "一个项目"
    if not isinstance(specs, list) or (not specs and r["required_specs"]):
        problems.append(f"spec_status: 必须是非空列表，每条对应{source}指标 ID")
    else:
        seen = set()
        for i, s in enumerate(specs):
            tag = f"spec_status[{i}]"
            if not isinstance(s, dict):
                problems.append(f"{tag}: 必须是映射")
                continue
            sid = str(s.get("id", "")).strip()
            if not sid:
                problems.append(f"{tag}.id: 必填" + (f"，例如 {r['spec_example']}" if r["spec_example"] else ""))
            elif sid in seen:
                problems.append(f"{tag}.id: {sid} 重复")
            seen.add(sid)
            _enum(problems, f"{tag}.evidence", s.get("evidence"), r["evidence"])
            _enum(problems, f"{tag}.status", s.get("status"), r["spec_status"])
            if s.get("current") not in (None, "") and not s.get("condition"):
                problems.append(
                    f"{tag}.condition: 给出了数值就必须说明测试条件"
                    + (f"（{r['condition_hint']}）" if r["condition_hint"] else "")
                )
        missing = [sid for sid in r["required_specs"] if sid not in seen]
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
