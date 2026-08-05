# SimX↔RTL 模型一致性（model parity）

## 1. 本讲目标

Vortex 有两套互相独立的实现来完成同一件事——执行 RISC-V GPGPU 程序：一套是用 C++ 写的周期近似仿真器 **SimX**，另一套是用 SystemVerilog 写的 **RTL**（经 Verilator 编译成 `rtlsim`）。本讲要回答一个核心问题：**项目如何保证这两套实现始终在做「同一件事」？**

学完后你应当掌握：

1. 理解为什么 SimX 不只是一个功能性参考实现，而是 **RTL 的时序模型（timing model）**，两者必须保持功能与时序同步（lockstep）。
2. 掌握 `model_parity` 这道 CI 门控的**判定标准**：同一份 app/args/configs 在 simx 与 rtlsim 上跑两遍，**退休指令必须精确相等**，**周期数必须在容差内一致**。
3. 了解一条铁律：**绝不能靠放宽容差（tolerance）来吸收两者的差异**——差异必须靠「把行为建进模型」来消除，而不是靠调参掩盖。

本讲是「核心流水线 RTL」单元的收尾讲，承接 u7-l3（RTL 调度器与 warp 控制）和 u6-l4（SimX 功能单元）。它的作用是把前面几讲反复强调的「SimX 与 RTL 逐模块对应」从一个口头主张，变成一道**机器强制执行的纪律**。

---

## 2. 前置知识

阅读本讲前，你应当已经了解：

- **SimX 与 RTL 的双实现关系**（见 u5-l3、u6-x、u7-x 系列）：Vortex 先在 SimX 上原型化，再把设计前推到 RTL，两者描述同一个微架构。
- **6 级流水线骨架** Schedule→Fetch→Decode→Issue→Execute→Commit，以及它在 SimX（如 `sim/simx/scheduler.cpp`）与 RTL（如 `hw/rtl/core/VX_scheduler.sv`）中各有一份同名实现。
- **基数规则**（u5-l3）：SimX 模块只通过 channel 通信，这是它能忠实建模连线时序的前提。
- **退休指令计数器 `MINSTRET` 与周期计数器 `MCYCLE`**：这是 RISC-V 机器模式 CSR，Vortex 用它们做性能度量，也是 model_parity 比较的两个数字的源头。

几个本讲会用到的术语：

- **退休（retire）**：一条指令走过流水线末级、真正改变了体系结构状态，称为「退休」。`MINSTRET` 统计的就是退休指令数。
- **容差（tolerance）**：允许的相对误差，本讲默认是 5%（0.05）。
- **lockstep（步调一致）**：两个模型在任何时刻对同一个输入产生同样的功能结果、并且周期数足够接近。

> 一个关键直觉：**指令数是功能问题，周期数是时序问题。** 指令数不一致，意味着两边对「该执行哪条指令」看法不同，是功能分歧（functional divergence）；周期数不一致但指令数一致，意味着两边对「执行这条指令要花多少拍」看法不同，是时序建模的精度问题。model_parity 把这两类问题分别对待。

---

## 3. 本讲源码地图

本讲涉及的文件分为三组：**规则文档**、**CI 引擎实现**、**被比较的数字来源**。

| 文件 | 作用 |
|------|------|
| [AGENTS.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md) §4 | 项目纪律的「宪法」。第 4 节明文规定 SimX 必须与 RTL 保持 lockstep，由 `model_parity` 门控兜底。 |
| [docs/designs/continuous_integration.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/continuous_integration.md) §3.3/§3.4 | CI 架构设计文档，§3.3 专门描述 `check: model_parity` 的语义。 |
| [docs/debugging.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/debugging.md) | 调试手册，其中「SimX as Oracle」一节是 lockstep 纪律在调试场景下的操作化。 |
| [ci/test_runner.py](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py) | model_parity 的**判定逻辑**所在：`_model_parity()` 函数跑两遍、比指令数、比周期。 |
| [ci/testcase.py](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py) | 测试用例的数据模型 `Spec`，定义了默认容差 `0.05` 与各种约束。 |
| [ci/testcases/model_parity.yaml](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml) | model_parity 的**测试数据**：通用流水线（vecadd/sgemm/stencil3d 等）的对齐用例。 |
| [ci/conftest.py](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/conftest.py) | pytest 胶水：把 `known_issue` 转成 `xfail`、按 build-key 共享一次 sim 编译。 |
| [sw/runtime/common/perf.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp) | 被比较的 `PERF: instrs=…, cycles=…` 汇总行的**产生处**，数字来自 `MINSTRET`/`MCYCLE` CSR。 |

