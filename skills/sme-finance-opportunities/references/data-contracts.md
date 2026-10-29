# 财务采集与人工回写

命令在独立财务工作目录中运行，PYTHON替换成本机实际解释器。核心脚本运行副本位于工作目录，不向插件/技能缓存写数据。安装和自检从完整包根的 runtime/bootstrap.py 运行。

## 每轮实际读取

先看画像、来源、既有财务事项、未审与回执，再用本轮正常网页/浏览器工具实查。保存唯一 run_id、带时区 checked_at、查询词/地区/分页/时间范围、证据和覆盖缺口；success只表示所记范围完成，不是全网。覆盖状态 success / partial / failed / auth_required，网页失败不能当“无新增且已完成”。

候选输入参照 `runtime/examples/incoming.example.json` 的结构而不导入演示内容。所有财务事项 kind=policy，scope=policy；财务版本由 profile.workflow=finance 与工作目录安装配置识别，不发明 finance 的kind。每条至少 kind/source_id/source_url/title，可补 original_url/original_verified、issuer、document_number、lot、round、stage、published_at、deadline_at、deadlines、summary、hard_conditions、evidence_path。每个有效支持路径和办理期间保留身份，不把不同年度、批次、兑现轮次混为一条。

stage 可取 open/upcoming/change/terminated/awarded/historical/unknown。长期税惠办法不因没有一轮申报窗口就一律 historical；指南征集、拟拨付名单也不能冒充开放申报或实际到账。原文没给确切时间不要编时刻。企业填报、属地受理/推荐、补料、兑现等节点分别保存。

财务条款按 `{"label":"财务条件","status":"unknown","evidence":"未取得企业证明"}` 保存，只取 pass/fail/unknown。判fail须同时有适用官方门槛和企业事实，缺证据不判不符。original_verified=true仅在本轮实际读到正式原文时使用；摘要/平台详情/官方转载分别描述，不拔高核验等级。

筛选输入参照 `runtime/examples/screening.example.json`。每个实际读到列表的可辨认标题都留 observation，不只保存候选。结构含 run_id/checked_at/scope/coverage/observations；每条至少 kind=policy、source_id、list_url、title、machine_bucket、machine_reason、reason_code、reading_level。

| 字段 | 可用值 |
| --- | --- |
| machine_bucket | 推荐／待核／暂不推荐／历史信息 |
| reason_code | 业务／地区／主体／期限／公告性质／重复／未知／相关／其他 |
| reading_level | 标题／摘要／平台详情／原文 |

可补平台/详情/正式原文URL、发布日、页码、检索词、机关、文号、路径/轮次、deadline_at、财务条件证据和 opportunity_id。original_verified=true须有实际原文URL且reading_level=原文。共享列表不能充当单条原文。低层级新观察保留但不覆盖已核事实；发现明确新标题/截止/条件冲突，留下待复核线索并补读原文。

## 导入与导出

覆盖前，归档本工作目录上一轮 exports、回执与必要快照。导入顺序：

```text
PYTHON monitor.py run --input incoming/<run_id>.json --profile profile.json --db data/monitor.sqlite --out exports
PYTHON human_review.py ingest --input incoming/<run_id>_screening.json --db data/human_review.sqlite
PYTHON human_review.py export --db data/human_review.sqlite --out exports
PYTHON monitor.py export --profile profile.json --db data/monitor.sqlite --out exports
```

monitor稳定分配P号，筛选池独立分配R号。data/display_ids.json 是内部记录编号→P显示号，不能反写或手占号。为筛选项补齐真实内部 opportunity_id，必要时重导展示；不猜关联、不让一个列表URL合并不同事项。后续财务log/export接口见 [财务扩展](finance-data-contracts.md)。空候选可保存空items，空观察用空observations；保留真实coverage，不凑数。

## 材料和解读

materials/materials_manifest.json 顶层为对象，使用 attachments 数组。每件保存 item_id（真实内部编号，不是P）、short_id、title、url、source_url、local_path、status、checked_at、note。相对local_path以工作目录为根，建议materials/内部编号/实际文件名。

downloaded仅用于实际取得且核文件/哈希的材料，补downloaded_at、sha256、size_bytes；其他状态如auth_required、failed、inline_notice_verified照实记录且local_path=null。通知正文不能伪称独立申报文件。材料路径存在和程序导出不能替代本轮SHA验证；已有文件重验未变不写新附件。

data/card_notes.json 按内部编号存 source_version、brief_interpretation、relevance_reason、key_conditions（最多3条）。source_version与主库当前版本匹配才用；不把解读写回来源事实或靠润色制造更新。

## 只有人员明确回复后

人员复核机器筛选时：

```text
PYTHON human_review.py review --db data/human_review.sqlite --id R实际编号 --reviewer 实际姓名 --outcome 先补证据 --note 实际原话及依据 --evidence-url 实际核读链接
PYTHON human_review.py independent --db data/human_review.sqlite --kind policy --reviewer 实际姓名 --url 实际链接 --title 实际标题 --note 实际判断 --read-at 实际ISO时间 --reading-level 平台详情
```

review的结论为恢复候选／维持暂不推荐／先补证据／仅作储备，不改参与。independent只记录人员实际自主阅读，平台页不能写原文。机器重读、开标签、发卡片、生成日志都不能调用这些接口制造人已审。

人员决定参与时使用：

```text
PYTHON monitor.py review --db data/monitor.sqlite --id <内部记录编号> --state 先补资料 --note <实际姓名、原话和依据>
```

monitor的id是内部编号，须从P映射反查。或使用财务review的明确participation选项桥接，见财务扩展。不能两处重复写相矛盾意见。恢复候选、财务可推进准备、参与、已提交、资金到账是不同事实。已参与继续跟进，窗口关闭不自动结束。

## 通知与开场

data/notification_receipts.json初始为空，首次真正发送后维护version/items/review_opening_receipts。items按内部编号记display_id、content_version、materials_sha256、last_notice_at、notice_kind、reminder_markers与notice_history，依据真实版本/附件/7-3-1日节点去重；仅改路径或润色不新通知。

早间财务开场单独存 `review_opening_receipts["北京时间日期|finance_policy_morning_review"]`，记录run_id、at、display_ids、实际pages结果、human_review_inferred=false。排队/失败不记当前可见，生成素材未发给人不提前写已交付。普通机会回执和开场回执不互相覆盖，人员主动查全部不受去重限制。

自动化登记只在接收人当前对话工具成功并读回后写data/automation_setup.json。真实ID、edition=finance、scope=policy、slot、Asia/Shanghai、时间、工作目录、对话与状态要核实；requested_schedule只是计划，未到点的first_scheduled_run_verified仍为false。
