# 量子动力学与时间演化（含批处理 evolve）

## 1. 本讲目标

本讲聚焦 CUDA-Q 的「动力学（dynamics）」能力——把一个量子系统在含时哈密顿量下随时间演化，并在每个时间点记录态或可观测量。学完后你应当能够：

- 说清 `cudaq.evolve` 解决的是什么问题（薛定谔方程 / Lindblad 主方程的数值积分），以及它与 `sample`/`observe` 这类「量子门采样」语义的本质区别。
- 区分 `evolve` 在不同后端上的两条执行路径：`dynamics`（GPU、cuDensityMat、真正批处理）与非 `dynamics` 目标（`qpp-cpu`/`density-matrix-cpu`，Python 端逐状态循环）。
- 掌握三种内置积分器（Runge-Kutta、Crank-Nicolson、Magnus 展开）的原理与适用场景，并知道积分器头文件近期被迁移过。
- 理解「单个哈密顿量 + 多个初始状态」这种批处理调用的语义，以及近期 PR #4835 修复的一个会让该调用**静默返回空列表**的 bug，连同 `Schedule` 作为有状态迭代器为何必须在每次演化前 `reset()`。

> 本讲是 `update` 模式新增的讲义。其触发点是 commit `a250d5c8`（PR #4835）修复的批处理 bug，因此「批处理初始状态调度」一节会结合该 diff 重点讲解。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（对应前置讲义）：

- **算符代数（u3-l5）**：`cudaq.evolve` 的输入是 `Operator`（`spin_op`/`boson_op`/`matrix_op` 等统一体系）。你需要知道算符可以带参数（`scalar_operator`，支撑含时哈密顿量）、可以 `.to_matrix(dimensions)` 求矩阵、可以 `.evaluate(**params)` 求值。本讲会直接使用这些能力。
- **执行模型（u3-l1）**：`evolve` 仍然走 `quantum_platform`/`QPU`/`ExecutionContext` 的分发骨架，只是上下文名变成了 `"evolve"`，并且只在**模拟器**后端上工作。
- **状态获取（u3-l6）**：`evolve` 的输入/输出是完整的量子态 `cudaq::state`（密度矩阵或态向量），而不是采样计数——这一点与 `get_state` 同源。

几个动力学专属的基础概念，先用最朴素的方式解释：

- **含时哈密顿量**：系统的能量算符显式依赖时间，写作 \( H(t) \)。例如一个被外场驱动的自旋，\( H(t) = \frac{\Omega(t)}{2}\sigma_x + \frac{\Delta(t)}{2}\sigma_z \)，其中 \(\Omega(t)\)、\(\Delta(t)\) 随时间变化。
- **薛定谔方程（闭系统）**：态矢量演化服从
  \[ \frac{d}{dt}|\psi(t)\rangle = -i\,H(t)\,|\psi(t)\rangle \]
  在一小段时间 \(\Delta t\) 内，若 \(H\) 近似不变，则 \( |\psi(t+\Delta t)\rangle \approx e^{-i H \Delta t}|\psi(t)\rangle \)。
- **Lindblad 主方程（开系统）**：当系统与环境耦合（有噪声）时，密度矩阵 \(\rho\) 演化服从
  \[ \dot\rho = -i[H,\rho] + \sum_k \gamma_k\!\left(L_k \rho L_k^\dagger - \tfrac{1}{2}\{L_k^\dagger L_k, \rho\}\right) \]
  其中 \(L_k\) 是「塌缩算符（collapse operator）」，描述一种耗散通道（如光子泄漏、振幅阻尼）。
- **数值积分器（integrator）**：把连续微分方程离散化、逐步推进时间的算法。不同积分器在精度、稳定性、是否保持酉性上各有取舍——这是本讲第 4 节的核心。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `python/cudaq/dynamics/evolution.py` | Python 顶层 `cudaq.evolve`/`evolve_async`，负责分发到 dynamics 或非 dynamics 路径，**批处理 bug 及其修复就在这里**。 |
| `python/cudaq/dynamics/schedule.py` | `Schedule` 类——一个**有状态的迭代器**，产出每个时间步的参数映射。`reset()` 是修复的关键。 |
| `python/cudaq/dynamics/cudm_solver.py` | `dynamics` 目标的 Python 求解器入口 `evolve_dynamics`，处理批大小与多哈密顿量分批。 |
| `runtime/cudaq/algorithms/evolve.h` | C++ `cudaq::evolve` 重载集合（单态/批、单哈/多哈、超算符），声明入口。 |
| `runtime/cudaq/algorithms/integrator.h` | **当前正典**的三种积分器：`runge_kutta`/`crank_nicolson`/`magnus_expansion`。 |
| `runtime/cudaq/dynamics_integrators.h` | **已废弃**的转发头，仅提示改用 `integrator.h`。 |
| `runtime/nvqir/cudensitymat/CuDensityMatSim.cpp` | cuDensityMat 后端模拟器，注册为 `dynamics`。 |
| `runtime/nvqir/cudensitymat/CuDensityMatEvolution.cpp` | C++ 端 `evolveSingle`/`evolveBatched` 实现，含真正的批处理（打包密度矩阵）。 |
| `runtime/nvqir/cudensitymat/CuDensityMatTimeStepper.h` | 把 Liouvillian 作用交给 cuDensityMat 库计算的时间步进器。 |
| `runtime/nvqir/cudensitymat/dynamics.yml` | `dynamics` 目标的 YAML 配置（GPU、`CUDAQ_ANALOG_TARGET`）。 |
| `runtime/nvqir/qpp/qpp-cpu.yml`、`density-matrix-cpu.yml` | 两个 CPU 目标配置，`simulator` 字段分别是 `qpp` 与 `dm`。 |
| `python/tests/dynamics/test_evolve_simulators.py` | 含 PR #4835 新增的回归测试。 |

