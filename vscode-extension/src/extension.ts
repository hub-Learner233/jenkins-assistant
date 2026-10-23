import * as vscode from "vscode";

/* ------------------------------------------------------------------ */
/* Jenkins REST 客户端（Node 18+ 原生 fetch，无第三方依赖）              */
/* ------------------------------------------------------------------ */

interface JenkinsAuth {
  url: string;
  user: string;
  token: string;
}

interface JenkinsJob {
  name: string;
  url: string;
  color: string;
  buildable: boolean;
  lastBuild?: { number: number; result: string | null; timestamp: number; duration: number };
}

interface BuildRecord {
  number: number;
  result: string | null;
  duration: number;
  timestamp: number;
}

class JenkinsClient {
  constructor(private auth: JenkinsAuth) {}

  private async request(path: string, init?: RequestInit): Promise<Response> {
    const headers: Record<string, string> = {
      Authorization: "Basic " + Buffer.from(`${this.auth.user}:${this.auth.token}`).toString("base64"),
      "User-Agent": "vscode-jenkins-assistant",
    };
    const resp = await fetch(`${this.auth.url}${path}`, { ...init, headers });
    if (!resp.ok) {
      throw new Error(`Jenkins 请求失败 HTTP ${resp.status}: ${path}`);
    }
    return resp;
  }

  private async getJson<T>(path: string): Promise<T> {
    return (await (await this.request(path)).json()) as T;
  }

  async listJobs(): Promise<JenkinsJob[]> {
    const tree = "jobs[name,url,color,buildable,lastBuild[number,result,timestamp,duration]]";
    const data = await this.getJson<{ jobs: JenkinsJob[] }>(
      `/api/json?tree=${encodeURIComponent(tree)}`
    );
    return data.jobs ?? [];
  }

  async jobInfo(jobPath: string): Promise<any> {
    const tree =
      "name,url,description,buildable,concurrentBuild,healthReport[description,score]," +
      "property[parameterDefinitions[name,type,defaultParameterValue[value],description]]," +
      "lastBuild[number,result,timestamp],lastSuccessfulBuild[number],lastFailedBuild[number]";
    return this.getJson(`${jobApiPath(jobPath)}/api/json?tree=${encodeURIComponent(tree)}`);
  }

  private async crumb(): Promise<Record<string, string>> {
    try {
      const c = await this.getJson<{ crumbRequestField: string; crumb: string }>("/crumbIssuer/api/json");
      return { [c.crumbRequestField]: c.crumb };
    } catch {
      return {};
    }
  }

  async trigger(jobPath: string, params: Record<string, string>): Promise<string> {
    const crumb = await this.crumb();
    const headers: Record<string, string> = {
      Authorization: "Basic " + Buffer.from(`${this.auth.user}:${this.auth.token}`).toString("base64"),
      ...crumb,
    };
    const qs = new URLSearchParams(params).toString();
    const url = Object.keys(params).length
      ? `${this.auth.url}${jobApiPath(jobPath)}/buildWithParameters?${qs}`
      : `${this.auth.url}${jobApiPath(jobPath)}/build`;
    const resp = await fetch(url, { method: "POST", headers });
    if (!resp.ok) {
      throw new Error(`触发失败 HTTP ${resp.status}`);
    }
    return resp.headers.get("Location") ?? "";
  }

  async recentBuilds(jobPath: string, limit: number): Promise<BuildRecord[]> {
    const data = await this.getJson<{ builds: BuildRecord[] }>(
      `${jobApiPath(jobPath)}/api/json?tree=${encodeURIComponent("builds[number,result,duration,timestamp]")}`
    );
    return (data.builds ?? []).filter((b) => b.result).slice(0, limit);
  }

  async configXml(jobPath: string): Promise<string> {
    try {
      return await (await this.request(`${jobApiPath(jobPath)}/config.xml`)).text();
    } catch {
      return "";
    }
  }
}

function jobApiPath(nameOrUrl: string): string {
  if (nameOrUrl.startsWith("http")) {
    // 从完整 URL 提取 /job/... 路径
    const m = nameOrUrl.match(/\/job\/.*/);
    return m ? m[0].replace(/\/$/, "") : nameOrUrl;
  }
  return nameOrUrl
    .split("/")
    .filter(Boolean)
    .map((p) => `/job/${encodeURIComponent(p)}`)
    .join("");
}

