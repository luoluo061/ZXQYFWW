# 首次运行的状态与材料格式

本页是本工具的本地约定，不是 Codex 调度器格式。使用程序生成的真实内部编号和版本；下面的键名示意不能当实际机会或已发送记录。更新时保留未知字段及已有历史，写入前核对原文件，不能用空模板清掉既有状态。

## P/T 映射

`data/display_ids.json` 初始为 `{}`，由 monitor 导出维护：

```json
{"内部记录编号":"P001","另一内部记录编号":"T001"}
```

此文件方向为内部编号→显示编号；不要反写，也不要人工先占号。每台新机独立编号，同号不代表跨人同一事项。R编号由 human_review 数据库自动生成。

## 材料清单

`materials/materials_manifest.json` 顶层对象含 `attachments` 数组；不是平铺列表或以 P/T 为键的对象。每件材料这样记录：

```json
{
  "attachments": [
    {
      "item_id": "真实内部记录编号",
      "short_id": "真实P或T显示号",
      "title": "官方附件名称",
      "url": "真实官方直接下载链接或获取入口",
      "source_url": "本轮实际核读的正式公告URL",
      "local_path": null,
      "status": "auth_required",
      "checked_at": "实际带时区ISO时间",
      "note": "须登录取文件，本轮未取得"
    }
  ]
}
```

链接字段保存真实 HTTP/HTTPS 业务地址，无直接链接留 null，不填上述描述文字。`item_id` 必须匹配主库内部记录编号，`short_id` 仅辅助展示。相对 `local_path` 以工作目录为根，推荐 `materials/<内部记录编号>/<实际文件名>`；不能引用作者电脑路径。

状态约定：`downloaded`=实际文件已取得；`auth_required`=需正常登录未取得；`purchase_or_login_required`=需登录或购标未取得；`failed`=实际下载失败；`inline_notice_verified`=已读正文但独立资料未核得。非下载状态 local_path=null。`downloaded` 还应记录真实 `downloaded_at`、`sha256`、`size_bytes`，可记格式/容器校验；计算实际文件哈希并确认能正常解析后再标成功。当前导出程序只以状态和文件存在判断本地可用，不替你自动验证 SHA256，不能据导出文案宣称哈希已验。

## 展示解读

`data/card_notes.json` 初始 `{}`，按真实内部编号保存：

```json
{"内部记录编号":{"source_version":1,"brief_interpretation":"一句用途","relevance_reason":"一句企业关联","key_conditions":["门槛或待核1","门槛或待核2"]}}
```

`source_version` 要等于主库当前版本；不匹配的展示注释将被忽略，重新核原文后再更新。最多3条关键条件。不要把解读写回来源正文或靠换措辞制造公告更新。

## 通知与早间开场

`data/notification_receipts.json` 初始 `{}`。首次真实交付后逐项补结构：

```json
{
  "version": 1,
  "items": {
    "真实内部记录编号": {
      "display_id": "真实P或T显示号",
      "content_version": 1,
      "materials_sha256": [],
      "last_notice_at": "实际发送时间",
      "notice_kind": "new_opportunity",
      "reminder_markers": [],
      "notice_history": []
    }
  },
  "review_opening_receipts": {}
}
```

普通去重按该内部事项的内容版本、真实材料SHA集合和临期节点判断。改变本地路径或仅润色不新发；实质变更先留原文/附件证据。新的7/3/1日提醒 marker 可用 `节点名称|真实截止日期|days=3`，同marker不重复；记录对应截止、原文、run_id及发送时间，不能把部门推荐时间当企业提交。

每日政策开场写同一文件内 `review_opening_receipts["北京时间日期|policy_morning_review"]`，值至少保存 `run_id`、`at`、`display_ids`、`pages`、真实页面结果及 `human_review_inferred=false`。pages记本轮URL及可用tab标识；排队/失败记真实状态，不记“当前可见”。普通机会回执和开场回执不能互相覆盖。仅生成素材尚未发给人时，不提前写发送回执。

这些 JSON 由当次 Codex 按真实操作维护，不是程序自动通知服务；人员是否看过、是否参与另在两个数据库中依真实回复留痕。

## 自动化登记

`data/automation_setup.json` 初始 `automations=[]`、`status=not_registered`。`requested_schedule` 只是预定计划。接收人当前对话的 automation_update 工具实际成功并读回后，更新真实状态及 automations 数组，每项至少保存实际 id、name、scope、slot、local_time、timezone、workspace、工具返回status和verified_at；有当前对话ID时保存实际值，不猜或复制。

只有工具成功可记录已登记；本地写文件不会启动自动化。此格式仅供定位和核对，不直接导入 Codex。登记与实际运行分开：首次定时未到点时 `first_scheduled_run_verified=false`；到点有真实输入、查询范围和运行结果才可更新。
