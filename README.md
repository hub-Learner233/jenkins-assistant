# Jenkins Copilot Agent

在 VS Code 中通过 GitHub Copilot 管理 Jenkins：**拉取 job 信息、触发构建、分析优化方向**。

本套件包含三部分：

| 组件 | 路径 | 作用 |
| --- | --- | --- |
| Copilot 自定义 Agent | `.github/agents/jenkins-assistant.agent.md` | 在 Copilot Chat 中以 Jenkins 专家身份对话，自动调用脚本完成操作 |
| Jenkins CLI 脚本 | `scripts/jenkins_client.py` | 零依赖 Python 脚本，直连 Jenkins REST API（Agent 和命令行共用） |
| VS Code 扩展 | `vscode-extension/` | 侧边栏 job 列表 + 一键触发/分析/查看详情，凭据存于 SecretStorage |

---

## 一、准备认证信息

Jenkins → 右上角用户名 → **Configure → API Token → Add new Token**，拿到三样东西：

- `JENKINS_URL`：如 `https://jenkins.example.com`
- `JENKINS_USER`：你的用户名
- `JENKINS_TOKEN`：刚生成的 API Token

## 二、使用 Copilot 自定义 Agent（推荐）

> 需要 VS Code 1.102+（或 Insiders）且开启 Copilot Chat 的自定义 Agent 功能。

1. 本仓库已包含 `.github/agents/jenkins-assistant.agent.md` 和 `scripts/jenkins_client.py`，clone 后在 VS Code 打开即可。
2. 在终端设置环境变量：
   ```bash
   export JENKINS_URL="https://your-jenkins.example.com"
   export JENKINS_USER="your-username"
   export JENKINS_TOKEN="your-api-token"
   ```
3. 打开 Copilot Chat，在 Agent 选择器中切换到 **「Jenkins 助手」**，然后直接对话：
   - 「列出所有 job，哪些最近失败了？」
   - 「看一下 deploy-prod 这个 job 的详情」
   - 「触发 release 构建，分支用 main，环境 staging」
   - 「分析一下 nightly-test 最近 30 次构建，有没有优化方向？」

Agent 会自动调用脚本拉取真实数据，并结合统计结果给出可落地的优化建议（缓存、并行化、
日志轮转、Webhook 触发、超时保护等）。**触发构建前 Agent 会先向你确认**。

## 三、使用 VS Code 扩展（图形界面）

```bash
cd vscode-extension
npm install
npm run compile
```

按 `F5` 启动 Extension Development Host 调试，或打包安装：

```bash
npx @vscode/vsce package   # 生成 .vsix
code --install-extension jenkins-assistant-0.1.0.vsix
```

功能：

- 活动栏新增 **Jenkins** 图标，侧边栏展示全部 job 及最近构建状态（成功/失败/不稳定图标）
- 首次使用执行命令 **「Jenkins: 配置连接」**（Ctrl+Shift+P），凭据加密存于 VS Code SecretStorage
- 右键 job：**触发构建**（参数化构建会提示输入参数，触发前弹确认框）、**分析优化建议**
  （以 Markdown 报告打开）、**查看详情**、**在浏览器中打开**

## 四、纯命令行使用

```bash
python3 scripts/jenkins_client.py list                        # 全部 job 总览
python3 scripts/jenkins_client.py info my-job                 # job 详情（健康度、参数）
python3 scripts/jenkins_client.py trigger my-job \
    --param BRANCH=main --param ENV=staging --wait            # 触发并等待结果
python3 scripts/jenkins_client.py analyze my-job --builds 30  # 分析优化建议
python3 scripts/jenkins_client.py --json list                 # JSON 输出（供程序消费）
```

Folder 下的 job 用路径形式：`folder/sub/my-job`。

## 分析维度说明

`analyze` 会从最近 N 次构建 + `config.xml` 中提取信号并生成建议：

- **稳定性**：成功率/失败率/不稳定率、最长连续失败 → 重试机制、flaky test 隔离
- **性能**：平均/P95 耗时、最近 1/3 vs 最旧 1/3 耗时趋势 → 依赖缓存、测试并行拆分、workspace 清理
- **配置**：构建保留策略、SCM 轮询 vs Webhook、超时/时间戳插件、并发构建、节点 Label、产物归档

## 常见问题

- **401/403**：检查用户名与 API Token，确认账号有对应 job 的读/构建权限。
- **触发返回 403 且提示 crumb**：脚本与扩展均已自动处理 CSRF crumb；若仍失败，检查
  Jenkins 是否开了反向代理并丢弃了 crumb 头。
- **公司 Jenkins 走代理**：在终端设置 `HTTPS_PROXY` 后再使用脚本。