/* ------------------------------------------------------------------ */
/* 分析逻辑（与 scripts/jenkins_client.py 相同的启发式规则）              */
/* ------------------------------------------------------------------ */

function analyze(jobName: string, builds: BuildRecord[], configXml: string, concurrent: boolean): string {
  const lines: string[] = [`# Jenkins Job 分析报告：${jobName}`, ""];
  if (!builds.length) {
    return lines.join("\n") + "\n没有足够的已完成构建可供分析。";
  }

  const durations = builds.filter((b) => b.duration > 0).map((b) => b.duration / 1000);
  const failures = builds.filter((b) => b.result === "FAILURE" || b.result === "ABORTED").length;
  const unstable = builds.filter((b) => b.result === "UNSTABLE").length;
  const successRate = ((builds.length - failures - unstable) / builds.length) * 100;

  let streak = 0, maxStreak = 0;
  for (const b of builds) {
    if (b.result === "FAILURE") { streak++; maxStreak = Math.max(maxStreak, streak); }
    else { streak = 0; }
  }

  const avg = durations.reduce((a, b) => a + b, 0) / (durations.length || 1);
  const p95 = durations.length ? [...durations].sort((a, b) => a - b)[Math.max(0, Math.ceil(durations.length * 0.95) - 1)] : 0;

  let trend = "样本不足";
  if (durations.length >= 6) {
    const k = Math.floor(durations.length / 3);
    const recent = durations.slice(0, k).reduce((a, b) => a + b, 0) / k;
    const old = durations.slice(-k).reduce((a, b) => a + b, 0) / k;
    const change = (recent - old) / old;
    trend = `${change > 0.05 ? "变慢" : change < -0.05 ? "变快" : "平稳"}（最近 ${recent.toFixed(0)}s vs 早期 ${old.toFixed(0)}s，${(change * 100).toFixed(0)}%）`;
  }

  lines.push("## 构建统计", "",
    `- 分析样本：最近 ${builds.length} 次构建`,
    `- 成功率：${successRate.toFixed(0)}%　失败率：${((failures / builds.length) * 100).toFixed(0)}%　不稳定率：${((unstable / builds.length) * 100).toFixed(0)}%`,
    `- 最长连续失败：${maxStreak} 次`,
    `- 平均耗时：${avg.toFixed(0)}s　P95 耗时：${p95.toFixed(0)}s`,
    `- 耗时趋势：${trend}`, "");

  const has = (re: RegExp) => re.test(configXml);
  const recs: string[] = [];
  if (maxStreak >= 3) {
    recs.push(`存在连续 ${maxStreak} 次失败：检查环境问题或 flaky test，考虑 retry 机制与失败告警。`);
  } else if (failures / builds.length >= 0.2) {
    recs.push(`失败率偏高：建议先提升稳定性（超时保护、自动重试、失败通知）。`);
  }
  if (trend.startsWith("变慢")) {
    recs.push("构建耗时上升：排查依赖下载（加缓存/镜像仓库）、测试规模增长（并行拆分）、workspace 膨胀（cleanWs）。");
  }
  if (configXml) {
    if (!/<numToKeep>\d+<\/numToKeep>/.test(configXml)) {
      recs.push("未配置构建保留策略：建议 buildDiscarder(logRotator(numToKeepStr: '30'))，避免磁盘无限增长。");
    }
    if (has(/SCMTrigger/) && !has(/GitHubPushTrigger/)) {
      recs.push("使用 SCM 轮询触发：建议改用 Webhook，消除轮询延迟、降低负载。");
    }
    if (!has(/BuildTimeoutWrapper/)) {
      recs.push("未配置构建超时：建议 options { timeout(time: 30, unit: 'MINUTES') }，防止挂死占用 executor。");
    }
    if (!has(/TimestamperBuildWrapper/)) {
      recs.push("未启用时间戳：建议 options { timestamps() }，便于定位慢步骤。");
    }
    if (concurrent) {
      recs.push("已开启并发构建：确认无资源冲突；不需要时建议 disableConcurrentBuilds()。");
    }
  } else {
    recs.push("无法读取 config.xml（权限不足？），配置层面分析已跳过。");
  }
  if (!recs.length) { recs.push("未发现明显优化点，该 job 状态健康。"); }

  lines.push("## 优化建议", "");
  recs.forEach((r, i) => lines.push(`${i + 1}. ${r}`));
  return lines.join("\n");
}