> 记忆线索：**文档说「为什么」→ continuous_integration.md 说「怎么设计」→ test_runner.py 真正「做判定」→ testcase.py/yaml 提供「数据」→ perf.cpp 提供「被比较的数字」。** 这条链就是本讲的全部脉络。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应规格要求的两块（AGENTS.md §4 测试规则、continuous_integration model_parsity），并把「判定标准」与「容差纪律」单列出来讲透。

### 4.1 为什么需要 model parity：SimX 是 RTL 的时序模型

#### 4.1.1 概念说明

很多项目里，「仿真器」只是一个跑得快、用来验证功能正确性的参考实现——它只管「结果对不对」，不管「花了多少拍」。Vortex 对 SimX 的要求更高：**SimX 既是功能 oracle，又是 RTL 的时序模型。** 也就是说，SimX 不仅要算出和 RTL 一样的结果，还要在「每条指令大约花多少周期」上和 RTL 足够接近。

为什么非要让一个 C++ 模型去追时序？因为 Vortex 的开发节奏是「**先在 SimX 原型化，再前推到 RTL**」：

- SimX 用 C++ 写，几秒钟编译、几秒到几十秒跑完一个 kernel，还能用普通 GDB 单步调；
- RTL 用 SystemVerilog 写，Verilator 编译要几分钟、跑一个 kernel要几十分钟到几小时。

如果 SimX 只管功能、不管时序，那么一旦 RTL 的某个仲裁逻辑或队列深度变了、把周期数挪了几千拍，SimX 完全察觉不到——两边就**悄悄分叉**了。等哪天有人想拿 SimX 当快速参考来调试 RTL，却发现它早已不代表 RTL 的真实行为，这个「快」就毫无意义。

所以项目立了一条规矩：**任何挪动 RTL 周期的改动（流水线结构、仲裁、队列深度、cache/内存行为），必须连同对应的 SimX 时序模型更新一起提交，反之亦然。** 这条规矩写在 AGENTS.md §4，由 model_parity 门控机械执行。

#### 4.1.2 核心流程

lockstep 这条规矩的执行回路可以画成：

```text
   改 RTL 流水线/仲裁/cache
              │
              ├──▶ 必须同步改 SimX 时序模型   （人工纪律）
              │
              └──▶ CI 跑 model_parity 用例    （机器兜底）
                       │
            同一 app/args/configs 各跑一遍
                       │
        ┌──────────────┴───────────────┐
        ▼                              ▼
    simx 一腿                      rtlsim 一腿
   (C++ 周期近似)                (Verilator 周期精确)
        │                              │
        └──────────┬───────────────────┘
                   ▼
         比较 PERF: instrs / cycles
                   │
      instrs 必须完全相等？  cycles 在 5% 内？
         任一不满足 ──▶ CI 红，禁止合入
```

注意回路里有两道防线：第一道是**人的纪律**（提交前自觉同步两套模型），第二道是**机器的断言**（CI 用 model_parity 兜底）。文档反复强调第二道存在的意义就是——不能假设人不会忘。

#### 4.1.3 源码精读

lockstep 规矩的原文在 AGENTS.md 第 4 节，关键一句：

> SimX is the RTL's timing model — keep them in lockstep.

这段话同时给出了三件事：定位（SimX 是 RTL 的时序模型）、纪律（必须 lockstep）、兜底（由 `model_parity` CI 门控强制）。见：

- [AGENTS.md:89](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L89) — 这一行把「改动必须成对提交」「门控判定标准（退休指令精确一致 + 周期默认 5% 容差）」「不许放宽容差」「新增硬件特性要配 parity 用例」四件事压成一段。本讲几乎是在逐句展开这一行。

紧跟着的第 90 行给出了 lockstep 在**调试**场景下的操作版本——「SimX-as-oracle」模式：

- [AGENTS.md:90](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L90) — 当 RTL 调试卡住（数值错、深流水线竞争、rtlsim「差一点但错了」）时，先把 SimX 改到能模拟**新的 RTL 结构**并让它 PASS，再给两边加同样格式的 trace，diff 出第一处分歧。

