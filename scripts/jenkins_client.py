#!/usr/bin/env python3
"""
Jenkins CLI 客户端 —— 供 Copilot Agent 与命令行直接使用。

功能：
  list      拉取 Jenkins 全部 job（含最近构建状态）
  info      查看单个 job 的详细信息
  trigger   触发 job 构建（支持参数化构建）
  analyze   分析 job 的历史构建，给出优化建议

认证：通过环境变量注入，不要在命令行明文传 token
  JENKINS_URL    例如 https://jenkins.example.com
  JENKINS_USER   用户名
  JENKINS_TOKEN  API Token（Jenkins -> 用户 -> Configure -> API Token）

用法：
  python3 jenkins_client.py list
  python3 jenkins_client.py info my-job
  python3 jenkins_client.py trigger my-job --param BRANCH=main --param ENV=staging
  python3 jenkins_client.py analyze my-job --builds 30
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


# --------------------------------------------------------------------------- #
# 基础 HTTP 层
# --------------------------------------------------------------------------- #

class JenkinsError(Exception):
    pass


def _load_config(args) -> tuple[str, str, str]:
    url = (args.url or os.environ.get("JENKINS_URL") or "").rstrip("/")
    user = args.user or os.environ.get("JENKINS_USER") or ""
    token = args.token or os.environ.get("JENKINS_TOKEN") or ""
    if not url:
        raise JenkinsError("缺少 Jenkins 地址：请设置 JENKINS_URL 或使用 --url")
    if not user or not token:
        raise JenkinsError("缺少认证信息：请设置 JENKINS_USER / JENKINS_TOKEN")
    return url, user, token


def _request(method: str, url: str, user: str, token: str,
             data: bytes | None = None, extra_headers: dict | None = None,
             raw: bool = False):
    import base64
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization",
                   "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode())
    req.add_header("User-Agent", "jenkins-copilot-agent/1.0")
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            if raw:
                return body, dict(resp.headers)
            if not body:
                return None, dict(resp.headers)
            return json.loads(body.decode("utf-8")), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raise JenkinsError(f"HTTP {e.code} {e.reason}: {url}\n{e.read().decode('utf-8', 'ignore')[:500]}")
    except urllib.error.URLError as e:
        raise JenkinsError(f"连接失败: {url} ({e.reason})")


def _get_json(base, user, token, path, tree=None):
    url = f"{base}{path}"
    if tree:
        url += ("&" if "?" in url else "?") + "tree=" + urllib.parse.quote(tree)
    data, _ = _request("GET", url, user, token)
    return data


def _get_crumb(base, user, token) -> dict:
    """获取 CSRF crumb；未启用 CSRF 防护时返回空 dict。"""
    try:
        data, _ = _request("GET", f"{base}/crumbIssuer/api/json", user, token)
        return {data["crumbRequestField"]: data["crumb"]}
    except JenkinsError:
        return {}


def _job_path(job_name: str) -> str:
    """支持 folder 路径，如 'folder/sub/job' -> /job/folder/job/sub/job/job"""
    parts = [p for p in job_name.strip("/").split("/") if p]
    return "".join("/job/" + urllib.parse.quote(p, safe="") for p in parts)


def _fmt_ts(ms: int | None) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_dur(ms: int | None) -> str:
    if not ms:
        return "-"
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m{s % 60}s"


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #

COLOR_MAP = {
    "blue": "成功", "red": "失败", "yellow": "不稳定", "aborted": "已中止",
    "notbuilt": "未构建", "disabled": "已禁用", "blue_anime": "构建中",
    "red_anime": "构建中(上次失败)", "yellow_anime": "构建中(上次不稳定)",
    "grey": "排队中", "grey_anime": "排队中",
}


def cmd_list(args):
    base, user, token = _load_config(args)
    data = _get_json(base, user, token, "/api/json",
                     tree="jobs[name,url,color,buildable,lastBuild[number,result,timestamp,duration]]")
    jobs = data.get("jobs", [])
    rows = []
    for j in jobs:
        lb = j.get("lastBuild") or {}
        rows.append({
            "job": j.get("name"),
            "状态": COLOR_MAP.get(str(j.get("color", "")).replace("_anime", "_anime"), j.get("color")),
            "最近构建": f"#{lb.get('number')}" if lb else "-",
            "最近结果": lb.get("result") or "-",
            "最近时间": _fmt_ts(lb.get("timestamp")),
            "耗时": _fmt_dur(lb.get("duration")),
            "url": j.get("url"),
        })
    _print(rows, args)


# --------------------------------------------------------------------------- #
# info
# --------------------------------------------------------------------------- #

def cmd_info(args):
    base, user, token = _load_config(args)
    path = _job_path(args.job)
    data = _get_json(base, user, token, f"{path}/api/json",
                     tree="name,url,description,buildable,concurrentBuild,"
                          "healthReport[description,score],"
                          "property[parameterDefinitions[name,type,defaultParameterValue[value],description]],"
                          "lastBuild[number,result,timestamp],lastSuccessfulBuild[number],"
                          "lastFailedBuild[number],lastStableBuild[number],nextBuildNumber")
    props = data.get("property") or []
    params = []
    for p in props:
        for pd in (p.get("parameterDefinitions") or []):
            params.append({
                "参数": pd.get("name"),
                "类型": pd.get("type"),
                "默认值": (pd.get("defaultParameterValue") or {}).get("value"),
                "说明": (pd.get("description") or "").strip()[:60],
            })
    out = {
        "job": data.get("name"),
        "url": data.get("url"),
        "描述": (data.get("description") or "").strip() or "-",
        "可构建": data.get("buildable"),
        "允许并发构建": bool(data.get("concurrentBuild")),
        "健康度": [{"说明": h.get("description"), "得分": h.get("score")}
                    for h in (data.get("healthReport") or [])],
        "构建参数": params or "（非参数化构建）",
        "最近构建": (data.get("lastBuild") or {}).get("number"),
        "最近成功": (data.get("lastSuccessfulBuild") or {}).get("number"),
        "最近失败": (data.get("lastFailedBuild") or {}).get("number"),
        "下一个构建号": data.get("nextBuildNumber"),
    }
    _print(out, args)


# --------------------------------------------------------------------------- #
# trigger
# --------------------------------------------------------------------------- #

def cmd_trigger(args):
    base, user, token = _load_config(args)
    path = _job_path(args.job)
    crumb = _get_crumb(base, user, token)

    params = {}
    for kv in args.param or []:
        if "=" not in kv:
            raise JenkinsError(f"参数格式应为 KEY=VALUE，收到: {kv}")
        k, v = kv.split("=", 1)
        params[k] = v

    if params:
        url = f"{base}{path}/buildWithParameters?" + urllib.parse.urlencode(params)
    else:
        url = f"{base}{path}/build"
    _, headers = _request("POST", url, user, token, data=b"", extra_headers=crumb)
    queue_url = headers.get("Location", "")

    result = {"已触发": args.job, "队列地址": queue_url or "（未返回，可稍后查看）", "参数": params or None}

    if queue_url and args.wait:
        result["跟踪"] = _wait_queue_and_build(base, user, token, queue_url)
    _print(result, args)


def _wait_queue_and_build(base, user, token, queue_url, timeout=600):
    """轮询队列 -> 拿到 build number -> 轮询构建直到结束。"""
    deadline = time.time() + timeout
    build_url = None
    q_api = queue_url.rstrip("/") + "/api/json"
    while time.time() < deadline:
        try:
            q, _ = _request("GET", q_api, user, token)
        except JenkinsError:
            break
        exe = q.get("executable")
        if exe and exe.get("url"):
            build_url = exe["url"]
            break
        if q.get("cancelled"):
            return {"结果": "已在队列中被取消"}
        time.sleep(3)
    if not build_url:
        return {"结果": "等待构建分配超时", "队列": queue_url}

    build_api = build_url.rstrip("/") + "/api/json"
    while time.time() < deadline:
        b, _ = _request("GET", build_api, user, token)
        if not b.get("building"):
            return {
                "构建": f"#{b.get('number')}",
                "结果": b.get("result"),
                "耗时": _fmt_dur(b.get("duration")),
                "链接": b.get("url"),
            }
        time.sleep(5)
    return {"结果": "等待构建结束超时", "链接": build_url}


# --------------------------------------------------------------------------- #
# analyze
# --------------------------------------------------------------------------- #

def cmd_analyze(args):
    base, user, token = _load_config(args)
    path = _job_path(args.job)
    n = args.builds

    job = _get_json(base, user, token, f"{path}/api/json",
                    tree="name,url,buildable,concurrentBuild,"
                         "builds[number,result,timestamp,duration,queueId,builtOn,"
                         "actions[causes[shortDescription]]]")
    builds = [b for b in (job.get("builds") or []) if b.get("result")][:n]

    # 读取 config.xml 做配置层面分析
    try:
        cfg_raw, _ = _request("GET", f"{base}{path}/config.xml", user, token, raw=True)
        config_xml = cfg_raw.decode("utf-8", "ignore")
    except JenkinsError:
        config_xml = ""

    report = {
        "job": job.get("name"),
        "url": job.get("url"),
        "分析构建数": len(builds),
        "统计": _stats(builds),
        "配置观察": _config_findings(job, config_xml),
        "优化建议": [],
    }
    report["优化建议"] = _recommendations(report["统计"], report["配置观察"], builds)
    _print(report, args)


def _stats(builds):
    if not builds:
        return {"提示": "没有足够的已完成构建可供分析"}
    results = [b["result"] for b in builds]
    durations = [b.get("duration", 0) / 1000 for b in builds if b.get("duration")]
    fail = sum(1 for r in results if r in ("FAILURE", "ABORTED"))
    unstable = sum(1 for r in results if r == "UNSTABLE")

    # 连续失败段
    streak, max_streak = 0, 0
    for r in results:  # builds 按时间倒序
        if r == "FAILURE":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    # 耗时趋势：最近 1/3 vs 最旧 1/3
    trend = "样本不足"
    if len(durations) >= 6:
        k = len(durations) // 3
        recent = statistics.mean(durations[:k])
        old = statistics.mean(durations[-k:])
        if old > 0:
            change = (recent - old) / old
            trend = f"{'变快' if change < -0.05 else '变慢' if change > 0.05 else '平稳'}（最近均值 {recent:.0f}s vs 早期均值 {old:.0f}s，变化 {change:+.0%}）"

    stat = {
        "成功率": f"{(len(results) - fail - unstable) / len(results):.0%}",
        "失败率": f"{fail / len(results):.0%}",
        "不稳定率": f"{unstable / len(results):.0%}",
        "最长连续失败": max_streak,
        "平均耗时": f"{statistics.mean(durations):.0f}s" if durations else "-",
        "P95耗时": f"{sorted(durations)[int(len(durations) * 0.95) - 1]:.0f}s" if durations else "-",
        "耗时趋势": trend,
    }
    return stat


def _config_findings(job, config_xml: str):
    findings = {}
    findings["允许并发构建"] = bool(job.get("concurrentBuild"))
    if not config_xml:
        findings["config.xml"] = "无法读取（权限不足？），配置分析跳过"
        return findings

    try:
        root = ET.fromstring(config_xml)
    except ET.ParseError:
        findings["config.xml"] = "解析失败"
        return findings

    def has(tag):
        el = root.find(f".//{tag}")
        return el is not None and (el.text or "").strip() not in ("", "false")

    log_rotator = root.find(".//logRotator")
    if log_rotator is not None:
        keep = (log_rotator.findtext("numToKeep") or "-1").strip()
        findings["构建保留数量"] = "无限制" if keep in ("-1", "") else keep
    else:
        findings["构建保留数量"] = "未配置（无限制）"

    findings["SCM 轮询触发"] = has("hudson.triggers.SCMTrigger")
    findings["定时触发"] = has("hudson.triggers.TimerTrigger")
    findings["GitHub Webhook 触发"] = has("com.cloudbees.jenkins.GitHubPushTrigger")
    findings["阻塞上下游构建"] = root.find(".//blockBuildWhenDownstreamBuilding") is not None
    findings["安静期(秒)"] = (root.findtext("quietPeriod") or "默认").strip()
    findings["超时插件"] = root.find(".//hudson.plugins.build__timeout.BuildTimeoutWrapper") is not None
    findings["时间戳插件"] = root.find(".//hudson.plugins.timestamper.TimestamperBuildWrapper") is not None
    findings["归档产物"] = root.find(".//hudson.tasks.ArtifactArchiver") is not None
    findings["限定节点(Label)"] = (root.findtext("assignedNode") or
                                    (root.find(".//assignedLabel") is not None and "有") or "任意节点")
    if isinstance(findings["限定节点(Label)"], bool):
        findings["限定节点(Label)"] = "有"
    return findings


def _recommendations(stats, cfg, builds):
    recs = []

    # 稳定性
    try:
        fail_rate = float(stats.get("失败率", "0%").strip("%")) / 100
    except Exception:
        fail_rate = 0
    if stats.get("最长连续失败", 0) >= 3:
        recs.append(f"存在连续 {stats['最长连续失败']} 次失败：建议检查是否为环境问题或不稳定测试，"
                    "考虑引入重试机制（Retry 插件 / retry 步骤）并将 flaky test 隔离标记。")
    elif fail_rate >= 0.2:
        recs.append(f"失败率偏高（{stats['失败率']}）：建议先提升稳定性，"
                    "如在 Pipeline 中加入超时（timeout）、失败自动重试与更明确的失败通知。")

    # 性能
    trend = stats.get("耗时趋势", "")
    if "变慢" in trend:
        recs.append(f"构建耗时呈上升趋势（{trend}）：建议排查依赖下载（可引入依赖缓存 / 镜像仓库）、"
                    "测试数量增长（可按模块并行拆分测试）、以及 workspace 体积膨胀（定期 cleanWs）。")

    # 配置
    if cfg.get("构建保留数量") in ("无限制", "未配置（无限制）"):
        recs.append("未配置构建历史保留策略：磁盘会持续增长，建议配置 Log Rotation"
                    "（如保留最近 30 次 / 30 天），Pipeline 中使用 options { buildDiscarder(logRotator(numToKeepStr: '30')) }。")
    if cfg.get("SCM 轮询触发") and not cfg.get("GitHub Webhook 触发"):
        recs.append("当前使用 SCM 轮询触发：建议改用 Webhook（GitHub/GitLab 插件）以消除轮询延迟并降低服务器负载。")
    if not cfg.get("超时插件"):
        recs.append("未配置构建超时：建议添加 timeout（如 Pipeline: options { timeout(time: 30, unit: 'MINUTES') }），"
                    "避免任务挂死长时间占用 executor。")
    if not cfg.get("时间戳插件"):
        recs.append("未启用时间戳输出：建议启用 Timestamper（Pipeline: options { timestamps() }），便于定位慢步骤。")
    if cfg.get("允许并发构建"):
        recs.append("已开启并发构建：确认 workspace/资源（端口、数据库、产物目录）不存在并发冲突；"
                    "若不需要并发，建议关闭并改用 disableConcurrentBuilds() 避免排队异常。")
    if cfg.get("限定节点(Label)") in ("任意节点",):
        recs.append("Job 未限定节点 Label：如构建依赖特定环境（JDK 版本、Docker 等），"
                    "建议指定 agent label，保证构建环境一致、可复现。")
    if cfg.get("归档产物"):
        recs.append("检测到产物归档：确认仅归档必要文件（配合 excludes / fingerprint 控制体积），"
                    "过大的产物会拖慢构建并占用 master 存储。")
    if not recs:
        recs.append("未发现明显优化点：该 job 的稳定性与配置都处于健康状态。")
    return recs


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #

def _print(obj, args):
    if getattr(args, "json", False):
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        _print_table(obj)
    else:
        _print_kv(obj)


def _print_table(rows):
    keys = list(rows[0].keys())
    widths = {k: max(_w(str(r.get(k, ""))) for r in rows + [dict(zip(keys, keys))]) for k in keys}
    header = "  ".join(k.ljust(widths[k] - _w(k) + len(k)) for k in keys)
    print(header)
    print("  ".join("-" * widths[k] for k in keys))
    for r in rows:
        print("  ".join(str(r.get(k, "")).ljust(widths[k] - _w(str(r.get(k, ""))) + len(str(r.get(k, ""))))
                        for k in keys))


def _w(s: str) -> int:
    """中文字符按宽度 2 计算。"""
    return sum(2 if ord(c) > 0x2E7F else 1 for c in s)


def _print_kv(obj, indent=0):
    pad = "  " * indent
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                print(f"{pad}{k}:")
                _print_kv(v, indent + 1)
            else:
                print(f"{pad}{k}: {v}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _print_kv(item, indent)
                print(f"{pad}---")
            else:
                print(f"{pad}- {item}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="Jenkins CLI client for Copilot Agent")
    parser.add_argument("--url", help="Jenkins 地址（默认读 JENKINS_URL）")
    parser.add_argument("--user", help="用户名（默认读 JENKINS_USER）")
    parser.add_argument("--token", help="API Token（默认读 JENKINS_TOKEN）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出所有 job")

    p_info = sub.add_parser("info", help="查看 job 详情")
    p_info.add_argument("job")

    p_trig = sub.add_parser("trigger", help="触发 job 构建")
    p_trig.add_argument("job")
    p_trig.add_argument("--param", action="append", help="构建参数 KEY=VALUE，可多次")
    p_trig.add_argument("--wait", action="store_true", help="等待构建完成并返回结果")

    p_ana = sub.add_parser("analyze", help="分析 job 并给出优化建议")
    p_ana.add_argument("job")
    p_ana.add_argument("--builds", type=int, default=30, help="分析的最近构建数量（默认 30）")

    args = parser.parse_args()
    try:
        {"list": cmd_list, "info": cmd_info, "trigger": cmd_trigger,
         "analyze": cmd_analyze}[args.cmd](args)
    except JenkinsError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