## 4. 核心概念与源码讲解

### 4.1 evolve 时间演化

#### 4.1.1 概念说明

`cudaq.evolve` 是 CUDA-Q 的「主方程求解器」。与 `sample`（重复跑线路、给计数分布）和 `observe`（给期望值）不同，`evolve` 关心的是**系统的连续时间演化轨迹**：给定初态 \(\rho(0)\)（或 \(|\psi(0)\rangle\)）、含时哈密顿量 \(H(t)\)、（可选）塌缩算符 \(L_k\) 和一组时间点，求出每个时间点上的态与可观测量期望值。

这正是动力学模拟与量子门线路模拟的根本区别：线路模拟把系统当作「施加一串离散逻辑门」，动力学模拟则「直接解系统随时间的物理演化」，能刻画驱动脉冲、谐振腔、耦合器、噪声等门级抽象以下的物理细节（见 `docs/sphinx/examples/python/dynamics/dynamics_intro_1.ipynb` 开篇的类比）。

一个典型的调用形如（取自该 notebook）：

```python
evolution_result = cudaq.evolve(
    hamiltonian,          # 含时哈密顿量（Operator）
    dimensions,           # 每个自由度的能级数，如 {0: 2, 1: 2}
    schedule,             # 时间步 Schedule
    rho0,                 # 初态
    observables=[...],    # 要记录期望值的算符
    collapse_operators=[],# 塌缩算符（开系统噪声）
    store_intermediate_results=cudaq.IntermediateResultSave.EXPECTATION_VALUE,
    integrator=ScipyZvodeIntegrator())
```

#### 4.1.2 核心流程

`evolve` 的第一道分叉是**目标（target）**。在 `python/cudaq/dynamics/evolution.py` 中，顶层 `evolve` 先做参数校验，然后按下图分发：

```
cudaq.evolve(...)
   │
   ├─ target == "dynamics" ─────────────► evolve_dynamics()  → C++ evolveSingle/evolveBatched（cuDensityMat，GPU）
   │                                      （cudm_solver.py，真正批处理）
   │
   └─ 其他目标 (qpp-cpu / density-matrix-cpu / ...)
         │
         ├─ hamiltonian 是 Sequence ──► 逐对 zip(ham, state, collapse_ops) 调 evolve_single
         │
         ├─ initial_state 是 Sequence ──► 对每个 state 复用同一个 hamiltonian 调 evolve_single  ★ 修复点
         │
         └─ 单个初态 ────────────────► 直接调 evolve_single
```

非 `dynamics` 路径的 `evolve_single` 并不调用 C++ 积分器，而是**把时间演化翻译成一串「步矩阵」相乘的量子线路**：对每个时间步算出 \(U_{\text{step}} = e^{-i H \Delta t}\)（用泰勒级数近似矩阵指数），把它注册成一个自定义门，再拼出一个内核依次施加所有步矩阵。这正是它只能在「量子比特」（`dimension == 2`）上工作、且塌缩算符只能在 `dm` 模拟器上工作的原因。

#### 4.1.3 源码精读

