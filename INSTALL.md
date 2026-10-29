# 财务版安装说明

本说明用于 `finance-policy-funding` 分支，当前版本 `0.2.1-finance.1`。财务版采用独立技能和工作目录，不直接转换正在使用的通用政策或招标工作区。

## 发给财务人员 Codex 的指令

把下面整段复制到财务人员自己的 Codex 桌面应用对话，另附企业画像私有包。

```text
请为我安装并启用财务找政策与政策性资金流程。
仓库：https://github.com/luoluo061/ZXQYFWW
指定分支：finance-policy-funding，请不要用main通用版代替。
请核对完整包VERSION为0.2.1-finance.1或该财务分支的后续版本，不使用旧版缓存。

请读取此分支INSTALL.md和skills/sme-finance-opportunities/SKILL.md，
下载完整分支，检查后运行install.py install --edition finance。
在源码和技能缓存以外新建独立“财务政策资金监测”工作目录，
用runtime/bootstrap.py init --edition finance --scope policy初始化。
导入我提供的私有画像包后，再运行configure-finance和doctor。
公司画像、财务证明和原始Word/Excel不要上传公开仓库。

日常只查税费优惠、奖补兑现、人才用工稳岗、融资增信贴息。
资质主要看衍生奖励减免或业务转来的财务指标复核，
不把资质认定或商业银行贷款产品作为本岗位日常主范围。
按真实证据核地区、纳税身份、财税所属期、财务资料及已享受情况，未知保留待核。

先做一次真实网页试跑：服务网发现、官方原文与附件复核，保留未推荐与理由，
用finance_workflow.py export自动生成每日检索政策台账.xlsx（8列，日期加P编号，一条政策一行，同日去重）、
财务政策资金台账.xlsx（长期10列）和人工复核池，不只交付CSV。
两份Excel的“资料与用途”在表内跳到“资料明细”，每份材料一行，
再点官方正文或真实附件地址，不链接作者电脑或本地相对文件夹。
写清正文、填写表样、历史参考和银行财政表的用途；未取得表样只列缺口，不放下载按钮。
采用通俗标题、一句用途和最多3项关键待核，政策执行期和企业办理截止分开写。
完整财务字段留财务政策资金明细.csv，原日汇总和失败缺口留每日检索运行日志.csv备查。
我按编号在Codex回复，由系统回写；直接改Excel不会自动回写。
无新政策也留本地检索日志，旧数据重导出不能称为今天已查。
待补齐写缺口、协同部门和期限；是否参与由我决定。
到账或税惠享受只按我提供的真实日期、凭证及证明记录，不自动确认。

试跑可用后，在当前对话用自动化工具建立每天北京时间09:00和16:30的财务检查，
优先核对已有相同财务任务，不重复创建或复制他人的任务ID和聊天ID。
上午打开服务网和重点页，同时给筛选理由、最多3条条件、期限、原文和材料；
下午按实质变化提醒，暂不另设固定完整日报。
请分别说明技能安装、画像缺项、真实试跑、两条定时登记和首次触发的验收状态。
```

缺少自动化工具时完成已能执行的部分，提供待登记提示词并明确尚未建任务。没有财务数据可查来源、保留线索，不得宣称税惠或资助条件已满足。

## 下载与初始化

也可[下载财务分支完整ZIP](https://github.com/luoluo061/ZXQYFWW/archive/refs/heads/finance-policy-funding.zip)并解压，再在其中执行安装命令。

```text
git clone --branch finance-policy-funding --single-branch https://github.com/luoluo061/ZXQYFWW.git
python install.py install --edition finance
python install.py doctor --edition finance
python runtime/bootstrap.py init --edition finance --scope policy --workspace "独立工作目录绝对路径" --operator "负责人显示名"
```

默认技能为 `~/.agents/skills/sme-finance-opportunities`，可以与通用技能并存。本分支安装器默认finance；显式 `--edition general` 仅兼容通用能力。Python 3.10+，可由 Codex 依赖工具找到本机自带运行时。Excel导出只用Python标准库，不需要额外安装表格依赖，也不依赖作者电脑的路径。

JSON画像可用初始化参数 `--profile-import`，保留企业事实并补财务配置。使用私有ZIP时先初始化，再执行：

```text
python tools/profile_bundle.py unpack --bundle "企业画像私有包.zip" --workspace "独立工作目录绝对路径" --replace-profile
python runtime/bootstrap.py configure-finance --workspace "独立工作目录绝对路径"
python runtime/bootstrap.py doctor --workspace "独立工作目录绝对路径"
```

替换前比较已有画像。configure-finance保存原画像备份，只补财务工作流和检索词，保留主体、已有资质和证据；纳税身份、年度数值、享受记录仍由人员确认。不能因已有高企等资格推定财务门槛全满足。

## 每轮运行

在个人工作目录执行，输入必须来自当轮实际网页读取。详细字段与人工回写见 [财务数据约定](skills/sme-finance-opportunities/references/finance-data-contracts.md)。

```text
python monitor.py run --input incoming/<run_id>.json --profile profile.json --db data/monitor.sqlite --out exports
python human_review.py ingest --input incoming/<run_id>_screening.json --db data/human_review.sqlite
python human_review.py export --db data/human_review.sqlite --out exports
python monitor.py export --profile profile.json --db data/monitor.sqlite --out exports
python finance_workflow.py log --workspace "当前工作目录绝对路径" --input incoming/<run_id>.json --screening incoming/<run_id>_screening.json
python finance_workflow.py export --workspace "当前工作目录绝对路径"
```

先归档上轮exports，再生成本轮导出。保留查询、地区、分页、失败和材料缺口。P编号财务审核用finance review，R编号筛选纠错用human_review review，人员参与决定另据明确回复记录。只是export不构成查新或人员已审。

最后的 `finance_workflow.py export` 自动生成两份供人员阅读的Excel：`exports/每日检索政策台账.xlsx` 和 `exports/财务政策资金台账.xlsx`，各含主表及“资料明细”。同名CSV、完整明细CSV和运行日志CSV仅供备查与程序处理。文件被WPS或Excel占用时按程序返回的实际新版路径打开，不关闭人员正在编辑的表。核对表内跳转和官方链接，不能把本地文件存在当成官网当前可访问。

## 验收和升级

技能完整、工作区edition=finance且scope=policy；真实画像和证据能定位；真实网页轮次可追溯；两份Excel主表按8/10列逐条可看、资料表内跳转和官方入口正确、备查CSV与R复核池可打开；没有人回复仍待审；当前账户返回的两条任务及对话归属、09:00/16:30状态读回正确。首次到点执行另行验收，电脑和应用需满足本地运行条件。

升级从新版完整财务包运行install，再用同包runtime/bootstrap.py upgrade更新既有财务目录。保留画像、人员审核、兑现证据、数据库和真实定时登记。自定义文件出现.new先合并；升级不等于网站查新。
