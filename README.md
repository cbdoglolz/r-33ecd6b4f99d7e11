# FongMi 四源订阅聚合（V1）

把 `clun / wang / qist / pray` 四份订阅中的站点配置汇总为一条订阅。保留来源、请求参数和各站解析器，不运行来源代码，不下载影片、不代理影片、不记录观看历史。V1 不提供统一影片目录、统一分类、跨站索引或合并播放线路。

仓库：<https://github.com/cbdoglolz/fongmi-tvbox-aggregator>。

**当前为私有仓库。2026-11-04 实测创建 Pages 返回 HTTP 422：当前套餐不支持此私有仓库的 Pages。Pages 暂未上线，没有可填写的正式订阅 URL。未经用户确认，不会把仓库改为公开。**

## 自动更新

工作流 [`Aggregate and deploy`](https://github.com/cbdoglolz/fongmi-tvbox-aggregator/actions/workflows/update.yml) 每天 UTC `02:17 / 14:17`（北京时间 `10:17 / 22:17`）检查四源，也可在 Actions → Aggregate and deploy → Run workflow 手动运行。日常无需手动更新。修改构建代码、来源或测试后会自动构建；PR 只执行离线测试，没有发布权限。

只有完整读取四源且全部校验通过，才生成新的成功版本。任意一源失败、空站点、非法路径、数量比最近成功版本下降超过 30% 都阻止自动更新，保持原 Pages 和成功快照。解析器和请求头等行为相同才能去重；只同名、同 key、同域名不足以去重。所有输出 key 都以来源 ID 开头，并包含行为指纹，重复 key 不会相互覆盖。相同输入生成相同 key；行为字段/来源全局上下文变化会改变指纹，相关收藏可能需重新选择。

检查成功但内容无变化时，只记录 Actions 检查结果，不提交快照、不重发 Pages、不伪造活动。网页中的最近成功时间是**内容生成时间**；最近检查是否成功、距离上次检查多久，须看 Actions 的真实运行时间。程序版本变化也会生成新版本。可以从 Actions Summary 中看到内容是否发生变化。

GitHub 的 schedule 不保证准点：高负载时可能延迟或跳过，只从默认分支运行，公开仓库长时间无活动（当前文档为 60 天）可能自动停用。不要把它当 100% 永久在线服务。请关注 [Actions 页面](https://github.com/cbdoglolz/fongmi-tvbox-aggregator/actions)，在 GitHub 个人 Settings → Notifications 中启用 Actions 失败通知；这里没有额外创建 Issue 或发送邮件。建议关注最近成功检查是否超过 36 小时；停用后按 GitHub 界面的正常流程重新启用，禁止无意义机器人提交。

## 产物与报告

构建产物在 `public/`：

| 文件 | 用途 |
| --- | --- |
| `content.json` | 默认订阅；仅隐藏明确的推送、文件管理、搜索辅助和直播工具入口 |
| `tvbox.json` | 全部保留站点，含不同 JAR/引擎的备用实现 |
| `report.json` | 来源抓取状态、实际计数、去重映射、重名、依赖、HTTP 引用、版本和最近成功生成时间 |
| `report.html` / `index.html` | 可读的简体中文检查报告 |
| `manifest.json` | 每个最终 key 对应的来源、API、JAR、引擎和原始 key |

Pages 未启用时，在成功 Actions 运行的 Artifacts 下载 `complete-运行ID`，包含 `public/` 和 `.snapshots/版本/`。长期副本在独立 [`snapshots` 分支](https://github.com/cbdoglolz/fongmi-tvbox-aggregator/tree/snapshots)；每个成功版本都是不可变 Git 提交，其 `public/` 可审查，`raw/` 保留四源原始配置及 SHA-256。原始配置快照不放进 Pages artifact。Actions 完整 bundle 保留 90 天，失败诊断保留 30 天；过期后仍可从 snapshots 的 Git 历史恢复，只要不删除该分支/仓库历史。请勿将 snapshots 当默认分支或手工推送生成文件触发其他发布流程。

报告中的依赖状态明确为“仅记录 URL，未下载、未执行、未审计代码”。`md5` 声明只检查格式，不冒充已验证二进制哈希。`http_references` 记录所有可识别的明文地址（包括嵌入参数），`local_references` 标出站点自身指向本机/内网的行为配置；构建不会主动访问这些地址。

## 本地运行与检查

固定 CI Ubuntu `24.04` + Python `3.12.16`（已核对官方构建清单），无第三方 Python 运行依赖，无需 pip 安装。Actions 的依赖均固定到完整提交 SHA。固定 runner 避免 ubuntu-latest 升级后旧解释器没有匹配构建。

```powershell
python -m unittest discover -s tests -v
python -m compileall -q build.py scripts tests
python build.py
python build.py --validate public
```

`python build.py --last-good public` 可对比当前完整产物。程序先在临时目录生成、校验，再替换 public；失败不会用残缺站点覆盖。构建失败的 `failure.json` 只保存受限错误信息，不输出响应体、Cookie 或 GitHub Token。

可选 `python scripts/check_dependency_urls.py` 仅对所有直接 JAR URL、每源代表性的 JS/Python URL 发 HEAD，不请求文件体。输出 `diagnostics/dependency-url-checks.json`。HTTP 200 不是代码安全或可运行的证明；特别是文件 URL 返回 `text/html` 时，可能是错误页或防护页面，需标记为异常。重定向签名查询串不写入报告。`scripts/verify_pages.py` 只在 Pages 实际启用后检查真实 URL 的 HTTP 200、Content-Type、JSON、完整性及版本；未部署时不生成成功凭证。

## 合并规则与边界

- 严格 UTF-8/BOM 或 GB18030 解析；连接/读取超时 15 秒、有限两次尝试、单配置上限 4 MiB，读取总时限 30 秒；多仓最大深度 3、最大配置数 24，去环且去重复引用。
- 配置抓取拒绝内网/本机、带凭据 URL、HTTPS 降级重定向；HTTPS 使用系统证书校验。配置只是 JSON 数据，绝不执行其中的 JAR、JS、Python 或 shell。
- `type=3` 站点的 JAR 缺省值继承所属来源的 `spider`，每站显式设置 `jar`；站点已有 JAR 保持优先。Python/JS 的运行容器也来自所属来源，不会混用另一来源的主 JAR。
- 只转换已识别字段中的完整文件路径、已知嵌套文件字段以及 `$$$` 分隔的独立文件参数。标签、正则、分类、内嵌字符串、URL 查询顺序不会被全局替换。校验后缀 `;md5;32位哈希` 保持原样。以本次真实 `used_url`（包括 clun 备用源）为路径基准。
- 去重比较除 name/key 外的所有字段，并额外比较原来源的解析/网络全局上下文。保守多留也不误删；字段缺省和显式默认值视为不同。报告有每项“删除 → 保留”映射，不使用历史数量作为目标。
- 全局 `hosts/proxy/doh/rules/ijk/parses/flags/lives/wallpaper` 等不叠加；报告列出忽略字段和理由。需要这些原全局规则的站点仍可能存在播放兼容性问题，不能仅凭配置校验宣称可播。
- 不改变 `searchable=0`。工具隐藏规则见 `TOOLS` / `TOOL_KEYS`，只影响 `content.json`；被隐藏的项目仍保留于全量订阅和报告。内容优先不表示安全审计，也不表示全站内容符合任何年龄或版权要求。

## 新增订阅及上游失效

V1 默认只使用任务书指定四源。如之后明确要增加来源，在 `sources.json` 中新增唯一 `id`、`url` 和可选 `fallbacks`，补充对应结构和路径测试后提交；ID 一旦使用不要随意改动，以免改变站点 key。新增来源同样必须成功，不能当成可有可无的补充。

上游失败时先看 Actions → 对应运行 → `failure-运行ID` 和 Build 日志，检查来源 HTTP 状态、超时、编码或数量变化。恢复上游/修正 sources 后手动 Run workflow；不要删掉失败来源来“让构建通过”。clun 原站失败时可以使用任务书指定的 GitHub 备用源，报告会明确记录 fallback 和本次实际地址，不宣称与原站一致。首次构建若缺源且没有 last-good，则不生成可发布订阅。

## Pages 启用与公开边界

GitHub Pages 对 Free 账号的私有仓库不可用，支持私有仓库 Pages 的套餐见 [GitHub 官方说明](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site)。本项目实际 API 已返回套餐限制。继续上线需用户选择升级套餐保留私有代码，或明确同意把这个独立仓库及其快照历史设为公开。**代码私有也不代表 Pages 私有，发布 JSON 和站点列表会公开可读。**

还须遵守 [GitHub 可接受使用政策](https://docs.github.com/en/site-policy/acceptable-use-policies/github-sexually-obscene-content) 和版权/隐私要求。本工程处理配置，不托管影片、海报、私人影像；第三方站点及执行依赖未经内容或代码安全审计，不得宣称 GitHub 已审核许可。如果具体发布内容被平台政策限制，应停止公开发布，不通过隐藏 URL 或更换仓库规避。

具备相应权限和发布条件后：

1. Repository → Settings → Pages → Source 选择 GitHub Actions（或 `gh api --method POST repos/cbdoglolz/fongmi-tvbox-aggregator/pages -f build_type=workflow`）。
2. Settings → Secrets and variables → Actions → Variables 设置 `PAGES_ENABLED=true`。
3. Actions → Run workflow，`restore_snapshot` 填 `snapshots`，发布现存完整成功快照。这个字段也接受 snapshots 历史中明确的 40 位提交 SHA，用于回滚。留空则完整抓取四源。
4. 工作流直接上传 `public/` 为 Pages artifact，由 deploy-pages 发布；不会依赖机器人提交再次触发 push。
5. 以 deploy job 返回的真实 page_url 为准；等待 `Verify published JSON and version` 通过后才能把该地址下的 `content.json` 填入 FongMi。当前未启用，不把预计 URL 交付为可用链接。

## 回滚、恢复和告警

自动构建失败不执行部署，已经上线的 Pages 保留之前版本，不需要用残缺文件覆盖。`snapshots` 指向**最近成功构建**，不保证它曾成功部署；每次已部署版本以对应 deploy job 及验证 artifact 为准。

要恢复指定已知版本：在 snapshots 分支 History 选完整成功提交 SHA → Actions 手动运行 → `restore_snapshot` 填该 SHA。恢复模式不抓取上游，校验快照所有 public 文件哈希后，直接重新上传并发布；失败则不发布。恢复不会改写 snapshots 历史。初次启用 Pages 可用 `snapshots` 指向最新完整构建。恢复行为是明确的人工回滚，允许重新发布相同版本。

本地恢复示例（须先 `gh auth login`，不要在聊天/命令行粘贴明文 Token）：

```powershell
python scripts/snapshot.py recover --repo cbdoglolz/fongmi-tvbox-aggregator --directory .last-good/public
python build.py --validate .last-good/public
```

若需完整复现原始来源，可 `git fetch origin snapshots`，从选定快照提交读取 `raw/` 和 `snapshot.json`，而不是重新下载已经变化的上游。恢复接口异常（403/网络错误）不会被当成“首次无快照”。

## 未验收及 V2 待办

本次构建不执行第三方解析器，未验证电视设备的首页、分类、搜索、播放地址或影片播放。HEAD 和 JSON 校验通过都不等于实机测试。

V2 仅记录需求：虚拟总站、国产/欧美/日本/短剧/AI 分类映射、首页更新、统一搜索、同片多来源线路合并；需要不同爬虫运行环境和真实 `homeContent/homeVideoContent/categoryContent/searchContent/detailContent` 调用，静态配置合并无法完成，不在 V1 部署中冒充实现。

官方定时任务限制：[Events that trigger workflows — schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)；Pages 工作流：[Using custom workflows with GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。