**顶层分发**：[python/cudaq/dynamics/evolution.py:517-552](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/cudaq/dynamics/evolution.py#L517-L552) — `dynamics` 目标走 `evolve_dynamics`，其余目标在「`initial_state` 是序列」时进入批处理分支（★ 即修复所在）。

**步矩阵计算**：[python/cudaq/dynamics/evolution.py:96-111](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/cudaq/dynamics/evolution.py#L96-L111) — `op_matrix = -1j * H.to_matrix(...) * dt`，CPU 端用 `_taylor_series_expm`（泰勒展开）近似 \(e^{-iH\Delta t}\)，GPU 端可用 cupy 的 `cp.exp`。

**演化内核生成**：[python/cudaq/dynamics/evolution.py:202-246](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/cudaq/dynamics/evolution.py#L202-L246) — `_evolution_kernel` 把每个步矩阵 `register_operation` 成自定义门，然后用 `make_kernel` 拼出「依次施加」的量子内核。第 0 步是恒等矩阵（即初态本身）。

**evolve_single 主干**：[python/cudaq/dynamics/evolution.py:249-314](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/cudaq/dynamics/evolution.py#L249-L314) — 这里能看到两处关键约束：第 278-282 行要求所有 `dimensions` 必须为 2（qubit）；第 285-288 行要求塌缩算符只在 `simulator == "dm"` 时可用。第 300 行的 `schedule.reset()` 是单次演化自身就把迭代器「物化」为列表的前提（见 4.4 节）。

**C++ 单态入口（仅供 dynamics 目标编译）**：[runtime/cudaq/algorithms/evolve.h:82-104](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/cudaq/algorithms/evolve.h#L82-L104) — 注意整个 `cudaq::evolve` 模板族都被 `#if defined(CUDAQ_ANALOG_TARGET)` 守卫，非 dynamics 目标编译会触发 `static_assert` 报错。C++ 的 `evolve` 仅是声明层，真正实现在 cuDensityMat 后端。

#### 4.1.4 代码实践

**实践目标**：亲手跑一次最简单的自旋进动，确认 `evolve` 的输出形态。

1. 安装带 dynamics 支持的 cudaq（或用开发容器）。
2. 把下面这段（**示例代码**，仿照 `dynamics_intro_1.ipynb` 的结构简化）保存为 `spin_precession.py`：

   ```python
   # 示例代码
   import numpy as np, matplotlib.pyplot as plt
   import cudaq
   from cudaq import spin, Schedule

   cudaq.set_target("qpp-cpu")                 # 用 CPU 状态向量后端即可
   dimensions = {0: 2}                          # 1 个 qubit
   H = spin.x(0)                                # H = σx，绕 x 轴进动

   steps = np.linspace(0, np.pi, 51)            # 0 ~ π
   schedule = Schedule(steps, ["time"])

   psi0 = cudaq.State.from_data(np.array([1.0, 0.0], dtype=np.complex128))  # |0>

   result = cudaq.evolve(
       H, dimensions, schedule, psi0,
       observables=[spin.z(0)],
       store_intermediate_results=cudaq.IntermediateResultSave.EXPECTATION_VALUE)

   z_vals = [ev.expectation() for ev in result.expectation_values()]
   plt.plot(steps, z_vals); plt.xlabel("t"); plt.ylabel("<σz>"); plt.show()
   ```

3. 运行 `python spin_precession.py`。
4. **需要观察的现象**：`<σz>` 应随时间按 \(\cos(2t)\) 振荡（因为 \(H=\sigma_x\) 下 \(|0\rangle\) 绕 x 轴进动，\(\langle\sigma_z\rangle(t)=\cos(2t)\)）。
5. **预期结果**：曲线在 \(t=0\) 为 1、\(t=\pi/2\) 为 -1、\(t=\pi\) 回到 1。若你只装了 CPU 版本无法用 `dynamics` 目标，`qpp-cpu` 同样能跑通这段（它走的是 4.1.2 描述的「步矩阵相乘」路径）。**待本地验证**精确数值。

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `H` 换成 `spin.z(0)`，初态仍为 `|0>`，`<σz>` 随时间会是什么形状？

**答案**：\(H=\sigma_z\) 与 `|0>` 都是 \(\sigma_z\) 的本征态，态不随时间改变（只多一个全局相位），故 \(\langle\sigma_z\rangle(t)\equiv 1\)，是一条水平直线。

**练习 2**：为什么 `evolve_single` 在非 dynamics 目标上要求 `dimensions` 全为 2？

**答案**：因为该路径把步矩阵 \(e^{-iH\Delta t}\) 注册成「自定义量子门」施加到量子比特上，而量子门模型只描述 qubit；非 2 维的 qudit 系统必须用 `dynamics` 目标（cuDensityMat 直接做密度矩阵演化，不经过门抽象）。源码注释见 [evolution.py:278-282](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/cudaq/dynamics/evolution.py#L278-L282)。

### 4.2 积分器策略

#### 4.2.1 概念说明

无论薛定谔方程还是 Lindblad 方程，本质上都是「给定当前态和时间，求下一步态」的常微分方程（ODE）。**积分器（integrator）** 就是干这件事的算法。CUDA-Q 内置三种积分器，它们都继承自 `base_integrator`，对外暴露统一接口：`setState(初态, t0)` → 反复 `integrate(目标时刻)` → `getState()` 取当前 `(t, state)`。

选择哪种积分器，是在**精度、稳定性、计算量、是否保酉性**之间做权衡：

- **Runge-Kutta（龙格-库塔）**：最通用的显式 ODE 积分器，默认 4 阶（RK4）。每步多次求值右端函数，精度高、实现简单，适合大多数光滑、非刚性的哈密顿量。
- **Crank-Nicolson（克兰克-尼科尔森）**：隐式方法，配合预测-校正（predictor-corrector）迭代。隐式格式对刚性（stiff）方程更稳定，长时间演化不易发散。
- **Magnus 展开（Magnus expansion）**：专门为量子演化设计。它直接逼近**时间有序指数** \( \mathcal{T}\exp(\int -iH(t)\,dt) \)，用 \(\exp(h\cdot L_{\text{mid}})\)（区间中点的有效生成元）的泰勒级数近似，**天然保持酉性/保迹**——这对闭系统尤为重要，因为数值误差不会让态「泄漏」出概率空间。

> 注意：非 dynamics 路径（4.1）不使用这些 C++ 积分器，它直接用矩阵指数相乘；这些积分器主要服务于 `dynamics`（cuDensityMat）目标，以及 Python 端通过 `ScipyZvodeIntegrator` 等包装接入的第三方求解器。

#### 4.2.2 核心流程

所有积分器都实现同一套「时间步进」骨架，差异只在「如何由当前态算出下一态」：

```
setState(ρ0, t0)
for 每个 schedule 时刻 t_target:
    integrate(t_target)        # 内部可能做若干子步，每子步调用 time_stepper.compute(...)
    getState() → (t, ρ_t)      # 记录态 / 期望值
```

其中 `time_stepper`（如 `CuDensityMatTimeStepper`）负责「算一次 Liouvillian 作用」这件最贵的操作——在 dynamics 目标上它调用 cuDensityMat 库；积分器只决定**调用几次、如何加权组合**。例如 RK4 每步调 4 次、Magnus 在区间中点算有效生成元后再做泰勒展开。

#### 4.2.3 源码精读

**当前正典头文件**：[runtime/cudaq/algorithms/integrator.h:19-50](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/cudaq/algorithms/integrator.h#L19-L50) — `runge_kutta` 类，默认阶数 4（注释说明 4 阶兼顾收敛与稳定性，且「Runge-Kutta」通常就指 RK4），可配 `max_step_size` 限制内部子步。

**Crank-Nicolson**：[runtime/cudaq/algorithms/integrator.h:53-81](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/cudaq/algorithms/integrator.h#L53-L81) — 默认 2 步校正（1 次预测 + 2 次校正），同样支持 `max_step_size` 子步切分。

**Magnus 展开**：[runtime/cudaq/algorithms/integrator.h:84-111](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/cudaq/algorithms/integrator.h#L84-L111) — 默认最多 10 个泰勒项近似 \(\exp(h\cdot L_{\text{mid}})\)，某项小到可忽略时提前退出。

**已废弃的转发头**：[runtime/cudaq/dynamics_integrators.h:13-19](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/cudaq/dynamics_integrators.h#L13-L19) — 老代码若 `#include "cudaq/dynamics_integrators.h"` 会触发 `[[deprecated]]` 警告，提示改用 `cudaq/algorithms/integrator.h`。这正是本讲规格中「`dynamics_integrators.h`」一项的现状：它只是个转发壳。

**cuDensityMat 时间步进器**：[runtime/nvqir/cudensitymat/CuDensityMatTimeStepper.h:16-32](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/nvqir/cudensitymat/CuDensityMatTimeStepper.h#L16-L32) — `compute(inputState, t, parameters)` 是积分器每步实际调用的函数，内部 `computeImpl` 把状态交给 cuDensityMat 算 Liouvillian 作用，支持 `batchSize`（批处理态）。

**Python 端积分器基类**：[python/cudaq/dynamics/integrator.py](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/cudaq/dynamics/integrator.py) — `BaseIntegrator`（抽象），内置实现在 `python/cudaq/dynamics/integrators/`（`builtin_integrators.py` 提供 `RungeKuttaIntegrator` 等，另有 `scipy_integrators.py`、`cuda_torchdiffeq_integrator.py` 接入第三方 ODE 求解器）。

#### 4.2.4 代码实践

**实践目标**：体会「积分器是可替换的旋钮」。

1. 复用 4.1.4 的自旋进动脚本，但目标切到 `dynamics`（需 GPU 与 cuQuantum）：`cudaq.set_target("dynamics")`。
2. 分别用三种内置积分器跑同一演化：
   ```python
   # 示例代码
   from cudaq import RungeKuttaIntegrator, CrankNicolsonIntegrator, MagnusExpansionIntegrator
   for I in [RungeKuttaIntegrator(), CrankNicolsonIntegrator(), MagnusExpansionIntegrator()]:
       result = cudaq.evolve(H, dimensions, schedule, rho0,
                             observables=[spin.z(0)],
                             store_intermediate_results=cudaq.IntermediateResultSave.EXPECTATION_VALUE,
                             integrator=I)
       print(type(I).__name__, [ev.expectation() for ev in result.expectation_values()][-1])
   ```
3. **需要观察的现象**：三种积分器在步长足够小时应给出几乎相同的末态 \(\langle\sigma_z\rangle\)；把 `steps` 改得很稀疏（如只取 5 个点）时，Magnus 通常最稳（保酉），RK4 次之。
4. **预期结果**：稠密时间网格下三者一致；稀疏网格下可能出现差异。**待本地验证**（需要 dynamics 目标环境）。

#### 4.2.5 小练习与答案

**练习**：Magnus 展开积分器为什么特别适合**闭系统**（无塌缩算符）的长时间演化？

**答案**：闭系统演化是酉的，\(\rho\) 或 \(|\psi\rangle\) 的「范数/迹」必须守恒。Magnus 直接逼近时间有序指数 \( \mathcal{T}e^{\int -iH\,dt} \)，其本身是酉算符，因此数值上天然保酉，误差不会让概率「漏掉」。显式 RK 在长时间或大步长下可能引入微小非酉误差累积。

### 4.3 cudensitymat 后端

#### 4.3.1 概念说明

`dynamics` 目标是 CUDA-Q 动力学的「重型」后端：它基于 NVIDIA 的 **cuDensityMat** 库，在 GPU 上做密度矩阵演化，面向大希尔伯特空间（官方示例称千级以上维度相对 CPU 可有 >1000× 加速），并具备多 GPU、多节点（MPI）能力。它直接解 Lindblad 主方程，**不经过量子门抽象**，因此既能处理 qubit，也能处理任意 d 能级系统（transmon、谐振腔等）。

在运行时体系里，`dynamics` 就是「链接期决定的模拟器」之一（承接 u6 的「链接期决定后端」）：它由一个继承自 `CircuitSimulatorBase` 的类实现，通过注册宏暴露成名为 `dynamics` 的后端，再用 YAML 目标配置（`dynamics.yml`）把它包装成一个可供 `cudaq.set_target("dynamics")` 使用的目标。

#### 4.3.2 核心流程

`dynamics` 目标的演化走 C++：

```
Python: evolve_dynamics()                          (cudm_solver.py)
   └─► bindings.evolveSingle / evolveBatched
        └─► CuDensityMatEvolution.cpp:
              evolveSingle  → evolveSingleImpl:  逐 schedule 步 integrator.integrate(t)，取态、算期望
              evolveBatched → evolveBatchedImpl: 把多个态打包成一个 batched 密度矩阵，
                                               一次性积分，最后 splitBatchedState 拆开
```

**真正的批处理**发生在 `evolveBatchedImpl`：它不是「循环多次单态演化」，而是把 `batchSize` 个初始态塞进同一个批处理密度矩阵（cuDensityMat 原生支持），让 Liouvillian 一次性作用在整批上，最后再 `splitBatchedState` 拆成各自的结果。这正是 `dynamics` 目标在「单哈密顿量 + 多初态」上**从不返回空列表**的原因——它走的是 C++ 的批处理重载，而非 4.4 节那个有 bug 的 Python 循环。

#### 4.3.3 源码精读

**模拟器类**：[runtime/nvqir/cudensitymat/CuDensityMatSim.cpp:65](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/nvqir/cudensitymat/CuDensityMatSim.cpp#L65) — `CuDensityMatSim` 继承 `CircuitSimulatorBase<double>` 与 `MpiCircuitSimulator`（后者赋予它 MPI 分布能力）。

**注册宏**：[runtime/nvqir/cudensitymat/CuDensityMatSim.cpp:209](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/nvqir/cudensitymat/CuDensityMatSim.cpp#L209) — `NVQIR_REGISTER_SIMULATOR(CuDensityMatSim, dynamics)`，把该类注册成名为 `dynamics` 的模拟器。

**目标配置**：[runtime/nvqir/cudensitymat/dynamics.yml:11-13](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/nvqir/cudensitymat/dynamics.yml#L11-L13) — `nvqir-simulation-backend: dynamics`、`platform-library: mqpu`、`preprocessor-defines` 含 `CUDAQ_ANALOG_TARGET`。注意 `gpu-requirements: true`，无 GPU 时该目标不可用。

**单态演化实现**：[runtime/nvqir/cudensitymat/CuDensityMatEvolution.cpp:206-267](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/nvqir/cudensitymat/CuDensityMatEvolution.cpp#L206-L267) — `evolveSingleImpl` 的核心是第 223 行 `for (const auto &step : schedule)`：每个时间点 `integrator.integrate(step.real())`，取当前态，按 `storeIntermediateResults` 决定是否记录中间态与期望值。

**真正批处理实现**：[runtime/nvqir/cudensitymat/CuDensityMatEvolution.cpp:343-422](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/nvqir/cudensitymat/CuDensityMatEvolution.cpp#L343-L422) — `evolveBatchedImpl` 第 380 行同样遍历 schedule，但第 389 行 `expectation.compute(..., batchSize)` 一次算出整批的期望，第 397 行 `splitBatchedState` 拆批；第 352-359 行还校验批大小必须能被 MPI rank 数整除（分布式均匀划分）。

**C++ 批处理声明（单哈 + 多态）**：[runtime/cudaq/algorithms/evolve.h:269-292](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/cudaq/algorithms/evolve.h#L269-L292) — `evolve(hamiltonian, ..., vector<state>, ...)` 重载，转调 `detail::evolveBatched`。

#### 4.3.4 代码实践

**实践目标**：用「源码阅读」确认 `dynamics` 目标的批处理与 Python 循环批处理是两条不同路径。

1. 打开 [CuDensityMatEvolution.cpp:380](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/nvqir/cudensitymat/CuDensityMatEvolution.cpp#L380)，注意 `evolveBatchedImpl` 只遍历 `schedule` 一次，且 `batchSize` 作为参数贯穿期望计算与拆批。
2. 对照 [evolve.h:269-292](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/cudaq/algorithms/evolve.h#L269-L292)，确认「单哈密顿量 + 多初态」在 C++ 端对应的是 `evolveBatched`（不是循环 `evolveSingle`）。
3. **需要观察的现象**：C++ 端的批处理是「一次积分一批态」，而 4.4 节修复的 Python 路径是「循环多次单态」——这两条路径在「单哈 + 多态」语义上结果应一致，但实现与性能特性不同。
4. **预期结果**：你能用自己的话指出：`dynamics` 目标的「批」是密度矩阵维度的真批；`qpp-cpu`/`density-matrix-cpu` 的「批」是 Python 层的 for 循环。

#### 4.3.5 小练习与答案

**练习**：为什么 `dynamics.yml` 要定义 `CUDAQ_ANALOG_TARGET` 这个预处理宏？

**答案**：C++ 的 `cudaq::evolve` 全族模板用 `#if defined(CUDAQ_ANALOG_TARGET)` 守卫（见 [evolve.h:94](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/cudaq/algorithms/evolve.h#L94)）。只有当目标是 `dynamics`（或中性原子模拟目标）时，应用才会带着这个宏编译，`evolve` 的 C++ 实现才可见；否则触发 `static_assert`，提示用 `--target dynamics` 重编译。这样把「动力学 C++ API」与「普通量子门程序」的编译期依赖隔离开。

### 4.4 批处理初始状态调度（★ 近期修复重点）

#### 4.4.1 概念说明

`cudaq.evolve` 支持把**一组初始状态**喂给**同一个**（或一组）哈密顿量，一次性算出每个初态的演化结果——这在需要扫描多个初态（如不同初始布居、参数化初态族）时很常用。调用形如：

```python
cudaq.evolve(
    hamiltonian,          # 单个 Operator
    dimensions,
    schedule,
    [initial_state_0, initial_state_1],   # 多个初态
)
```

PR #4835（commit `a250d5c8`，标题 *Fix cudaq.evolve with shared Hamiltonian and batched initial states*）修复的正是这种用法在**非 dynamics 目标**上的一个 bug：修复前它会**静默返回空列表 `[]`**，而不是「每个初态一个结果」。

#### 4.4.2 核心流程（含 bug 与修复）

**Bug 成因**——修复前，非 dynamics 分支对 `initial_state` 是序列的情况一律写：

```python
return [
    evolve_single(ham, dimensions, schedule, state, collapse_ops, ...)
    for ham, state, collapse_ops in zip(hamiltonian, initial_state, collapse_operators)
]
```

问题在 `zip`：

- `hamiltonian` 是**单个** Operator，`collapse_operators` 默认是**空列表** `[]`；
- `zip` 在最短的输入处截断，空列表 `[]` 让整个 zip **直接产出 0 项**；
- 于是列表推导生成 `[]`，`cudaq.evolve` 没有任何报错，静默返回空。

**修复**（[evolution.py:528-552](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/cudaq/dynamics/evolution.py#L528-L552)）做了三件事：

1. 区分两种「批」：
   - `hamiltonian` **是**序列 → 保留老的「一哈一态」zip 配对（每对用各自的哈密顿量）；
   - `hamiltonian` **不是**序列（单个算符）→ 对每个 `state` **复用同一个** `hamiltonian` 和同一个 `collapse_operators`。
2. 抽出 `evolve_one(ham, state, collapse_ops)` 闭包，在每次调用 `evolve_single` **之前**先 `schedule.reset()`。
3. 这样每个初态都从完整 schedule 的起点开始演化，且每个初态都产生一个结果。

**为什么必须 `schedule.reset()`**——这是修复里最微妙的一点。`Schedule` 不是普通列表，而是一个**有状态迭代器**：它的 `_current_idx` 在被迭代（`__next__`）时单调递增，直到 `StopIteration`。而 `evolve_single` 内部第 300 行 `parameters = [mapping for mapping in schedule]` 会**把整个 schedule 物化一遍**，这会把迭代器指针推到末尾。于是如果不 reset，**第二个初态**拿到的是一个已经耗尽的 schedule → 没有时间步 → 结果为空/错误。`schedule.reset()` 把 `_current_idx` 拨回 -1，让每个初态都重新从第一步走完整条时间轴。

#### 4.4.3 源码精读

**修复后的批处理分支**：[python/cudaq/dynamics/evolution.py:528-552](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/cudaq/dynamics/evolution.py#L528-L552) — 注释里 `evolve_one` 第 532 行 `schedule.reset()` 是关键；第 538-543 行处理「哈密顿量是序列」（保留 zip），第 544-547 行处理「单个哈密顿量 + 多初态」（复用同一哈密顿量）。

**`Schedule.reset`**：[python/cudaq/dynamics/schedule.py:99-103](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/cudaq/dynamics/schedule.py#L99-L103) — 仅一句 `self._current_idx = -1`，把迭代器拨回起点。

**`Schedule.__next__` 与状态指针**：[python/cudaq/dynamics/schedule.py:108-114](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/cudaq/dynamics/schedule.py#L108-L114) — 每次 `__next__` 先 `_current_idx += 1` 再取步，越界抛 `StopIteration`。`_current_idx` 初始化为 -1（[schedule.py:45](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/cudaq/dynamics/schedule.py#L45)），所以「未开始」和「已结束」都表现为越界。

**物化 schedule 的地方**：[python/cudaq/dynamics/evolution.py:299-301](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/cudaq/dynamics/evolution.py#L299-L301) — `evolve_single` 里 `parameters = [mapping for mapping in schedule]` 与 `schedule.reset(); tlist = [...]`，说明单次 `evolve_single` 自身会消费并重置 schedule；但在「循环多次调 `evolve_single`」的场景里，对象是同一个 schedule，必须由调用方在每轮前 reset（这正是 `evolve_one` 做的）。

**回归测试**：`python/tests/dynamics/test_evolve_simulators.py` 中的 `test_evolve_reuses_single_hamiltonian_for_batched_initial_states`，用 `@pytest.mark.parametrize("target", ["qpp-cpu", "density-matrix-cpu"])` 在两个 CPU 目标上各跑一次，断言 `len(results) == 2`，且 `results[0]`/`results[1]` 分别等于「单独用 `zero_state`/`one_state` 演化」的期望末态。`density-matrix-cpu` 的初态被构造成密度矩阵 `np.outer(x, conj(x))`（见 4.4.4）。

#### 4.4.4 代码实践

**实践目标**：复现并验证 PR #4835 的修复——「同一哈密顿量 + 一组初态」应返回每个初态的独立结果，并对比两个 CPU 目标。

1. 确认你的环境装了 cudaq；新建 `batched_evolve_check.py`（**示例代码**，直接照搬回归测试的思路）：

   ```python
   # 示例代码
   import numpy as np
   import cudaq
   from cudaq import spin, Schedule

   def run(target):
       cudaq.reset_target()
       cudaq.set_target(target)
       dt = 0.1
       dimensions = {0: 2}
       zero = np.array([1.0, 0.0], dtype=np.complex128)
       one  = np.array([0.0, 1.0], dtype=np.complex128)
       # density-matrix-cpu 的模拟器名是 "dm"，需要密度矩阵初态
       if target == "density-matrix-cpu":
           zero = np.outer(zero, np.conj(zero))
           one  = np.outer(one, np.conj(one))
       s0 = cudaq.State.from_data(zero)
       s1 = cudaq.State.from_data(one)
       sched = Schedule([0.0, dt], ["time"])

       # 基线：单独演化
       exp0 = cudaq.evolve(spin.x(0), dimensions, sched, s0)
       exp1 = cudaq.evolve(spin.x(0), dimensions, sched, s1)

       # 批处理：同一哈密顿量 + 一组初态
       res = cudaq.evolve(spin.x(0), dimensions, Schedule([0.0, dt], ["time"]),
                          [s0, s1])

       assert isinstance(res, list) and len(res) == 2, f"{target}: 返回了 {res!r}"
       np.testing.assert_allclose(res[0].final_state().to_numpy(),
                                  exp0.final_state().to_numpy(), atol=1e-12)
       np.testing.assert_allclose(res[1].final_state().to_numpy(),
                                  exp1.final_state().to_numpy(), atol=1e-12)
       print(target, "OK — 批处理返回了", len(res), "个独立结果")

   for t in ["qpp-cpu", "density-matrix-cpu"]:
       run(t)
   ```

2. 运行 `python batched_evolve_check.py`。
3. **需要观察的现象**：两个目标都打印 `OK — 批处理返回了 2 个独立结果`。如果你把代码换成修复前的写法（手动 `zip(spin.x(0), [s0,s1], [])`），会看到返回 `[]`——那就是 bug。
4. **预期结果**：`res[0]` 与单独演化的 `exp0` 数值一致（`|0>` 在 `σx` 下演化 dt 的末态），`res[1]` 与 `exp1` 一致。`density-matrix-cpu`（`dm`）与 `qpp-cpu` 给出等价的末态（一个以密度矩阵表示，一个以态向量表示）。**待本地验证**。
5. **可选进阶**：把脚本里 `hamiltonian` 换成「一组哈密顿量」`[spin.x(0), spin.z(0)]` 配 `[s0, s1]`，验证「多哈 + 多态」的 zip 配对路径仍然正常（这部分行为未被本次修复改动）。

#### 4.4.5 小练习与答案

**练习 1**：如果不调用 `schedule.reset()`，第二个初态的演化会怎样？

**答案**：第一个 `evolve_single` 已经把 schedule 的 `_current_idx` 推到末尾（物化为 `parameters` 列表时消费完毕）。第二个 `evolve_single` 再迭代同一个 schedule 时立即 `StopIteration`，`parameters`/`tlist` 为空，演化没有时间步，要么报错要么给出无意义/空结果。`reset()` 把指针拨回 -1 是让每个初态都走完整条时间轴的必要操作。源码：[schedule.py:99-103](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/cudaq/dynamics/schedule.py#L99-L103)。

**练习 2**：同样的「单哈 + 多初态」调用，为什么 `dynamics` 目标在修复前也不会返回空列表？

**答案**：`dynamics` 目标走的是 C++ `evolveBatched`（[evolve.h:269-292](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/cudaq/algorithms/evolve.h#L269-L292)），它把多个初态打包进一个批处理密度矩阵，由 `evolveBatchedImpl`（[CuDensityMatEvolution.cpp:343](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/nvqir/cudensitymat/CuDensityMatEvolution.cpp#L343)）一次积分后拆批，根本不经过有 bug 的那个 Python `zip` 分支。bug 只存在于「非 dynamics 目标 + Python 循环」路径。

**练习 3**：为什么 `density-matrix-cpu` 的初态要构造成 `np.outer(x, np.conj(x))` 而 `qpp-cpu` 不用？

**答案**：`density-matrix-cpu` 的模拟器名是 `dm`（[density-matrix-cpu.yml:12](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/nvqir/qpp/density-matrix-cpu.yml#L12)），它以密度矩阵为内部表示；`evolution.py` 的 `_canonicalize_initial_state` 检测到 `simulator == "dm"` 时会主动把态向量包成 `outer(x, conj(x))`（[evolution.py:89-93](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/python/cudaq/dynamics/evolution.py#L89-L93)）。回归测试里手工构造密度矩阵是为了显式控制初态形态；无论哪种方式，最终在 `dm` 后端上都是密度矩阵。

## 5. 综合实践

把本讲四个模块串起来，完成一个「含时驱动 + 多初态扫描」的小任务：

1. **建系统**：一个 qubit 在含时外场下演化，\( H(t) = \frac{\Omega}{2}\sigma_x + \frac{\Delta}{2}\sigma_z \)，\(\Omega=1.0\)、\(\Delta=0.5\)。用 `spin` 算符构造（参数化或直接常量均可）。
2. **定 schedule**：`steps = np.linspace(0, 10, 101)`，`Schedule(steps, ["time"])`。
3. **单态演化 + 画曲线**（对应 4.1）：初态 `|0>`，用 `evolve` 配 `observables=[spin.z(0)]` 与 `IntermediateResultSave.EXPECTATION_VALUE`，画出 \(\langle\sigma_z\rangle\) 随时间曲线。
4. **换积分器对比**（对应 4.2）：若有 `dynamics` 目标，分别用 `RungeKuttaIntegrator` 与 `MagnusExpansionIntegrator` 重跑，确认曲线一致。
5. **批处理扫描多初态**（对应 4.4）：把初态换成 `[|0>, |1>, (|0>+|1>)/√2]` 三个，一次 `cudaq.evolve(H, dims, schedule, [psi0, psi1, psi2])` 调用，**验证返回长度为 3**，并分别画出三条 \(\langle\sigma_z\rangle\) 曲线。
6. **对比目标**（对应 4.3 与 4.4）：在 `qpp-cpu` 与 `density-matrix-cpu` 上各跑一遍第 5 步，确认两者每个初态的末态等价（一个态向量、一个密度矩阵），且都不返回空列表。

完成这个任务，你就同时用到了：evolve 的时间演化语义、积分器旋钮、dynamics 与非 dynamics 两条后端路径的差异、以及批处理调用的正确用法（含 `schedule.reset()` 的意义）。

## 6. 本讲小结

- `cudaq.evolve` 是主方程求解器，输入含时哈密顿量/塌缩算符/schedule/初态，输出每个时间点的态与期望值；它解的是薛定谔方程（闭）或 Lindblad 主方程（开）。
- evolve 按目标分两条路：`dynamics`（GPU、cuDensityMat、C++ 真正批处理）与非 dynamics（`qpp-cpu`/`density-matrix-cpu`、Python 端把步矩阵相乘、逐状态循环）。
- 三种内置积分器 Runge-Kutta（通用 RK4）、Crank-Nicolson（隐式、稳）、Magnus 展开（保酉、适合闭系统长时演化）共享 `base_integrator` 接口；老头文件 `dynamics_integrators.h` 已废弃，改用 `cudaq/algorithms/integrator.h`。
- `dynamics` 后端由 `CuDensityMatSim` 实现，注册为 `dynamics`，配置在 `dynamics.yml`（需 GPU、定义 `CUDAQ_ANALOG_TARGET`）；其批处理把多个初态打包成批密度矩阵一次积分。
- **PR #4835 修复**：非 dynamics 目标上「单哈密顿量 + 多初态」曾因 `zip(hamiltonian, initial_state, collapse_operators)` 在空 `collapse_operators` 下产出 0 项而**静默返回空列表**；修复改为对每个初态复用同一哈密顿量，并在每次 `evolve_single` 前 `schedule.reset()`。
- `Schedule` 是有状态迭代器，`_current_idx` 在物化/迭代后会被推到末尾，因此「同一 schedule 多次复用」必须 `reset()`——这是理解该修复的关键。

## 7. 下一步学习建议

- **想深入动力学后端实现**：阅读 `runtime/nvqir/cudensitymat/` 全目录，重点看 `CuDensityMatOpConverter.cpp`（算符→cuDensityMat 算子）、`CuDensityMatState.cpp`（批密度矩阵与 `splitBatchedState`）、`RungeKuttaIntegrator.cpp`/`MagnusIntegrator.cpp`/`CrankNicolsonIntegrator.cpp`（积分器实现）。
- **想跑更多动力学例子**：`docs/sphinx/examples/python/dynamics/` 下的 notebook（Jaynes-Cummings、离子阱、超导、自旋链、LZ 跃迁）覆盖了闭系统、开系统（塌缩算符）、多体耦合等典型场景。
- **想理解算符如何喂给 evolve**：复习 u3-l5（算符代数），尤其是 `scalar_operator`（含时系数）、`boson`/`spin` 工厂、`.to_matrix()` 与 `.evaluate()`。
- **想对比「门采样」与「动力学」**：把本讲与 u3-l1/u3-l3（执行模型、observe）对照，体会 CUDA-Q 在同一运行时上同时支持「离散门」与「连续时间演化」两种量子计算范式的设计。
- **后续讲义衔接**：本讲是单元 7 的首篇；接下来 u7-l2 介绍 Realtime 子系统（FPGA/GPU 紧耦合的低延迟控制），u7-l3/u7-l4 讲如何扩展新后端与自定义门——后者正是理解「为什么 evolve 在非 dynamics 目标上能用 `register_operation` 注册自定义步矩阵」的钥匙。
