# 采集和人员回写接口

执行时把工作目录设为安装后的 workspace，运行该目录中的脚本；不要运行插件缓存中的核心脚本向缓存写台账。命令中的 `PYTHON` 表示本机实际 Python 路径，参数值由实际文件替换。

## 每轮真实采集

先读取现有画像、来源、台账、待核与通知回执，真实访问本轮范围的网站。保存独立 `run_id`、带时区 `checked_at`、查询/地区/分页、原始证据位置。覆盖状态为 `success` / `partial` / `failed` / `auth_required`；成功只表示所记范围完成，不等于全网。

采集 JSON 结构参照安装包的 `runtime/examples/incoming.example.json`，或初始化后保留的示例位置；仅参照字段，不导入其演示内容。正式候选需要 `kind`、`source_id`、`source_url`、`title`，可记录 `original_url`、`original_verified`、`issuer`、`document_number/project_id`、`lot`、`round`、`stage`、`published_at`、`deadline_at`、`deadlines`、`summary`、`hard_conditions`、`evidence_path`。同一公告有多个可申报路径/软件标包时逐一留去向；项目号、标段、轮次不要混合。`stage` 取 open/upcoming/change/terminated/awarded/historical/unknown；指南征集、名单公示不能写成开放申报。

硬性条件格式是 `{"label":"条件","status":"unknown","evidence":"未取得企业证明"}`，状态只取 pass/fail/unknown。只有正式条款和企业事实能共同支持时才认定不符；缺证据不能写 fail。日期原文只有日期时不要编出办理时刻；企业提交、部门推荐、取文件及响应分别保存。`original_verified=true` 仅在正式原文实际已读时使用。

筛选 JSON 参照 `runtime/examples/screening.example.json`。保存 `run_id`、`checked_at`、`scope`、`coverage`、`observations`。每条 observation 需 `kind`、`source_id`、`list_url`、`title`、`machine_bucket`、`machine_reason`、`reason_code`、`reading_level`。其中：

- `machine_bucket`：推荐 / 待核 / 暂不推荐 / 历史信息。
- `reason_code`：业务 / 地区 / 主体 / 期限 / 公告性质 / 重复 / 未知 / 相关 / 其他。
- `reading_level`：标题 / 摘要 / 平台详情 / 原文。`original_verified=true` 还要求真实 `original_url` 且 reading_level=原文。
- 可补 `detail_url`、`platform_url`、`original_url`、`published_at`、`page`、`query`、`evidence_path`、`opportunity_id`、`issuer`、`document_number/project_id`、`lot`、`round`、`deadline_at`、`condition_evidence`、正文或有效条款的 `source_content_sha256`。

共享列表 URL 不能冒充每条公告详情。低层级标题观察保留为观察，不覆盖已有正式原文事实；出现明确新的截止/标题/阶段冲突时记待复核线索并补读正式原文。不为解读润色重复改机器理由、制造版本。

覆盖前先归档上一轮 exports、回执及必要核验快照，范围限本工作目录内。随后按顺序运行：

```text
PYTHON monitor.py run --input incoming/<run_id>.json --profile profile.json --db data/monitor.sqlite --out exports
PYTHON human_review.py ingest --input incoming/<run_id>_screening.json --db data/human_review.sqlite
PYTHON human_review.py export --db data/human_review.sqlite --out exports
PYTHON monitor.py export --profile profile.json --db data/monitor.sqlite --out exports
```

主台账的 P/T 由程序稳定递增，筛选池 R 独立编号；读取 `data/display_ids.json` 后为有关筛选项补准确 P/T 关联，再导出，不能猜编号。最后一次 export 是关联展示刷新，不是网站新查。无正式候选也可保存空 items 与真实 coverage；无观察用空 observations，不能伪造数量。

材料保存到 `materials/<内部记录编号>/`，`materials/materials_manifest.json` 记录名称、官方 URL、本地相对路径、状态、下载时间和 SHA256。必须实际取得且验文件/哈希后标已下载；需登录、购标、取不到附件如实记。展示用的 `data/card_notes.json` 带 source_version；原文变更后重核展示内容。运行和通知回执与人审事件分离。首次写这些 JSON 前读 [状态与材料格式](state-files.md)，尤其材料必须使用 attachments 数组和 item_id 内部键，否则导出无法关联。

## 人员真实回复后

只在有清楚对应事项的人员回复时执行以下接口。保留实际审核人、原话、依据和时间；模糊的“看过了/可以”不批量写参与。

```text
PYTHON human_review.py review --db data/human_review.sqlite --id R实际编号 --reviewer 实际姓名 --outcome 先补证据 --note 实际原话及依据 --evidence-url 实际核读链接
PYTHON human_review.py independent --db data/human_review.sqlite --kind policy --reviewer 实际姓名 --url 实际链接 --title 实际标题 --note 实际判断 --read-at 实际ISO时间 --reading-level 平台详情
```

`review` 的 outcome 为恢复候选 / 维持暂不推荐 / 先补证据 / 仅作储备，不更改参与状态。恢复后补核正式原文和附件，建立或恢复 P/T，仍待人员决定。`independent` 仅记录人员实际自主阅读，平台页不能记原文。机器重读、打开标签或推送都不能调用这些接口制造人工完成。

参与决定使用：

```text
PYTHON monitor.py review --db data/monitor.sqlite --id <内部记录编号> --state 先补资料 --note <真实姓名、原话与依据>
```

此 CLI 的 `--id` 要用台账“内部记录编号”或 display_ids 反查到的内部值，不能直接把 P/T 显示号当内部键。之后重新导出。参与不等于报名/提交；没有凭证不写完成。已参与事项继续跟受理、补正、评审、结果、兑现，窗口截止不自动结束。

## 报告与去重

新相关机会、实质条件/附件/期限变化、新 7/3/1 日临期节点或需人员处理的障碍可报告。维护 `data/notification_receipts.json`，只在实际报告后记已送达；回执不限制人员主动查看任何已存项。普通未推荐进复核池，不逐条推送。

包含政策的早间每日开场是独立约定：以北京时间日期与 `policy_morning_review` 在 `review_opening_receipts` 去重，保存真实页面准备结果、展示编号和交付时间。已报旧项标“今日待核/复看”，不冒称新发现。下午无实质变化安静。若由 heartbeat 触发，按宿主要求输出 NOTIFY/DONT_NOTIFY；日常开场尚未交付时可通知，重复触发不再通知。