这条调试纪律是 4.1.1 节直觉的镜像：**既然 SimX 是 RTL 的模型，那么 RTL 错了，最快的定位办法就是问模型「你这一拍在干嘛」，然后看 RTL 哪一拍偏离了模型。**

#### 4.1.4 代码实践

> **实践目标**：用你自己的话把 lockstep 这条纪律复述出来，并解释「为什么 RTL 流水线改动必须同时更新 SimX 时序模型」。

操作步骤：

1. 打开 [AGENTS.md §4](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L72)，通读第 89 行所在段落。
2. 设想一个具体改动场景：假设你把 `hw/rtl/core/VX_issue.sv` 里某个功能单元的派发信用（fu credit）从 2 改成 3，意味着该 FU 能多容纳一条在途指令。
3. 回答下面三个问题（写下来）：
   - 这个改动会**挪动 RTL 周期数**吗？为什么？（提示：更多在途指令 → 更少派发停顿 → 总周期可能下降。）
   - 如果**只改 RTL、不改 SimX**，model_parity 门控会怎么反应？（提示：指令数不变，但 simx 周期会偏高。）
   - 按照 lockstep 纪律，这次提交里还必须改哪个目录下的什么文件？

需要观察的现象 / 预期结果：

- 你应当得出结论：改动 RTL 的仲裁/容量会移动周期数，所以**必须同步**改 `sim/simx/` 下对应的时序建模（比如对应 FU 的 `latency_of` 或派发信用）。否则 model_parity 的周期断言会红。
- 待本地验证：如果你本地有 build 树，可以试着只改 RTL 跑一次 `pytest ci -m "model_parity and rtlsim"`，观察 `PARITY:` 行里 simx 与 rtlsim 的周期差是否超过 5%。

#### 4.1.5 小练习与答案

**练习 1**：如果 SimX 仅仅是一个「功能 oracle」（只保证结果对、不管周期），model_parity 还能成立吗？为什么？

> **参考答案**：不能。model_parsity 的周期断言（5% 容差）直接假设 SimX 是 RTL 的时序模型。如果 SimX 不追时序，它的周期数就和 RTL 没有可预期关系，周期断言会随机红，门控就退化成「只比指令数」，失去了时序保真的意义。

**练习 2**：「SimX 是 RTL 的时序模型」这句话和基数规则（模块只通过 channel 通信）有什么联系？

> **参考答案**：正是因为模块间只通过 channel 通信，channel 又自带延迟与背压（见 u5-l1），SimX 才能把「连线代价」忠实表达出来。如果允许跨层级直接读写 DRAM 后备存储，就绕过了被建模的 cache/NoC 路径，时序就不准了——时序模型的前提就塌了。

---

### 4.2 model_parity 门控的判定标准

#### 4.2.1 概念说明

model_parity 是 Vortex CI 里一类特殊的测试用例，用 `check: model_parity` 字段标记。它和普通测试用例最大的区别是：**普通用例在一个 driver 上跑一遍、断言退出码为 0；model_parity 用例在 simx 和 rtlsim 上各跑一遍，断言两次的度量值吻合。**

判定标准有两条，一条硬、一条软：

1. **退休指令数必须精确相等**（硬）。simx 和 rtlsim 都是确定性的 ISA 级执行——给定同一份程序、同一份配置，它们「该退休哪些指令」是唯一确定的。所以只要指令数对不上，就一定是**功能分歧**，绝不是时序问题，必须修。
2. **周期数必须在容差内一致**（软，默认 5%）。SimX 是「周期近似」（cycle-approximate）模型，不要求逐拍精确，但要求总体吻合。容差就是为「SimX 没有逐拍建模但宏观影响很小」的细节留的余量。

为什么指令数用「相等」、周期数用「容差」？因为两者确定性来源不同：指令数由 ISA 语义唯一决定（强约束），周期数由微架构实现的精细程度决定（弱约束，SimX 总比 RTL 简化）。

#### 4.2.2 核心流程

一个 `check: model_parity` 用例的生命周期：

