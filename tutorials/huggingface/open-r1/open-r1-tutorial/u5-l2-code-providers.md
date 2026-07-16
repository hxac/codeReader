# 代码执行 Provider 抽象

## 1. 本讲目标

上一讲（u5-l1）我们把代码奖励的判分内核 `evaluation_script_template` 拆完了：模型生成的代码被渲染成一段完整 Python 脚本，送进一个「沙箱」里跑测试用例，再用 `float(结果)` 把通过率回收成奖励。当时我们刻意把「沙箱到底是什么、怎么把成百上千段脚本送进去」当成黑盒。

本讲就打开这个黑盒。读完本讲你应当能够：

1. 说清 `CodeExecutionProvider` 抽象基类定下的「契约」，以及 `get_provider` 工厂如何用一行配置切换沙箱后端。
2. 读懂 `E2BProvider` 的异步执行链路：同步入口 → `asyncio.run` → 信号量限流 → `asyncio.gather` 并发 → 超时兜底，并解释几个「魔数」超时的来历。
3. 读懂 `MorphProvider` 如何用 `asyncio.to_thread` 把同步的 Sandbox API 包装成可并发执行，以及它比 E2B 更繁琐的结果解析逻辑。
4. 动手实现一个 `LocalProvider`，在本地用 `subprocess` 跑渲染好的脚本，并让它被 `get_provider` 选中。

## 2. 前置知识

本讲是专家层内容，默认你已经学过 u1～u4 和 u5-l1。下面几个概念会反复出现，先简单过一遍。

- **沙箱（sandbox）**：一个隔离的、用完即弃的代码执行环境。open-r1 不能直接在训练机的 Python 进程里跑用户/模型生成的代码（会污染训练环境、可能死循环），而是把每段代码丢进一个独立沙箱执行，拿到 stdout 后销毁沙箱。E2B、MorphCloud 都是第三方云沙箱服务。
- **奖励函数（reward function）**：GRPO 训练里给模型回答打分的函数，输入一批 completion，输出一批 `float`。代码奖励的特殊之处在于：它的「打分」必须真去跑代码。详见 u3-l1、u5-l1。
- **抽象基类（ABC）与工厂（factory）**：`abc.ABC` + `@abc.abstractmethod` 定义「所有 Provider 都必须实现 `execute_scripts`」的契约；`get_provider` 是工厂函数，根据字符串名返回具体实现。这是「面向接口编程」的标准组合。
- **`asyncio` 并发原语**：
  - `asyncio.run(coro)`：在同步代码里启动一个事件循环跑完协程。open-r1 的奖励函数是**同步**的（TRL 调用时不 `await`），但沙箱 API 是**异步**的，于是需要 `asyncio.run` 做同步↔异步桥接。
  - `asyncio.Semaphore(n)`：信号量，限制「同时最多 n 个」协程进入临界区，防止一次性开几百个云沙箱把额度打爆。
  - `asyncio.gather(*tasks)`：并发调度一批协程，等它们全部完成，按提交顺序返回结果列表。
  - `asyncio.wait_for(coro, timeout)`：给协程设一个硬超时，超时就抛 `TimeoutError`。
  - `asyncio.to_thread(func, ...)`：把一个**同步阻塞**函数扔到线程池里跑，返回可 `await` 的对象——用于让同步 API 不卡住事件循环。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/open_r1/utils/code_providers.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py) | 本讲主角。定义抽象基类 `CodeExecutionProvider`、两个具体实现 `E2BProvider`/`MorphProvider`、工厂 `get_provider`。 |
| [src/open_r1/utils/import_utils.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/import_utils.py) | 用 `transformers` 的工具检测 `e2b` / `morphcloud` 是否安装，让 `code_providers.py` 可以做条件 import。 |
| [src/open_r1/rewards.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py) | `code_reward`（约 586–592 行）调用 `get_provider(...)` 再 `execute_scripts(...)`，是 Provider 的唯一调用方。 |
| [src/open_r1/configs.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py) | `GRPOScriptArguments` 里的 `code_provider`、`parallel_code_exec_per_proc`、`e2b_router_url`、`morph_router_url` 等字段，经注册表流入 Provider。 |
| [README.md](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md) | 255–285 行讲 E2B / Morph 的 API key 配置与 `provider_type` 切换。 |

> 提示：`code_providers.py` 顶部的条件 import 还会引入 `routed_sandbox.py` / `routed_morph.py`，那是「Router 路由沙箱」模式，本讲只在「它提供了批量执行分支」这一层提及，完整拆解放到下一讲 u5-l3。

## 4. 核心概念与源码讲解

### 4.1 CodeExecutionProvider 抽象与 get_provider 工厂

#### 4.1.1 概念说明

回到 u5-l1 的结论：`code_reward` 把一批 completion 渲染成一整批完整 Python 脚本（`scripts: list[str]`），然后需要一个「执行器」把它们送进沙箱、回收一批 `float` 奖励。问题来了——这个执行器该用哪家云沙箱？E2B 还是 Morph？以后会不会还想加一个本地执行？

