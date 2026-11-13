# 代码奖励与沙箱执行

> 本讲属于单元 6「RLVR 与 GRPO 核心」，承接 u6-l3「可验证奖励：verifier 注册表与数学判分」。
> 本轮仓库更新（`896a734` → `bae4209`）把原本外置的代码执行客户端移入了仓库内部（`open_instruct/code_utils/execution_client/`），本讲已按新结构重写，并在 4.5 节专门讲解这一衔接变化。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `CodeVerifier` 这条「代码类可验证奖励」的完整执行链路：模型输出 → 抽取 Python 代码 → 发给执行服务 → 按测试通过率打分。
2. 理解判分服务端的沙箱执行工具：子进程隔离、超时杀进程、`reliability_guard` 与 `should_execute` 两层防护，以及它们各自的边界（哪些防得住、哪些防不住）。
3. 区分两种测试用例格式（assert 断言式与 stdio 标准输入输出式）的判定流程与错误码语义。
4. 理解 `generic_sandbox` 这个面向**多轮工具调用 RL** 的 Docker 沙箱环境，以及它与「判分沙箱」在进程模型、状态保持上的本质差异。
5. 掌握本轮更新的核心变化：执行客户端已从外部私有包移入库内 `open_instruct.code_utils.execution_client`，`complete_code` 验证器随之改为直接导入，`test_complete_code.py` 的 mock 方式也从「劫持 importlib」变成「给 `complete_code.execution_verifier` 打补丁」。

## 2. 前置知识

- **可验证奖励（RLVR）**：强化学习中不给奖励模型打分，而是用程序自动判定回答对错。数学题可以用 sympy 判等价（u6-l3），代码题的「对错」则只能靠**真的运行代码**来判定——这就是本讲的主角。
- **verifier 注册表**：u6-l3 讲过 `open_instruct/ground_truth_utils.py` 中的 `VerifierFunction` 抽象类与按数据集名分发的 `apply_verifiable_reward`。代码验证器 `CodeVerifier` 是其中的 `"code"` 项，本讲的 4.1 节从它出发。
- **测试用例（test case）**：一段用来检验程序行为的代码。assert 式形如 `assert add(1, 2) == 3`；stdio 式形如「给定输入 `"1\n"`，期望输出 `"2\n"`」（竞赛题风格，来自 LiveCodeBench）。
- **沙箱（sandbox）**：隔离执行不可信代码的环境。本讲会碰到两种完全不同的沙箱：判分用的「子进程 + 函数屏蔽」轻量沙箱，和工具 RL 用的 Docker 容器沙箱。
- **pickle 的风险**：`pickle.loads` 可以执行任意代码。数据集里的测试用例历史上用 pickle 编码存储，理解这一点才能看懂 4.5 节新客户端里「受限 unpickler」的安全设计。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [open_instruct/ground_truth_utils.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/ground_truth_utils.py) | verifier 注册表；`CodeVerifier`（`"code"` 验证器）在此定义，把模型输出经 HTTP 发给执行服务 |
| [open_instruct/code_utils/api.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/api.py) | FastAPI 判分服务端，暴露 `/test_program` 与 `/test_program_stdio` 两个端点 |
| [open_instruct/code_utils/code_utils.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/code_utils.py) | 判分核心：子进程执行、超时控制、`reliability_guard`、assert/stdio 两种判定 |
| [open_instruct/code_utils/testing_util.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/testing_util.py) | `grade_stdio`，改自 LiveCodeBench 的 stdio 判分实现 |
| [open_instruct/environments/generic_sandbox.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/environments/generic_sandbox.py) | 多轮工具 RL 的通用沙箱环境（`execute_bash` + `str_replace_editor`） |
| [open_instruct/environments/backends.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/environments/backends.py) | 沙箱后端抽象与 Docker 实现（容器启动、命令超时、文件读写） |
| [open_instruct/code_utils/execution_client/__init__.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py) | **本轮新增（入库）**：`CodeExecutionClient`，带认证、重试与错误分类的执行客户端 |
| [open_instruct/code_utils/execution_client/protocol.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/protocol.py) | **本轮新增**：线格式限制、测试解码与校验（含受限 unpickler） |
| [open_instruct/code_utils/execution_client/verifier.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/verifier.py) | **本轮新增**：`CodeVerifier` 注册表适配器（更严格的代码抽取 + 服务故障上抛） |
| [open_instruct/miles/rewards/complete_code.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rewards/complete_code.py) | MILES 的 complete_code 验证器，本轮改为导入库内 `execution_client` |
| [open_instruct/test_complete_code.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_complete_code.py) | complete_code 的测试；本轮 mock 方式随入库而简化 |

## 4. 核心概念与源码讲解

### 4.1 CodeVerifier 执行链路：从模型输出到判分请求

#### 4.1.1 概念说明

代码题的 ground truth 不是「标准答案字符串」，而是**一组测试用例**。奖励的定义因此变成：

\[
\text{pass\_rate} = \frac{1}{N}\sum_{i=1}^{N} r_i, \quad r_i \in \{0, 1\} \text{（第 } i \text{ 个测试是否通过）}
\]

再经过阈值整流得到最终分数：

\[
\text{score} = \begin{cases} 0 & \text{pass\_rate} < \theta \\ \text{pass\_rate} & \text{pass\_rate} \ge \theta \end{cases}
\]

其中 \(\theta\) 是 `code_pass_rate_reward_threshold`。设 \(\theta = 1\) 就是「全部通过才给分」，设低一点则允许部分通过的程序拿到部分信用。

`ground_truth_utils.py` 中的 `CodeVerifier` 是 u6-l3 注册表里名字为 `"code"` 的验证器：它**自己不执行代码**，只负责抽取代码、组装请求、调用外部执行服务（也就是 4.2/4.3 节的 `api.py` 服务）并计算分数。这种「客户端 / 服务端」分工意味着判分可以部署在带隔离的机器上，训练进程只收发 HTTP。

#### 4.1.2 核心流程