/* ------------------------------------------------------------------ */
/* Job 树视图                                                            */
/* ------------------------------------------------------------------ */

class JobItem extends vscode.TreeItem {
  constructor(public readonly job: JenkinsJob) {
    super(job.name, vscode.TreeItemCollapsibleState.None);
    const lb = job.lastBuild;
    this.description = lb ? `#${lb.number} ${lb.result ?? "构建中"}` : "无构建记录";
    this.tooltip = `${job.name}\n${job.url}`;
    this.iconPath = new vscode.ThemeIcon(iconForColor(job.color));
    this.contextValue = "job";
  }
}

function iconForColor(color: string): string {
  const base = color.replace("_anime", "");
  switch (base) {
    case "blue": return "pass-filled";
    case "red": return "error";
    case "yellow": return "warning";
    case "aborted": case "disabled": return "circle-slash";
    default: return "circle-outline";
  }
}

class JobsProvider implements vscode.TreeDataProvider<JobItem> {
  private _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this._onDidChange.event;
  constructor(private getClient: () => JenkinsClient | undefined) {}
  refresh(): void { this._onDidChange.fire(); }
  getTreeItem(e: JobItem): vscode.TreeItem { return e; }
  async getChildren(): Promise<JobItem[]> {
    const client = this.getClient();
    if (!client) { return []; }
    try {
      return (await client.listJobs()).map((j) => new JobItem(j));
    } catch (err) {
      vscode.window.showErrorMessage(`拉取 Jenkins job 失败: ${err}`);
      return [];
    }
  }
}

/* ------------------------------------------------------------------ */
/* 扩展入口                                                              */
/* ------------------------------------------------------------------ */