open-r1 用经典的「**抽象基类 + 工厂**」来解耦这件事：

- **抽象基类 `CodeExecutionProvider`** 只规定「契约」——任何执行器都必须提供一个 `execute_scripts(scripts, languages) -> List[float]` 方法。它不关心你是连云沙箱还是本地起子进程，只要签名对、语义对（一段脚本对应一个 float）就行。
- **具体实现** `E2BProvider` / `MorphProvider` 各自实现这个方法。
- **工厂 `get_provider(provider_type, **kwargs)`** 根据 YAML 里写的字符串（`"e2b"` / `"morph"`）返回对应实例。

好处是：奖励函数 `code_reward` 永远只和抽象基类打交道，切换沙箱后端只需在 YAML 改一个字段，不用动奖励逻辑。

#### 4.1.2 核心流程

从 YAML 配置到奖励回收的完整链路：

```text
YAML: code_provider: e2b   parallel_code_exec_per_proc: 2
        │  （TrlParser 解析进 GRPOScriptArguments）
        ▼
rewards.get_reward_funcs: 注册表把 "code" 映射到
        partial(code_reward, provider_type=script_args.code_provider,
                           num_parallel=script_args.parallel_code_exec_per_proc)
        │  （GRPOTrainer 训练时调用 code_reward）
        ▼
code_reward: 渲染出 scripts（一批完整脚本）
        │
        ▼
get_provider(provider_type="e2b", num_parallel=2, **kwargs)
        │  （工厂 dispatch）
        ▼
E2BProvider(num_parallel=2).execute_scripts(scripts, ["python"]*N)
        │
        ▼
List[float]  ← 每段脚本一个奖励
```

工厂内部还有一个细节：它从 `kwargs` 里 **`pop`** 出通用参数 `num_parallel` 和各家专属参数（`e2b_router_url` / `morph_router_url`），再构造对应 Provider，避免把无关参数透传进构造函数。

#### 4.1.3 源码精读

抽象基类只有一个抽象方法，签名就是契约本体：

