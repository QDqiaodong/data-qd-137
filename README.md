# data-qd-137 · 限额志愿稳定匹配

Applicant-proposing deferred acceptance with blocking-pair audit。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/admission-match --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/admission_match/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 共享区域配额录取工作台（SQLite）

在保留上面单次命令的同时，新增可反复办理的招生工作台，业务输入、持久化与并发
语义见 [工作台数据契约](docs/workbench_contract.md)。仅用标准库（含 sqlite3），
不连接外部服务；新契约独立，旧 DA、blocking_pairs 与 `common.save` 不作为新增
能力的替代实现。

```sh
.venv/bin/admission-match init   --source fixtures/workbench.json --db run/wb.sqlite
.venv/bin/admission-match apply  --db run/wb.sqlite --request one-request.json
.venv/bin/admission-match export --db run/wb.sqlite --output run/snapshot.json
# python -m admission_match.cli <子命令> 等价
```

要点：同一学院多个项目共享区域上限（区域构成一棵有根树，容量约束全部后代项目的
总录取）；已确认录取受保护，候补结果随退档或配额调整按跨项目选择规则 `C_L`
重新计算。`apply` 是带乐观 revision 的原子事务，支持异进程并发、幂等重放与崩溃
后的整版可见性；`offer_token` 绑定库密钥、revision、申请者与项目。`export` 输出
字节确定的一致快照。空申请者与零容量样例见 `fixtures/empty_workbench.json`。
测试中 `test_regional.py` 用独立枚举小实例的预言机核对申请方最优稳定结果
（预言机不调用生产选择函数），`test_workbench.py` 含真实双子进程竞争与故障注入
恢复，`test_scale.py` 核对 500/100/40 生产上限及匹配成本不随请求日志增长。

## 招生导入前双边声明核查（audit）

只读数据质量产物：对照申请者志愿表与项目排序表，定位需要回源确认的单边声明。
不运行录取匹配、不读写 SQLite 工作台、不修改源数据；`--source` 沿用
[数据契约](docs/contract.md) 的 applicants/programs 结构与全部边界。

```sh
.venv/bin/admission-match audit --source fixtures/audit_source.json \
    --policy fixtures/audit_policy.json --output run/audit.json
# python -m admission_match.cli audit ... 等价
```

核查对象是两侧声明的并集（不枚举申请者×项目全组合）：双方都声明为 `mutual`，
仅志愿表声明为 `applicant_only`，仅排序表声明为 `program_only`，双方都未声明不产生
记录。报告列出全部单边记录（按 applicant、program 排序，保留双方原列表 0 基位置，
缺失侧为 null）、三类总数、按项目排序的声明并集/差异/免审/未免审汇总与阈值结果。

政策文件是严格 JSON，恰含三个字段：

- `source_sha256`：必须等于本次源文件原始字节 SHA256，源字节任何变化都使旧政策失效；
- `waivers`：`{applicant, program, reason}` 数组，每条唯一对应当前源中真实存在的
  一条单边声明且 `reason` 非空；过期、重复、针对双方声明或不存在组合的条目使整次
  核查失败。有效免审只把该记录标记为 `waived` 并使所属项目未解决数减一，记录仍在
  报告中，两侧名次不被改写；
- `limits`：恰含 `max_unresolved_per_program`（0..500 整数）与
  `max_unresolved_share`（`{numerator, denominator}`，分母 1..100000，分子不大于
  分母）。每个项目以免审后未解决数为分子、声明并集数为分母，按整数交叉相乘判定，
  不使用浮点；声明并集为空的项目比例为 0。

退出码：全部通过为 0；任一项目超出数量或比例限制时仍写出完整报告并以 1 退出，
报告 `exceeded_programs` 明确列出超限项目；输入/政策/输出路径错误以 2 退出并保留
旧报告。输出拒绝符号链接及与源文件或政策文件相同的路径；相同输入字节与政策字节
必得字节相同报告。样例见 `fixtures/audit_source.json` 与
`fixtures/audit_policy.json`（后者内嵌前者字节 SHA256）。

## 当前业务与样例
# Applicant-proposing matching v1

将不完整严格志愿表与项目优先级、限额匹配，供名额分配人员离线核验。最多500名applicants(id,preferences)、1..100个programs(id,capacity,ranking)。两侧id唯一、排序不允许重复，引用必须已声明；capacity=0..500。仅双方列入对方才互相可接受，未列入视不可接受，不是并列或最低优先级。
申请方提出的延迟接受算法；被拒者继续下一志愿，项目保留最高优先级至容量。初始申请方字典序保证证据确定，输出分配、未分配、各项目已接收及提议日志；用独立全对检查输出blocking_pairs。阻塞对需申请者更偏好该项目且项目有空位或更偏好其于当前最差者。
样例B→X、A→Y、C未分配，无阻塞对。它保证给定严格可接受偏好下的稳定性，不保证最大人数、社会效用、群体公平或真实招生合规；不连接真实人员系统。

