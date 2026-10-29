# 财务数据与每日交付

财务版新增 finance_workflow.py，仍不联网。主候选/版本在 data/monitor.sqlite，实际观察和人员筛选复核在 data/human_review.sqlite；财务检索日志和人员财务进度追加保存到 data/finance_workflow.sqlite。人员按P/R编号在Codex回复，由系统回写；直接修改Excel或CSV不会自动回写数据库，不用Git合并个人SQLite。

## 财务画像

profile.workflow=finance；finance_scope保留四类方向、资质边界、资金范围和官方核验要求。finance_profile按真实证据补充以下区块，未知用null/unknown而不是零或未取得：

| 区块 | 核对内容 |
| --- | --- |
| tax_jurisdiction | 纳税所属省市区、主管机关、有效期间及证据 |
| taxpayer_identity | 身份值、所属期、变化记录和证据 |
| annual_financial_metrics | 会计年度、营收、利润、金额单位、报表口径、审计/证据 |
| rd_expense_collection | 年度、研发金额、会计与扣除口径、辅助账、项目明细 |
| employment_and_social_insurance | 用工/社保期间、地区、人员条件及证明可得性 |
| qualification_records | 业务提供的真实资质与编号/期限，不代替技术审查 |
| benefits_received | 已确认的奖补或税惠、所属期、范围、真实日期、金额/凭证及重复享受依据 |
| current_materials | 申报表、报表凭证、银行回单、社保工资、发票合同和辅助账的期间/可得性/证据 |
| pending_material_coordination | 实际缺口、协同部门、期限与跟进 |

导入通用企业画像ZIP后，运行完整包的 `runtime/bootstrap.py configure-finance --workspace <绝对目录>`，再做证据doctor及bootstrap doctor。转换仅补配置并保留事实，不把旧证书状态改成今天已官方复核。

地区字段分用途：已确认经营/办公所在地、注册地或用户指定目标地区可用于启用省市发现来源；注册、纳税、用工或项目归属用于具体适用审查。已知昆明经营/办公时可查云南昆明候选，即使纳税/注册归属仍未知；未知保持待核，不停止发现或自动认符合。全无地区事实或目标时不默认云南。

## 每条候选的财务扩展

候选仍按通用接口入monitor，在item里加finance对象。只有category有枚举限制；其余字段按实际内容使用文本、列表或对象，不编金额或财务数值。

```json
{
  "finance": {
    "category": "待核分类",
    "applicable_period": null,
    "support_type": null,
    "benefit_description": null,
    "payment_mode": null,
    "qualification_reward_link": null,
    "financial_requirements": [],
    "company_evidence": [],
    "material_gaps": [],
    "coordination_department": null,
    "supplement_deadline": null,
    "nonduplication": null,
    "tax_notes": null,
    "application_deadline": null,
    "local_acceptance_deadline": null,
    "recommendation_deadline": null
  }
}
```

category取税费优惠／奖补兑现／人才用工稳岗／融资增信贴息／待核分类。支持内容和拨付方式分开；财务条件与企业证据分开，资料缺口不是已完成补料。时间字段同时在通用deadlines保存适用办理节点，不把部门推荐截止当企业截止。

每轮在候选输入顶层用 `next_keywords_suggested` 文本数组保存系统对次日检索词的建议，依据实际覆盖缺口和人员反馈；确无建议可空数组并注明待确认，不凑词。人员真实确认通过review的next-keyword记录。不要把系统建议标成人员已确认。

## 每轮日志与导出

先按 [通用接口](data-contracts.md) 将同一真实轮次导入monitor及human_review，然后在财务工作目录运行：

```text
PYTHON finance_workflow.py log --workspace <本目录绝对路径> --input incoming/<run_id>.json --screening incoming/<run_id>_screening.json
PYTHON finance_workflow.py export --workspace <本目录绝对路径>
```

