# 代码执行客户端：execution_client 协议与判分适配

## 1. 本讲目标

本讲精读 PR [#2248](https://github.com/allenai/open-instruct/pull/2248)（提交 `bcbf21543`）移入仓库的 `open_instruct/code_utils/execution_client` 包。学完本讲你应该能够：

1. 说清客户端与服务端的**职责边界**：客户端只做编码、校验与判定汇总，**绝不执行任何模型生成的代码**——执行只发生在外部判分服务里。
2. 掌握错误二分类模型：`ServiceError`（429/5xx/传输错误/坏响应，重试耗尽后上抛）与 `ClientError`（4xx，立即失败），并理解为什么**服务故障绝不能被当成测试失败**。
3. 理解 `protocol.py` 的线格式限制与受限 unpickler 构成的安全模型。
4. 会用 `CodeVerifier` 注册表适配器打分，并用 `livecodebench` / `nemo_comparison` 构造带标签的测试。

本讲是 u6-l7 的续篇：u6-l7 讲的是「代码奖励的执行与沙箱判定」，本讲专门讲这套判分背后**新入库的客户端库本身**。

## 2. 前置知识

### 2.1 HTTP 状态码与重试语义

- **429**：请求过多（限流），稍后重试通常能恢复。
- **5xx**：服务端错误（500 内部错误、502/504 网关超时等），重试可能恢复。
- **4xx**：客户端错误（401 未认证、413 载荷过大等），重试没有意义——请求本身就是错的。
- **指数退避（exponential backoff）与 full jitter**：第 \(k\) 次失败后等待时间上限按 \(b \cdot 2^{k-1}\) 增长，实际取 \(U(0, \min(b \cdot 2^{k-1}, r))\) 均匀随机（\(r\) 为剩余时间预算）。加入随机抖动（jitter）是为了避免大量调用方在同一时刻同步重试，压垮刚恢复的服务。

### 2.2 pickle 反序列化的风险

Python 的 `pickle.loads` 在解出一个对象引用（`GLOBAL`/`STACK_GLOBAL` 操作码）时会按 `模块名.类名` 动态导入并查找任意类，因此**反序列化不可信的 pickle 数据等同于执行任意代码**。防御手段之一是继承 `pickle.Unpickler` 并重写 `find_class`，只放行一个极小的白名单——本讲的 `_RestrictedUnpickler` 就是这么做的。

### 2.3 为什么「0 分」和「故障」必须区分

在 RLVR 训练里，奖励是梯度信号。如果判分服务宕机时把样本记成 0 分，策略会被错误地惩罚——模型会「学到」正确的代码也是错的。所以本包的设计哲学写在模块文档里：服务故障要么**掩掉该样本**、要么**中止训练**，唯独不能静默变成测试失败。对应地，「模型输出里抽不出合法 Python」是一种**已完成的 0 分**（合法训练信号），两者必须区分。

### 2.4 注册表适配器约定（承接 u6-l3 / u6-l7）

open-instruct 的验证器注册表约定：验证器类提供 `get_config_class()` 返回配置 dataclass，构造时传入配置实例，调用接口为 `async_call(tokens, prediction, label, **kwargs)`，返回带 `score` / `cost` / `diagnostics` 的对象。本讲 `verifier.py` 的 `CodeVerifier` 正是按这个约定接入的适配器。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [open_instruct/code_utils/execution_client/__init__.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py) | 客户端主体：`CodeExecutionClient`、`ServiceError`/`ClientError`、`Outcome`/`ExecuteResult`、重试与分批逻辑 |
| [open_instruct/code_utils/execution_client/protocol.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/protocol.py) | 线格式常量、三种编码的测试解码、测试形态校验、时限钳制；文档自明「无任何执行实现」 |
| [open_instruct/code_utils/execution_client/verifier.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/verifier.py) | 注册表适配器：`extract_python_code` 代码抽取、`ServiceConfig`、`CodeVerifier` 打分 |
| [open_instruct/code_utils/execution_client/livecodebench.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/livecodebench.py) | 把 LiveCodeBench 形状的 inputs/outputs（可选 fn_name）编译成 assert/stdio 测试标签 |
| [open_instruct/code_utils/execution_client/nemo_comparison.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/nemo_comparison.py) | NeMo 容差比较函数（源码形式被嵌入生成的断言测试） |
| open_instruct/test_code_execution_client*.py（4 个文件） | 单元测试，全部用 mock 传输层，不需要真实服务 |
| [open_instruct/miles/rewards/complete_code.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rewards/complete_code.py) | 上游消费者（u6-l7 已精读），本讲用它验证适配器的接入方式 |

包内每个文件的第一行都有出处注释，例如 `# Client-side code ported from code-execution-service revision 103ca279...`——这个包原本是外部私有服务仓库里的客户端代码，被原样搬进了 open-instruct，便于与部署的服务端保持线格式一致。

## 4. 核心概念与源码讲解

### 4.1 CodeExecutionClient：认证、重试与错误二分类

#### 4.1.1 概念说明

`CodeExecutionClient` 是判分服务的 HTTP 客户端。它的职责被刻意收窄为三件事：

1. **编码**：把程序 + 测试列表编码成服务端接受的请求载荷（必要时分批、压缩）。
2. **校验**：对响应做严格的结构校验，不信任任何字段。
3. **判定汇总**：把各批结果按顺序拼成 `ExecuteResult`，供上层换算分数。

它**不执行代码**。模块文档开头就给出用法与错误哲学：

> Service faults (429, 5xx, transport errors, timeouts) raise ServiceError after retries so the caller must mask the sample or abort training; they are never silently turned into test failures. Other client errors (4xx) raise ClientError immediately.

认证用 Bearer 令牌（`Authorization` 头），令牌值只进头、不进载荷，出错时还会从错误文本里打码。

#### 4.1.2 核心流程

调用 `execute(mode, program, tests, per_test_seconds=...)` 的整体流程：

```
async_call（上层适配器）
   │
   ▼
execute()                          ── 客户端（本层，绝不执行代码）
   ├─ _batches()                   ── 解码+校验 tests → 按时间/大小预算分批
   │     ├─ protocol.decode_tests  ── 列表 / JSON 串 / zlib+pickle 旧编码
   │     ├─ protocol.validate_tests── 数量/形态/比较方式校验
   │     ├─ count = min(max_batch_tests, ⌊(S_req−2)/(t+0.1)⌋)
   │     └─ fit()：超限时 zlib 压缩 → 仍超限则二分递归拆批
   ├─ 对每一批 _post("/v2/execute", payload, validate, deadline)
   │     ├─ 200 → 能力协商头 + 源码指纹头 + 结构校验 → 通过才接受
   │     ├─ 429 / ≥500 / <400 / 传输异常 / 响应非法 → ServiceError（可重试）
   │     ├─ 其他 4xx → ClientError（立即失败，不重试）
   │     └─ 重试等待 = U(0, min(backoff·2^(k−1), 剩余deadline))，full jitter
   └─ 按批序拼接 outcomes → ExecuteResult(pass_fraction, request_ids, attempts, wall_seconds)
                                        │
                                        ▼
                              HTTP POST /v2/execute ──► 服务端沙箱（唯一执行代码的地方）
```

关键的不变式：

- **总尝试次数 = `retries + 1`**：`for attempt in range(1, self.retries + 2)`，即 `retries=2` 时最多发 3 次请求。
- **一个批次一个 `X-Request-Id`**：重试复用同一个 id（服务端可去重/追踪），换批次才换新 id。
- **deadline 跨批共享**：`execute()` 入口用 `time.monotonic() + total_timeout` 设一个总期限，所有批次、所有重试、所有等待共用；任何时刻超期都抛 `ServiceError("... deadline exceeded")`，已耗时间的「迟到成功响应」也不接受。
- **分批大小公式**：\( n_{\text{batch}} = \min\left(N_{\max},\ \left\lfloor \frac{S_{\text{req}} - 2}{t + 0.1} \right\rfloor\right) \)，其中 \(S_{\text{req}}\) 是单请求时间预算（默认 20s），\(t\) 是单测试时限。预留的 2 秒给进程启动/清理开销，0.1 秒是每个解释器的固定开销。

#### 4.1.3 源码精读

**入口文档与用法示例**——[open_instruct/code_utils/execution_client/__init__.py:L1-L12](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L1-L12)：模块文档给出最小用法（构造客户端 → `execute` → 读 `all_passed`/`outcomes`），并写明错误二分类哲学。这是整个包的「设计声明」。

**两个错误类型**——[__init__.py:L73-L82](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L73-L82)：`ServiceError(RuntimeError)` 携带 `status/attempts/request_id`，表示「服务侧故障，重试后仍失败」；`ClientError(ValueError)` 携带 `status/request_id`，表示「请求本身不合法」。注意基类不同：前者是运行时故障，后者本质是调用方传参错误。

顺带一提 [__init__.py:L29](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L29) 定义的 `RETRY_STATUSES = {429, 500, 502, 503, 504}` 只是意图声明，`_post` 里真正生效的分类条件是更宽的行内表达式（见下）。

**结果数据类**——[__init__.py:L85-L111](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L85-L111)：`Outcome(status, runtime, detail)` 的 `passed` 属性只认 `"passed"`；`ExecuteResult` 提供

\[ \text{pass\_fraction} = \frac{\#\{i : \text{status}_i = \text{passed}\}}{n} \]

且空结果集返回 0.0（`all_passed` 对空集为 False），保证「没有测试」永远拿不到满分。

**构造参数与防御**——[__init__.py:L114-L155](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L114-L155)：所有数值参数必须有限且为正，`max_batch_tests` 被夹在 `[1, protocol.MAX_TESTS]`；还接受 `expected_source_sha256`（部署指纹，见下）与 `cancel_event`（协作式取消）。`token` 可为 None（本地开发服务可不认证）。

**认证与令牌打码**——[__init__.py:L161-L169](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L161-L169)：`_headers` 只在 `X-Request-Id` 和 `Content-Type` 之外加 `Authorization: Bearer <token>`；`_safe_error` 把错误文本里出现的令牌替换成 `<redacted>`——因为服务端错误页可能回显请求头。

**重试主循环**——[__init__.py:L171-L242](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L171-L242)，逐段看：

- [L176-L187](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L176-L187)：每次尝试前检查取消与剩余期限；`session.post` 的单请求超时取 `min(self.timeout, remaining)`，保证单次等待不会击穿总 deadline。
- [L188-L191](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L188-L191)：传输层异常（连接重置、DNS 失败等 `requests.RequestException`）包装成 `ServiceError`，同样打码。
- [L193-L217](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L193-L217)：**HTTP 200 也不直接信任**。三道检查：
  - [L196-L200](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L196-L200) 能力协商：stdio 模式下若测试要求了非 `exact` 的比较方式（如 `nemo_v1`），响应头 `X-Service-Comparisons` 必须声明支持，否则视为无效判分；
  - [L201-L205](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L201-L205) 部署指纹：配置了 `expected_source_sha256` 时，响应头 `X-Service-Source-Sha256` 必须完全一致——防止打到错误版本/错误部署的服务上；
  - [L206-L207](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L206-L207) 结构校验（`_validate_v2`）。
  
  任一检查失败都进入 [L208-L211](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L208-L211)：包装成 `ServiceError("invalid grading response: ...")` 并**参与重试**——「拿到 200」不等于「拿到有效判分」。
- [L213-L217](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L213-L217)：响应到达时已超期的，同样拒绝——迟到的好消息不算数。
- [L218-L230](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L218-L230)：状态分类的核心一行——`429 或 ≥500 或 <400`（意外的 3xx 重定向也算服务侧异常）记为可重试的 `ServiceError`；**其余（即 4xx）立即抛 `ClientError`，本次调用直接失败**。
- [L231-L239](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L231-L239)：重试等待 `delay = random.uniform(0, min(backoff·2^(attempt−1), remaining))`（full jitter，且不让等待延长 deadline）；有 `cancel_event` 时用 `event.wait(delay)` 替代 `sleep`，取消能立刻打断等待。
- [L240-L242](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L240-L242)：循环走完仍失败，抛出最后一次的 `ServiceError`。

**响应结构校验**——[__init__.py:L54-L70](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L54-L70)：`_validate_v2` 要求 `outcomes` 数量与请求测试数一致；`wall_seconds` 有限、`exit` 是字符串；`exit in ("wall_timeout","runner_error")` 或存在 `runner_error` 说明**执行不完整**，整体判为无效（这正是「服务故障≠测试失败」在数据层面的落实）；每个 outcome 的 `status` 必须属于 `{passed, failed, timeout, error, crashed}`——`error/crashed` 表示该测试没能正常跑完，不等于 `failed`。[_validate_v1](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L39-L51) 是旧版接口的对应校验（results 只能是 0/1）。

**分批与体积控制**——[__init__.py:L244-L306](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L244-L306)：

- [L245-L254](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L245-L254)：客户端本地校验失败（`protocol.BadRequest`）转成 `ClientError(..., 400)`——问题在请求，不该重试。
- [L257-L259](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L257-L259)：按时间预算算单批测试数（见 4.1.2 公式），算出 0 说明单测试时限放不进请求预算，立即 `ClientError`。
- [L261-L282](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L261-L282)：`payload_for` 生成 v1/v2 两种线格式，并携带 `required_comparisons` 能力声明。
- [L284-L299](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L284-L299)：`fit` 先试明文 JSON，超限则把 tests 压成 `zlib+json` base64 再试；仍超限就二分递归拆批；**只剩一个测试还超限**才抛 `ClientError(..., 413)`——且这一切发生在**发出任何请求之前**。
- [L301-L306](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L301-L306)：按顺序切批、逐批 fit，保证批与批之间测试顺序不乱。

**execute 主入口与 v1 兼容层**——[__init__.py:L318-L334](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L318-L334)：`execute` 是 v2 接口（`/v2/execute`），跨批汇总 outcomes、累加 attempts/wall、保留全量 `request_ids`，`exit_kind` 取第一个非 `"ok"` 的退出类型。[L337-L356](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L337-L356) 的 `test_program` / `test_program_stdio` 是 v1 兼容助手，返回旧服务 `(results, runtimes)` 形状，方便老调用方迁移。另有一个不走重试的 [health()](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L308-L311) 探活。

#### 4.1.4 代码实践

**实践目标**：用 `unittest.mock` 拦截 HTTP 层，亲眼验证 429（重试后成功）与持续 5xx（ServiceError）两种行为，并确认「服务故障不会变成 0 分」。

**操作步骤**（以下为示例代码，保存为本地脚本运行，不需要真实服务）：

```python
# 示例代码：mock_service_drill.py
from unittest.mock import Mock

from open_instruct.code_utils.execution_client import ClientError, CodeExecutionClient, ServiceError


def response(status, payload=None):
    return Mock(status_code=status, text="response", json=Mock(return_value=payload))


OK = {
    "outcomes": [
        {"status": "passed", "runtime": 0.01, "detail": None},
        {"status": "passed", "runtime": 0.01, "detail": None},
        {"status": "passed", "runtime": 0.01, "detail": None},
    ],
    "wall_seconds": 0.05,
    "exit": "ok",
}
PROGRAM = "def add(a, b):\n    return a + b\n"
TESTS = ["assert add(1, 2) == 3", "assert add(0, 0) == 0", "assert add(-1, 1) == 0"]

# 场景 A：先 429 后 200 —— 重试后成功
session = Mock()
session.post.side_effect = [response(429), response(200, OK)]
client = CodeExecutionClient("http://unused", session=session, retries=2, backoff=0)
result = client.execute("assert", PROGRAM, TESTS)
print("A:", result.all_passed, result.pass_fraction, session.post.call_count)

# 场景 B：持续 503 —— 重试耗尽后抛 ServiceError，绝不返回「0 分」
session = Mock()
session.post.return_value = response(503)
client = CodeExecutionClient("http://unused", session=session, retries=2, backoff=0)
try:
    client.execute("assert", PROGRAM, TESTS)
    print("B: 不该走到这里")
except ServiceError as error:
    print("B: ServiceError", error.status, error.attempts)

# 场景 C：401 —— ClientError，且一次重试都不做
session = Mock()
session.post.return_value = response(401)
client = CodeExecutionClient("http://unused", session=session, retries=2, backoff=0)
try:
    client.execute("assert", PROGRAM, TESTS)
except ClientError as error:
    print("C: ClientError", error.status, session.post.call_count)
```

**需要观察的现象**：三行输出分别反映重试恢复、故障上抛、快速失败。

**预期结果**（依据 [open_instruct/test_code_execution_client.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_code_execution_client.py) 的既有断言推得）：

- 场景 A 打印 `A: True 1.0 2`（3 个测试在默认时间预算下装进同一批，所以只有「失败 + 成功」两次 POST）；
- 场景 B 打印 `B: ServiceError 503 3`（`retries=2` → 共 3 次尝试；对照测试 `test_exhausted_throttling_is_a_service_error`）；
- 场景 C 打印 `C: ClientError 401 1`（4xx 不重试；对照 `test_invalid_authentication_still_fails_without_retry`）。

**练习提示**：可以把场景 B 的 mock 换成 `response(200, {"outcomes": [{"status": "failed", "runtime": 0.0, "detail": "boom"}] * 3, "wall_seconds": 0.1, "exit": "ok"})`，对比「真实测试失败」（返回 `pass_fraction=0.0`，不抛异常）与「服务故障」（抛异常）的区别。若本地行为与预期不符，请以实际输出为准并回读源码。

#### 4.1.5 小练习与答案

**练习 1**：`retries=2` 时对一批测试最多发出几次 HTTP 请求？为什么？

答案：3 次。`_post` 的循环是 `range(1, retries + 2)`，总尝试次数 = `retries + 1`；且每次尝试前都检查共享 deadline，超期会提前以 ServiceError 终止。

**练习 2**：为什么 HTTP 200 的响应也可能被包装成 `ServiceError` 并参与重试？列举至少三种情况。

答案：因为「状态码 200」不代表「拿到了有效判分」。至少有：① 结构校验失败（`outcomes` 数量不符、含 `error/crashed` 之类未执行完的状态、`exit=wall_timeout/runner_error`、JSON 解析失败）；② 能力协商失败（服务不支持 `nemo_v1` 比较）；③ 部署指纹不符（`X-Service-Source-Sha256` 与 `expected_source_sha256` 不一致）；④ 响应到达时已超过共享 deadline。

**练习 3**：`RETRY_STATUSES` 常量和 `_post` 里实际生效的分类条件一致吗？

答案：不完全一致。常量列了 `{429, 500, 502, 503, 504}`，但真正生效的行内条件是 `status == 429 or status >= 500 or status < 400`——比常量更宽（任何 ≥500 与任何 <400 的意外状态码都算服务侧异常）。读源码时要以实际执行路径为准。

### 4.2 protocol：线格式限制与受限反序列化

#### 4.2.1 概念说明

`protocol.py` 是「线格式契约」的纯客户端表述：服务端能接受什么、拒绝什么，这里原样复刻一份。文件文档第一行就声明：*Client-side wire limits, test decoding and validation; **no execution implementation***。它回答三个问题：

1. 一份「测试」在传输前允许长什么样（数量、形态、时限、比较方式）？
2. 历史数据里的三种编码如何统一解码？
3. 解码不可信数据时如何不引入任意代码执行风险？

#### 4.2.2 核心流程

`decode_tests(tests)` 的解码决策树：

```
tests 是 None / ""        → 空列表
tests 是 list             → 原样返回
tests 是 dict 且 encoding=="zlib+json"
                           → base64 解码 → zlib 解压（限 128MiB）→ json.loads → 必须是 list
tests 是字符串             → 先试 json.loads（必须是 list）
                             失败 → 再试 base64(zlib(pickle(list)))
                                     → _RestrictedUnpickler 加载（白名单外的 GLOBAL 一律拒绝）
任何失败 → BadRequest（ValueError 子类），由上层转成 ClientError(400)
```

校验规则（`validate_tests`）按模式分叉：

| 模式 | 每个测试的合法形态 | 额外约束 |
| --- | --- | --- |
| `assert` | 字符串 | 总数 ≤ `MAX_TESTS`(2000) |
| `stdio` | 含 `input`/`output` 的对象 | `input` 是字符串或字符串列表；`output` 是字符串；`comparison ∈ {exact, nemo_v1}` |
| 其他 | — | 直接 `BadRequest("unknown mode ...")` |

时限钳制：`clamp_per_test_seconds` 把任意数值夹进 \([0.05, 60.0]\)（秒），NaN/Inf/非数值直接拒绝。

#### 4.2.3 源码精读

**线格式常量**——[open_instruct/code_utils/execution_client/protocol.py:L10-L15](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/protocol.py#L10-L15)：`COMPARISONS = ("exact", "nemo_v1")`、`MAX_TESTS = 2000`、单测试时限范围、`MAX_DECODED_TEST_BYTES = 128 << 20`（128 MiB 解压上限）。这些常量就是客户端与服务的共同契约，`CodeExecutionClient` 构造时校验 `max_batch_tests` 就引用了 `MAX_TESTS`。

**受限 unpickler**——[protocol.py:L22-L30](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/protocol.py#L22-L30)：白名单只有 `builtins` 下的 `set` / `frozenset` / `bytearray` 三个纯数据容器；`find_class` 对其他任何 `模块.类名` 引用抛 `UnpicklingError`。这意味着即使数据集标签里被塞了恶意 pickle（经典攻击：`__reduce__` 引用 `os.system`），加载阶段就会被掐断。

**防压缩炸弹的解压**——[protocol.py:L33-L40](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/protocol.py#L33-L40)：`decompressobj.decompress(data, MAX_DECODED_TEST_BYTES + 1)` 限制解压产出长度，超出即拒绝；同时要求 `eof`（流完整）且无 `unused_data`（无尾随垃圾）；`base64.b64decode(..., validate=True)` 拒绝脏 base64。

**三编码统一入口**——[protocol.py:L43-L75](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/protocol.py#L43-L75)：`decode_tests` 接受历史上客户端发过的三种形态（列表、JSON 字符串、`base64(zlib(pickle(list)))` 旧编码），全部失败路径统一收敛为 `BadRequest`，异常信息只暴露异常类型名（`tests could not be decoded: UnpicklingError`），不回显数据本身。

**形态校验与模式分派**——[protocol.py:L78-L99](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/protocol.py#L78-L99)：见 4.2.2 表格；注意 `comparison` 字段缺省视为 `"exact"`，但一旦出现就必须在 `COMPARISONS` 里——`test_code_execution_client_labels.py` 的 [L30-L32](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_code_execution_client_labels.py#L30-L32) 专门验证「未知比较方式在任何网络传输之前就被拒绝」。

**时限钳制**——[protocol.py:L102-L109](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/protocol.py#L102-L109)：非有限值（含 `10**400` 这类溢出为 `inf` 的写法，见 [test_code_execution_client_batching.py:L96-L99](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_code_execution_client_batching.py#L96-L99)）一律 `BadRequest`，合法值夹进上下限。

#### 4.2.4 代码实践

**实践目标**：不碰任何网络，亲手走一遍三种编码的解码路径，并验证受限 unpickler 对越权类的拒绝。

**操作步骤**（示例代码，可直接在 `uv run python` 交互环境执行）：

```python
# 示例代码
import base64
import pickle
import zlib

from open_instruct.code_utils.execution_client import protocol

# 1) 列表与 JSON 字符串：最平凡的两种编码
print(protocol.decode_tests(["assert True"]))
print(protocol.decode_tests('["assert True"]'))

# 2) zlib+json 编码（客户端 fit() 压缩后同款形态）
packed = base64.b64encode(zlib.compress(b'["assert True"]')).decode()
print(protocol.decode_tests({"encoding": "zlib+json", "data": packed}))

# 3) 旧 pickle 编码：白名单容器（frozenset）可以过
legacy_ok = base64.b64encode(zlib.compress(pickle.dumps([frozenset({1, 2})]))).decode()
print(protocol.decode_tests(legacy_ok))

# 4) 旧 pickle 编码：引用任意类 → 必须被 BadRequest 拒绝
class Marker:
    pass


legacy_evil = base64.b64encode(zlib.compress(pickle.dumps([Marker()]))).decode()
try:
    protocol.decode_tests(legacy_evil)
except protocol.BadRequest as error:
    print("rejected:", error)
```

**需要观察的现象**：前四步都解出列表；第 5 步抛 `BadRequest`，信息形如 `tests could not be decoded: UnpicklingError`（`Marker` 走 `__main__.Marker` 的 GLOBAL 查找，不在白名单）。

**预期结果**：与上述一致。pickle 对「按引用腌制」的类实例会在加载时触发 `find_class('__main__', 'Marker')`，被白名单拦下。若想复现解压上限，可参考 [test_code_execution_client_batching.py:L89-L93](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_code_execution_client_batching.py#L89-L93)（monkeypatch 掉 `MAX_DECODED_TEST_BYTES` 后喂高压缩比载荷）。

#### 4.2.5 小练习与答案

**练习 1**：为什么客户端要保留对 pickle 旧编码的支持，而不是强制所有人用 JSON？

答案：数据集标签是历史产物——旧客户端曾经以 `base64(zlib(pickle(list)))` 发送测试。为了不破坏既有数据，解码兼容三种形态；但加载侧加了受限 unpickler 与解压上限，把兼容性的安全成本压到最低。

**练习 2**：`_decompress` 同时检查 `len(raw) > MAX`、`unconsumed_tail`、`not eof`、`unused_data` 四个条件，各防什么？

答案：前两个防「解压后超限」（压缩炸弹：小的压缩流展开成超大 payload）；`not eof` 防截断的压缩流被静默接受；`unused_data` 防压缩流后面拼接的尾随数据。四者合起来保证「解出来的恰好是一个完整且不超限的载荷」。

**练习 3**：`validate_tests` 为什么要求 stdio 的 `output` 必须是字符串，而 `input` 允许字符串或字符串列表？

答案：`output` 是期望的标准输出，服务端逐行/整体比对，必须是文本；`input` 是喂给进程的标准输入，历史上既有整段文本也有「按行给出」的写法，两种都被服务接受，所以客户端校验放宽到两种形态，但列表元素必须仍是字符串。

### 4.3 CodeVerifier：Open Instruct 注册表适配器

#### 4.3.1 概念说明

`verifier.py` 把裸客户端包装成 open-instruct 验证器注册表能用的形状：

- `extract_python_code`：从模型输出（Markdown 混文本）中**抽取**要判分的 Python 程序——只做词法/AST 级分析，绝不执行；
- `ServiceConfig`：面向训练配置的 dataclass（服务地址、时限、阈值、令牌环境变量名等）；
- `CodeVerifier`：实现 `async_call` 的打分适配器，把 `prediction` + `label`（测试列表）变成 `score/diagnostics`。

它的失败策略与客户端一脉相承，文档写明：*This adapter never substitutes a zero reward for an unavailable or incomplete grade*（绝不用 0 分顶替拿不到的判分）。

#### 4.3.2 核心流程

`async_call(tokens, prediction, label)` 的状态机：

```
校验配置（阈值/时限/total_timeout）          ── 配置错 → ValueError
    │
label → decode_tests + validate_tests       ── 坏标签 → ClientError/ValueError
    │  空测试集 → ValueError（"requires at least one test"）
    │
从环境变量取令牌（token_env，默认 CODE_EXECUTION_TOKEN）
    │  缺令牌 → ValueError（宁可失败也不匿名调用）
    │
extract_python_code(prediction)
    │  抽不出/为空 → 返回 completed 0 分（diagnostics.outcome = "invalid_code"）
    ▼
asyncio.to_thread(execute)                  ── 阻塞的 requests 调用放进线程
    │  asyncio 取消 → 置 cancel_event，放干在途请求，上抛 CancelledError
    │  ServiceError → 原样上抛（不折算成分数！）
    ▼
score = pass_fraction
    ├─ score < threshold → 0.0
    └─ 否则可选 perf penalty：
      score = (1/n) · Σᵢ max(0, 1 − rᵢ/T) · 1[passedᵢ]
    ▼
返回 SimpleNamespace(score, cost=0.0, diagnostics={status, tests, request_ids, attempts, wall_seconds})
```

这里出现了本讲最重要的三分法：

| 情形 | 结果 | 语义 |
| --- | --- | --- |
| 抽不出合法 Python | `score=0.0`，`status="completed"`，`outcome="invalid_code"` | **已完成的 0 分**：模型确实没交出可判程序，是合法训练信号 |
| 部分测试未通过 | `score=pass_fraction`（可能被阈值整流为 0） | 正常判分 |
| 服务故障/超时/取消 | 抛 `ServiceError` | **没有判分**：调用方掩样本或中止训练 |

#### 4.3.3 源码精读

**代码抽取**——[open_instruct/code_utils/execution_client/verifier.py:L27-L71](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/verifier.py#L27-L71)：`extract_python_code` 的选择规则：① 先 `ast.parse` 判定整段输出是否本身就是合法 Python（是则原样返回——防止把「代码字符串里的 ``` 围栏」误当 Markdown）；② 剥掉**末尾**解码残留的模型结束符（`<|endoftext|>` 等，仅限结尾）；③ 逐行扫描围栏，收集 Python 围栏（```python/python3/py）与无语言围栏，**取最后一 个完整 Python 围栏**，其次最后一个无标签围栏；④ 显式声明其他语言的围栏不算 Python；⑤ 围栏未闭合 → 返回 `None`（残缺代码不回退到更早草稿）。这 18 条规则全部参数化在 [test_code_execution_client_verifier.py:L76-L100](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_code_execution_client_verifier.py#L76-L100) 的表里。

**服务配置**——[verifier.py:L74-L87](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/verifier.py#L74-L87)：`ServiceConfig` 关键字段：`api_url`、`stdio`（断言式还是 stdio 式）、`max_execution_time`（默认 1s）、`pass_rate_reward_threshold`（默认 0.99——几乎全对才算对）、`apply_perf_penalty`、`token_env`（默认 `"CODE_EXECUTION_TOKEN"`）、以及透传给客户端的预算与指纹字段。

**构造校验**——[verifier.py:L90-L102](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/verifier.py#L90-L102)：阈值必须是 \([0,1]\) 内的有限数；单测试时限必须在服务支持的 \([0.05, 60]\) 范围内；`total_timeout` 必须有限为正。配置错误在构造期就爆，不拖到训练中途。

**打分主流程**——[verifier.py:L104-L120](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/verifier.py#L104-L120)：按 4.3.2 状态机走：解码并校验 label、拒绝空测试集、取令牌、抽代码；抽不出代码时返回 `completed/invalid_code` 的 0 分——注意这一步**不会**调用服务（[test_code_execution_client_verifier.py:L154-L165](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_code_execution_client_verifier.py#L154-L165) 断言此时连 HTTP 会话都不能打开）。

**线程执行与取消**——[verifier.py:L122-L157](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/verifier.py#L122-L157)：`execute` 闭包在**每次调用**里新建 `requests.Session`（用完即关），剥掉 `api_url` 上可能带的旧路由后缀再构造 `CodeExecutionClient`，把剩余 deadline 传进去；`await asyncio.to_thread(execute)` 让同步 HTTP 不阻塞事件循环。取消语义很讲究：`requests` 无法中断在途请求，所以捕获 `CancelledError` 后只**置 cancel_event**再上抛——在途的那一次调用按自己的超时放干，但后续批次与重试全部掐断（对照 [test_code_execution_client_verifier.py:L13-L57](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_code_execution_client_verifier.py#L13-L57)：取消后 `session.post` 恰好只被调用 1 次，会话在响应放干后才关闭）。排队等待也算时间：worker 线程排队超期同样抛 `ServiceError("... waiting for a worker")`（[L125-L127](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/verifier.py#L125-L127)，对照 [test:L60-L73](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_code_execution_client_verifier.py#L60-L73)）。

**分数计算与诊断**——[verifier.py:L158-L176](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/verifier.py#L158-L176)：先取 `pass_fraction`，低于阈值整流为 0；开启 `apply_perf_penalty` 时按运行时间打折（公式见 4.3.2，越接近时限折扣越大）。`diagnostics` 携带 `request_ids/attempts/wall_seconds`，给后续审计留证据链。

**上游消费者**——[open_instruct/miles/rewards/complete_code.py:L99-L108](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rewards/complete_code.py#L99-L108)：MILES 的 `CompleteCodeVerifier` 每次打分时现场构造 `CodeVerifier(ServiceConfig(...))`，硬编码 `pass_rate_reward_threshold=1.0`（全过才算对）与 `fresh_process_per_test=True`（每个测试独立进程，防测试间污染）；并在 [L113-L125](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rewards/complete_code.py#L113-L125) 按 2000 个一组分区判分、分数连乘、把非 `ok/completed` 状态归为 `service_error`。这是「服务故障穿透、不折算分数」在 MILES 侧的最后一道闸门（详见 u6-l7 与 u8-l6）。

#### 4.3.4 代码实践

**实践目标**：离线验证 `extract_python_code` 的抽取规则与 `invalid_code` 零分路径。

**操作步骤**（示例代码，纯本地、无网络）：

```python
# 示例代码
import asyncio
import os

from open_instruct.code_utils.execution_client import verifier

# 1) 抽取规则速览：对比正反例
for response in [
    "```python\nprint(5)\n```\n```text\n5\n```",   # 多个围栏，取最后一个 Python
    "```python\nprint(1)\n```\n```python\nprint(",  # 最后一个 Python 围栏未闭合 → None
    "```javascript\nconsole.log(5)\n```",           # 显式非 Python → None
    "```python\ndef f():\n    return 1\n```<|endoftext|>",  # 结束符要剥掉
]:
    print(repr(verifier.extract_python_code(response)))

# 2) invalid_code 是「完成的 0 分」，且不触碰服务
os.environ["CODE_EXECUTION_TOKEN"] = "dummy"

from unittest.mock import Mock  # noqa: E402  （示例代码，简化演示）

verifier.CodeExecutionClient.execute = Mock(side_effect=AssertionError("不该被调用"))
adapter = verifier.CodeVerifier(verifier.ServiceConfig(api_url="http://local"))
result = asyncio.run(adapter.async_call([], "```python\nprint(", ["assert True"]))
print(result.score, result.diagnostics)
```

**需要观察的现象**：第一步四个输出分别是 `'print(5)'`、`None`、`None`、`'def f():\n    return 1'`；第二步打印 `0.0 {'status': 'completed', 'outcome': 'invalid_code'}`，且 mock 从未被调用。

**预期结果**：与上表一致（与 [test_code_execution_client_verifier.py:L76-L100、L154-L165](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_code_execution_client_verifier.py#L76-L100) 的断言同源）。正式写脚本时建议改用 `unittest.mock.patch.object` 而非直接赋值，避免污染模块状态。

#### 4.3.5 小练习与答案

**练习 1**：同样得不到分数，为什么「抽不出 Python」返回 0 分，而「服务 503」必须抛异常？

答案：抽不出 Python 是**模型的行为**（输出里没有合法程序），这是确定的训练信号——惩罚它是正确的；服务 503 是**基础设施的状态**，与模型输出无关，若折算成 0 分会让策略学到「好代码也会被惩罚」。所以前者是 completed 0 分，后者必须让调用方掩样本或中止。

**练习 2**：为什么用 `asyncio.to_thread` + `cancel_event`，而不是找一个原生异步 HTTP 库？

答案：判分链路要与 open-instruct 其余验证器共用 `requests` 生态（会话、代理、重试补丁），而 `requests` 是同步库且不能中断在途调用。折中方案：同步调用放线程池；取消时置事件——在途请求按自身超时放干（结果作废），后续批次与重试全部停止，既不泄漏也不误判。

**练习 3**：`async_call` 为什么拒绝空测试集，而不是直接返回满分（「没有测试就是全过」）？

答案：空标签大概率是数据制备事故。`ExecuteResult.pass_fraction` 对空集本来就返回 0.0，`all_passed` 为 False——语义上「没有证据」不等于「通过」。显式抛 `ValueError` 把事故暴露在数据准备阶段，比在训练中静默给 0/1 分都安全。

### 4.4 livecodebench 与 nemo_comparison：测试标签构造

#### 4.4.1 概念说明

有了客户端和适配器，还差一样东西：**数据集里的标签怎么变成可执行的测试**？LiveCodeBench 风格的数据自带 `inputs` / `outputs`（可选 `fn_name`），`livecodebench.py` 负责把它们编译成服务端认识的 assert/stdio 测试；`nemo_comparison.py` 提供 NeMo 风格的浮点容差比较函数，其**源码会被嵌入**生成的断言测试里随标签一起传输。

设计要点：比较语义属于**数据集**（docstring：*Dataset-owned NeMo semantics; no training configuration or tolerance knobs*），不暴露任何容差旋钮给训练配置，保证同一份数据在任何环境里判分一致。

#### 4.4.2 核心流程

`prepare_tests(inputs, outputs, fn_name=None, comparison="exact")` → `(mode, tests)`：

```
inputs/outputs 必须等长、非空、元素为字符串          ── 否则 ValueError
    │
    ├─ fn_name 为 None ──► stdio 模式
    │     tests = [{"input": i, "output": o} for i, o in zip(...)]
    │     nemo_v1 时每项加 "comparison": "nemo_v1"
    │
    └─ fn_name 为合法标识符 ──► assert 模式（生成的每个测试是一段 Python 源码）
          对第 k 组 (input, output)：
            args    = [json.loads(line) for line in input.splitlines()]   # 每行一个 JSON 位置参数
            want    = json.loads(output)                                  # 期望返回值（单个 JSON）
            断言体  = 调 Solution().fn_name（若定义了 Solution）否则模块级 fn_name
                      exact：assert json.loads(json.dumps(actual)) == want   # JSON round-trip 后比较
                      nemo_v1：把 _nemo_values_close/_nemo_call_outputs_match
                               的源码 exec 进命名空间，用其比较
```

NeMo 容差（`_nemo_values_close`）对标 NumPy `allclose` 的标量语义：

\[ |p - e| \le 10^{-8} + 10^{-5}\,\lvert e\rvert \]

且仅当两侧都是数值（bool 除外）时启用容差；否则退回严格相等 / JSON 序列化相等 / 逐元素递归。

#### 4.4.3 源码精读

**NeMo 入口**——[open_instruct/code_utils/execution_client/livecodebench.py:L11-L13](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/livecodebench.py#L11-L13)：`prepare_nemotron_tests` 只是 `prepare_tests(..., comparison="nemo_v1")` 的别名——「NeMo 语义」完全由数据集侧选择，训练配置无权改动。

**stdio 分支与顺序保证**——[livecodebench.py:L25-L45](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/livecodebench.py#L25-L45)：无 `fn_name` 时按输入输出原序生成 stdio 测试对象，随后立即 `validate_tests` 自检。[test_code_execution_client_labels.py:L20-L28](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_code_execution_client_labels.py#L20-L28) 断言顺序与 `comparison` 字段都逐项保留。

**函数式分支**——[livecodebench.py:L46-L86](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/livecodebench.py#L46-L86)：

- [L46-L47](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/livecodebench.py#L46-L47)：`fn_name` 必须是合法 Python 标识符且非关键字。
- [L49-L60](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/livecodebench.py#L49-L60)：`_json` 拒绝 `NaN/Infinity` 等非有限常量（[L16-L22](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/livecodebench.py#L16-L22)）；exact 比较做 **JSON round-trip**——先把实际返回值 `dumps` 再 `loads`，让 `tuple` 归一化成 `list` 等类型差异在比较前消失，配合 Python `==` 得到「JSON 语义的相等」。
- [L61-L76](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/livecodebench.py#L61-L76)：`nemo_v1` 时用 `inspect.getsource` 把两个比较函数的源码取出来，`exec` 进一个隔离命名空间，再让断言调用它——**比较逻辑随标签走**，服务端无需预装 NeMo 代码。
- [L77-L86](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/livecodebench.py#L77-L86)：拼装测试源码：参数与期望值以 `json.dumps` 后的字面量嵌入（`allow_nan=False`），调用目标是 `Solution().fn_name`（若定义了 Solution 类，对应 LiveCodeBench 的类风格提交）否则模块级 `fn_name`。

**NeMo 容差比较**——[open_instruct/code_utils/execution_client/nemo_comparison.py:L14-L31](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/nemo_comparison.py#L14-L31)：`_nemo_values_close` 先严格相等短路；布尔值单独排除（`True == 1` 的坑）；只有至少一侧是 `float` 且两侧都能转成数值时才用容差公式。[L34-L51](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/nemo_comparison.py#L34-L51)：`_nemo_call_outputs_match` 依次尝试：tuple→list 归一后相等 → JSON 序列化相等 → 标量容差 → 两个等长 list 的逐元素容差。文件头注释（[L2-L7](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/nemo_comparison.py#L2-L7)）说明它是从 NVIDIA-NeMo/Gym 的 `testing_util.py` 改编：把 NumPy `allclose` 换成标量标准库算术，并把「比较」从执行/计时/早停逻辑中剥离。

**容差语义的回归测试**——[test_code_execution_client_labels.py:L8-L17](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_code_execution_client_labels.py#L8-L17)：用 `f = lambda _: 1.0000005` 对照期望 `"1.0"`——按公式 \(1.0000005 - 1.0 = 5\times10^{-7} \le 10^{-8} + 10^{-5}\)，应通过；而 `1.01` 应触发 `AssertionError`。**测试直接在本地 `exec` 生成的断言**来验证语义被完整保留（正式判分仍由服务端执行）。

#### 4.4.4 代码实践

**实践目标**：亲手生成一份函数式测试标签，并在本地验证其判分语义（通过/拒绝各一次）。

**操作步骤**（示例代码；注意输入是「每行一个 JSON 位置参数」，两个参数要写成两行）：

```python
# 示例代码
from open_instruct.code_utils.execution_client import livecodebench

mode, tests = livecodebench.prepare_tests(["1\n2", "0\n0"], ["3", "0"], "add")
print(mode)
print(tests[0])

# 本地验证语义（仅练习用；正式判分在服务端执行）
good, bad = {}, {}
exec("def add(a, b):\n    return a + b", good)
exec("def add(a, b):\n    return a + b + 1", bad)
exec(tests[0], good)          # 正确实现：静默通过
try:
    exec(tests[0], bad)
    print("不该通过")
except AssertionError:
    print("错误实现被断言拒绝，符合预期")
```

**需要观察的现象**：`mode` 为 `assert`；`tests[0]` 是一段以 `import json as __verifier_json` 开头、以 `assert (...)` 结尾的源码；正确实现静默通过，错误实现抛 `AssertionError`。

**预期结果**：如上。`exec(tests[0], good)` 时命名空间里没有 `Solution`，所以走 `globals()['add']` 分支。若把 `fn_name` 去掉改走 stdio 分支，`mode` 会变成 `"stdio"`，可对照 [test_code_execution_client_labels.py:L20-L28](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_code_execution_client_labels.py#L20-L28) 检查生成的对象形状。

#### 4.4.5 小练习与答案

**练习 1**：`fn_name=None` 与 `fn_name="add"` 分别生成哪种模式的测试？各自的「判定」发生在哪里？

答案：`None` → stdio 模式，测试是 `{input, output[, comparison]}` 对象，服务端把 `input` 喂给程序标准输入、比对标准输出；`"add"` → assert 模式，测试是一段 Python 源码，服务端在沙箱里执行它，断言 `fn(*args)` 的返回值与期望一致。

**练习 2**：为什么 exact 比较要先 `json.dumps` 再 `json.loads`（round-trip），而不是直接 `==`？

答案：round-trip 把值归一化到 JSON 类型系统（如 `tuple → list`、int/float 的表示差异），使得「模型返回 `(1, 2)`」与「期望 `[1, 2]`」按 JSON 语义判定为相等，与数据集标签的书写方式一致，避免纯 Python 类型差异造成的误判。

**练习 3**：`nemo_v1` 的比较函数为什么不要求服务端预装 NeMo，也不会被训练配置改容差？

答案：比较函数源码经 `inspect.getsource` 取出后 `exec` 进隔离命名空间并拼进断言测试，随标签一起传输——服务端只需要能执行 Python；同时 `prepare_nemotron_tests` 不暴露任何容差参数，容差写死在 `_nemo_values_close` 里，属于数据集语义，配置侧无从干预。

## 5. 综合实践

**任务**：把本讲四个模块串成一次「故障注入演练」，并用四个测试文件交叉验证你的理解。

**步骤 1——跑测试并记录覆盖点**：

```bash
uv run pytest open_instruct/test_code_execution_client.py \
  open_instruct/test_code_execution_client_batching.py \
  open_instruct/test_code_execution_client_verifier.py \
  open_instruct/test_code_execution_client_labels.py -q
```

逐文件填一张覆盖点清单（依据测试名与断言）：

| 文件 | 你应记录到的覆盖点 |
| --- | --- |
| test_code_execution_client.py | 429 重试恢复且同批共用 Request-Id；重试耗尽 → ServiceError(status=429, attempts=N)；401 → ClientError 且只发 1 次；坏响应参与重试；坏 JSON → ServiceError；部署指纹不符 → ServiceError；full jitter 可变；等待不能延长 deadline；取消可在发请求前/等待中生效；令牌不出现在载荷与错误文本（`<redacted>` 打码） |
| test_code_execution_client_batching.py | 时间预算分批保序（7s/2s → 批大小 2）；单测试超限 → 413 且零请求；**后一批失败绝不返回部分分数**；deadline 跨批共享；迟到响应拒收；旧编码解码计数；解压大小上限；非有限时限拒绝 |
| test_code_execution_client_verifier.py | 异步取消放干在途请求（post 恰好 1 次）；worker 排队时间计入 deadline；`extract_python_code` 18 条规则全表；适配器透传令牌与剥后缀后的 URL；端 token 残留仍可判分；**服务失败绝不变成奖励**；空测试集拒绝；invalid_code 是 completed 0 分且不开 HTTP 会话；MILES validity 契约（completed 零分 valid，ServiceError 则 validity 未知） |
| test_code_execution_client_labels.py | 函数标签保留 NeMo 容差（1.0000005 过、1.01 拒）；stdio 标签保序且带 comparison；未知 comparison 在传输前拒绝 |

**步骤 2——完整故障注入脚本**：把 4.1.4 的三场景脚本扩展为下表并逐行验证（全部用 `Mock` 拦截 `session.post`，`backoff=0` 消除等待）：

| 场景 | mock 行为 | 期望结局 | 验证点 |
| --- | --- | --- | --- |
| A | 429 → 200（合法 v2 响应，3 个 outcome） | `all_passed=True`，post 计数 2 | 重试恢复 |
| B | 持续 503 | `ServiceError(status=503, attempts=3)` | 故障上抛而非 0 分 |
| C | 401 | `ClientError`，post 计数 1 | 4xx 不重试 |
| D | 200 但 outcomes 里混入 `"status": "error"` | `ServiceError("invalid grading response")` | 执行不完整 ≠ 测试失败 |
| E | 200 合法但 3 个测试只有 2 个 outcome | `ServiceError`（数量不符） | 结构校验 |

场景 D/E 的构造方式：在 `OK` 字典基础上分别改 `outcomes` 内容与长度即可（`_validate_v2` 对应 [__init__.py:L54-L70](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L54-L70)）。注意场景 D 要让重试耗尽：mock 固定返回坏 200，`retries=0` 即一次尝试后抛错。

**步骤 3——写结论**：用不超过五句话回答：如果生产训练中判分服务抖动 5 分钟，这套客户端 + 适配器会把损失控制在什么程度？哪些样本会被掩掉？哪些情况下训练应中止？（提示：`ServiceError` 的传播路径是 `CodeExecutionClient._post` → `execute` → `CodeVerifier.async_call` → MILES `CompleteCodeVerifier` 的 `service_error` 状态，见 4.3.3 最后一段。）

**预期结果**：五个场景全部与表格一致；四个测试文件全绿。若某场景与预期不符，先回读对应断言（本文各表均给了测试行号），再检查 mock 的响应形状是否严格满足 `_validate_v2`。

## 6. 本讲小结

- **职责边界**：`execution_client` 包只做编码、校验与判定汇总；`protocol.py` 自明「无执行实现」，模型代码只在外部判分服务的沙箱里运行。
- **错误二分类**：429/5xx/传输错误/坏响应/指纹不符 → `ServiceError`（full-jitter 重试耗尽后上抛，调用方掩样本或中止训练）；4xx → `ClientError` 立即失败。**HTTP 200 也不被信任**，必须过能力协商、部署指纹与结构校验三道关。
- **三分打分法**：抽不出程序 = completed 0 分（合法训练信号）；部分测试未过 = `pass_fraction`（可被阈值整流、可按运行时间打折）；服务故障 = 异常穿透，绝不折算成分数。
- **线格式安全模型**：受限 unpickler 白名单只有 `set/frozenset/bytearray`；zlib 解压限 128 MiB 防 bomb；时限夹在 \([0.05, 60]\) 秒；比较方式只认 `exact/nemo_v1` 且在传输前校验。
- **标签构造**：`livecodebench.prepare_tests` 把 inputs/outputs 编译成 stdio 对象或一段段 assert 源码；NeMo 容差 \(10^{-8} + 10^{-5}|e|\) 的比较函数源码随标签嵌入，语义归数据集所有，配置无旋钮。
- **工程细节**：一个批次一个 `X-Request-Id`、重试复用；deadline 跨批跨等待共享；分批大小由时间预算公式决定，超限先 zlib 压缩再二分拆批；令牌只进 Authorization 头且错误文本打码。

## 7. 下一步学习建议

- **u8-l6（MILES 奖励体系）**：看 `CompleteCodeVerifier` 如何在可信注册表里登记、`service_error` 状态如何被资格检查与训练排除逻辑消费——本讲的错误哲学在那里落地为运维语义。
- **u8-l8（MILES 启动与运维）**：`CODE_EXECUTION_TOKEN` 经 `launch.secrets` 按名注入、判分器依赖随 MILES 镜像分发并在构建期自检（`scripts/miles/check_verifier_dependencies.py`），把本讲的客户端放进完整的部署链路。
- **延伸阅读**：`docs/miles/dataset-verifiers.md` 与 `docs/miles/source-preparation.md` 讲数据集如何声明 complete_code 目标；`open_instruct/test_complete_code.py` 则从消费者视角复测了「服务故障 ≠ 0 分」契约。
- 如果你想动手改这个包：先跑通本讲综合实践的五个场景，再读四个测试文件里 monkeypatch 时钟/抖动的技巧——它们是这个没有真实服务也能完整测试的包的工程精髓。