```text
   YAML 里写 check: model_parity
              │
              ▼   (testcase.py load_category)
   不做 driver 展开，pin 到 rtlsim 驱动（因为它要 elaborate RTL）
              │
              ▼   (conftest.py) 挂上 model_parity marker + rtlsim marker
              │
              ▼   (test_runner.test_case) 发现 case.check == "model_parity"
              │
              ▼   _model_parity():
   ┌──────────────────────────────────────┐
   │  _run_one(simx)   → (instrs_s, cyc_s) │   跑 simx，解析 PERF 行
   │  _run_one(rtlsim) → (instrs_r, cyc_r) │   跑 rtlsim，解析 PERF 行
   └──────────────────────────────────────┘
              │
              ▼   gap = |cyc_r - cyc_s| / cyc_r
              │
   断言 1: instrs_s == instrs_r          （精确相等）
   断言 2: gap <= case.tolerance          （默认 0.05）
              │
   打印 PARITY: ... gap=..% (tolerance 5%)   （留趋势痕迹）
```

这里有两个值得注意的设计：

- **用例不被 driver 展开**。普通用例写 `drivers: [simx, rtlsim]` 会变成两个独立用例；model_parity 用例是**一个用例跑两条腿**（two legs of one case），因为两条腿的结果要放在一起比，不能各跑各的。
- **pin 到 rtlsim**。因为只有 rtlsim 会 elaborate RTL，把它放在 rtlsim 的矩阵格（cell）里，build/matrix 归位才正确；simx 那条腿由 runner 自己驱动。

#### 4.2.3 源码精读

**默认容差与判定逻辑**。先看容差常量和判定函数。`ci/testcase.py` 定义默认容差：

- [ci/testcase.py:40-43](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L40-L43) — `VALID_CHECK = {"model_parity", "perf_gate"}` 把这两类「跨 driver / 跨提交」的检查单列；`DEFAULT_PARITY_TOLERANCE = 0.05` 就是「默认 5%」的出处。

判定本体在 `ci/test_runner.py` 的 `_model_parity`：

- [ci/test_runner.py:47-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L47-L60) — 这是整道门控的心脏。关键几行（示意，非逐字）：

  ```python
  simx_instrs, simx_cycles = _run_one(case, xlen, "simx")
  rtl_instrs, rtl_cycles   = _run_one(case, xlen, "rtlsim")
  gap = abs(rtl_cycles - simx_cycles) / float(rtl_cycles)
  assert simx_instrs == rtl_instrs, "...功能分歧，不是时序问题"
  assert gap <= case.tolerance, "...周期差距超过容差"
  ```

  注意 gap 的分母是 `rtl_cycles`（RTL 为基准），且两条 assert 的报错文案刻意区分「功能分歧」与「时序差距」——这是 4.2.1 节那条直觉在代码里的体现。

**两条腿怎么跑**。`_run_one` 对 simx 与 rtlsim 完全对称，都是跑一遍、用正则抠出 `PERF` 行：

- [ci/test_runner.py:31](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L31) — `_PERF_RE = re.compile(r"^PERF: instrs=(\d+), cycles=(\d+), IPC=", re.M)`，这是它「抠数字」的方式。注意正则只匹配**不带 `coreN:` 前缀的设备级汇总行**，因为每核行有前缀、不会误匹配。
- [ci/test_runner.py:34-44](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L34-L44) — `_run_one` 先断言退出码为 0（程序本身得跑通），再断言输出里**有** `PERF` 汇总行（否则没法比），最后取最后一组匹配的 `instrs/cycles` 返回。

**被比较的数字从哪来**。这是理解「指令数为什么能精确相等」的关键。`PERF: instrs=…, cycles=…` 这行由运行时 `perf.cpp` 打印，数字直接来自两个 RISC-V CSR：

