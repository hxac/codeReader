# 编译期与运行期 Fallback 容错

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 TileIR 后端在「出错」这件事上的两层防线：**编译期容错**（autotuner 剪枝 + `warp_specialize` 预防性 workaround）与**运行期 fallback**（`tileir_run` 捕获 `RuntimeError` 后临时切回 PTX 后端）。
- 追踪 [`tileir_run`](#) 的完整 `try/except` 流程，解释 `TRITON_TILEIR_RUNTIME_FALLBACK` 为什么默认关闭、它在回退时如何把 `ENABLE_TILE` 置 0、切换 driver、再用 `finally` 恢复原状。
- 理解 `_tileir_force_warp_specialize_off` 这个临时 workaround 为何是「数学无损」的，以及它规避的是 tileir 13.3.x 的哪条编译失败路径。
- 区分 `HitFallback`、`OutOfResources`、`TileirasError`、`RuntimeError` 四类错误的职责：谁抛出、谁捕获、谁喂给 autotuner 剪枝。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**1）「编译期出错」和「运行期出错」是两件事。**
Triton 的 `@triton.jit` 内核是**惰性编译**的：第一次真正调用（warmup/launch）时才触发编译。所以一次「出错」可能发生在两个时间点：

- 编译期：TTIR → cuda_tile 方言转换失败、或外部 `tileiras` 把 IR 编译成 cubin 失败。这类错误通常是**配置相关的**（某个 autotune 配置占资源太多、或踩到不支持的算子），换一个配置可能就好。
- 运行期：cubin 已经编出来了，但装载/启动阶段抛 `RuntimeError`（例如装载失败、或运行链路异常）。

本讲的核心就是：**对这两类错误，TileIR 后端给了不同的兜底策略。**

**2）TileIR 后端不是默认后端，它是「叠加」上去的。**
默认后端是 NVIDIA PTX 后端（上游 Triton）。`ENABLE_TILE=1` 才切到 TileIR 后端。所以「回退」天然有一个明确的退路——**退回 PTX 后端**。这正是 README ChangeList 第 2 条的设计意图：

> When a compilation bug occurs with the CUDA Tile IR Backend, it falls back to the NVIDIA PTX backend. Main changes include `jit.py` and `nvidia/backend/driver.py`。

**3）TileIR 用「无序内存模型」，回退必须谨慎。**
TileIR 当前只支持无序内存模型（unordered memory model）：全局访存默认不保证顺序，存在内存别名或跨 tile 块数据流动时**可能算错（静默错误）**，而不只是崩溃。这意味着一个 `RuntimeError` 可能只是冰山一角——如果默认就静默回退，会**掩盖真正的正确性问题**。这是理解「为什么运行期 fallback 默认关闭」的关键。

> 关键术语速查：
> - **PTX 后端 / NVIDIA 后端**：上游 Triton 默认后端，`GlobalNvidiaDriver`，产物是 PTX→cubin。
> - **TileIR 后端**：本仓库新增后端，`GlobalTileIRDriver`，产物是 cuda_tile bytecode→cubin。
> - **fallback（回退）**：TileIR 出错时退回 PTX 后端继续完成任务。
> - **autotuner（自动调优器）**：对一组 config 逐个编译+测速，挑最快的；可对「编译失败」的 config 剪枝。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/triton/runtime/jit.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/jit.py) | 运行期 fallback 的主战场：`tileir_run` 的 `try/except`、`warp_specialize` workaround、`__getitem__` 路由。 |
| [third_party/tileir/backend/errors.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/errors.py) | 定义 `HitFallback` 错误类型。 |
| [python/triton/runtime/errors.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/errors.py) | 定义 `OutOfResources`、`TileirasError` 等编译期错误类型。 |
| [third_party/tileir/backend/compiler.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py) | `call_tileiras` 抛出 `OutOfResources` / `TileirasError`；`make_tileir` 抛 `RuntimeError`；导入 `HitFallback`。 |
| [python/triton/compiler/compiler.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py) | `CompiledKernel` 装载阶段对 shared memory 超限抛 `OutOfResources`。 |
| [python/triton/runtime/autotuner.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/autotuner.py) | autotuner 捕获编译期错误并把该 config 计为 `inf`（剪枝）。 |
| [python/triton/runtime/driver.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py) | `driver.set_active()` / `active` 属性，是切换后端的关键开关。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 运行期 fallback**（最核心）、**4.2 warp_specialize workaround**（编译期预防）、**4.3 错误类型与分类**（贯穿编译期/运行期）。在进入 4.1 前，先用一张表把两条防线讲清楚。

