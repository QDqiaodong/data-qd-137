# 招生材料库 v1（独立契约）

本契约与单次 `--source/--output` 匹配命令、共享区域配额工作台（init/apply/
export）和双边声明核查（audit）完全独立：材料库**不做录取分配**，也**不做志愿表
与排序表的双边声明免审**，只从本地文件收取申请材料、保留不可变内容与每个申请者
材料槽的版本，并向招生管理员回答指定日期各志愿的材料是否齐备。仅使用 Python
标准库（含 sqlite3），不连接任何外部服务。现有命令的契约与结果保持不变。

## 命令

```sh
admission-match materials init   --store ROOT --source ROSTER --policy POLICY
admission-match materials ingest --store ROOT --manifest FILE --files DIR
admission-match materials report --store ROOT --day D --output FILE
# 等价模块入口
python -m admission_match.materials.cli init   --store ... --source ... --policy ...
python -m admission_match.cli    materials ingest ...
```

所有数据/状态错误以退出码 2 结束并输出可读错误；失败不改动既有目录，也不替换旧
报告。合法但未齐备的报告仍正常写出并以 0 退出。报告输出拒绝符号链接，且不得位于
store 内部。

## store 布局与 init 契约

`ROOT` 是专用目录，`init` 拒绝任何已存在的路径（含符号链接），绝不覆盖旧库：

```
ROOT/
  ledger/materials.sqlite   # SQLite 目录账本
  objects/xx/<sha256>       # 按内容 SHA256 去重的不可变文件对象
  objects/xx/.incoming-*    # 落盘中的临时对象（崩溃后可残留）
```

- `ROSTER` 沿用 [contract.md](contract.md) 的 `applicants`/`programs` 严格结构与
  全部边界（最多 500 申请者、1..100 项目、严格不重复志愿/排序、引用必须已声明、
  capacity 0..500），材料库只借用申请者、项目与志愿身份，不执行 analyze/allocate。
- `POLICY` 恰含 `requirements` 数组；每项恰为 `program`、`kinds` 两个字段；每个
  已声明项目恰好出现一次；`kinds` 为 0..20 个不重复的 ident（空数组合法）。
- init 保存原始 ROSTER 与 POLICY 字节的 SHA256（写入 SQLite meta），此后不再依赖
  这两个文件仍然存在。

## ingest 清单契约

清单 JSON 恰含 `batch_id`、`expected_revision`、`documents`；`documents` 为
1..1000 项，同批不得重复 `(applicant, kind)` 槽。拒绝重复 JSON 键与多余字段，
布尔不视为整数。清单文件本身限制为 16 MiB（最多 1000 份文档，每份含若干有界
ident）。

- put 项恰含 `action="put"`、`applicant`、`kind`、`version`、`issued_day`、
  `expires_day`、`path`、`sha256`、`byte_size`。
- withdraw 项恰含 `action="withdraw"`、`applicant`、`kind`、`version`。
- 申请者必须在库中；`kind` 必须被 POLICY 中至少一个项目要求；id 走统一 ident
  边界；`revision` 为非负整数，`version` 为正整数；日期为 0..3652058 整数且
  `issued_day < expires_day`。
- 新槽只能 put `version=1`；已有槽每次必须为当前版本 +1；withdraw 只接受当前为
  put 的槽；put 可接替 put 或 withdraw。不合法引用、虚报版本一律整批拒绝。

### 内容接收职责

- `path` 是 `DIR` 下的相对 POSIX 路径：拒绝绝对路径、反斜线、空段、`.`、`..`，
  路径任一层是符号链接都拒绝，只接收常规文件（目录逃逸失败）。
- 声明 `sha256` 必须为 64 位小写十六进制；`byte_size` 为 0..67108864 整数；逐文件
  实际字节数与 SHA256 均须与声明吻合（末尾字节损坏、虚报长度分别失败）；一批累计
  不超过 512 MiB。
