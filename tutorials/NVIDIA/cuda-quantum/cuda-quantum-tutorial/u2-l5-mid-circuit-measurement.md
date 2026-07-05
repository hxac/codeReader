# 线路中途测量与条件执行

## 1. 本讲目标

本讲聚焦 CUDA-Q 中一类「让程序随测量结果而改变后续线路」的能力。读完本讲，你应当能够：

- 说清楚什么叫做**中途测量（mid-circuit measurement）**，以及它和我们在 u2-l3 学到的「末端批量采样」在执行语义上有什么本质区别。
- 写出一个把测量结果当作经典值、用来驱动 `if` 条件分支的内核，并知道这种「条件反馈（conditional feedback）」在不同编译模式（MLIR 模式 / 库模式）下分别由哪些源码兑现。
- 知道不同后端（`qpp` 状态向量、`stim` 稳定器、远程 QPU）对中途测量的支持差异，遇到「use `cudaq::run` instead」这类报错时能立刻定位原因。

本讲承接 u2-l3（`mz/mx/my` 与 `SampleResult`），把「测量」从「线路终点的一次性统计」推进到「线路中途、可被后续逻辑消费的经典比特」。

## 2. 前置知识

在进入源码前，先用三条直觉建立认知：

1. **测量即塌缩**。对量子比特做 Z 基测量 \(mz\)，叠加态 \(\alpha|0\rangle+\beta|1\rangle\) 会以概率 \(P(0)=|\alpha|^2\)、\(P(1)=|\beta|^2\) 塌缩到 \(|0\rangle\) 或 \(|1\rangle\)，并产生一个**经典比特** \(m\in\{0,1\}\)。塌缩是不可逆的，被测比特之后的演化只能从塌缩后的态出发。

2. **经典比特可以参与控制**。既然测量产生的是一个 0/1 经典值，它当然可以作为 `if` 的条件，决定后面要不要施加某个门。这就是「测量—反馈」的雏形，也是量子纠错、隐形传态、动态线路的基础。

3. **CUDA-Q 的测量有两种"兑现时机"**。这正是本讲的关键：同一个 `mz`，在不同执行语境下行为不同。
   - **延迟兑现（deferred）**：在 `cudaq::sample` 的常规路径里，`mz` 只是把「这个比特要被测量」**登记**下来，真正的塌缩推迟到一次批量采样里完成，效率高，但你**拿不到**单次测量值去做 `if`。
   - **即时兑现（eager）**：当测量结果被「消费」（参与 `if`、被 `return`、被算术运算）时，模拟器必须在那一刻真实地塌缩状态、给出一个确定的 0/1，后续门才能依据它分支。

   这两种时机由执行上下文（`ExecutionContext`）和一个标志位 `hasConditionalsOnMeasureResults` 共同决定，下文会逐一对照源码。

> 术语提示：u2-l3 学过，`cudaq::sample` 给出「比特串→次数」的**分布**，`cudaq::run` 给出**逐 shot 的返回值**。本讲会发现：需要中途测量反馈时，`cudaq::run` 是最自然的入口。

## 3. 本讲源码地图

本讲涉及的源码分布在前端桥接、运行时、模拟器三层：

| 文件 | 作用 |
| --- | --- |
| `docs/sphinx/examples/cpp/basics/mid_circuit_measurement.cpp` | 隐形传态示例：中途测量 + 条件修正的完整可运行内核。 |
| `runtime/cudaq/qis/qubit_qis.h` | `mz/mx/my` 的声明；注释说明 MLIR 模式下桥接如何拦截这些调用。 |
| `runtime/cudaq/qis/measure_handle.h` | MLIR 模式下的测量句柄 `measure_handle`：「延迟判别」的抽象。 |
| `runtime/cudaq/qis/execution_manager.h` | 库模式下的 `measure_result` 类型，及其向 `bool` 的转换钩子。 |
| `runtime/cudaq/qis/execution_manager_c_api.cpp` | `__nvqpp__MeasureResultBoolConversion` 的实现：库模式下 `if(b)` 的入口。 |
| `cudaq/lib/Frontend/nvqpp/ConvertStmt.cpp` | AST 桥把 C++ `if` 翻译成 MLIR `cc.if`。 |
| `cudaq/lib/Frontend/nvqpp/ASTBridge.cpp` | 入口内核签名校验：测量句柄不得跨越主机/设备边界。 |
| `cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td` | `quake.discriminate` 操作：把测量值转成经典整数的 IR 原语。 |
| `runtime/nvqir/CircuitSimulator.h` | 模拟器基类：登记式采样 `handleBasicSampling`、单 shot 强制、`flushAnySamplingTasks`。 |
| `runtime/internal/compiler/Compiler.cpp` | 编译期静态检测「条件依赖于测量」并据此放行或拒绝。 |
| `runtime/cudaq/algorithms/sample.h` | 库模式下 `sample` 用 tracer 探测内核是否含条件反馈。 |
| `runtime/cudaq/algorithms/run.h` | `cudaq::run`：逐 shot 执行、返回向量。 |
| `runtime/nvqir/stim/StimCircuitSimulator.cpp` | `stim` 稳定器后端的中途测量实现。 |
| `cudaq/include/cudaq/Target/CompileTarget.h` | 目标能力位 `supportConditionalsOnMeasureResults` 的默认值。 |

