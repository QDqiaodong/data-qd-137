# 招生费用收退清算契约（admission finance）v1

独立于单次匹配命令、共享配额工作台、双边声明核查与材料库的**第四条数据链路**。
它只把银行流水与管理员逐笔认领/退款分摊变成可核算的申请者费用余额：**不分配录取
名额、不判定材料齐备、不核查志愿双边声明，也不发起任何真实付款**。仅使用 Python
标准库（含 `sqlite3`），完全离线运行。既有命令、工作台、audit、materials 的输入
输出与状态契约保持不变。

## 命令

```sh
admission-match finance init   --source ROSTER --charges FILE --db DB
admission-match finance apply  --db DB --request FILE --bank CSV
admission-match finance report --db DB --output FILE
# python -m admission_match.finance.cli 与上面等价（不带 finance 前缀）
# python -m admission_match.cli finance ... 同样等价
```

数据/状态错误向 stderr 输出可读信息并以 **2** 退出，数据库与旧报告保持不变；合法
存在欠费仍以 0 退出。

## init

* `--source` 沿用 `docs/contract.md` 的 applicants/programs 结构与全部边界（最多
  500 申请者、1..100 项目、严格唯一偏好/排序列表、引用必须声明、capacity 0..500），
  **只取申请者身份**，延迟接受匹配器从不导入、从不运行。
* `--charges` 恰含一个 `charges` 数组。每项恰为 `applicant,currency,due_minor`：
  同一 `(applicant,currency)` 唯一；申请者必须存在于 roster；`currency` 仅
  `CNY/USD/EUR`；`due_minor` 为 `0..10^12` 的整数（布尔不是整数）。
* 拒绝重复 JSON 键、额外字段；id 校验沿用 `common.ident`。
* 已存在的 DB 路径一律拒绝；失败时不留半成品库。
* 保存两份初始化输入的**原始字节 SHA-256**与费用账户，此后不再依赖原文件。
* `revision=0`。

## 银行事实（bank CSV）

严格 UTF-8、标准逗号 CSV，表头依次恰为
`bank_id,version,kind,status,currency,amount_minor,origin_id`。

* 每行恰 7 列；禁止空白行；字段**不做自动 strip**；数字为无前导零的 ASCII 十进制。
* `bank_id` 与非空 `origin_id` 沿用 `ident`；`version` 为 `1..10^9` 整数。
* `kind=credit/refund`，`status=posted/void`。credit 的 `origin_id` 必须为空；
  refund 必须指向**同币种** credit（引用可在同批稍后出现，行顺序不是依赖顺序）。
* posted 金额 `1..10^12`；void 金额必须为 0。
* 一个文件内同一 `bank_id` 只出现一次。新项 `version=1`；旧项必须是当前版本 +1。
  `kind/currency/origin_id` 一经建立不可修改；`status` 与金额可更正。新版本只追加，
  不可覆盖旧事实（不可变历史表 `bank_facts`）。
* 单文件最多 200000 数据行、256 MiB。

### 流式接收与暂存

解析期间**绝不整文件读入内存，也不持有主库的读/写事务**。逐行流式处理：每块同时
喂给 SHA-256 哈希器与一个**私有临时 SQLite 暂存库**（有界页缓存，溢出落盘），因此
最大合法输入下峰值 RSS 低于 96 MiB（实测 120000 行 / 17.3 MiB 文件约 26–40 MiB）。
解析失败删除整个暂存目录；暂存目录从不被启动时扫描，中断遗留不会自动入账。文件原始
字节 SHA-256 必须等于 `request.bank_sha256`，否则整批失败。跨币种引用、跳版本、末行
错误或 SHA 不符都使整批失败，已读行不进入现行银行事实。

## 请求（request JSON）

恰含 `request_id,expected_revision,bank_sha256,operations`。`revision` 为非负整数；
SHA 为 64 位小写十六进制；`operations` 最多 1000 项，每项恰为以下一种：

* `{"type":"claim","id","bank_id","applicant","amount_minor"}`：把 posted credit
  认领到申请者费用账户；
* `{"type":"refund","id","bank_id","claim_id","amount_minor"}`：把 posted refund
  分摊回一笔原 claim；其 `origin_id` 必须等于该 claim 的 `bank_id`，币种与申请者由
  claim 决定；