| 维度 | 编译期容错 | 运行期 fallback |
| --- | --- | --- |
| 触发时机 | 编译/装载时（`make_tileir`、`tileiras`、`load_binary`） | 运行时 `RuntimeError` |
| 主力机制 | autotuner 剪掉失败 config + `warp_specialize` 预防性关掉 | `tileir_run` 切回 PTX 后端 |
| 是否默认开启 | 是（autotuner 天然如此） | **否**（需 `TRITON_TILEIR_RUNTIME_FALLBACK=1`） |
| 代价 | 丢掉个别 config | 整个内核退回 PTX，丢失 TileIR 性能、且可能掩盖正确性问题 |

---

### 4.1 运行期 fallback：tileir_run 的 try/except

#### 4.1.1 概念说明

「运行期 fallback」指：当一个已经走过编译流程、准备启动的 TileIR 内核在**运行时**抛出 `RuntimeError` 时，系统**临时**把后端切回 NVIDIA PTX，用 PTX 后端把同一个内核重新编译并跑一遍，跑完再把环境变量和 driver 恢复成 TileIR。

它和「编译期容错」的本质区别在于：

- 编译期出错（资源超限等）是**可预期的、配置相关的**，autotuner 自然会换 config 重试，无需切后端。
- 运行期 `RuntimeError` 意味着 TileIR 这条链路本身**整条出了问题**，单个 config 换不掉，只能整条退回 PTX。

正因为它代价大、且可能掩盖正确性问题，这个机制**默认关闭**，必须用环境变量显式开启。

#### 4.1.2 核心流程

`tileir_run` 的执行流程（伪代码）：

```
tileir_run(args, kwargs, grid, warmup):
    1. 先跑 warp_specialize workaround（见 4.2），把 warp_specialize 强制设 False
    2. try:
           driver.set_active(GlobalTileIRDriver)        # 显式锁定 TileIR driver
           ret = self.run(grid, warmup=False, ...)       # 走 TileIR 编译+启动
    3. except RuntimeError:
           if 环境变量 TRITON_TILEIR_RUNTIME_FALLBACK != "1":   # 默认 "0"
               raise                                            # 不开回退 → 直接上抛，暴露问题
           # ---- 进入回退 ----
           os.environ["ENABLE_TILE"] = "0"                     # 临时关闭 TileIR
           driver.set_active(GlobalNvidiaDriver)               # 切到 PTX driver
           try:
               ret = self.run(grid, warmup=False, ...)         # 用 PTX 重新编译+启动
           finally:                                            # 无论成功失败都恢复
               os.environ["ENABLE_TILE"] = "1"
               driver.set_active(GlobalTileIRDriver)
    4. return ret
```

这里有一个关键细节：**为什么既改环境变量、又调 `set_active`？** 因为 `driver.active` 是带缓存的，而 `ENABLE_TILE` 在多处被重新读取：

- `driver.set_active(X)` 会**直接覆盖**缓存的当前 driver（`self._active = driver`），立即生效，下一次 `driver.active` 就返回新的 driver。这一步让「切换」当下就发生。
- 但 `self.run(...)` 内部的编译/启动链路里还有不少地方**重新读 `os.environ["ENABLE_TILE"]`** 来做分支决策（例如 `__getitem__` 的路由、`compiler.py` 里 target 改写与 `CompiledKernel` 装载分支）。所以必须同时把环境变量置 0，才能保证回退期间**整条链路一致地走 PTX**。

#### 4.1.3 源码精读

运行期 fallback 的全部逻辑就在 `KernelInterface.tileir_run`：