```
模型输出 prediction（通常含 markdown 代码块）
   │
   ├─ 1. extract_python_code(): 正则抽出最后一个 ```python``` 代码块
   │        （没有代码块则把整个输出当作代码）
   ├─ 2. 组装 payload = {program, tests, max_execution_time}
   ├─ 3. asyncio.to_thread 里 POST 到 code_api_url（带连接池与自动重试的 Session）
   ├─ 4. 服务端返回 {results: [0/1,...], runtimes: [float,...]}
   ├─ 5. pass_rate = sum(results) / len(results)；低于阈值 θ 则记 0 分
   └─ 6. （可选）apply_perf_penalty：按运行时间打折
```

性能惩罚开启时，分数进一步变为：

\[
\text{score} = \frac{1}{N}\sum_{i=1}^{N} r_i \cdot \frac{T - t_i}{T}
\]

其中 \(T\) 是单测试时限、\(t_i\) 是第 \(i\) 个测试的实际耗时——同样通过，跑得快的程序得分更高，这是鼓励模型生成高效代码的机制。

#### 4.1.3 源码精读

配置数据类只有四个字段，全部由训练脚本的 `RewardConfig` 传入：

- [open_instruct/ground_truth_utils.py:89-94](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/ground_truth_utils.py#L89-L94) — `CodeVerifierConfig` 定义 `code_api_url`（判分服务地址）、单测试时限、通过率阈值、是否开性能惩罚。

代码抽取是纯文本处理：

- [open_instruct/ground_truth_utils.py:902-912](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/ground_truth_utils.py#L902-L912) — `extract_python_code` 用正则 `` ```(?:python)?(.*?)``` `` 抓出所有代码块，返回**最后一个**（模型常常先给草稿再给终稿），没有代码块时直接返回原文。

HTTP 客户端做了连接池与重试：

- [open_instruct/ground_truth_utils.py:917-931](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/ground_truth_utils.py#L917-L931) — `_get_session` 惰性创建类级共享的 `requests.Session`，挂 100 连接的 `HTTPAdapter`，并对 500/502/503/504 配置自动重试。

主判定逻辑：

- [open_instruct/ground_truth_utils.py:933-995](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/ground_truth_utils.py#L933-L995) — `async_call` 组装 payload、在 `asyncio.to_thread` 中发请求、按上式计算 `pass_rate` 与 `score`。注意 L996-998：**任何异常都被吞掉并返回 `score=0`**——这在后面 4.5 节会形成鲜明对比（新链路认为「服务故障 ≠ 测试失败」）。

配套的服务端就是 `api.py`：

- [open_instruct/code_utils/api.py:39-42](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/api.py#L39-L42) — `TestRequest` 请求体：`program`（代码字符串）、`tests`（测试列表或编码串）、`max_execution_time`（默认 1 秒）。
- [open_instruct/code_utils/api.py:50-61](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/api.py#L50-L61) — `/test_program` 端点先 `decode_tests` 解码，再调 `get_successful_tests_fast`（assert 模式），返回 `results` 与 `runtimes`。
- [open_instruct/code_utils/api.py:64-75](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/api.py#L64-L75) — `/test_program_stdio` 端点走 stdio 模式 `get_successful_tests_stdio`。

#### 4.1.4 代码实践

**实践目标**：不写训练代码，直接用 HTTP 复现一次代码判分，直观看到 `results` 列表的含义。

**操作步骤**（命令取自 `api.py` 模块文档字符串）：

1. 启动本地判分服务：
   ```bash
   uv run nohup uvicorn open_instruct.code_utils.api:app --host 0.0.0.0 --port 1234 &
   ```
2. 探活：
   ```bash
   curl -X GET http://localhost:1234/health
   ```
3. 提交一个「两个断言通过、一个失败」的程序：
   ```bash
   curl -X POST http://localhost:1234/test_program -H "Content-Type: application/json" \
     -d '{"program": "def add(a, b): return a + b", "tests": ["assert add(1, 2) == 3", "assert add(-1, 1) == 0", "assert add(0, 0) == 1"], "max_execution_time": 1.0}'
   ```

**需要观察的现象**：返回 JSON 中 `results` 应为 `[1, 1, 0]`，`runtimes` 为每个测试的秒数。

**预期结果**：`pass_rate = 2/3 ≈ 0.667`；若阈值 \(\theta = 1\)，这个样本在训练里得 0 分。具体响应格式以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `extract_python_code` 取**最后一个**代码块而不是第一个？

**答案**：模型输出常见「先分析、给一个草稿实现、再修正」的模式，最后一个代码块通常是最终答案；取第一个会把草稿当答案，系统性低估模型质量。

**练习 2**：`async_call` 把异常吞成 `score=0` 有什么隐患？

**答案**：服务宕机、网络超时等基础设施故障会被当成「程序没通过测试」，训练信号被污染——模型写得再好也拿 0 分。4.5 节的库内客户端用 `ServiceError` 上抛来修复这一点，服务故障会让调用方选择丢弃样本或中止，而不是伪造一个 0 分。

**练习 3**：`http_timeout = max(30, min(300, code_max_execution_time * 10))`（L968）为什么要给单测试时限乘 10？

**答案**：一次 HTTP 请求里服务端要跑**全部** N 个测试，HTTP 超时必须覆盖 N 倍的单测试时限再加调度开销；乘 10 是一个粗略的安全系数，并夹在 30~300 秒之间防止极端值。

### 4.2 沙箱执行工具：子进程隔离、超时与 reliability_guard

#### 4.2.1 概念说明

判分服务端拿到的是**任意模型生成的代码**，可能是死循环、删文件、甚至 fork 炸弹。`code_utils.py` 提供三层防护：

1. **`should_execute` 黑名单**：程序文本里出现危险关键字就直接拒绝执行，全部测试记 0——连尝试都不尝试。
2. **子进程隔离 + 超时击杀**：每个测试在独立的 `multiprocessing.Process` 里跑，主进程 `join(timeout)` 超时后 `kill()`，一个死循环只会废掉自己这个测试。
3. **`reliability_guard` 函数屏蔽**：改编自 OpenAI HumanEval，在子进程里把 `os.remove`、`shutil.rmtree`、`subprocess.Popen` 等破坏性函数置为 `None`。

必须强调源码里自己的警告：**这不是安全沙箱**。它防的是「意外的破坏」（模型生成的代码误删文件），防不住「故意的逃逸」。真正的隔离靠把 `api.py` 部署在独立容器/机器上。

#### 4.2.2 核心流程

`get_successful_tests_fast`（assert 模式）的执行过程：

```
输入: program(str), tests(断言列表), max_execution_time
  │
  ├─ tests 为空 → 返回 ([], [])
  ├─ should_execute(program, tests) == False → 全部记 0，不执行
  ├─ 为每个测试建 shared array（结果 i 型、耗时 d 型），初值 0 / -1.0
  ├─ 对每个测试 i:
  │     fork 子进程 run_individual_test_helper(program, test, i)
  │     子进程: reliability_guard() → exec(program) → exec(test)
  │             任一步抛异常 → 结果 0；都成功 → 结果 1 并记耗时
  │     父进程: p.join(timeout=max_execution_time)，还活着就 p.kill()
  └─ 返回 (results, runtimes)
```

关键设计：**程序定义与测试断言在同一个 `exec` 命名空间里执行**——`exec(program)` 定义的函数，`exec(test)` 直接可见，测试断言引用 `add(...)` 就是在引用刚定义的函数。

#### 4.2.3 源码精读

- [open_instruct/code_utils/code_utils.py:271-286](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/code_utils.py#L271-L286) — `should_execute`：一个纯字符串包含检查的黑名单（`threading`、`multiprocessing`、`import os`、`shutil`、`torch`、`sklearn` 等），命中即整体拒绝执行。
- [open_instruct/code_utils/code_utils.py:123-143](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/code_utils.py#L123-L143) — `run_individual_test_helper`：子进程入口。先 `reliability_guard()`，再把 `program` 和 `test` 先后 `exec` 进同一个 `execution_context`；异常 → 0，成功 → 1 并记录 `time.time()` 差值。
- [open_instruct/code_utils/code_utils.py:179-188](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/code_utils.py#L179-L188) — 每测试一进程 + 超时击杀的核心：`p.join(timeout=max_execution_time)` 后 `p.is_alive()` 则 `p.kill()`。结果通过 `multiprocessing.Array` 跨进程回传，即使子进程被杀，共享数组里也留着初值 0。
- [open_instruct/code_utils/code_utils.py:314-393](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/code_utils.py#L314-L393) — `reliability_guard`：置 `builtins.exit/quit` 为 `None`、把 `os` 的 `system/remove/chmod/chown/...` 置 `None`、`shutil.rmtree/move/chown` 置 `None`、`subprocess.Popen = None`、禁 `faulthandler`、`chdir` 到 `cache/` 目录；可选参数还能设 `resource.setrlimit` 限内存。L321-326 的 docstring 明确写着「This function is NOT a security sandbox」。
- [open_instruct/code_utils/code_utils.py:294-311](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/code_utils.py#L294-L311) — `partial_undo_reliability_guard`：用模块顶部保存的原始函数引用（L24-32 的 `tmp_chmod = os.chmod` 等）恢复被屏蔽的函数。这个「先存原件再屏蔽」的模式是进程内打补丁可撤销的关键。

测试文件直接验证了防护行为：

- [open_instruct/code_utils/test_code_utils.py:281-300](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/test_code_utils.py#L281-L300) — `test_dangerous_imports_blocked` 参数化地断言 `import threading`、`import shutil` 等程序的结果是 `[0]`（被 `should_execute` 拒绝）。
- [open_instruct/code_utils/test_code_utils.py:302-312](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/test_code_utils.py#L302-L312) — 对照组：`math`、`json`、`re` 等安全导入正常执行得 `[1]`。

#### 4.2.4 代码实践

**实践目标**：亲眼验证「危险关键字整体拒绝」与「单测试超时只废一个测试」。

**操作步骤**：

1. 运行现成的判定测试：
   ```bash
   uv run pytest open_instruct/code_utils/test_code_utils.py -k "add_function or mixed_pass_and_fail" -v
   ```
2. 再跑防护相关的一组：
   ```bash
   uv run pytest open_instruct/code_utils/test_code_utils.py -k "ReliabilityGuard" -v
   ```

**需要观察的现象**：`test_mixed_pass_and_fail` 中含 `time.sleep(10)` 的测试项因 1 秒超时被判 0，而它前后的正常测试不受影响；`test_dangerous_imports_blocked` 的全部参数化用例结果都是 `[0]`。

**预期结果**：所有用例通过；`test_mixed_pass_and_fail` 期望 `[0, 1, 0, 1, 1, 0, 1]`（见 [test_code_utils.py:34-39](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/test_code_utils.py#L34-L39)）。整条命令在 CPU 机器可跑（待本地验证具体耗时）。

#### 4.2.5 小练习与答案

**练习 1**：为什么每个测试要单独一个子进程，而不是像 stdio 模式那样全放一个进程里？

**答案**：assert 模式下每个测试独立无状态，单进程隔离让死循环、段错误、内存爆炸只影响当前测试（kill 掉即可），其余测试照常判定；stdio 模式则必须共享一次编译出的程序对象，所以只能整体放一个进程（见 4.3 节）。

**练习 2**：`should_execute` 用子串匹配（`"import os" in program`）会有什么误伤？

**答案**：程序里出现字符串 `"import os"`（比如在注释或文档里）也会被拒绝；但这是**保守方向**的误伤——被拒只是 0 分，不会带来安全风险，设计上宁可误杀。

**练习 3**：`reliability_guard` 里为什么要 `os.chdir(cache_wd)` 切到 `cache/` 目录？

**答案**：让程序在隔离的工作目录里读写文件，即使文件操作漏网（`open` 并未被屏蔽，见 L344 的注释）也只污染 `cache/` 目录，`partial_undo_reliability_guard` 最后还会 `chdir` 回原目录。

### 4.3 测试用例判定：assert 模式与 stdio 模式

#### 4.3.1 概念说明

两种测试格式对应两类数据生态：

| | assert 模式 | stdio 模式 |
| --- | --- | --- |
| 测试形态 | `"assert add(1, 2) == 3"` | `{"input": "2 3", "output": "5"}` |
| 数据来源 | AceCode / AceCoder 风格 | LiveCodeBench / 竞赛题风格 |
| 入口函数 | `get_successful_tests_fast` | `get_successful_tests_stdio` → `grade_stdio` |
| 进程模型 | 每测试一个进程 | 全部测试一个进程 |
| 判定方式 | `exec` 断言不抛异常即通过 | 捕获 stdout 与期望逐行比对 |
| 数值等价 | 由断言本身决定 | 精确字符串失败后退到 `Decimal` 数值比较 |

stdio 模式的「数值等价」值得注意：输出 `1.0000005` 对期望 `1.0` 算对，输出 `alice` 对期望 `Alice` 算错（大小写敏感）——这来自 LiveCodeBench/NeMo 的判分语义，也是 4.5 节 `nemo_v1` comparison 的源头。

另外，数据集里的测试常常不是明文列表，而是 **pickle → zlib → base64** 压缩串（AceCoder 格式），`encode_tests` / `decode_tests` 负责互转。

#### 4.3.2 核心流程

stdio 判定链路（`get_successful_tests_stdio` → `run_tests_stdio_helper` → `grade_stdio`）：

```
tests = [{"input": ..., "output": ...}, ...]
  │
  ├─ should_execute 黑名单检查
  ├─ 总超时预算 total_timeout = max_execution_time × N + 5.0（所有测试串行跑）
  ├─ 单独子进程执行 run_tests_stdio_helper:
  │     reliability_guard()
  │     grade_stdio(program, inputs, outputs, timeout):
  │       1. clean_if_name: 剥掉 if __name__ == '__main__' 外壳
  │       2. make_function: 把代码包进 wrapped_function，并预置一大段
  │          import_string（from math import * 等，模拟竞赛环境）
  │       3. compile_code: signal.alarm 限时编译；失败 → 全部 -1
  │       4. 逐测试: mock stdin 喂输入 → Capturing 捕获 stdout →
  │          逐行比对（先字符串精确，再 Decimal 数值）
  │       错误码: True=通过 / -1=编译失败 / -2=答案错 / -3=超时 / -4=运行时错误
  │     True → 1，其余 → 0，写回共享数组
  └─ 父进程 join(total_timeout)，超时 kill
```

#### 4.3.3 源码精读

- [open_instruct/code_utils/code_utils.py:146-190](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/code_utils.py#L146-L190) — `get_successful_tests_fast`：docstring 明确警告「只应在虚拟环境中运行」（L149-151）；L162-167 空表与黑名单短路；L170-171 用 `multiprocessing.Array("i"/"d")` 跨进程收集结果与耗时。
- [open_instruct/code_utils/code_utils.py:222-263](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/code_utils.py#L222-L263) — `get_successful_tests_stdio`：注意 L249-250 的总超时预算 `max_execution_time * test_ct + 5.0`——因为 stdio 模式所有测试在**一个**进程里串行执行。
- [open_instruct/code_utils/code_utils.py:198-219](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/code_utils.py#L198-L219) — `run_tests_stdio_helper`：子进程侧调用 `grade_stdio`，把 `True` 映射为 1（L210），任何异常都保持初始化的 0（L215-217 的注释说明了这一兜底）。
- [open_instruct/code_utils/testing_util.py:220-320](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/testing_util.py#L220-L320) — `grade_stdio` 主体：`signal.alarm` 实现每测试超时（L240、L252）；`Capturing` 上下文管理器劫持 `sys.stdout` 收集输出；L289-306 是「精确字符串 → Decimal 数值」两级比对；L316-318 命中第一个失败即打印并返回。
- [open_instruct/code_utils/testing_util.py:25](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/testing_util.py#L25) — `import_string`：预注入 `from math import *`、`from collections import *` 等竞赛常用命名空间，让模型无需写 import 也能用 `sqrt`、`Counter`。
- [open_instruct/code_utils/code_utils.py:43-49](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/code_utils.py#L43-L49) 与 [L52-88](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/code_utils.py#L52-L88) — `encode_tests` / `decode_tests`：列表 ⇄ `base64(zlib(pickle(list)))` 压缩串互转；`decode_tests` 先试 JSON、再试压缩格式，解不出记 error 并返回空表。

#### 4.3.4 代码实践

**实践目标**：用 stdio 模式判一道「读入一个数、输出加一」的竞赛题，并观察数值等价判定。

**操作步骤**（curl 示例同样取自 `api.py` 文档字符串）：

1. 服务按 4.1.4 启动后提交 stdio 程序：
   ```bash
   curl -X POST http://localhost:1234/test_program_stdio -H "Content-Type: application/json" \
     -d '{"program": "import sys\nfor line in sys.stdin.read().splitlines():\n    print(int(line.strip()) + 1)", "tests": [{"input": "1\n", "output": "2\n"}, {"input": "100\n", "output": "101\n"}], "max_execution_time": 1.0}'
   ```
2. 把程序里的 `int(...)` 换成 `float(...)` 再提交一次（输出会变成 `2.0`、`101.0`）。
3. 阅读并运行 [open_instruct/test_complete_code.py:170-181](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_complete_code.py#L170-L181) 的参数化用例表，对照其中的 `("print('1.0000005')", ..., 1)` 与 `("print('alice')", ..., 0)`。

**需要观察的现象**：第 2 步换 `float` 后，`Decimal` 数值比较应把 `2.0` 与 `2` 判为相等；而把输出改成大写 `Alice` 对期望 `alice` 则判错。

**预期结果**：第 1 步 `results` 为 `[1, 1]`；第 2 步仍为 `[1, 1]`（数值等价生效）。stdio 具体行为待本地验证；判定语义以 `grade_stdio` 源码与 test_complete_code.py 的断言表为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 stdio 模式的总超时要按 `max_execution_time × N + 5` 计算，而 assert 模式不需要？

**答案**：assert 模式每个测试独立进程、独立 `join(timeout)`；stdio 模式一个进程串行跑完 N 个测试（程序只编译一次），外层必须给足 N 倍预算，`+5` 是编译与启动开销。

**练习 2**：`grade_stdio` 返回 `-1/-2/-3/-4` 各代表什么？`get_successful_tests_stdio` 如何处理它们？

**答案**：-1 编译失败、-2 答案错误（含输出行数不匹配）、-3 超时（Time Limit Exceeded）、-4 运行时错误；`run_tests_stdio_helper` 只把 `True` 映射为 1，其余一律 0——对训练来说只需「过/不过」二值信息，错误码主要供人诊断。

**练习 3**：`decode_tests` 里 `pickle.loads`（code_utils.py L83）有什么风险？4.5 节的库内版本如何改进？

**答案**：恶意构造的 pickle 串在 `pickle.loads` 时可执行任意代码。库内 `execution_client/protocol.py` 用 `_RestrictedUnpickler` 只放行 `set/frozenset/bytearray` 三种内建类型，其余一律 `UnpicklingError`，把 pickle 载荷限制为纯数据。

### 4.4 generic_sandbox：面向多轮工具 RL 的 Docker 沙箱环境

#### 4.4.1 概念说明

`generic_sandbox` 与前面两节的「判分沙箱」目的完全不同：它是 u6-l6 讲过的 `RLEnvironment` 体系中的一个环境，让模型在**多轮对话**中调用两个工具——`execute_bash`（有状态 bash）和 `str_replace_editor`（文件查看/编辑）——去完成编程任务。奖励不再来自测试通过率，而来自每一步的 `StepResult`：命令退出码 0 得 0.0，非 0 得负的 `penalty`（默认 -0.05）。

它解决的问题是：判分沙箱是「一次性执行、无状态」，而 agent 式编程需要在**同一个文件系统**里连续地建文件、改代码、跑测试、看报错——所以它用 Docker 容器做后端，用 bash wrapper 持久化环境变量与工作目录。

#### 4.4.2 核心流程

```
reset(task_prompt)
  ├─ 失败重试至多 3 次（每次重建 backend）
  ├─ create_backend("docker") → 容器以 sleep infinity 常驻启动
  ├─ mkdir /testbed/{input,output}；有 git 则 git init
  ├─ 写入 _BASH_WRAPPER 并 chmod +x
  └─ 返回 StepResult("Sandbox ready.") + 两个工具的 JSON schema

step(EnvCall)
  ├─ "execute_bash" → bash wrapper 执行命令
  │     wrapper: source 上次的环境变量 → cd 上次的目录 → eval 命令
  │              → 导出新环境变量与目录（跨调用持久化）
  │     奖励: exit_code == 0 → 0.0，否则 penalty
  ├─ "str_replace_editor" → view / create / str_replace / insert
  └─ 其他名字 → 报错 StepResult + penalty
```

#### 4.4.3 源码精读

- [open_instruct/environments/generic_sandbox.py:26-34](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/environments/generic_sandbox.py#L26-L34) — `_BASH_WRAPPER`：有状态 bash 的全部秘密。每次执行前 `source /tmp/.sandbox_env`、`cd $(cat /tmp/.sandbox_cwd)`，执行后 `export -p > /tmp/.sandbox_env`、`pwd > /tmp/.sandbox_cwd`——用两个临时文件把环境变量和工作目录在调用之间「带过去」。
- [open_instruct/environments/generic_sandbox.py:137-152](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/environments/generic_sandbox.py#L137-L152) — `reset` 的三次重试：容器启动可能因镜像拉取、Docker 抖动失败，每次失败关掉 backend 重建，三次全败才抛 `RuntimeError`。
- [open_instruct/environments/generic_sandbox.py:206-225](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/environments/generic_sandbox.py#L206-L225) — `_execute_bash`：经 wrapper 执行命令，stdout/stderr 用 `_truncate_output` 各保留首尾 40 行（L98-106）防止撑爆上下文，按退出码给奖励。
- [open_instruct/environments/backends.py:26-47](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/environments/backends.py#L26-L47) — `SandboxBackend` 抽象接口：`start / run_command / write_file / read_file / close` 五个方法，未来可替换执行载体。
- [open_instruct/environments/backends.py:86-100](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/environments/backends.py#L86-L100) — `DockerBackend.run_command`：用 `timeout --signal=TERM --kill-after=10 {N} bash -c ...` 包装实现命令级超时（先 TERM 后 KILL），输出截断到 1MB。容器启动时带 `mem_limit`（L76-83）限制内存。
- [open_instruct/environments/backends.py:155-168](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/environments/backends.py#L155-L168) — `create_backend` 工厂：目前只支持 `"docker"`，其他值直接抛错。

**与判分沙箱的对比**：

| 维度 | code_utils 判分沙箱 | generic_sandbox |
| --- | --- | --- |
| 进程模型 | `multiprocessing` 子进程 | Docker 容器常驻 |
| 状态 | 无状态，测试间互不影响 | 有状态：cwd、env、文件跨调用持久 |
| 用途 | 单轮判分（RLVR 奖励） | 多轮工具调用（agentic RL） |
| 超时 | `p.join(timeout)` + `kill` | `timeout` 命令 + 容器 mem_limit |
| 防护 | `reliability_guard` + 黑名单 | 容器隔离本身 |

#### 4.4.4 代码实践

**实践目标**：理解「mock 掉 backend 工厂」如何让环境测试脱离 Docker 运行。

**操作步骤**：

1. 运行（无需 Docker）：
   ```bash
   uv run pytest tests/test_generic_sandbox.py -v
   ```
2. 阅读 [tests/test_generic_sandbox.py:12-66](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/tests/test_generic_sandbox.py#L12-L66) — `MockBackend` 用一个内存 dict 模拟文件系统、按命令子串返回预制结果。
3. 注意 [tests/test_generic_sandbox.py:83-95](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/tests/test_generic_sandbox.py#L83-L95) — patch 点是 `open_instruct.environments.generic_sandbox.create_backend`，即**消费方模块里引用的工厂函数**，而不是 `backends.create_backend` 原函数。

**需要观察的现象**：所有用例在无 Docker 的机器上通过；`test_reset_returns_observation_and_tools` 断言 reset 返回两个工具定义 `{"execute_bash", "str_replace_editor"}`。

**预期结果**：全绿。这个「patch 消费方命名空间」的手法在 4.5 节的 `test_complete_code.py` 里会再次出现，是本讲反复出现的测试模式。

#### 4.4.5 小练习与答案

**练习 1**：为什么 patch 的是 `generic_sandbox.create_backend` 而不是 `backends.create_backend`？

**答案**：`generic_sandbox.py` 顶部 `from .backends import create_backend` 已把函数绑定进**自己的模块命名空间**；patch 原模块不影响这个已存在的绑定。要拦截调用必须替换消费方命名空间里的名字。（`monkeypatch.setattr` 对模块属性做整体替换则两种写法等价，但 patch 字符串路径时必须找准查找发生的位置。）

**练习 2**：`_BASH_WRAPPER` 为什么要 `export -p > /tmp/.sandbox_env`？

**答案**：容器的 `exec_run` 每次都是新 shell，环境变量不跨执行保留；把 `export -p` 的结果存文件、下次执行前 `source` 回来，就模拟出了一个「常驻终端」的体验，这是 agent 连续工作的前提。

**练习 3**：这个环境的奖励信号（exit code → 0 / penalty）和测试通过率奖励相比缺什么？

**答案**：它只反映「命令有没有跑成功」，不反映任务有没有完成。所以实践中常作为过程奖励或与最终测试判分（如 4.1/4.5 的 verifier）组合使用——环境负责交互，判分器负责终局验收。

### 4.5 库内执行客户端的衔接：execution_client 入库（本轮更新点）

#### 4.5.1 概念说明

本讲最后讲清楚本轮仓库更新（`896a734` → `bae4209`）在代码奖励链路上动了什么。

**更新前**：MILES 的 `complete_code` 验证器依赖一个**外部私有包** `code_execution_client`（对应私有仓库 code-execution-service 的客户端），通过 `importlib.import_module("code_execution_client.verifier")` 动态导入，并用 `CLIENT_REVISION = "103ca27..."` 字符串记录钉住的版本。这带来两个问题：外部依赖不可审查、不可测试（测试要靠劫持 `importlib` 才能 mock）；版本钉住只是「记了个号」，装错版本只能运行时才发现。

**更新后**：客户端代码整体移植入库，成为 `open_instruct/code_utils/execution_client/` 包，每个文件头部都注明「Client-side code ported from code-execution-service revision 103ca279b35e8e81d118947f82b72c22676c6540」。职责边界非常清晰：

- **客户端只做编码、校验与判定逻辑，绝不执行代码**——执行仍归远端服务（客户端甚至会校验服务端源码指纹 `expected_source_sha256`，防止连到「被换过判分逻辑」的服务）。
- **错误二分法**：`ServiceError`（429/5xx/传输错误/超时，重试后仍失败则上抛——调用方必须丢弃样本或中止训练）与 `ClientError`（4xx，请求本身有错，立即失败）。**服务故障绝不能被当成测试失败**，这是对 4.1 节「吞异常记 0 分」哲学的修正。
- `complete_code.py` 从 `importlib.import_module("code_execution_client.verifier")` 改为 `from open_instruct.code_utils.execution_client import verifier as execution_verifier`，测试的 mock 方式随之简化。

下一篇讲义 u6-l8 会深入这个包的协议与安全模型；本节只讲「它如何接进验证器注册表」。

#### 4.5.2 核心流程

MILES 中一次代码样本判分的调用链：

```
sample.metadata.verifiers = [{"name": "complete_code", "target": {"mode": ..., "tests": [...]}}]
   │
   ├─ rewards 注册表按名找到 CompleteCodeVerifier（本节）
   ├─ validate_target: 校验 mode/tests 结构与每条 assert 的语法
   ├─ 组装库内 execution_verifier.CodeVerifier + ServiceConfig
   │     （pass_rate_reward_threshold=1.0、fresh_process_per_test=True）
   ├─ 按 _SUITE_LIMIT=2000 分区，逐区调 verifier.async_call
   │     └─ CodeVerifier.async_call:
   │          extract_python_code（AST 感知，取最后一个完整 python fence）
   │          → CodeExecutionClient.execute(mode, program, tests, ...)
   │               自动分批（批数受时长/字节限制约束，超限递归二分）
   │               POST /v2/execute（Bearer token 认证，429/5xx 重试）
   │          → score = pass_fraction，低于阈值记 0
   ├─ 跨分区分数连乘 score = Π s_k，任一分区 0 分则总分为 0
   └─ 检查每个分区 diagnostics.status ∈ {"ok", "completed"}；
      否则整体标记 "service_error"，不为未完成的判分发明分数
```

#### 4.5.3 源码精读

**衔接点——`complete_code.py` 的导入与使用**：

- [open_instruct/miles/rewards/complete_code.py:13-14](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rewards/complete_code.py#L13-L14) — 本轮更新把外部导入改为 `from open_instruct.code_utils.execution_client import livecodebench` 与 `from open_instruct.code_utils.execution_client import verifier as execution_verifier`。`as execution_verifier` 这个别名是后面测试 mock 的抓手。
- [open_instruct/miles/rewards/complete_code.py:99-108](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rewards/complete_code.py#L99-L108) — `CompleteCodeVerifier.async_call` 中用 `execution_verifier.CodeVerifier(execution_verifier.ServiceConfig(...))` 构造库内适配器，硬编码 `pass_rate_reward_threshold=1.0`（全部通过才算对）与 `fresh_process_per_test=True`。
- [open_instruct/miles/rewards/complete_code.py:113-125](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rewards/complete_code.py#L113-L125) — 分区循环：每 2000 个测试一区，分数连乘；随后 `failed = any(status not in {"ok", "completed"})`——**未完成的判分（服务错误）不折算成 0 分**，而是把整体诊断标成 `"service_error"` 交给上游处置（丢弃样本或中止），注释（L119-121）明确写了这一边界语义。

**库内客户端本体**：

- [open_instruct/code_utils/execution_client/__init__.py:1-12](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L1-L12) — 模块文档给出最小用法：`CodeExecutionClient(url, token=os.environ["CODE_EXECUTION_TOKEN"]).execute("assert", program, tests)`，并写明 ServiceError/ClientError 的语义分工。
- [open_instruct/code_utils/execution_client/__init__.py:73-82](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L73-L82) — `ServiceError(RuntimeError)` 与 `ClientError(ValueError)` 两个错误类，携带 `status/attempts/request_id` 便于诊断。
- [open_instruct/code_utils/execution_client/__init__.py:171-242](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/__init__.py#L171-L242) — `_post` 重试循环：200 才成功；429/5xx/传输错误记 `ServiceError` 并以 full jitter 指数退避重试（L234-235 的注释解释了 full jitter 防止多调用方同步重试）；4xx 立即抛 `ClientError` 不重试；还会核对响应头里的服务源码指纹（L201-205）。
- [open_instruct/code_utils/execution_client/verifier.py:27-71](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/verifier.py#L27-L71) — 增强版 `extract_python_code`：先用 `ast.parse` 判断整段输出是否已是合法 Python（是则直接用，避免误伤含 markdown 记号的合法代码）；否则扫描代码栅栏，**未闭合的 python 栅栏判 invalid**（不回退到早前草稿），显式标了其他语言的栅栏不算 Python 提交。
- [open_instruct/code_utils/execution_client/verifier.py:104-176](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/execution_client/verifier.py#L104-L176) — `CodeVerifier.async_call`：校验配置、从环境变量取 token（缺失直接 `ValueError`）、抽代码（抽不出记 `invalid_code` 的 0 分——这是「完成了判定」的 0，不是服务故障）、`asyncio.to_thread` 里跑 `CodeExecutionClient.execute`，最终分数取 `pass_fraction` 低于阈值记 0。

**测试 mock 方式的变化（本讲实践重点）**：

- [open_instruct/test_complete_code.py:18-39](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_complete_code.py#L18-L39) — 现在的 `mock_client` 只需一行：`monkeypatch.setattr(complete_code, "execution_verifier", SimpleNamespace(CodeVerifier=Client, ServiceConfig=SimpleNamespace))`，把消费方模块里的**属性**换成假命名空间。而更新前的旧代码要先 `native_import = importlib.import_module`，再 monkeypatch `complete_code.importlib.import_module`、在 lambda 里按模块名 `"code_execution_client.verifier"` 拦截——既依赖导入时机，又全局劫持了 import 机制。
- [open_instruct/test_complete_code.py:152-162](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_complete_code.py#L152-L162) — `test_livecodebench_adaptation_preserves_test_order`：更新前首行有 `pytest.importorskip("code_execution_client.livecodebench")`（外部包没装就跳过）；入库后 livecodebench 模块随仓库必在，该行已删除，测试从「可能被跳过」变成「必然执行」。
- [open_instruct/test_complete_code.py:57-62](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_complete_code.py#L57-L62) — `test_unknown_service_failure_is_not_zero`：mock 的客户端抛 `RuntimeError`，断言异常**穿透** `CompleteCodeVerifier` 上抛而不是变成 0 分——正是 4.5.1 说的错误哲学。

#### 4.5.4 代码实践

**实践目标**：亲手验证 mock 点的位置，并追踪一次「mock 客户端 → 库内接口」的调用路径。

**操作步骤**：

1. 运行测试（外部服务相关的 nemotron 用例会自动跳过）：
   ```bash
   uv run pytest open_instruct/test_complete_code.py -v
   ```
2. 打开 [open_instruct/test_complete_code.py:36-38](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_complete_code.py#L36-L38)，确认 patch 的目标是 `complete_code` 模块的 `execution_verifier` 属性。
3. 反向追踪消费点：在 `complete_code.py` 中搜索 `execution_verifier`，会命中 [L101-107](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rewards/complete_code.py#L101-L107) 的 `execution_verifier.CodeVerifier(...)` ——mock 生效的原因是 Python 的属性查找发生在**调用时**、且查的是 `complete_code` 自己的模块全局名字。
4. 用一条命令直观验证第 3 步（示例代码，非项目原有）：
   ```python
   # verify_mock.py —— 演示「模块属性打补丁」为何能拦截调用
   import open_instruct.miles.rewards.complete_code as cc
   from types import SimpleNamespace

   class FakeVerifier:
       def __init__(self, config): self.config = config
       async def async_call(self, *a, **k):
           return SimpleNamespace(score=1.0, diagnostics={"status": "ok"})

   cc.execution_verifier = SimpleNamespace(CodeVerifier=FakeVerifier, ServiceConfig=lambda **kw: SimpleNamespace(**kw))
   # 此后 cc.CompleteCodeVerifier(...).async_call(...) 内部构造的就是 FakeVerifier
   print("patched:", cc.execution_verifier.CodeVerifier is FakeVerifier)
   ```
   运行：`uv run python verify_mock.py`（在仓库根目录）。

**需要观察的现象**：第 1 步大部分用例通过、`test_nemotron_adapter_selects_native_comparison_over_http` 因 `pytest.importorskip("code_execution_service")`（[L183](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_complete_code.py#L183)）被 skip；第 4 步打印 `patched: True`。

**预期结果**：mock 拦截成立。注意第 4 步是演示脚本，跑完即弃，不要提交（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：为什么「给 `complete_code.execution_verifier` 打属性补丁」比「劫持 `importlib.import_module`」更稳健？

**答案**：属性补丁只影响 `complete_code` 这一个消费方的名字查找，作用域精确、不依赖导入顺序；劫持 `importlib` 则全局改变所有模块的导入行为，还要正确放行其他模块名（旧代码的 lambda 里保留了 `native_import` 兜底），任何遗漏都会引发连锁错误。

**练习 2**：`CompleteCodeVerifier` 为什么把测试按 2000 个一组分区、分数连乘，而不是一次全发？

**答案**：服务端与客户端对单请求的测试数、字节数都有限制（库内 `protocol.MAX_TESTS = 2000`），分区保证每批可执行；连乘语义等价于「全部测试都通过才得 1 分」，与 `pass_rate_reward_threshold=1.0` 的全对要求一致。

**练习 3**：`CodeVerifier.async_call`（verifier.py）在什么情况下返回 0 分、什么情况下抛异常？为什么要区分？

**答案**：抽不出合法 Python 代码（`invalid_code`）或测试未全过（pass_fraction 低于阈值）返回 0 分——这是**完成了判定**的真实结果；缺 token、服务 429/5xx 重试耗尽、响应非法等抛 `ServiceError`/`ValueError`——这些是**没完成判定**的故障。混同两者会把「服务宕机」误当「模型答错」，向训练注入虚假的负信号。

## 5. 综合实践

把本讲四条线索（判分链路、沙箱防护、两种判定格式、库内客户端衔接）串成一个任务：

**任务：搭建一个 3 样本代码判分实验台，并追踪 MILES 的判分适配链。**

1. **准备样本**：写三个程序与配套断言测试（示例代码，非项目原有）：
   ```python
   SAMPLES = [
       ("def add(a, b):\n    return a + b\n",
        ["assert add(1, 2) == 3", "assert add(-1, 1) == 0", "assert add(0, 0) == 1"]),   # 2/3 通过
       ("def add(a, b):\n    return a - b\n",
        ["assert add(1, 2) == 3", "assert add(-1, 1) == 0", "assert add(0, 0) == 1"]),   # 0/3 通过（含 bug）
       ("def add(a, b:\n    return a + b\n",
        ["assert add(1, 2) == 3"]),                                                       # 语法错误 → 0/1
   ]
   ```
2. **直接判分**：在仓库根目录用 `uv run python` 调 `open_instruct.code_utils.code_utils.get_successful_tests_fast`（沙箱内执行，注意别在重要目录跑），打印每个样本的 `results` 与通过率，验证：含 bug 实现得 0/3、语法错误实现得 0/1（程序 `exec` 失败时全部测试记 0，见 [code_utils.py:106-110](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/code_utils/code_utils.py#L106-L110)）。
3. **换 HTTP 通道**：按 4.1.4 启动 `api.py` 服务，用 `/test_program` 对同样的样本判分，核对与第 2 步结果一致（同一套底层函数，只是包了 HTTP）。
4. **追踪适配链**：按 4.5.4 的步骤运行 `open_instruct/test_complete_code.py`，画出从 `CompleteCodeVerifier.async_call` → `execution_verifier.CodeVerifier.async_call` → `CodeExecutionClient.execute` 的调用图，并在 `CodeVerifier.async_call` 那一层标注「哪三种情况给 0 分、哪些情况必须抛异常」。
5. **记录**：把三样本的通过率表格和调用图写进笔记；凡是没实际运行的部分标注「待本地验证」。

## 6. 本讲小结

- 代码类可验证奖励的核心是「真的运行」：`CodeVerifier`（注册表里的 `"code"`）把模型输出抽出的 Python 代码连同测试用例发给执行服务，按通过率（可阈值整流、可按耗时打折）打分。
- 判分服务端的沙箱是三层防护的组合：`should_execute` 关键字黑名单直接拒执行、每测试独立子进程 + `join(timeout)`/`kill()` 超时击杀、`reliability_guard` 屏蔽破坏性函数——但源码自己声明这不是安全沙箱，真正的隔离靠部署边界。
- 测试有两种格式：assert 式（每测试一进程、断言不抛异常即过）与 stdio 式（`grade_stdio` 单进程串行、mock stdin、捕获 stdout、先字符串后 Decimal 数值比对，错误码 -1/-2/-3/-4 供诊断）。
- `generic_sandbox` 是另一类沙箱：面向多轮工具调用 RL 的 Docker 容器环境，靠 `_BASH_WRAPPER` 两个临时文件实现跨调用的 cwd/env 持久化，奖励来自每步退出码。
- 本轮更新把外部私有的 `code_execution_client` 移入库内 `open_instruct/code_utils/execution_client`：客户端只编码/校验/判定、绝不执行代码，`ServiceError`（重试后上抛）与 `ClientError`（立即失败）的二分保证「服务故障绝不等于测试失败」；`complete_code` 改为直接导入，测试 mock 从劫持 `importlib` 简化为给 `complete_code.execution_verifier` 打属性补丁。

## 7. 下一步学习建议

- **下一篇（u6-l8）**：`代码执行客户端：execution_client 协议与判分适配` 将深入本讲 4.5 只带过的部分——`protocol.py` 的受限 unpickler 与线格式限制、`livecodebench.py`/`nemo_comparison.py` 的测试标签构造、客户端的分批与重试细节，并配合四个 `test_code_execution_client*.py` 测试文件实践 HTTP 层 mock。
- **MILES 奖励全景（u8-l6）**：看 `complete_code` 如何与可信注册表、math_verifier 的守卫机制并列，成为 MILES 奖励体系的一等公民，以及判分器依赖如何随镜像分发、`CODE_EXECUTION_TOKEN` 如何经 `launch.secrets` 注入。
- **源码延伸阅读**：`open_instruct/code_utils/api.py` 的文档字符串（本地起服务的完整命令）、`tests/test_generic_sandbox.py`（不依赖 Docker 的环境测试范式）、`open_instruct/grpo_fast.py` 中引用 `GenericSandboxEnv` 的工具环境组装处（约 L2105 附近），把「判分沙箱」与「交互沙箱」在训练脚本里的接入位置对上号。
