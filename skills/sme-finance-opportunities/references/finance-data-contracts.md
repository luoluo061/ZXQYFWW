# 财务数据与每日交付

财务版新增 finance_workflow.py，仍不联网。主候选/版本在 data/monitor.sqlite，实际观察和人员筛选复核在 data/human_review.sqlite；财务检索日志和人员财务进度追加保存到 data/finance_workflow.sqlite。不能手改CSV当作回写数据库，不用Git合并个人SQLite。

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

产生两份CSV，UTF-8 BOM便于Excel打开：

- exports/每日检索政策台账.csv：保留岗位表8列，按北京时间日期汇总本日实际登记轮次及人员反馈。
- exports/财务政策资金台账.csv：P号长期候选、财务条件、证据/缺口、办理节点、来源版本、人工筛选/参与/财务状态与凭证。

岗位八列按此顺序：

| 列 | 标题 | 使用口径 |
| --- | --- | --- |
| A | 检索日期 | 北京时间的实际检索日；仅有人员进展的日期不伪称已查网 |
| B | 今日检索关键词 | 来自本轮真实查询和观察，不能把预设词都当搜过 |
| C | 检索地域（省/市） | 实际地区筛选及覆盖，不用企业所在地替代实际搜索范围 |
| D | 今日检索政策条数 | 当日已记录可辨认信息去重数，不是全网总量或全部符合数；失败且已读0条显示未完成 |
| E | 今日待办事项进展 | 人员真实审核、补料和兑现反馈；机器运行不填成人员已推进 |
| F | 次日待检索关键词 | 区分“系统建议（待人员确认）”“人员确认”或待人员确认 |
| G | 政策线索及人工判断 | P号、标题、真实原文、财务审核和参与状态；历史/待核不能写已符合 |
| H | 备注 | 真实轮次数、缺口和阅读/计数边界，无新也留痕 |

G列替换“符合条件的政策标题及链接”，是为让待核/历史保留线索不被误读为已符合。每日CSV是便于人看的汇总，完整逐轮证据和未推荐项仍在日志库与R复核池；不是另建一套人工维护主账。零新增轮次可使日检表出现真实日志行，但不能造一条“无新政策”进候选库。

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
