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

## 当前业务与样例
# Applicant-proposing matching v1

将不完整严格志愿表与项目优先级、限额匹配，供名额分配人员离线核验。最多500名applicants(id,preferences)、1..100个programs(id,capacity,ranking)。两侧id唯一、排序不允许重复，引用必须已声明；capacity=0..500。仅双方列入对方才互相可接受，未列入视不可接受，不是并列或最低优先级。
申请方提出的延迟接受算法；被拒者继续下一志愿，项目保留最高优先级至容量。初始申请方字典序保证证据确定，输出分配、未分配、各项目已接收及提议日志；用独立全对检查输出blocking_pairs。阻塞对需申请者更偏好该项目且项目有空位或更偏好其于当前最差者。
样例B→X、A→Y、C未分配，无阻塞对。它保证给定严格可接受偏好下的稳定性，不保证最大人数、社会效用、群体公平或真实招生合规；不连接真实人员系统。