[python/triton/runtime/jit.py:L407-L425](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/jit.py#L407-L425) —— `tileir_run` 主体：先跑 workaround，再 `try` 走 TileIR，`except RuntimeError` 时按 `TRITON_TILEIR_RUNTIME_FALLBACK` 决定是否回退到 PTX。

关键片段（仅保留主干）：

```python
def tileir_run(self, *args, grid, warmup, **kwargs):
    args, kwargs = _tileir_force_warp_specialize_off(...)   # 见 4.2
    try:
        driver.set_active(GlobalTileIRDriver)
        ret = self.run(grid=grid, warmup=False, *args, **kwargs)
    except RuntimeError:
        tileir_runtime_fallback = os.environ.get("TRITON_TILEIR_RUNTIME_FALLBACK", "0") == "1"
        if not tileir_runtime_fallback:
            raise                                          # 默认：不回退，暴露错误
        os.environ["ENABLE_TILE"] = "0"
        driver.set_active(GlobalNvidiaDriver)
        try:
            ret = self.run(grid=grid, warmup=False, *args, **kwargs)
        finally:
            os.environ["ENABLE_TILE"] = "1"
            driver.set_active(GlobalTileIRDriver)
    return ret
```

注释里写得很明确：「Fallback TileIR -> native driver on RuntimeError; **off unless** `TRITON_TILEIR_RUNTIME_FALLBACK=1`.」

那么谁决定调用 `tileir_run` 而不是普通的 `run`？是 `__getitem__`（即 `fn[grid](...)` 里的取下标操作）做的路由：

[python/triton/runtime/jit.py:L433-L441](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/jit.py#L433-L441) —— `__getitem__` 依据 `ENABLE_TILE`（环境变量或类属性 `enable_tile`）决定返回 `tileir_run` 还是普通 `run` 的闭包。注意它**实时读环境变量**，这正是回退期间置 0 能生效的原因之一。

```python
if os.environ.get("ENABLE_TILE", "0") == "1" or self.enable_tile:
    return lambda *args, **kwargs: self.tileir_run(grid=grid, warmup=False, *args, **kwargs)
return lambda *args, **kwargs: self.run(grid=grid, warmup=False, *args, **kwargs)
```

`driver.set_active` 与 `active` 属性的实现在 driver 单例上，理解它才能理解「切换」为什么立即生效：

[python/triton/runtime/driver.py:L43-L49](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py#L43-L49) —— `active` 是惰性缓存属性（首次访问才用 `default` 即 `_create_driver()` 初始化）；`set_active` 直接覆盖缓存。所以回退时 `set_active(GlobalNvidiaDriver)` 立即把当前 driver 换成 PTX，无需重新触发探测。

```python
@property
def active(self) -> DriverBase:
    if self._active is None:
        self._active = self.default
    return self._active

def set_active(self, driver: DriverBase) -> None:
    self._active = driver
```

#### 4.1.4 代码实践

**实践目标**：通过源码阅读追踪 `tileir_run` 的回退分支，搞清「默认为何不回退」与「回退如何临时改写全局状态再恢复」。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [python/triton/runtime/jit.py:L407-L425](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/jit.py#L407-L425)，定位 `tileir_run`。
2. 回答以下问题（在 `try` 块里逐行标注）：
   - `os.environ.get("TRITON_TILEIR_RUNTIME_FALLBACK", "0")` 的默认值是什么？这意味着默认走哪条分支？
   - 进入回退后，被修改的全局状态有哪**三处**？（提示：两个环境变量赋值 + 两次 `driver.set_active`）
   - `finally` 块为什么要恢复 `ENABLE_TILE=1` 和 `GlobalTileIRDriver`？如果不恢复，后续内核会怎样？
3. 打开 [python/triton/runtime/driver.py:L43-L49](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py#L43-L49)，确认 `set_active` 是否绕过了 `_create_driver()` 里的 `ENABLE_TILE` 检测。

**需要观察的现象**：回退是「临时切走又切回」的对称操作，状态修改全部成对出现（置 0 ↔ 恢复 1，切 Nvidia ↔ 切回 TileIR）。

**预期结果**：

- 默认 `TRITON_TILEIR_RUNTIME_FALLBACK=0`，`RuntimeError` 会直接 `raise` 上抛——**这是有意为之**，目的是让 TileIR 的运行期错误暴露出来，而不是被静默吞掉。
- 回退路径改写的三处全局状态成对可逆，`finally` 保证即使 PTX 重试也成功，也会把环境恢复成 TileIR，避免「一个内核回退后整个进程都变成 PTX」的副作用。

> 待本地验证：在真实 Blackwell + CUDA 13.1 环境下，构造一个会让 TileIR 在运行期抛 `RuntimeError` 的内核，分别在 `TRITON_TILEIR_RUNTIME_FALLBACK` 为 0 和 1 时观察：前者是否报错上抛、后者是否静默用 PTX 跑通。

#### 4.1.5 小练习与答案

**练习 1**：假设把 `finally` 块删掉，只保留 `os.environ["ENABLE_TILE"] = "0"` 和 PTX 重试，会有什么后果？

**参考答案**：回退成功后，进程的 `ENABLE_TILE` 永远停在 "0"、当前 driver 永远是 `GlobalNvidiaDriver`，**之后所有内核都会走 PTX 后端**，TileIR 被意外全局关闭。`finally` 的作用就是把回退严格限定在「这一次调用」内。

**练习 2**：`tileir_run` 只捕获 `RuntimeError`，不捕获 `OutOfResources`/`TileirasError`，为什么？

**参考答案**：`OutOfResources`/`TileirasError` 是**编译期**错误，它们在 autotuner 场景下应该触发 config 剪枝（见 4.3），而不是整条退回 PTX。运行期 fallback 只针对「编译已过、运行时才炸」的 `RuntimeError`，两者职责不同，故捕获类型不同。

---

### 4.2 warp_specialize workaround：把编译失败提前规避掉

#### 4.2.1 概念说明

这是一个**编译期预防性 workaround**：在调用真正编译之前，先把用户传入的 `warp_specialize=True` 改成 `False`，从而**主动绕开一条已知的 tileir 编译失败路径**。

背景（来自源码注释）：tileir 13.3.x 在 SM100（Blackwell）、HEAD_DIM=128 的场景下，编译 `warp_specialize=True` 的内核会失败。而 TileIR 后端**会自动做 warp specialization**，所以用户显式传的 `warp_specialize` 标志对它而言**本来就是 no-op（空操作）**。因此把它强制设为 `False` 是**数学无损**的——不改变计算结果，只是避开一条会崩的编译路径。

这是一个临时措施，注释明确写着「REMOVE at tileir 13.4」。

#### 4.2.2 核心流程

```
_tileir_force_warp_specialize_off(arg_names, args, kwargs):
    1. 定义 _is_on(v)：兼容裸 bool 与 tl.constexpr，统一判断「是否为真」
    2. 优先看 kwargs 里的 warp_specialize：若为真 → 改 False，标记 forced
    3. 否则看位置参数 args（用 arg_names 定位 warp_specialize 的下标）：若为真 → 替换为 False
    4. 若发生过强制（forced）且尚未警告过 → 打一条 logging.warning（只警告一次）
    5. 返回 (args, kwargs)
```

注意它是**无条件的**——只要走 `tileir_run` 就一定会调用它（见 4.1.3 的第一行），不依赖任何环境变量开关。

#### 4.2.3 源码精读

[python/triton/runtime/jit.py:L369-L400](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/jit.py#L369-L400) —— workaround 的注释（说明原因与无损性）与 `_tileir_force_warp_specialize_off` 的实现。

注释（解释为何无损、以及何时移除）：

```python
# WORKAROUND (tileir 13.3.x): the tileir compiler can fail to compile warp_specialize=True
# kernels on SM100 with HEAD_DIM=128. The tileir backend applies warp specialization
# automatically, so this user-facing flag is effectively a no-op for it; forcing it to False is
# mathematically lossless and avoids the affected compilation path. This is a temporary
# workaround — REMOVE this helper and its call in tileir_run once the fix ships in tileir 13.4.
```

实现要点（已精简）：

```python
_TILEIR_WS_FORCED_OFF_WARNED = False   # 模块级标志，保证只警告一次

def _tileir_force_warp_specialize_off(arg_names, args, kwargs):
    def _is_on(v):
        return bool(getattr(v, "value", v))   # 兼容 bool 和 tl.constexpr

    forced = False
    if "warp_specialize" in kwargs:
        if _is_on(kwargs["warp_specialize"]):
            kwargs = {**kwargs, "warp_specialize": False}; forced = True
    elif arg_names is not None and "warp_specialize" in arg_names:
        i = arg_names.index("warp_specialize")
        if i < len(args) and _is_on(args[i]):
            args = args[:i] + (False, ) + args[i + 1:]; forced = True
    if forced and not _TILEIR_WS_FORCED_OFF_WARNED:
        _TILEIR_WS_FORCED_OFF_WARNED = True
        logging.warning("[tileir WORKAROUND] forcing warp_specialize=False ...")
    return args, kwargs
```

两个细节值得注意：

- `_is_on` 用 `getattr(v, "value", v)` 同时兼容**裸 bool** 和 **`tl.constexpr`**（后者把真值包在 `.value` 里）。`warp_specialize` 作为 `constexpr` 参数，可能是任一形态。
- 分两种入口处理：关键字参数（`kwargs`）和位置参数（`args`，用 `arg_names` 定位下标）。位置参数的改写用切片重组 tuple，保证不可变性。

它在 `tileir_run` 的第一行被无条件调用：

[python/triton/runtime/jit.py:L407-L409](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/jit.py#L407-L409) —— `tileir_run` 一进来就先消化掉 `warp_specialize`，把可能的编译失败**消灭在编译之前**。

#### 4.2.4 代码实践

**实践目标**：理解 workaround「无损」的论证，并验证它的「只警告一次」行为。

**操作步骤**（源码阅读型）：

1. 读 [python/triton/runtime/jit.py:L369-L373](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/jit.py#L369-L373) 的注释，用自己的话写出：为什么把 `warp_specialize` 从 `True` 改成 `False` 不会改变计算结果？（提示词：auto-applies、no-op）
2. 找到模块级变量 `_TILEIR_WS_FORCED_OFF_WARNED`（L374），解释它配合 `if forced and not ...` 如何保证「整个进程只打一次 warning」。
3. 思考：如果用户既没用关键字、参数列表里也没有 `warp_specialize`，这个函数会怎样？（答案：`forced` 保持 `False`，原样返回，不报警告。）

**预期结果**：workaround 只在「用户确实开了 `warp_specialize=True`」时才触发改写与告警；否则是零成本透传。

#### 4.2.5 小练习与答案

**练习 1**：为什么这个 workaround 放在「运行期」的 `tileir_run` 里，而不是放在编译流水线 `make_tileir` 里？

**参考答案**：`warp_specialize` 是用户在调用内核时传入的 `constexpr` 参数，它在**进入编译**之前就存在于调用参数里。在 `tileir_run` 入口处拦截，能在调用编译之前就把这个值改掉，从源头规避失败路径；而 `make_tileir` 是更下游的 IR 处理阶段，此时参数语义早已烘焙进 IR，再处理更复杂。放在最前端拦截最简单、最彻底。

**练习 2**：注释说「REMOVE at tileir 13.4」。如果某天升级到 13.4 后忘了删，最坏后果是什么？

**参考答案**：功能上仍正确（因为该标志对 TileIR 本就是 no-op），只是白白把用户的 `True` 改成 `False` 并打一条过时警告——属于「冗余但无害」。所以这是一个可以安全滞留、但应定期清理的技术债。

---

### 4.3 错误类型：HitFallback / OutOfResources / TileirasError / RuntimeError

#### 4.3.1 概念说明

要把容错讲透，必须分清四类错误的**职责边界**——谁抛、谁接、接了之后干什么。这正是「编译期容错」与「运行期 fallback」的分水岭。

| 错误类型 | 定义位置 | 抛出场景 | 谁捕获/消费 | 后果 |
| --- | --- | --- | --- | --- |
| `OutOfResources` | `runtime/errors.py` | `tileiras` 报共享内存/TMEM 超限；`load_binary` 装载时 shared 超限 | autotuner | config 计 `inf`，被剪枝 |
| `TileirasError` | `runtime/errors.py` | `tileiras` 其他失败（含被信号杀死的崩溃） | autotuner | config 计 `inf`，被剪枝 |
| `HitFallback` | `tileir/backend/errors.py` | TileIR 后端预留的「命中回退」信号 | （已定义并在 compiler.py 导入，作为信号基础设施） | 表达「该内核需要 X 能力，命中回退」 |
| `RuntimeError` | Python 内建 | `make_tileir` 合法性校验失败；运行期异常 | `tileir_run` | 默认上抛；开启 `TRITON_TILEIR_RUNTIME_FALLBACK` 时回退 PTX |

#### 4.3.2 核心流程

编译期错误的流转（以 autotuner 为终点）：

```
tileiras 子进程失败
   ├── stderr 含 "uses too much shared data"  → 解析十六进制 → raise OutOfResources("shared memory")
   ├── stderr 含 "allocated tmem out of resource" → 解析十进制 → raise OutOfResources("tensor memory")
   └── 其他（含 returncode<0 即被信号杀死）    → logging.warning + raise TileirasError

autotunner.do_bench:
   except (OutOfResources, CompileTimeAssertionFailure, PTXASError, TileirasError):
       return [inf, inf, inf]      # 该 config 永远不会胜出 → 等价剪枝
```

运行期错误的流转（以 `tileir_run` 为终点）：

```
self.run(...) 抛 RuntimeError
   └── tileir_run.except RuntimeError:
          TRITON_TILEIR_RUNTIME_FALLBACK=0（默认）→ raise
          TRITON_TILEIR_RUNTIME_FALLBACK=1         → 切 PTX 重试
```

#### 4.3.3 源码精读

**`OutOfResources` 与 `TileirasError`（编译期错误）**

[python/triton/runtime/errors.py:L14-L26](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/errors.py#L14-L26) —— `OutOfResources`：携带 `required`/`limit`/`name` 三元组，`__str__` 提示「减小 block size 或 num_stages」。注意它实现了 `__reduce__`，是为了让异常在异步编译（pickle）时可序列化。

[python/triton/runtime/errors.py:L39-L46](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/errors.py#L39-L46) —— `TileirasError`：对标上游的 `PTXASError`，表示外部 `tileiras` 编译器失败。

它们在 `call_tileiras` 里被精确分类抛出：

[third_party/tileir/backend/compiler.py:L236-L272](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L236-L272) —— 解析 `tileiras` 的 stderr：共享内存用十六进制正则 `0x([0-9a-fA-F]+)`，TMEM（tensor memory）用十进制正则 `([0-9]+)\s*vs\s*([0-9]+)`；其余失败（含被信号杀死、`returncode` 为负）先 `logging.warning` 再抛 `TileirasError`。注释明确：「a compiler crash, not a user error … Surface it as TileirasError so the autotuner can prune the offending config (mirrors PTXASError), but always log it so the underlying tileiras failure stays visible and is never silently swallowed.」

```python
if "uses too much shared data" in log:
    match = re.search(r"0x([0-9a-fA-F]+) bytes, 0x([0-9a-fA-F]+) max", log)
    if match:
        raise OutOfResources(int(match.group(1),16), int(match.group(2),16), "shared memory")
if "allocated tmem out of resource" in log:
    match = re.search(r"allocated tmem out of resource:\s*([0-9]+)\s*vs\s*([0-9]+)", log)
    if match:
        raise OutOfResources(int(match.group(1)), int(match.group(2)), "tensor memory")
...
raise TileirasError(f"{error}\n`tileiras` stderr:\n{log}\nRepro command: {repro}")
```

装载阶段（`load_binary` 之后）也会做资源校验，这是另一处编译期/装载期 `OutOfResources`：

[python/triton/compiler/compiler.py:L489-L510](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L489-L510) —— `ENABLE_TILE=1` 分支下，装载 cubin 后若 `metadata.shared > max_shared`，同样抛 `OutOfResources`，供 autotuner 剪枝。

**autotuner 如何消费这些错误（剪枝）**

[python/triton/runtime/autotuner.py:L165-L170](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/autotuner.py#L165-L170) —— autotuner 在 `do_bench` 里捕获 `OutOfResources`/`PTXASError`/`TileirasError` 等，返回 `[inf, inf, inf]`。`inf` 意味着该 config 永远不会是最快的，等价于**自动剪枝**——这正是「编译期容错」的核心：失败的 config 不影响其它 config 继续测速。

```python
try:
    return self.do_bench(kernel_call, quantiles=(0.5, 0.2, 0.8))
except (OutOfResources, CompileTimeAssertionFailure, PTXASError, TileirasError) as e:
    if verbose:
        print(f"Autotuning failed with {e}")
    return [float("inf"), float("inf"), float("inf")]
```

**`HitFallback`（TileIR 后端预留的回退信号错误）**

[third_party/tileir/backend/errors.py:L4-L14](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/errors.py#L4-L14) —— `HitFallback` 继承 `TritonError`，携带 `required` 与 `name`，`__str__` 形如 `HitFallback: <name>, Required: <required>.`。它的结构与 `OutOfResources`（`required/limit/name`）同源——都是「描述一个不满足条件的结构化错误」。

```python
class HitFallback(TritonError):
    def __init__(self, required, name):
        self.required = required
        self.name = name
    def __str__(self) -> str:
        return f"HitFallback: {self.name}, Required: {self.required}."
```

[third_party/tileir/backend/compiler.py:L1-L2](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L1-L2) —— 该错误类型被导入到 TileIR 后端编译器模块（`from triton.backends.tileir.errors import HitFallback`），作为「表达命中回退条件」的信号基础设施存在。它提供了一种与 `OutOfResources` 同风格的、可被上层（autotuner/fallback 决策层）识别的结构化信号。

> 说明：在当前 HEAD，`HitFallback` 已定义并在编译器模块导入，作为 TileIR 后端错误体系的一部分。它的 `required`/`name` 字段设计意图是表达「该内核需要某项能力（`required`）但命中回退」，与 `OutOfResources` 同属「结构化、可决策」的错误族。

**`RuntimeError`（运行期 fallback 的触发器）**

[third_party/tileir/backend/compiler.py:L321-L324](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L321-L324) —— `make_tileir` 末尾的合法性校验：若 `only_contain_legal_dialects` 发现残留非法 op，抛 `RuntimeError`。注意这是**编译期**抛出的 `RuntimeError`，它会被 `tileir_run` 的 `except RuntimeError` 捕获——也就是说，这类「转换不彻底」的失败也可能触发运行期 fallback 分支（在开关开启时退回 PTX）。

```python
if not tileir.only_contain_legal_dialects(mod):
    raise RuntimeError(
        "Triton ttir to tileir ir failed. Some ttir ops cannot be converted to tileir.")
```

#### 4.3.4 代码实践

**实践目标**：建立「错误类型 → 消费者 → 后果」的完整映射，区分编译期剪枝与运行期回退。

**操作步骤**（源码阅读型）：

1. 打开 [third_party/tileir/backend/compiler.py:L244-L272](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L244-L272)，写出 `tileiras` stderr 到三类错误的两条判别正则（共享内存 vs TMEM），并说明为何第三类用 `TileirasError` 而非 `OutOfResources`。
2. 打开 [python/triton/runtime/autotuner.py:L165-L170](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/autotuner.py#L165-L170)，确认 `TileirasError` 与 `OutOfResources` 都在捕获列表里，解释「返回 `inf`」为何等价于剪枝。
3. 对比 `HitFallback`（[errors.py:L4-L14](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/errors.py#L4-L14)）与 `OutOfResources`（[runtime/errors.py:L14-L26](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/errors.py#L14-L26)）的字段结构，说明它们的共同设计模式。

**预期结果**：

- 共享内存用十六进制正则（`tileiras` 以 `0x...` 报告），TMEM 用十进制正则（`<used> vs <max>`）。
- 第三类失败（含崩溃）无法归因到具体资源，故用笼统的 `TileirasError`，但仍保证可见（先 warning 再 raise）。
- `HitFallback` 与 `OutOfResources` 都采用「携带语义字段 + 实现 `__reduce__` 可序列化」的结构化错误模式。

#### 4.3.5 小练习与答案

**练习 1**：`tileiras` 被 SIGSEGV 杀死（`returncode = -11`）时，会抛哪个错误？autotuner 会怎样？

**参考答案**：stderr 不含资源超限关键字，落入第三类——先 `logging.warning("tileiras failed (code -11)...")`，再 `raise TileirasError(...)`。autotuner 捕获 `TileirasError`，返回 `[inf, inf, inf]`，该 config 被剪掉，其它 config 继续测速。

**练习 2**：`HitFallback` 的 `__reduce__` 返回 `(type(self), (self.required, self.name))`，为什么要实现它？

**参考答案**：Triton 支持异步编译（`_async_compile`），异常对象会在进程间传递（pickle）。`__reduce__` 指明反序列化时如何用 `required`/`name` 重建实例，保证异常在跨进程传播后仍是 `HitFallback` 而非丢失类型。这与 `OutOfResources` 的 `__reduce__` 同理（注释明说「necessary to make CompilationError picklable」）。

**练习 3**：一个内核在 `make_tileir` 因残留非法 op 抛了 `RuntimeError`，此时若 `TRITON_TILEIR_RUNTIME_FALLBACK=1`，会发生什么？

**参考答案**：这个 `RuntimeError` 会沿调用栈冒泡到 `tileir_run` 的 `except RuntimeError`，由于开关已开启，系统会把 `ENABLE_TILE` 置 0、切到 `GlobalNvidiaDriver`，用 PTX 后端重新编译并运行该内核，成功后 `finally` 恢复 TileIR 状态。这正是「转换不彻底」也能受益于运行期 fallback 的情形。

---

## 5. 综合实践

**任务**：画出 TileIR 后端「从出错到兜底」的完整决策树，把本讲三个模块串起来。

请按以下步骤完成（纯源码阅读 + 画图，不修改源码）：

1. **入口**：从 [python/triton/runtime/jit.py:L433-L441](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/jit.py#L433-L441) 的 `__getitem__` 出发，标注「`ENABLE_TILE=1` → `tileir_run`」这条路由。
2. **预防**：在 `tileir_run` 内标出第一道关卡 `_tileir_force_warp_specialize_off`（[L409](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/jit.py#L409)），注明它规避的是「tileir 13.3.x + SM100 + HEAD_DIM=128」编译失败。
3. **编译期分支**：画出 `self.run` 内部编译链路上的两类错误出口——`OutOfResources` / `TileirasError`（来自 [compiler.py:L244-L272](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L244-L272)），箭头指向 autotuner 的 `[inf, inf, inf]` 剪枝（[autotuner.py:L167](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/autotuner.py#L167)）。
4. **运行期分支**：画出 `except RuntimeError` 的两个出口——默认 `raise`；开启 `TRITON_TILEIR_RUNTIME_FALLBACK=1` 时「置 `ENABLE_TILE=0` → `set_active(GlobalNvidiaDriver)` → PTX 重试 → `finally` 恢复」（[jit.py:L413-L424](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/jit.py#L413-L424)）。
5. **错误族**：在图侧注明 `HitFallback`（[errors.py:L4-L14](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/errors.py#L4-L14)）作为 TileIR 后端预留的结构化回退信号，与 `OutOfResources` 同族。

**验收标准**：图上能清晰看出「编译期错误 → autotuner 剪枝（默认启用、不切后端）」与「运行期 RuntimeError → 切 PTX（默认关闭、显式开启）」是两条互不混淆的路径，且 `warp_specialize` workaround 是编译之前的预防手段。

## 6. 本讲小结

- TileIR 后端有**两层容错**：编译期由 autotuner 对 `OutOfResources`/`TileirasError` 剪枝（默认启用、不切后端），运行期由 `tileir_run` 的 `except RuntimeError` 切回 PTX（默认**关闭**，需 `TRITON_TILEIR_RUNTIME_FALLBACK=1`）。
- 运行期 fallback 默认关闭是有意为之：TileIR 采用无序内存模型，`RuntimeError` 可能是正确性问题的征兆，静默回退会**掩盖问题**并丢失 TileIR 性能。
- 回退是「临时切走又切回」的对称操作：置 `ENABLE_TILE=0` + `set_active(GlobalNvidiaDriver)` 重试，`finally` 里恢复 `ENABLE_TILE=1` + `set_active(GlobalTileIRDriver)`，把回退严格限定在单次调用。
- `set_active` 直接覆盖 `driver.active` 缓存使切换立即生效，同时改环境变量是因为编译/启动链路多处实时重读 `ENABLE_TILE`。
- `_tileir_force_warp_specialize_off` 是编译前的**预防性 workaround**：因 TileIR 本就自动做 warp specialization，强制关掉用户标志是**数学无损**的，用于绕开 tileir 13.3.x 的 SM100/HEAD_DIM=128 编译失败（临时措施，待 13.4 移除）。
- 四类错误职责分明：`OutOfResources`/`TileirasError`（编译期，喂 autotuner 剪枝）、`HitFallback`（TileIR 预留的结构化回退信号）、`RuntimeError`（运行期 fallback 的触发器，也含 `make_tileir` 合法性校验失败）。

## 7. 下一步学习建议

- **回看编译流水线**：本讲的编译期错误都源自 `make_tileir` / `make_cubin`，建议复习 u2-l3（三段式编译流水线）与 u2-l7（tileiras 调用），把「错误在哪个 stage 抛出」与「stage 的职责」对上。
- **深入错误源头**：想理解 `OutOfResources` 的 shared/TMEM 判别正则为何一十六进制、一十进制，可阅读 u2-l7 关于 `tileiras` stderr 解析的部分。
- **autotuner 协同**：若想看剪枝的全貌（`prune_configs`、`early_config_prune`），可阅读 `python/triton/runtime/autotuner.py` 的 `Autotuner._bench` 与 `prune_configs`，理解「错误→inf→剪枝」如何与「性能测速」耦合。
- **后续讲义**：u4-l2（性能调优）会讲解如何在 autotune 里正确设置 `occupancy`/`num_ctas` 等旋钮，避免大规模触发本讲所述的编译期错误；u4-l4（构建系统）会讲解 `tileiras` 工具链如何随 cuda-tile 一起被管理。
