# 财务政策与资金工作目录

这里用于财务查税费优惠、奖补兑现、人才用工稳岗、融资增信贴息；业务资质认定技术条件由业务负责。每天先真实查阅、再机器整理，人员主动浏览并复核后决定是否参与。

## 人员先看什么

早间09:00自动检查准备服务网入口与最多2—3条重点财务政策分析。直接打开[服务网找政策](https://www.chinasme.cn/policy)，与解析同时看；自选约2条细看，抽查未推荐项，缺证据先补核。操作与反馈方式见 `人工每日审核说明.txt`。

每次保留每日8列逐条台账、长期10列财务台账、人工复核池和备查明细/日志。每日主表按日期加P编号一条政策一行，同日重复核查合并；长期主表跟关键条件、资料、是否参与和办理进展。日志记录真实范围与缺口，无新也留日志，不伪造一条政策充数。具体文件与程序接口见完整技能的 `references/finance-data-contracts.md`。

发现地区可据已确认经营/办公所在地、注册地或用户指定目标地区选来源。已知昆明即可查云南昆明候选，注册、纳税和用工归属未核不妨碍发现；适用条件仍待核，不能据所在地认已符合。没有任何地区事实或用户目标的新模板不默认云南。

- `exports/每日检索政策台账.xlsx`：每日8列逐条主表，并带“资料明细”。检索日期、编号及政策名称、检索词/地区、能带来什么、要核实什么、办理时间、资料与用途、人工意见及下一步。
- `exports/财务政策资金台账.xlsx`：长期10列主表，并带“资料明细”。编号及政策名称、政策类别、能带来什么、要核实/缺什么资料、办理时间、资料与用途、是否参与、办理进展、下一步/负责人、最近跟进。
- 同名CSV、`exports/财务政策资金明细.csv`、`exports/每日检索运行日志.csv`：程序处理及备查，含完整财务字段、原日汇总、查询观察计数、失败缺口和次日关键词。
- `exports/政策人工复核池.csv`、`exports/每日人工复核包.md`：全部实际筛选去向及人工抽查入口。
- `data/finance_workflow.sqlite`：逐轮日志和真实人员财务反馈；不直接手改数据库或用CSV覆盖状态。

## 三类判断分开

R编号审机器筛选，选择恢复候选／维持暂不推荐／先补证据／仅作储备。P编号决定参与／先补资料／不参与。财务审核与办理进度另记财务条件、补料部门和期限、兑现/税费享受及凭证。机器推荐、页面已开和可推进准备都不等于人员决定参与。

资金到账或税费已享受要有人员确认、实际日期或期间及凭证/回执和归档位置，金额有证据才记；分期未完、年度持续和后续义务继续跟。没有材料保持待核，不据未知认不适用。人员按编号在Codex回复，由系统回写后重导；直接改Excel或CSV不会自动更新系统。

表格与解析使用通俗短标题、一句用途、最多3项关键待核。政策执行期和企业办理截止分开写，未核到时间就写待核；原文与完整条件留在链接和明细里。

原文标题或条款较长时，Codex可按财务数据契约将通俗显示说明保存到本机data/finance_display_notes.json，绑定真实内部编号、当前来源版本和实际说明时间；这只是机器说明，不改原文或人员决定，也不靠改写文字制造新机会。

Excel主表将“资料下载”显示为“资料与用途”，点击后在本工作簿内跳到“资料明细”的对应P号，不打开本地相对文件夹。明细每份材料一行，给真实官方HTTPS正文或具体附件链接，并说明用途、已取得状态和缺口。无独立表样不设下载按钮；HTML正文用来读政策，旧指南仅历史参考，银行财政表不是企业申请表。单独转发Excel即可保留说明和官方入口，本地副本可另附；CSV表头与主表8/10列数量不变。

## 时间和边界

每天北京时间09:00与16:30检查。早间每天提供一次页面与重点解析；下午仅在新机会、重要变化或需处理时通知，无变化安静。本地日志每轮照记；完整日报时间由团队另定，未自行建立。

`monitor.py`、`human_review.py`、`finance_workflow.py`不联网采集。Codex先实际查本轮网页再保存输入，单纯导出和重跑旧JSON不能称为查新。人员完成只根据真实回复留痕，不自动申报、纳税、申请贷款、外联或付款。

企业画像、财务报表、纳税资料、社保工资、银行回单和凭证只保存在本机私有目录，不能提交公开Git。每人记录独立，不承诺跨人同步；汇总带原文/文号/办理期间，不只用本机P/R编号。

## 给Codex的维护提示

完整包根的 `runtime/bootstrap.py` 负责本地自检/升级，不假设bootstrap已复制到工作目录。程序运行接口按本机 `--help` 与完整技能参考文件执行，所有相对路径以本工作目录为根。升级保留画像、数据库、财务状态、人员意见和材料，自定义规则发生冲突先比较新副本；升级不等于重新查网站。

从完整包根初始化用 `PYTHON runtime/bootstrap.py init --workspace <本目录绝对路径> --edition finance --scope policy --operator <人员标识>`。使用通用企业画像ZIP导入后，先运行 `PYTHON runtime/bootstrap.py configure-finance --workspace <本目录绝对路径>` 补回财务配置，再用 `PYTHON tools/profile_bundle.py doctor --workspace <本目录绝对路径>` 核证据SHA，最后用 `PYTHON runtime/bootstrap.py doctor --workspace <本目录绝对路径>` 本地检查。PYTHON是本机实际解释器；configure-finance保留企业事实并备份，不能据此宣称财务资料已齐。

先看 `日常监测与人工判断规则.txt`、`profile.json`、`sources.json`、现有台账/日志/未审及回执，再按手动或定时提示执行。材料必须有官方链接、真实下载状态和实际文件/哈希。网站登录与首次定时触发在本机分别验收，不把本地计划写成任务已运行。

## 每轮本地处理顺序

先实际读网页并保存本轮候选及筛选JSON。其run_id/checked_at一致，kind与screening.scope均为policy；覆盖失败也保存，示例不能作为真实检索。覆盖输出前归档旧exports和回执。候选finance对象使用类别、适用期间、财务条件/证据/缺口、拨付方式、重复享受/涉税事项和不同办理节点；字段约定见完整技能参考文件。

```text
PYTHON monitor.py run --input incoming/<run_id>.json --profile profile.json --db data/monitor.sqlite --out exports
PYTHON human_review.py ingest --input incoming/<run_id>_screening.json --db data/human_review.sqlite
PYTHON human_review.py export --db data/human_review.sqlite --out exports
PYTHON monitor.py export --profile profile.json --db data/monitor.sqlite --out exports
PYTHON finance_workflow.py log --workspace "{{WORKSPACE}}" --input incoming/<run_id>.json --screening incoming/<run_id>_screening.json
PYTHON finance_workflow.py export --workspace "{{WORKSPACE}}"
```

PYTHON替换为本机解释器，命令在本目录运行。P映射到内部编号后给R观察补准确关联，必要时再重导展示；重导不算新查。最后两条分别登记已有真实轮次与重导视图，不能冒充程序联网。最后的export会通过本目录的finance_workbooks.py自动生成两本Excel，不用另装表格库或复制作者脚本；打开程序返回的实际路径，文件占用时可能另存为“_新版.xlsx”。

只有人员明确反馈后，才用 `finance_workflow.py review --workspace <绝对目录> --id <本机P号或内部编号> --state <财务状态> --reviewer <真实人员> --note <实际原话>`。状态为待财务审核、可推进准备、待补齐、不适用、历史参考、已到账、已享受；不自动改参与。待补齐再加真实 --material-gap/--department/--due；已到账要 --date/--voucher/--evidence，已享受另要 --period。仅人员明确作参与决定时附 --participation。参数和归档文件验证以 `--help` 及技能数据契约为准，不能在定时采集阶段调用review填满状态。