log要求两个输入同run_id、同checked_at，screening.scope=policy，且核心库已有完全对应的轮次。coverage不能为空，包含真实source_id/status、查询、地区和缺口；无候选/无观察可用空数组，失败也记录实际范围。演示example_only输入拒绝作为真实检索日志。相同run_id同内容幂等，不同内容不能改写历史；export只重导视图，不新增扫描或人审事件。

`finance_workflow.py export` 自动产生两份供人员阅读的Excel，各含主表和“资料明细”页：

- exports/每日检索政策台账.xlsx：8列，按北京时间日期和P编号一条政策一行，同日重复核查去重。
- exports/财务政策资金台账.xlsx：10列，一项政策一行，便于判断是否参与和跟踪办理。

Excel由随安装复制的 `finance_workbooks.py` 生成，只需Python标准库；不依赖作者本机的表格生成脚本或附件目录。导出返回实际文件路径；表被WPS/Excel占用时保留原文件，另存新版并返回占用说明。程序不因导出增加真实检索轮次或人工审核。

以下CSV保留备查与程序处理，UTF-8 BOM便于必要时打开：

- exports/每日检索政策台账.csv：8列，按北京时间日期和P编号逐条列政策，同一天同一P号只占一行；同日多轮核查保留截至当天的来源和人员意见，不把整天内容塞进一个单元格。
- exports/财务政策资金台账.csv：10列，一项政策一行，便于判断是否参与和跟踪办理。
- exports/财务政策资金明细.csv：完整原字段备查，保留财务证据、来源版本、审核和兑现凭证等细节，不要求每天逐列维护。
- exports/每日检索运行日志.csv：保留原日汇总字段及查询范围、观察计数、失败缺口和次日关键词，用于核查本日实际做过什么。无新、零候选或失败轮次也留日志，不造一条政策充数。

每日主表8列按此顺序：

| 列 | 标题 | 使用口径 |
| --- | --- | --- |
| A | 检索日期 | 实际查阅的北京时间日期；仅有人员反馈不伪称重新查网 |
| B | 编号及政策名称 | 稳定P号、通俗短标题及原文链接；正式全名和文号留明细 |
| C | 检索词/地区 | 与本条实际发现相关的检索词、地区，不重复堆入全日所有查询 |
| D | 能带来什么 | 一句话说明可核对的减税、退税、补助、贴息或担保用途，不写成已获支持 |
| E | 要核实什么 | 最多3项关键条件或资料缺口，使用财务人员容易理解的说法 |
| F | 办理时间 | 分别写政策执行期、税费所属期与企业办理截止；未知直说待核 |
| G | 资料下载 | 原文、已取得材料或明确缺口，历史参考和银行财政表样标用途 |
| H | 人工意见及下一步 | 真实人员意见、尚未完成的核实及下一步；机器建议要标明建议 |

长期主表10列按此顺序：编号及政策名称、政策类别、能带来什么、要核实/缺什么资料、办理时间、资料下载、是否参与、办理进展、下一步/负责人、最近跟进。是否参与和办理进展分列，待财务审核不等于已决定参与；负责人只写真实人员安排，未明确时保持待确认。最近跟进不伪称网站已重新核查。

主表只列已入P号候选。其他实际看过的标题、暂不推荐和历史项保留R复核池，人员可抽查并恢复候选。运行日志的观察计数不等于主表政策数或全部符合数。无候选时主表可为空，日志照记。历史日期按当日来源与意见回放，后续更新不回灌过去。

对话和表格统一使用通俗短标题、一句用途、最多3项关键待核条件；原文和完整条件仍可追溯。期限必须明确是政策执行期还是企业办理截止，不能把年底执行期终点写成已核本地受理截止。人员按编号在Codex回复，由系统更新后重导表格，直接改Excel不会自动回写或确认参与。

### 通俗显示说明

