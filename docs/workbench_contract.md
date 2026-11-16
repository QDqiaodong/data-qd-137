# 共享区域配额录取工作台 v1（独立契约）

本契约与单次 `--source/--output` 命令（见 [contract.md](contract.md)）完全独立：
不共享 DA、blocking_pairs 或报告发布逻辑之外的业务语义。所有命令仅使用 Python
标准库，数据落在本地 SQLite，不连接任何外部服务。

## 命令

```sh
admission-match init   --source FILE --db DB
admission-match apply  --db DB --request FILE
admission-match export --db DB --output FILE
# 等价入口
python -m admission_match.cli init   --source FILE --db DB
```

旧的无方子命令调用 `admission-match --source ... --output ...` 保持不变。
所有数据/状态错误以退出码 2 结束并输出可读错误，失败不改动数据库，也不替换
旧报告（报告仍走 `common.save` 的临时文件 + `os.replace` 安全发布）。

## init 源文件契约

顶层恰为 `applicants` / `programs` / `regions` 三个数组。

- 申请者沿用原契约字段：`id`、`preferences`（严格、不重复、0..100 项），
  最多 500 人；空申请者数组合法。
- 项目：`id`、`capacity`（0..500 整数）、`ranking`（最多 500，无重复）、
  新增 `region`；最多 100 个项目，每个项目恰归一个已声明区域。
- 区域：`id`、`parent`（区域 id 或 null）、`capacity`（0..500 整数），
  最多 200 个。`parent` 关系必须构成**一棵**无环有根树：恰好一个
  `parent=null` 的根，任何父引用必须已声明，沿父链上行不得成环。
- 偏好与 ranking 的引用必须已声明；仅双方互相列入对方才是可接受合同。
- 区域容量约束其**全部后代**项目的录取总数（即占用该区域的每个项目录取都计入
  该区域及其所有祖先）。项目与区域容量为 0 均合法。
- `init` 拒绝任何已存在路径（含符号链接），绝不覆盖旧库。成功后 `revision=0`，
  并立即按下方选择规则生成首批候补 offers。

## 区域选择规则 C_L

确认集合 `L` 先占用其项目及全部祖先区域额度。对候选合同集合 `S`：

1. 不可接受合同不进入 `S`；
2. 其余合同按全局键
   `(申请者在项目 ranking 中的 0 基名次, 项目id, 申请者id)` 升序扫描；
3. 逐个保留加入后不突破**任何**项目或祖先区域容量的合同，得到 `C_L(S)`。

匹配由申请方提议驱动（不是先跑各项目旧 DA 再截断区域结果）：每个重建轮次，
所有未确认、未退档且当前无座的申请者从完整志愿的下一志愿提议；项目侧对
`确认合同 ∪ 现存候补合同 ∪ 本轮新提议` 整体施加一次 `C_L`；被拒者续投下一志愿，
直到无人可投。每次重建都从所有未退档、未确认者的完整志愿重新开始，退档释放的
祖先额度允许此前被拒者重新竞争。已确认录取受保护，不会被更高优先级候补挤出。

### 稳定性审计（定义与旧 blocking_pairs 不同）

对每个未确认申请者，取其比当前 offer 更偏好的每个可接受合同 `x`，当且仅当
`x ∈ C_L(H ∪ {x})` 时构成阻塞，其中 `H` 为当前未确认合同集合。审计集合保留其
旧合同，不额外施加“申请者唯一”约束；确认者不参与审计。提交修订前审计必须为空。

## apply 请求契约

每份请求 JSON 恰含 `request_id`、`expected_revision`（非负整数）、
`operations`（最多 1000 项）。操作类型：

- `{"type":"confirm","applicant":...,"offer_token":...}`
- `{"type":"withdraw","applicant":...}`
- `{"type":"set_capacity","kind":"program|region","id":...,"capacity":0..500}`

字段严格校验（无多余字段；布尔不视为整数；id 走统一 ident 边界）。同批中每个
申请者至多出现一次，每个 `(kind,id)` 至多出现一次。

- `confirm` 只接受当前 revision 真实发给该人的 offer。`offer_token` 是 HMAC，
  绑定**本库密钥**、revision、申请者与项目；异库 token、旧版 token、他人 token
  一律失败。
- `withdraw` 只允许 active/confirmed → withdrawn；withdrawn 为终态，不能恢复，
  其余非法状态转换整批拒绝。
- 同批操作表示同时生效的最终状态，与排列次序无关：先用旧版 offer 校验确认来源，
  再对整批最终确认集合与新容量做可行性判定。最终确认数超过任一项目或祖先区域
  容量即整批失败；同批退掉旧确认后再降容量，只要最终可行就必须成功。
- 只有最终确认状态与容量全部通过校验，匹配职责才按剩余额度重建所有活动
  offer；任何失败下 revision、请求日志、状态与 offers 都不变，失败请求**不写日志**。

### 并发、重放与崩溃语义

- 每个成功请求只把 revision 推进一次；`expected_revision` 必须匹配提交时版本。
- 同一 `request_id` 携带相同规范内容重放时返回首次结果且不改库，即使数据库已
  前进（id 命中后直接回放首次结果，不再做实时 expected_revision 校验）；重放比较
  只忽略 JSON 键顺序与 operations 排列顺序，`expected_revision` 或操作本身不同
  即视为不同内容并冲突。相同 id 不同内容冲突。
- 不同进程可同时 apply：写事务以 `BEGIN IMMEDIATE` 立即预留写锁，锁内重读
  revision 并完成全部校验、匹配与写入，因此争用同一版本最多一个成功，其余得到
  revision 冲突。重放判定走主键索引查询，不在事务内回放历史；单次匹配工作量只
  依赖当前申请者、志愿边、项目与区域，不随日志长度增长。
- 修订号、状态、容量、offers 与请求日志在同一事务提交。计算期间写锁保证版本不
  会变化，因而不会提交“按旧版本算出的过期 offer”；进程中断后重开数据库，
  SQLite 回滚日志保证只可见完整旧版或完整新版，不会出现已写确认却挂旧候补的
  混合状态。测试通过环境变量 `ADMISSION_MATCH_FAULT=before_offers|before_commit`
  在写事务内两处注入 `os._exit` 中断（仅测试用途，未设置时零开销），由
  `tests/test_workbench.py` 的崩溃恢复用例核对整版可见性。

## export 快照

单次一致读快照输出，键固定、按 id 排序，相同快照字节确定：

`schema_version`、初始源文件 `source_sha256`、当前 `revision`、申请者状态与最终
分配（按 id）、项目录取（按 id）、区域占用（由最终分配独立核算，按 id）、当前
offers（按申请者；含 revision 与 token）、当前策略 `blocking_pairs`、请求日志
（每条含规范请求与当时记录的结果，支持精确重放）。每人最终至多一个项目；确认者
不列入 offers 而列入申请者状态与分配。报告文件不充当数据库提交标志。