- [sw/runtime/common/perf.cpp:287-291](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp#L287-L291) — 每个核读 `VX_CSR_MCYCLE`（周期）和 `VX_CSR_MINSTRET`（退休指令）；`instrs` 是**所有核的退休指令之和**，`cycles` 是**所有核周期的最大值**（取 max 而非 sum，因为多核并行执行，墙钟周期由最慢的核决定）。
- [sw/runtime/common/perf.cpp:776-777](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp#L776-L777) — 最终打印 `instrs=<total>, cycles=<max>, IPC=<…>`，正是 `_PERF_RE` 匹配的那一行。

> 把这条链串起来：**RTL/SimX 各自维护 `MINSTRET`/`MCYCLE` CSR → 运行时 `perf.cpp` 读它们并打印 `PERF:` 行 → `test_runner.py` 用正则抠出两个数字 → `_model_parity` 比指令数相等、比周期数容差。** 因为两边读的是同一个 ISA 规定的 CSR，所以「指令数精确相等」才是一个有意义的硬断言——它本质上是在说「两边退休了同样多条指令」。

**用例数据长什么样**。`ci/testcases/model_parity.yaml` 是通用流水线的 parity 用例集：

- [ci/testcases/model_parity.yaml:35-46](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml#L35-L46) — `vecadd`（ALU/LSU/branch 流式，约 314k 周期）和 `sgemm`（FPU + 多 warp 复用，约 1.29M 周期）。注意它们都**带较大的 `-n` 参数**，因为文档明确要求「工作负载要大到稳态占主导（≥~300k 周期）」——太小的 kernel 全是启动/派发偏斜，gap 比例会变得很噪。
- [ci/testcases/model_parity.yaml:23-33](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml#L23-L33) — defaults 段把 `xlen` 钉在 32（SimX 时序模型只对 RV32 的 rtlsim 校验过，RV64 不在此门控）、`tier: full`（rtlsim 重，只在 PR + nightly 跑）、`touches: [sim/simx/, hw/rtl/]`（任何核心模型或 RTL 改动都会挪动周期，所以全量扫）。

#### 4.2.4 代码实践

> **实践目标**：亲手跑（或至少读懂）一个 model_parity 用例，看清 `PARITY:` 行的两个数字与 gap。

操作步骤：

1. 进入你的 build 树（假设 `build32/`，XLEN=32）。若没有树，参考 u1-l3 先 `../configure --xlen=32` 并 `make -s`。
2. 运行最轻的 parity 用例：
   ```bash
   VX_XLEN=32 pytest ci -m "model_parity and rtlsim" -k vecadd --strict-markers
   ```
   （`-k vecadd` 只挑 vecadd 这一个用例，避免把整组跑完。）
3. 在输出里找 `PARITY:` 开头的那一行，它形如：
   ```
   PARITY: model_parity:vecadd:rtlsim: instrs simx=482756 rtlsim=482756, cycles simx=150340 rtlsim=147902, gap=1.63% (tolerance 5%)
   ```
   （上面的数字是示例，来自 `docs/simulation.md` 的样例，**不是**一次真实 vecadd parity 跑的结果——真实数字请以本地输出为准。）

需要观察的现象：

- **instrs 两个数字应当完全相同**（这是硬断言）。如果不同，pytest 会报「retired-instruction mismatch — functional divergence」。
- **gap 应当 ≤ 5%**。如果超过，pytest 会报「cycle gap … exceeds tolerance」。

预期结果：用例 PASS，且日志里留下一行 `PARITY:` 趋势痕迹（绿色运行也打这行，方便长期观察 gap 漂移）。如果本地无法运行 rtlsim（Verilator 太慢或缺工具链），**待本地验证**——此时改做下面的源码阅读型实践：

- 阅读 [ci/test_runner.py:47-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L47-L60)，确认两条 assert 的判定顺序与报错文案；再阅读 [sw/runtime/common/perf.cpp:287-291](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp#L287-L291)，确认 `instrs` 是「求和」、`cycles` 是「求最大」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cycles` 取所有核的 `max` 而不是 `sum`，而 `instrs` 却取 `sum`？

> **参考答案**：多核是**并行**执行的，墙钟时间由最慢的核决定，所以周期取 max 才反映「整个程序跑了多久」。而退休指令是**累积量**——每个核各退各的，总工作量是各核之和，所以 instrs 取 sum。

**练习 2**：model_parity 用例为什么「不被 driver 展开」，而是 pin 到 rtlsim？

> **参考答案**：因为两条腿（simx 和 rtlsim）的结果必须在**同一个测试函数里**放在一起比较，不能拆成两个独立用例各跑各的。同时只有 rtlsim 会 elaborate 出 RTL，把它 pin 到 rtlsim 驱动，CI 的 build/matrix 归位才正确（放在 rtlsim 的格里编译 RTL），simx 那条腿由 `_model_parity` 自己用 `_run_one(..., "simx")` 驱动。

**练习 3**：如果工作负载只有 1000 周期，model_parity 的 gap 断言会变得怎样？

> **参考答案**：会变得很「噪」、不可靠。因为小 kernel 里启动（boot）和 CTA 派发（dispatch）的偏斜占大头，SimX 与 RTL 在这些一次性事件上的建模差异会被放大成很高的 gap 比例，掩盖真实的稳态时序。所以 yaml 里所有用例都把 `-n` 调大到让稳态占主导（≥~300k 周期）。

---

### 4.3 「绝不能放宽容差来吸收差异」的纪律

#### 4.3.1 概念说明

5% 的容差很容易被误用成「挡箭牌」：只要 simx 与 rtlsim 的周期差超过了 5%，最省事的做法就是把那个用例的 `tolerance` 改成 10%、20%，让门控转绿。Vortex 明确禁止这种做法。

理由很简单：**容差吸收的是「已建模但建模不精确」的宏观细节，不是「根本没建模」的特性。** 如果 RTL 新加了一个解耦的 LSU pending pool（一种访存延迟缓冲），而 SimX 还没建模它，那么两边周期差可能到 14%、28%——这是「特性缺失」，不是「精度不足」。把容差调到 30% 等于宣布「我们承认两边不一致，并且打算永远不一致下去」，lockstep 纪律就名存实亡了。

项目的正确做法分两种情况：

- **已建模、只是精度问题** → 可以用容差，甚至可以临时调宽 `tolerance` 字段，但要在注释里写清「为什么这个用例的 SimX 模型确实追不到 5%」。
- **根本没建模的特性缺失** → **不许动容差**，改用 `known_issue:` 字段把它标记成一个「正在追踪的已知偏差（tracked divergence）」，并写明原因。这样它仍然在 CI 里跑、仍然报告 gap，只是失败不阻塞合入；等 SimX 把那个特性补上，偏差自然消失，known_issue 也能被 `XPASS`（意外通过）提醒移除。

注意 model_parity 容差（5%）和性能基线容差（perf_gate，±2%）是两套不同门控、不同语义，不要混。

#### 4.3.2 核心流程

遇到一个 parity 周期超差的决策树：

```text
        model_parity 周期 gap > 容差
                  │
   是功能分歧（instrs 不等）吗？
        ├── 是 ──▶ 这是 bug，去修 RTL 或 SimX，绝不许碰容差
        └── 否（instrs 相等，仅周期差）
                  │
            SimX 有没有建模相关特性？
        ├── 有，只是精度问题 ──▶ 可保留/微调 tolerance，注释说明
        └── 没有（特性缺失）   ──▶ 加 known_issue: "<原因>"，
                                   去补 SimX 模型，绝不调宽 tolerance
```

`known_issue` 不是「关掉用例」。它在 `conftest.py` 里被转成 pytest 的 `xfail`（预期失败）：用例**照常编译、照常运行、照常打印 PARITY 行**，只是它的失败不会让 CI 变红；而且如果哪天偏差意外消失（XPASS），CI 会高亮提醒你「该把这个 known_issue 撤掉了」。

#### 4.3.3 源码精读

**known_issue 的真实案例**。`ci/testcases/model_parity.yaml` 里有三个用例就是特性缺失、靠 `known_issue` 追踪，而非调宽容差：

- [ci/testcases/model_parity.yaml:56-64](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml#L56-L64) — `sgemv` 的 `known_issue` 明说「周期差 ~14%：SimX 没有建模解耦的 LSU pending pool（指令数仍然相等）」。注意措辞：它**特别声明 instrs 仍精确匹配**，把问题锁定在「时序建模缺失」，而不是功能错。
- [ci/testcases/model_parity.yaml:66-78](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml#L66-L78) — `sgemm-mc` 的 `known_issue` 是全文件最宽的缺口（~28%），原因是两个未建模效应叠加（解耦 LSU pending pool + 重做过的 cache bank 流水线）。即便这么宽，**也没有去碰 tolerance**，而是老实承认「pending SimX LSU/cache 时序对齐」。

这两个案例是「纪律」的活样本：宁可挂着 known_issue、让缺口在日志里天天可见，也不把容差偷偷调到 30%。

**known_issue 怎么生效**。在数据模型里它只是一个字符串，由 conftest 转成语义化的 `xfail`：

- [ci/testcase.py:73](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L73) — `self.known_issue = entry.get("known_issue", ...)`，落到 `Spec` 的一个字段。
- [ci/conftest.py:59-65](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/conftest.py#L59-L65) — 把非空 `known_issue` 包成 `pytest.mark.xfail(reason=..., strict=False)`。`strict=False` 意味着「意外通过（XPASS）」不会变成硬失败，而是温和地浮现出来提醒你。

**容差的边界**。`testcase.py` 的 lint 还对容差做了静态约束，防止写出一个无意义的容差：

- [ci/testcase.py:375-384](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L375-L384) — lint 检查里有一条 `if not (0.0 < c.tolerance < 1.0): error`，即容差必须是 (0,1) 之间的分数。这挡住了「写 tolerance: 1.0 让所有差距都通过」这种赤裸裸的放水，但它挡不住「写 0.9」这种较隐蔽的放水——所以真正的防线还是文档里的纪律和 code review。

**对照：perf_gate 的容差纪律**。model_parity 的「不许放水」和 perf_gate 如出一辙，可以互相对照理解：

- [AGENTS.md:88](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L88) — perf 基线是 golden data，永不手改、永不靠 bump 数字来「修」红门控；要更新只能由人跑 `--update-baselines` 并 review JSON diff。CI 永远不许传这个 flag。model_parity 的容差也是同样的精神：**差异要建模，不要调参吸收。**

#### 4.3.4 代码实践

> **实践目标**：把「调宽容差」和「加 known_issue」两条路对照一遍，判断哪条符合纪律。

操作步骤：

1. 打开 [ci/testcases/model_parity.yaml:56-78](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml#L56-L78)，读 `sgemv` 与 `sgemm-mc` 的 `known_issue` 原文。
2. 假设你是代码评审者，看到有人提了一个 PR：为了「修」`sgemm-mc` 的 parity 红，把它的 `tolerance` 改成了 `0.30`，并删掉了 `known_issue`。请写出你的 review 意见，要点包括：
   - 这个改动是否合规？（不合规。）
   - 正确的做法是什么？（保留 known_issue，去 `sim/simx/` 补上「解耦 LSU pending pool」与「cache bank 流水线」的时序建模。）
   - 为什么不能调宽容差？（会把「特性缺失」伪装成「精度问题」，让两边永久不一致，lockstep 失效。）
3. 进阶：在 [docs/designs/continuous_integration.md:174-203](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/continuous_integration.md#L174-L203) 找到一句明确的话「Use `known_issue:` (not a loosened tolerance) for a tracked gap under investigation」，把它抄进你的 review 意见当依据。

需要观察的现象 / 预期结果：你能清楚说出「容差吸收精度、known_issue 追踪缺口」的区分，并能引用 yaml 里的真实案例与文档原文。如果本地有 build 树，可额外跑 `pytest ci -m "model_parity and rtlsim" -k sgemv` 观察 known_issue 用例的 `xfail` 标记与 PARITY 行；**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：model_parity 默认容差是 5%，perf_gate 容差是 ±2%。为什么 perf_gate 更严？

> **参考答案**：perf_gate 比的是「本次提交 vs 一份 checked-in 的 golden 基线」，两者都是**同一个 rtlsim** 在不同提交上的周期，确定性极高、没有跨模型噪声，所以容差能压到 2%，只吸收良性的、有意的微改动。model_parity 比的是「两个不同模型（SimX vs RTL）」，SimX 是周期近似，天然有跨模型噪声，所以容差宽到 5%。

**练习 2**：什么情况下**允许**给某个 model_parity 用例单独设一个比 5% 更宽的 `tolerance`？

> **参考答案**：当且仅当「SimX 已经建模了相关特性、但客观上追不到 5% 精度」时，可以在用例上写 `tolerance: 0.10` 并在注释里写清原因（yaml 头部的注释给了这个例子）。如果根本没建模，就必须用 `known_issue` 而不是放宽 tolerance。

**练习 3**：`known_issue` 标记的用例失败时不让 CI 变红，那它为什么还要在 CI 里跑？

> **参考答案**：因为它要 (a) 每天打印 `PARITY:` 行，让缺口可见、可追踪趋势；(b) 一旦 SimX 补上了建模、偏差消失，用例会 XPASS，提醒你「该撤掉这个 known_issue 了」。如果直接跳过不跑，缺口就被静默藏起来了。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「**给一个新 RTL 改动配 parity 用例**」的小任务。

**背景**：假设你给 Vortex 新加了一个自定义加速器 FuncUnit（回顾 u6-l4 的 FuncUnit CRTP 骨架与 u14-l3 的扩展思路），它已经同时落地在 `hw/rtl/` 和 `sim/simx/`，功能测试在 simx 上 PASS。

**任务**：

1. **判断要不要加 parity 用例**。阅读 [AGENTS.md:89](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L89) 最后一句「When adding a hardware feature, add or extend a parity case that exercises it」，确认你的新 FuncUnit 需要一个 parity 用例。说明理由（它挪动了 RTL 周期，必须保证 SimX 与 RTL lockstep）。

2. **写最小用例**。参照 [ci/testcases/model_parity.yaml:35-46](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml#L35-L46) 的 vecadd 条目，为你的加速器写一个 `check: model_parity` 用例草稿（写在草稿纸上即可，**不要真的去改仓库里的 yaml**——本讲只读不写源码）。要点：
   - 选一个能充分用到该 FuncUnit 的 app；
   - 把 `args` 调大到让稳态占主导（≥~300k 周期）；
   - 默认沿用 5% 容差，**不要**预先调宽。

3. **预演失败处置**。假设用例跑出来 `instrs` 相等、但 gap = 18%。参照 4.3.2 的决策树，写出你的处置：(a) 判断这是「精度问题」还是「特性缺失」；(b) 若是特性缺失，写一条 `known_issue:` 文案，模仿 [ci/testcases/model_parity.yaml:56-64](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml#L56-L64) 的格式，**特别注明 instrs 仍匹配**；(c) 说明下一步该改 `sim/simx/` 下的什么。

4. **追溯被比较的数字**。最后回到 [sw/runtime/common/perf.cpp:287-291](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp#L287-L291) 和 [ci/test_runner.py:47-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L47-L60)，用一句话说清「你用例里 PARITY 行的那两个 instrs、那两个 cycles，分别是从哪个 CSR、经过哪两步代码走到 `_model_parity` 的 assert 里的」。

预期结果：你能独立说出「新硬件特性 → 必须配 parity 用例 → 失败时建模而非调参 → 数字链路追溯到 CSR」这条完整闭环。

---

## 6. 本讲小结

- **SimX 是 RTL 的时序模型**，不只是功能 oracle；任何挪动 RTL 周期的改动必须连同 SimX 时序模型一起提交（lockstep），反之亦然，规矩见 [AGENTS.md:89](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L89)。
- **model_parity 是一道 CI 门控**：同一个 `check: model_parity` 用例在 simx 与 rtlsim 上各跑一遍，比较运行时打印的 `PERF: instrs=…, cycles=…` 汇总，判定逻辑在 [ci/test_runner.py:47-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L47-L60)。
- **两条判定标准**：退休指令必须**精确相等**（功能硬约束），周期数必须在**容差内**一致（默认 5%，[ci/testcase.py:43](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L43)）。
- **被比较的数字来自 CSR**：`instrs` 是各核 `MINSTRET` 之和、`cycles` 是各核 `MCYCLE` 之最大值，见 [sw/runtime/common/perf.cpp:287-291](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp#L287-L291)。
- **绝不能放宽容差来吸收差异**：精度问题用容差，特性缺失用 `known_issue`（如 [ci/testcases/model_parity.yaml:56-78](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml#L56-L78)），差异要建模、不要调参。
- **调试场景下用 SimX-as-oracle**：RTL 卡住时，先把 SimX 改到模拟新结构并 PASS，再 diff 两边同格式的 trace 找第一处分歧（[AGENTS.md:90](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L90)、[docs/debugging.md:76-93](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/debugging.md#L76-L93)）。

---

## 7. 下一步学习建议

- **进入 U13 测试/调试单元**：u13-l2（调试追踪与 SimX-as-oracle）会展开本讲点到的 trace_csv.py 与 trace diff 调试法，u13-l4（CI 与 model_parity 门控）会从 CI catalog/pytest 框架视角再讲一遍 parity，与本讲互补——本讲侧重「判定标准与纪律」，u13-l4 侧重「门控在 CI 工作流里的位置」。
- **回头看时序建模的源头**：如果你对「SimX 怎么把连线代价建进模型」还有疑问，重读 u5-l1（SimObject/SimChannel/SimPlatform）里「channel 就是流水线」一节，理解 `output.send(trace, latency_of)` 如何承载可变延迟。
- **亲手补一个 parity 案例**：参照综合实践，挑一个现有的 `tests/regression/*` 程序，尝试在草稿上为它写一个 `check: model_parity` 用例，并预演一次「gap 超标 → known_issue → 补 SimX 模型」的完整流程，这是把本讲知识内化最快的方式。