## 4. 核心概念与源码讲解

### 4.1 中途测量语义

#### 4.1.1 概念说明

「中途测量」指的是：在线路**执行到一半**时测量某个比特，然后**继续**对这个比特（或别的比特）施加后续门。它的两个标志性特征：

1. 测量发生在时间轴中段，后面还有量子操作；
2. 测量塌缩了被测比特的量子态，后续演化建立在塌缩后的态上。

这和 u2-l3 讲的「末端测量」不同。末端测量时，所有测量都在线路最末尾、彼此可交换，模拟器可以「先算完整个酉演化、再一次性采样」，因此能批量并行、效率极高。而中途测量打断了这种「先演化后采样」的结构——后面的门依赖前面的测量结果，必须**一步一步、一次 shot**地走。

CUDA-Q 用「**延迟判别（deferred discrimination）**」来统一这两种情形。一次 `mz` 调用返回的不是一个立刻可用的 `bool`，而是一个**测量句柄**：

- **MLIR 模式**（默认）：返回 [`measure_handle`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/measure_handle.h#L30-L69)，它在 IR 里表示为 `!cc.measure_handle`，含义是「发生过一次测量事件，尚未转成经典值」。
- **库模式**（`nvq++ -flibrary-only`，旧式直接执行）：返回 [`measure_result`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager.h#L46-L73)，注释里写得很直白：用它「跟踪测量结果何时被隐式转成 `bool`（通常就是条件反馈的场景），并据此影响模拟」。

> 注意：MLIR 模式下 [`using measure_result = measure_handle`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager.h#L74-L78)，两个名字是同一类型，源码里看到的 `measure_result` 在 MLIR 模式就是句柄。

#### 4.1.2 核心流程

一次「含中途测量」的内核执行，可以抽象成下面这条流程：

```
内核开始
  ├── 施加一批门 H, X, CNOT, ...          （酉演化）
  ├── b = mz(q_i)                         （产生测量句柄，尚未判别）
  ├── ... 可能还有不依赖 b 的门 ...
  ├── if (b) { 某个门(q_j) }              （消费 b：此刻必须把句柄判别成 0/1）
  │       └── 触发即时塌缩：measureQubit(q_i) → 真实经典值
  ├── mz(q_k)                             （末端测量，可延迟批量）
  └── 返回
```

关键在于第 4 步「消费 b」：只有当句柄被**判别（discriminate）**成经典整数的那一刻，模拟器才被迫真正塌缩状态、给出确定值。在那之前，测量事件可以被「登记」下来推迟处理。这一点直接解释了为什么 `sample`（不消费单次值）能批量优化，而 `run`（每 shot 都要拿值回来）必须逐 shot 真测。

数学上，对一个中途测量的比特，设其测量前态为 \(|\psi\rangle=\alpha|0\rangle+\beta|1\rangle\)：

\[
P(m=0)=|\alpha|^2,\quad P(m=1)=|\beta|^2
\]

测得 \(m\) 后，比特塌缩为 \(|m\rangle\)；后续条件分支 \(g^{m}\)（\(m=1\) 时施加 \(g\)，否则恒等）的最终状态由 \(m\) 的具体取值决定。要正确仿真，必须**逐 shot** 抽样 \(m\)、再据此走分支，而不能把所有 shot 的演化合并。

#### 4.1.3 源码精读

**`mz` 的双重人格。** 先看 [`qubit_qis.h`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L426-L444) 中 `mz` 的定义和它上方的注释：注释明确说，在 MLIR 模式下，桥接会**拦截** `__qpu__` 内核里每一次 `mz` 调用，直接发出 `quake.mz` 操作，因此内联函数体**根本不会执行**；库模式下才会真的调用 `getExecutionManager()->measure(...)`。

```cpp
// runtime/cudaq/qis/qubit_qis.h
inline measure_result mz(qubit &q) {
#ifdef CUDAQ_LIBRARY_MODE
  return getExecutionManager()->measure(QuditInfo{q.n_levels(), q.id()});
#else
  throw std::runtime_error(detail::kQpuOnlyHostScopeError); // 主机域禁用
#endif
}
```

也就是说：**在主机（host）代码里直接写 `mz(q)` 是不允许的**，`mz` 只能出现在 `__qpu__` 内核里。这是 CUDA-Q「主机/设备分离」的一条硬约束。

**测量句柄的「延迟判别」语义。** [`measure_handle`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/measure_handle.h#L30-L59) 的文档定义了它的核心性质：默认构造的句柄是「未绑定」的；它会在 AST→Quake 转换时落地为 `!cc.measure_handle`，再由 QIR 类型转换降级成一个裸 `i64`。最关键的一行在它的 `operator bool()`：

```cpp
// runtime/cudaq/qis/measure_handle.h
operator bool() const {
  throw std::runtime_error(
      "`cudaq::measure_handle`: implicit `bool` conversion at host scope is "
      "not supported. Discriminate the handle inside a `__qpu__` kernel ...");
}
```

这个函数体**在内核里永远不会运行**——桥接会拦截每一次向 `bool` 的转换、发出 `quake.discriminate`。它之所以写成抛异常，纯粹是为了让「在主机域误用句柄」 loudly fail，而不是返回一个无意义的 `bool`。

**末端采样为什么能批量优化。** 在模拟器基类 [`CircuitSimulator.h`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/CircuitSimulator.h#L636-L679) 里，`handleBasicSampling` 是「延迟兑现」的开关：当执行上下文名字是 `"sample"` 时，`mz` 只把比特号塞进 `sampleQubits` 列表就立刻 `return true` 提前退出，**不调用** `measureQubit`、**不塌缩**：

```cpp
// runtime/nvqir/CircuitSimulator.h
bool handleBasicSampling(const std::size_t qubitIdx, const std::string &regName) {
  auto executionContext = cudaq::getExecutionContext();
  if (executionContext && executionContext->name == "sample") {
    ...
    sampleQubits.push_back(qubitIdx);   // 只登记，不测量
    ...
    return true;                        // 提前返回 = 跳过真实塌缩
  }
  return false;
}
```

而 [`cudaq::run`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/run.h#L110-L153) 创建的上下文名字是 `"run"`（不是 `"sample"`），所以 `handleBasicSampling` 返回 `false`，执行流落到 [测量主路径](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/CircuitSimulator.h#L1459-L1467)，真实调用 `measureQubit` 完成塌缩：

```cpp
// runtime/nvqir/CircuitSimulator.h
// If sampling, just store the bit, do nothing else.
if (handleBasicSampling(qubitIdx, registerName))
  return true;
// Get the actual measurement from the subtype measureQubit implementation
auto measureResult = measureQubit(qubitIdx);
return measureResult;
```

这就是「`run` 天然支持中途测量、`sample` 默认延迟」的根因。

#### 4.1.4 代码实践

**实践目标：** 跑通官方隐形传态示例，确认中途测量能正确传递量子态。

**操作步骤：**

1. 编译并运行 [`mid_circuit_measurement.cpp`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/mid_circuit_measurement.cpp#L1-L48)：

   ```bash
   nvq++ docs/sphinx/examples/cpp/basics/mid_circuit_measurement.cpp -o teleport.x
   ./teleport.x
   ```

2. 阅读内核本体（第 8–31 行）。注意三个动作段：
   - [第 12–19 行](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/mid_circuit_measurement.cpp#L12-L19)：制备待传态 \(|1\rangle\) 与 Bell 对、做 Bell 测量前的纠缠；
   - [第 21–22 行](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/mid_circuit_measurement.cpp#L21-L22)：**中途测量** `q[0]`、`q[1]`；
   - [第 24–27 行](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/mid_circuit_measurement.cpp#L24-L27)：**条件反馈** `if (b1) x(q[2]); if (b0) z(q[2]);`，再末端测量 `q[2]`。

3. 注意主程序用的是 [`cudaq::run`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/mid_circuit_measurement.cpp#L33-L47)，逐 shot 收集返回值并统计 `q[2]==1` 的次数。

**需要观察的现象：** 因为传送的是 \(|1\rangle\)，经过正确的条件修正后，目标比特 `q[2]` 在每一次 shot 里都应当测得 1。

**预期结果：** 输出形如 `Measured '1' on target qubit 100 times out of 100 shots.`，且 [第 47 行的 `assert`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/mid_circuit_measurement.cpp#L47) 不会触发。若你删掉第 24–27 行的条件修正，应观察到 `q[2]` 不再恒为 1（待本地验证：删去修正后统计值会显著偏离 100）。

#### 4.1.5 小练习与答案

**练习 1：** 为什么本示例用 `cudaq::run` 而不是 `cudaq::sample`？二者在「能否拿到单次测量值」上有何区别？

> **参考答案：** `cudaq::run` 逐 shot 执行内核、把每次的返回值（这里是 `q[2]` 的测量结果）收集进 `std::vector`；执行上下文名为 `"run"`，`mz` 会真实塌缩。`cudaq::sample` 走的是延迟批量采样路径（`handleBasicSampling` 提前返回），更适合「只要分布、不要单次值」的场景。本例要逐 shot 检查目标比特是否为 1 并 `assert`，所以用 `run`。

**练习 2：** `measure_handle` 的 `operator bool()` 为什么写成抛异常？

> **参考答案：** 在 MLIR 模式下，`__qpu__` 内核里向 `bool` 的转换会被 AST 桥拦截并发出 `quake.discriminate`，函数体本不该执行；只有当用户在**主机域**误用测量句柄时才会走到这里。抛异常能让这种误用立刻暴露，而不是静默返回一个无意义的 `bool` 导致结果错误。

### 4.2 测量结果驱动的条件分支

#### 4.2.1 概念说明

「条件反馈」= 测量句柄被用作 `if` 的条件，从而改变后续线路。它把一个**量子—经典边界**显式地带进了内核：

- 左侧是量子态（线路、门）；
- 右侧是经典控制流（`if`、`while`）；
- 桥梁就是「把测量句柄判别成一个经典整数」这一步。

CUDA-Q 在两个编译模式下用不同源码兑现同一语义：

| 模式 | `mz` 产生 | `if (b)` 如何工作 |
| --- | --- | --- |
| MLIR 模式 | `!cc.measure_handle` | 桥接发 [`quake.discriminate`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L1179-L1203) 把句柄转 `i1`，再发 `cc.if` |
| 库模式 | `measure_result` | `measure_result::operator bool()` 调 [`__nvqpp__MeasureResultBoolConversion`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager_c_api.cpp#L12-L18) |

无论哪种模式，编译器/运行时都需要**静态判定**「这个内核到底有没有条件依赖测量」，因为这个判定会改变执行策略（见 4.3）。

#### 4.2.2 核心流程

条件反馈从源码到执行的完整链路：

```
C++ 源码: if (b) x(q[2]);
        │
        ├─[MLIR 模式]─ ASTBridge::TraverseIfStmt
        │       ├─ 条件表达式若来自 mz → 值类型是 !cc.measure_handle
        │       ├─ 插入 quake.discriminate  → 得到 i1
        │       └─ 发出 cc.if(i1) { ... }
        │
        ├─[库模式]── measure_result::operator bool()
        │       └─ __nvqpp__MeasureResultBoolConversion(result) → bool
        │          （tracer 模式下顺便记下 registerName，用于探测）
        │
        └─[共同]── 编译器/运行时把内核标记为 hasConditionalsOnMeasureResults
                   → 强制单 shot 执行 → 后端逐 shot 真实塌缩与分支
```

隐形传态的条件修正 `if (b1) x(q[2]); if (b0) z(q[2]);` 正是这条链路的典型用例。数学上，Bell 测量得到经典比特 \((b_0,b_1)\) 后，目标比特处于 \(X^{b_1}Z^{b_0}|\psi\rangle\)，再施加同样的 \(X^{b_1}Z^{b_0}\) 即可还原 \(|\psi\rangle\)：

\[
X^{b_1}Z^{b_0}\cdot X^{b_1}Z^{b_0}|\psi\rangle = |\psi\rangle
\]

所以「测量 → 用结果决定施加哪些修正门」是不可或缺的一步，缺了它传送就会失败。

#### 4.2.3 源码精读

**MLIR 模式：`if` 的翻译。** [`ConvertStmt.cpp` 的 `TraverseIfStmt`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertStmt.cpp#L509-L572) 把 C++ `if` 翻成 `cc::IfOp`，条件值来自栈顶（`popValue()`）。当条件是一个测量句柄时，桥接在更早的表达式翻译阶段已经为其插入了 `quake.discriminate`，从而把 `!cc.measure_handle` 转成 `i1` 喂给 `cc.if`。摘录关键段：

```cpp
// cudaq/lib/Frontend/nvqpp/ConvertStmt.cpp
if (x->getElse())
  cc::IfOp::create(builder, loc, TypeRange{}, popValue(),
                   stmtBuilder(x->getThen()), stmtBuilder(x->getElse()));
else
  cc::IfOp::create(builder, loc, TypeRange{}, popValue(),
                   stmtBuilder(x->getThen()));
```

**`discriminate` 操作的语义。** [`quake.discriminate`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L1179-L1203) 的 TableGen 描述给出了两条重要保证：测量改变被测线路（wire）的状态，但**经典测量值是非易失的**；对同一个标量测量句柄多次 `discriminate` 会得到同样的结果，因此标量形式被视为「纯净（pure）」。这正是 `if (b) ... if (b) ...` 两次用同一个 `b` 不会出现两次随机塌缩的语义依据。

**主机/设备边界校验。** 入口内核（从主机直接调用的内核）不允许把测量句柄作为返回值或参数跨边界。[`ASTBridge.cpp`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L666-L669) 在签名检查里给出明确报错：`measurement handle cannot cross the host-device boundary; entry-point kernels must discriminate first`——也就是「先把句柄判别成经典值再传出来」。

**库模式：`operator bool` 钩子。** [`measure_result::operator bool()`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager.h#L62-L63) 把转换委托给一个 C 链接的钩子，[其实现](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager_c_api.cpp#L12-L18)非常薄：在 tracer 上下文里把寄存器名记下来，否则直接返回 `result == 1`。

```cpp
// runtime/cudaq/qis/execution_manager_c_api.cpp
bool cudaq::__nvqpp__MeasureResultBoolConversion(int result) {
  auto &platform = get_platform();
  auto *ctx = getExecutionContext();
  if (ctx && ctx->name == "tracer")
    ctx->registerNames.push_back("");   // 探测：发现这里被调用 → 含条件反馈
  return result == 1;
}
```

这一行 `tracer` 分支正是库模式下「探测内核是否含条件反馈」的关键：[`sample.h`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L100-L117) 在采样前用 tracer 上下文空跑一遍内核，如果 `registerNames` 非空（说明某次 `operator bool` 被触发，即存在 `if(测量结果)`），就把上下文标记为 `hasConditionalsOnMeasureResults = true`。

**编译期静态检测（MLIR 模式）。** [`Compiler.cpp`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/internal/compiler/Compiler.cpp#L400-L409) 用 `QuakeFunctionAnalysis` 直接在 IR 上判定 `hasConditionalsOnMeasure`，结果写入编译产物的元数据，运行时再由 [`qpu.cpp`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/qpu.cpp#L88-L89) 读回到 `ExecutionContext`。

#### 4.2.4 代码实践

**实践目标：** 写一个最小内核，验证「测量到 1 才翻转辅助比特」的条件反馈能改变输出分布。

**操作步骤（源码阅读 + 修改）：**

1. 新建 `cond_feedback.cpp`（示例代码，非项目原有文件）：

   ```cpp
   #include <cudaq.h>

   struct kernel {
     __qpu__ bool operator()() {
       cudaq::qarray<2> q;
       x(q[0]);            // q0 = |1>
       auto b = mz(q[0]);  // 中途测量：几乎必然得 1
       if (b) x(q[1]);     // 条件反馈：b=1 时翻转 q1
       return mz(q[1]);    // 末端测量 q1
     }
   };

   int main() {
     auto rs = cudaq::run(200, kernel{});
     int ones = 0;
     for (bool r : rs) if (r) ones++;
     printf("q1 == 1 in %d / %zu shots\n", ones, rs.size());
     return 0;
   }
   ```

2. 用 `nvq++ cond_feedback.cpp -o cond_feedback.x && ./cond_feedback.x` 编译运行。

**需要观察的现象：** 因为 `q0` 被制备成 \(|1\rangle\)，`b` 几乎恒为 1，所以条件分支几乎总会翻转 `q1`，`q1` 测得 1 的比例应接近 100%。

**预期结果：** 输出形如 `q1 == 1 in 200 / 200 shots`。把 `if (b) x(q[1]);` 注释掉再运行，应看到 `q1` 几乎全为 0（因为 `q1` 始终是 \(|0\rangle\)）——这一对比能直观体现条件反馈的作用。

> 说明：本示例只为演示条件反馈写法，依赖默认的 `qpp` 后端；切换 `stim` 后端的做法见 4.3.4。

#### 4.2.5 小练习与答案

**练习 1：** MLIR 模式下，`if (b)` 中的 `b` 是 `measure_handle`，编译器是怎样把它变成 `cc.if` 可用的 `i1` 条件的？

> **参考答案：** AST 桥在翻译条件表达式时为测量句柄插入 `quake.discriminate` 操作，把 `!cc.measure_handle` 转成经典 `i1`；`TraverseIfStmt` 再用这个 `i1` 作为条件发出 `cc.if`。`discriminate` 对同一标量句柄是纯净的，多次判别结果一致。

**练习 2：** 库模式下，`sample` 是如何在「不实际采样」的前提下发现内核含条件反馈的？

> **参考答案：** `sample` 先用名字为 `"tracer"` 的上下文空跑内核。一旦执行流走到 `measure_result::operator bool()`，`__nvqpp__MeasureResultBoolConversion` 就往 `ctx->registerNames` 追加一项；空跑结束后若 `registerNames` 非空，就把真正的采样上下文标记为 `hasConditionalsOnMeasureResults = true`。

### 4.3 后端支持差异

#### 4.3.1 概念说明

「能写条件反馈」和「后端能跑条件反馈」是两件事。条件反馈强制要求**逐 shot、按真实塌缩值走分支**，这把后端能力分成了几档：

- **本地状态向量后端（`qpp`/`custatevec`）**：通用，逐 shot 塌缩即可，精确但内存随比特数指数增长。
- **稳定器后端（`stim`）**：只能跑 Clifford 线路（H、S、CNOT、X、Z、测量、复位），但能模拟成千上万个比特；它把测量记录在 `measurement_record` 里，天然适合含大量中途测量的纠错线路。
- **远程 QPU**：取决于硬件是否支持实时反馈；若不支持，只能退化为 `emulate`（本地模拟）。

CUDA-Q 用一个目标能力位 `supportConditionalsOnMeasureResults` 来声明某后端是否允许在 `sample` 里跑条件反馈，[默认值为 `true`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Target/CompileTarget.h#L82)。远程后端则根据 `emulate` 标志动态设置（见 `BaseRemoteRESTQPU.h`）。

#### 4.3.2 核心流程

当一个含条件反馈的内核被提交执行，后端侧的关键决策点是：

```
内核被标记 hasConditionalsOnMeasureResults = true
        │
        ├─ 目标是否 supportConditionalsOnMeasureResults?
        │     ├─ 否 → 抛错："Use cudaq::run or cudaq::run_async instead"
        │     └─ 是 → 继续
        │
        ├─ getNumShotsToExec() 被强制返回 1   （逐 shot 执行）
        │
        ├─ flushAnySamplingTasks 提前返回      （不能批量合并采样）
        │
        └─ 后端逐 shot：
              ├─ qpp ：measureQubit 真实塌缩状态向量
              └─ stim：在 measurement_record 里记账，XOR 参考样本
```

`getNumShotsToExec` 返回 1 意味着「模拟器内部每次只跑 1 个 shot，由主机层循环 `shots` 次」，这正是动态线路（dynamic circuit）区别于静态批量采样的执行形态。

#### 4.3.3 源码精读

**目标能力位与拒绝逻辑。** [`CompileTarget.h`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Target/CompileTarget.h#L82) 里 `supportConditionalsOnMeasureResults` 默认为 `true`，所以 `qpp`、`stim` 等本地后端天然放行。一旦某目标把它设为 `false`，[`Compiler.cpp`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/internal/compiler/Compiler.cpp#L424-L433) 会在编译期就抛错，引导用户改用 `cudaq::run`：

```cpp
// runtime/internal/compiler/Compiler.cpp
if (hasConditionalsOnMeasRes &&
    !target->supportConditionalsOnMeasureResults) {
  throw std::runtime_error(
      "`cudaq::sample` and `cudaq::sample_async` no longer support kernels "
      "that branch on measurement results. Kernel '" + kernelName +
      "' uses conditional feedback. Use `cudaq::run` or `cudaq::run_async` "
      "instead. See CUDA-Q documentation for migration guide.");
}
```

**逐 shot 强制。** [`getNumShotsToExec`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/CircuitSimulator.h#L584-L594) 在 `hasConditionalsOnMeasureResults` 为真时直接返回 1：

```cpp
// runtime/nvqir/CircuitSimulator.h
if (executionContext->hasConditionalsOnMeasureResults)
  return 1;
```

配合 [`flushAnySamplingTasks` 在条件反馈时提前返回](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/CircuitSimulator.h#L766-L770)，确保测量不会被错误地批量合并：

```cpp
// runtime/nvqir/CircuitSimulator.h
if (sampleQubits.empty())
  return;
if (executionContext->hasConditionalsOnMeasureResults && !force)
  return;   // 条件反馈：禁止合并采样
```

**`stim` 的中途测量实现。** 稳定器后端重写了 [`measureQubit`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/stim/StimCircuitSimulator.cpp#L643-L661)，用 tableau 比特与采样器比特的 XOR 得到最终测量值：

```cpp
// runtime/nvqir/stim/StimCircuitSimulator.cpp
bool measureQubit(const std::size_t index) override {
  applyOpToSims("M", std::vector<std::uint32_t>{...index});
  num_measurements++;
  const bool tableauBit = *tableau->measurement_record.storage.crbegin();
  bool sampleSimBit =
      sampleSim->m_record.storage[num_measurements - 1][/*shot=*/0];
  bool result = tableauBit ^ sampleSimBit;   // 中途样本与 tableau 比特异或
  return result;
}
```

stim 还专门处理「只保留最末若干测量，其余视为已记账的中途测量」（见 [第 743–745 行注释](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/stim/StimCircuitSimulator.cpp#L743-L745)），并声明 [`canHandleObserve() = false`](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/stim/StimCircuitSimulator.cpp#L681)——稳定器后端不支持 `observe` 的期望值直接计算。

**`qpp` 与 `stim` 的能力对照：**

| 维度 | `qpp`（CPU 状态向量） | `stim`（稳定器） |
| --- | --- | --- |
| 支持线路 | 任意 | 仅 Clifford（H/S/CNOT/X/Z/mz/reset） |
| 中途测量 | 逐 shot 真实塌缩 | tableau + 参考样本 XOR，记账式 |
| 比特规模 | < 30 左右（内存指数增长） | 数千及以上 |
| `observe` | 支持 | 不支持（`canHandleObserve=false`） |
| 条件反馈 | 支持（`supportConditionalsOnMeasureResults` 默认 true） | 支持 |

#### 4.3.4 代码实践

**实践目标：** 用同一个 Clifford 内核，分别指定 `qpp` 与 `stim` 后端运行，对比结果与适用性。

**操作步骤：**

1. 用 4.1.4 的隐形传态内核（它只含 H、X、CNOT、Z、`mz`，全部是 Clifford 门），分别用两个目标编译：

   ```bash
   nvq++ -t qpp   docs/sphinx/examples/cpp/basics/mid_circuit_measurement.cpp -o teleport_qpp.x
   nvq++ -t stim  docs/sphinx/examples/cpp/basics/mid_circuit_measurement.cpp -o teleport_stim.x
   ./teleport_qpp.x
   ./teleport_stim.x
   ```

   > `stim` 目标对应稳定器后端，[文档](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/using/backends/simulators.rst#L83-L88)将其列为用于 QEC 仿真、可跑数千比特的 CPU 后端。

2. 再写一个**含非 Clifford 门**的对照内核（示例代码），例如把传态前的制备从 `x(q[0])` 换成 `t(q[0])`（T 门非 Clifford），同样分别用 `qpp` 与 `stim` 编译运行。

**需要观察的现象：**

- 对纯 Clifford 的传态内核：两个后端都应让 `q[2]` 恒为 1（`assert` 通过），统计分布一致。
- 对含 T 门的内核：`qpp` 仍能正常运行；`stim` 因只能模拟 Clifford 线路，预期会在运行或编译阶段报错。

**预期结果：** 纯 Clifford 时 `qpp` 与 `stim` 结果一致；引入非 Clifford 门后 `stim` 不再适用。具体报错文案与触发阶段（编译期 vs 运行期）**待本地验证**——本实践的重点是观察「同一内核、不同后端」的适用边界，而非具体报错字串。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「带条件反馈的小内核 + 多后端对照」任务。

**任务：** 实现一个「如果测量到 1 则翻转辅助比特」的内核，先用 `cudaq::run` 在默认 `qpp` 后端验证逻辑，再切到 `stim` 后端对照，最后回答：条件反馈在哪一层被识别？两个后端结果是否一致？为什么？

**建议步骤：**

1. **写内核（示例代码）。** 在 4.2.4 的 `cond_feedback.cpp` 基础上，把辅助比特改为由 `q0` 的中途测量结果驱动：`x(q[0]); auto b = mz(q[0]); if (b) x(q[1]);`，主程序用 `cudaq::run(1000, kernel{})` 统计 `q1` 的分布。

2. **`qpp` 上验证。** `nvq++ cond_feedback.cpp -o cf_qpp.x && ./cf_qpp.x`，记录 `q1==1` 的次数，应接近 1000。

3. **`stim` 上对照。** `nvq++ -t stim cond_feedback.cpp -o cf_stim.x && ./cf_stim.x`，记录同样指标。该内核只含 X 与测量，属于 Clifford，`stim` 应能跑出与 `qpp` 一致的比例。

4. **静态识别验证（可选）。** 用 `CUDAQ_LOG_LEVEL=info` 重跑，从日志里寻找「逐 shot 执行」的迹象（`getNumShotsToExec` 返回 1），确认条件反馈被识别。具体日志字段**待本地验证**。

5. **回答三个问题：**
   - 条件反馈在 **AST 桥 / 编译期分析**（MLIR 模式）或 **tracer 探测**（库模式）层被识别，结果存入 `hasConditionalsOnMeasureResults`。
   - `qpp` 与 `stim` 在 Clifford 范围内结果一致。
   - 一致的原因：两个后端都把 `supportConditionalsOnMeasureResults` 默认置真，且都被强制逐 shot 执行；区别仅在内部仿真算法（状态向量塌缩 vs 稳定器记账）与可处理线路范围。

## 6. 本讲小结

- **中途测量 = 中段塌缩 + 后续演化**，与 u2-l3 的末端批量采样相对；其可观测后果是「后续门可以依赖单次测量值」。
- CUDA-Q 用**延迟判别**统一两种情形：`mz` 返回测量句柄（MLIR 的 `measure_handle` / 库模式的 `measure_result`），只有在被「消费」时才塌缩。
- **`cudaq::run` 天然支持中途测量**（上下文名 `"run"`，`mz` 真实塌缩、逐 shot 返回值）；**`cudaq::sample` 默认延迟**，但若内核含条件反馈，会通过 `hasConditionalsOnMeasureResults` 切换到逐 shot 执行。
- 条件反馈在 MLIR 模式由 `quake.discriminate` + `cc.if` 兑现，在库模式由 `__nvqpp__MeasureResultBoolConversion` 兑现；入口内核不得让测量句柄跨主机/设备边界。
- 后端是否能在 `sample` 里跑条件反馈，取决于 `supportConditionalsOnMeasureResults`（默认 true），否则报「Use `cudaq::run` instead」。
- `qpp` 通用但内存指数增长、`stim` 仅 Clifford 但可跑数千比特；二者在 Clifford 范围内对中途测量的统计结果一致。

## 7. 下一步学习建议

- **执行模型全景：** 本讲只讲了 `run`/`sample` 对中途测量的处理，建议下一站读 u3-l1「执行模型：quantum_platform 与 QPU」，把 `ExecutionContext`、平台分发与 `hasConditionalsOnMeasureResults` 这条链放到完整执行框架里理解。
- **噪声与稳定器：** 若对 `stim` 的 `measurement_record` 与稳定器仿真感兴趣，可先读 u6-l3「噪声建模与密度矩阵模拟」，再看 `stim` 后端的探测器（detector）/逻辑可观测量机制（`qubit_qis.h` 中的 `detector`/`logical_observable` 注释是入口）。
- **动态线路与远程 QPU：** 真实 QPU 对实时反馈的支持各不相同，建议结合 u6-l5「远程 QPU、互操作与 OpenQASM 导出」了解 `emulate` 标志如何决定 `supportConditionalsOnMeasureResults`。
- **源码延伸阅读：** 想看「条件反馈探测」的完整实现，可顺着 `runtime/cudaq/algorithms/sample.h` 的 tracer 分支 → `runtime/cudaq/qis/execution_manager_c_api.cpp` → `runtime/internal/compiler/Compiler.cpp` 的 `hasConditionalsOnMeasureResults` 这条线读下去。
