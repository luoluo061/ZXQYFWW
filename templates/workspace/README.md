# 本机政策与软件招标监测

工作目录：{{WORKSPACE}}  
负责范围：{{SCOPE}}；人员：{{OPERATOR}}

Codex实际查网站、核证据和初筛；人员主动浏览、复核未推荐并决定是否参与。先看[人工每日审核说明](人工每日审核说明.txt)，完整规则见[日常监测与人工判断规则](日常监测与人工判断规则.txt)。

`monitor.py` 与 `human_review.py` 只处理已采集数据，运行脚本不会自动查网站。真实采集需当前Codex可用的网页/浏览器工具；正常登录在本人浏览器完成。

## 看结果

- `exports/政策台账.csv`、`exports/招标台账.csv`：两份长期台账，P/T稳定编号，默认待人员判断。
- `exports/每日人工复核包.md`：当天重点、待审及建议抽查。
- `exports/政策人工复核池.csv`、`exports/招标人工复核池.csv`：实际发现的全部轻量记录，包含机器未推荐与历史项，R号审筛选。
- `exports/每日人工查阅记录.csv`：仅据真实人员回复记录的独立阅读。
- `materials/materials_manifest.json`：实际下载状态、官方链接、文件与SHA256。

每天直接在当前对话查看重点分析、自主浏览服务网并抽查未推荐，用五行交付“主动浏览、推荐审核、未推荐抽查、画像/检索反馈、未完成/协作”。打开网页和收到消息不代表人已审。

## Codex运行命令

先把命令工作目录设为本目录。`PYTHON`须替换为本机实际Python 3.10+路径，优先使用Codex的load_workspace_dependencies返回的运行时；需要时PowerShell用 `&` 调用带空格的可执行路径。文件参数来自本次实际采集，不可把模板示例当机会。

```text
PYTHON monitor.py run --input incoming/<run_id>.json --profile profile.json --db data/monitor.sqlite --out exports
PYTHON human_review.py ingest --input incoming/<run_id>_screening.json --db data/human_review.sqlite
PYTHON human_review.py export --db data/human_review.sqlite --out exports
PYTHON monitor.py export --profile profile.json --db data/monitor.sqlite --out exports
```

覆盖导出前归档前轮，P/T关联更新后再导出。`data/display_ids.json`是内部编号至P/T显示号映射；参与回写用monitor的内部编号，筛选回写用human_review的R号。详见完整安装包 `skills/sme-opportunity-monitor/references/data-contracts.md`。

`profile.json`仅用本机已确认企业资料；待核字段保留待核。`sources.json`定义负责来源和地域范围，初始化通用来源须按实际情况复核。私有画像、证明、数据库和回执不要提交公共仓库。

## 安装、定时与更新

完整安装包的 `runtime/bootstrap.py init/doctor/upgrade --workspace <本目录绝对路径>`负责初始化和升级。规则被本地修改时升级保留原文件并提供新副本；人工记录、画像和数据库不因升级清空。

安装程序不直接建立定时任务；接收人Codex在自己的当前对话用automation_update创建并读回，实际ID保存 `data/automation_setup.json`。默认每天北京时间09:00与16:30，范围取本机分工；完整日报时间未定。记录为空不代表任务已激活。不同人员各有本地台账，不会自动合并团队审核状态。