export function activate(context: vscode.ExtensionContext): void {
  let client: JenkinsClient | undefined;

  async function loadAuth(): Promise<JenkinsAuth | undefined> {
    const url = await context.secrets.get("jenkins.url");
    const user = await context.secrets.get("jenkins.user");
    const token = await context.secrets.get("jenkins.token");
    if (!url || !user || !token) { return undefined; }
    return { url: url.replace(/\/$/, ""), user, token };
  }

  async function ensureClient(): Promise<JenkinsClient | undefined> {
    if (!client) {
      const auth = await loadAuth();
      if (auth) { client = new JenkinsClient(auth); }
      else {
        const pick = await vscode.window.showInformationMessage(
          "尚未配置 Jenkins 连接", "立即配置");
        if (pick) { await vscode.commands.executeCommand("jenkins.configure"); }
      }
    }
    return client;
  }

  const provider = new JobsProvider(() => client);
  vscode.window.registerTreeDataProvider("jenkinsJobs", provider);

  context.subscriptions.push(
    vscode.commands.registerCommand("jenkins.configure", async () => {
      const url = await vscode.window.showInputBox({
        prompt: "Jenkins 地址", placeHolder: "https://jenkins.example.com", ignoreFocusOut: true });
      if (!url) { return; }
      const user = await vscode.window.showInputBox({ prompt: "用户名", ignoreFocusOut: true });
      if (!user) { return; }
      const token = await vscode.window.showInputBox({
        prompt: "API Token（Jenkins → 用户 → Configure → API Token）",
        password: true, ignoreFocusOut: true });
      if (!token) { return; }
      await context.secrets.store("jenkins.url", url.replace(/\/$/, ""));
      await context.secrets.store("jenkins.user", user);
      await context.secrets.store("jenkins.token", token);
      client = new JenkinsClient({ url: url.replace(/\/$/, ""), user, token });
      vscode.window.showInformationMessage("Jenkins 连接已保存（存储于 VS Code SecretStorage）");
      provider.refresh();
    }),

    vscode.commands.registerCommand("jenkins.refreshJobs", async () => {
      await ensureClient();
      provider.refresh();
    }),

    vscode.commands.registerCommand("jenkins.triggerJob", async (item?: JobItem) => {
      const c = await ensureClient();
      if (!c) { return; }
      const job = item?.job ?? await pickJob(c);
      if (!job) { return; }

      // 参数化构建则询问参数
      let params: Record<string, string> = {};
      try {
        const info = await c.jobInfo(job.name);
        const defs = (info.property ?? []).flatMap((p: any) => p.parameterDefinitions ?? []);
        if (defs.length) {
          const input = await vscode.window.showInputBox({
            prompt: `参数化构建，请输入参数（KEY=VALUE，空格分隔）。可用参数：${defs.map((d: any) => d.name).join(", ")}`,
            placeHolder: "BRANCH=main ENV=staging", ignoreFocusOut: true });
          if (input === undefined) { return; }
          for (const kv of input.split(/\s+/).filter(Boolean)) {
            const idx = kv.indexOf("=");
            if (idx > 0) { params[kv.slice(0, idx)] = kv.slice(idx + 1); }
          }
        }
      } catch { /* 拿不到参数定义则按无参触发 */ }

      const confirm = await vscode.window.showWarningMessage(
        `确认触发构建「${job.name}」${Object.keys(params).length ? "，参数 " + JSON.stringify(params) : ""}？`,
        { modal: true }, "触发");
      if (confirm !== "触发") { return; }

      try {
        const queueUrl = await c.trigger(job.name, params);
        vscode.window.showInformationMessage(
          `已触发「${job.name}」${queueUrl ? "，队列: " + queueUrl : ""}`);
        setTimeout(() => provider.refresh(), 3000);
      } catch (err) {
        vscode.window.showErrorMessage(`触发失败: ${err}`);
      }
    }),

    vscode.commands.registerCommand("jenkins.analyzeJob", async (item?: JobItem) => {
      const c = await ensureClient();
      if (!c) { return; }
      const job = item?.job ?? await pickJob(c);
      if (!job) { return; }
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `正在分析 ${job.name}...` },
        async () => {
          try {
            const [builds, configXml, info] = await Promise.all([
              c.recentBuilds(job.name, 30),
              c.configXml(job.name),
              c.jobInfo(job.name),
            ]);
            const md = analyze(job.name, builds, configXml, !!info.concurrentBuild);
            const doc = await vscode.workspace.openTextDocument({
              content: md, language: "markdown" });
            await vscode.window.showTextDocument(doc, { preview: false });
          } catch (err) {
            vscode.window.showErrorMessage(`分析失败: ${err}`);
          }
        });
    }),

    vscode.commands.registerCommand("jenkins.showJobInfo", async (item?: JobItem) => {
      const c = await ensureClient();
      if (!c) { return; }
      const job = item?.job ?? await pickJob(c);
      if (!job) { return; }
      try {
        const info = await c.jobInfo(job.name);
        const params = (info.property ?? []).flatMap((p: any) => p.parameterDefinitions ?? []);
        const md = [
          `# ${info.name}`, "",
          `- 地址：${info.url}`,
          `- 描述：${(info.description || "无").trim()}`,
          `- 并发构建：${info.concurrentBuild ? "允许" : "不允许"}`,
          `- 健康度：${(info.healthReport ?? []).map((h: any) => `${h.description} (${h.score}分)`).join("；") || "无"}`,
          `- 最近构建：#${info.lastBuild?.number ?? "-"}（${info.lastBuild?.result ?? "-"}）`,
          `- 最近成功：#${info.lastSuccessfulBuild?.number ?? "-"}　最近失败：#${info.lastFailedBuild?.number ?? "-"}`,
          params.length
            ? `\n## 构建参数\n${params.map((p: any) => `- \`${p.name}\`（${p.type}）默认：${p.defaultParameterValue?.value ?? "无"} — ${p.description ?? ""}`).join("\n")}`
            : "\n（非参数化构建）",
        ].join("\n");
        const doc = await vscode.workspace.openTextDocument({ content: md, language: "markdown" });
        await vscode.window.showTextDocument(doc, { preview: false });
      } catch (err) {
        vscode.window.showErrorMessage(`获取详情失败: ${err}`);
      }
    }),

    vscode.commands.registerCommand("jenkins.openInBrowser", (item: JobItem) => {
      if (item?.job.url) { vscode.env.openExternal(vscode.Uri.parse(item.job.url)); }
    })
  );

  ensureClient().then(() => provider.refresh());
}

async function pickJob(c: JenkinsClient): Promise<JenkinsJob | undefined> {
  const jobs = await c.listJobs();
  const picked = await vscode.window.showQuickPick(
    jobs.map((j) => ({ label: j.name, job: j })),
    { placeHolder: "选择 Jenkins Job" });
  return picked?.job;
}

export function deactivate(): void {}