- 接收按 1 MiB 分块流式哈希与复制，不整份载入内存；处理最大合法材料时进程峰值
  RSS 低于 64 MiB。
- 相同内容只存一个对象。重用已存在对象前仍重新验证字节数与完整 SHA256；发现坏
  对象则整批失败，**不能覆盖坏对象**（可能已有槽引用它）。
- 任何接收失败都不更新任何材料槽，失败批次的材料不进入现行状态。

### 目录、重放与并发职责

- 每个成功新 batch 只把 revision 推进一次；整批槽变化与不可变 batch 记录在同一
  SQLite 写事务提交。
- 相同 `batch_id` 且相同**规范清单**的重试直接返回首次结果，即使 revision 已前进
  或原材料文件已删除（重放不再需要 DIR 中的文件）。规范比较只忽略 JSON 对象键与
  `documents` 排列顺序，字段值全部参与（对象键 sha256、path 也参与）；id 相同而
  内容不同必须冲突。
- 两个进程争用同一 `expected_revision` 时最多一个成功：目录事务以
  `BEGIN IMMEDIATE` 预留写锁，锁内重新核对 revision 与各槽当前版本，不能提交按
  旧状态判断的接替，败者得到 revision 冲突且无部分目录变更。
- 复制/校验材料期间不持有 SQLite 写事务。

## 对象与目录的提交顺序及恢复边界

文件对象与 SQLite 不是同一事务，提交顺序固定为：

1. 先把本批引用的每个对象完整接收：写同目录临时文件、fsync、原子 `os.replace`
   到 `objects/xx/<sha256>`，此阶段没有任何 SQLite 写事务；
2. 再打开 `BEGIN IMMEDIATE` 目录事务，重读 revision、重查每个槽版本，写入槽行、
   不可变 batch 记录并把 revision 加一后提交。

因此进程在任一对象落盘后、目录提交前中断时：重开只能看到完整旧目录（对象为无
引用的完整对象，可保留但不会被 report 枚举为材料）；目录事务中断由 SQLite 回滚
日志保证只可见完整旧版或完整新版。不会出现 ready 引用缺对象，也不会让失败批次的
一部分材料进入现行状态。对象内容在提交后不得再改写；无需删除或回收对象。槽的
逻辑撤回或有效期变化只改变该槽，不影响共享同一对象的其他槽。

测试通过环境变量 `ADMISSION_MATCH_MATERIALS_FAULT=objects_durable_before_commit`
在对象全部落盘后、目录事务前注入 `os._exit` 中断（仅测试用途）。

## report 快照与齐备判定

report 用同一个一致只读目录快照生成：

- 为每个申请者**实际列出的每个志愿**生成一行，按 `(applicant, program)` 排序；
  项目 ranking 与 capacity 不影响材料要求；无志愿的申请者不产生行；某项目
  `kinds` 为空时该行 `required_kinds` 与 `missing_kinds` 均为空且 `ready=true`。
- 每种必需 kind 只看**当前版本**：当前为 put 且
  `issued_day <= D < expires_day` 才为 `valid`；当前为 withdraw 给 `withdrawn`；
  尚未生效给 `not_yet_valid`；已过期给 `expired`；缺槽给 `missing`。不得退回旧
  有效版本（接替为过期材料后不能沿用旧版）。
- 每行列出 `required_kinds`、每种 kind 的 `status`/当前 `version`/对象
  `object_sha256`（非 put 当前版本对象 sha 为 null）、`missing_kinds` 与 `ready`；
  数组均排序，相同快照字节确定。
- 顶层输出 `schema_version`、初始化两份 SHA（`roster_sha256`、`policy_sha256`）、
  当前 `revision`、`day`、`pairs`。
- report 不得把缺失或损坏的当前 put 对象当作齐备：发布前对每个当前 put 引用的
  对象重新验证字节数与完整 SHA256，发现缺失/损坏则整次失败并保留旧报告。