* `{"type":"reverse","id","target_id"}`：撤销此前已提交且当前有效的 claim/refund。

分录 id 全库唯一且不可变；金额为 `1..10^12` 整数。reverse 不能针对 reverse，不能针对
同批新分录，一个目标最多撤销一次（同批也不行）；撤销保留原分录，仅置为 reversed。
新 refund 可引用同批新 claim。

## 守恒（最终状态，与操作/CSV 排列无关）

账本按**同时生效的最终状态**校验，不按逐项中间余额拒绝可行批次：

* 每笔 credit 的有效 claim 总额 ≤ 其 posted 金额；
* 每笔 refund 的有效分摊总额 ≤ 其 posted 金额；
* 每笔 claim 上的有效退款总额 ≤ 该 claim 金额；
* 有效 refund 只能引用仍有效的 claim；
* 任一 void 银行项不得有有效分摊；posted refund 的原 credit 也必须 posted；
* 每个账户 `net_paid = 有效 claim 总额 − 有效 refund 总额`，必须在 `0..due_minor`；
  `due_remaining = due_minor − net_paid`；
* 银行未认领/未分摊额在报告中逐笔可核算；
* 禁止浮点金额，禁止跨币种相抵。累计可能超过 SQLite INTEGER 范围，所有求和用 Python
  任意精度整数完成，库中绝不出现 REAL。

银行更正导致金额减少或作废时，账本用**新金额重新校验该银行项上的全部既有分摊**，而
不只检查本批新增分录。反向地，账本仍有效的退款依赖会阻止原 credit 作废；同批解除全部
依赖后才接受作废。只有银行候选版本与账本最终有效分摊**共同**满足守恒才提交。

## 事务、并发与崩溃边界

1. CSV 在主库之外流式落入暂存库（无主库事务）。
2. 主库打开一个 `BEGIN IMMEDIATE` 保留写锁，ATTACH 暂存库：
   - 按索引做 request_id 重放/冲突检查，并在锁内重查 `expected_revision`、涉及银行
     版本与分录依赖——CSV 校验期间另一进程改动余额后，基于过期状态的分摊无法提交；
   - 银行不可变历史追加、当前指针替换、分录写入、撤销链、请求结果与 revision 自增
     全部在同一事务内原子可见。
3. 进程在银行当前版本写入后、账本/revision 更新前被杀死时，SQLite 回滚日志恢复完整
   旧版：不会出现新银行金额配旧余额。重开只有完整旧版或完整新版。
4. 异进程争用同一 revision 时最多一笔新请求成功。
5. 相同 `request_id` 且相同**规范 request** 的重试返回首次结果，且该检查先于 revision
   与银行文件检查（CSV 已删除也能重放）；同 id 不同内容冲突。规范比较只忽略 JSON 键序
   与 operations 顺序；`expected_revision`、`bank_sha256` 都参与。CSV 仅换排列即使业务
   结果相同，因原始 SHA 不同仍属不同请求内容。

### 余额/依赖索引的选择

* `bank_facts` 为不可变追加历史；`bank_current` 只存当前版本。正常 apply **从不读
  `bank_facts`**，更正校验通过 `bank_current` 与暂存表的连接完成。
* `entries(bank_id)`、`entries(claim_id)`、`entries(applicant,currency)`、
  `entries(target_id)` 索引支撑按本批涉及银行项/账户/依赖的精确读取，不重扫全量历史
  或无关账户。本批键集合放入连接私有的 TEMP 表（避免把最多 200000 个银行 id 展开成
  参数列表，绕开 SQLITE_MAX_VARIABLE_NUMBER），所有查找走索引连接而非全表扫描
  （`EXPLAIN QUERY PLAN` 与 SQL trace 在测试中作为证据）。
* 重放按 `requests.request_id` 主键索引命中，成本与历史长度无关。

## report

在单一延迟读事务（一致快照）内输出，不跨快照拼接；相同快照字节确定。包含
`schema_version`、两份初始化 SHA、`revision`、按 `bank_id` 排序的银行当前事实及
`allocated_minor/unallocated_minor`、按 id 排序的全部分录与撤销状态（effective/
reversed/reverse）、按 `(applicant,currency)` 排序的
`due_minor/net_paid/due_remaining`。输出拒绝符号链接、拒绝覆盖 DB 或任一输入；失败
保留旧报告。