[CodeExecutionProvider 抽象基类 · src/open_r1/utils/code_providers.py#L46-L60](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L46-L60) —— 用 `abc.ABC` + `@abc.abstractmethod` 强制子类实现 `execute_scripts`，返回 `List[float]`。

工厂函数按 `provider_type` 分发，未知类型直接抛 `ValueError`：

[get_provider 工厂 · src/open_r1/utils/code_providers.py#L339-L366](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L339-L366) —— 第 349 行先 `pop` 出公共的 `num_parallel`（默认 2），第 351–364 行两个分支各自 `pop` 掉自家专属参数再构造实例。

工厂的唯一调用方就是 `code_reward`，注意它把整批脚本**一次性**送进 Provider，而不是逐条送——这是后续并发优化的前提：

[get_provider 的调用点 · src/open_r1/rewards.py#L586-L592](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L586-L592) —— 第 586 行 `get_provider(...)` 拿到执行器，第 592 行 `execute_scripts(scripts, ["python"] * len(scripts))` 回收奖励。

配置侧，YAML 里的 `code_provider` 字段（注意：注册表里叫 `provider_type`，YAML 里叫 `code_provider`，二者经 `partial` 绑定）合法取值在 `configs.py` 里声明：

[code_provider 配置字段 · src/open_r1/configs.py#L308-L314](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L308-L314) —— `choices` 列了 `["e2b", "local", "morph"]` 三种。

> ⚠️ **一个值得注意的「缺口」**：`configs.py` 把 `"local"` 列为合法 `code_provider` 取值，但 `get_provider` 只实现了 `"e2b"` 和 `"morph"` 两个分支，传 `"local"` 会落到第 365–366 行的 `else` 抛 `ValueError`。也就是说「本地执行」是一个**文档里承诺、代码里空缺**的功能——本讲的综合实践正是来补上它。

#### 4.1.4 代码实践

**目标**：在不安装任何云沙箱 SDK 的前提下，验证工厂的分发逻辑与「未知类型抛错」行为。

**步骤**：

1. 打开 `code_providers.py`，确认 `get_provider` 的两个分支与 `else: raise ValueError`。
2. 在一个装了 open-r1（`pip install -e .`，无需 e2b/morphcloud）的环境里运行下面这段（**示例代码**，非项目原有）：

```python
# 示例代码：观察工厂分发
from open_r1.utils.code_providers import get_provider, CodeExecutionProvider

# 1) "e2b" 会返回 E2BProvider 实例（只要不调用 execute_scripts，就不会真的连云）
p = get_provider(provider_type="e2b", num_parallel=4)
print(type(p).__name__, isinstance(p, CodeExecutionProvider), p.num_parallel)

# 2) 未知类型应当抛 ValueError
try:
    get_provider(provider_type="local")
except ValueError as e:
    print("ValueError:", e)
```

**需要观察的现象**：第一行打印 `E2BProvider True 4`（说明工厂确实把 `num_parallel=4` 透传进去了，且实例是抽象基类的子类）；第二段打印 `ValueError: Unknown provider type: local`，呼应上面提到的「缺口」。

**预期结果**：`E2BProvider True 4` 与 `ValueError: Unknown provider type: local`。注意：`E2BProvider.__init__` 在第 73 行会检查 `is_e2b_available()`，**未安装 e2b 时构造就会抛 `ImportError`**——若你环境里没装 e2b，本练习第 1 步会先报 `ImportError`，这正是下一节要讲的「条件 import + 可用性检查」机制。遇到这种情况属于正常现象，把注意力放在 `get_provider` 的分支结构即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_provider` 用 `kwargs.pop(...)` 取参数，而不是直接读 `kwargs[...]`？
**答案**：`pop` 在取出后会从 `kwargs` 删除该键。这样公共参数（`num_parallel`）和各家专属参数（`e2b_router_url` 等）被「消费」掉，不会作为未知关键字传进 Provider 构造函数引发 `TypeError`。

**练习 2**：如果把 `get_provider` 改成直接 `return E2BProvider()` 或 `return MorphProvider()`（即不经过工厂），会失去什么？
**答案**：失去「用字符串名解耦」的能力——`code_reward` 里写死了具体类，切换沙箱就得改奖励代码；而且 `provider_type` 来自 YAML，绕过工厂就没法把「配置」和「实现」分开。

---

### 4.2 E2BProvider 异步执行、信号量与超时

#### 4.2.1 概念说明

`E2BProvider` 是默认后端，对接 [E2B](https://e2b.dev/) 的云沙箱。它的难点不在「连云」，而在「**如何把一批脚本又快又稳地跑完**」：

- **快**：一段脚本 = 开一个沙箱 = 一次网络往返。GRPO 一个 batch 可能有几百段脚本，串行跑会慢到训练卡死。必须**并发**。
- **稳**：并发不能无上限——E2B 的免费档同时只能开几个沙箱，开多了会限流甚至报错。需要一个**并发上限**（信号量）。
- **不崩训练**：云沙箱可能超时、可能返回无法解析的垃圾。但奖励函数**绝不能抛异常**（会让整个 GRPO step 失败），所以任何错误都要兜底成 `0.0`。

E2B 还提供一个「Router 批量模式」：当配置了 `e2b_router_url` 时，不再自己开沙箱，而是把整批脚本 POST 给一个自建的 router 服务（见 u5-l3），由它批量执行。这一支走 `RoutedSandbox`，逻辑很简单；本节聚焦更有教学价值的「直连异步」分支。

#### 4.2.2 核心流程

`execute_scripts` 是**同步**入口（TRL 要求奖励函数同步返回），但内部跑**异步**沙箱。它通过三层调用把同步桥接到异步：

```text
execute_scripts(scripts, languages)            # 同步入口
  ├── if e2b_router_url:                        # Router 批量分支
  │     RoutedSandbox(...).run_code(scripts, languages, timeout=30, request_timeout=28)
  │     → 逐个 float(execution.text)，失败记 None
  │
  └── else:                                     # 直连异步分支
        _run_async_from_sync(scripts, languages, num_parallel)
          └── asyncio.run( _run_async(...) )    # 启动事件循环
                │
                ▼
        _run_async(scripts, languages, num_parallel)
          semaphore = Semaphore(num_parallel)   # 并发上限
          tasks = [_run_script(s, ..., semaphore) for s in scripts]
          return await asyncio.gather(*tasks)   # 并发 + 按序回收
                │
                ▼
        _run_script(script, semaphore):         # 每段脚本一个协程
          async with semaphore:                 # 抢信号量，最多 N 个同时进
            sandbox = await AsyncSandbox.create(timeout=30, request_timeout=28)
            execution = await asyncio.wait_for(sandbox.run_code(...), timeout=32)
            return float(execution.text)
          finally: await sandbox.kill()         # 无论成败都销毁沙箱
```

几个关键设计：

- **信号量限流**：`async with semaphore` 保证「同时进入临界区的协程 ≤ `num_parallel`」。设 `num_parallel=2` 时，即便有 256 段脚本，任意时刻也最多只有 2 个沙箱在跑。
- **三层超时**（本节最重要的细节）。`_run_script` 里定义了三个时间常量：

  \[ \texttt{REQUEST\_TIMEOUT} = \texttt{SANDBOX\_TIMEOUT} - \texttt{MARGIN},\quad \texttt{ASYNCIO\_TIMEOUT} = \texttt{SANDBOX\_TIMEOUT} + \texttt{MARGIN} \]

  代入 `SANDBOX_TIMEOUT=30`、`MARGIN=2`，得到 `REQUEST_TIMEOUT=28`、`ASYNCIO_TIMEOUT=32`。源码注释解释：**E2B `AsyncSandbox` 自带的 timeout 看起来并不生效**，所以他们额外套一层 `asyncio.wait_for(..., timeout=32)` 做硬兜底；`request_timeout=28` 略小于沙箱超时 30，让 HTTP 请求先超时；`asyncio` 超时 32 略大于沙箱超时，保证「沙箱自己先判超时 → 再由 asyncio 兜底」的先后顺序。这些数字是用 256 条 gold solution 实测标定的（见注释引用的 `scripts/benchmark_e2b.py`）。
- **结果回收**：`float(execution.text)`。E2B 沙箱像 Jupyter Notebook 一样会捕获脚本**最后一个表达式的值**——这正是 u5-l1 讲过的 `evaluation_script_template` 末行 `evaluate_code(...)` 能被回收到奖励的原因。
- **全链路兜底**：`_run_script` 里 `TypeError/ValueError`（`float()` 解析失败）、`TimeoutError`、其他异常**统统返回 `0.0`**；`finally` 里无论如何都 `kill()` 沙箱；最外层 `execute_scripts` 还有一层 `try/except`，整个异步流程抛错时返回 `[0.0] * len(scripts)`。设计意图：**奖励函数永不抛异常**。

#### 4.2.3 源码精读

条件 import 与可用性检查：未装 e2b 时把 `AsyncSandbox` 等设为 `None`，真正构造 Provider 时才在 `__init__` 抛 `ImportError`：

[条件 import · src/open_r1/utils/code_providers.py#L25-L33](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L25-L33) —— 这依赖 `is_e2b_available()`，定义在 `import_utils.py`。

[is_e2b_available · src/open_r1/utils/import_utils.py#L19-L23](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/import_utils.py#L19-L23) —— 复用 `transformers.utils._is_package_available("e2b")` 做检测，把「可选依赖是否就绪」这件事交给 transformers 的成熟实现。

`__init__` 记下并发数与 router URL，并做可用性检查：

[E2BProvider.__init__ · src/open_r1/utils/code_providers.py#L63-L80](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L63-L80) —— 第 73–77 行未装 e2b 直接抛 `ImportError` 并给出安装提示。

同步入口 + 双分支：

[E2BProvider.execute_scripts · src/open_r1/utils/code_providers.py#L82-L113](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L82-L113) —— 第 88–105 行是 Router 分支（`float(execution.text)`，失败记 `None`，注意这里失败给的是 `None` 而非 `0.0`）；第 107–113 行是直连分支，整段 `try/except` 兜底成 `[0.0] * len(scripts)`。

异步桥接与信号量并发：

[_run_async_from_sync 与 _run_async · src/open_r1/utils/code_providers.py#L115-L133](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L115-L133) —— 第 118 行 `asyncio.run` 桥接同步↔异步；第 126 行建信号量；第 128 行为每段脚本建一个协程任务；第 130 行 `gather` 并发并按序返回。

单脚本执行 + 三层超时 + 兜底 + 销毁：

[_run_script · src/open_r1/utils/code_providers.py#L135-L166](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L135-L166) —— 第 141–144 行是三个超时常量；第 146 行 `async with semaphore` 限流；第 148 行建沙箱；第 149–152 行 `wait_for` 硬兜底；第 153 行 `float(execution.text)` 回收；第 154–161 行三类异常全返回 `0.0`；第 162–166 行 `finally` 里 `kill()`。

#### 4.2.4 代码实践

**目标**：在不连真实 E2B 的情况下，理解「信号量如何把并发压在上限」和「`asyncio.wait_for` 超时兜底」。我们用**示例代码**写一个同构的最小异步骨架，把 `AsyncSandbox.run_code` 换成一个会 sleep 的假函数。

**步骤**：

```python
# 示例代码：复刻 E2BProvider 的信号量+超时结构（非项目原有代码）
import asyncio, time

async def fake_run(script_id):
    # 假装每个沙箱跑 1 秒
    await asyncio.sleep(1)
    return f"ok-{script_id}"

async def _run_script(sid, semaphore, timeout):
    async with semaphore:                 # 对应 code_providers.py#L146
        try:
            res = await asyncio.wait_for(fake_run(sid), timeout=timeout)  # 对应 L149-L152
            return float(sid)             # 对应 L153 float(execution.text)
        except asyncio.TimeoutError:
            print(f"  [{sid}] timed out")
            return 0.0                    # 对应 L156-L158

async def _run_async(num_scripts, num_parallel, timeout):
    semaphore = asyncio.Semaphore(num_parallel)               # 对应 L126
    tasks = [_run_script(i, semaphore, timeout) for i in range(num_scripts)]
    return await asyncio.gather(*tasks)                       # 对应 L130

if __name__ == "__main__":
    N, P = 6, 2
    t0 = time.time()
    rewards = asyncio.run(_run_async(N, P, timeout=0.5))      # 把超时设小，观察兜底
    print("rewards:", rewards, "elapsed=%.2fs" % (time.time() - t0))
```

**需要观察的现象**：

- 把 `timeout=0.5`（小于每个任务 1 秒），所有任务都会触发 `asyncio.TimeoutError`，`rewards` 全是 `0.0`——对应真实代码「沙箱超时 → 奖励归零」。
- 把 `timeout` 改成 `2.0`，`rewards` 变成 `[0.0, 1.0, 2.0, 3.0, 4.0, 5.0]`（`float(sid)`）。
- 关注耗时：6 个任务、并发上限 2、每个 1 秒，理论耗时约 \( \lceil 6/2 \rceil \times 1 = 3 \) 秒。实测应在 3 秒上下——这就是信号量「把并发压在 2」的直接证据。

**预期结果**：`timeout=0.5` 时全 `0.0`；`timeout=2.0` 时返回 `[0,1,2,3,4,5]` 且耗时约 3 秒。本实践不依赖 E2B，可在纯 CPU 环境复现。**待本地验证**：精确耗时以你机器为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ASYNCIO_TIMEOUT (32)` 要**大于** `SANDBOX_TIMEOUT (30)`，而不是相等？
**答案**：让沙箱自己的超时先生效（30 秒时沙箱会停掉并返回部分结果或错误），`asyncio.wait_for` 作为**兜底**只在沙箱超时机制失灵（注释说它「看起来不生效」）时才在 32 秒强行中断。若两者相等或 asyncio 更小，就会抢在沙箱正常返回前误杀。

**练习 2**：Router 分支里解析失败给的是 `None`，直连分支兜底给的是 `0.0`。这俩在下游会有什么不同？
**答案**：回顾 u3-l2 / u5-l1——`None` 奖励会被 `GRPOTrainer` 当作「跳过该样本、不奖不罚、不计入优势」；而 `0.0` 是一个真实的负向信号（明确判错）。所以 Router 分支对「解析失败」更保守（不惩罚），直连分支则一律当错。

---

### 4.3 MorphProvider 的 client/Sandbox 与结果解析

#### 4.3.1 概念说明

`MorphProvider` 对接 [MorphCloud](https://www.morphcloud.ai/)，和 E2B 是同构替代品：同样实现 `execute_scripts`、同样有 Router 模式、同样用信号量限流。但它有两处关键差异，正是本节要讲的：

1. **MorphCloud 的 SDK 是同步的**。`Sandbox.new(...)` 和 `sandbox.run_code(...)` 都是阻塞调用，没有 `async` 版本。直接在事件循环里 `await` 它们会卡死整个循环。MorphProvider 的解法是用 `asyncio.to_thread(...)` 把这些同步调用扔到**线程池**里执行，从而不阻塞其他协程。这是「用同步 SDK 也能异步并发」的标准套路。
2. **结果回收更繁琐**。E2B 的 `execution.text` 干净地给出末行表达式值；Morph 的返回对象结构不同，需要按 `result.text → 末行 → 整段 → result.stdout` 多级 fallback 才能抠出奖励 `float`。

此外 MorphProvider 在 `__init__` 里就完成了「连云准备」：读 `.env`、取 `MORPH_API_KEY`、构造 `MorphCloudClient`、缓存 `Sandbox` 类引用。而 E2B 的 `__init__` 只是记参数，沙箱在每次 `_run_script` 里现建现销。

#### 4.3.2 核心流程

```text
__init__(num_parallel, morph_router_url):
  load_dotenv()                         # 读 .env 里的 MORPH_API_KEY
  if morph_router_url:                  # Router 分支：只建 RoutedMorphSandbox 就 return
  else:
    api_key = getenv("MORPH_API_KEY")   # 缺 key 直接 ValueError
    self.client = MorphCloudClient(api_key=...)   # 缓存客户端
    self.Sandbox = Sandbox              # 缓存类引用，供 _run_script 用

execute_scripts(scripts, languages):
  if hasattr(self, "routed_sandbox"):   # Router 批量分支
     results = routed_sandbox.run_code(..., timeout=90, request_timeout=96)
     → 逐个抠 result.text 的 float，失败给 0.0
  else:                                 # 直连异步分支
     asyncio.run(_run_async(...))

_run_script(script, semaphore):         # SANDBOX_TIMEOUT=90, MARGIN=6, ASYNCIO_TIMEOUT=96
  async with semaphore:
    sandbox = await asyncio.to_thread(self.Sandbox.new, client=..., ttl_seconds=90)  # 同步→线程池
    result  = await asyncio.wait_for(
                  asyncio.to_thread(sandbox.run_code, script, languages=..., timeout=90),
                  timeout=96)
    reward = parse(result)              # 多级 fallback 抠 float
  finally:
    to_thread(sandbox.close); to_thread(sandbox.shutdown)   # 两步销毁
```

两个值得对比的细节：

- **超时常量更大**：`SANDBOX_TIMEOUT=90`、`MARGIN=6`，远大于 E2B 的 30/2。因为 Morph 沙箱启动（`Sandbox.new`）比 E2B 重，需要更长的容忍窗口。
- **销毁是两步**：E2B 只 `kill()`；Morph 要先 `close()` 再 `shutdown()`，且都包在 `try/except: pass` 里——销毁失败也不影响奖励回收。
- **解析多级 fallback**（`_run_script` 第 302–322 行）：先看 `result.text`，取其**最后一行**转 float；失败再拿 `result.text` 整段转 float；再失败看 `result.stdout` 最后一行；全失败就维持默认 `0.0`。这层健壮性是因为 Morph 返回的文本里可能混入日志/警告，奖励值通常在最后一行。

#### 4.3.3 源码精读

构造：dotenv + API key + 客户端，Router 与直连两条初始化路径互斥：

[MorphProvider.__init__ · src/open_r1/utils/code_providers.py#L169-L209](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L169-L209) —— 第 185–190 行 `load_dotenv`（没装 python-dotenv 只警告不报错）；第 195–197 行 Router 分支建好 `routed_sandbox` 就 `return`；第 201–203 行缺 `MORPH_API_KEY` 抛 `ValueError`；第 206–207 行缓存 `client` 与 `Sandbox` 类。

执行入口，结构与 E2B 同构：

[MorphProvider.execute_scripts · src/open_r1/utils/code_providers.py#L211-L251](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L211-L251) —— 第 222 行用 `hasattr(self, "routed_sandbox")` 判断走哪条；Router 分支解析失败给 `0.0`（注意和 E2B Router 分支给 `None` 不同）；直连分支第 246 行 `asyncio.run`。

异步调度（与 E2B 几乎相同，可对照 4.2.3）：

[MorphProvider._run_async · src/open_r1/utils/code_providers.py#L253-L271](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L253-L271) —— 同样是信号量 + gather。

单脚本执行：核心是 `asyncio.to_thread` 包装同步 SDK，以及多级结果解析：

[MorphProvider._run_script · src/open_r1/utils/code_providers.py#L273-L336](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L273-L336) —— 第 291 行 `to_thread(self.Sandbox.new, ...)` 把同步构造扔进线程池；第 292–300 行 `wait_for(to_thread(sandbox.run_code, ...))` 同理；第 302–322 行多级 fallback 抠 `float`；第 330–336 行 `finally` 里两步销毁 `close` + `shutdown`。

可用性检测与 E2B 对称：

[is_morph_available · src/open_r1/utils/import_utils.py#L26-L30](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/import_utils.py#L26-L30) —— 检测 `morphcloud` 包；条件 import 在 [code_providers.py#L35-L43](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L35-L43)。

#### 4.3.4 代码实践

**目标**：体会「同步 SDK + `asyncio.to_thread` = 可并发」这条套路，以及多级 fallback 解析的必要性。

**步骤**：运行下面这段**示例代码**，它用同步的 `time.sleep` 模拟 Morph 的阻塞 SDK，复刻 `to_thread` 并发与结果解析：

```python
# 示例代码：复刻 MorphProvider 的 to_thread 并发 + 多级解析（非项目原有代码）
import asyncio, time

class FakeResult:                   # 模拟 Morph 返回对象
    def __init__(self, text, stdout=None):
        self.text, self.stdout = text, stdout

def blocking_run_code(script):      # 同步阻塞，模拟 Sandbox.run_code
    time.sleep(1)                   # 关键：这是同步 sleep，会阻塞线程而非协程
    if script == "good":   return FakeResult("log line\n0.75")     # text 末行可解析
    if script == "whole":  return FakeResult("0.5")                 # 整段可解析
    if script == "stdout": return FakeResult(None, "0.25")          # 退到 stdout
    return FakeResult("garbage")                                    # 全失败

def parse(result, default=0.0):     # 复刻 code_providers.py#L302-L322 的多级 fallback
    reward = default
    try:
        if getattr(result, "text", None):
            lines = result.text.strip().split("\n")
            if lines:
                try: reward = float(lines[-1])
                except ValueError:
                    try: reward = float(result.text.strip())
                    except ValueError: pass
        elif getattr(result, "stdout", None):
            lines = result.stdout.strip().split("\n")
            if lines:
                try: reward = float(lines[-1])
                except ValueError: pass
    except (ValueError, AttributeError):
        pass
    return reward

async def _run_script(script, semaphore):
    async with semaphore:
        result = await asyncio.wait_for(asyncio.to_thread(blocking_run_code, script), timeout=5)
        return parse(result)

async def _run_async(scripts, num_parallel):
    semaphore = asyncio.Semaphore(num_parallel)
    return await asyncio.gather(*[_run_script(s, semaphore) for s in scripts])

if __name__ == "__main__":
    scripts = ["good", "whole", "stdout", "bad"]
    t0 = time.time()
    print(asyncio.run(_run_async(scripts, num_parallel=2)))
    print("elapsed=%.2fs" % (time.time() - t0))
```

**需要观察的现象**：

- 输出应为 `[0.75, 0.5, 0.25, 0.0]`，正好演示四级 fallback：`good` 取 text 末行 `0.75`、`whole` 取整段 `0.5`、`stdout` 退到 stdout `0.25`、`bad` 全失败维持默认 `0.0`。
- 耗时：4 个任务每个 1 秒、并发上限 2，约 \( \lceil 4/2\rceil \times 1 = 2 \) 秒。注意这里用的是**同步** `time.sleep`——若没有 `to_thread`，它会卡死事件循环、4 个任务会串行成 4 秒；有了 `to_thread`，它们在线程池里并发，耗时压到约 2 秒。这正是 MorphProvider 必须用 `to_thread` 的原因。

**预期结果**：`[0.75, 0.5, 0.25, 0.0]`，耗时约 2 秒。纯 CPU 可复现，**待本地验证**精确耗时。

#### 4.3.5 小练习与答案

**练习 1**：如果 MorphProvider 不用 `asyncio.to_thread`，直接 `sandbox = self.Sandbox.new(...)`，会发生什么？
**答案**：`Sandbox.new` 是同步阻塞调用（内部有网络 I/O）。直接在 `_run_script` 协程里调用会**阻塞整个事件循环**——其他协程（其他脚本）无法被调度，于是信号量限的「并发」退化成「串行」，所有脚本一个接一个跑，失去并发加速。

**练习 2**：E2B 的 Router 分支解析失败给 `None`，Morph 的 Router 分支给 `0.0`（见 4.3.3）。结合 u5-l1 的「`None` 表示跳过样本」，说明这种不一致可能带来什么影响。
**答案**：同样的「解析失败」，E2B-Router 路径会让样本被跳过（不参与优势估计），Morph-Router 路径会把样本判为全错（给负梯度）。混用或切换后端时，相同的模型输出可能得到不同的训练信号。这是一处潜在的坑，排错时要留意后端差异。

---

## 5. 综合实践

把三个模块串起来：实现一个 `LocalProvider`，填补 4.1.3 指出的「`configs.py` 承诺了 `local`、`get_provider` 却没实现」的缺口。

**任务**：写一个继承 `CodeExecutionProvider` 的 `LocalProvider`，用 `subprocess` 在本地 `python3` 里直接跑 `code_reward` 渲染出的脚本，回收奖励；并让它能被 `get_provider("local")` 选中。

**关键提示（最容易踩的坑）**：渲染出的 `evaluation_script_template` 末行是裸的 `evaluate_code(code_snippet, test_cases)` 调用。在 E2B/Morph 沙箱里，沙箱像 Notebook 一样会**捕获末行表达式的值**；但用普通 `python3 -c` 跑，这个返回值**不会打印到 stdout**，`float()` 无从解析。所以 LocalProvider 要么改写脚本末行成 `print(...)`，要么用 `exec` 捕获。下面采用「字符串替换末行为 print」的最简方案。

**步骤**：

1. 写一个最小渲染脚本，确认你能复现「末行不打印」的现象：

```python
# 示例代码：复现「裸调用不打印」（非项目原有代码）
template_end = "    evaluate_code(code_snippet, test_cases)"
script = (
    "def evaluate_code(c, t):\n"
    "    return 0.42\n"
    "code_snippet, test_cases = 'x', []\n"
    + template_end
)
import subprocess
proc = subprocess.run(["python3", "-c", script], capture_output=True, text=True)
print("stdout=", repr(proc.stdout))   # 期望：空——返回值没被打印
```

2. 实现 `LocalProvider`，把末行裸调用替换成 `print(...)`，再跑、再解析：

```python
# 示例代码：LocalProvider 骨架（非项目原有代码）
import subprocess
from open_r1.utils.code_providers import CodeExecutionProvider

class LocalProvider(CodeExecutionProvider):
    """在本地用 subprocess 直接跑渲染好的脚本，回收末行 float 作为奖励。"""

    def __init__(self, num_parallel: int = 2, timeout: int = 30):
        self.num_parallel = num_parallel
        self.timeout = timeout

    def _run_one(self, script: str) -> float:
        # 关键修复：模板末行是裸调用，本地 python 不会打印其返回值
        runnable = script.replace(
            "evaluate_code(code_snippet, test_cases)",
            "print(evaluate_code(code_snippet, test_cases))",
        )
        try:
            proc = subprocess.run(
                ["python3", "-c", runnable],
                capture_output=True, text=True, timeout=self.timeout,
            )
            lines = (proc.stdout or "").strip().splitlines()
            return float(lines[-1]) if lines else 0.0
        except Exception as e:
            print(f"LocalProvider error: {e}")
            return 0.0

    def execute_scripts(self, scripts, languages):
        # 骨架版：串行执行。进阶可像 E2BProvider 那样用 asyncio + Semaphore 并发。
        return [self._run_one(s) for s in scripts]
```

3. 让 `get_provider("local")` 能选中它。因为本教程不能改源码，这里用 **monkeypatch** 注入（生产代码里应在 `get_provider` 加一个 `elif provider_type == "local": return LocalProvider(...)` 分支）：

```python
# 示例代码：不改源码，让 get_provider 识别 "local"（非项目原有代码）
import open_r1.utils.code_providers as cp
_orig_get_provider = cp.get_provider

def patched_get_provider(provider_type="e2b", **kwargs):
    if provider_type == "local":
        return LocalProvider(num_parallel=kwargs.pop("num_parallel", 2))
    return _orig_get_provider(provider_type, **kwargs)

cp.get_provider = patched_get_provider   # 让 rewards.code_reward 走到 LocalProvider
```

4. 端到端验证：用一段假代码 + 假 `verification_info` 走一遍 `code_reward`，确认它最终调到你的 `LocalProvider`。可参考 `tests/slow/test_code_reward.py` 里 `test_python_code_reward_morph`（[第 127–139 行](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/slow/test_code_reward.py#L127-L139)）的构造方式，把 `provider_type` 换成 `"local"`。

**需要观察的现象**：

- 步骤 1 打印 `stdout= ''`，确认末行返回值丢失。
- 步骤 2 的 `LocalProvider` 对一个「能通过全部测试」的样本返回 `1.0`，对一个语法错误样本返回 `0.0`，对一个超时样本（把 `timeout` 调到 1 秒、脚本里 `while True`）也返回 `0.0`。
- 步骤 3 后，`get_provider("local")` 返回 `LocalProvider` 实例而不是抛 `ValueError`。

**预期结果**：`LocalProvider` 能正确回收 `[0.0, 1.0]` 这类奖励，且 `get_provider("local")` 不再报错。**待本地验证**：端到端走 `code_reward` 需要构造合法 `verification_info`，可先用步骤 1–3 的单元片段验证骨架正确性。

**进阶思考**：把 `execute_scripts` 从串行改成 4.2 那样的 `asyncio` 并发版（用 `asyncio.to_thread` 包装 `subprocess.run`），对比大批量脚本下的耗时下降。

## 6. 本讲小结

- `CodeExecutionProvider` 是只有一个抽象方法 `execute_scripts(scripts, languages) -> List[float]` 的契约；`get_provider` 工厂按 `provider_type` 字符串分发到 `E2BProvider` / `MorphProvider`，让奖励函数与沙箱后端彻底解耦——YAML 里改一个字段就能换沙箱。
- 两个 Provider 都是「同步入口 + `asyncio.run` 桥接异步 + `Semaphore` 限流 + `gather` 并发」的同构骨架，且对任何异常都兜底成 `0.0`，保证奖励函数永不抛错、不拖垮 GRPO 训练。
- `E2BProvider` 的精华是三层超时（`REQUEST=28` / `SANDBOX=30` / `ASYNCIO=32`）——因为 E2B 自带超时「看起来不生效」，靠 `asyncio.wait_for` 硬兜底，数字由 256 条 gold solution 实测标定；末行表达式靠沙箱的 Notebook 式捕获回收成 `float`。
- `MorphProvider` 的精华是 `asyncio.to_thread`——MorphCloud SDK 是同步阻塞的，必须扔进线程池才能并发；此外它的结果回收要做 `text末行 → text整段 → stdout` 多级 fallback，销毁也更重（`close` + `shutdown`）。
- `configs.py` 把 `"local"` 列为合法 `code_provider`，但 `get_provider` 并未实现——本讲用 `LocalProvider` 综合实践补上了这个缺口。
- 可选依赖的「条件 import + 可用性检测」统一收口在 `import_utils.py`，复用 transformers 的 `_is_package_available`。

## 7. 下一步学习建议

本讲把 Provider 的「直连异步」分支讲透了，但「Router 批量分支」只用到了 `RoutedSandbox.run_code` 这个接口。下一讲 **u5-l3 Router 路由沙箱与限流** 会拆开 `routed_sandbox.py` / `routed_morph.py` 和 `scripts/e2b_router.py`，讲清：

- 为什么多机训练时直接用 E2B 会被限流，router 服务如何用一个共享 IP 池规避；
- `RoutedSandbox.run_code` 的 HTTP `/execute_batch` 批量协议长什么样；
- Provider 在 `router_url` 提供时如何从「自建 N 个沙箱」切换到「POST 给 router」。

如果你更关心竞赛编程，可以跳到第六单元（u6）：`ioi_code_reward` 用的 `ioi_provider`（piston/morph）和本讲的 `code_provider`（e2b/morph）是**两套并行的 Provider 体系**——本讲建立的所有抽象（契约、工厂、异步兜底）在那里会再次出现，只是判分内核从「逐行 stdout 比对」换成了「子任务分批 + 早停」。