需要缩短原文标题和长条款时，可在本机 `data/finance_display_notes.json` 按真实内部记录ID保存机器显示说明，内部ID从 `data/display_ids.json` 对照，不把P号当内部ID。每项带当前 `source_version` 及带时区的 `source_checked_at`；若后来改写文字，再记真实 `updated_at`。字段可用 `title`（短标题）、`benefit`（一句用途）、`checks`（最多3项待核文本或列表）、`timing`（期间与办理节点）和 `next_action`（建议下一步）。说明只简化表达，不改来源事实或人员意见，不靠润色制造政策更新。版本不匹配时程序回到当前来源文字；历史日表不采用未来才写的说明。人员已给出的真实反馈优先于机器下一步建议，材料下载状态仍按材料清单和实际文件校验。

### Excel资料入口

Excel呈现时将主表的“资料下载”显示为“资料与用途”，仍占原来的一列，不改变8/10列数量或CSV表头接口。主表入口用工作簿内部链接跳到“资料明细”中对应P号，每份材料一行，写明用途、已取得状态或缺口，并给真实官方HTTPS正文或具体附件地址；不用本地相对文件夹作点击目标。无独立表样不给“下载”按钮，正式HTML正文不是申请表，旧指南标历史参考，银行财政表标非企业申请表。已下载的文件须在本机实际存在并与清单SHA相符，否则降为缺口，不能因旧清单仍标downloaded就宣称资料已取得。人员单独转发Excel仍能阅读说明并打开官方链接；本地副本可另附，不能以本地文件存在代替当前官方链接可用性核查。

## 人员财务反馈

仅在人员清楚指向本机事项并给出真实结论后执行review。财务state只接受：待财务审核、可推进准备、待补齐、不适用、历史参考、已到账、已享受。默认待财务审核；来源重要变更后的“待财务重新复核”为导出提示，不是新增CLI状态，也不会擦除历史凭证。

```text
PYTHON finance_workflow.py review --workspace <绝对目录> --id <真实P号或内部编号> --state 可推进准备 --reviewer <实际人员> --note <实际原话和证据>
```

可推进准备不自动参与；真实参与意见可另用monitor review，或在本命令显式加 `--participation 参与`、`--participation 先补资料`、`--participation 不参与` 同事务桥接。人员未作参与决定时不要附该参数。R筛选审核仍由human_review.py独立记录。

待补齐必须同时有缺口、部门和期限：

```text
PYTHON finance_workflow.py review --workspace <绝对目录> --id <真实P号> --state 待补齐 --reviewer <实际人员> --note <实际原话> --material-gap <缺的材料> --department <真实协同部门> --due <YYYY-MM-DD>
```

material-gap可重复，due为真实补齐日期，不代替政策外部截止。来源财务条件缺口与人员确认的补料责任分开展示。不适用也须在note写明官方门槛与企业事实，不能因缺资料或不确定就排除。

真实资金到账：

```text
PYTHON finance_workflow.py review --workspace <绝对目录> --id <真实P号> --state 已到账 --reviewer <实际人员> --note <实际核验原话和范围> --date <实际到账日> --voucher <会计凭证号> --evidence <实际本地归档文件>
```

真实税费享受：

```text
PYTHON finance_workflow.py review --workspace <绝对目录> --id <真实P号> --state 已享受 --reviewer <实际人员> --note <实际核验原话和范围> --date <实际享受日> --period <税费所属期> --voucher <凭证号> --evidence <实际本地享受证明文件>
```

date用YYYY-MM-DD，不能填未来日；已享受必须period，享受日和到账日分别展示。evidence可重复且须是已取得的本地归档文件，程序校验实际文件并留哈希；一个文件存在不证明它的业务内容成立，仍须财务人员核验。金额只在有证据时据实记入note/画像，不由程序测算。分期或后续义务在note清楚说明并继续跟踪，单次闭环证据不表示整项政策再无后续。

可选 `--reviewed-at <含时区ISO>` 记录真实审核时间，不能用早于当前来源核查的旧意见确认新版本；不提供则记本次真实回写时刻。人员明确确认次日检索词时加重复的 `--next-keyword <确认词>`。自动采集和生成报告时不得调用review来填满日报或虚构财务完成。
